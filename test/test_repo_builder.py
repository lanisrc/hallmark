"""
Tests for hallmark.repo_builder, using mocked network calls so
nothing here depends on reaching the real CyVerse server.
"""
from unittest.mock import patch

import pandas as pd
import pytest
import yaml
import requests

from mock_server import MockServer
from hallmark.transport.base import CapabilityError
from hallmark.repo_builder import (
    KNOWN_FIELD_VALUES,
    _match_file_against_fmts,
    _resolve_manifest_path,
    build_repo,
    list_remote_files,
    _normalize_index_href,
    _remote_url)

BASE_URL = "https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/EHTC_TEST/"


# helper function to assert that the repo's remote config matches expected
def _assert_remote_config(repo, expected):
    """
    Assert that the repo's remote config matches the expected value.
    Args:
        repo: The Repo object to check.
        expected: The expected remote config value.
    """
    assert repo.state.config["remote"] == expected, (
        f"unexpected remotes: {repo.state.config['remote']}, expected {expected}")


# helper function to patch requests.get and requests.head to serve from a MockServer
def _served(server: MockServer):
    """
    Context manager patching requests.get/head to serve from server.
    Args:
        server: The MockServer instance to serve responses from.
    Returns:
        A context manager that patches requests.get and requests.head.
    """
    return patch("hallmark.transport.requests.Session", return_value=server)


# helper function to monkeypatch list_remote_files for testing build_repo
def _inventory(monkeypatch, files):
    """
    Monkeypatch list_remote_files to return a provided inventory of files
    and record the base_url.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        files: A dictionary of file paths to (checksum_algorithm, checksum) tuples.
    Returns:
        A list of base_url values passed to the fake list_remote_files function.
    """
    calls = []
    def fake_list(base_url, **kwargs):
        """create a fake list_remote_files list"""
        calls.append(base_url)
        return dict(files)
    monkeypatch.setattr("hallmark.repo_builder.list_remote_files", fake_list)
    return calls


### _normalize_index_href tests ###

@pytest.mark.parametrize(
    "href",[
        "../outside/",
        "/absolute/file.dat",
        "https://other.test/file.dat",
        r"..\outside.dat",
        "file.dat?download=1",
        "file.dat#fragment"])
def test_normalize_index_href_rejects_unsafe_links(href):
    """
    Test that _normalize_index_href rejects unsafe links that could lead
    to directory traversal or external URLs.
    Args:
        href: The href string to test.
    Raises:
        ValueError: If the href is unsafe and should be rejected.
    """
    with pytest.raises(ValueError, match="remote index path"):
        _normalize_index_href(href, is_directory=href.endswith("/"))


def test_normalize_index_href_decodes_safe_relative_links():
    """
    Test that _normalize_index_href correctly decodes safe relative links.
    """
    assert _normalize_index_href(
        "nested%20directory/",
        is_directory=True) == "nested directory/", f"unexpected normalized href: \
        {_normalize_index_href('nested%20directory/', is_directory=True)}"
    assert _normalize_index_href(
        "nested%20directory/data%20file.dat",
        is_directory=False) == "nested directory/data file.dat", \
        f"unexpected normalized href: \
      {_normalize_index_href('nested%20directory/data%20file.dat', is_directory=False)}"


### list_remote_files tests ###

def test_list_remote_files_partial_manifest_below_threshold_is_rejected():
    """
    If checksum-like content is mostly invalid, it should not be treated as a manifest.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("data-object", "data.tar"),
        ("data-object", "checksum_report.txt"),])
    server.add_file(
        "checksum_report.txt",
        "\n".join([
            "this is not a checksum line",
            "another bad line",
            "aa11bb22cc33dd44ee55ff66aa11bb22  data.tar",]),)
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums["data.tar"] == (None, None), \
        f"unexpected checksum for data.tar: {file_checksums}, \
            expected (None, None) because the manifest was mostly invalid"


def test_list_remote_files_sibling_manifest_covers_project_files():
    """
    If a manifest in the same directory as a project subdir folder is complete,
    it should cover the files in that subdir and provide their checksums.
    """
    server = MockServer(BASE_URL)
    server.add_directory(
        "",[
            ("collection", "2016.1.01114.V/"),
            ("data-object", "2016.1.01114.V.md5sums")])
    server.add_directory("2016.1.01114.V/", [("data-object", "data.tgz")])
    server.add_file(
        "2016.1.01114.V.md5sums",
        "2f1d7fed9ceda962bf12bc4a20e068a8  "
        "2016.1.01114.V/data.tgz\n")
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums["2016.1.01114.V/data.tgz"] == \
        ("md5", "2f1d7fed9ceda962bf12bc4a20e068a8"), \
        f"unexpected checksum for 2016.1.01114.V/data.tgz: {file_checksums}, \
            expected md5 2f1d7fed9ceda962bf12bc4a20e068a8"


def test_list_remote_files_partial_sibling_manifest_does_not_hide_files():
    """
    If a manifest in the same directory as a project subdir folder is partial,
    it should not hide other files.
    """
    server = MockServer(BASE_URL)
    server.add_directory(
        "",[
            ("collection", "2016.1.01114.V/"),
            ("data-object", "2016.1.01114.V.md5sums")])
    server.add_directory(
        "2016.1.01114.V/",[
            ("data-object", "listed.tgz"),
            ("data-object", "unlisted.tgz")])
    server.add_file(
        "2016.1.01114.V.md5sums",
        "2f1d7fed9ceda962bf12bc4a20e068a8  "
        "2016.1.01114.V/listed.tgz\n")
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums[
        "2016.1.01114.V/listed.tgz"] == ("md5", "2f1d7fed9ceda962bf12bc4a20e068a8"), \
        f"unexpected checksum for 2016.1.01114.V/listed.tgz: {file_checksums}, \
            expected md5 2f1d7fed9ceda962bf12bc4a20e068a8"
    assert file_checksums["2016.1.01114.V/unlisted.tgz"] == (None, None), \
        f"unexpected checksum for 2016.1.01114.V/unlisted.tgz: {file_checksums}, \
            expected (None, None) because the file was not listed in the manifest"


def test_list_remote_files_does_not_probe_custom_checksum_payloads():
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("data-object", "data.tar"),
        ("data-object", "custom_checksum_manifest.log")])
    # No payload is registered: fetching either body would fail the test.
    with _served(server):
        files = list_remote_files(server.base_url)
    assert files["data.tar"] == (None, None)


def test_list_remote_files_generic_manifest_algorithm_from_own_filename():
    """
    A manifest file whose name is generic (like "sha1sum.txt") should
    still be detected as a manifest if its content looks like one, and
    the algorithm should be inferred from the manifest's own filename.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("data-object", "data.tar"),
        ("data-object", "sha1sum.txt"),])
    server.add_file("sha1sum.txt",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  data.tar\n")
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums["data.tar"][0] == "sha1", \
        f"unexpected checksum algorithm for data.tar: {file_checksums}, \
            expected sha1 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_list_remote_files_non_manifest_text_file_not_misdetected():
    """
    A plain README shouldn't be treated as a manifest just because
    it happens to be a small text file.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("data-object", "README.txt"),])
    server.add_file("README.txt", "This is a plain readme, not a checksum list.\n")
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums["README.txt"] == (None, None), \
        f"unexpected checksum for README.txt: {file_checksums}, \
            expected (None, None) for a non-manifest text file"


def test_list_remote_files_name_matching_content_not_manifest_rejected():
    """
    A file whose NAME matches the checksum keyword filter, but whose
    CONTENT doesn't look like a manifest, must still be rejected.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("data-object", "checksum_results.tar"),])
    server.add_file("checksum_results.tar", "not actually a manifest, just junk text")
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums["checksum_results.tar"] == (None, None), \
        f"unexpected checksum for checksum_results.tar: {file_checksums}, \
            expected (None, None) for a non-manifest file"


def test_list_remote_files_root_level_static_file_has_no_checksum():
    """
    A file at the root level of the dataset, with no manifest coverage,
    should be listed with no checksum, rather than being skipped or
    misdetected as a manifest.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("data-object", "README.md"),])
    server.add_file("README.md", "root-level readme")
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums["README.md"] == (None, None), \
        f"unexpected checksum for README.md: {file_checksums}, \
        expected (None, None) for a root-level static file with no manifest coverage"


def test_list_remote_files_empty_directory_returns_empty_mapping():
    """
    An empty dataset root should produce an empty file/checksum mapping.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [])
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums == {}, f"expected empty mapping, got {file_checksums}"


