"""Manage transport selection and resources for each remote operation."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, local

import requests

from .auth import resolve_ssh_settings
from .base import DataBackend, RemoteEntry, RemoteSpec, TransferCancelled


class OperationContext:
    """
    Manage transport resources for one discovery or download operation.

    Leaving the context closes its transport and HTTP sessions. An exception
    also cancels active work.

    Args:
        remote (RemoteSpec): Data source and optional local profile name.
        output_root (Path | str, optional): Root used to validate download paths.
    """

    def __init__(
        self,
        remote,
        output_root=None,
    ):
        self.remote = remote
        self.settings = resolve_ssh_settings(remote)
        self.output_root = Path(output_root) if output_root is not None else None
        self.cancelled = Event()
        self.on_bytes = None
        self.text_limit = 16 * 1024 * 1024
        self.listing_timeout = 60
        self._local = local()
        self._lock = Lock()
        self._sessions = []
        from ..remote.backends import get_backend

        try:
            self.transport = get_backend(remote.backend)(self)
        except BaseException:
            self.cancelled.set()
            for session in self._sessions:
                session.close()
            raise

    def session(self):
        """Return the reusable HTTP session for the current thread."""
        if not hasattr(self._local, "session"):
            session = requests.Session()
            with self._lock:
                self._sessions.append(session)
            self._local.session = session
        return self._local.session

    def read_text(self, path):
        """Read remote metadata within the configured size limit."""
        return self.transport.read_text(path, self.text_limit)

    def check_cancelled(self):
        """Raise TransferCancelled if this operation was cancelled."""
        if self.cancelled.is_set():
            raise TransferCancelled("Download cancelled")

    def cancel(self):
        """Cancel this operation and stop its active transport work."""
        self.cancelled.set()
        cancel = getattr(self.transport, "cancel", None)
        if cancel is not None:
            cancel()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self.cancel()
        finally:
            try:
                self.transport.close()
            finally:
                for session in self._sessions:
                    session.close()


__all__ = ["DataBackend", "OperationContext", "RemoteEntry", "RemoteSpec"]
