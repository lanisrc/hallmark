from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
import hashlib

from hallmark.downloader import (
    TSV_READ_CHUNK_SIZE,
    DownloadError,
    _download_file,
    _resolve_remote_path,
    _safe_remote_path,
    _select_remote_config,
    download_remote_data,
    select_download_files,
    _verify_validated_checksum,
    _entry_checksum,
    _remote_file_url)

# Mock response object for testing _download_file
class _Response:
    """A mock response object to simulate the behavior of requests.Response.
    Attributes:
        chunks (tuple): A tuple of byte strings representing the content of the response
        error (Exception): An optional exception for when raise_for_status is called.
    """
    def __init__(self, chunks=(), error=None):
        self.chunks = chunks
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def iter_content(self, chunk_size):
        return iter(self.chunks)


# Helper function to create a mock repository for testing
def _repo(tmp_path, config=None, data=None):
    """
    Create a mock repo object with a .hm directory and optional configuration and data.
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
        config (dict): Optional configuration dictionary to be stored in the repo.
        data (pd.DataFrame): Optional pandas DataFrame representing the repo's data.
    Returns:
        SimpleNamespace: A mock repo object with .hm path, state.config, and state.data
    """
    dothm_path = tmp_path / ".hm"
    dothm_path.mkdir()
    return SimpleNamespace(
        dothm=SimpleNamespace(path=dothm_path),
        state=SimpleNamespace(
            config={} if config is None else config,
            data=pd.DataFrame() if data is None else data))


### _download_file tests ###

def test_download_file_streams_atomically_and_returns_size(monkeypatch, tmp_path):
    """
    Test that _download_file streams data to a temporary file and then renames it
    to the final destination, returning the correct size of the downloaded file.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    """
    calls = {}
    def fake_get(url, **kwargs):
        """A fake requests.get function that records the URL and kwargs"""
        calls.update(url=url, kwargs=kwargs)
        return _Response([b"abc", b"", b"def"])
    monkeypatch.setattr("hallmark.downloader.requests.get", fake_get)
    destination = tmp_path / "nested" / "file.bin"
    size = _download_file("https://example.test/file.bin", destination, chunk_size=3)

    assert size == 6, f"Expected size 6, but got {size}"
    assert destination.read_bytes() == b"abcdef", f"Expected file content 'abcdef', \
        but got {destination.read_bytes()}"
    assert not destination.with_name("file.bin.part").exists(), \
        "Expected temporary file to be removed, but it still exists"
    assert calls == {
        "url": "https://example.test/file.bin",
        "kwargs": {"stream": True, "timeout": (10, 30)}}, f"Expected requests.get to be\
              called with the correct URL and kwargs, but got {calls}"


def test_download_file_removes_partial_file_after_http_error(monkeypatch, tmp_path):
    """
    Test that _download_file removes the temporary file if an HTTP error occurs during
    the download.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the download fails due to an HTTP error.
    """
    monkeypatch.setattr(
        "hallmark.downloader.requests.get",
        lambda *args, **kwargs: _Response(error=requests.HTTPError("404")))
    destination = tmp_path / "file.bin"

    with pytest.raises(DownloadError, match="Failed to download"):
        _download_file("https://example.test/file.bin", destination)
    assert not destination.with_name("file.bin.part").exists(), \
        "Expected temporary file to be removed after HTTP error, but it still exists"


def test_download_file_removes_partial_file_after_checksum_error(
    monkeypatch, tmp_path):
    """
    Test that _download_file removes the temporary file if a checksum mismatch occurs
    during the download.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the download fails due to a checksum mismatch.
    """
    monkeypatch.setattr(
        "hallmark.downloader.requests.get",
        lambda *args, **kwargs: _Response([b"content"]))
    destination = tmp_path / "file.bin"

    with pytest.raises(DownloadError, match="Checksum mismatch"):
        _download_file(
            "https://example.test/file.bin",
            destination,
            expected_checksum="0" * 40)
    assert not destination.exists(), "Expected destination file to be removed after\
          checksum error, but it still exists"
    assert not destination.with_name("file.bin.part").exists(), \
        "Expected temporary file to be removed after checksum error, but still exists"


def test_download_file_wraps_write_errors(monkeypatch, tmp_path):
    """
    Test that _download_file raises a DownloadError if an OSError occurs while writing
    to the temporary file.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the download fails due to an OSError while writing.
    """
    monkeypatch.setattr(
        "hallmark.downloader.requests.get",
        lambda *args, **kwargs: _Response([b"content"]))
    original_open = Path.open
    def fail_part_open(path, *args, **kwargs):
        """A Path.open wrapper that raises an OSError when opening a .part file."""
        if path.name.endswith(".part"):
            raise OSError("disk full")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_part_open)
    destination = tmp_path / "file.bin"

    with pytest.raises(DownloadError, match="Failed to write"):
        _download_file("https://example.test/file.bin", destination)


### _resolve_remote_path tests ###

def test_resolve_remote_path_wraps_invalid_typed_format():
    """
    Test that _resolve_remote_path raises a DownloadError when a data format
    string contains a type specifier that cannot be satisfied by the data in the row.
    Raises:
        DownloadError: If the data format string cannot be satisfied by the row data.
    """
    row = pd.Series({"number": "not-an-integer"})

    with pytest.raises(DownloadError, match="Invalid data format"):
        _resolve_remote_path(row, [{"fmt": "{number:03d}.dat"}])


def test_resolve_remote_path_uses_explicit_path():
    """
    Test that _resolve_remote_path returns the explicit path from the row if it exists,
    regardless of the data format configuration.
    """
    row = pd.Series({"path": "nested/file.txt", "sha1": "abc"})

    assert _resolve_remote_path(row, []) == Path("nested/file.txt"), \
        f"Expected explicit path 'nested/file.txt', got {_resolve_remote_path(row, [])}"


