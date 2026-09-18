"""Discover remote file metadata without transferring dataset payloads."""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from dataclasses import replace
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlsplit

import parse
from tqdm import tqdm

from .helper_functions import CHECKSUM_ALGORITHMS_BY_STRENGTH, valid_checksum
from .transport.base import (
    CapabilityError, DownloadError, RemoteEntry, RemoteObjectMissing,
    literal_path, reject_controls,
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
    return parse.compile(fmt, case_sensitive=True)


def path_matches(path: str, filter=None, fmt: str | None = None) -> bool:
    """Match root-relative paths by glob(s), exact Hallmark format, or both."""
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


class _IndexParser(HTMLParser):
    """Read links and recognizable index markers without depending on layout."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text = []
        self.row_kind = None
        self.listing = False
        self.has_table = False
        self.has_form = False
        self.current = None
        self.in_anchor = False
        self.cell_role = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.has_table = True
        elif tag == "form":
            self.has_form = True
        elif tag == "tr":
            self.current = None
            classes = attrs.get("class", "").split()
            self.row_kind = None
            if "object" in classes:
                self.listing = True
                self.row_kind = "directory" if "collection" in classes else "file"
        elif tag == "td":
            self.cell_role = attrs.get("class", "")
        elif tag == "a" and "href" in attrs:
            self.current = {"href": attrs["href"], "kind": self.row_kind,
                            "tail": "", "size": None, "mtime": None}
            for field in ("size", "mtime"):
                value = attrs.get(f"data-{field}")
                if value is not None and value.isdigit():
                    self.current[field] = int(value)
            self.links.append(self.current)
            self.in_anchor = True

    def handle_endtag(self, tag):
        if tag == "tr":
            self.row_kind = None
            self.current = None
        elif tag == "a":
            self.in_anchor = False
        elif tag == "td":
            if self.current is not None:
                self.current["tail"] += " "
            self.cell_role = None

    def handle_data(self, data):
        self.text.append(data)
        if self.current is not None and not self.in_anchor:
            self.current["tail"] += data
            if self.cell_role in {"size", "bytes"}:
                raw = data.strip().replace(",", "")
                if raw.isdigit():
                    self.current["size"] = int(raw)


def _origin(url):
    return (url.scheme, url.hostname,
            url.port or (443 if url.scheme == "https" else 80),
            url.username, url.password)


def _index_link(root_url, directory, href, kind):
    """Resolve a link only when it stays beneath the supplied URL root."""
    reject_controls(href, "Index href")
    root = urlsplit(root_url.rstrip("/") + "/")
    current = urljoin(root.geturl(), quote(directory, safe="/"))
    resolved = urlsplit(urljoin(current, href))
    if _origin(resolved) != _origin(root):
        return None
    # Sorting/navigation query links are not dataset objects.
    if resolved.query or resolved.fragment:
        return None
    root_path = unquote(root.path, errors="strict")
    path = unquote(resolved.path, errors="strict")
    reject_controls(path, "Index path")
    if "\\" in path or ".." in path.split("/"):
        raise DownloadError("Remote index contains an unsafe path")
    if not path.startswith(root_path):
        return None
    relative = path[len(root_path):]
    if not relative:
        return None
    if any(part.lower() in {".hm", ".git"} for part in relative.split("/")):
        return None
    is_directory = kind == "directory" or resolved.path.endswith("/")
    relative = literal_path(relative.rstrip("/")).as_posix()
    return relative + ("/" if is_directory else ""), is_directory


def _parse_index(text, root_url, directory):
    parser = _IndexParser()
    parser.feed(text)
    links = []
    for entry in parser.links:
        link = _index_link(root_url, directory, entry["href"], entry["kind"])
        if link is not None:
            path, is_directory = link
            size = entry["size"]
            if size is None:
                tail = re.search(
                    r"(?:\d{4}-\d\d-\d\d|\d\d-[A-Za-z]{3}-\d{4})"
                    r"\s+\d\d:\d\d(?::\d\d)?\s+(\d+)(?:\s|$)", entry["tail"])
                if tail:
                    size = int(tail.group(1))
            links.append((path, is_directory, size, entry["mtime"]))
    markers = " ".join(parser.text).lower()
    recognized = (parser.listing or "index of" in markers
                  or "directory listing" in markers or "parent directory" in markers
                  or any(is_dir for _, is_dir, _, _ in links))
    # CyVerse can emit a completely empty table without row-level markers.
    empty_table = (parser.has_table and not parser.links
                   and not parser.has_form and not markers.strip())
    if not recognized and not empty_table:
        raise CapabilityError(
            "This URL exposes no usable directory listing. Use a browsable "
            "directory URL or a published Hallmark catalog.")
    return links


def _response_directory(context, requested):
    """Use a redirected listing's canonical location when resolving child links."""
    transport = getattr(context, "transport", None)
    final_url = getattr(transport, "text_urls", {}).get(requested)
    if final_url is None:
        return requested
    root = urlsplit(context.remote.url.rstrip("/") + "/")
    final = urlsplit(final_url)
    if (_origin(root) == _origin(final)
            and unquote(root.path).rstrip("/") == unquote(final.path).rstrip("/")):
        return ""
    link = _index_link(context.remote.url, "", final_url, "directory")
    if link is None:
        raise DownloadError("Redirected directory is outside the dataset root")
    return link[0]


def _manifest_algorithm(path):
    """Recognize conventional manifests; never guess from arbitrary substrings."""
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
    """Recursively collect file metadata; formats and globs never authorize transfer.

    ``progress=True`` displays an indeterminate progress bar. A callable instead
    receives dictionaries with ``directories``, ``files``, ``matched``, and
    ``current`` keys, suitable for notebook or application rendering.
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
        if context.remote.scheme in {"ssh", "sftp"}:
            for entry in context.transport.iter_entries(on_directory=report):
                context.check_cancelled()
                add(entry)
        else:
            queue, visited = deque([""]), set()
            while queue:
                directory = queue.popleft()
                if directory in visited:
                    continue
                visited.add(directory)
                context.check_cancelled()
                text = context.read_text(directory)
                canonical = _response_directory(context, directory)
                if canonical != directory and canonical in visited:
                    continue
                visited.add(canonical)
                links = _parse_index(text, context.remote.url, canonical)
                for path, is_directory, size, mtime in links:
                    if is_directory:
                        if path not in visited:
                            queue.append(path)
                    else:
                        add(RemoteEntry(path=path, size=size, mtime=mtime))
                report(canonical)
        _manifest_checksums(context, entries)
        return [entries[path] for path in sorted(entries)
                if path_matches(path, filter=filter, fmt=fmt)]
    finally:
        if bar is not None:
            bar.close()
