"""CyVerse WebDAV HTML listing support, sharing the HTTP transfer backend."""

from html.parser import HTMLParser

from .http import HttpBackend
from .index import _IndexParser, _parse_index


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
    class Probe(HTMLParser):
        found = False

        def handle_starttag(self, tag, attrs):
            if tag == "tr" and "object" in dict(attrs).get("class", "").split():
                self.found = True

    parser = Probe()
    parser.feed(text)
    return parser.found


class CyVerseBackend(HttpBackend):
    """Discover CyVerse collections and transfer their files over HTTP(S)."""

    def _parse_index(self, text, directory):
        return _parse_index(text, self.context.remote.url, directory,
                            parser_class=CyVerseIndexParser)
