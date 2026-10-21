"""Edge cases of planning, transferring and staging downloads."""

import errno
import hashlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from hallmark import Repo
from hallmark.remote import download as download_module
from hallmark.remote.download import (
    DownloadError,
    _checksum_pair,
    _select_download_items,
    _select_remote_config,
    execute_download_plan,
    plan_download,
)
from hallmark.remote.plan import DownloadItem, DownloadPlan
from hallmark.repo.objects import Objects
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing
from hallmark.transport.http import HttpBackend

ROOT = "https://example.test/data/"
FILES = {"a.fits": b"alpha", "b.fits": b"bravo"}


def sha1(data):
    return hashlib.sha1(data).hexdigest()


@pytest.fixture
def server(monkeypatch):
    """Serve FILES with their sizes in an index and record every payload fetch."""
    files = dict(FILES)
    fetched = []

    def read_text(context, path, **_):
        if path:
            raise RemoteObjectMissing("No metadata")
        return "<h1>Index of data</h1>" + "".join(
            f'<a href="{name}" data-size="{len(data)}">{name}</a>'
            for name, data in files.items())

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        fetched.append(relative_path)
        Path(destination).write_bytes(files[relative_path])

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    return files, fetched


@pytest.fixture
def repo(tmp_path, server):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT)
    repo.commit("Catalogue")
    return repo


def download(repo, **selection):
    plan = repo.plan_download(**(selection or {"all_files": True}))
    return plan, repo.download(plan, approved=True)


def fetch_hook(monkeypatch, before):
    """Run ``before(relative_path)`` ahead of each served fetch."""
    real_fetch = HttpBackend.fetch

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        before(relative_path)
        return real_fetch(self, relative_path, destination, chunk_size=chunk_size)

    monkeypatch.setattr(HttpBackend, "fetch", fetch)


def catalogue(tmp_path, rows=(), config=None, *, store=True):
    """A stand-in repository whose staged data.tsv holds ``rows``."""
    dothm = tmp_path / ".hm"
    dothm.mkdir()
    repo = SimpleNamespace(
        dothm=SimpleNamespace(path=dothm), worktree=tmp_path,
        state=SimpleNamespace(
            config={"data": [{"db": "data.tsv"}]} if config is None else config,
            data=pd.DataFrame(list(rows))))
    if store:
        repo.objects = Objects(dothm)
    return repo


def paths(items):
    return [item.relative_path.as_posix() for item in items]


# Selecting files from the catalogue


def test_a_repository_without_a_config_selects_its_catalogued_paths(tmp_path):
    repo = catalogue(tmp_path, [{"path": "a.fits"}, {"path": "nested/b.fits"}],
                     config=None)
    repo.state.config = None
    assert paths(_select_download_items(repo)) == ["a.fits", "nested/b.fits"]


def test_repeated_rows_merge_into_one_item_keeping_what_either_records(tmp_path):
    digest = hashlib.sha256(b"alpha").hexdigest()
    repo = catalogue(tmp_path, [
        {"path": "a.fits", "checksum_algorithm": "sha256", "checksum": digest,
         "size_bytes": "5"},
        {"path": "a.fits", "checksum_algorithm": "", "checksum": "",
         "size_bytes": ""},
        {"path": "b.fits", "checksum_algorithm": "", "checksum": "",
         "size_bytes": ""},
        {"path": "b.fits", "checksum_algorithm": "", "checksum": "",
         "size_bytes": "7"},
    ])
    items = {item.relative_path.as_posix(): item
             for item in _select_download_items(repo)}
    assert list(items) == ["a.fits", "b.fits"]
    assert items["a.fits"].checksum == ("sha256", digest)
    assert items["a.fits"].size_bytes == 5
    assert items["b.fits"].checksum is None
    assert items["b.fits"].size_bytes == 7


def test_rows_recording_different_sizes_for_one_path_are_refused(tmp_path):
    repo = catalogue(tmp_path, [{"path": "a.fits", "size_bytes": "4"},
                                {"path": "a.fits", "size_bytes": "5"}])
    with pytest.raises(DownloadError, match="Conflicting sizes for 'a.fits'"):
        _select_download_items(repo)


def test_rows_recording_different_sha1_for_one_path_are_refused(tmp_path):
    digest = hashlib.sha256(b"alpha").hexdigest()
    repo = catalogue(tmp_path, [
        {"path": "a.fits", "checksum_algorithm": "sha256", "checksum": digest,
         "sha1": "1" * 40},
        {"path": "a.fits", "checksum_algorithm": "sha256", "checksum": digest,
         "sha1": "2" * 40},
    ])
    with pytest.raises(DownloadError, match="Conflicting SHA-1 for 'a.fits'"):
        _select_download_items(repo)