def test_resolve_remote_path_builds_path_from_data_format():
    """
    Test that _resolve_remote_path constructs a path from the row data using the
    provided data format configuration.
    """
    row = pd.Series({
            "sha1": "abc",
            "release": "SR1",
            "source": "M87",
            "year": 2017,
            "doy": 95,
            "band": "hi",
            "pipeline": "hops",
            "step": "netcal",
            "type": "StokesI"})
    data_config = [{
            "fmt": (
                "{release}_{source}_{year}_{doy:03d}_{band}_"
                "{pipeline}_{step}_{type}.uvfits")}]

    assert _resolve_remote_path(row, data_config) == Path(
        "SR1_M87_2017_095_hi_hops_netcal_StokesI.uvfits"), \
        f"Expected constructed path 'SR1_M87_2017_095_hi_hops_netcal_StokesI.uvfits', \
            got {_resolve_remote_path(row, data_config)}"


def test_resolve_remote_path_builds_typed_path_from_string_values():
    """
    Test that _resolve_remote_path correctly formats a path using string values in the
    row, even when the format string specifies a type conversion (e.g., integer).
    """
    row = pd.Series({
            "sha1": "abc",
            "release": "SR1",
            "source": "M87",
            "year": "2017",
            "doy": "95",
            "band": "hi",
            "pipeline": "hops",
            "step": "netcal",
            "type": "StokesI"})
    data_config = [{
            "fmt": (
                "{release}_{source}_{year}_{doy:03d}_{band}_"
                "{pipeline}_{step}_{type}.uvfits")}]

    assert _resolve_remote_path(row, data_config) == Path(
        "SR1_M87_2017_095_hi_hops_netcal_StokesI.uvfits"), \
        f"Expected constructed path 'SR1_M87_2017_095_hi_hops_netcal_StokesI.uvfits', \
            got {_resolve_remote_path(row, data_config)}"


def test_resolve_remote_path_raises_when_no_path_can_be_built():
    """
    Test that _resolve_remote_path raises a DownloadError when it cannot construct a
    valid path from the row data and the provided data format configuration.
    Raises:
        DownloadError: If no valid path can be constructed from the row data.
    """
    row = pd.Series({"sha1": "abc", "release": "SR1"})

    with pytest.raises(DownloadError, match="Unable to resolve download path"):
        _resolve_remote_path(row, [{"fmt": "{missing}.uvfits"}])


def test_resolve_remote_path_accepts_mapping_row():
    """
    Test that _resolve_remote_path can accept a mapping (dictionary) as the row
    argument, in addition to a pandas Series, and correctly resolves the path.
    """
    resolved = _resolve_remote_path({"source": "M87"}, [{"fmt": "data/{source}.fits"}])

    assert resolved == Path("data/M87.fits"), \
        f"Expected resolved path 'data/M87.fits', got {resolved}"


def test_resolve_remote_path_uses_fmt_when_path_is_missing_value():
    """
    Test that _resolve_remote_path falls back to fmt-based resolution when a path field
    is present but contains a missing value.
    """
    row = pd.Series({"path": pd.NA, "source": "M87"})

    assert _resolve_remote_path(row, [{"fmt": "data/{source}.fits"}]) == \
        Path("data/M87.fits"), \
        "Expected fmt-based path resolution when row path is missing"


### _safe_remote_path tests ###

@pytest.mark.parametrize(
    "unsafe_path",[
        ".hm/config.yml",
        ".git/config",
        "C:/outside/file.dat",
        r"C:\outside\file.dat",
        "folder/../../outside.dat",
        "/absolute/file.dat",
        "../outside.dat",
        "",
        "."])
def test_safe_remote_path_rejects_unsafe_and_reserved_paths(unsafe_path):
    """
    Test that _safe_remote_path raises a DownloadError for unsafe or reserved paths.
    Args:
        unsafe_path (str): The path value to test for safety.
    Raises:
        DownloadError: If the path is unsafe or reserved.
    """
    with pytest.raises(DownloadError):
        _safe_remote_path(unsafe_path)


def test_safe_remote_path_preserves_literal_whitespace():
    """Catalog filenames are literal; significant spaces must survive download."""
    assert _safe_remote_path("  nested/file.txt  ") == Path("  nested/file.txt  ")


@pytest.mark.parametrize(
    "value", ["   ", r"nested\file"])
def test_safe_remote_path_rejects_unsafe_values(value):
    """
    Test that _safe_remote_path raises a DownloadError for unsafe or invalid path
    values, such as empty strings, absolute paths, or paths that traverse outside the
    intended directory.
    Args:
        value (str): The path value to test for safety.
    Raises:
        DownloadError: If the path value is unsafe or invalid.
    """
    with pytest.raises(DownloadError):
        _safe_remote_path(value)


### _select_remote_config tests ###

def test_select_remote_config_returns_none_when_unconfigured(tmp_path):
    """
    Test that _select_remote_config returns None when repo has no remote configuration.
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
    """
    assert _select_remote_config(_repo(tmp_path)) is None, f"Expected None for \
        unconfigured remote, got {_select_remote_config(_repo(tmp_path))}"


def test_select_remote_config_accepts_single_mapping(tmp_path):
    """
    Test that _select_remote_config returns the single remote configuration when only
    one mapping is present in the repo's configuration.
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
    """
    remote = {"name": "mirror", "url": "https://example.test/data"}
    repo = _repo(tmp_path, {"remote": remote})

    assert _select_remote_config(repo) == remote, f"Expected the single remote \
        configuration, got {_select_remote_config(repo)}"


def test_select_remote_config_uses_requested_remote(tmp_path):
    """
    Test that _select_remote_config returns the correct remote configuration when a
    specific remote name is requested
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
    """
    mirror = {"name": "mirror", "url": "https://mirror.test/data"}
    repo = _repo(tmp_path, {"remote": [{"name": "origin"}, mirror]})

    assert _select_remote_config(repo, "mirror") == mirror, f"Expected the requested \
        remote configuration 'mirror', got {_select_remote_config(repo, 'mirror')}"


def test_select_remote_config_prefers_origin(tmp_path):
    """
    Test that _select_remote_config returns the 'origin' remote configuration when
    multiple remotes are present and no specific remote is requested.
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
    """
    origin = {"name": "origin", "url": "https://origin.test/data"}
    repo = _repo(tmp_path, {"remote": [{"name": "mirror"}, origin]})

    assert _select_remote_config(repo) == origin, f"Expected 'origin' remote \
        configuration, got {_select_remote_config(repo)}"


