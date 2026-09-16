"""Supervised OpenSSH connection sharing and exact-file SFTP transfers."""

from __future__ import annotations

import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from threading import BoundedSemaphore, Lock

from .base import (
    CapabilityError,
    DownloadError,
    RemoteConfigurationError,
    Transport,
    literal_path,
    reject_controls,
)


def batch_argument(path):
    """Encode one absolute SFTP argument (command quoting and optional globbing)."""
    text = str(path)
    reject_controls(text, "SFTP path")
    if not text.startswith("/"):
        raise RemoteConfigurationError("SFTP operands must be absolute paths")
    # Use unquoted escapes: OpenSSH adds glob escapes itself inside quotes,
    # so adding backslashes there would instead request literal backslashes.
    special = " \\\"'#?[*"  # ] has no special meaning outside a bracket pattern.
    return "".join("\\" + char if char in special else char for char in text)


# Fixed read-only programs. The server needs a POSIX login shell and python3.
# Paths are operands, never interpolated into program text. Symlinks are omitted.
_LIST_SCRIPT = """import os, stat, sys, time
root, max_entries, max_bytes, seconds = sys.argv[1:]
limit, budget = int(max_entries), int(max_bytes)
deadline = time.monotonic() + int(seconds)
count = size = 0
if not os.path.isdir(root):
    raise RuntimeError("Not a directory")
def fail(error):
    raise error
for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
    dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(directory, d))]
    for name in files:
        path = os.path.join(directory, name)
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            continue
        relative = os.path.relpath(path, root)
        record = (os.fsencode(relative) + b"\\0" + str(info.st_size).encode()
                  + b"\\0" + str(info.st_mtime_ns).encode() + b"\\0")
        count += 1
        size += len(record)
        if count > limit or size > budget or time.monotonic() > deadline:
            raise RuntimeError("Listing limit exceeded")
        sys.stdout.buffer.write(record)
"""

_HASH_SCRIPT = """import hashlib, os, stat, sys
path, size, mtime = sys.argv[1:]
with open(path, "rb") as handle:
    before = os.fstat(handle.fileno())
    if (not stat.S_ISREG(before.st_mode) or before.st_size != int(size)
            or before.st_mtime_ns != int(mtime)):
        raise RuntimeError("File changed")
    digest = hashlib.sha256()
    remaining = int(size)
    while remaining:
        chunk = handle.read(min(65536, remaining))
        if not chunk:
            raise RuntimeError("File shortened")
        remaining -= len(chunk)
        digest.update(chunk)
    after = os.fstat(handle.fileno())
    if (handle.read(1) or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns):
        raise RuntimeError("File changed")
print(digest.hexdigest())
"""


