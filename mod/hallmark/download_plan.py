"""Immutable descriptions of dataset transfers, separate from their approval."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlsplit, urlunsplit

from .transport.base import RemoteSpec, backend_name, freeze_backend_options


def _redacted_url(url: str) -> str:
    """Return a URL without credentials, query parameters, or fragments."""
    parsed = urlsplit(url)
    return urlunsplit(parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""))


@dataclass(frozen=True)
class DownloadSource:
    """
    The data location files are downloaded from.

    Attributes:
        url (str): Directory URL that cataloged paths are relative to.
        name (str, optional): Name of the configured data remote, if any.
        backend (str, optional): Registered backend selected for transfer.
        backend_options (mapping): Immutable backend configuration snapshot.
    """

    url: str = field(repr=False)
    name: Optional[str] = None
    backend: Optional[str] = None
    backend_options: Mapping = field(default_factory=dict, repr=False, hash=False)

    def __post_init__(self) -> None:
        """Validate the source fields and freeze the backend options."""
        if not isinstance(self.url, str) or not self.url:
            raise TypeError("source url must be a nonempty string")
        if any(value is not None and not isinstance(value, str)
               for value in (self.name, self.backend)):
            raise TypeError("source name and backend must be strings or None")
        if self.backend is not None:
            backend_name(self.backend)
        object.__setattr__(self, "backend_options",
                           freeze_backend_options(self.backend_options))

    @property
    def display(self) -> str:
        """The URL without credentials, query parameters, or fragments."""
        return _redacted_url(self.url)

    def remote(self) -> RemoteSpec:
        """Parse the source into a remote specification for transfer."""
        return RemoteSpec.parse(self.url, backend=self.backend,
                                backend_options=self.backend_options)


@dataclass(frozen=True)
class DownloadItem:
    """
    A file and its catalog metadata at the time a download is planned.

    Attributes:
        relative_path (Path): File path relative to the data source.
        checksum (str | tuple, optional): Digest or (algorithm, digest) pair.
        size_bytes (int, optional): Recorded file size; None if unknown.
        mtime (str, optional): Recorded modification time; None if unknown.
        source (DownloadSource, optional): Where the file is downloaded from;
            None uses the plan's source.
    """

    relative_path: Path
    checksum: Optional[Union[str, tuple[str, str]]] = None
    size_bytes: Optional[int] = None
    mtime: Optional[str] = None
    source: Optional[DownloadSource] = None

    def __post_init__(self) -> None:
        """Normalize the path and validate optional file metadata."""
        object.__setattr__(self, "relative_path", Path(self.relative_path))
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
        if self.source is not None and not isinstance(self.source, DownloadSource):
            raise TypeError("source must be a DownloadSource or None")


@dataclass(frozen=True)
class DownloadPlan:
    """
    An immutable selection of files, sources, and destination for a download.

    Creating a plan does not approve a transfer. Sizes come from the catalog;
    a duration estimate requires every file size and a supplied transfer rate.
    Files may come from several sources, such as templates added from
    different URLs. The plan-level source fields describe the source of items
    without their own source; plans with one source fill them in.

    Attributes:
        items (tuple[DownloadItem]): Selected files and their metadata.
        remote_url (str, optional): Data source URL; None for an empty plan or
            one whose items come from several sources.
        output_path (Path): Absolute destination directory.
        remote_name (str, optional): Name of the selected data remote.
        estimated_bytes_per_second (float, optional): Positive rate supplied
            by the caller for duration estimates.
        remote_backend (str, optional): Registered backend selected for transfer.
        backend_options (mapping): Immutable backend configuration snapshot.
        unsourced_count (int): Cataloged files skipped because no data source
            is configured for them.
    """

    items: tuple[DownloadItem, ...]
    remote_url: Optional[str] = field(repr=False)
    output_path: Path
    remote_name: Optional[str] = None
    estimated_bytes_per_second: Optional[float] = None
    remote_backend: Optional[str] = None
    backend_options: Mapping = field(default_factory=dict, repr=False, hash=False)
    unsourced_count: int = 0

    def __post_init__(self) -> None:
        """Copy the selection and validate the source fields and rate."""
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(
            self, "output_path", Path(self.output_path).expanduser().absolute())
        if any(not isinstance(item, DownloadItem) for item in self.items):
            raise TypeError("items must contain DownloadItem values")
        if any(value is not None and not isinstance(value, str) for value in
               (self.remote_url, self.remote_name,
                self.remote_backend)):
            raise TypeError("remote fields must be strings or None")
        if self.remote_backend is not None:
            backend_name(self.remote_backend)
        object.__setattr__(self, "backend_options",
                           freeze_backend_options(self.backend_options))
        if (isinstance(self.unsourced_count, bool)
                or not isinstance(self.unsourced_count, int)
                or self.unsourced_count < 0):
            raise ValueError("unsourced_count must be a nonnegative integer")
        rate = self.estimated_bytes_per_second
        if rate is not None and (
            isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not isfinite(rate) or rate <= 0
        ):
            raise ValueError("estimated_bytes_per_second must be a positive rate")

    @property
    def default_source(self) -> Optional[DownloadSource]:
        """The plan-level source, or None when the plan has none."""
        if not self.remote_url:
            return None
        return DownloadSource(self.remote_url, self.remote_name,
                              self.remote_backend, self.backend_options)

    def source_for(self, item: DownloadItem) -> Optional[DownloadSource]:
        """Return the source an item is downloaded from."""
        return item.source or self.default_source

    @property
    def sources(self) -> tuple[DownloadSource, ...]:
        """Distinct sources of the selected files, in plan order."""
        sources = []
        for item in self.items:
            source = self.source_for(item)
            if source is not None and source not in sources:
                sources.append(source)
        return tuple(sources)

    @property
    def file_count(self) -> int:
        """Number of files selected for download."""
        return len(self.items)

    @property
    def known_bytes(self) -> int:
        """Sum of recorded sizes, excluding files whose sizes are unknown."""
        return sum(item.size_bytes or 0 for item in self.items)

    @property
    def unknown_size_count(self) -> int:
        """Number of files without a recorded size."""
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
            URL credentials, query parameters, and fragments are omitted.
        """
        size = f"{self.known_bytes:,} bytes"
        if self.unknown_size_count:
            size += f" known; {self.unknown_size_count} file(s) with unknown size"
        duration = ("unknown" if self.estimated_seconds is None
                    else f"about {self.estimated_seconds:.1f} seconds")
        sources = self.sources
        if len(sources) > 1:
            counts = {source: 0 for source in sources}
            for item in self.items:
                counts[self.source_for(item)] += 1
            lines = "".join(
                f"\n  {source.display} ({count} file(s)"
                + (f"; transport {source.backend}" if source.backend else "") + ")"
                for source, count in counts.items())
            origin = f"\nSources:{lines}"
        else:
            source = (sources[0] if sources else self.default_source)
            origin = f"\nSource: {source.display if source else '<none>'}"
            if source is not None and source.backend:
                origin += f"\nTransport: {source.backend}"
        skipped = (f"\nSkipped {self.unsourced_count} cataloged file(s) without a "
                   "data source" if self.unsourced_count else "")
        return (f"{self.file_count} file(s); {size}; estimated duration: {duration}"
                f"{origin}"
                f"\nDestination: {self.output_path}{skipped}")
