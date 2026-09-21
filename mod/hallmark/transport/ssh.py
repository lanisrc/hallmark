"""Share an OpenSSH connection for SFTP transfers and metadata reads."""

from __future__ import annotations

import os
import re
import selectors
import stat
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
    RemoteEntry,
    Transport,
    literal_path,
    reject_controls,
)


def batch_argument(path):
    """
    Escape an absolute path for a literal SFTP batch argument.

    Args:
        path (Path | str): Absolute local or remote path.

    Returns:
        str: Argument with command and glob characters escaped.

    Raises:
        RemoteConfigurationError: If the path is relative or contains controls.
    """
    text = str(path)
    reject_controls(text, "SFTP path")
    if not text.startswith("/"):
        raise RemoteConfigurationError("SFTP operands must be absolute paths")
    # Use unquoted escapes: OpenSSH adds glob escapes itself inside quotes,
    # so adding backslashes there would instead request literal backslashes.
    special = " \\\"'#?[*"  # ] has no special meaning outside a bracket pattern.
    return "".join("\\" + char if char in special else char for char in text)


class SshTransport(Transport):
    """
    Share one OpenSSH connection for SFTP transfers and metadata requests.

    Args:
        context (OperationContext): Local settings and cancellation state.
        ssh (list[str], optional): Internal command override for offline tests.
        sftp (list[str], optional): Internal command override for offline tests.
    """
    def __init__(self, context, *, ssh=None, sftp=None):
        super().__init__(context)
        # Command overrides support offline tests and cannot come from repo YAML.
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

    def _options(self, *, master=False):
        """Build noninteractive SSH options for the shared connection."""
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
            # A failed shared connection must not reauthenticate for each file.
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
        """Start a client in a separate process group and track it for cleanup."""
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
        """Stop a client process group and wait for the child to exit."""
        def signal_group(signum):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                # macOS can report EPERM for a group containing only zombies.
                # poll() may return None while another thread holds Popen's
                # wait lock, so allow a bounded wait for concurrent reaping.
                try:
                    process.wait(timeout=self.context.settings.shutdown_timeout)
                except subprocess.TimeoutExpired:
                    raise exc from None

        signal_group(signal.SIGTERM)
        try:
            process.wait(timeout=self.context.settings.shutdown_timeout)
        except subprocess.TimeoutExpired:
            pass
        signal_group(signal.SIGKILL)
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
        report_progress=False,
    ):
        """
        Run an OpenSSH command with output limits and a total time limit.

        Optionally bound a downloaded metadata file's size or report received
        dataset bytes. Always stop and reap the client before returning.
        """
        output = bytearray()
        stderr_size = 0
        diagnostics = bytearray()
        reported_bytes = 0

        def report_bytes():
            nonlocal reported_bytes
            if report_progress and destination is not None and destination.exists():
                current = destination.stat().st_size
                if current > reported_bytes:
                    if self.context.on_bytes is not None:
                        self.context.on_bytes(current - reported_bytes)
                    reported_bytes = current

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
                        report_bytes()
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
                report_bytes()
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
        """Check client support and establish the shared SSH connection."""
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
        """Fetch one literal path within the session and transfer limits."""
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
                report_progress=file_limit is None,
            )

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        """Download one file over SFTP; OpenSSH controls the read size."""
        self._fetch(relative_path, destination)

    def read_text(self, relative_path, limit):
        """Read UTF-8 metadata with size checks before and after transfer."""
        entry = self.stat(relative_path)
        if entry.size is not None and entry.size > limit:
            raise DownloadError("Remote text exceeds its size limit")
        with tempfile.TemporaryDirectory(prefix="hm-text-") as directory:
            destination = Path(directory) / "content"
            self._fetch(relative_path, destination, file_limit=limit)
            if destination.stat().st_size > limit:
                raise DownloadError("Remote text exceeds its size limit")
            try:
                return destination.read_text(encoding="utf-8")
            except UnicodeError:
                raise DownloadError("Remote manifest must contain UTF-8 text") from None

    def _metadata(self):
        """Create an SFTP metadata session on the shared connection."""
        from .sftp import SftpMetadata

        return SftpMetadata(self)

    def stat(self, relative_path):
        """Read regular-file attributes without fetching the file contents."""
        path = literal_path(relative_path).as_posix()
        with self._metadata() as session:
            attrs = session.lstat(self.context.remote.pathname(path))
        mode = attrs["mode"]
        if mode is None or not stat.S_ISREG(mode):
            raise DownloadError("Remote metadata must be a regular file")
        return RemoteEntry(path, attrs["size"], attrs["mtime"])

    def iter_entries(self, on_directory=None):
        """
        Yield regular files recursively using the SFTP subsystem.

        Args:
            on_directory (callable, optional): Callback receiving each directory
                path relative to the dataset root.

        Yields:
            RemoteEntry: File path, size, and modification time when available.
            Symlinks, special files, and repository metadata directories are skipped.

        Raises:
            DownloadError: If discovery fails or a path escapes the dataset root.
            CapabilityError: If the server omits required file types.
        """
        with self._metadata() as session:
            root = session.realpath(self.context.remote.root).rstrip("/") or "/"
            reject_controls(root, "SFTP root")
            if not root.startswith("/") or ".." in root.split("/"):
                raise DownloadError("Invalid SFTP canonical root")
            pending = [("", root)]
            visited = set()
            while pending:
                self.context.check_cancelled()
                relative, directory = pending.pop()
                canonical = session.realpath(directory).rstrip("/") or "/"
                if (root != "/" and canonical != root
                        and not canonical.startswith(root + "/")):
                    raise DownloadError("SFTP directory escapes the source root")
                if canonical in visited:
                    continue
                visited.add(canonical)
                attrs = session.lstat(directory)
                if attrs["mode"] is None:
                    raise CapabilityError("SFTP server omitted directory type")
                if not stat.S_ISDIR(attrs["mode"]):
                    raise DownloadError("SFTP discovery root must be a directory")
                if on_directory is not None:
                    on_directory(relative)
                names = set()
                for name, attrs in session.iterdir(directory):
                    self.context.check_cancelled()
                    if name in {".", ".."}:
                        continue
                    if name.lower() in {".hm", ".git"}:
                        continue
                    if not name or "/" in name or name in names:
                        raise DownloadError("Invalid or duplicate SFTP directory name")
                    names.add(name)
                    path = literal_path(relative + name).as_posix()
                    absolute = directory.rstrip("/") + "/" + name
                    mode = attrs["mode"]
                    if mode is None:
                        attrs = session.lstat(absolute)
                        mode = attrs["mode"]
                    if mode is None:
                        raise CapabilityError("SFTP server omitted file type")
                    if stat.S_ISDIR(mode):
                        pending.append((path + "/", absolute))
                    elif stat.S_ISREG(mode):
                        entry = RemoteEntry(path, attrs["size"], attrs["mtime"])
                        yield entry
                    # Symlinks and special files are deliberately not traversed.

    def cancel(self):
        """Stop the client process groups started by this transport."""
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            self._stop(process)

    def close(self):
        """Stop clients and remove the shared connection socket directory."""
        self.cancel()
        self._master = None
        if self._socket_dir is not None:
            self._socket_dir.cleanup()
            self._socket_dir = None