@pytest.mark.parametrize(
    "configured, message",[
        ("origin", "Invalid remote configuration"),
        (["origin"], "Invalid remote entry"),
        (
            [{"name": "one"}, {"name": "two"}],
            "select one with --remote")])
def test_select_remote_config_rejects_invalid_or_ambiguous_config(
    tmp_path, configured, message):
    """
    Test that _select_remote_config raises a DownloadError when the remote configuration
    is invalid or ambiguous, such as a string instead of a mapping, an empty list,
    or multiple mappings without a clear selection.
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
        configured: The remote configuration to test.
        message (str): The expected error message to match in the raised exception.
    Raises:
        DownloadError: If the remote configuration is invalid or ambiguous.
    """
    repo = _repo(tmp_path, {"remote": configured})

    with pytest.raises(DownloadError, match=message):
        _select_remote_config(repo)


def test_select_remote_config_rejects_unknown_name(tmp_path):
    """
    Test that _select_remote_config raises a DownloadError when a requested remote name
    is not found in the repo's configuration.
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
    Raises:
        DownloadError: If the requested remote name is not found in the configuration.
    """
    repo = _repo(tmp_path, {"remote": [{"name": "origin"}]})

    with pytest.raises(DownloadError, match="'missing' is not configured"):
        _select_remote_config(repo, "missing")


def test_select_remote_config_normalizes_requested_name(tmp_path):
    """
    Test that _select_remote_config correctly normalizes the requested remote name by
    stripping whitespace and matching it against the configured remotes.
    Args:
        tmp_path (Path): A temporary directory path provided by pytest.
    """
    remote = {"name": " mirror ", "url": "https://example.test"}
    repo = _repo(tmp_path, {"remote": [remote]})

    assert _select_remote_config(repo, " mirror ") == {
        "name": "mirror", "url": "https://example.test"}, f"Expected normalized remote \
            name 'mirror', got {_select_remote_config(repo, ' mirror ')}"


### download_remote_data tests ###

def test_download_remote_data_builds_urls_and_destinations_from_fmt(
        monkeypatch, tmp_path):
    """
    Test that download_remote_data constructs the correct URLs and destination paths
    based on the data format configuration and the row data, and that it calls
    _download_file with the expected arguments.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    """
    captured = {}
    def fake_download_file(context, relative_path, destination, sha1, chunk_size):
        url = context.remote.file_url(relative_path.as_posix())
        """A fake _download_file function that records its arguments"""
        captured.setdefault("files", []).append((url, destination, sha1))
        return 123
    monkeypatch.setattr("hallmark.downloader._fetch_file", fake_download_file)
    repo = SimpleNamespace(
        state=SimpleNamespace(
            config={
                "data": [{
                        "fmt": (
                            "{release}_{source}_{year}_{doy:03d}_{band}_"
                            "{pipeline}_{step}_{type}.uvfits")}],
                "remote": {"url": "https://example.com/data"},},
            data=pd.DataFrame([{
                        "sha1": "deadbeef",
                        "release": "SR1",
                        "source": "M87",
                        "year": "2017",
                        "doy": "95",
                        "band": "hi",
                        "pipeline": "hops",
                        "step": "netcal",
                        "type": "StokesI"}])))
    result = download_remote_data(repo, tmp_path, approved=True)

    assert result == {"succeeded": 1, "failed": 0, "total_bytes": 123, "errors": []},\
        f"Expected result {{'succeeded': 1, 'failed': 0, 'total_bytes': 123, \
            'errors': []}}, got {result}"
    assert captured["files"] == [(
            "https://example.com/data/SR1_M87_2017_095_hi_hops_netcal_StokesI.uvfits",
            tmp_path / "SR1_M87_2017_095_hi_hops_netcal_StokesI.uvfits",
            "deadbeef")], f"Expected captured files to contain the correct URL, \
                destination, and SHA1, but got {captured['files']}"


def test_download_remote_data_returns_empty_result_without_remote(tmp_path):
    """
    Test that download_remote_data returns an empty result when the repo has no remote
    configuration, indicating that there are no files to download.
    Args:
        tmp_path: A pytest fixture providing a temporary directory."""
    repo = _repo(tmp_path)

    assert download_remote_data(repo, tmp_path, approved=True) == {
        "succeeded": 0,
        "failed": 0,
        "total_bytes": 0,
        "errors": []}, f"Expected empty result for repo without remote, but got \
            {download_remote_data(repo, tmp_path, approved=True)}"


def test_download_remote_data_requires_remote_url(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when the repo has a remote
    config but the URL is missing, indicating that the remote is not properly set up.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the remote configuration is present but the URL is missing."""
    repo = _repo(tmp_path, {"remote": {"name": "origin"}})

    with pytest.raises(DownloadError, match="Remote URL not configured"):
        download_remote_data(repo, tmp_path, selected_files=[], approved=True)


def test_download_remote_data_returns_empty_result_for_empty_selection(tmp_path):
    """
    Test that download_remote_data returns an empty result when no files are selected
    for download, even if the remote is configured.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path,
        {"remote": {"name": "origin", "url": "https://example.test/data"}})
    result = download_remote_data(repo, tmp_path, selected_files=[], approved=True)

    assert result["succeeded"] == result["failed"] == result["total_bytes"] == 0, \
        f"Expected all counts to be 0, but got {result}"
    assert result["errors"] == [], f"Expected no errors, but got {result['errors']}"


