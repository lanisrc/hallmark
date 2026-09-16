"""
A minimal mock of a CyVerse WebDAV directory tree, for testing
list_remote_files and build_repo without hitting the real network.
"""
from __future__ import annotations
from urllib.parse import urljoin


class MockServer:
    """
    Register directory listings and file contents by relative path,
    then use .patch_requests() as a context manager (or directly with
    unittest.mock.patch) to serve them for any requests.get/head call.
    """

    def __init__(self, base_url: str):
        """
        Initialize the mock server with a base URL.
        Args:
            self: The instance of the MockServer class.
            base_url: The base URL for the mock server. It should end with a slash.
        """
        if not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
        self._html_by_url: dict[str, str] = {}
        self._content_by_url: dict[str, bytes] = {}
        self._text_by_url: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def close(self):
        pass

    def get(self, url, timeout=None, stream=False):
        return self.fake_get(url, timeout=timeout)

    def head(self, url, timeout=None):
        return self.fake_head(url, timeout=timeout)

    def add_directory(self, rel_dir: str, entries: list[tuple[str, str]]) -> None:
        """
        Add a directory listing to the mock server.
        Args:
            self: The instance of the MockServer class.
            rel_dir: The relative directory path (e.g., 'dir1/dir2').
            entries: A list of (entry_type, href) pairs, where entry_type
                is "collection" or "data-object" (matching the real HTML).
        returns:
            None
        """
        if rel_dir and not rel_dir.endswith("/"):
            rel_dir += "/"
        rows = "".join(
            f'<tr class="object {entry_type}"><td class="name">'
            f'<a href="{href}">{href}</a></td></tr>'
            for entry_type, href in entries
        )
        html = f"<html><body><table><tbody>{rows}</tbody></table></body></html>"
        url = urljoin(self.base_url, rel_dir)
        self._html_by_url[url] = html

    def add_file(self, rel_path: str, content: str | bytes) -> None:
        """
        Add a file to the mock server.
        Args:
            self: The instance of the MockServer class.
            rel_path: The relative file path (e.g., 'dir1/file.txt').
            content: The content of the file, either as a string or bytes.
        returns:
            None
        """
        url = urljoin(self.base_url, rel_path)
        if isinstance(content, str):
            self._text_by_url[url] = content
            self._content_by_url[url] = content.encode()
        else:
            self._content_by_url[url] = content
            self._text_by_url[url] = content.decode(errors="replace")

    def fake_get(self, url, timeout=None):
        """
        Fake requests.get() method for the mock server.
        Args:
            self: The instance of the MockServer class.
            url: The URL to fetch.
            timeout: Optional timeout parameter (not used in the mock).
        returns:
            A MagicMock object simulating the response.
        """
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if url in self._html_by_url:
            resp.text = self._html_by_url[url]
            resp.content = resp.text.encode()
        elif url in self._text_by_url:
            resp.text = self._text_by_url[url]
            resp.content = self._content_by_url[url]
        else:
            raise AssertionError(f"MockServer: no registered response for GET {url}")
        resp.encoding = "utf-8"
        resp.iter_content = lambda chunk_size: iter([resp.content])
        resp.__enter__.return_value = resp
        return resp

    def fake_head(self, url, timeout=None):
        """
        Fake requests.head() method for the mock server.
        Args:
            self: The instance of the MockServer class.
            url: The URL to fetch.
            timeout: Optional timeout parameter (not used in the mock).
        returns:
            A MagicMock object simulating the response.
        """
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if url in self._content_by_url:
            resp.headers = {"Content-Length": str(len(self._content_by_url[url]))}
        else:
            raise AssertionError(f"MockServer: no registered response for HEAD {url}")
        resp.__enter__.return_value = resp
        return resp