class SshTransport(Transport):
    def __init__(self, context, *, ssh=None, sftp=None):
        super().__init__(context)
        # Injection is internal, for offline process tests; never from repo YAML.
        self.ssh = ssh or ["ssh"]
        self.sftp = sftp or ["sftp"]
        self._injected = ssh is not None or sftp is not None
        self._prepare_lock = Lock()
        self._process_lock = Lock()
        self._processes = set()
        self._master = None
        self._socket_dir = None
        self._socket = None
        self._slots = BoundedSemaphore(context.settings.max_sessions)
        self._entries = {}
        self._hash_lock = Lock()
        self._hash_bytes = 0
        self._hash_started = None

    def _options(self, *, master=False):
        settings = self.context.settings
        policy = "yes" if settings.host_key_policy == "strict" else "accept-new"
        options = [
            "BatchMode=yes",
            f"StrictHostKeyChecking={policy}",
            "ForwardAgent=no",
            "ForwardX11=no",
            "PermitLocalCommand=no",
            "ClearAllForwardings=yes",
            "RequestTTY=no",
            "RemoteCommand=none",
            "ForkAfterAuthentication=no",
            "ControlPersist=no",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=2",
            "ConnectionAttempts=1",
            f"ConnectTimeout={settings.connect_timeout}",
            f"ControlPath={self._socket}",
            "ControlMaster=yes" if master else "ControlMaster=no",
        ]
        if not master:
            # If the owned master dies, never authenticate a replacement per file.
            options.append("ProxyCommand=false")
        if settings.user is not None:
            options.append(f"User={settings.user}")
        if settings.port is not None:
            options.append(f"Port={settings.port}")
        args = [part for option in options for part in ("-o", option)]
        if settings.identity_file is not None:
            args.extend(["-i", settings.identity_file, "-o", "IdentitiesOnly=yes"])
        return args

    def _spawn(self, argv, **kwargs):
        with self._process_lock:
            self.context.check_cancelled()
            try:
                process = subprocess.Popen(
                    argv, shell=False, start_new_session=True, **kwargs
                )
            except OSError:
                raise DownloadError("Unable to start the OpenSSH client") from None
            self._processes.add(process)
        return process

    def _stop(self, process):
        # Every owned client starts a new process group, including proxy children.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=self.context.settings.shutdown_timeout)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        with self._process_lock:
            self._processes.discard(process)

    def _run(
        self,
        argv,
        *,
        timeout,
        data=b"",
        limit=65536,
        destination=None,
        file_limit=None,
        capture_stderr=False,
    ):
        output = bytearray()
        stderr_size = 0
        diagnostics = bytearray()
        with tempfile.TemporaryFile() as batch:
            batch.write(data)
            batch.seek(0)
            process = self._spawn(
                argv, stdin=batch, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            deadline = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    for pipe in (process.stdout, process.stderr):
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, selectors.EVENT_READ)
                    while selector.get_map() or process.poll() is None:
                        self.context.check_cancelled()
                        if time.monotonic() >= deadline:
                            raise DownloadError("SSH operation exceeded its time limit")
                        if (
                            file_limit is not None
                            and destination.exists()
                            and destination.stat().st_size > file_limit
                        ):
                            raise DownloadError("Remote text exceeds its size limit")
                        for key, _ in selector.select(timeout=0.05):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            elif key.fileobj is process.stdout:
                                output.extend(chunk)
                            else:
                                stderr_size += len(chunk)
                                if capture_stderr:
                                    diagnostics.extend(chunk)
                            if len(output) > limit or stderr_size > 65536:
                                raise DownloadError(
                                    "SSH output exceeded its size limit"
                                )
                self.context.check_cancelled()
                if process.wait() != 0:
                    # stderr is untrusted and may contain echoed credentials/paths.
                    # SFTP exits do not reliably distinguish absence from auth errors.
                    raise DownloadError(
                        f"SSH operation failed for {self.context.remote.display}; "
                        "check access, file existence, and server capabilities"
                    )
                return bytes(diagnostics if capture_stderr else output)
            finally:
                self._stop(process)
                process.stdout.close()
                process.stderr.close()

    def prepare(self):
        with self._prepare_lock:
            if self._master is not None:
                if self._master.poll() is not None:
                    raise DownloadError("The owned SSH master disconnected")
                return
            if os.name != "posix":
                raise CapabilityError("SSH transport requires a Linux/macOS client")
            if not self._injected:
                if not shutil.which("ssh") or not shutil.which("sftp"):
                    raise CapabilityError("Install OpenSSH ssh and sftp clients (9.6+)")
                version = self._run(self.ssh + ["-V"], timeout=5, capture_stderr=True)
                match = re.search(rb"OpenSSH_(\d+)\.(\d+)", version)
                if not match or tuple(map(int, match.groups())) < (9, 6):
                    raise CapabilityError("SSH transport requires OpenSSH 9.6 or newer")
            self._socket_dir = tempfile.TemporaryDirectory(prefix="hm-", dir="/tmp")
            os.chmod(self._socket_dir.name, 0o700)
            self._socket = str(Path(self._socket_dir.name) / "s")
            if len(os.fsencode(self._socket)) > 100:
                raise CapabilityError("SSH control socket path is too long")
            try:
                self._master = self._spawn(
                    self.ssh
                    + self._options(master=True)
                    + ["-N", "--", self.context.remote.host],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + self.context.settings.connect_timeout
                while not Path(self._socket).exists():
                    self.context.check_cancelled()
                    if self._master.poll() is not None:
                        raise DownloadError(
                            f"Cannot establish SSH connection to "
                            f"{self.context.remote.display}; check trusted host key, "
                            "loaded key/agent, network and OpenSSH settings"
                        )
                    if time.monotonic() > deadline:
                        raise DownloadError("SSH connection exceeded its time limit")
                    self.context.cancelled.wait(0.05)
                self._run(
                    self.ssh
                    + self._options()
                    + ["-O", "check", "--", self.context.remote.host],
                    timeout=self.context.settings.connect_timeout,
                )
            except BaseException:
                self.close()
                raise

    def _fetch(self, relative_path, destination, file_limit=None):
        remote = batch_argument(self.context.remote.pathname(relative_path))
        local = batch_argument(destination)
        batch = f"get {remote} {local}\n".encode("utf-8")
        if len(batch) > 8000:
            raise RemoteConfigurationError("SFTP command exceeds the client path limit")
        self.prepare()
        host = self.context.remote.host
        if ":" in host:
            host = f"[{host}]"
        with self._slots:
            self._run(
                self.sftp + self._options() + ["-b", "-", "--", host],
                timeout=self.context.settings.transfer_timeout,
                data=batch,
                destination=destination,
                file_limit=file_limit,
            )

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        self._fetch(relative_path, destination)

    def read_text(self, relative_path, limit):
        with tempfile.TemporaryDirectory(prefix="hm-text-") as directory:
            destination = Path(directory) / "content"
            self._fetch(relative_path, destination, file_limit=limit)
            if destination.stat().st_size > limit:
                raise DownloadError("Remote text exceeds its size limit")
            try:
                return destination.read_text(encoding="utf-8")
            except UnicodeError:
                raise DownloadError("Remote manifest must contain UTF-8 text") from None

    def _command(self, script, operands, *, timeout, limit):
        if not self.context.allow_remote_commands:
            raise CapabilityError(
                "SSH builds require --allow-remote-commands, a POSIX shell and "
                "server python3; SFTP-only accounts support downloads only"
            )
        self.prepare()
        command = " ".join(
            shlex.quote(value)
            for value in ["python3", "-c", script, *map(str, operands)]
        )
        with self._slots:
            return self._run(
                self.ssh + self._options() + ["--", self.context.remote.host, command],
                timeout=timeout,
                limit=limit,
            )

    def list_entries(self):
        context = self.context
        data = self._command(
            _LIST_SCRIPT,
            [
                context.remote.root,
                context.listing_limit,
                context.text_limit,
                context.listing_timeout,
            ],
            timeout=context.listing_timeout,
            limit=context.text_limit,
        )
        try:
            fields = data.decode("utf-8").split("\0")
            if fields.pop() != "" or len(fields) % 3:
                raise ValueError
            for i in range(0, len(fields), 3):
                path = literal_path(fields[i]).as_posix()
                size, mtime = int(fields[i + 1]), int(fields[i + 2])
                if size < 0 or path in self._entries:
                    raise ValueError
                self._entries[path] = (size, mtime)
            if len(self._entries) > context.listing_limit:
                raise ValueError
        except (UnicodeError, ValueError):
            raise DownloadError("Invalid or oversized SSH listing") from None
        return list(self._entries)

    def checksum_small(self, relative_path):
        if not self.context.remote_hash:
            return "unknown", "unknown"
        size, mtime = self._entries.get(relative_path, (-1, 0))
        with self._hash_lock:
            if (
                size < 0
                or size > self.context.hash_file_limit
                or self._hash_bytes + size > self.context.hash_total_limit
            ):
                return "unknown", "unknown"
            if self._hash_started is None:
                self._hash_started = time.monotonic()
            remaining = self.context.hash_timeout - (
                time.monotonic() - self._hash_started
            )
            if remaining <= 0:
                return "unknown", "unknown"
            self._hash_bytes += size
        result = (
            self._command(
                _HASH_SCRIPT,
                [self.context.remote.pathname(relative_path), size, mtime],
                timeout=remaining,
                limit=128,
            )
            .decode("ascii", errors="replace")
            .strip()
        )
        if not re.fullmatch(r"[0-9a-f]{64}", result):
            raise DownloadError("Invalid remote SHA-256 result")
        return "sha256", result

    def cancel(self):
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            self._stop(process)

    def close(self):
        self.cancel()
        self._master = None
        if self._socket_dir is not None:
            self._socket_dir.cleanup()
            self._socket_dir = None
