"""Metadata-only discovery against local fixtures; no public network is used."""

from types import SimpleNamespace

import pytest

from hallmark.remote.discovery import (
    _checksum_file_algorithm, discover_remote_files, path_matches,
)
from hallmark.remote.backends import HttpBackend
from hallmark.transport.base import (
    CapabilityError, DownloadError, RemoteEntry, RemoteSpec, TransferCancelled,
)


class Source:
    """Serve directory listings and record requests for discovery tests."""
    def __init__(self, pages, url="https://data.test/public/"):
        self.remote = RemoteSpec.from_url(url)
        self.pages = pages
        self.reads = []
        if self.remote.scheme in {"http", "https"}:
            self.transport = HttpBackend(self)

    def read_text(self, path, **_):
        self.reads.append(path)
        assert path in self.pages, f"Unexpected payload request: {path}"
        return self.pages[path]

    def check_cancelled(self):
        pass


def index(*links):
    """Render a small HTML directory index from the supplied links."""
    return '<title>Index of /</title>' + ''.join(
        f'<a href="{path}">{path}</a>' for path in links)


class Listing:
    """A source whose transport yields fixed entries and records requests."""

    def __init__(self, entries, texts=None, digests=None):
        self.remote = RemoteSpec.from_url("sftp://lab/srv/data/")
        self.texts = texts or {}
        self.reads, self.listed, self.hashed = [], [], []
        entries = [RemoteEntry(entry) if isinstance(entry, str) else entry
                   for entry in entries]

        def iter_entries(on_directory):
            on_directory("")
            for entry in entries:
                self.listed.append(entry.path)
                yield entry

        def server_checksums(paths, algorithm):
            self.hashed.append((paths, algorithm))
            assert digests is not None, "no file needs a server checksum"
            return digests

        self.transport = SimpleNamespace(
            iter_entries=iter_entries, prepare=lambda: None,
            complete_entries=lambda found: found,
            server_checksums=server_checksums)

    def read_text(self, path, **_):
        self.reads.append(path)
        assert path in self.texts, f"Unexpected payload request: {path}"
        return self.texts[path]

    def check_cancelled(self):
        pass


def checksums(source, **options):
    """Discover files and return each path's checksum fields."""
    return {entry.path: (entry.checksum_algorithm, entry.checksum,
                         entry.checksum_source)
            for entry in discover_remote_files(source, **options)}


@pytest.mark.parametrize("url", ["https://data.test/public/", "http://data.test/public"])
def test_auto_index_walks_all_directories_without_payloads(url):
    source = Source({
        "": index("root.fits", "nested/", "nested/", "../"),
        "nested/": index("item.bin", "deeper/", "../"),
        "nested/deeper/": index("file.txt"),
    }, url)
    entries = discover_remote_files(source)
    assert [entry.path for entry in entries] == [
        "nested/deeper/file.txt", "nested/item.bin", "root.fits"]
    assert source.reads == ["", "nested/", "nested/deeper/"]
    assert all(entry.checksum is None for entry in entries)


def test_cyverse_markup_is_detected_automatically():
    source = Source({"": '<table><tr class="object data-object">'
                     '<td class="name"><a href="file.fits">file</a>'
                     '</td></tr></table>'})
    assert [entry.path for entry in discover_remote_files(source)] == ["file.fits"]


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
    assert discover_remote_files(Source({"": page})) == [
        RemoteEntry(path="file.fits", size=size, mtime=mtime)]


def test_hallmark_and_git_directories_are_not_dataset_files():
    source = Source({"": index(".hm/", ".git/", ".HM/", "file.fits")})
    assert [entry.path for entry in discover_remote_files(source)] == ["file.fits"]
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
    assert [entry.path for entry in discover_remote_files(source)] == [
        "dr1/spectro/redshift.fits", "edr/old.fits"]


@pytest.mark.parametrize("page", [
    '<title>Index of /empty/</title>', '<table><tbody></tbody></table>',
])
def test_recognized_empty_index(page):
    assert discover_remote_files(Source({"": page})) == []


