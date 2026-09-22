"""Parse scoped HTML directory indexes for HTTP data backends."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit

from .base import CapabilityError, DownloadError, literal_path, reject_controls


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
            self.row_kind = None
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
    """Return connection fields used to compare directory-listing URLs."""
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
    # Sorting and navigation links do not identify dataset files.
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


def _parse_index(text, root_url, directory, parser_class=_IndexParser):
    """Extract file and directory metadata from a supported HTML index."""
    parser = parser_class()
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
