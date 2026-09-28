"""CyVerse WebDAV HTML listing support, sharing the HTTP transfer backend."""

from .http import HttpBackend
from .index import _IndexParser, _parse_directory_listing


class CyVerseIndexParser(_IndexParser):
    """Read CyVerse object and collection rows, including exact byte sizes."""

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "tr":
            classes = dict(attrs).get("class", "").split()
            if "object" in classes:
                self.listing = True
                self.row_kind = "directory" if "collection" in classes else "file"


def is_cyverse_index(text):
    """Recognize CyVerse markup independently of its hosting domain."""
    parser = CyVerseIndexParser()
    parser.feed(text)
    return parser.listing


class CyVerseBackend(HttpBackend):
    """Discover CyVerse collections and transfer their files over HTTP(S)."""

    def _parse_directory_listing(self, text, directory):
        return _parse_directory_listing(text, self.context.remote.url, directory,
                            parser_class=CyVerseIndexParser)
