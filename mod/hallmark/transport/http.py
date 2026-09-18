"""Requests transport, including its default .netrc/environment behavior."""

from __future__ import annotations

from urllib.parse import unquote, urljoin, urlsplit

import requests

from ..helper_functions import REMOTE_REQUEST_TIMEOUT
from .base import DownloadError, RemoteObjectMissing, Transport, reject_controls


class HttpTransport(Transport):
    def __init__(self, context):
        super().__init__(context)
        self.text_urls = {}

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

    def _metadata_get(self, relative_path):
        """Follow bounded metadata redirects only within the declared source root."""
        url = self.context.remote.file_url(relative_path)
        root = urlsplit(self.context.remote.url)
        root_path = unquote(root.path, errors="strict").rstrip("/") or "/"

        def endpoint(parsed):
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return parsed.scheme, parsed.hostname, port

        for _ in range(11):
            self.context.check_cancelled()
            try:
                reject_controls(url, "Metadata URL")
                target = urlsplit(url)
                path = unquote(target.path, errors="strict")
                reject_controls(path, "Metadata path")
                within_root = (root_path == "/" or path == root_path
                               or path.startswith(root_path + "/"))
                if (endpoint(target) != endpoint(root) or not within_root
                        or ".." in path.split("/") or "\\" in path
                        or target.username != root.username
                        or target.password != root.password):
                    raise DownloadError("Metadata redirect leaves the source root")
            except (ValueError, UnicodeError):
                raise DownloadError("Invalid metadata redirect URL") from None
            response = self.context.session().get(
                url, stream=True, timeout=REMOTE_REQUEST_TIMEOUT,
                allow_redirects=False,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response, url
            location = response.headers.get("Location")
            response.close()
            if not isinstance(location, str) or not location:
                raise DownloadError("Metadata redirect has no location")
            url = urljoin(url, location)
        raise DownloadError("Metadata redirect limit exceeded")

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        try:
            with self._get(relative_path) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        self.context.check_cancelled()
                        if chunk:
                            handle.write(chunk)
                            if self.context.on_bytes is not None:
                                self.context.on_bytes(len(chunk))
        except requests.RequestException as exc:
            raise self._error(exc) from None

    def read_text(self, relative_path, limit):
        content = bytearray()
        try:
            response, final_url = self._metadata_get(relative_path)
            with response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=8192):
                    self.context.check_cancelled()
                    content.extend(chunk)
                    if len(content) > limit:
                        raise DownloadError("Remote text exceeds the configured limit")
                encoding = response.encoding or "utf-8"
            text = content.decode(encoding, errors="replace")
            self.text_urls[relative_path] = final_url
            return text
        except requests.RequestException as exc:
            raise self._error(exc) from None