@pytest.mark.parametrize("page", [
    '<h1>Welcome</h1><a href="login.html">Log in</a>',
    '<table><form><input name="password"></form></table>',
    '<table><tr><td>Access denied</td></tr></table>',
    'not a directory index',
])
def test_unsupported_page_is_not_an_empty_catalog(page):
    with pytest.raises(CapabilityError, match="no usable directory listing"):
        discover_remote_files(Source({"": page}))


def test_link_resolution_preserves_literal_names_and_stays_under_root():
    source = Source({
        "": index(
            "https://other.test/x/", "/elsewhere/", "../", "?C=N;O=D",
            "#top", "/public/sub%20dir/", "/public/%2520/", "root%25.fits"),
        "sub dir/": index("file%20name.fits", "../"),
        "%20/": index("literal%2520.fits"),
    })
    assert [entry.path for entry in discover_remote_files(source)] == [
        "%20/literal%20.fits", "root%.fits", "sub dir/file name.fits"]
    assert source.reads == ["", "sub dir/", "%20/"]


@pytest.mark.parametrize("href", ["%2e%2e/secret/", "sub%5cfile", "bad%00file"])
def test_encoded_unsafe_paths_are_rejected(href):
    with pytest.raises((DownloadError, ValueError),
                       match="unsafe path|control characters"):
        discover_remote_files(Source({"": index(href)}))


@pytest.mark.parametrize("path, pattern, matched", [
    ("root.fits", "**/*.fits", True),
    ("a/b/root.fits", "**/*.fits", True),
    ("a/b/root.txt", "**/*.fits", False),
    ("a/b/root.fits", "a/*.fits", False),
    ("a/root.fits", "a/?.fits", False),
    ("a/a.fits", "a/[ab].fits", True),
    ("root.txt", ["**/*.fits", "**/*.txt"], True),
    ("a/b.fits", "a/**/b.fits", True),
    ("a/x/y/b.fits", "a/**/b.fits", True),
    ("a/b.fits", "**.fits", False),
    ("a.FITS", "*.fits", False),
])
def test_recursive_globs(path, pattern, matched):
    assert path_matches(path, filter=pattern) is matched


def test_globs_stay_case_sensitive_where_local_paths_are_not(monkeypatch):
    # Windows' os.path.normcase lower-cases, which fnmatch.fnmatch applies.
    monkeypatch.setattr("os.path.normcase", str.lower)
    assert not path_matches("a.FITS", filter="*.fits")
    assert not path_matches("A/b.fits", filter="a/*.fits")
    assert path_matches("a.fits", filter="*.fits")


def test_exact_format_and_filter_are_both_required():
    assert path_matches("sample_001.fits", fmt="sample_{id:03d}.fits")
    assert not path_matches("sample_bad.fits", fmt="sample_{id:03d}.fits")
    assert not path_matches("Sample_001.fits", fmt="sample_{id:03d}.fits")
    assert not path_matches("sample_001.fits", filter="*.txt", fmt="sample_{id}.fits")
    assert not path_matches("sample_001.fits", filter="*.fits", fmt="other_{id}.fits")
    assert path_matches("sample_001.fits", filter="*.fits", fmt="sample_{id:03d}.fits")


@pytest.mark.parametrize("empty", ["", [], ()])
def test_an_empty_filter_means_no_filter(empty):
    assert path_matches("a.fits", filter=empty)
    assert path_matches("nested/b.txt", filter=empty, fmt="nested/{name}.txt")
    assert not path_matches("a.fits", filter=empty, fmt="nested/{name}.txt")
    source = Source({"": index("a.fits", "nested/"), "nested/": index("b.txt")})
    assert [entry.path for entry in discover_remote_files(source, filter=empty)] == [
        "a.fits", "nested/b.txt"]
    assert [entry.path for entry in discover_remote_files(
        source, filter=empty, fmt="nested/{name}.txt")] == ["nested/b.txt"]