def test_download_remote_data_aggregates_successes_and_failures(monkeypatch, tmp_path):
    """
    Test that download_remote_data correctly aggregates the number of successful and
    failed downloads, the total bytes downloaded, and any error messages when
    downloading multiple files.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If any of the downloads fail, the error is captured in the result
    """
    repo = _repo(
        tmp_path,
        {
            "remote": [
                {"name": "origin", "url": "https://origin.test/data"},
                {"name": "mirror", "url": "https://mirror.test/base/"}]})
    calls = []
    def fake_download(context, relative_path, destination, sha1, chunk_size):
        url = context.remote.file_url(relative_path.as_posix())
        """A fake _download_file function that records its arguments and
        simulates a failure for a specific file"""
        calls.append((url, destination, sha1))
        if destination.name == "bad.bin":
            raise DownloadError("bad download")
        return 7
    class Progress:
        """A simple progress tracker to simulate tqdm behavior for testing."""
        def __init__(self):
            self.updates = 0
            self.closed = False

        def update(self, amount):
            self.updates += amount

        def close(self):
            self.closed = True
    progress = Progress()
    monkeypatch.setattr("hallmark.downloader._fetch_file", fake_download)
    monkeypatch.setattr("hallmark.downloader.tqdm", lambda **kwargs: progress)
    result = download_remote_data(
        repo,
        tmp_path,
        max_workers=2,
        show_progress=True,
        selected_files=[
            (Path("nested/good.bin"), "good-sha"),
            (Path("bad.bin"), None)],
        remote_name="mirror", approved=True)

    assert result == {
        "succeeded": 1,
        "failed": 1,
        "total_bytes": 7,
        "errors": ["bad download"]}, f"Expected result {{'succeeded': 1, 'failed': 1, \
            'total_bytes': 7, 'errors': ['bad download']}}, but got {result}"
    assert set(calls) == {
        (
            "https://mirror.test/base/nested/good.bin",
            tmp_path / "nested/good.bin",
            "good-sha"),
        (
            "https://mirror.test/base/bad.bin",
            tmp_path / "bad.bin",
            None,)}, f"Expected calls to _download_file to match the selected files, \
                but got {calls}"
    assert progress.updates == 2, f"Expected 2 progress updates, got {progress.updates}"
    assert progress.closed, "Expected progress to be closed, but it was not"