def test_list_remote_files_walks_nested_subdirectories_without_manifest():
    """
    Nested subdirectories should be walked recursively when no manifest covers them.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [("collection", "level1/")])
    server.add_directory("level1", [
        ("collection", "level2/"),
        ("data-object", "root.txt"),])
    server.add_directory("level1/level2", [
        ("data-object", "deep.txt"),])
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert set(file_checksums) == {"level1/root.txt", "level1/level2/deep.txt"}, \
        f"unexpected nested listing: {file_checksums}, \
            expected both level1/root.txt and level1/level2/deep.txt"


def test_list_remote_files_walks_directory_when_sibling_checksum_is_invalid():
    """
    If a checksum file is present but its content is mostly invalid, the
    directory should still be walked to find files, rather than being skipped.
    """
    server = MockServer(BASE_URL)
    server.add_directory(
        "", [("collection", "project/"),("data-object", "project.md5sums")])
    server.add_directory("project", [("data-object", "data.tar")])
    server.add_file("project.md5sums", "deadbeef  project/data.tar\n")
    with _served(server):
        files = list_remote_files(server.base_url)

    assert files["project/data.tar"] == (None, None), \
        f"unexpected checksum for project/data.tar: {files}"
    assert files["project.md5sums"] == (None, None), \
        f"unexpected checksum for project.md5sums: {files}"


def test_list_remote_files_fetches_each_directory_once():
    """
    Each directory should be fetched at most once, even if multiple references exist.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [("collection", "nested/"), ("collection", "nested/")])
    server.add_directory("nested/", [("data-object", "file.dat")])
    requested_urls = []
    original_get = server.get
    def recording_get(url, timeout=None, **kwargs):
        """A wrapper around the original server.get that records requested URLs."""
        requested_urls.append(url)
        return original_get(url, timeout=timeout)
    server.get = recording_get
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums == {"nested/file.dat": (None, None)}, f"unexpected file \
        checksums: {file_checksums}, expected nested/file.dat to be listed"
    assert requested_urls.count(server.base_url + "nested/") == 1, f"expected nested/ \
        to be fetched once, got {requested_urls.count(server.base_url + 'nested/')}"


def test_list_remote_files_unavailable_manifest_does_not_hide_files():
    """
    If a manifest is present but unavailable (e.g., 404), the files should still be
    listed with no checksums, rather than being skipped.
    """
    server = MockServer(BASE_URL)
    server.add_directory(
        "", [("data-object", "data.tar"), ("data-object", "checksums.txt")])
    original_get = server.get
    def unavailable_manifest_get(url, timeout=None, **kwargs):
        """A wrapper around the original server.get that simulates
        an unavailable manifest."""
        if url.endswith("/checksums.txt"):
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("manifest unavailable", response=response)
        return original_get(url, timeout=timeout)
    server.get = unavailable_manifest_get
    with _served(server):
        file_checksums = list_remote_files(server.base_url)

    assert file_checksums["data.tar"] == (None, None), \
        f"unexpected checksum for data.tar: {file_checksums}, \
            expected (None, None) because the manifest was unavailable"
    assert file_checksums["checksums.txt"] == (None, None), \
        f"unexpected checksum for checksums.txt: {file_checksums}, \
            expected (None, None) because the manifest was unavailable"


### _match_file_against_fmts tests ###