@pytest.mark.parametrize("invalid", [12, {"*.fits": True}, iter(["*.fits"])])
def test_invalid_filter_types_fail_before_source_access(invalid):
    source = Source({"": index("file.fits")})
    with pytest.raises(ValueError, match="glob string"):
        discover_remote_files(source, filter=invalid)
    assert source.reads == []


@pytest.mark.parametrize("fmt, message", [
    ("{x:Q}.fits", "format spec 'Q'"),
    (5, "Filename format must be a string"),
    (["{x}.fits"], "Filename format must be a string"),
])
def test_invalid_formats_fail_before_source_access(fmt, message):
    source = Source({"": index("file.fits")})
    with pytest.raises(ValueError, match=message):
        discover_remote_files(source, fmt=fmt)
    assert source.reads == []


@pytest.mark.parametrize("algorithm", ["SHA256", ""])
def test_an_unsupported_server_checksum_fails_before_listing(algorithm):
    source = Listing(["a.fits"])
    with pytest.raises(ValueError, match="Unsupported server checksum"):
        discover_remote_files(source, server_checksum=algorithm)
    assert source.listed == [] and source.reads == []


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
    entries = discover_remote_files(source)
    assert [entry.path for entry in entries] == [
        "release one/file.fits", "release one/nested/other.fits"]
    assert source.reads == ["", "alias/", "release one/nested/"]


def test_filter_traverses_unmatched_directories_and_excludes_other_payloads():
    source = Source({"": index("root.fits", "nested/", "notes.txt"),
                     "nested/": index("other.fits", "other.bin")})
    assert [
        entry.path for entry in discover_remote_files(source, filter="**/*.fits")
    ] == ["nested/other.fits", "root.fits"]
    assert source.reads == ["", "nested/"]


def test_only_conventional_checksum_metadata_is_read():
    digest = "a" * 64
    source = Source({
        "": index("data.fits", "sha256sums.txt", "custom_checksum_payload.bin"),
        "sha256sums.txt": f"{digest}  data.fits\n",
    })
    entries = discover_remote_files(source, filter="*.fits")
    assert entries == [RemoteEntry(path="data.fits", checksum_algorithm="sha256",
                                   checksum=digest, checksum_source="published")]
    assert source.reads == ["", "sha256sums.txt"]


def test_checksum_manifest_cannot_add_unlisted_files():
    source = Source({"": index("sha256sums.txt"),
                     "sha256sums.txt": "a" * 64 + "  phantom.fits\n"})
    assert [entry.path for entry in discover_remote_files(source)] == ["sha256sums.txt"]


def test_nested_manifest_accepts_published_directory_prefixed_paths():
    source = Source({
        "": index("release/"),
        "release/": index("project/"),
        "release/project/": index("sha256sums", "file.fits"),
        "release/project/sha256sums": "a" * 64 + "  project/file.fits\n",
    })
    assert discover_remote_files(source, filter="**/*.fits")[0].checksum == "a" * 64


def test_conflicting_manifests_fail():
    source = Source({
        "": index("file.fits", "sha256sums", "project.sha256sums"),
        "sha256sums": "a" * 64 + "  file.fits\n",
        "project.sha256sums": "b" * 64 + "  file.fits\n",
    })
    with pytest.raises(DownloadError, match="Conflicting"):
        discover_remote_files(source)


@pytest.mark.parametrize("name, algorithm", [
    ("checksums", "unknown"), ("checksum.txt", "unknown"),
    ("CHECKSUMS.TXT", "unknown"),
    ("sha256sum.txt", "sha256"), ("x.sha256sum", "sha256"), ("MD5SUM", "md5"),
    ("SHA512SUMS", "sha512"), ("x.md5sums", "md5"), ("sub/SHA1SUMS", "sha1"),
    ("file.fits.sha256", None), ("SHA256SUMS.asc", None),
    ("x.sha256sums.txt", None), (".sha256sums", None), ("checksums.md5", None),
    ("md5.txt", None),
])
def test_checksum_manifests_are_known_by_conventional_names(name, algorithm):
    assert _checksum_file_algorithm(name) == algorithm


