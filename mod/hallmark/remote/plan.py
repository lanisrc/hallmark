"""Immutable descriptions of dataset transfers, separate from their approval."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlsplit, urlunsplit

from ..transport.base import validate_backend_name, readonly_backend_options
from ..utils import is_valid_checksum


@dataclass(frozen=True)
class DownloadItem:
    """
    A file and its catalog metadata at the time a download is planned.

    Attributes:
        relative_path (Path): Catalogue path, relative to the destination.
        checksum (str | tuple, optional): Digest or (algorithm, digest) pair.
        size_bytes (int, optional): Recorded file size; None if unknown.
        mtime (str, optional): Recorded modification time; None if unknown.
        remote (str, optional): Name of the plan remote serving the file;
            None for the plan's single remote.
        remote_path (Path, optional): Path beneath that remote's URL when it
            differs from ``relative_path``, as for a source with a prefix.
        sha1 (str, optional): SHA-1 of the file's content when the catalogue
            already knows it, for example from an earlier download.
        replaces (str, optional): SHA-1 of an outdated file at the
            destination that this item replaces. Its bytes stay in the
            object store.
    """

    relative_path: Path
    checksum: Optional[Union[str, tuple[str, str]]] = None
    size_bytes: Optional[int] = None
    mtime: Optional[str] = None
    remote: Optional[str] = None
    remote_path: Optional[Path] = None
    sha1: Optional[str] = None
    replaces: Optional[str] = None

    def __post_init__(self) -> None:
        """Normalize the path and validate optional file metadata."""
        object.__setattr__(self, "relative_path", Path(self.relative_path))
        if self.remote_path is not None:
            object.__setattr__(self, "remote_path", Path(self.remote_path))
        if self.remote is not None and not isinstance(self.remote, str):
            raise ValueError("remote must be a remote name or None")
        for name in ("sha1", "replaces"):
            value = getattr(self, name)
            if value is not None:
                if not is_valid_checksum("sha1", value):
                    raise ValueError(
                        f"{name} must be 40 hexadecimal characters or None")
                object.__setattr__(self, name, value.lower())
        if isinstance(self.checksum, list):
            object.__setattr__(self, "checksum", tuple(self.checksum))
        if self.checksum is not None and not (
            isinstance(self.checksum, str)
            or (isinstance(self.checksum, tuple) and len(self.checksum) == 2
                and all(isinstance(part, str) for part in self.checksum))
        ):
            raise ValueError("checksum must be a string or algorithm/value pair")
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a nonnegative integer or None")
        if self.mtime is not None and not isinstance(self.mtime, str):
            raise ValueError("mtime must be a string or None")

    @property
    def source_path(self) -> Path:
        """Path of the file beneath its remote's URL."""
        return self.remote_path if self.remote_path is not None else self.relative_path