@pytest.mark.parametrize("size", ["1.5", "-3", "Infinity", "NaN"])
def test_sizes_that_are_not_byte_counts_are_unknown(tmp_path, size):
    repo = catalogue(tmp_path, [{"path": "a.fits", "size_bytes": size}],
                     config={"data": [{"db": "data.tsv"}],
                             "remote": {"name": "origin", "url": ROOT}})
    plan = plan_download(repo)
    assert plan.items[0].size_bytes is None
    assert plan.unknown_size_count == 1 and plan.total_bytes is None


def test_a_tsv_entry_without_a_format_is_passed_over_when_building_paths(
        tmp_path):
    repo = catalogue(tmp_path, [{"name": "m87"}, {"name": "sgra"}], config={
        "data": [{"db": "data.tsv"}, {"db": "data.tsv", "fmt": "{name}.fits"}]})
    assert paths(_select_download_items(repo, tsv_names=["data.tsv"])) == [
        "m87.fits", "sgra.fits"]


def test_rows_that_no_format_can_name_are_refused(tmp_path):
    repo = catalogue(tmp_path, [{"name": "m87"}])
    with pytest.raises(DownloadError, match="Available columns: name"):
        _select_download_items(repo, tsv_names=["data.tsv"])


def test_a_checksum_specification_must_be_a_pair():
    assert _checksum_pair(None) is None
    assert _checksum_pair("ABC") == ("sha1", "abc")
    with pytest.raises(DownloadError, match="Invalid checksum specification"):
        _checksum_pair(("sha256", "a" * 64, "extra"))


@pytest.mark.parametrize("remote, message", [
    ({"name": "origin", "url": ROOT, "colour": "blue"}, "support only name, url"),
    ({"name": "origin", "url": ROOT, "auth": "../profile"}, "Auth profile must"),
])
def test_a_malformed_remote_entry_is_a_download_error(tmp_path, remote, message):
    repo = catalogue(tmp_path, config={"remote": [remote]})
    with pytest.raises(DownloadError, match=message):
        _select_remote_config(repo)


# Planning


@pytest.fixture
def legacy(tmp_path):
    """A catalogue written before sources: one remote, paths without SHA-1."""
    return catalogue(
        tmp_path, [{"path": "a.fits", "size_bytes": "5"},
                   {"path": "b.fits", "size_bytes": "5"}],
        config={"data": [{"db": "data.tsv"}],
                "remote": {"name": "origin", "url": ROOT}})


