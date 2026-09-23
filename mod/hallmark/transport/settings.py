"""Internal resource limits for SSH transfers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SSHSettings:
    """
    Local SSH settings shared by the connections in one operation.

    Timeouts are in seconds. ``transfer_timeout`` limits the total time
    for each file, and ``max_sessions`` limits concurrent SFTP sessions.
    """
    connect_timeout: int = 10
    transfer_timeout: int = 3600
    shutdown_timeout: int = 2
    max_sessions: int = 4