def test_manifest_lines_take_binary_markers_upper_case_and_crlf():
    source = Listing(["a.fits", "b.fits", "SHA256SUMS"], {
        "SHA256SUMS": "A" * 64 + " *a.fits\r\n" + "b" * 64 + "  b.fits\r\n"})
    found = checksums(source)
    assert found["a.fits"] == ("sha256", "a" * 64, "published")
    assert found["b.fits"] == ("sha256", "b" * 64, "published")


def test_gnu_escaped_manifest_names_are_refused():
    source = Listing(["a.fits", "SHA256SUMS"], {
        "SHA256SUMS": "a" * 64 + "  a.fits\n\\" + "b" * 64 + "  a\\nb.fits\n"})
    with pytest.raises(DownloadError, match="GNU escaped manifest filenames"):
        discover_remote_files(source)


def test_a_plain_checksums_file_reads_a_40_character_digest_as_sha1():
    source = Listing(["a.fits", "checksums.txt"],
                     {"checksums.txt": "E" * 40 + "  a.fits\n"})
    assert checksums(source)["a.fits"] == ("sha1", "e" * 40, "published")


def test_a_nested_manifest_names_files_in_its_own_folder_first():
    source = Listing(["x.fits", "sub/x.fits", "sub/SHA256SUMS"],
                     {"sub/SHA256SUMS": "a" * 64 + "  x.fits\n"})
    found = checksums(source)
    assert found["sub/x.fits"] == ("sha256", "a" * 64, "published")
    assert found["x.fits"] == (None, None, None)


def test_a_nested_manifest_does_not_checksum_a_root_file_of_the_same_name():
    source = Listing(["data.fits", "SHA256SUMS", "sub/SHA256SUMS"], {
        "SHA256SUMS": "a" * 64 + "  data.fits\n",
        "sub/SHA256SUMS": "b" * 64 + "  data.fits\n"})
    assert checksums(source)["data.fits"] == ("sha256", "a" * 64, "published")
    source = Listing(["data.fits", "sub/SHA256SUMS"],
                     {"sub/SHA256SUMS": "b" * 64 + "  data.fits\n"})
    assert checksums(source)["data.fits"] == (None, None, None)


def test_a_nested_manifest_may_name_a_path_from_the_root():
    source = Listing(["other/b.fits", "sub/SHA256SUMS"],
                     {"sub/SHA256SUMS": "a" * 64 + "  other/b.fits\n"})
    assert checksums(source)["other/b.fits"] == ("sha256", "a" * 64, "published")


@pytest.mark.parametrize("md5_names, sha256_name", [
    (("a.md5sums", "c.md5sums"), "b.sha256sums"),
    (("a.md5sums", "c.md5sums"), "z.sha256sums"),
    (("b.md5sums", "c.md5sums"), "a.sha256sums"),
])
def test_conflicting_manifests_fail_whatever_order_their_names_sort_in(
        md5_names, sha256_name):
    first, second = md5_names
    source = Listing(["file.fits", first, second, sha256_name], {
        first: "1" * 32 + "  file.fits\n", second: "2" * 32 + "  file.fits\n",
        sha256_name: "a" * 64 + "  file.fits\n"})
    with pytest.raises(DownloadError,
                       match="Conflicting md5 manifests for 'file.fits'"):
        discover_remote_files(source)


def test_equal_digests_in_different_letter_case_do_not_conflict():
    source = Listing(["file.fits", "SHA256SUMS", "x.sha256sums"], {
        "SHA256SUMS": "A" * 64 + "  file.fits\n",
        "x.sha256sums": "a" * 64 + "  file.fits\n"})
    assert checksums(source)["file.fits"] == ("sha256", "a" * 64, "published")


def test_a_manifest_listing_one_file_twice_with_different_digests_fails():
    source = Listing(["file.fits", "SHA256SUMS"], {
        "SHA256SUMS": "a" * 64 + "  file.fits\n" + "b" * 64 + "  ./file.fits\n"})
    with pytest.raises(DownloadError,
                       match="Conflicting sha256 manifests for 'file.fits'"):
        discover_remote_files(source)


