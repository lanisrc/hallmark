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
from urllib3.exceptions import (HTTPError as Urllib3Error, ProtocolError,
                                ReadTimeoutError)
from urllib3.response import HTTPResponse as Urllib3Response

from ..utils import REMOTE_REQUEST_TIMEOUT
from .base import (AccessRefused, DownloadError, RemoteConfigurationError,
                   RemoteEntry, RemoteObjectMissing, DataBackend, decode_metadata,
                   reject_control_characters)
from .auth import LOOPBACK_HOSTS
from .index import _parse_directory_listing, _redirected_directory


def _refuse_short_body(response):
    """Make urllib3 1.x refuse a body shorter than its Content-Length, as 2.x does."""
    if isinstance(response.raw, Urllib3Response):
        response.raw.enforce_content_length = True
    return response


def _declared_charset(content_type):
    """
    Return the charset a Content-Type header declares, or None.

    Requests assumes ISO-8859-1 for undeclared text; Hallmark assumes UTF-8.
    """
    for parameter in (content_type or "").split(";")[1:]:
        key, _, value = parameter.strip().partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip("\"'")
    return None


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
            text = self.context.read_text(directory, large=True)
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
        return _refuse_short_body(self.context.session().get(
            self.context.remote.file_url(path),
            stream=True,
            timeout=REMOTE_REQUEST_TIMEOUT,
            headers={"Accept-Encoding": "identity"},
            **self._auth_option(),
        ))

    def _download_error(self, exc, action):
        """
        Describe an HTTP failure by its status or kind, never by its text.

        Requests exceptions can embed credentials in their URL or response
        body. Commands add the advice on credentials an AccessRefused needs.

        Args:
            exc (Exception): A Requests or urllib3 error.
            action (str): What failed, such as ``Download from HOST``.
        """
        status = getattr(getattr(exc, "response", None), "status_code", None)
        # Requests reports a stalled body, or a connection the server closed
        # before replying, as a ConnectionError around urllib3's error.
        inner = (exc.args[0] if isinstance(exc, requests.ConnectionError)
                 and exc.args else exc)
        stalled = isinstance(exc, requests.Timeout) or isinstance(
            inner, ReadTimeoutError)
        reset = isinstance(inner, (ProtocolError,
                                   requests.exceptions.ChunkedEncodingError))
        cls = DownloadError
        if status == 404:
            cls, cause = RemoteObjectMissing, "not found on the server"
        elif status in {401, 403}:
            return AccessRefused(f"{action} failed: access refused (HTTP {status})",
                                 profile=self.context.remote.auth)
        elif isinstance(status, int) and status >= 500:
            cause = f"server error (HTTP {status}); try again later"
        elif isinstance(status, int):
            cause = f"HTTP {status}"
        elif stalled:
            cause = "no response in time"
        elif isinstance(exc, requests.exceptions.SSLError):
            cause = "certificate could not be verified"
        elif reset:
            cause = "the connection was reset or the body was truncated"
        elif isinstance(exc, requests.ConnectionError):
            cause = "could not connect; check the URL and network"
        else:
            cause = type(exc).__name__
        return cls(f"{action} failed: {cause}")

    def _read_action(self, relative_path):
        """Name a metadata read: a folder's listing, or a file's contents."""
        display = self.context.remote.display
        if not relative_path:
            return f"Listing the dataset root on {display}"
        if relative_path.endswith("/"):
            return f"Listing {relative_path} on {display}"
        return f"Reading {relative_path} from {display}"

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
                return _refuse_short_body(response), url
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
        # Reading response.raw directly bypasses Requests' error wrapping, so
        # a reset, truncated or stalled transfer arrives as a urllib3 error.
        except (requests.RequestException, Urllib3Error) as exc:
            raise self._download_error(
                exc, f"Download from {self.context.remote.display}") from None

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
                charset = _declared_charset(response.headers.get("Content-Type"))
            text = decode_metadata(content, charset)
            self.text_urls[relative_path] = final_url
            return text
        except requests.RequestException as exc:
            raise self._download_error(exc, self._read_action(relative_path)) from None