def test_download_remote_data_revalidates_selected_paths(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when a selected file path
    is outside the intended download directory, even if it was previously selected.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If a selected file path is unsafe or outside intended directory.
        """
    repo = _repo(
        tmp_path,
        {"remote": {"name": "origin", "url": "https://example.test/data"}})

    with pytest.raises(DownloadError, match="must be a safe relative path"):
        download_remote_data(
            repo,
            tmp_path,
            selected_files=[(Path("../escape.bin"), None)], approved=True)

def test_download_remote_data_rejects_symlink_parent_escape(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when a selected file path
    is a symbolic link that points outside the intended download directory, preventing
    potential directory traversal attacks.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If a selected file path is a symbolic link pointing outside the
        download directory."""
    output_root = tmp_path / "downloads"
    outside_root = tmp_path / "outside"
    output_root.mkdir()
    outside_root.mkdir()
    symlink = output_root / "linked"
    try:
        symlink.symlink_to(
            outside_root,
            target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")
    repo = _repo(
        tmp_path,{
            "remote": {
                "name": "origin",
                "url": "https://example.test/data"}})

    with pytest.raises(DownloadError, match="symbolic link"):
        download_remote_data(
            repo,
            output_root,
            selected_files=[(Path("linked/file.bin"), None)], approved=True)
    assert not (outside_root / "file.bin").exists(), \
        "Expected no file to be created outside the download root, but it exists"


def test_download_remote_data_rejects_symlink_destination(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when a selected file path
    is a symbolic link that points to an existing file outside the intended download
    directory, preventing potential overwriting of important files.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If a selected file path is a symbolic link pointing to an
        existing file outside the download directory.
    """
    output_root = tmp_path / "downloads"
    outside_root = tmp_path / "outside"
    output_root.mkdir()
    outside_root.mkdir()
    outside_file = outside_root / "existing.bin"
    outside_file.write_bytes(b"do not overwrite")
    destination = output_root / "file.bin"
    try:
        destination.symlink_to(outside_file)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")
    repo = _repo(
        tmp_path,{
            "remote": {
                "name": "origin",
                "url": "https://example.test/data"}})

    with pytest.raises(DownloadError, match="symbolic link"):
        download_remote_data(
            repo,
            output_root,
            selected_files=[(Path("file.bin"), None)], approved=True)
    assert outside_file.read_bytes() == b"do not overwrite", \
        "Expected the outside file to remain unchanged, but it was modified"


def test_download_remote_data_deduplicates_selected_paths(monkeypatch, tmp_path):
    """
    Test that download_remote_data deduplicates selected file paths, ensuring that
    the same file is not downloaded multiple times even if it appears multiple times in
    the selection list, and that the checksum is correctly applied.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path,{
            "remote": {
                "name": "origin",
                "url": "https://example.test/data"}})
    calls = []
    def fake_download(context, relative_path, destination, checksum, chunk_size):
        url = context.remote.file_url(relative_path.as_posix())
        """A fake _download_file function that records its arguments and
        simulates a download."""
        calls.append((url, destination, checksum))
        return 4
    monkeypatch.setattr("hallmark.downloader._fetch_file", fake_download)
    result = download_remote_data(
        repo,
        tmp_path,
        selected_files=[
            (Path("file.bin"), None),
            (Path("file.bin"), "abc123"),
            (Path("file.bin"), "abc123")], approved=True)

    assert result["succeeded"] == 1, \
        f"Expected succeeded to be 1, but got {result['succeeded']}"
    assert result["total_bytes"] == 4, \
        f"Expected total_bytes to be 4, but got {result['total_bytes']}"
    assert calls == [
        ("https://example.test/data/file.bin", tmp_path / "file.bin", "abc123")], \
        f"Expected a single download call with the correct checksum, but got {calls}"


def test_download_remote_data_rejects_conflicting_checksums(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when the same file path is
    selected with different checksums, indicating conflict in the expected file content.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the same file path is selected with conflicting checksums.
    """
    repo = _repo(
        tmp_path, {"remote": {"name": "origin", "url": "https://example.test/data"}})

    with pytest.raises(DownloadError, match="Conflicting checksums"):
        download_remote_data(
            repo,
            tmp_path,
            selected_files=[(Path("file.bin"), "first"), (Path("file.bin"), "second")],
            approved=True)


@pytest.mark.parametrize("max_workers", [0, -1, 1.5, True, None])
def test_download_remote_data_rejects_invalid_worker_count(max_workers, tmp_path):
    """
    Test that download_remote_data raises a DownloadError when the max_workers parameter
    is not a positive integer, ensuring the function enforces valid concurrency settings
    Args:
        max_workers: The value to test for the max_workers parameter.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If max_workers is not a positive integer.
    """
    repo = _repo(tmp_path)

    with pytest.raises(
        DownloadError, match="max_workers must be a positive integer"):
        download_remote_data(repo, tmp_path, max_workers=max_workers, approved=True)


def test_download_remote_data_rejects_selected_files_without_remote(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when selected files are
    provided but the repo has no remote configuration, indicating that there is no
    source from which to download the files.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If selected files are provided but no remote is configured.
    """
    repo = _repo(tmp_path)

    with pytest.raises(DownloadError, match="No remote is configured"):
        download_remote_data(repo, tmp_path,
                             selected_files=[(Path("data.bin"), None)], approved=True)


def test_download_remote_data_reuses_session_per_worker(monkeypatch, tmp_path):
    """
    Test that download_remote_data reuses a single requests.Session per worker thread,
    ensuring that multiple downloads in the same thread share the same session for
    efficiency and connection pooling.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    """
    sessions = []

    class FakeSession:
        """A fake requests.Session that records the URLs and simulates a response."""
        def __init__(self):
            self.urls = []
            sessions.append(self)
        def close(self):
            self.closed = True
        def get(self, url, **kwargs):
            self.urls.append(url)
            return _Response([b"contents"])

    monkeypatch.setattr("hallmark.downloader.requests.Session", FakeSession)
    repo = _repo(
        tmp_path,{
            "remote": {"name": "origin", "url": "https://example.test/data"}})
    result = download_remote_data(
        repo,
        tmp_path,
        selected_files=[
            (Path("first.dat"), None), (Path("second.dat"), None)],
        max_workers=1, approved=True)

    assert result["succeeded"] == 2, \
        f"Expected 2 successful downloads, but got {result['succeeded']}"
    assert sessions[0].closed
    assert len(sessions) == 1, f"Expected a single session to be reused for both \
        downloads, but got {len(sessions)}"
    assert sessions[0].urls == [
        "https://example.test/data/first.dat", "https://example.test/data/second.dat"],\
        f"Expected a single session to be reused for both downloads, \
            but got {sessions[0].urls}"


def test_download_remote_data_rejects_malformed_selection(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when the selected_files
    parameter contains entries that are not tuples of (path, checksum), ensuring that
    the function enforces the expected structure for file selection.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If selected_files contains entries that are not tuples of
        (path, checksum).
    """
    repo = _repo(tmp_path,{"remote": {"name": "origin",
                                      "url": "https://example.test/data",}})

    with pytest.raises(DownloadError, match="path, checksum"):
        download_remote_data(
            repo, tmp_path, selected_files=[("file.dat", None, "extra")], approved=True)


def test_download_remote_data_rejects_malformed_checksum_tuple(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when a selected file's
    checksum specification is itself a tuple with the wrong number of elements
    (valid entries are either a plain sha1 string or an (algorithm, checksum) pair).
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If a checksum specification is a tuple of the wrong length.
    """
    repo = _repo(tmp_path, {"remote": {"name": "origin",
                                       "url": "https://example.test/data",}})

    with pytest.raises(DownloadError, match="Invalid checksum specification"):
        download_remote_data(
            repo,
            tmp_path,
            selected_files=[(Path("file.dat"), ("sha1", "abc123", "extra"))],
            approved=True)


def test_download_remote_data_rejects_output_path_that_is_file(tmp_path):
    """
    Test that download_remote_data raises a DownloadError when worktree_path exists but
    is a file instead of a directory.
    """
    repo = _repo(
        tmp_path,
        {"remote": {"name": "origin", "url": "https://example.test/data"}})
    output_path = tmp_path / "output.bin"
    output_path.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(DownloadError, match="not a directory"):
        download_remote_data(
            repo,
            output_path,
            selected_files=[(Path("file.bin"), None)], approved=True)


def test_download_remote_data_deduplicates_equivalent_checksum_tuples(
    monkeypatch, tmp_path):
    """
    Test that download_remote_data treats checksum tuples as equivalent when algorithm
    and checksum differ only by case, avoiding false conflicts and duplicate downloads.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path, {
            "remote": {
                "name": "origin",
                "url": "https://example.test/data"}})
    calls = []
    def fake_download(context, relative_path, destination, checksum, chunk_size):
        url = context.remote.file_url(relative_path.as_posix())
        """Record download requests and return a fixed byte count."""
        calls.append((url, destination, checksum))
        return 11
    monkeypatch.setattr("hallmark.downloader._fetch_file", fake_download)
    checksum_upper = ("SHA256", "A" * 64)
    checksum_lower = ("sha256", "a" * 64)
    result = download_remote_data(
        repo,
        tmp_path,
        selected_files=[
            (Path("file.bin"), checksum_upper),
            (Path("file.bin"), checksum_lower)], approved=True)

    assert result["succeeded"] == 1, \
        f"Expected succeeded to be 1, but got {result['succeeded']}"
    assert result["total_bytes"] == 11, \
        f"Expected total_bytes to be 11, but got {result['total_bytes']}"
    assert calls == [("https://example.test/data/file.bin", tmp_path / "file.bin",
                      checksum_upper)], \
                        f"Expected one deduplicated download call, got {calls}"


### select_download_files tests ###

def test_select_download_files_combines_explicit_and_tsv_and_upgrades_checksum(
    tmp_path):
    """
    Test that select_download_files correctly combines explicitly requested file paths
    with files listed in a TSV, and upgrades the checksum for files that are explicitly
    requested, ensuring selection includes all relevant files with correct checksums.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "{name}.fits", "db": "data.tsv"}]})
    pd.DataFrame(
        [
            {"path": "same.fits", "sha1": "abc123"},
            {"path": "other.fits", "sha1": "unknown"},
        ]).to_csv(repo.dothm.path / "data.tsv", sep="\t", index=False)
    selected = select_download_files(
        repo,
        file_paths=["same.fits"],
        tsv_names=["data", "data.tsv"])

    assert selected == [
        (Path("same.fits"), "abc123"),
        (Path("other.fits"), None)], \
            f"Expected combined selection with upgraded checksum, but got {selected}"