@pytest.mark.parametrize("names", [
    ("a.sha256sums", "b.md5sums"), ("a.md5sums", "b.sha256sums"),
])
def test_the_strongest_published_checksum_wins_whatever_the_name_order(names):
    texts = {name: ("a" * 64 if "sha256" in name else "b" * 32) + "  file.fits\n"
             for name in names}
    source = Listing(["file.fits", *names], texts)
    assert checksums(source)["file.fits"] == ("sha256", "a" * 64, "published")


@pytest.mark.parametrize("md5_name", ["MD5SUMS", "b.md5sums"])
def test_a_published_digest_disagreeing_with_the_listing_names_the_server(md5_name):
    listed = RemoteEntry("file.fits", checksum_algorithm="md5", checksum="1" * 32,
                         checksum_source="server")
    source = Listing([listed, "SHA256SUMS", md5_name], {
        "SHA256SUMS": "a" * 64 + "  file.fits\n", md5_name: "2" * 32 + "  file.fits\n"})
    with pytest.raises(DownloadError, match="md5 checksum for 'file.fits' "
                       "disagrees with the server's checksum"):
        discover_remote_files(source)


def test_a_published_digest_may_match_the_listing_in_any_letter_case():
    listed = RemoteEntry("file.fits", checksum_algorithm="md5", checksum="A" * 32,
                         checksum_source="server")
    source = Listing([listed, "MD5SUMS"], {"MD5SUMS": "a" * 32 + "  file.fits\n"})
    algorithm, digest, _ = checksums(source)["file.fits"]
    assert (algorithm, digest.lower()) == ("md5", "a" * 32)


def test_an_equally_strong_published_checksum_is_not_hashed_again():
    source = Listing(["a.fits", "SHA256SUMS"], {"SHA256SUMS": "a" * 64 + "  a.fits\n"})
    found = checksums(source, filter="*.fits", server_checksum="sha256")
    assert found == {"a.fits": ("sha256", "a" * 64, "published")}
    assert source.hashed == []


def test_a_weaker_published_checksum_is_hashed_by_the_server():
    source = Listing(["a.fits", "MD5SUMS"], {"MD5SUMS": "b" * 32 + "  a.fits\n"},
                     digests={"a.fits": "c" * 64})
    found = checksums(source, filter="*.fits", server_checksum="sha256")
    assert found == {"a.fits": ("sha256", "c" * 64, "server")}
    assert source.hashed == [(["a.fits"], "sha256")]


def test_only_selected_files_are_sent_to_the_server():
    source = Listing(["a.fits", "b.txt", "nested/c.fits"],
                     digests={"a.fits": "a" * 64, "nested/c.fits": "c" * 64})
    found = checksums(source, filter="**/*.fits", server_checksum="sha256")
    assert sorted(found) == ["a.fits", "nested/c.fits"]
    assert source.hashed == [(["a.fits", "nested/c.fits"], "sha256")]


@pytest.mark.parametrize("filter, selected", [("*.fits", ["a.fits"]), ("*.txt", [])])
def test_a_source_that_cannot_hash_is_never_asked_when_no_file_needs_it(
        filter, selected):
    source = Source({"": index("a.fits", "SHA256SUMS"),
                     "SHA256SUMS": "a" * 64 + "  a.fits\n"})
    assert list(checksums(source, filter=filter, server_checksum="sha256")) == selected


def test_a_source_that_cannot_hash_fails_when_a_selected_file_needs_it():
    source = Source({"": index("a.fits", "SHA256SUMS"),
                     "SHA256SUMS": "a" * 64 + "  a.fits\n"})
    with pytest.raises(CapabilityError, match="cannot compute checksums"):
        discover_remote_files(source, filter="*.fits", server_checksum="sha512")


