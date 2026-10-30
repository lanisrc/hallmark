"""Manage transport selection and resources for each remote operation."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, local

import requests

from .auth import resolve_settings, resolve_ssh_settings
from .base import DataBackend, RemoteEntry, RemoteSpec, TransferCancelled


class OperationContext:
    """
    Manage transport resources for one discovery or download operation.

    Leaving the context closes its transport and HTTP sessions. An exception
    also cancels active work.

    Args:
        remote (RemoteSpec): Data source and optional local profile name.
        output_root (Path | str, optional): Root used to validate download paths.

    Attributes:
        remote (RemoteSpec): Data source and optional local profile name.
        output_root (Path, optional): Root used to validate download paths.
        settings (SSHSettings): SSH settings; defaults for other schemes.
        auth (HttpSettings | S3Settings | None): Credentials from the remote's
            profile, for backends declaring that ``auth_type``. Secrets are
            read only when a request needs them.
        transport (DataBackend): Backend selected by ``remote.backend``.
        cancelled (threading.Event): Set once the operation is cancelled.
        on_bytes (callable, optional): Called with each count of bytes
            received. The downloader sets it; workers call it concurrently.
        text_limit (int): Byte limit for small single documents, such as a
            published ``config.yml``: 16 MiB.
        large_text_limit (int): Byte limit for listings, checksum manifests
            and catalogue tables: 1 GiB.
        listing_timeout (int): Seconds allowed for each SFTP metadata request.
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
        # Small single documents, such as a published config.yml.
        self.text_limit = 16 * 1024 * 1024
        # Listings, checksum manifests and catalogue tables grow with the
        # number of files: about 25 MB for 100,000 catalogue rows.
        self.large_text_limit = 1024 * 1024 * 1024
        self.listing_timeout = 60
        self._local = local()
        self._lock = Lock()
        self._sessions = []
        from ..remote.backends import get_backend

        backend = get_backend(remote.backend)
        self.auth = resolve_settings(remote, getattr(backend, "auth_type", None))
        try:
            self.transport = backend(self)
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

    def read_text(self, path, *, large=False):
        """
        Read remote metadata within its size limit.

        Args:
            path (str): Path beneath the remote URL.
            large (bool): The document lists files, as a listing, checksum
                manifest or catalogue table does, and gets the larger limit.

        Returns:
            str: Decoded metadata text.

        Raises:
            RemoteObjectMissing: If the server reports that the document is missing.
            DownloadError: If the document exceeds ``text_limit``, or
                ``large_text_limit`` when ``large`` is set, or does not decode.
            CapabilityError: If the backend cannot read metadata.
        """
        limit = self.large_text_limit if large else self.text_limit
        return self.transport.read_text(path, limit)

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