def test_select_download_files_all_includes_tsv_static_and_meta(tmp_path):
    """
    Test that select_download_files returns all files from the TSV, static files, and
    meta files when the all_files parameter is set to True, ensuring comprehensive
    selection of files for download.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path,
        {
            "data": [
                {"fmt": "{name}.fits", "db": "science.tsv"},
                {"file": "README.md", "sha1": "readme-sha"},
                "ignored"],
            "meta": {"file": "meta.yml"}})
    pd.DataFrame(
        [{"path": "nested/image.fits", "sha1": "image-sha"}]
    ).to_csv(repo.dothm.path / "science.tsv", sep="\t", index=False)
    selected = dict(
        (path.as_posix(), sha1)
        for path, sha1 in select_download_files(repo, all_files=True))

    assert selected == {
        "nested/image.fits": "image-sha",
        "README.md": "readme-sha",
        "meta.yml": None}, f"Expected all files to be selected, but got {selected}"


@pytest.mark.parametrize("all_files", [False, True])
def test_select_download_files_supports_legacy_state_data(tmp_path, all_files):
    """
    Test that select_download_files correctly selects files from legacy state data
    when the repo's configuration does not include a data format. This ensures backward
    compatibility with older versions of the repo that used a different data structure.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
        all_files (bool): Whether to select all files or just the legacy ones.
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "legacy_{index}.dat"}]},
        pd.DataFrame([{"path": "legacy_1.dat", "sha1": "legacy-sha"}]))
    selected = select_download_files(repo, all_files=all_files)

    assert selected == [(Path("legacy_1.dat"), "legacy-sha")], \
        f"Expected legacy file to be selected, but got {selected}"


@pytest.mark.parametrize("name", ["", "../data", "nested/data", r"nested\data"])
def test_select_download_files_rejects_invalid_tsv_names(tmp_path, name):
    """
    Test that select_download_files raises a DownloadError when an invalid TSV name is
    provided, such as an empty string or a path that traverses outside the intended dir.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the provided TSV name is invalid or unsafe.
    """
    repo = _repo(tmp_path)

    with pytest.raises(DownloadError):
        select_download_files(repo, tsv_names=[name])


def test_select_download_files_rejects_unconfigured_tsv(tmp_path):
    """
    Test that select_download_files raises a DownloadError when a TSV file is requested
    that is not configured in the repo's data configuration.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the requested TSV file is not configured in the repo.
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "{name}.fits", "db": "science.tsv"}]})

    with pytest.raises(DownloadError, match="TSV 'missing.tsv' is not configured"):
        select_download_files(repo, tsv_names=["missing"])


def test_select_download_files_rejects_missing_configured_tsv(tmp_path):
    """
    Test that select_download_files raises a DownloadError when a TSV file is
    configured in the repo's data configuration but does not exist in the .hm directory.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the configured TSV file does not exist in the .hm directory.
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "{name}.fits", "db": "science.tsv"}]})

    with pytest.raises(DownloadError, match="Configured TSV does not exist"):
        select_download_files(repo, tsv_names=["science"])


def test_select_download_files_ignores_empty_tsv(tmp_path):
    """
    Test that select_download_files returns an empty list when a configured TSV file
    exists but contains no data rows, indicating that there are no files to download.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "{name}.fits", "db": "science.tsv"}]})
    (repo.dothm.path / "science.tsv").write_text("", encoding="utf-8")

    assert select_download_files(repo, tsv_names=["science"]) == [], \
        f"Expected empty list for empty TSV, but got \
            {select_download_files(repo, tsv_names=['science'])}"


def test_select_download_files_wraps_tsv_parser_errors(monkeypatch, tmp_path):
    """
    Test that select_download_files raises a DownloadError when pandas raises a
    ParserError while reading a TSV file, simulating a corrupted or malformed TSV.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If pandas raises a ParserError while reading the TSV.
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "{name}.fits", "db": "science.tsv"}]},)
    (repo.dothm.path / "science.tsv").write_text("path\n", encoding="utf-8")

    def bad_read(*args, **kwargs):
        """fake pd.read_csv function that raises a ParserError to simulate bad TSV."""
        raise pd.errors.ParserError("bad table")
    monkeypatch.setattr("hallmark.downloader.pd.read_csv", bad_read)

    with pytest.raises(DownloadError, match="Unable to read TSV"):
        select_download_files(repo, tsv_names=["science"])


def test_select_download_files_preserves_uppercase_tsv_suffix(tmp_path):
    """
    Test that select_download_files correctly handles TSV files with uppercase suffixes,
    ensuring that the selection process is case-insensitive and still retrieves the
    expected files.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(tmp_path, {"data": [{"fmt": "{name}.fits", "db": "SCIENCE.TSV"}]})
    pd.DataFrame([{"path": "image.fits"}]
    ).to_csv(repo.dothm.path / "SCIENCE.TSV", sep="\t", index=False)
    selected = select_download_files(repo, tsv_names=["SCIENCE.TSV"])

    assert selected == [(Path("image.fits"), None)], \
        f"Expected file from uppercase TSV to be selected, but got {selected}"


def test_select_download_files_preserves_default_pandas_na_tokens(tmp_path):
    """
    Test that select_download_files correctly interprets default pandas NA tokens
    (such as 'NA', 'None', and 'null') in the TSV file, ensuring that these values are
    treated as valid sources for constructing file paths.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path,{
            "data": [{
                "fmt": "data/{source}.fits",
                "db": "data.tsv"}]})
    (repo.dothm.path / "data.tsv").write_text(
        "sha1\tsource\n"
        f"{'a' * 40}\tNA\n"
        f"{'b' * 40}\tNone\n"
        f"{'c' * 40}\tnull\n",
        encoding="utf-8")
    selected = select_download_files(repo, tsv_names=["data.tsv"])

    assert [path.as_posix() for path, _ in selected] == [
        "data/NA.fits", "data/None.fits", "data/null.fits"], f"Expected files with \
            default pandas NA tokens to be selected, but got {selected}"


