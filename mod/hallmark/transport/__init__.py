"""Explicit internal dispatch and per-invocation resource ownership."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, local

import requests

from .auth import resolve_settings
from .base import RemoteEntry, RemoteSpec, TransferCancelled


class OperationContext:
    """Own sessions, cancellation and one transport for a download or build."""

    def __init__(
        self,
        remote,
        output_root=None,
    ):
        self.remote = remote
        self.settings = resolve_settings(remote)
        self.output_root = Path(output_root) if output_root is not None else None
        self.cancelled = Event()
        self.on_bytes = None
        self.text_limit = 16 * 1024 * 1024
        self.listing_timeout = 60
        self._local = local()
        self._lock = Lock()
        self._sessions = []
        if remote.scheme in {"http", "https"}:
            from .http import HttpTransport

            self.transport = HttpTransport(self)
        else:
            from .ssh import SshTransport

            self.transport = SshTransport(self)

    @contextmanager
    def executor(self, max_workers):
        """Cancel active transport work before waiting for interrupted workers."""
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            yield executor
        except BaseException:
            self.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def session(self):
        if not hasattr(self._local, "session"):
            session = requests.Session()
            with self._lock:
                self._sessions.append(session)
            self._local.session = session
        return self._local.session

    def read_text(self, path):
        return self.transport.read_text(path, self.text_limit)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise TransferCancelled("Download cancelled")

    def cancel(self):
        self.cancelled.set()
        cancel = getattr(self.transport, "cancel", None)
        if cancel is not None:
            cancel()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.cancel()
        try:
            self.transport.close()
        finally:
            for session in self._sessions:
                session.close()


__all__ = ["OperationContext", "RemoteEntry", "RemoteSpec"]
