"""HTTP transport using Requests with its normal .netrc and environment settings."""

from __future__ import annotations

from collections import deque
from urllib.parse import unquote, urljoin, urlsplit

import requests

from ..helper_functions import REMOTE_REQUEST_TIMEOUT
from .base import (DownloadError, RemoteConfigurationError, RemoteEntry,
                   RemoteObjectMissing, DataBackend, reject_controls)
from .index import _parse_index, _response_directory


class HttpBackend(DataBackend):
    """Transfer files and metadata using reusable Requests sessions."""
    def __init__(self, context):
        super().__init__(context)
        if context.remote.scheme not in {"http", "https"}:
            raise RemoteConfigurationError("HTTP backends require an HTTP(S) URL")
        self.text_urls = {}

    def _parse_index(self, text, directory):
        """Read generic indexes and recognize the CyVerse dialect automatically."""
        from .cyverse import CyVerseIndexParser, is_cyverse_index

        if is_cyverse_index(text):
            return _parse_index(text, self.context.remote.url, directory,
                                parser_class=CyVerseIndexParser)
        return _parse_index(text, self.context.remote.url, directory)

    def iter_entries(self, on_directory=None):
        """Yield metadata from recursive listings without reading payloads."""
        descend = getattr(self.context, "descend", None)
        queue, visited = deque([""]), set()
        while queue:
            directory = queue.popleft()
            if directory in visited:
                continue
            visited.add(directory)
            self.context.check_cancelled()
            text = self.context.read_text(directory)
            canonical = _response_directory(self.context, directory)
            if canonical != directory and canonical in visited:
                continue
            visited.add(canonical)
            for path, is_directory, size, mtime in self._parse_index(text, canonical):
                if is_directory:
                    if path not in visited and (descend is None or descend(path)):
                        queue.append(path)
                else:
                    yield RemoteEntry(path=path, size=size, mtime=mtime)
            if on_directory is not None:
                on_directory(canonical)

    def _get(self, path):
        """Open a streaming response with the configured request timeout."""
        return self.context.session().get(
            getattr(self, "direct_url", None) or self.context.remote.file_url(path),
            stream=True,
            timeout=REMOTE_REQUEST_TIMEOUT,
        )

    def _error(self, exc):
        # Requests exceptions can embed credentials in their URL or response body.
        """Describe an HTTP failure without exposing response contents."""
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
        """Stream a file to the destination, checking for cancellation."""
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
        """Read metadata within the source root and byte limit."""
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


HttpTransport = HttpBackend