@dataclass(frozen=True)
class DownloadPlan:
    """
    An immutable selection of files, source, and destination for a download.

    Creating a plan does not approve a transfer. Sizes come from the catalog;
    a duration estimate requires the size of every file to transfer and a
    supplied transfer rate.

    Attributes:
        items (tuple[DownloadItem]): Files to transfer and their metadata.
        remote_url (str, optional): Data source URL; None when no remote is
            configured or the items come from several remotes.
        output_path (Path): Absolute destination directory.
        remote_auth (str, optional): Local authentication profile name.
        remote_name (str, optional): Name of the selected data remote.
        estimated_bytes_per_second (float, optional): Positive rate supplied
            by the caller for duration estimates.
        remote_backend (str, optional): Registered backend selected for transfer.
        backend_options (mapping): Immutable backend configuration snapshot.
        skipped_items (tuple[DownloadItem]): Existing files matching their checksums.
        remotes (mapping): Every remote the items name, by name, with its
            ``url``, ``auth``, ``backend`` and ``backend_options`` pinned. The
            ``remote_*`` fields describe the remote when there is only one.
        stored_items (tuple[DownloadItem]): Files whose content is already in
            the object store; they are placed without a transfer.

    Items whose ``replaces`` is set, listed by ``outdated_items``, replace an
    earlier download that no longer matches its catalogue row.
    """

    items: tuple[DownloadItem, ...]
    remote_url: Optional[str] = field(repr=False)
    output_path: Path
    remote_auth: Optional[str] = None
    remote_name: Optional[str] = None
    estimated_bytes_per_second: Optional[float] = None
    remote_backend: Optional[str] = None
    backend_options: Mapping = field(default_factory=dict, repr=False, hash=False)
    skipped_items: tuple[DownloadItem, ...] = ()
    remotes: Mapping = field(default_factory=dict, repr=False, hash=False)
    stored_items: tuple[DownloadItem, ...] = ()

    def __post_init__(self) -> None:
        """Copy the selection and validate the source fields and rate."""
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "skipped_items", tuple(self.skipped_items))
        object.__setattr__(self, "stored_items", tuple(self.stored_items))
        object.__setattr__(
            self, "output_path", Path(self.output_path).expanduser().absolute())
        if any(not isinstance(item, DownloadItem)
               for item in self.items + self.skipped_items + self.stored_items):
            raise TypeError("items must contain DownloadItem values")
        if any(item.sha1 is None for item in self.stored_items):
            raise ValueError("Cached items must record their SHA-1")
        if any(value is not None and not isinstance(value, str) for value in
               (self.remote_url, self.remote_auth, self.remote_name,
                self.remote_backend)):
            raise TypeError("remote fields must be strings or None")
        if self.remote_backend is not None:
            validate_backend_name(self.remote_backend)
        object.__setattr__(self, "backend_options",
                           readonly_backend_options(self.backend_options))
        remotes = readonly_backend_options(self.remotes)
        for name, remote in remotes.items():
            if (not isinstance(remote, Mapping)
                    or not isinstance(remote.get("url"), str)):
                raise TypeError(f"Plan remote {name!r} must record its URL")
            if remote.get("backend") is not None:
                validate_backend_name(remote["backend"])
        object.__setattr__(self, "remotes", remotes)
        unknown = {item.remote for item in
                   self.items + self.skipped_items + self.stored_items
                   if item.remote is not None} - set(remotes)
        if unknown:
            raise ValueError("Items refer to remotes the plan does not record")
        rate = self.estimated_bytes_per_second
        if rate is not None and (
            isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not isfinite(rate) or rate <= 0
        ):
            raise ValueError("estimated_bytes_per_second must be a positive rate")

    @property
    def file_count(self) -> int:
        """Number of files the plan places, including those from the object store."""
        return len(self.items) + len(self.stored_items)

    @property
    def has_changes(self) -> bool:
        """Whether executing the plan would place any file."""
        return bool(self.file_count)

    @property
    def outdated_items(self) -> tuple[DownloadItem, ...]:
        """Items that replace an outdated file at their destination."""
        return tuple(item for item in self.items + self.stored_items
                     if item.replaces is not None)

    @property
    def known_bytes(self) -> int:
        """Bytes to transfer, excluding files whose sizes are unknown."""
        return sum(item.size_bytes or 0 for item in self.items)

    @property
    def unknown_size_count(self) -> int:
        """Number of files to transfer without a recorded size."""
        return sum(item.size_bytes is None for item in self.items)

    @property
    def total_bytes(self) -> Optional[int]:
        """Estimated transfer size, or None if any file size is unknown."""
        return None if self.unknown_size_count else self.known_bytes

    @property
    def estimated_seconds(self) -> Optional[float]:
        """Estimated duration, or None when sizes or the transfer rate are unknown."""
        if self.total_bytes is None or self.estimated_bytes_per_second is None:
            return None
        return self.total_bytes / self.estimated_bytes_per_second

    def summary(self) -> str:
        """
        Describe the planned transfer, including unavailable estimates.

        Returns:
            str: File count, size and duration estimates, source, and destination.
            A plan using several remotes lists each with its file count. Further
            lines count files placed from ``.hm/objects`` and outdated files to be
            replaced, and name files already downloaded. URL credentials, query
            parameters, and fragments are omitted.
        """
        size = f"{self.known_bytes:,} bytes"
        if self.unknown_size_count:
            size += f" known; {self.unknown_size_count} file(s) with unknown size"
        duration = ("unknown" if self.estimated_seconds is None
                    else f"about {self.estimated_seconds:.1f} seconds")
        source = "Source: <none>"
        if self.remote_url:
            source = f"Source: {_display_url(self.remote_url)}"
        if self.remote_backend:
            source += f"\nBackend: {self.remote_backend}"
        if len(self.remotes) > 1:
            counts = {}
            for item in self.items + self.stored_items:
                counts[item.remote] = counts.get(item.remote, 0) + 1
            source = "Sources:" + "".join(
                f"\n  {name}: {_display_url(remote['url'])} "
                f"({counts.get(name, 0)} file(s))"
                for name, remote in self.remotes.items())
        skipped = ""
        if self.stored_items:
            skipped += (f"\nAlready in .hm/objects: {len(self.stored_items)} file(s) "
                        "(no transfer)")
        outdated = self.outdated_items
        if outdated:
            skipped += (f"\nOutdated: {len(outdated)} file(s) will be replaced "
                        "(old versions stay in .hm/objects)")
            unverified = sum(item.checksum is None and item.sha1 is None
                             for item in outdated)
            if unverified:
                skipped += (f"; {unverified} without a catalogued checksum are "
                            "fetched to compare and kept if unchanged")
        for item in self.skipped_items:
            skipped += f"\nAlready downloaded: {item.relative_path.as_posix()}"
        return (f"{self.file_count} file(s); {size}; estimated duration: {duration}"
                f"\n{source}"
                f"\nDestination: {self.output_path}{skipped}")


def _display_url(url: str) -> str:
    """Return a URL without user information, query or fragment."""
    parsed = urlsplit(url)
    return urlunsplit(parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""))
