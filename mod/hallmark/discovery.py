"""Discover remote file metadata without downloading dataset contents."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import PurePosixPath

import parse
from tqdm import tqdm

from .helper_functions import CHECKSUM_ALGORITHMS_BY_STRENGTH, valid_checksum
from .transport.base import (
    DownloadError, RemoteEntry, RemoteObjectMissing, literal_path,
)


def _glob_matches(path, pattern):
    """Match path segments, including zero-directory matches for ``**/``."""
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(maxsize=None)
    def match(path_index, pattern_index):
        if pattern_index == len(patterns):
            return path_index == len(parts)
        if patterns[pattern_index] == "**":
            return (match(path_index, pattern_index + 1)
                    or path_index < len(parts) and match(path_index + 1, pattern_index))
        return (path_index < len(parts)
                and fnmatch.fnmatchcase(parts[path_index], patterns[pattern_index])
                and match(path_index + 1, pattern_index + 1))

    return match(0, 0)


@lru_cache(maxsize=128)
def _format_parser(fmt):
    """Compile and cache a case-sensitive filename format."""
    return parse.compile(fmt, case_sensitive=True)


def path_matches(path: str, filter=None, fmt: str | None = None) -> bool:
    """
    Match a relative path against globs and a filename format.

    Args:
        path (str): Path relative to the dataset root.
        filter (str | list[str], optional): Globs to match. At least one
            must match; ``**`` spans zero or more directories.
        fmt (str, optional): Filename format that the complete path must match.

    Returns:
        bool: True when the path satisfies every supplied selector.

    Raises:
        ValueError: If the filter or format is invalid.
    """
    path = str(path)
    parser = _format_parser(fmt) if fmt is not None else None
    if filter is not None:
        if not isinstance(filter, (str, list, tuple)):
            raise ValueError(
                "Path filters must be a glob string or a list of glob strings")
        patterns = [filter] if isinstance(filter, str) else list(filter)
        if not all(isinstance(pattern, str) for pattern in patterns):
            raise ValueError("Path filters must be glob strings")
        if not any(_glob_matches(path, pattern) for pattern in patterns):
            return False
    return parser is None or parser.parse(path) is not None


def _manifest_algorithm(path):
    """Identify checksum manifests by their conventional filenames."""
    name = PurePosixPath(path).name.lower()
    if re.fullmatch(r"checksums?(?:\.txt)?", name):
        return "unknown"
    for algorithm in CHECKSUM_ALGORITHMS_BY_STRENGTH:
        if (re.fullmatch(rf"{algorithm}sums?(?:\.txt)?", name)
                or re.fullmatch(rf".+\.{algorithm}sums?", name)):
            return algorithm
    return None


def _manifest_checksums(context, entries):
    """Read published checksum metadata and attach it to discovered paths."""
    strength = {name: index for index, name in
                enumerate(CHECKSUM_ALGORITHMS_BY_STRENGTH)}
    for path in sorted(entries):
        algorithm = _manifest_algorithm(path)
        if algorithm is None:
            continue
        try:
            text = context.read_text(path)
        except RemoteObjectMissing:
            continue
        parent = PurePosixPath(path).parent
        for line in text.splitlines():
            if line.startswith("\\"):
                raise DownloadError("GNU escaped manifest filenames are unsupported")
            match = re.fullmatch(r"([0-9a-fA-F]{8,}) [ *](.+)", line)
            if not match:
                continue
            digest, filename = match.groups()
            if not valid_checksum(algorithm, digest, allow_unknown_algorithm=True):
                continue
            while filename.startswith("./"):
                filename = filename[2:]
            filename = literal_path(filename).as_posix()
            candidates = [(parent / filename).as_posix(), filename]
            if parent.name == PurePosixPath(filename).parts[0]:
                candidates.append((parent.parent / filename).as_posix())
            target = next((candidate for candidate in candidates
                           if candidate in entries), None)
            if target is None:
                continue
            previous = entries[target]
            if (previous.checksum_algorithm == algorithm and previous.checksum
                    and previous.checksum.lower() != digest.lower()):
                raise DownloadError(f"Conflicting {algorithm} manifests for {target!r}")
            if (previous.checksum is None
                    or strength.get(algorithm, 99)
                    < strength.get(previous.checksum_algorithm, 100)):
                entries[target] = replace(
                    previous, checksum_algorithm=algorithm, checksum=digest.lower())


def discover(context, *, filter=None, fmt=None, progress=False) -> list[RemoteEntry]:
    """
    Discover remote files and their published metadata recursively.

    Directory listings and checksum manifests are read before filtering.
    Dataset files are not downloaded to compute missing checksums.

    Args:
        context (OperationContext): Source connection and cancellation state.
        filter (str | list[str], optional): Relative path glob or globs.
        fmt (str, optional): Filename format that selected paths must match.
        progress (bool | callable): True displays a progress bar with an
            unknown total. A callback receives dictionaries containing
            ``directories``, ``files``, ``matched``, and ``current``.
            Defaults to False.

    Returns:
        list[RemoteEntry]: Matching files sorted by relative path.
        Unavailable sizes, modification times, and checksums remain None.

    Raises:
        ValueError: If a filter or format is invalid.
        CapabilityError: If the source provides no supported listing.
        DownloadError: If discovery fails, paths are unsafe, or manifests conflict.
    """
    # Validate selectors before contacting the source, even for an empty index.
    path_matches("", filter=filter, fmt=fmt)
    entries = {}
    counts = {"directories": 0, "files": 0, "matched": 0, "current": ""}
    bar = tqdm(total=None, unit="dir", desc="Discovering", disable=not bool(progress)) \
        if not callable(progress) else None

    def report(directory=None):
        if directory is not None:
            counts["directories"] += 1
            counts["current"] = directory or "/"
            if bar is not None:
                bar.update()
        if callable(progress):
            progress(dict(counts))
        elif bar is not None:
            bar.set_postfix(files=counts["files"], matched=counts["matched"])

    def add(entry):
        path = literal_path(entry.path).as_posix()
        if path not in entries:
            entries[path] = entry
            counts["files"] += 1
            counts["matched"] += int(path_matches(path, filter=filter, fmt=fmt))
            report()

    try:
        context.transport.prepare()
        for entry in context.transport.iter_entries(on_directory=report):
            context.check_cancelled()
            add(entry)
        _manifest_checksums(context, entries)
        return [entries[path] for path in sorted(entries)
                if path_matches(path, filter=filter, fmt=fmt)]
    finally:
        if bar is not None:
            bar.close()
