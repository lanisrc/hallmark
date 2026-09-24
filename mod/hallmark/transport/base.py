"""Shared transport interfaces and data-remote URL validation."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from ..error import HallmarkError
from ..helper_functions import validate_relative_path


class DownloadError(HallmarkError):
    """Raised when download setup, transfer, or verification fails."""


class RemoteConfigurationError(DownloadError, ValueError):
    """Raised when a data remote or its local settings are invalid."""


class RemoteObjectMissing(DownloadError):
    """Raised when the server reports that a remote file is missing."""


class CapabilityError(DownloadError):
    """Raised when the source or client does not support an operation."""


class TransferCancelled(DownloadError):
    """Raised when a transfer is cancelled."""


def backend_name(value):
    """Validate a registered backend name, never an import path."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise RemoteConfigurationError(
            "Backend name must match [a-z][a-z0-9_-]{0,63}")
    return value


def freeze_backend_options(options=None):
    """Copy YAML-compatible options into a deeply immutable mapping."""
    active = set()

    def freeze(value):
        if isinstance(value, (Mapping, list, tuple)):
            if id(value) in active:
                raise RemoteConfigurationError("Backend options cannot contain cycles")
            active.add(id(value))
            try:
                if isinstance(value, Mapping):
                    if any(not isinstance(key, str) for key in value):
                        raise RemoteConfigurationError(
                            "Backend option keys must be strings")
                    return MappingProxyType({key: freeze(item)
                                             for key, item in value.items()})
                return tuple(freeze(item) for item in value)
            finally:
                active.remove(id(value))
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        raise RemoteConfigurationError(
            "Backend options must contain YAML scalar, list, or mapping values")

    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise RemoteConfigurationError("Backend options must be a mapping")
    return freeze(options)


def thaw_backend_options(options=None):
    """Return an independent YAML-compatible copy of backend options."""
    def thaw(value):
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [thaw(item) for item in value]
        return value

    return thaw(freeze_backend_options(options))


def reject_controls(value: str, label: str) -> None:
    """Reject control characters and text that cannot be encoded as UTF-8."""
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