@pytest.mark.parametrize("digests", [
    {}, {"a.fits": None}, {"a.fits": "xyz"}, {"a.fits": "a" * 40},
])
def test_missing_or_invalid_server_digests_are_refused(digests):
    source = Listing(["a.fits", "b.fits"], digests={"b.fits": "b" * 64, **digests})
    with pytest.raises(DownloadError, match="no valid sha256 checksum for 'a.fits'"):
        discover_remote_files(source, server_checksum="sha256")


def test_server_digests_are_stored_in_lower_case():
    source = Listing(["a.fits"], digests={"a.fits": "E" * 64})
    assert checksums(source, server_checksum="sha256") == {
        "a.fits": ("sha256", "e" * 64, "server")}


def test_discovered_paths_are_normalized():
    source = Listing([RemoteEntry("./a.fits", size=1), "sub//b.fits", "sub/SHA256SUMS"],
                     {"sub/SHA256SUMS": "b" * 64 + "  b.fits\n"},
                     digests={"a.fits": "a" * 64})
    entries = discover_remote_files(
        source, filter="**/*.fits", server_checksum="sha256")
    assert entries == [
        RemoteEntry("a.fits", size=1, checksum_algorithm="sha256", checksum="a" * 64,
                    checksum_source="server"),
        RemoteEntry("sub/b.fits", checksum_algorithm="sha256", checksum="b" * 64,
                    checksum_source="published"),
    ]
    assert source.hashed == [(["a.fits"], "sha256")]


def test_sftp_metadata_and_progress_callback():
    source = Source({}, url="sftp://lab/srv/data/")

    def entries(on_directory):
        on_directory("")
        yield RemoteEntry(path="root.fits", size=12, mtime=123)
        on_directory("nested")
        yield RemoteEntry(path="nested/file.bin", size=20)

    source.transport = SimpleNamespace(iter_entries=entries, prepare=lambda: None,
                                       complete_entries=lambda found: found)
    snapshots = []
    selected = discover_remote_files(
        source, filter="**/*.fits", progress=snapshots.append
    )
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
    with pytest.raises(TransferCancelled, match="cancelled"):
        discover_remote_files(source)


def test_discovery_checks_cancellation_before_each_entry():
    source = Listing(["a.fits", "b.fits", "c.fits"])

    def cancel():
        if len(source.listed) == 2:
            raise TransferCancelled("cancelled")

    source.check_cancelled = cancel
    snapshots = []
    with pytest.raises(TransferCancelled, match="cancelled"):
        discover_remote_files(source, progress=snapshots.append)
    assert source.listed == ["a.fits", "b.fits"]
    assert snapshots[-1]["files"] == 1


def test_progress_reports_the_root_folder_as_a_slash():
    snapshots = []
    discover_remote_files(Listing(["a.fits"]), progress=snapshots.append)
    assert snapshots[0] == {"directories": 1, "files": 0, "matched": 0,
                            "current": "/"}


@pytest.fixture
def bars(monkeypatch):
    """Replace the discovery progress bar with one that records its use."""
    made = []

    class Bar:
        def __init__(self, **kwargs):
            self.options, self.postfix, self.closed = kwargs, None, False
            made.append(self)

        def update(self):
            pass

        def set_postfix(self, **kwargs):
            self.postfix = kwargs

        def close(self):
            self.closed = True

    monkeypatch.setattr("hallmark.remote.discovery.tqdm", Bar)
    return made


def test_discovery_bar_has_unknown_total(bars):
    discover_remote_files(Source({"": index("a.fits", "b.txt")}), filter="*.fits",
                          progress=True)
    assert bars[0].options["total"] is None
    assert not bars[0].options["disable"]
    assert bars[0].postfix == {"files": 2, "matched": 1}
    assert bars[0].closed


def test_without_progress_the_bar_is_disabled(bars):
    discover_remote_files(Source({"": index("a.fits")}))
    assert bars[0].options["disable"]


def test_the_bar_is_closed_when_discovery_fails(bars):
    with pytest.raises(CapabilityError, match="no usable directory listing"):
        discover_remote_files(Source({"": "not a directory index"}), progress=True)
    assert bars[0].closed