def test_select_download_files_reads_tsv_in_chunks(monkeypatch, tmp_path):
    """
    Test that select_download_files reads a TSV file in chunks when the file is large,
    ensuring that the function can handle large TSV files without loading the entire
    file into memory at once.
    Args:
        monkeypatch: A pytest fixture for creating fake functions or objects.
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path, {"data": [{"fmt": "data/{source}.fits", "db": "data.tsv"}]})
    (repo.dothm.path / "data.tsv").write_text("placeholder\n", encoding="utf-8")
    captured = {}
    def fake_read_csv(path, **kwargs):
        """A fake pd.read_csv function that records its arguments and returns
        two chunks of data to simulate reading a TSV in chunks."""
        captured["path"] = path
        captured["kwargs"] = kwargs
        return iter([
            pd.DataFrame([{"sha1": "a" * 40, "source": "M87"}]),
            pd.DataFrame([{"sha1": "b" * 40, "source": "SGRA"}])])
    monkeypatch.setattr("hallmark.downloader.pd.read_csv", fake_read_csv)
    selected = select_download_files(repo, tsv_names=["data.tsv"])

    assert captured["path"] == repo.dothm.path / "data.tsv", f"Expected TSV path to be \
        {repo.dothm.path / 'data.tsv'}, but got {captured['path']}"
    assert captured["kwargs"]["chunksize"] == TSV_READ_CHUNK_SIZE, f"Expected chunksize\
          {TSV_READ_CHUNK_SIZE}, but got {captured['kwargs']['chunksize']}"
    assert [path.as_posix() for path, _ in selected] == [
        "data/M87.fits", "data/SGRA.fits"], \
            f"Expected files from chunked TSV to be selected, but got {selected}"


def test_select_download_files_rejects_conflicting_checksums_for_same_path(tmp_path):
    """
    Test that select_download_files raises DownloadError when a TSV contains the same
    file path with conflicting checksums.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the TSV contains the same file path with conflicting checksums
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "{name}.fits", "db": "science.tsv"}]})
    pd.DataFrame([
        {"path": "same.fits", "sha1": "a" * 40},
        {"path": "same.fits", "sha1": "b" * 40},
    ]).to_csv(repo.dothm.path / "science.tsv", sep="\t", index=False)

    with pytest.raises(DownloadError, match="Conflicting checksums"):
        select_download_files(repo, tsv_names=["science"])


def test_select_download_files_rejects_unsupported_tsv_checksum_algorithm(tmp_path):
    """
    Test that select_download_files raises DownloadError when a TSV row includes an
    unsupported checksum algorithm.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If a TSV row includes an unsupported checksum algorithm.
    """
    repo = _repo(
        tmp_path,
        {"data": [{"fmt": "{name}.fits", "db": "science.tsv"}]})
    pd.DataFrame([{
        "path": "image.fits",
        "checksum_algorithm": "sha999",
        "checksum": "a" * 64,
    }]).to_csv(repo.dothm.path / "science.tsv", sep="\t", index=False)

    with pytest.raises(DownloadError, match="Unsupported checksum algorithm"):
        select_download_files(repo, tsv_names=["science"])


def test_select_download_files_rejects_unsupported_static_checksum_algorithm(tmp_path):
    """
    Test that select_download_files raises DownloadError when a static config entry
    provides an unsupported checksum algorithm.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the static config entry has an unsupported checksum algorithm.
    """
    repo = _repo(tmp_path,
                 {"data": [{
                     "file": "README.md",
                     "checksum_algorithm": "sha999",
                     "checksum": "a" * 64}]},
                 pd.DataFrame())

    with pytest.raises(DownloadError, match="Unsupported checksum algorithm"):
        select_download_files(repo, all_files=True)


def test_select_download_files_uses_builder_checksums(tmp_path):
    """
    Test that select_download_files correctly includes the checksum information for
    files when the checksum is provided in the TSV or static file configuration,
    ensuring that the selected files have the expected checksum algorithms and values.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    content = b"science"
    sha256 = hashlib.sha256(content).hexdigest()
    readme_md5 = hashlib.md5(b"readme").hexdigest()
    repo = _repo(
        tmp_path,{
            "data": [{
                    "fmt": "{name}.fits",
                    "db": "science.tsv"},
                {
                    "file": "README.md",
                    "md5": readme_md5}]})
    pd.DataFrame([{
                "path": "image.fits",
                "checksum_algorithm": "sha256",
                "checksum": sha256,
            }]).to_csv(repo.dothm.path / "science.tsv", sep="\t", index=False)
    selected = dict(select_download_files(repo, all_files=True))

    assert selected == {
        Path("image.fits"): ("sha256", sha256),
        Path("README.md"): ("md5", readme_md5)}, \
            f"Expected selected files to include checksums, but got {selected}"


def test_select_download_files_accepts_single_data_mapping(tmp_path):
    """
    Test that select_download_files correctly selects files when the repo has a single
    data mapping, ensuring that the function can handle simple configurations without
    requiring multiple data mappings.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(
        tmp_path,{
            "data": {
                "fmt": "{name}.fits",
                "db": "data.tsv"}})
    pd.DataFrame([{
            "path": "image.fits",
            "sha1": "abc123",
        }]).to_csv(repo.dothm.path / "data.tsv", sep="\t", index=False)
    selected = select_download_files(repo, tsv_names=["data.tsv"])

    assert selected == [(Path("image.fits"), "abc123")], f"Expected selected files to \
        include the file from the single data mapping, but got {selected}"


@pytest.mark.parametrize(
    "db_name",
    ["../outside.tsv", "nested/data.tsv", r"nested\data.tsv", "/absolute.tsv"])
def test_select_download_files_rejects_invalid_configured_tsv(tmp_path, db_name):
    """
    Test that select_download_files raises a DownloadError when a configured TSV file
    has an invalid path, such as one that traverses outside the intended directory or is
      absolute, ensuring that the function enforces safe file paths for TSV configs.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
        db_name: The invalid TSV file name to test.
    Raises:
        DownloadError: If the configured TSV file path is invalid or unsafe.
    """
    repo = _repo(tmp_path, {"data": [{"fmt": "{name}.fits", "db": db_name}]})

    with pytest.raises(DownloadError):
        select_download_files(repo, all_files=True)


def test_select_download_files_normalizes_configured_tsv_name(tmp_path):
    """
    Test that select_download_files correctly normalizes the TSV name from the repo's
    configuration, allowing for selection of files even when the TSV name has extra
    whitespace or different casing, ensuring that the function can handle variations in
    TSV naming.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    """
    repo = _repo(tmp_path, {"data": [{"fmt": "{name}.fits", "db": " science "}]})
    pd.DataFrame([
        {"name": "image"}]).to_csv(
            repo.dothm.path / "science.tsv", sep="\t", index=False)
    selected = select_download_files(repo, tsv_names=["science"])

    assert selected == [(Path("image.fits"), None)], f"Expected selected files to \
        include the file from the normalized TSV name, but got {selected}"