def test_a_download_into_a_file_is_refused(legacy, tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("not a folder")
    with pytest.raises(DownloadError, match="not a directory"):
        plan_download(legacy, target)
    assert target.read_text() == "not a folder"


def test_a_single_tsv_or_source_may_be_named_by_a_string(legacy):
    assert paths(plan_download(legacy, tsv_names="data.tsv").items) == [
        "a.fits", "b.fits"]
    assert paths(plan_download(legacy, sources="origin").items) == [
        "a.fits", "b.fits"]
    with pytest.raises(DownloadError, match="Unknown source"):
        plan_download(legacy, sources="mirror")


def test_all_files_cannot_be_combined_with_a_selection(legacy):
    with pytest.raises(DownloadError, match="cannot be combined"):
        plan_download(legacy, file_paths="a.fits", all_files=True)
    with pytest.raises(DownloadError, match="cannot be combined"):
        plan_download(legacy, tsv_names=["data.tsv"], all_files=True)


def test_a_malformed_sources_section_is_a_download_error(repo):
    repo.state.config["sources"] = "origin"
    with pytest.raises(DownloadError, match="sources must be a list"):
        repo.plan_download(all_files=True)


def test_a_source_whose_remote_is_not_configured_is_refused(tmp_path):
    repo = catalogue(
        tmp_path, [{"path": "a.fits", "catalog_source": "lab"}],
        config={"data": [{"db": "data.tsv"}],
                "sources": [{"name": "lab", "remote": "archive"}]})
    with pytest.raises(DownloadError,
                       match="Source 'lab' uses unconfigured remote 'archive'"):
        plan_download(repo)


def test_a_linked_download_whose_catalogued_size_changed_is_a_conflict(
        repo, server):
    download(repo)
    data = repo.state.data
    assert data.set_index("path").loc["a.fits", "sha1"] == sha1(b"alpha")
    data.loc[data.path == "a.fits", "size_bytes"] = "99"
    with pytest.raises(DownloadError, match=r"conflict[\s\S]*\na\.fits"):
        repo.plan_download(all_files=True)
    assert (repo.worktree / "a.fits").read_bytes() == b"alpha"


# Executing a plan


def test_only_a_download_plan_can_be_executed(repo):
    with pytest.raises(TypeError, match="plan must be a DownloadPlan"):
        execute_download_plan(repo, {"items": ["a.fits"]}, approved=True)


def test_an_empty_plan_needs_no_object_store(tmp_path):
    repo = catalogue(tmp_path, store=False)
    plan = DownloadPlan((), ROOT, tmp_path)
    assert execute_download_plan(repo, plan) == {
        "succeeded": 0, "failed": 0, "total_bytes": 0, "errors": []}


def test_a_plan_with_files_needs_an_object_store(tmp_path, server):
    repo = catalogue(tmp_path, store=False)
    plan = DownloadPlan([DownloadItem("a.fits")], ROOT, tmp_path)
    with pytest.raises(DownloadError, match="object store"):
        execute_download_plan(repo, plan, approved=True)
    assert server[1] == [] and not (tmp_path / "a.fits").exists()


@pytest.mark.parametrize("checksum, message", [
    (("crc32", "abcd1234"), "Unsupported checksum algorithm: 'crc32'"),
    (("sha256", "xyz"), "Invalid sha256 checksum: 'xyz'"),
])
def test_an_unusable_checksum_fails_its_file_before_any_transfer(
        repo, server, checksum, message):
    plan = DownloadPlan([DownloadItem("a.fits", checksum), DownloadItem("b.fits")],
                        ROOT, repo.worktree)
    result = execute_download_plan(repo, plan, approved=True)
    assert (result["succeeded"], result["failed"]) == (1, 1)
    assert result["errors"] == [message]
    assert server[1] == ["b.fits"]
    assert not (repo.worktree / "a.fits").exists()


def test_content_that_no_longer_matches_its_recorded_sha1_is_refused(
        repo, server):
    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    # The server's copy changed without the catalogue being updated.
    files["a.fits"] = b"ALPHA"
    (repo.worktree / "a.fits").unlink()
    repo.objects.discard(sha1(b"alpha"))
    fetched.clear()
    plan, result = download(repo)
    assert paths(plan.items) == ["a.fits"]
    assert fetched == ["a.fits"]
    assert result["failed"] == 1
    assert result["errors"] == ["Checksum mismatch for a.fits (sha1)"]
    assert not (repo.worktree / "a.fits").exists()
    assert not repo.objects.contains(sha1(b"ALPHA"))
    assert list(repo.objects.tmp.iterdir()) == []


def test_a_recorded_sha1_is_checked_even_when_the_checksum_matches(repo, server):
    data = repo.state.data
    data["checksum_algorithm"] = "sha256"
    data["checksum"] = [hashlib.sha256(FILES[path]).hexdigest()
                        for path in data.path]
    # An earlier download recorded a SHA-1 the served bytes no longer have.
    data["sha1"] = [sha1(b"stale"), sha1(b"bravo")]
    repo.dothm.save_state(repo.state)
    _, result = download(repo)
    assert (result["succeeded"], result["failed"]) == (1, 1)
    assert result["errors"] == ["Checksum mismatch for a.fits (sha1)"]
    assert not (repo.worktree / "a.fits").exists()
    assert (repo.worktree / "b.fits").read_bytes() == b"bravo"


def test_a_local_write_failure_is_reported_for_its_file(repo, server, monkeypatch):
    def fetch(self, relative_path, destination, *, chunk_size=8192):
        if relative_path == "a.fits":
            raise OSError(errno.ENOSPC, "No space left on device")
        Path(destination).write_bytes(FILES[relative_path])

    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    _, result = download(repo)
    assert (result["succeeded"], result["failed"]) == (1, 1)
    assert result["errors"] == [
        "Failed to download a.fits: [Errno 28] No space left on device"]
    assert not (repo.worktree / "a.fits").exists()
    assert (repo.worktree / "b.fits").read_bytes() == b"bravo"
    assert list(repo.objects.tmp.iterdir()) == []


def test_a_stored_copy_removed_while_downloading_is_reported(repo, server,
                                                             monkeypatch):
    files, fetched = server
    repo.state.data["sha1"] = [sha1(FILES[path]) for path in repo.state.data.path]
    repo.dothm.save_state(repo.state)
    download(repo)
    (repo.worktree / "b.fits").unlink()
    plan = repo.plan_download(all_files=True)
    assert paths(plan.stored_items) == ["b.fits"]
    real_check = download_module._check_existing_files

    def check_then_lose_object(items, output_root, store=None):
        found = real_check(items, output_root, store)
        # Another process clears the object after it was found.
        store.discard(sha1(b"bravo"))
        return found

    monkeypatch.setattr(download_module, "_check_existing_files",
                        check_then_lose_object)
    fetched.clear()
    result = repo.download(plan, approved=True)
    assert (result["succeeded"], result["failed"]) == (0, 1)
    assert result["errors"] == [
        "The stored copy of b.fits is missing; run hm download again"]
    assert fetched == [] and not (repo.worktree / "b.fits").exists()


def test_a_file_blocking_a_downloads_folder_is_kept(repo, server, monkeypatch):
    files, _ = server
    files["sub/deeper/c.fits"] = b"charlie"
    blocker = repo.worktree / "sub"
    fetch_hook(monkeypatch, lambda path: blocker.write_text("my notes"))
    plan = DownloadPlan([DownloadItem("sub/deeper/c.fits")], ROOT, repo.worktree)
    result = execute_download_plan(repo, plan, approved=True)
    assert result["failed"] == 1
    assert result["errors"][0].startswith("Failed to place sub/deeper/c.fits: ")
    assert blocker.read_text() == "my notes"
    # The verified bytes stay in the store for the next attempt.
    assert repo.objects.contains(sha1(b"charlie"))


def test_an_outdated_file_replaced_by_a_folder_during_the_download_is_kept(
        repo, server, monkeypatch):
    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT)
    path = repo.worktree / "b.fits"

    def replace_with_folder(relative_path):
        path.unlink()
        path.mkdir()
        (path / "notes.txt").write_text("keep")

    fetch_hook(monkeypatch, replace_with_folder)
    plan, result = download(repo)
    assert [item.relative_path.name for item in plan.outdated_items] == ["b.fits"]
    assert result["failed"] == 1
    assert result["errors"] == ["b.fits changed during the download; it was kept"]
    assert (path / "notes.txt").read_text() == "keep"


