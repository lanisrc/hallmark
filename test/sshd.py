"""A disposable loopback OpenSSH server for tests.

``start_sshd`` generates throwaway keys, starts ``sshd`` on a free port of
127.0.0.1 and writes a client configuration that reaches it as ``hm-test``.
No external host, agent or persistent key is involved.
"""

from __future__ import annotations

from contextlib import contextmanager
import getpass
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

SSHD_CONFIG = """ListenAddress 127.0.0.1
Port {port}
HostKey {base}/host
PidFile {base}/sshd.pid
AuthorizedKeysFile {authorized}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxSessions 4
LogLevel VERBOSE
Subsystem sftp internal-sftp
"""

CLIENT_CONFIG = """Host hm-test
    HostName 127.0.0.1
Host *
    User {user}
    Port {port}
    IdentityFile {base}/client
    IdentityAgent none
    IdentitiesOnly yes
    UserKnownHostsFile {known}
    GlobalKnownHostsFile /dev/null
"""


class SshdError(RuntimeError):
    """The server could not be started; the message holds its log."""


def sshd_binary():
    """Return the sshd path, or None when openssh-server is not installed."""
    found = shutil.which("sshd") or "/usr/sbin/sshd"
    return found if Path(found).exists() else None


@contextmanager
def start_sshd(base, mode=None):
    """Run sshd for the duration of the block.

    Args:
        base (Path): Empty folder for keys, configuration, the log and the
            exported ``export/`` folder.
        mode (str, optional): ``sftp-only`` (ForceCommand internal-sftp),
            ``no-sftp`` (no SFTP subsystem) or ``max-one`` (MaxSessions 1).

    Yields:
        dict: ``root`` (exported folder), ``port``, ``known`` (known_hosts),
        ``base``, ``config`` (client ssh_config), ``daemon`` (the process),
        ``log`` (sshd.log path) and ``url`` (``ssh://hm-test<root>/``).

    Raises:
        SshdError: If sshd is missing, exits early or does not listen.
    """
    base = Path(base)
    sshd = sshd_binary()
    if sshd is None:
        raise SshdError("sshd is not installed")
    for name in ("host", "client", "second", "untrusted"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(base / name)],
            check=True,
        )
    authorized = base / "authorized"
    authorized.write_text(
        (base / "client.pub").read_text() + (base / "second.pub").read_text()
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    text = SSHD_CONFIG.format(port=port, base=base, authorized=authorized)
    if mode == "sftp-only":
        text += "ForceCommand internal-sftp\n"
    elif mode == "no-sftp":
        text = text.replace("Subsystem sftp internal-sftp\n", "")
    elif mode == "max-one":
        text = text.replace("MaxSessions 4", "MaxSessions 1")
    config = base / "sshd_config"
    config.write_text(text)
    log_path = base / "sshd.log"
    log = log_path.open("wb")
    daemon = subprocess.Popen(
        [sshd, "-D", "-e", "-f", str(config)], stderr=log, start_new_session=True
    )
    known = base / "known_hosts"
    known.write_text(f"[127.0.0.1]:{port} " + (base / "host.pub").read_text())
    client_config = base / "ssh_config"
    client_config.write_text(CLIENT_CONFIG.format(
        user=getpass.getuser(), port=port, base=base, known=known))
    root = base / "export"
    root.mkdir()
    try:
        deadline = time.monotonic() + 5
        while True:
            if daemon.poll() is not None:
                log.flush()
                raise SshdError(log_path.read_text(errors="replace"))
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SshdError("Local SSH test server did not start")
                time.sleep(0.05)
        yield {
            "root": root,
            "port": port,
            "known": known,
            "base": base,
            "config": client_config,
            "daemon": daemon,
            "log": log_path,
            "url": "ssh://hm-test" + quote(str(root), safe="/") + "/",
        }
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)
        log.close()
