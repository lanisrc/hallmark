"""Explicit internal dispatch and per-invocation resource ownership."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, local

import requests

from .auth import resolve_settings
from .base import DownloadError, RemoteSpec, TransferCancelled


class OperationContext:
    """Own sessions, cancellation and one transport for a download or build."""

    def __init__(
        self,
        remote,
        output_root=None,
        *,
        allow_remote_commands=False,
        remote_hash=False,
    ):
        self.remote = remote
        self.settings = resolve_settings(remote)
        self.output_root = Path(output_root) if output_root is not None else None
        self.cancelled = Event()
        self.allow_remote_commands = allow_remote_commands
        self.remote_hash = remote_hash
        self.text_limit = 16 * 1024 * 1024
        self.listing_limit = 100_000
        self.listing_timeout = 60
        self.hash_file_limit = 10 * 1024 * 1024
        self.hash_total_limit = 100 * 1024 * 1024
        self.hash_timeout = 60
        self._local = local()
        self._lock = Lock()
        self._sessions = []
        self._text_bytes = 0
        self._listing_started = None
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
        with self._lock:
            if self._listing_started is None:
                self._listing_started = time.monotonic()
            if time.monotonic() - self._listing_started > 300:
                raise DownloadError("Remote crawl exceeded its five minute budget")
        text = self.transport.read_text(path, self.text_limit)
        with self._lock:
            self._text_bytes += len(text.encode("utf-8"))
            if self._text_bytes > 64 * 1024 * 1024:
                raise DownloadError("Remote crawl exceeded its 64 MiB text budget")
        return text

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


__all__ = ["OperationContext", "RemoteSpec"]
