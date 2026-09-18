"""Immutable descriptions of dataset transfers, separate from their approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class DownloadItem:
    """A catalog file and the metadata available when a transfer was planned."""

    relative_path: Path
    checksum: Optional[Union[str, tuple[str, str]]] = None
    size_bytes: Optional[int] = None
    mtime: Optional[str] = None

    def __post_init__(self) -> None:
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


@dataclass(frozen=True)
class DownloadPlan:
    """The exact files, source, and destination presented for approval.

    Sizes describe catalog metadata; they are estimates until the transfer ends.
    A duration is available only when every size and a supplied rate are known.
    """

    items: tuple[DownloadItem, ...]
    remote_url: Optional[str] = field(repr=False)
    output_path: Path
    remote_auth: Optional[str] = None
    remote_name: Optional[str] = None
    estimated_bytes_per_second: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(
            self, "output_path", Path(self.output_path).expanduser().absolute())
        if any(not isinstance(item, DownloadItem) for item in self.items):
            raise TypeError("items must contain DownloadItem values")
        if any(value is not None and not isinstance(value, str) for value in
               (self.remote_url, self.remote_auth, self.remote_name)):
            raise TypeError("remote fields must be strings or None")
        rate = self.estimated_bytes_per_second
        if rate is not None and (
            isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not isfinite(rate) or rate <= 0
        ):
            raise ValueError("estimated_bytes_per_second must be a positive rate")

    @property
    def file_count(self) -> int:
        """Number of files authorized if this plan is approved."""
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
        """Estimated duration from a supplied rate, never a network probe."""
        if self.total_bytes is None or self.estimated_bytes_per_second is None:
            return None
        return self.total_bytes / self.estimated_bytes_per_second

    def summary(self) -> str:
        """Describe the planned transfer without hiding unavailable estimates."""
        size = f"{self.known_bytes:,} bytes"
        if self.unknown_size_count:
            size += f" known; {self.unknown_size_count} file(s) with unknown size"
        duration = ("unknown" if self.estimated_seconds is None
                    else f"about {self.estimated_seconds:.1f} seconds")
        source = "<none>"
        if self.remote_url:
            parsed = urlsplit(self.remote_url)
            source = urlunsplit(parsed._replace(
                netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""))
        return (f"{self.file_count} file(s); {size}; estimated duration: {duration}"
                f"\nSource: {source}"
                f"\nDestination: {self.output_path}")