def test_select_download_files_rejects_nonmapping_config(tmp_path):
    """
    Test that select_download_files raises a DownloadError when the repo's configuration
    is not a mapping (e.g., a list or other type), ensuring that the function enforces
    the expected structure for the configuration data.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the repo's configuration is not a mapping.
    """
    repo = _repo(tmp_path, config=["invalid"])

    with pytest.raises(DownloadError, match="expected a mapping"):
        select_download_files(repo)


### _download_file tests ###

def test_download_file_preserves_existing_part_file(monkeypatch, tmp_path):
    """
    Test that _download_file preserves an existing .part file when downloading a new
    file, ensuring that the existing partial download is not overwritten or deleted.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    """
    monkeypatch.setattr(
        "hallmark.downloader.requests.get",
        lambda *args, **kwargs: _Response([b"downloaded"]))

    destination = tmp_path / "file.bin"
    existing_part = tmp_path / "file.bin.part"
    existing_part.write_bytes(b"keep this")
    result = _download_file("https://example.test/file.bin", destination)

    assert result == len(b"downloaded"), \
        f"Expected downloaded length {len(b'downloaded')}, but got {result}"
    assert destination.read_bytes() == b"downloaded", \
        "Expected destination file to contain downloaded data, but it did not"
    assert existing_part.read_bytes() == b"keep this", \
        "Expected existing .part file to be preserved, but it was modified"
    assert list(tmp_path.glob(".file.bin.*.part")) == [], \
        "Expected no leftover .part files, but found some"


@pytest.mark.parametrize("chunk_size", [0, -1, 1.5, True, None])
def test_download_file_rejects_invalid_chunk_size(chunk_size, tmp_path):
    """
    Test that _download_file raises a DownloadError when an invalid chunk_size is
    provided, ensuring that the function enforces the requirement for a positive integer
    chunk size for downloading files.
    Args:
        chunk_size: The invalid chunk size value to test.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the chunk_size is not a positive integer.
    """
    with pytest.raises(DownloadError, match="chunk_size must be a positive integer"):
        _download_file(
            "https://example.test/file.bin",
            tmp_path / "file.bin",
            chunk_size=chunk_size)


@pytest.mark.parametrize(
    "checksum", ["abc", "g" * 40, ("md5", "a" * 31), ("unsupported", "a" * 40)])
def test_download_file_rejects_invalid_checksum_before_request(monkeypatch, tmp_path,
                                                               checksum):
    """
    Test that _download_file raises a DownloadError when an invalid checksum is provided
    and that it does not attempt to make an HTTP request when the checksum is invalid.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
        checksum: The invalid checksum value to test.
    Raises:
        AssertionError: If an HTTP request is attempted when the checksum is invalid.
        DownloadError: If the checksum is invalid or unsupported."""
    def unexpected_request(*args, **kwargs):
        """
        A fake requests.get function that raises an AssertionError if called, to ensure
        that _download_file does not attempt to make an HTTP request when the checksum
        is invalid."""
        raise AssertionError("HTTP request should not run")
    monkeypatch.setattr("hallmark.downloader.requests.get", unexpected_request)
    destination = tmp_path / "file.bin"

    with pytest.raises(
        DownloadError,
        match=(
            "Invalid .* checksum"
            "|Unsupported checksum algorithm")):
        _download_file(
            "https://example.test/file.bin", destination, expected_checksum=checksum)
    assert not destination.exists(), \
        "Expected destination file not to exist after failed checksum validation"


def test_download_file_wraps_directory_creation_error(monkeypatch, tmp_path):
    """
    Test that _download_file raises a DownloadError when it fails to create the
    necessary directories for the destination file, simulating a failure in directory
    creation and ensuring that the function handles such errors gracefully.
    Args:
        monkeypatch: A pytest fixture for monkeypatching.
        tmp_path: A pytest fixture providing a temporary directory.
    Raises:
        DownloadError: If the directory for the destination file cannot be created.
    """
    destination = tmp_path / "nested" / "file.dat"
    def fail_mkdir(self, *args, **kwargs):
        """A fake Path.mkdir method that raises an OSError to simulate a failure in
        creating the directory for the destination file."""
        raise OSError("cannot create directory")
    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with pytest.raises(DownloadError, match="Failed to write"):
        _download_file("https://example.test/file.dat", destination)


### _remote_file_url tests ###

def test_remote_file_url_escapes_filename_characters():
    """
    Test that _remote_file_url correctly escapes special characters in the filename
    when constructing the full URL, ensuring that the resulting URL is valid and safe
    for HTTP requests.
    """
    result = _remote_file_url("https://example.test/base", Path("nested/a+b #1.dat"))

    assert result == ("https://example.test/base/nested/a%2Bb%20%231.dat"), \
        f"Expected URL to escape special characters, but got {result}"


### checksum verification tests ###

@pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha256", "sha512"])
def test_verify_checksum_supports_builder_algorithms(tmp_path, algorithm):
    """
    Test that _verify_validated_checksum correctly computes and verifies checksums
    using various algorithms supported by hashlib, ensuring that the function can
    handle different checksum types as specified in the repo's configuration.
    Args:
        tmp_path: A pytest fixture providing a temporary directory.
        algorithm (str): The name of the checksum algorithm to test.
    """
    content = b"hallmark checksum test"
    path = tmp_path / "data.bin"
    path.write_bytes(content)
    expected = hashlib.new(algorithm, content).hexdigest()

    _verify_validated_checksum(path, (algorithm, expected), chunk_size=2)


def test_entry_checksum_prefers_strongest_named_checksum():
    """
    Test that _entry_checksum correctly identifies and returns the strongest available
    checksum from a given entry, preferring stronger algorithms over weaker ones when
    multiple checksums are present.
    """
    entry = {"md5": "a" * 32, "sha1": "b" * 40, "sha256": "c" * 64, "sha512": "d" * 128}

    assert _entry_checksum(entry) == ("sha512", "d" * 128), \
        f"Expected strongest checksum to be sha512, but got {_entry_checksum(entry)}"
