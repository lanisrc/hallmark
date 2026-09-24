"""Discover remote file metadata without downloading dataset contents."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import PurePosixPath
from string import Formatter
from typing import Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import parse
from tqdm import tqdm

from .helper_functions import CHECKSUM_ALGORITHMS_BY_STRENGTH, valid_checksum
from .repo_config import RESERVED_FIELDS
from .transport.base import (
    DownloadError, RemoteEntry, RemoteObjectMissing, literal_path, reject_controls,
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


CATALOG_COLUMNS = ("path", "checksum_algorithm", "checksum", "size_bytes", "mtime")


@lru_cache(maxsize=128)
def extraction_parser(template):
    """Validate a named filename template and return its parser and columns."""
    if not isinstance(template, str) or not template.strip():
        raise ValueError("format must be a nonempty filename template")
    fields = []
    for _, name, _, conversion in Formatter().parse(template):
        if name is None:
            continue
        if not name.isidentifier() or conversion is not None:
            raise ValueError(
                "Extraction fields must be simple names without conversions")
        if name in {*CATALOG_COLUMNS, *RESERVED_FIELDS}:
            raise ValueError(f"Extraction field {name!r} is reserved catalog metadata")
        if name not in fields:
            fields.append(name)
    if not fields:
        raise ValueError("Extraction templates require at least one named field")
    parser = parse.compile(template, case_sensitive=True)
    # Force regular-expression compilation now, before accessing the source.
    parser.parse("")
    return parser, tuple(fields)


def _template_parts(segment: str):
    """Split one template segment into literal text and field definitions."""
    try:
        return list(Formatter().parse(segment))
    except ValueError as exc:
        raise ValueError(f"Invalid filename template segment {segment!r}: {exc}") \
            from None


def _escape_braces(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def is_remote_url(value) -> bool:
    """Return True for a string that names a remote location, such as a URL."""
    return isinstance(value, str) and "://" in value


def split_url_template(url: str) -> tuple[str, Optional[str]]:
    """
    Split a URL template at the first path segment containing a field.

    The part before that segment is the remote directory; the rest is a
    filename template relative to it. Literal parts of the template are
    percent-decoded once, so ``%20`` names a space and ``%7B`` a literal brace.
    A URL without fields names one file, unless it ends in ``/``.

    Args:
        url (str): URL such as ``https://host/ER2/{src}_{day}.h5``.

    Returns:
        tuple[str, str | None]: The directory URL, ending in ``/``, and the
        template; the template is None for a directory URL.

    Raises:
        ValueError: If the URL has credentials, a query or fragment, fields
            outside its path, or an invalid template.
    """
    if not is_remote_url(url):
        raise ValueError("Expected a URL such as https://host/path/{field}.ext")
    reject_controls(url, "URL")
    parsed = urlsplit(url)
    if any(brace in parsed.scheme + parsed.netloc for brace in "{}"):
        raise ValueError("Template fields are allowed only in the URL path")
    if "?" in url or "#" in url:
        raise ValueError("Remote URLs must not contain a query or fragment")
    if parsed.password is not None or (
            parsed.username is not None and parsed.scheme in {"http", "https"}):
        raise ValueError(
            "Remote URLs must not contain credentials; configure them locally, "
            "for example in ~/.netrc or ~/.ssh/config")
    segments = parsed.path.split("/")
    index = next((position for position, segment in enumerate(segments)
                  if any(name is not None
                         for _, name, _, _ in _template_parts(segment))), None)
    if index is None:
        if not parsed.path.strip("/") or parsed.path.endswith("/"):
            return url, None
        index = len(segments) - 1
    base = urlunsplit((parsed.scheme, parsed.netloc,
                       "/".join(segments[:index]) + "/", "", ""))
    decoded = []
    for segment in segments[index:]:
        text = []
        for literal, name, spec, conversion in _template_parts(segment):
            text.append(_escape_braces(unquote(literal)))
            if name is not None:
                if conversion is not None:
                    raise ValueError("Template fields cannot use conversions")
                text.append("{" + name + (f":{spec}" if spec else "") + "}")
        decoded.append("".join(text))
    return base, "/".join(decoded)


def template_url(base: str, template: str) -> str:
    """Join a directory URL and a template, percent-encoding literal text."""
    segments = []
    for segment in template.split("/"):
        text = []
        for literal, name, spec, _ in _template_parts(segment):
            text.append(quote(literal, safe=""))
            if name is not None:
                text.append("{" + name + (f":{spec}" if spec else "") + "}")
        segments.append("".join(text))
    return base.rstrip("/") + "/" + "/".join(segments)


class TemplateMatcher:
    """
    Match relative paths against a filename template, one segment at a time.

    Fields never span ``/``, and a path segment starting with ``.`` matches
    only a template segment starting with ``.``, as with local glob patterns.

    Args:
        template (str): Filename template such as ``{src}/{day}.h5``.

    Raises:
        ValueError: If the template is invalid or uses reserved field names.
    """

    def __init__(self, template: str):
        if not isinstance(template, str) or not template.strip():
            raise ValueError("format must be a nonempty filename template")
        segments = template.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError(
                f"Template {template!r} must be a relative path naming files")
        self.template = template
        self.fields = []
        self._parsers, self._hidden, globs = [], [], []
        for segment in segments:
            stripped, pattern = [], []
            for literal, name, _, conversion in _template_parts(segment):
                stripped.append(_escape_braces(literal))
                pattern.append(re.sub(r"([\[\]*?])", r"[\1]", literal))
                if name is None:
                    continue
                if not name.isidentifier() or conversion is not None:
                    raise ValueError(
                        "Template fields must be simple names without conversions")
                if name in RESERVED_FIELDS:
                    raise ValueError(
                        f"Template field {name!r} is reserved catalog metadata")
                if name not in self.fields:
                    self.fields.append(name)
                stripped.append("{" + name + "}")
                pattern.append("*")
            self._parsers.append(parse.compile("".join(stripped), case_sensitive=True))
            self._hidden.append(segment.startswith("."))
            globs.append(re.sub(r"\*+", "*", "".join(pattern)))
        self.glob = "/".join(globs)

    def _match_segment(self, index: int, part: str) -> Optional[dict]:
        if part.startswith(".") and not self._hidden[index]:
            return None
        result = self._parsers[index].parse(part)
        return None if result is None else dict(result.named)

    def match(self, path: str) -> Optional[dict]:
        """Return the field values of a matching path, or None."""
        parts = path.split("/")
        if len(parts) != len(self._parsers):
            return None
        values = {}
        for index, part in enumerate(parts):
            named = self._match_segment(index, part)
            if named is None:
                return None
            for name, value in named.items():
                if values.setdefault(name, value) != value:
                    return None
        return values

    def may_contain(self, directory: str) -> bool:
        """Return True if files under a relative directory could match."""
        parts = [part for part in directory.split("/") if part]
        return len(parts) < len(self._parsers) and all(
            self._match_segment(index, part) is not None
            for index, part in enumerate(parts))


def path_matches(path: str, filter=None) -> bool:
    """Match a relative path against any supplied case-sensitive inclusion glob.

    ``**`` spans zero or more directories. With no patterns every path matches.
    """
    if filter is None:
        return True
    if not isinstance(filter, (str, list, tuple)):
        raise ValueError("filter must be a glob string or a list of glob strings")
    patterns = [filter] if isinstance(filter, str) else list(filter)
    if not all(isinstance(pattern, str) and pattern for pattern in patterns):
        raise ValueError("Inclusion patterns must be nonempty glob strings")
    return any(_glob_matches(str(path), pattern) for pattern in patterns)


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


def discover(context, *, filter=None, progress=False, path_prefix="",
             descend=None) -> list[RemoteEntry]:
    """
    Discover remote files and their published metadata recursively.

    Directory listings and checksum manifests are read before filtering.
    Dataset files are not downloaded to compute missing checksums.

    Args:
        context (OperationContext): Source connection and cancellation state.
        filter (str | list[str], optional): Relative path glob or globs.
        path_prefix (str): Release-relative root for collection selection.
            Returned entry paths remain relative to the context root.
        descend (callable, optional): Predicate receiving a relative directory
            path ending in ``/``; listings skip directories for which it
            returns False. Checksum manifests in skipped directories are
            not read.
        progress (bool | callable): True displays a progress bar with an
            unknown total. A callback receives dictionaries containing
            ``directories``, ``files``, ``matched``, and ``current``.
            Defaults to False.

    Returns:
        list[RemoteEntry]: Matching files sorted by relative path.
        Unavailable sizes, modification times, and checksums remain None.

    Raises:
        ValueError: If an inclusion pattern is invalid.
        CapabilityError: If the source provides no supported listing.
        DownloadError: If discovery fails, paths are unsafe, or manifests conflict.
    """
    # Validate selectors before contacting the source, even for an empty index.
    path_matches("", filter=filter)
    def matches(path):
        logical_path = path_prefix + "/" + path if path_prefix else path
        return path_matches(logical_path, filter=filter)

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
            counts["matched"] += int(matches(path))
            report()

    previous_descend = getattr(context, "descend", None)
    context.descend = descend
    try:
        context.transport.prepare()
        for entry in context.transport.iter_entries(on_directory=report):
            context.check_cancelled()
            add(entry)
        _manifest_checksums(context, entries)
        return [entries[path] for path in sorted(entries)
                if matches(path)]
    finally:
        context.descend = previous_descend
        if bar is not None:
            bar.close()
