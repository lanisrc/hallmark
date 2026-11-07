"""Discover remote file metadata without downloading dataset contents."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import PurePosixPath

import parse
from tqdm import tqdm

from ..utils import (
    CHECKSUM_ALGORITHMS_BY_STRENGTH, infer_checksum_algorithm, is_valid_checksum)
from ..transport.base import (
    DownloadError, RemoteEntry, RemoteObjectMissing, validate_remote_path,
)


def _matches_wildcard_path(path, pattern):
    """Match path segments, including zero-directory matches for ``**/``."""
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(maxsize=None)
    def match_path_parts(path_index, pattern_index):
        if pattern_index == len(patterns):
            return path_index == len(parts)
        if patterns[pattern_index] == "**":
            return (
                match_path_parts(path_index, pattern_index + 1)
                or path_index < len(parts)
                and match_path_parts(path_index + 1, pattern_index)
            )
        return (path_index < len(parts)
                and fnmatch.fnmatchcase(parts[path_index], patterns[pattern_index])
                and match_path_parts(path_index + 1, pattern_index + 1))

    return match_path_parts(0, 0)


def path_matches(path: str, filter=None, fmt: str | None = None) -> bool:
    """
    Match a relative path against globs and a filename format.

    Globs are matched one path segment at a time and are case-sensitive, so
    ``*``, ``?`` and ``[...]`` never match a ``/``. Only a whole ``**``
    segment spans folders, zero or more of them: ``a/**/b.fits`` matches
    ``a/b.fits`` and ``a/x/y/b.fits``, while ``**.fits`` acts like
    ``*.fits`` and matches no path inside a folder.

    Args:
        path (str): Path relative to the dataset root.
        filter (str | list[str], optional): Globs to match. At least one
            must match. None or an empty filter (``""``, ``[]`` or ``()``)
            means no filter.
        fmt (str, optional): Filename format that the complete path must match.

    Returns:
        bool: True when the path satisfies every supplied selector.

    Raises:
        ValueError: If the filter is not a glob string or a list of glob
            strings, or the format is not a valid filename format string.
    """
    path = str(path)
    if fmt is not None and not isinstance(fmt, str):
        raise ValueError("Filename format must be a string")
    parser = parse.compile(fmt, case_sensitive=True) if fmt is not None else None
    if filter is not None:
        if not isinstance(filter, (str, list, tuple)):
            raise ValueError(
                "Path filters must be a glob string or a list of glob strings")
        patterns = [filter] if isinstance(filter, str) else list(filter)
        if not all(isinstance(pattern, str) for pattern in patterns):
            raise ValueError("Path filters must be glob strings")
        if filter and not any(
                _matches_wildcard_path(path, pattern) for pattern in patterns):
            return False
    return parser is None or parser.parse(path) is not None


def _checksum_file_algorithm(path):
    """
    Identify checksum manifests by their conventional filenames.

    A plain ``checksums.txt`` does not say its algorithm; each digest's
    length tells it instead (``"unknown"`` here).
    """
    name = PurePosixPath(path).name.lower()
    if re.fullmatch(r"checksums?(?:\.txt)?", name):
        return "unknown"
    for algorithm in CHECKSUM_ALGORITHMS_BY_STRENGTH:
        if (re.fullmatch(rf"{algorithm}sums?(?:\.txt)?", name)
                or re.fullmatch(rf".+\.{algorithm}sums?", name)):
            return algorithm
    return None


def _attach_published_checksums(context, entries):
    """
    Read published checksum metadata and attach it to discovered paths.

    Each file keeps its strongest digest. Two published digests of one
    algorithm that disagree, or one that disagrees with the server
    listing's digest, raise DownloadError whatever order they are read in.
    """
    strength = {name: index for index, name in
                enumerate(CHECKSUM_ALGORITHMS_BY_STRENGTH)}
    known = {(path, entry.checksum_algorithm):
             (entry.checksum.lower(), entry.checksum_source)
             for path, entry in entries.items() if entry.checksum}
    for path in sorted(entries):
        manifest_algorithm = _checksum_file_algorithm(path)
        if manifest_algorithm is None:
            continue
        try:
            text = context.read_text(path, large=True)
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
            digest = digest.lower()
            algorithm = (infer_checksum_algorithm(digest)
                         if manifest_algorithm == "unknown" else manifest_algorithm)
            if algorithm is None or not is_valid_checksum(algorithm, digest):
                continue
            while filename.startswith("./"):
                filename = filename[2:]
            filename = validate_remote_path(filename).as_posix()
            candidates = [(parent / filename).as_posix()]
            if "/" in filename:
                candidates.append(filename)
            if parent.name == PurePosixPath(filename).parts[0]:
                candidates.append((parent.parent / filename).as_posix())
            target = next((candidate for candidate in candidates
                           if candidate in entries), None)
            if target is None:
                continue
            earlier, source = known.setdefault(
                (target, algorithm), (digest, "published"))
            if earlier != digest:
                if source == "server":
                    raise DownloadError(
                        f"Published {algorithm} checksum for {target!r} "
                        "disagrees with the server's checksum")
                raise DownloadError(f"Conflicting {algorithm} manifests for {target!r}")
            previous = entries[target]
            if (previous.checksum is None
                    or strength.get(algorithm, 99)
                    < strength.get(previous.checksum_algorithm, 100)):
                entries[target] = replace(
                    previous, checksum_algorithm=algorithm, checksum=digest,
                    checksum_source="published")


def _attach_server_checksums(context, entries, algorithm):
    """Ask the server for digests of files without one at least as strong."""
    strength = {name: index for index, name in
                enumerate(CHECKSUM_ALGORITHMS_BY_STRENGTH)}
    needed = [entry for entry in entries
              if entry.checksum is None or strength.get(entry.checksum_algorithm, 100)
              > strength[algorithm]]
    if not needed:
        return entries
    digests = context.transport.server_checksums(
        [entry.path for entry in needed], algorithm)
    hashed = {}
    for entry in needed:
        digest = digests.get(entry.path)
        if not is_valid_checksum(algorithm, digest):
            raise DownloadError(
                f"The server returned no valid {algorithm} checksum "
                f"for {entry.path!r}")
        hashed[entry.path] = replace(
            entry, checksum_algorithm=algorithm, checksum=digest.strip().lower(),
            checksum_source="server")
    return [hashed.get(entry.path, entry) for entry in entries]


def discover_remote_files(
    context, *, filter=None, fmt=None, progress=False, server_checksum=None
) -> list[RemoteEntry]:
    """
    Discover remote files and their published metadata recursively.

    Directory listings and checksum manifests are read before filtering.
    Dataset files are not downloaded to compute missing checksums.

    Args:
        context (OperationContext): Source connection and cancellation state.
        filter (str | list[str], optional): Relative path glob or globs,
            matched as :func:`path_matches` does. An empty filter selects
            every file.
        fmt (str, optional): Filename format that selected paths must match.
        progress (bool | callable): True displays a progress bar with an
            unknown total. A callback receives dictionaries containing
            ``directories``, ``files``, ``matched``, and ``current``
            (``"/"`` for the root folder). Defaults to False.
        server_checksum (str, optional): Have the server compute this
            algorithm for matching files without an equal or stronger
            published checksum. Only some backends can.

    Returns:
        list[RemoteEntry]: Matching files sorted by relative path, each with
        its normalized path (``./a.fits`` becomes ``a.fits``).
        Unavailable sizes, modification times, and checksums remain None.

    Raises:
        ValueError: If a filter, format or ``server_checksum`` is invalid.
        CapabilityError: If the source provides no supported listing, or a
            selected file needs a ``server_checksum`` checksum that the
            server cannot compute.
        DownloadError: If discovery fails, paths are unsafe, manifests
            conflict with each other or with the server listing, or the
            server returns a missing or invalid checksum.
    """
    # Validate selectors before contacting the source, even for an empty index.
    path_matches("", filter=filter, fmt=fmt)
    if server_checksum is not None and \
            server_checksum not in CHECKSUM_ALGORITHMS_BY_STRENGTH:
        raise ValueError(f"Unsupported server checksum {server_checksum!r}")
    entries = {}
    counts = {"directories": 0, "files": 0, "matched": 0, "current": ""}
    bar = tqdm(total=None, unit="dir", desc="Discovering", disable=not bool(progress)) \
        if not callable(progress) else None

    def report_progress(directory=None):
        if directory is not None:
            counts["directories"] += 1
            counts["current"] = directory or "/"
            if bar is not None:
                bar.update()
        if callable(progress):
            progress(dict(counts))
        else:
            bar.set_postfix(files=counts["files"], matched=counts["matched"])

    def add_file_entry(entry):
        path = validate_remote_path(entry.path).as_posix()
        if path not in entries:
            entries[path] = (replace(entry, path=path) if path != entry.path
                             else entry)
            counts["files"] += 1
            counts["matched"] += int(path_matches(path, filter=filter, fmt=fmt))
            report_progress()

    try:
        context.transport.prepare()
        for entry in context.transport.iter_entries(on_directory=report_progress):
            context.check_cancelled()
            add_file_entry(entry)
        _attach_published_checksums(context, entries)
        selected = [entries[path] for path in sorted(entries)
                    if path_matches(path, filter=filter, fmt=fmt)]
        selected = context.transport.complete_entries(selected)
        if server_checksum is not None:
            selected = _attach_server_checksums(context, selected, server_checksum)
        return selected
    finally:
        if bar is not None:
            bar.close()