# Recording SHA-1s after a download


def test_a_catalogue_that_stops_being_remote_is_not_staged(repo, server,
                                                          monkeypatch):
    local = deepcopy(repo.state)
    del local.config["sources"]
    local.data["sha1"] = ""

    def save_local_catalogue(relative_path):
        if relative_path == "a.fits":
            repo.dothm.save_state(local)

    fetch_hook(monkeypatch, save_local_catalogue)
    _, result = download(repo)
    assert (result["succeeded"], result["failed"]) == (2, 0)
    assert "staged" not in result and "staging_error" not in result
    saved = Repo(repo.worktree).state
    assert "sources" not in saved.config
    assert saved.data["sha1"].fillna("").tolist() == ["", ""]


def test_rows_without_a_path_column_are_downloaded_but_not_staged(repo, server):
    files, _ = server
    files["x1.dat"] = b"x-one"
    repo.state.config["data"].append({"db": "extra.tsv", "fmt": "x{i}.dat"})
    repo.state.tables["extra.tsv"] = pd.DataFrame({"i": ["1"]})
    repo.dothm.save_state(repo.state)
    plan, result = download(repo, tsv_names=["data.tsv", "extra.tsv"])
    assert paths(plan.items) == ["a.fits", "b.fits", "x1.dat"]
    assert (result["succeeded"], result["staged"]) == (3, 2)
    assert (repo.worktree / "x1.dat").read_bytes() == b"x-one"
    saved = Repo(repo.worktree).state
    assert saved.tables["extra.tsv"].columns.tolist() == ["i"]
    assert saved.data.set_index("path")["sha1"].to_dict() == {
        "a.fits": sha1(b"alpha"), "b.fits": sha1(b"bravo")}


def test_a_file_entry_with_an_unsafe_path_is_left_alone_when_staging(repo,
                                                                    server):
    repo.state.config["data"].append({"file": "../outside.txt"})
    repo.dothm.save_state(repo.state)
    _, result = download(repo, tsv_names=["data.tsv"])
    assert (result["succeeded"], result["staged"]) == (2, 2)
    saved = Repo(repo.worktree).state.config["data"]
    assert {"file": "../outside.txt"} in saved
    assert not (repo.worktree.parent / "outside.txt").exists()
