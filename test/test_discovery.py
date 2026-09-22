"""Metadata-only discovery against local fixtures; no public network is used."""

from types import SimpleNamespace

import pytest

from hallmark.discovery import discover, path_matches
from hallmark.backends import HttpBackend
from hallmark.transport.base import (
    CapabilityError, DownloadError, RemoteEntry, RemoteSpec, TransferCancelled,
)


class Source:
    """Serve directory listings and record requests for discovery tests."""
    def __init__(self, pages, url="https://data.test/public/"):
        self.remote = RemoteSpec.parse(url)
        self.pages = pages
        self.reads = []
        if self.remote.scheme in {"http", "https"}:
            self.transport = HttpBackend(self)

    def read_text(self, path):
        self.reads.append(path)
        assert path in self.pages, f"Unexpected payload request: {path}"
        return self.pages[path]

    def check_cancelled(self):
        pass


def index(*links):
    """Render a small HTML directory index from the supplied links."""
    return '<title>Index of /</title>' + ''.join(
        f'<a href="{path}">{path}</a>' for path in links)


@pytest.mark.parametrize("url", ["https://data.test/public/", "http://data.test/public"])
def test_auto_index_walks_all_directories_without_payloads(url):
    source = Source({
        "": index("root.fits", "nested/", "nested/", "../"),
        "nested/": index("item.bin", "deeper/", "../"),
        "nested/deeper/": index("file.txt"),
    }, url)
    entries = discover(source)
    assert [entry.path for entry in entries] == [
        "nested/deeper/file.txt", "nested/item.bin", "root.fits"]
    assert source.reads == ["", "nested/", "nested/deeper/"]
    assert all(entry.checksum is None for entry in entries)


def test_cyverse_markup_is_detected_automatically():
    source = Source({"": '<table><tr class="object data-object">'
                     '<td class="name"><a href="file.fits">file</a>'
                     '</td></tr></table>'})
    assert [entry.path for entry in discover(source)] == ["file.fits"]


@pytest.mark.parametrize("page, size, mtime", [
    ('<title>Index of /</title><a href="file.fits">file</a>'
     ' 17-Sep-2026 02:31 12345\n', 12345, None),
    ('<title>Index of /</title><table><tr><td><a href="file.fits">file</a>'
     '</td><td>2026-09-17 02:31</td><td>12345</td></tr></table>', 12345, None),
    ('<table><tr class="object data-object"><td><a href="file.fits">file</a>'
     '</td><td class="size">12,345</td></tr></table>', 12345, None),
    ('<title>Index of /</title><a href="file.fits" data-size="12345" '
     'data-mtime="1750000000">file</a>', 12345, 1750000000),
    ('<title>Index of /</title><a href="file.fits">file</a>'
     ' 17-Sep-2026 02:31 1.2M\n', None, None),
])
def test_exact_sizes_are_preserved_and_rounded_sizes_remain_unknown(page, size, mtime):
    assert discover(Source({"": page})) == [
        RemoteEntry(path="file.fits", size=size, mtime=mtime)]


def test_hallmark_and_git_directories_are_not_dataset_files():
    source = Source({"": index(".hm/", ".git/", ".HM/", "file.fits")})
    assert [entry.path for entry in discover(source)] == ["file.fits"]
    assert source.reads == [""]


def test_desi_style_landing_page_links_to_scoped_indexes():
    source = Source({
        "": '<h1>DESI public data</h1><ul><li><a href="/public/dr1/">'
            'Data release 1</a></li><li><a href="/public/edr/">EDR</a></li>'
            '<li><a href="https://docs.test/">Documentation</a></li></ul>',
        "dr1/": index("spectro/"),
        "dr1/spectro/": index("redshift.fits"),
        "edr/": index("old.fits"),
    })
    assert [entry.path for entry in discover(source)] == [
        "dr1/spectro/redshift.fits", "edr/old.fits"]


@pytest.mark.parametrize("page", [
    '<title>Index of /empty/</title>', '<table><tbody></tbody></table>',
])
def test_recognized_empty_index(page):
    assert discover(Source({"": page})) == []


@pytest.mark.parametrize("page", [
    '<h1>Welcome</h1><a href="login.html">Log in</a>',
    '<table><form><input name="password"></form></table>',
    '<table><tr><td>Access denied</td></tr></table>',
    'not a directory index',
])
def test_unsupported_page_is_not_an_empty_catalog(page):
    with pytest.raises(CapabilityError, match="no usable directory listing"):
        discover(Source({"": page}))


def test_link_resolution_preserves_literal_names_and_stays_under_root():
    source = Source({
        "": index(
            "https://other.test/x/", "/elsewhere/", "../", "?C=N;O=D",
            "#top", "/public/sub%20dir/", "/public/%2520/", "root%25.fits"),
        "sub dir/": index("file%20name.fits", "../"),
        "%20/": index("literal%2520.fits"),
    })
    assert [entry.path for entry in discover(source)] == [
        "%20/literal%20.fits", "root%.fits", "sub dir/file name.fits"]
    assert source.reads == ["", "sub dir/", "%20/"]


@pytest.mark.parametrize("href", ["%2e%2e/secret/", "sub%5cfile", "bad%00file"])
def test_encoded_unsafe_paths_are_rejected(href):
    with pytest.raises((DownloadError, ValueError)):
        discover(Source({"": index(href)}))


