"""Requests transport, including its default .netrc/environment behavior."""

from __future__ import annotations

import hashlib

import requests

from ..helper_functions import REMOTE_REQUEST_TIMEOUT
from .base import DownloadError, RemoteObjectMissing, Transport


class HttpTransport(Transport):
    def _get(self, path):
        return self.context.session().get(
            getattr(self, "direct_url", None) or self.context.remote.file_url(path),
            stream=True,
            timeout=REMOTE_REQUEST_TIMEOUT,
        )

    def _error(self, exc):
        # Requests exceptions can embed credentials in their URL or response body.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        cls = RemoteObjectMissing if status == 404 else DownloadError
        detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
        return cls(f"Failed to download from {self.context.remote.display}: {detail}")

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        try:
            with self._get(relative_path) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        self.context.check_cancelled()
                        if chunk:
                            handle.write(chunk)
        except requests.RequestException as exc:
            raise self._error(exc) from None

    def read_text(self, relative_path, limit):
        content = bytearray()
        try:
            with self._get(relative_path) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=8192):
                    self.context.check_cancelled()
                    content.extend(chunk)
                    if len(content) > limit:
                        raise DownloadError("Remote text exceeds the configured limit")
                encoding = response.encoding or "utf-8"
            return content.decode(encoding, errors="replace")
        except requests.RequestException as exc:
            raise self._error(exc) from None

    def checksum_small(self, relative_path):
        unknown = ("unknown", "unknown")
        try:
            with self.context.session().head(
                self.context.remote.file_url(relative_path),
                timeout=REMOTE_REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                try:
                    size = int(response.headers.get("Content-Length"))
                except (TypeError, ValueError):
                    return unknown
            if size < 0 or size > self.context.hash_file_limit:
                return unknown
            digest, received = hashlib.md5(), 0
            with self._get(relative_path) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=8192):
                    self.context.check_cancelled()
                    received += len(chunk)
                    if received > self.context.hash_file_limit:
                        return unknown
                    digest.update(chunk)
            if received != size:
                raise DownloadError("Remote file changed while computing its checksum")
            return "md5", digest.hexdigest()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {401, 403}:
                raise self._error(exc) from None
            return unknown
