"""
HTTP transport using Requests.

Without an auth profile, Requests' own .netrc and environment settings apply.
With one, its basic or bearer credentials are sent only to the profile's hosts.
"""

from __future__ import annotations

import base64
from collections import deque
from threading import Lock
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.auth import AuthBase

from ..utils import REMOTE_REQUEST_TIMEOUT
from .base import (DownloadError, RemoteConfigurationError, RemoteEntry,
                   RemoteObjectMissing, DataBackend, reject_control_characters)
from .index import _parse_directory_listing, _redirected_directory

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class HostBoundAuth(AuthBase):
    """
    Attach a profile's credentials to requests for its own hosts only.

    The secret is read once, on first use. Requests drops the header when a
    redirect leaves the host, as for signed download links elsewhere.

    Args:
        settings (HttpSettings): Profile settings with secret references.
    """

    def __init__(self, settings):
        self.settings = settings
        self._header = None
        self._lock = Lock()

    def resolve(self) -> None:
        """Read the profile's secrets; raises if one is unavailable."""
        with self._lock:
            if self._header is not None:
                return
            settings = self.settings
            if settings.scheme == "bearer":
                self._header = "Bearer " + settings.token.resolve().reveal()
                return
            username = settings.username
            if not isinstance(username, str):
                username = username.resolve().reveal()
            pair = f"{username}:{settings.password.resolve().reveal()}"
            self._header = "Basic " + base64.b64encode(pair.encode()).decode()

    def __call__(self, request):
        target = urlsplit(request.url)
        host = (target.hostname or "").lower()
        if host in self.settings.hosts and (
                target.scheme == "https" or host in LOOPBACK_HOSTS):
            self.resolve()
            request.headers["Authorization"] = self._header
        return request


class HttpBackend(DataBackend):
    """Transfer files and metadata using reusable Requests sessions."""

    auth_type = "http"

    def __init__(self, context):
        super().__init__(context)
        if context.remote.scheme not in {"http", "https"}:
            raise RemoteConfigurationError("HTTP backends require an HTTP(S) URL")
        self.text_urls = {}
        # None leaves Requests to use .netrc and its environment settings.
        settings = getattr(context, "auth", None)
        self.auth = HostBoundAuth(settings) if settings is not None else None

    def prepare(self):
        """Read profile secrets once, before any request is made."""
        if self.auth is not None:
            self.auth.resolve()

    def _auth_option(self):
        # Without a profile, call Requests exactly as before.
        return {"auth": self.auth} if self.auth is not None else {}

    def _parse_directory_listing(self, text, directory):
        """Read generic indexes and recognize the CyVerse dialect automatically."""
        from .cyverse import CyVerseIndexParser, is_cyverse_index

        if is_cyverse_index(text):
            return _parse_directory_listing(text, self.context.remote.url, directory,
                                parser_class=CyVerseIndexParser)
        return _parse_directory_listing(text, self.context.remote.url, directory)

    def iter_entries(self, on_directory=None):
        """Yield metadata from recursive listings without reading payloads."""
        queue, visited = deque([""]), set()
        while queue:
            directory = queue.popleft()
            if directory in visited:
                continue
            visited.add(directory)
            self.context.check_cancelled()
            text = self.context.read_text(directory)
            canonical = _redirected_directory(self.context, directory)
            if canonical != directory and canonical in visited:
                continue
            visited.add(canonical)
            for path, is_directory, size, mtime in self._parse_directory_listing(
                text, canonical
            ):
                if is_directory:
                    if path not in visited:
                        queue.append(path)
                else:
                    yield RemoteEntry(path=path, size=size, mtime=mtime)
            if on_directory is not None:
                on_directory(canonical)

    def _open_file_response(self, path):
        """
        Open a streaming response for a file's bytes as the server stores them.

        Asking for the identity encoding keeps servers from compressing a file
        on the fly, so its checksums still match.
        """
        return self.context.session().get(
            self.context.remote.file_url(path),
            stream=True,
            timeout=REMOTE_REQUEST_TIMEOUT,
            headers={"Accept-Encoding": "identity"},
            **self._auth_option(),
        )

    def _download_error(self, exc):
        # Requests exceptions can embed credentials in their URL or response body.
        """Describe an HTTP failure without exposing response contents."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        cls = RemoteObjectMissing if status == 404 else DownloadError
        detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
        if status in {401, 403}:
            detail += (f"; check auth profile {self.context.remote.auth!r}"
                       if self.auth is not None else
                       "; name an auth profile with --auth, or use ~/.netrc")
        return cls(f"Failed to download from {self.context.remote.display}: {detail}")

    def _open_metadata_response(self, relative_path, *, method="GET", headers=None,
                                data=None):
        """
        Follow bounded metadata redirects only within the declared source root.

        Other methods, such as WebDAV's PROPFIND, are repeated unchanged at
        each redirect target.
        """
        url = self.context.remote.file_url(relative_path)
        root = urlsplit(self.context.remote.url)
        root_path = unquote(root.path, errors="strict").rstrip("/") or "/"

        def endpoint(parsed):
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return parsed.scheme, parsed.hostname, port

        for _ in range(11):
            self.context.check_cancelled()
            try:
                reject_control_characters(url, "Metadata URL")
                target = urlsplit(url)
                path = unquote(target.path, errors="strict")
                reject_control_characters(path, "Metadata path")
                within_root = (root_path == "/" or path == root_path
                               or path.startswith(root_path + "/"))
                if (endpoint(target) != endpoint(root) or not within_root
                        or ".." in path.split("/") or "\\" in path
                        or target.username != root.username
                        or target.password != root.password):
                    raise DownloadError("Metadata redirect leaves the source root")
            except (ValueError, UnicodeError):
                raise DownloadError("Invalid metadata redirect URL") from None
            options = dict(stream=True, timeout=REMOTE_REQUEST_TIMEOUT,
                           allow_redirects=False, **self._auth_option())
            if method == "GET":
                response = self.context.session().get(url, **options)
            else:
                response = self.context.session().request(
                    method, url, headers=headers, data=data, **options)
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
            with self._open_file_response(relative_path) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    # A .gz file served with Content-Encoding: gzip is stored as
                    # sent; undoing the encoding would change its bytes.
                    for chunk in response.raw.stream(chunk_size, decode_content=False):
                        self.context.check_cancelled()
                        if chunk:
                            handle.write(chunk)
                            if self.context.on_bytes is not None:
                                self.context.on_bytes(len(chunk))
        except requests.RequestException as exc:
            raise self._download_error(exc) from None
    def read_text(self, relative_path, limit):
        """Read metadata within the source root and byte limit."""
        content = bytearray()
        try:
            response, final_url = self._open_metadata_response(relative_path)
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
            raise self._download_error(exc) from None