@pytest.mark.parametrize("path, pattern, matched", [
    ("root.fits", "**/*.fits", True),
    ("a/b/root.fits", "**/*.fits", True),
    ("a/b/root.txt", "**/*.fits", False),
    ("a/b/root.fits", "a/*.fits", False),
    ("a/root.fits", "a/?.fits", False),
    ("a/a.fits", "a/[ab].fits", True),
    ("root.txt", ["**/*.fits", "**/*.txt"], True),
])
def test_recursive_globs(path, pattern, matched):
    assert path_matches(path, filter=pattern) is matched


def test_exact_format_and_filter_are_both_required():
    assert path_matches("sample_001.fits", fmt="sample_{id:03d}.fits")
    assert not path_matches("sample_bad.fits", fmt="sample_{id:03d}.fits")
    assert not path_matches("Sample_001.fits", fmt="sample_{id:03d}.fits")
    assert not path_matches("sample_001.fits", filter="*.txt", fmt="sample_{id}.fits")


@pytest.mark.parametrize("invalid", [12, {"*.fits": True}, iter(["*.fits"])])
def test_invalid_filter_types_fail_before_source_access(invalid):
    source = Source({"": index("file.fits")})
    with pytest.raises(ValueError, match="glob string"):
        discover(source, filter=invalid)
    assert source.reads == []


@pytest.mark.parametrize("final_url", [
    "https://data.test/public/release%20one/",
    "https://DATA.TEST:443/public/release%20one/",
])
def test_redirected_listing_uses_final_directory_and_deduplicates_crawl(final_url):
    source = Source({
        "": index("alias/", "release%20one/"),
        "alias/": index("file.fits", "nested/"),
        "release one/nested/": index("other.fits"),
    })
    source.transport.text_urls = {"alias/": final_url}
    entries = discover(source)
    assert [entry.path for entry in entries] == [
        "release one/file.fits", "release one/nested/other.fits"]
    assert source.reads == ["", "alias/", "release one/nested/"]


def test_filter_traverses_unmatched_directories_and_excludes_other_payloads():
    source = Source({"": index("root.fits", "nested/", "notes.txt"),
                     "nested/": index("other.fits", "other.bin")})
    assert [entry.path for entry in discover(source, filter="**/*.fits")] == [
        "nested/other.fits", "root.fits"]
    assert source.reads == ["", "nested/"]


def test_only_conventional_checksum_metadata_is_read():
    digest = "a" * 64
    source = Source({
        "": index("data.fits", "sha256sums.txt", "custom_checksum_payload.bin"),
        "sha256sums.txt": f"{digest}  data.fits\n",
    })
    entries = discover(source, filter="*.fits")
    assert entries == [RemoteEntry(path="data.fits", checksum_algorithm="sha256",
                                   checksum=digest)]
    assert source.reads == ["", "sha256sums.txt"]


def test_checksum_manifest_cannot_add_unlisted_files():
    source = Source({"": index("sha256sums.txt"),
                     "sha256sums.txt": "a" * 64 + "  phantom.fits\n"})
    assert [entry.path for entry in discover(source)] == ["sha256sums.txt"]


def test_nested_manifest_accepts_published_directory_prefixed_paths():
    source = Source({
        "": index("release/"),
        "release/": index("project/"),
        "release/project/": index("sha256sums", "file.fits"),
        "release/project/sha256sums": "a" * 64 + "  project/file.fits\n",
    })
    assert discover(source, filter="**/*.fits")[0].checksum == "a" * 64


def test_conflicting_manifests_fail():
    source = Source({
        "": index("file.fits", "sha256sums", "project.sha256sums"),
        "sha256sums": "a" * 64 + "  file.fits\n",
        "project.sha256sums": "b" * 64 + "  file.fits\n",
    })
    with pytest.raises(DownloadError, match="Conflicting"):
        discover(source)


def test_sftp_metadata_and_progress_callback():
    source = Source({}, url="sftp://lab/srv/data/")

    def entries(on_directory):
        on_directory("")
        yield RemoteEntry(path="root.fits", size=12, mtime=123)
        on_directory("nested")
        yield RemoteEntry(path="nested/file.bin", size=20)

    source.transport = SimpleNamespace(iter_entries=entries, prepare=lambda: None)
    snapshots = []
    selected = discover(source, filter="**/*.fits", progress=snapshots.append)
    assert selected == [RemoteEntry(path="root.fits", size=12, mtime=123)]
    assert snapshots[-1] == {"directories": 2, "files": 2,
                             "matched": 1, "current": "nested"}
    assert source.reads == []


def test_cancellation_does_not_return_a_partial_inventory():
    source = Source({"": index("a/"), "a/": index("file")})

    def cancel():
        if source.reads:
            raise TransferCancelled("cancelled")

    source.check_cancelled = cancel
    with pytest.raises(TransferCancelled):
        discover(source)


def test_discovery_bar_has_unknown_total(monkeypatch):
    bars = []

    class Bar:
        def __init__(self, **kwargs):
            bars.append(kwargs)

        def update(self):
            pass

        def set_postfix(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr("hallmark.discovery.tqdm", Bar)
    discover(Source({"": index("a.fits")}), progress=True)
    assert bars[0]["total"] is None
    assert not bars[0]["disable"]
