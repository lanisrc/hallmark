"""Internal transport contracts and validated data-remote locators."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from ..error import HallmarkError
from ..helper_functions import validate_relative_path


class DownloadError(HallmarkError):
    """A remote transfer or its verification failed."""


class RemoteConfigurationError(DownloadError, ValueError):
    """A remote cannot be used with the selected local policy."""


class RemoteObjectMissing(DownloadError):
    """The transport positively identified an absent object."""


class CapabilityError(DownloadError):
    """The selected source does not support this operation."""


class TransferCancelled(DownloadError):
    """The owning operation cancelled a transfer."""


def reject_controls(value: str, label: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise RemoteConfigurationError(f"{label} must be valid UTF-8") from None
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise RemoteConfigurationError(f"{label} contains control characters")


def literal_path(value) -> Path:
    """Validate a literal catalog path without decoding or trimming it."""
    raw = str(value)
    reject_controls(raw, "Remote path")
    if not raw.strip():
        raise RemoteConfigurationError("Remote path cannot be empty")
    try:
        return validate_relative_path(raw, label="remote path")
    except ValueError as exc:
        raise RemoteConfigurationError(str(exc)) from None


def profile_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise RemoteConfigurationError("Auth profile must match [A-Za-z0-9_-]{1,64}")
    return value


def ssh_host(value: str) -> str:
    # Aliases are not necessarily DNS names. Limit expansion tokens to characters
    # that cannot become shell syntax in a user's ProxyCommand/Match configuration.
    if ":" in value:
        if "%" in value:
            raise RemoteConfigurationError("Scoped IPv6 SSH hosts are unsupported")
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            raise RemoteConfigurationError("Invalid IPv6 SSH host") from None
    elif not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise RemoteConfigurationError("Invalid SSH host or config alias")
    return value


def ssh_user(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value
    ):
        raise RemoteConfigurationError("Invalid SSH user")
    return value


@dataclass(frozen=True)
class RemoteSpec:
    """A parsed URL root; catalog paths appended to it are always literal."""

    url: str = field(repr=False)
    scheme: str
    host: str
    root: str = field(repr=False)
    user: str | None = field(default=None, repr=False)
    port: int | None = None
    auth: str | None = None

    @classmethod
    def parse(cls, url: str, auth: str | None = None) -> RemoteSpec:
        if not isinstance(url, str) or not url.strip():
            raise RemoteConfigurationError("Remote URL must be a non-empty string")
        reject_controls(url, "Remote URL")
        url = url.strip()
        if auth is not None:
            profile_name(auth)
        try:
            parsed = urlsplit(url)
            host, port = parsed.hostname, parsed.port
        except ValueError:
            raise RemoteConfigurationError("Invalid remote URL host or port") from None
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "ssh", "sftp"}:
            raise RemoteConfigurationError(
                "Use an http(s):// or ssh://HOST/absolute/root/ URL; "
                "scp shorthand and this scheme are unsupported"
            )
        if not host or port == 0 or parsed.netloc.endswith(":"):
            raise RemoteConfigurationError("Invalid remote URL host or port")
        if scheme in {"ssh", "sftp"}:
            ssh_host(host)
            if parsed.password is not None:
                raise RemoteConfigurationError("SSH URLs cannot contain passwords")
            if "?" in url or "#" in url:
                raise RemoteConfigurationError(
                    "SSH URLs cannot contain query or fragment fields; "
                    "percent-encode filename characters"
                )
            if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
                raise RemoteConfigurationError("Invalid URL percent escape")
            try:
                root = unquote(parsed.path, errors="strict")
                user = (
                    ssh_user(unquote(parsed.username, errors="strict"))
                    if parsed.username is not None
                    else None
                )
            except UnicodeError:
                raise RemoteConfigurationError("SSH URL must encode UTF-8") from None
            reject_controls(root, "SSH root")
            if (
                not root.startswith("/")
                or root.startswith("//")
                or ".." in root.split("/")
                or "\\" in root
            ):
                raise RemoteConfigurationError(
                    "SSH root must be absolute, without traversal or backslashes"
                )
        else:
            root, user = parsed.path, None
            if auth is not None:
                raise RemoteConfigurationError(
                    "Auth profiles currently support SSH/SFTP only; "
                    "HTTP retains Requests .netrc authentication"
                )
        return cls(url, scheme, host, root, user, port, auth)

    def pathname(self, relative_path: str) -> str:
        return self.root.rstrip("/") + "/" + literal_path(relative_path).as_posix()

    def file_url(self, relative_path: str) -> str:
        path = literal_path(relative_path).as_posix() if relative_path else ""
        if relative_path.endswith("/") and path:
            path += "/"
        if self.scheme in {"http", "https"}:
            return urljoin(self.url.rstrip("/") + "/", quote(path, safe="/"))
        parsed = urlsplit(self.url)
        root = self.root.rstrip("/") + "/" + path
        return urlunsplit((self.scheme, parsed.netloc, quote(root, safe="/"), "", ""))

    @property
    def display(self) -> str:
        """Safe endpoint context without URL credentials, query, or local paths."""
        host = self.host if re.fullmatch(r"[A-Za-z0-9_.:-]+", self.host) else "remote"
        return f"{self.scheme}://{host}"


class Transport:
    """Internal behavior shared by transports; no configuration-driven imports."""

    def __init__(self, context):
        self.context = context

    def prepare(self):
        pass

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        raise NotImplementedError

    def list_entries(self):
        raise CapabilityError("Recursive listing is unsupported by this transport")

    def checksum_small(self, relative_path):
        return ("unknown", "unknown")

    def close(self):
        pass