def ssh_host(value: str) -> str:
    # Aliases are not necessarily DNS names. Limit expansion tokens to characters
    # that cannot become shell syntax in a user's ProxyCommand/Match configuration.
    """Validate an SSH hostname, configuration alias, or IPv6 address."""
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
    """Validate and return an SSH username."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value
    ):
        raise RemoteConfigurationError("Invalid SSH user")
    return value


@dataclass(frozen=True)
class RemoteEntry:
    """
    A discovered file and its available metadata.

    Attributes:
        path (str): Literal path relative to the dataset root.
        size (int, optional): File size in bytes.
        mtime (float, optional): Modification time reported by the server.
        checksum_algorithm (str, optional): Published checksum algorithm.
        checksum (str, optional): Published digest.

    Unavailable metadata remains None; file contents are not read to fill it.
    """

    path: str
    size: int | None = None
    mtime: float | None = None
    checksum_algorithm: str | None = None
    checksum: str | None = None


@dataclass(frozen=True)
class RemoteSpec:
    """
    A parsed data-remote URL and transport configuration.

    Use ``parse`` to validate a URL. SSH roots are decoded once; catalog
    paths appended to the root are treated as literal filenames.

    Attributes:
        url (str): Original source URL after trimming surrounding whitespace.
        scheme (str): HTTP, HTTPS, SSH, or SFTP scheme in lowercase.
        host (str): Hostname or SSH configuration alias.
        root (str): Dataset root, decoded for SSH and SFTP.
        user (str, optional): Explicit SSH username.
        port (int, optional): Explicit port number.
        backend (str): Registered backend name, resolved from the URL by default.
        backend_options (Mapping): Deeply immutable backend configuration.
    """

    url: str = field(repr=False)
    scheme: str
    host: str
    root: str = field(repr=False)
    user: str | None = field(default=None, repr=False)
    port: int | None = None
    backend: str | None = None
    backend_options: Mapping = field(default_factory=lambda: MappingProxyType({}),
                                     repr=False, hash=False)

    def __post_init__(self):
        selected = self.backend
        if selected is None:
            selected = "ssh" if self.scheme in {"ssh", "sftp"} else "http"
            if selected == "http" and (self.host == "cyverse.org"
                                      or self.host.endswith(".cyverse.org")):
                selected = "cyverse"
        object.__setattr__(self, "backend", backend_name(selected))
        object.__setattr__(self, "backend_options",
                           freeze_backend_options(self.backend_options))

    @classmethod
    def parse(cls, url: str, *,
              backend=None, backend_options=None) -> RemoteSpec:
        """
        Parse and validate a data-remote URL.

        Args:
            url (str): HTTP(S) URL or SSH/SFTP URL with an absolute dataset root.
            backend (str, optional): Registered backend name. Defaults to URL
                detection. Parsing does not load plugins.
            backend_options (Mapping, optional): Backend-specific configuration.

        Returns:
            RemoteSpec: Validated source description.

        Raises:
            RemoteConfigurationError: If the URL is invalid.
        """
        if not isinstance(url, str) or not url.strip():
            raise RemoteConfigurationError("Remote URL must be a non-empty string")
        reject_controls(url, "Remote URL")
        url = url.strip()
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
        return cls(url, scheme, host, root, user, port,
                   backend, backend_options)

    def pathname(self, relative_path: str) -> str:
        """Append a literal relative path to the dataset root."""
        return self.root.rstrip("/") + "/" + literal_path(relative_path).as_posix()

    def file_url(self, relative_path: str) -> str:
        """Encode a literal catalog path as a URL beneath the dataset root."""
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
        """Source scheme and host, with credentials and paths omitted."""
        host = self.host if re.fullmatch(r"[A-Za-z0-9_.:-]+", self.host) else "remote"
        return f"{self.scheme}://{host}"


class DataBackend:
    """
    Common interface for dataset discovery and data transfer.

    Implementations receive shared cancellation state and thread-local HTTP
    sessions through ``context``. Fetch may run concurrently: protect mutable
    backend state, check ``context.check_cancelled()`` during long operations,
    and release owned resources in ``close``. Authentication secrets belong in
    local configuration, not persisted backend options.

    Recursive listings should skip a subdirectory when ``context.descend`` is
    set and returns False for its relative path (ending in ``/``). Discovery
    sets it to avoid listing directories a filename template cannot match;
    backends that ignore it remain correct, only slower.

    Args:
        context (OperationContext): Resources and settings for this operation.
    """

    def __init__(self, context):
        self.context = context

    def prepare(self):
        """Prepare for discovery or transfer; repeated calls must be safe."""
        pass

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        """
        Transfer a file to a caller-managed temporary destination.

        Args:
            relative_path (str): Literal path relative to the dataset root.
            destination (Path): Local file to write.
            chunk_size (int): Read size for transports that support it.

        Checksum verification and atomic replacement are handled by the downloader.
        """
        raise NotImplementedError

    def list_entries(self):
        """Return the relative paths from a recursive directory listing."""
        return [entry.path for entry in self.iter_entries()]

    def iter_entries(self, on_directory=None):
        """
        Yield file metadata from a recursive directory listing.

        Args:
            on_directory (callable, optional): Callback receiving directory paths
                relative to the dataset root.

        Raises:
            CapabilityError: If the transport does not provide directory listings.
        """
        raise CapabilityError("Recursive listing is unsupported by this transport")

    def read_text(self, relative_path, limit):
        """
        Read a remote metadata file within a byte limit.

        Args:
            relative_path (str): Metadata path relative to the dataset root.
            limit (int): Maximum number of bytes to read.

        Returns:
            str: Decoded metadata text.

        Raises:
            CapabilityError: If the transport does not support metadata reads.
        """
        raise CapabilityError("Reading metadata is unsupported by this transport")

    def close(self):
        """Release connections and processes used by this transport."""
        pass

    def cancel(self):
        """Stop active backend work after the context's cancellation is set."""
        pass


# Existing transport imports remain valid for downstream integrations.
Transport = DataBackend