def test_match_simple_clean_match():
    """
    Test that a simple clean match works correctly.
    """
    fmt_entries = [{"fmt": "a{a}_i{i}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("a0.75_i30.h5", fmt_entries)

    assert i == 0, f"unexpected index: {i}, expected 0"
    assert result == {"a": "0.75", "i": "30"}, \
        f"unexpected result: {result}, expected {{'a': '0.75', 'i': '30'}}"


def test_match_missing_field_is_dropped():
    """
    A field that is missing from the filename should be dropped from the
    result, rather than causing a failed match.
    """
    fmt_entries = [{"fmt": "a{a}_i{i}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("a0.75.h5", fmt_entries)

    assert i == 0, f"unexpected index: {i}, expected 0"
    assert result == {"a": "0.75"}, \
        f"unexpected result: {result}, expected {{'a': '0.75'}}"
    assert "i" not in result, f"unexpected result: {result}, expected 'i' to drop out"


def test_match_missing_field_behind_long_literal_is_dropped():
    """
    A field that is missing from the filename, but is behind a long distinctive literal,
    should be dropped from the result, rather than causing a failed match.
    """
    fmt_entries = [
        {"fmt": "prefix_long_literal_{opt}_suffix.txt", "db": "x.tsv"}]
    i, result = _match_file_against_fmts(
        "prefix_long_literal_suffix.txt", fmt_entries)
    assert i == 0, f"unexpected index: {i}, expected 0"
    assert "opt" not in result, \
        f"unexpected result: {result}, expected 'opt' to drop out"

    i, result = _match_file_against_fmts(
        "prefix_long_literal_extra_suffix.txt", fmt_entries)
    assert i == 0, f"unexpected index: {i}, expected 0"
    assert result == {"opt": "extra"}, \
        f"unexpected result: {result}, expected {{'opt': 'extra'}}"

    i, result = _match_file_against_fmts("unrelated_suffix.txt", fmt_entries)
    assert i is None and result is None, \
        f"unexpected match: {(i, result)}, expected no match"


def test_match_no_matching_fmt_returns_none():
    """
    A filename that doesn't match any of the provided formats should
    return None for both the index and the result.
    """
    fmt_entries = [{"fmt": "a{a}_i{i}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("completely_unrelated_file.xyz", fmt_entries)

    assert i is None, f"unexpected index: {i}, expected None"
    assert result is None, f"unexpected result: {result}, expected None"


def test_match_prefers_more_specific_fmt_over_looser_one():
    """
    When multiple formats could match a filename, the one with more
    specific literals should be preferred over a looser one.
    """
    fmt_entries = [
        {"fmt": "{a}-longtoken1-{b}-longtoken2-{c}-longtoken3-{d}",
         "db": "looks_specific.tsv"},
        {"fmt": "{x}.{y}", "db": "actually_correct.tsv"},
    ]
    i, result = _match_file_against_fmts("hello.world", fmt_entries)

    assert fmt_entries[i]["db"] == "actually_correct.tsv", \
        f"unexpected index: {i}, expected the second format to match"


def test_match_repeated_name_resolves_when_values_agree():
    """
    When a repeated name's occurrences genuinely agree, that name
    should be resolved to that value, rather than being dropped.
    """
    fmt = "{project_code}/group.uid{guid}.{author}.{track}-{source}-{project_code}" \
    "-{kind}.{format}"
    rel_path = "2016.V/group.uid_ABC.author.e17-3C279-2016.V-4fit.tgz"
    fmt_entries = [{"fmt": fmt, "db": "data.tsv"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)

    assert result is not None, \
        f"unexpected None result for {rel_path} with fmt {fmt_entries}"
    assert result["project_code"] == "2016.V", \
        f"unexpected project_code: {result.get('project_code')}, expected '2016.V'"


def test_match_repeated_name_drops_when_values_disagree():
    """
    When a repeated name's occurrences genuinely disagree, that name
    must be dropped, not silently resolved to the wrong value.
    """
    fmt = "{project_code}/group.uid{guid}.{author}.{track}-{source}-{project_code}" \
    "-{kind}.{format}"
    rel_path = "2016.V/group.uid_ABC.author.e17-3C279-othervalue-4fit.tgz"
    fmt_entries = [{"fmt": fmt, "db": "data.tsv"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)

    if result is not None:
        assert "project_code" not in result, f"unexpected project_code: \
            {result.get('project_code')}, expected it to be dropped due to disagreement"


def test_match_known_value_resolves_field_shift_ambiguity():
    """
    A field with a known-value constraint should be resolved to that
    value, rather than being dropped, even if that means the next field
    is shifted over to fill the gap.
    """
    assert "kind" in KNOWN_FIELD_VALUES, \
        f"unexpected KNOWN_FIELD_VALUES: {KNOWN_FIELD_VALUES}, expected 'kind' to be \
            a known-value field"
    fmt = "{track}-{obs_num}-{band}-{pointing}-{source}-{kind}.{format}"
    rel_path = "e18a24-1-b1-na-CYG-A-4fit.tar"
    fmt_entries = [{"fmt": fmt, "db": "data.tsv"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)
    assert result["track"] == "e18a24", \
        f"unexpected track: {result.get('track')}, expected 'e18a24'"
    assert result["obs_num"] == "1", \
        f"unexpected obs_num: {result.get('obs_num')}, expected '1'"
    assert result["band"] == "b1", \
        f"unexpected band: {result.get('band')}, expected 'b1'"
    assert result["pointing"] == "na", \
        f"unexpected pointing: {result.get('pointing')}, expected 'na'"
    assert result["source"] == "CYG-A", \
        f"unexpected source: {result.get('source')}, expected 'CYG-A'"
    assert result["kind"] == "4fit", \
        f"unexpected kind: {result.get('kind')}, expected '4fit'"


def test_match_known_value_field_is_never_dropped():
    """
    A field with a known-value constraint should never be dropped, even
    if the next field is missing and would otherwise cause a shift.
    """
    fmt = "{scope}-processing-logs.{format}"
    fmt_entries = [{"fmt": fmt, "db": "log.tsv"}]
    i, result = _match_file_against_fmts("README.txt", fmt_entries)

    assert result is None, f"unexpected result: {result}, expected None because \
        'README.txt' does not match the known-value field 'scope'"


def test_match_long_literal_not_silently_cleared():
    """
    Regression test: a long distinctive literal should not be silently cleared
    """
    fmt_entries = [
        {"fmt": "{scope}-processing-logs.{format}", "db": "log.tsv"},]
    i, result = _match_file_against_fmts("EHTmetadata_2018April_v1.2.tar.gz",
                                         fmt_entries)

    assert result is None, f"unexpected result: {result}, expected None because the \
        long distinctive literal '-processing-logs.' should not be clearable"


def test_match_genuine_log_file_still_matches():
    """
    A genuine log file that matches the format should still match,
    even though the format has a long distinctive literal that is not
    clearable.
    """
    fmt_entries = [
        {"fmt": "{scope}-processing-logs.{format}", "db": "log.tsv"},]
    i, result = _match_file_against_fmts("na-processing-logs.tgz", fmt_entries)

    assert result == {"scope": "na", "format": "tgz"}, \
        f"unexpected result: {result}, expected {{'scope': 'na', 'format': 'tgz'}}"


def test_match_short_delimiter_still_clearable():
    """
    A short, generic delimiter (like "-") should still be clearable
    when its field is dropped -- only long, distinctive literals are
    restricted.
    """
    fmt_entries = [{"fmt": "a{a}-{b}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("a0.75.h5", fmt_entries)

    assert result == {"a": "0.75"}, \
        f"unexpected result: {result}, expected {{'a': '0.75'}}"


def test_match_empty_fmt_entries_returns_none():
    """
    No format candidates should yield no match.
    """
    i, result = _match_file_against_fmts("any_file.txt", [])

    assert i is None and result is None, f"expected no match, got {(i, result)}"


def test_match_prefers_exacter_match_when_one_format_is_looser():
    """
    A more exact format should win over a looser competing format.
    """
    fmt_entries = [
        {"fmt": "{a}.{b}", "db": "loose.tsv"},
        {"fmt": "hello.{b}", "db": "exact.tsv"},]
    i, result = _match_file_against_fmts("hello.world", fmt_entries)

    assert i == 1, f"expected second format to win, got index {i}"
    assert result == {"b": "world"}, \
        f"unexpected result: {result}, expected {{'b': 'world'}}"


def test_match_typed_field_can_use_greedy_fallback():
    """
    A typed field (like {a:d}) should be able to use the greedy fallback search to
    match a number that is followed by a long literal.
    """
    fmt_entries = [{"fmt": "{a:d}-{label}.dat", "db": "data.tsv"}]
    index, result = _match_file_against_fmts("12-foo-bar.dat", fmt_entries)

    assert index == 0, f"unexpected index: {index}, expected 0"
    assert result == {"a": "12", "label": "foo-bar"}, \
        f"unexpected result: {result}, expected {{'a': '12', 'label': 'foo-bar'}}"


def test_matcher_runs_greedy_fallback_once_per_format(monkeypatch):
    """
    Test that the greedy fallback search is only run once per format, even if
    the first attempt fails to match.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
    """
    calls = 0
    def fake_greedy_search(segments, rel_path):
        """A fake greedy search that always returns a match
        and increments a call counter."""
        nonlocal calls
        calls += 1
        return {"value": "a"}, 1
    monkeypatch.setattr("hallmark.repo_builder._leak_score", lambda *args: 1)
    monkeypatch.setattr(
        "hallmark.repo_builder._drop_and_greedy_search", fake_greedy_search)
    matched_index, values = _match_file_against_fmts(
        "x_a.dat", [{"fmt": "x_{value}.dat", "db": "data.tsv"}])

    assert matched_index == 0, f"unexpected matched_index: {matched_index}, expected 0"
    assert values == {"value": "a"}, \
        f"unexpected values: {values}, expected {{'value': 'a'}}"
    assert calls == 1, \
        f"expected _drop_and_greedy_search to be called once, got {calls} calls"


### _resolve_manifest_path tests ###

@pytest.mark.parametrize(
    "filename, rel_dir, expected",
    [
        ("project/data.tgz", "project/", "project/data.tgz"),
        ("data.tgz", "project/", "project/data.tgz"),
        ("project/data.tgz", "root/project/", "root/project/data.tgz"),
        ("other/data.tgz", "root/project/", "other/data.tgz")])
def test_resolve_manifest_path_handles_relative_and_complete_paths(
    filename, rel_dir, expected):
    """
    Test that _resolve_manifest_path correctly resolves relative and complete paths
    Args:
        filename: The filename to resolve.
        rel_dir: The relative directory to resolve against.
        expected: The expected resolved path.
    """
    assert _resolve_manifest_path(filename, rel_dir) == expected, \
        f"unexpected resolved path for filename '{filename}' in rel_dir '{rel_dir}': \
            got '{_resolve_manifest_path(filename, rel_dir)}', expected '{expected}'"


@pytest.mark.parametrize(
    "filename",[
        "",
        "../outside.dat",
        "/absolute.dat",
        r"..\outside.dat"])
def test_resolve_manifest_path_rejects_unsafe_paths(filename):
    """
    Test that _resolve_manifest_path raises a ValueError for unsafe paths
    Args:
        filename: The filename to test for unsafe path resolution.
    Raises:
        ValueError: If the filename is unsafe (e.g., empty, absolute, or contains
        parent directory references).
    """
    with pytest.raises(ValueError, match="manifest path"):
        _resolve_manifest_path(filename, "project/")


### _remote_url tests ###

def test_remote_url_encodes_relative_path_characters():
    """
    Test that _remote_url correctly encodes special characters in the relative path.
    """
    result = _remote_url(BASE_URL, "nested/a+b #1.dat")

    assert result == f"{BASE_URL}nested/a%2Bb%20%231.dat", \
        f"unexpected remote URL: {result}, expected '{BASE_URL}nested/a%2Bb%20%231.dat'"


def test_resolve_manifest_path_removes_only_dot_slash_prefixes():
    """
    Test that _resolve_manifest_path correctly removes only './' prefixes and does not
    remove other relative path components.
    """
    assert _resolve_manifest_path("././data.tgz", "project/") == "project/data.tgz", \
    f"unexpected resolved path for '././data.tgz': got \
    '{_resolve_manifest_path('././data.tgz', 'project/')}', expected 'project/data.tgz'"


### build_repo tests ###

# create a pytest fixture for a simple mock dataset server
@pytest.fixture
def simple_dataset_server():
    """
    A simple mock dataset server with a small directory structure and
    a manifest file, to test build_repo without hitting the real network.
    returns:
        A MockServer instance with the dataset structure registered.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("collection", "proj/"),
        ("data-object", "README.md"),])
    server.add_directory("proj", [
        ("data-object", "data.tar"),
        ("data-object", "md5sum.txt"),])
    server.add_file("proj/md5sum.txt",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  proj/data.tar\n")
    server.add_file("README.md", "root readme content")
    return server


# parametrize the remotes argument to test different input types
@pytest.mark.parametrize(
    "remotes_arg, expected",
    [
        ("origin", [{"name": "origin", "url": BASE_URL}]),
        ({"name": "origin"}, [{"name": "origin", "url": BASE_URL}]),
        ([{"name": "origin"}], [{"name": "origin", "url": BASE_URL}]),],)


def test_build_repo_remote_normalization(tmp_path, simple_dataset_server, remotes_arg,
                                         expected):
    """
    Test that build_repo normalizes the remotes argument correctly, using a simple mock
    dataset server to avoid hitting the real network.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
        simple_dataset_server: A fixture providing a MockServer instance
            with a simple dataset structure and manifest.
        remotes_arg: The input remotes argument to be normalized by build_repo.
        expected: The expected normalized remotes configuration.
    """
    with _served(simple_dataset_server):
        repo = build_repo(
            repo_path=tmp_path / "repo_remote_norm.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=[],
            remotes=remotes_arg,)

    _assert_remote_config(repo, expected)


def test_build_repo_writes_expected_tsv_row(tmp_path, simple_dataset_server):
    """
    Test that build_repo writes the expected TSV row for a file with a
    known checksum, using a simple mock dataset server to avoid hitting
    the real network.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
        simple_dataset_server: A fixture providing a MockServer instance
            with a simple dataset structure and manifest."""
    fmt_entries = [
        {"fmt": "proj/data.{format}", "db": "data.tsv"},]
    with _served(simple_dataset_server):
        repo = build_repo(
            repo_path=tmp_path / "repo.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=fmt_entries,)
    df = pd.read_csv(repo.dothm.path / "data.tsv", sep="\t", dtype=str)
    assert len(df) == 1, f"unexpected number of rows in data.tsv: {len(df)}, expected 1"
    row = df.iloc[0]

    assert row["path"] == "proj/data.tar", \
        f"unexpected path: {row['path']}, expected 'proj/data.tar'"
    assert row["checksum_algorithm"] == "md5", \
        f"unexpected checksum_algorithm: {row['checksum_algorithm']}, expected 'md5'"
    assert row["checksum"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", f"unexpected checksum\
        : {row['checksum']}, expected 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'"


def test_build_repo_root_static_file_gets_known_name(tmp_path, simple_dataset_server):
    """
    Test that a root-level static file with no manifest coverage gets
    the expected known name in the repo, using a simple mock dataset
    server to avoid hitting the real network.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
        simple_dataset_server: A fixture providing a MockServer instance
            with a simple dataset structure and manifest.
    """
    with _served(simple_dataset_server):
        repo = build_repo(
            repo_path=tmp_path / "repo2.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=[],)
    static_entries = [e for e in repo.state.config["data"]
                      if e.get("file") == "README.md"]

    assert len(static_entries) == 1, f"unexpected number of static entries for \
        README.md: {len(static_entries)}, expected 1"
    assert static_entries[0].get("name") == "readme", f"unexpected name for README.md:\
          {static_entries[0].get('name')}, expected 'readme'"


def test_build_repo_size_threshold_skips_large_unmatched_file(tmp_path):
    """
    Test that build_repo skips downloading a large unmatched file, using
    a mock server to avoid hitting the real network.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
    Helper functions:
        huge_head: A helper function to simulate a HEAD request for a large file.
        refuse_get_body: A helper function to simulate a GET request for a large file
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [("data-object", "huge_unmatched.dat"),])
    server.add_file("huge_unmatched.dat", b"x" * 100)
    def huge_head(url, timeout=None):
        """Simulate a HEAD request for a large file"""
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.headers = {"Content-Length": str(5 * 1024 * 1024 * 1024)}
        resp.__enter__.return_value = resp
        return resp
    server.fake_head = huge_head
    def refuse_get_body(url, timeout=None):
        """Simulate a GET request for a large file, raising an AssertionError"""
        raise AssertionError(f"should never GET a file this large: {url}")
    listing_get = server.fake_get
    server.fake_get = lambda url, timeout=None: (
        listing_get(url, timeout)
        if not url.endswith("huge_unmatched.dat")
        else refuse_get_body(url, timeout))
    with _served(server):
        repo = build_repo(
            repo_path=tmp_path / "repo3.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=[],)
    entry = next(
        e for e in repo.state.config["data"]
        if e.get("file") == "huge_unmatched.dat")

    assert entry == {"file": "huge_unmatched.dat"}, \
        f"unexpected entry for huge_unmatched.dat: {entry}, expected checksum 'unknown'"


def test_build_repo_small_unmatched_file_is_not_downloaded(tmp_path):
    server = MockServer(BASE_URL)
    server.add_directory("", [("data-object", "README.md")])
    # Even a tiny file stays remote until download approval.
    with _served(server):
        repo = build_repo(tmp_path / "repo.hm", "EHTC_TEST", fmt_entries=[])
    assert repo.state.config["data"] == [{"name": "readme", "file": "README.md"}]


def test_build_repo_defaults_to_generic_path_catalog_without_input(
        monkeypatch, tmp_path):
    _inventory(monkeypatch, {
        "arbitrary.bin": (None, None), "nested/a.fits": (None, None)})
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("Unexpected prompt"))
    repo = build_repo(tmp_path / "catalog", "label")
    assert repo.state.config["data"] == [{"fmt": "{path}", "db": "data.tsv"}]
    frame = pd.read_csv(repo.dothm.path / "data.tsv", sep="\t")
    assert set(frame["path"]) == {"arbitrary.bin", "nested/a.fits"}




def test_build_repo_rejects_payload_hashing_before_network(monkeypatch, tmp_path):
    monkeypatch.setattr("hallmark.repo_builder.list_remote_files",
                        lambda *_: pytest.fail("Unexpected network access"))
    with pytest.raises(CapabilityError, match="remote_hash"):
        build_repo(tmp_path / "catalog", "label", remote_hash=True)


def test_build_repo_multiple_remotes(tmp_path, simple_dataset_server):
    """
    Test that build_repo correctly records multiple remotes in the repo's
    configuration, using a simple mock dataset server to avoid hitting the real network.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
        simple_dataset_server: A fixture providing a MockServer instance
            with a simple dataset structure and manifest.
    """
    with _served(simple_dataset_server):
        repo = build_repo(
            repo_path=tmp_path / "repo5.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=[],
            remotes=[
                {"name": "origin", "url": "https://example.com/origin/"},
                {"name": "mirror", "url": "https://example.com/mirror/"},],)

    assert repo.state.config["remote"] == [
        {"name": "origin", "url": "https://example.com/origin/"},
        {"name": "mirror", "url": "https://example.com/mirror/"},]\
        , f"unexpected remotes: {repo.state.config['remote']}, expected two remotes"


def test_build_repo_remote_without_url_defaults_to_dataset_url(tmp_path,
                                                                simple_dataset_server):
    """
    Test that build_repo correctly defaults a remote without a URL to the dataset's
    base URL, using a simple mock dataset server to avoid hitting the real network.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
        simple_dataset_server: A fixture providing a MockServer instance
            with a simple dataset structure and manifest.
    """
    with _served(simple_dataset_server):
        repo = build_repo(
            repo_path=tmp_path / "repo6.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=[],
            remotes=[{"name": "origin"}],)

    assert repo.state.config["remote"] == [{"name": "origin", "url": BASE_URL}], \
        f"unexpected remotes: {repo.state.config['remote']}, expected one remote with \
            URL {BASE_URL}"


def test_build_repo_no_remotes_is_empty_list(tmp_path, simple_dataset_server):
    """
    Test that build_repo correctly records an empty list of remotes when none are
    provided, using a simple mock dataset server to avoid hitting the real network.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
        simple_dataset_server: A fixture providing a MockServer instance
            with a simple dataset structure and manifest.
    """
    with _served(simple_dataset_server):
        repo = build_repo(
            repo_path=tmp_path / "repo7.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=[])

    assert repo.state.config["remote"] == [{"name": "origin", "url": BASE_URL}], \
        f"unexpected remotes: {repo.state.config['remote']}, expected default origin"


def test_build_repo_uses_multiple_fmt_entries(tmp_path):
    """
    Separate format entries should be preserved in the repo config.
    Args:
        tmp_path: A temporary directory provided by pytest for the test.
    """
    server = MockServer(BASE_URL)
    server.add_directory("", [
        ("data-object", "projA_1.txt"),
        ("data-object", "projB_2.txt"),])
    fmt_entries = [
        {"fmt": "projA_{n}.txt", "db": "a.tsv"},
        {"fmt": "projB_{n}.txt", "db": "b.tsv"},]
    with _served(server):
        repo = build_repo(
            repo_path=tmp_path / "multi.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=fmt_entries,)

    assert repo.state.config["data"] == fmt_entries, \
        f"unexpected data config: {repo.state.config['data']}, expected {fmt_entries}"


def test_build_repo_combines_rows_for_formats_sharing_a_tsv(monkeypatch, tmp_path):
    """
    When multiple format entries share the same TSV, the rows for those formats
    should be combined into a single TSV file, rather than creating separate TSVs.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    _inventory(
        monkeypatch, {"a_1.dat": ("md5", "a" * 32), "b_2.dat": ("sha1", "b" * 40)})
    repo = build_repo(
        tmp_path / "combined.hm",
        "EHTC_TEST",
        fmt_entries=[
            {"fmt": "a_{first}.dat", "db": "science.tsv"},
            {"fmt": "b_{second}.dat", "db": "science.tsv"}])
    frame = pd.read_csv(
        repo.dothm.path / "science.tsv",
        sep="\t",
        dtype=str,
        keep_default_na=False).set_index("path")

    assert set(frame.index) == {"a_1.dat", "b_2.dat"}, \
        f"unexpected index: {set(frame.index)}, expected {{'a_1.dat', 'b_2.dat'}}"
    assert list(frame.columns) == [
        "checksum_algorithm", "checksum", "first", "second"], \
        f"unexpected columns: {list(frame.columns)}, expected ['checksum_algorithm', \
            'checksum', 'first', 'second']"
    assert frame.loc["a_1.dat", "first"] == "1", f"unexpected first value for a_1.dat: \
        {frame.loc['a_1.dat', 'first']}, expected '1'"
    assert frame.loc["a_1.dat", "second"] == "None", f"unexpected second value for \
        a_1.dat: {frame.loc['a_1.dat', 'second']}, expected 'None'"
    assert frame.loc["b_2.dat", "first"] == "None", f"unexpected first value for \
        b_2.dat: {frame.loc['b_2.dat', 'first']}, expected 'None'"
    assert frame.loc["b_2.dat", "second"] == "2", f"unexpected second value for \
        b_2.dat: {frame.loc['b_2.dat', 'second']}, expected '2'"
    assert repo.state.config["data"] == [
        {"fmt": "a_{first}.dat", "db": "science.tsv"},
        {"fmt": "b_{second}.dat", "db": "science.tsv"}], \
        f"unexpected data config: {repo.state.config['data']}, expected two fmt entries"


def test_build_repo_writes_none_for_missing_matched_values(monkeypatch, tmp_path):
    """
    If a matched format has a field that is missing from the filename, the
    corresponding value in the TSV should be "None" rather than an empty string.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    _inventory(
        monkeypatch,
        {
            "a0.75.h5": ("md5", "a" * 32),
            "a0.75_i30.h5": ("md5", "b" * 32)})
    repo = build_repo(
        tmp_path / "missing-values.hm",
        "EHTC_TEST",
        fmt_entries=[{"fmt": "a{a}_i{i}.h5", "db": "science.tsv"}])
    frame = pd.read_csv(
        repo.dothm.path / "science.tsv",
        sep="\t",
        dtype=str,
        keep_default_na=False).set_index("path")

    assert frame.loc["a0.75.h5", "i"] == "None", f"unexpected i value for a0.75.h5: \
        {frame.loc['a0.75.h5', 'i']}, expected 'None'"
    assert frame.loc["a0.75_i30.h5", "i"] == "30", f"unexpected i value for \
        a0.75_i30.h5: {frame.loc['a0.75_i30.h5', 'i']}, expected '30'"


def test_build_repo_config_separates_static_fmt_remote_and_meta_blocks(
    monkeypatch, tmp_path):
    """
    The config.yml should have separate blocks for static files, fmt entries, remotes,
    and meta entries, rather than merging them all into one block
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    _inventory(
        monkeypatch,
        {
            "README.md": ("md5", "a" * 32),
            "data_1.fits": ("sha1", "b" * 40),
            "meta.yml": ("md5", "c" * 32)})
    repo = build_repo(
        tmp_path / "formatted.hm",
        "EHTC_TEST",
        fmt_entries=[{"fmt": "data_{number}.fits", "db": "science.tsv"}],)
    text = (repo.dothm.path / "config.yml").read_text(encoding="utf-8")

    assert "\n\n- fmt: data_{number}.fits" in text, f"unexpected config.yml content: \
        {text}, expected separate blocks for fmt, remote, and meta"
    assert "\n\nremote:" in text, f"unexpected config.yml content: {text}, \
        expected separate blocks for fmt, remote, and meta"
    assert "\n\nmeta:" in text, f"unexpected config.yml content: {text}, \
        expected separate blocks for fmt, remote, and meta"


def test_build_repo_empty_dataset_writes_valid_empty_data_config(monkeypatch, tmp_path):
    """
    An empty dataset should still create a valid config.yml with an empty data list.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    _inventory(monkeypatch, {})
    repo = build_repo(tmp_path / "empty.hm", "EHTC_TEST", fmt_entries=[])
    config_path = repo.dothm.path / "config.yml"

    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["data"] == [], \
        f"unexpected config.yml content: {config_path.read_text(encoding='utf-8')},\
            expected empty data list"
    assert config_path.read_text(encoding="utf-8").startswith("data: []\n"), \
        f"unexpected config.yml content: {config_path.read_text(encoding='utf-8')}, \
            expected 'data: []' at the start"


def test_build_repo_loads_formats_and_remotes_from_explicit_config(
    monkeypatch, tmp_path):
    """
    If the user selects a config file that has formats and remotes, build_repo should
    load those formats and remotes into the new repo's config
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    source_config = tmp_path / "source.yml"
    source_config.write_text(
        yaml.safe_dump(
            {
                "data": [
                    {"file": "README.md"},
                    {"fmt": "item_{number}.dat", "db": "items.tsv"}],
                "remote": [
                    {"name": "mirror", "url": "https://mirror.test/data"}
                ]}), encoding="utf-8")
    answers = iter(["config", str(source_config)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})
    repo = build_repo(
        tmp_path / "from-config.hm", "EHTC_TEST", config_file=source_config)

    assert {entry.get("fmt") for entry in repo.state.config["data"]} == {
        "item_{number}.dat"}, f"unexpected fmt entries: {repo.state.config['data']}, \
            expected to load from config"
    assert repo.state.config["remote"] == [
        {"name": "mirror", "url": "https://mirror.test/data"}], f"unexpected remotes: \
            {repo.state.config['remote']}, expected to load from config"


def test_build_repo_explicit_remotes_override_loaded_config(monkeypatch, tmp_path):
    """
    If the user selects a config file that has remotes, but also provides explicit
    remotes to build_repo, the explicit remotes should override the config remotes.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    source_config = tmp_path / "source.yml"
    source_config.write_text(
        yaml.safe_dump(
            {
                "data": [{"fmt": "item_{number}.dat", "db": "items.tsv"}],
                "remote": [{"name": "old", "url": "https://old.test"}],
            }),encoding="utf-8")
    answers = iter(["config", str(source_config)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})
    repo = build_repo(
        tmp_path / "override.hm",
        "EHTC_TEST",
        remotes=[{"name": "new", "url": "https://new.test"}],
        config_file=source_config)

    assert repo.state.config["remote"] == [{"name": "new", "url": "https://new.test"}],\
     f"unexpected remotes: {repo.state.config['remote']}, \
        expected to override with new remote"


def test_build_repo_explicit_config_requires_a_format(monkeypatch, tmp_path):
    """
    If the user selects a config file that has no fmt entries, build_repo should raise
    a ValueError indicating that no formats were found.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        ValueError: If the selected config file has no fmt entries.
    """
    source_config = tmp_path / "source.yml"
    source_config.write_text("data:\n- file: README.md\n", encoding="utf-8")
    answers = iter(["config", str(source_config)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    with pytest.raises(ValueError, match="No fmt entries found"):
        build_repo(tmp_path / "missing-fmt.hm", "EHTC_TEST", config_file=source_config)


def test_build_repo_reuses_existing_formats_and_remotes(monkeypatch, tmp_path):
    """
    If a repo already exists, build_repo should prompt to reuse its formats and remotes.
    If user agrees, the existing formats and remotes should be preserved in the new repo
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    files = {"item_1.dat": ("md5", "a" * 32)}
    calls = _inventory(monkeypatch, files)
    repo_path = tmp_path / "existing.hm"
    original = build_repo(
        repo_path,
        "EHTC_TEST",
        fmt_entries=[{"fmt": "item_{number}.dat", "db": "items.tsv"}],
        remotes=[{"name": "mirror", "url": "https://mirror.test"}])
    monkeypatch.setattr("builtins.input", lambda prompt: "use")
    reused = build_repo(repo_path, "EHTC_TEST")

    assert reused.dothm.path == original.dothm.path, f"unexpected repo path: \
        {reused.dothm.path}, expected to reuse {original.dothm.path}"
    assert reused.state.config["remote"] == [
        {"name": "mirror", "url": "https://mirror.test"}], f"unexpected remotes: \
        {reused.state.config['remote']}, expected to reuse the original remote entry"
    assert [entry for entry in reused.state.config["data"] if "fmt" in entry] == [
        {"fmt": "item_{number}.dat", "db": "items.tsv"}], f"unexpected data config: \
            {reused.state.config['data']}, expected to reuse the original fmt entry"
    assert calls == [BASE_URL, BASE_URL], f"unexpected calls to list_remote_files: \
        {calls}, expected two calls to {BASE_URL}"


def test_build_repo_reuses_existing_catalog_without_input(monkeypatch, tmp_path):
    _inventory(monkeypatch, {"item_1.dat": (None, None)})
    path = tmp_path / "catalog"
    build_repo(path, "label", [{"fmt": "item_{number}.dat", "db": "data.tsv"}])
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("Unexpected prompt"))
    repo = build_repo(path, "label")
    assert repo.state.config["data"] == [{"fmt": "item_{number}.dat", "db": "data.tsv"}]






def test_build_repo_omits_detect_include_drives_when_fmt_entries_given(
    monkeypatch, tmp_path):
    """
    When fmt_entries are provided directly (not auto-detected), meta.yml should not
    contain a detect_include_drives key.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})
    repo = build_repo(
        tmp_path / "explicit.hm", "EHTC_TEST",
        fmt_entries=[{"fmt": "item_{number}.dat", "db": "data.tsv"}])

    assert "detect_include_drives" not in repo.state.meta, \
        f"did not expect detect_include_drives in meta.yml, got: {repo.state.meta}"








def test_build_repo_preserves_existing_destination_by_default(
    monkeypatch,
    tmp_path):
    """
    Test that build_repo raises a FileExistsError when the destination directory
    already exists, and that it does not modify any existing files in that directory.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        FileExistsError: If the destination directory already exists.
    """
    destination = tmp_path / "existing.hm"
    destination.mkdir()
    sentinel = destination / "important.txt"
    sentinel.write_text("do not delete\n", encoding="utf-8")
    def unexpected_network_request(_base_url):
        """This function should not be called, as the destination already exists."""
        raise AssertionError(
            "build_repo should reject the destination before listing files")
    monkeypatch.setattr(
        "hallmark.repo_builder.list_remote_files",
        unexpected_network_request)
    with pytest.raises(FileExistsError, match="already exists"):
        build_repo(repo_path=destination, dataset_name="EHTC_TEST", fmt_entries=[])

    assert sentinel.read_text(encoding="utf-8") == "do not delete\n", \
        f"expected sentinel file {sentinel} to remain unchanged, but it was modified"


def test_build_repo_replaces_destination_when_overwrite_is_explicit(
    monkeypatch,
    tmp_path):
    """
    Test that build_repo replaces the contents of an existing destination directory
    when the overwrite argument is set to True, and that it removes any existing files
    in that directory before creating the new repo.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    destination = tmp_path / "existing.hm"
    destination.mkdir()
    sentinel = destination / "old-file.txt"
    sentinel.write_text("old data\n", encoding="utf-8")
    monkeypatch.setattr(
        "hallmark.repo_builder.list_remote_files",
        lambda _base_url, **kwargs: {})
    repo = build_repo(
        repo_path=destination,
        dataset_name="EHTC_TEST",
        fmt_entries=[],
        overwrite=True)

    assert repo.dothm.path == destination.resolve(), \
        f"unexpected repo path: {repo.dothm.path}, expected {destination.resolve()}"
    assert not sentinel.exists(), \
        f"expected old file {sentinel} to be removed, but it still exists"
    assert (destination / "config.yml").is_file(), \
        f"expected config.yml to be created in {destination}, but it does not exist"


def test_build_repo_rejects_unsafe_db_before_network(monkeypatch, tmp_path):
    """
    Test that build_repo raises a ValueError when an unsafe db path is provided in
    fmt_entries, and it does not attempt any network operations before raising an error.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        AssertionError: If build_repo attempts to perform network operations before
            validating the db path.
        ValueError: If an unsafe db path is provided in fmt_entries.
    """
    def unexpected_network_request(_base_url):
        """This function should not be called, as the unsafe fmt entry should be
        rejected before any network access is attempted."""
        raise AssertionError(
            "unsafe fmt entries should be rejected before network access")
    monkeypatch.setattr(
        "hallmark.repo_builder.list_remote_files",
        unexpected_network_request)

    with pytest.raises(ValueError, match="TSV database name"):
        build_repo(
            repo_path=tmp_path / "repo.hm",
            dataset_name="EHTC_TEST",
            fmt_entries=[{
                    "fmt": "data_{number}.txt",
                    "db": "../../outside.tsv",}])


def test_build_repo_does_not_hide_unexpected_existing_repo_errors(monkeypatch,
                                                                  tmp_path):
    """
    Test that build_repo does not suppress unexpected errors when attempting to open
    an existing repo, and that it raises the original error instead of a generic one.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        OSError: If an unexpected error occurs when opening an existing repo.
    """
    repo_path = tmp_path / "existing.hm"
    repo_path.mkdir()
    def fail_to_open_repo(path):
        """Simulate a failure to open an existing repo, raising an OSError."""
        raise OSError("cannot read existing config")
    monkeypatch.setattr("hallmark.repo_builder.Repo", fail_to_open_repo)

    with pytest.raises(OSError, match="cannot read existing config"):
        build_repo(
            repo_path=repo_path,
            dataset_name="dataset",
            fmt_entries=None,
            overwrite=True)


def test_build_repo_accepts_config_repository_directory(monkeypatch, tmp_path):
    """
    Test that build_repo can accept a config file from a repository directory,
    and that it correctly loads the formats and remotes from that config.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    source_repo = tmp_path / "source.hm"
    source_repo.mkdir()
    (source_repo / "config.yml").write_text(
        yaml.safe_dump({
                "data": [{
                        "fmt": "item_{number}.dat",
                        "db": "items.tsv"}],
                "remote": {
                    "name": "mirror",
                    "url": "https://mirror.test/data"}}),encoding="utf-8")
    answers = iter(["config", str(source_repo)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})
    repo = build_repo(tmp_path / "result.hm", "EHTC_TEST", config_file=source_repo)
    actual = [
        entry for entry in repo.state.config["data"] if "fmt" in entry]
    expected = [{"fmt": "item_{number}.dat", "db": "items.tsv"}]

    assert actual == expected, f"unexpected data config: {actual}, expected {expected}"


def test_build_repo_preserves_meta_file_checksum(monkeypatch, tmp_path):
    """
    Test that build_repo correctly preserves the checksum of a meta file in the
    repo's configuration, and that it does not modify the checksum value.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    checksum = "c" * 64
    _inventory(monkeypatch, {"meta.yml": ("sha256", checksum)})
    repo = build_repo(tmp_path / "repo.hm", "EHTC_TEST", fmt_entries=[])

    assert repo.state.config["meta"] == [{"file": "meta.yml", "sha256": checksum}], \
        f"unexpected meta config: {repo.state.config['meta']}, \
            expected meta.yml with checksum {checksum}"


def test_build_repo_never_requests_static_checksum(monkeypatch, tmp_path):
    _inventory(monkeypatch, {"README.md": (None, None)})
    monkeypatch.setattr("hallmark.transport.requests.Session",
                        lambda: pytest.fail("Unexpected checksum request"))
    repo = build_repo(tmp_path / "repo.hm", "EHTC_TEST", fmt_entries=[])
    assert repo.state.config["data"] == [{"name": "readme", "file": "README.md"}]


def test_build_repo_preserves_multiple_meta_files(monkeypatch, tmp_path):
    """
    Test that build_repo correctly preserves the checksums of multiple meta files in the
    repo's configuration, and that it does not modify the checksum values.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    first_checksum = "a" * 64
    second_checksum = "b" * 64
    _inventory(
        monkeypatch,{
            "first/meta.yml": (
                "sha256",
                first_checksum),
            "second/meta.yml": (
                "sha256",
                second_checksum)})
    repo = build_repo(tmp_path / "repo.hm", "EHTC_TEST", fmt_entries=[])

    assert repo.state.config["meta"] == [{
            "file": "first/meta.yml",
            "sha256": first_checksum},
        {
            "file": "second/meta.yml",
            "sha256": second_checksum}], f"unexpected meta config: \
                {repo.state.config['meta']}, expected first/meta.yml with checksum \
                {first_checksum} and second/meta.yml with checksum {second_checksum}"


def test_build_repo_rejects_duplicate_formats_before_network(monkeypatch, tmp_path):
    """
    Test that build_repo raises a ValueError when duplicate fmt entries are provided,
    and that it does not attempt any network operations before raising an error.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        AssertionError: If build_repo attempts to perform network operations after
            detecting duplicate fmt entries.
        ValueError: If duplicate fmt entries are provided in fmt_entries.
    """
    def unexpected_network_request(base_url):
        """This function should not be called, as duplicate fmt entries should be
        rejected before any network access is attempted."""
        raise AssertionError(
            "duplicate formats should be rejected before network access")
    monkeypatch.setattr(
        "hallmark.repo_builder.list_remote_files",
        unexpected_network_request)

    with pytest.raises(ValueError, match="duplicates fmt entry"):
        build_repo(
            tmp_path / "repo.hm",
            "EHTC_TEST",
            fmt_entries=[{
                    "fmt": "data_{number}.fits",
                    "db": "first.tsv"},
                {
                    "fmt": " data_{number}.fits ",
                    "db": "second.tsv"}])


def test_build_repo_rejects_duplicate_remote_names_before_network(monkeypatch,
                                                                  tmp_path):
    """
    Test that build_repo raises a ValueError when duplicate remote names are provided,
    and that it does not attempt any network operations before raising an error.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        AssertionError: If build_repo attempts to perform network operations after
            detecting duplicate remote names.
        ValueError: If duplicate remote names are provided in remotes.
    """
    def unexpected_network_request(base_url):
        """This function should not be called, as duplicate remote names should be
        rejected before any network access is attempted."""
        raise AssertionError(
            "invalid remotes should be rejected before network access")
    monkeypatch.setattr(
        "hallmark.repo_builder.list_remote_files",
        unexpected_network_request)

    with pytest.raises(ValueError, match="duplicates the name"):
        build_repo(
            tmp_path / "repo.hm",
            "EHTC_TEST",
            fmt_entries=[],
            remotes=[{
                    "name": "mirror",
                    "url": "https://first.test"},
                {
                    "name": "mirror",
                    "url": "https://second.test"}])


def test_build_repo_rejects_multiple_unnamed_remotes(monkeypatch, tmp_path):
    """
    Test that build_repo raises a ValueError when multiple remotes are provided without
    names, and that it does not attempt any network operations before raising an error.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        AssertionError: If build_repo attempts to perform network operations after
            detecting multiple unnamed remotes.
        ValueError: If multiple unnamed remotes are provided in remotes.
    """
    def unexpected_network_request(base_url):
        """This function should not be called, as multiple unnamed remotes should be
        rejected before any network access is attempted."""
        raise AssertionError("invalid remotes should be rejected before network access")
    monkeypatch.setattr(
        "hallmark.repo_builder.list_remote_files",
        unexpected_network_request)

    with pytest.raises(ValueError, match="must all define names"):
        build_repo(
            tmp_path / "repo.hm",
            "EHTC_TEST",
            fmt_entries=[],
            remotes=[{"url": "https://first.test"}, {"url": "https://second.test"}])


def test_build_repo_preserves_config_when_final_yaml_write_fails(monkeypatch, tmp_path):
    """
    Test that build_repo preserves the existing config.yml file if the final write
    operation fails, and that it does not leave any temporary files behind.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        RuntimeError: If the final config.yml write operation fails.
    """
    _inventory(monkeypatch, {})
    original_dump = yaml.dump

    def fail_final_remote_dump(data, handle, **kwargs):
        """Simulate a failure when dumping the final config.yml,
        but allow earlier dumps to succeed."""
        if (isinstance(data, dict) and set(data) == {"remote"}):
            handle.write("partial output")
            raise RuntimeError("final config serialization failed")
        return original_dump(data, handle, **kwargs)

    monkeypatch.setattr("hallmark.dothm.yaml.dump", fail_final_remote_dump)
    repo_path = tmp_path / "repo.hm"
    with pytest.raises(RuntimeError, match="final config serialization failed"):
        build_repo(repo_path, "EHTC_TEST", fmt_entries=[])

    config_path = repo_path / "config.yml"
    preserved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(preserved_config, dict), \
        f"expected preserved config to be a dict, got {type(preserved_config)}"
    assert "data" in preserved_config, \
        f"expected 'data' key in preserved config, got {preserved_config.keys()}"
    assert list(repo_path.glob(".config.yml.*.tmp")) == [], \
        f"unexpected temporary config files found in {repo_path}, expected none"


def test_build_repo_loads_formats_and_remotes_from_config_file_parameter(
        monkeypatch, tmp_path):
    """
    Test that build_repo correctly loads formats and remotes from a provided config
    file parameter, and that it does not prompt the user for input.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    source_config = tmp_path / "source.yml"
    source_config.write_text(
        yaml.safe_dump({
                "data": [{"fmt": "item_{number}.dat", "db": "items.tsv"}],
                "remote": [{"name": "mirror", "url": "https://mirror.test/data"}]}),
                encoding="utf-8")
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})
    repo = build_repo(
        tmp_path / "param-config.hm", "EHTC_TEST", config_file=source_config)

    assert [entry for entry in repo.state.config["data"] if "fmt" in entry] == [
        {"fmt": "item_{number}.dat", "db": "items.tsv"}], \
            f"unexpected data config: {repo.state.config['data']}"
    assert repo.state.config["remote"] == [
        {"name": "mirror", "url": "https://mirror.test/data"}], \
            f"unexpected remotes: {repo.state.config['remote']}"


def test_build_repo_rejects_config_file_with_fmt_entries(monkeypatch, tmp_path):
    """
    Test that build_repo raises a ValueError when both fmt_entries and config_file
    are provided, and it does not attempt any network operations before raising error.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        ValueError: If both fmt_entries and config_file are provided to build_repo.
    """
    source_config = tmp_path / "source.yml"
    source_config.write_text(
        yaml.safe_dump({"data": [{"fmt": "item_{number}.dat", "db": "items.tsv"}]}),
        encoding="utf-8")
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})

    with pytest.raises(
        ValueError, match="Cannot provide both fmt_entries and config_file"):
        build_repo(
            tmp_path / "conflict.hm",
            "EHTC_TEST",
            fmt_entries=[{"fmt": "x_{n}.dat", "db": "x.tsv"}],
            config_file=source_config)


def test_build_repo_config_file_parameter_requires_a_format(monkeypatch, tmp_path):
    """
    Test that build_repo raises a ValueError when the config_file parameter points to
    a config file with no fmt entries, before any network operations are attempted.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        ValueError: If the config_file has no fmt entries.
    """
    source_config = tmp_path / "source.yml"
    source_config.write_text("data:\n- file: README.md\n", encoding="utf-8")
    def network_must_not_run(_base_url):
        """This function should not be called, as the missing fmt entries should be
        detected before any network access is attempted."""
        raise AssertionError("config file validation should run before network listing")
    monkeypatch.setattr("hallmark.repo_builder.list_remote_files", network_must_not_run)

    with pytest.raises(ValueError, match="No fmt entries found"):
        build_repo(
            tmp_path / "missing-fmt-param.hm", "EHTC_TEST", config_file=source_config)


def test_build_repo_loads_from_config_directory_parameter(monkeypatch, tmp_path):
    """
    Test that build_repo accepts config_file as a directory and loads config.yml from it
    without using the interactive prompt flow.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    source_repo = tmp_path / "source.hm"
    source_repo.mkdir()
    (source_repo / "config.yml").write_text(
        yaml.safe_dump({
            "data": [{"fmt": "item_{number}.dat", "db": "items.tsv"}],
            "remote": [{"name": "mirror", "url": "https://mirror.test/data"}]}),
        encoding="utf-8")
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})
    repo = build_repo(
        tmp_path / "from-dir-param.hm",
        "EHTC_TEST",
        config_file=source_repo)

    assert [entry for entry in repo.state.config["data"] if "fmt" in entry] == [
        {"fmt": "item_{number}.dat", "db": "items.tsv"}], \
        f"unexpected data config: {repo.state.config['data']}"
    assert repo.state.config["remote"] == [
        {"name": "mirror", "url": "https://mirror.test/data"}], \
        f"unexpected remotes: {repo.state.config['remote']}"


def test_build_repo_config_directory_without_config_yml_rejected_before_network(
    monkeypatch, tmp_path):
    """
    Test that build_repo raises FileNotFoundError when config_file points to a
    directory without config.yml, and does so before any network listing is attempted.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    Raises:
        AssertionError: If build_repo attempts to perform network operations before
            validating the presence of config.yml in the provided directory.
        FileNotFoundError: If config.yml is missing in the provided config directory.
    """
    config_dir = tmp_path / "config-dir"
    config_dir.mkdir()
    def network_must_not_run(_base_url):
        """This function should not be called, as the missing config.yml should be
        detected before any network access is attempted."""
        raise AssertionError("config path validation should run before network listing")
    monkeypatch.setattr("hallmark.repo_builder.list_remote_files", network_must_not_run)

    with pytest.raises(FileNotFoundError, match="Config file does not exist"):
        build_repo(
            tmp_path / "missing-config.hm",
            "EHTC_TEST",
            config_file=config_dir)


def test_build_repo_explicit_remotes_override_config_file_remotes(
    monkeypatch, tmp_path):
    """
    Test that explicit remotes passed to build_repo override remotes loaded from a
    config_file parameter.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
        tmp_path: A temporary directory provided by pytest for the test.
    """
    source_config = tmp_path / "source.yml"
    source_config.write_text(
        yaml.safe_dump({
            "data": [{"fmt": "item_{number}.dat", "db": "items.tsv"}],
            "remote": [{"name": "old", "url": "https://old.test"}]}),
        encoding="utf-8")
    _inventory(monkeypatch, {"item_1.dat": ("md5", "a" * 32)})
    repo = build_repo(
        tmp_path / "override-config-param.hm",
        "EHTC_TEST",
        config_file=source_config,
        remotes=[{"name": "new", "url": "https://new.test"}])

    assert repo.state.config["remote"] == [{"name": "new", "url": "https://new.test"}],\
        f"unexpected remotes: {repo.state.config['remote']}, \
            expected explicit remotes to override config_file remotes"
