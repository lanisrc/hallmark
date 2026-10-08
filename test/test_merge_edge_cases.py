"""Regression cases for the combined staging, branch, and author features."""

from pathlib import Path

import pandas as pd
import pytest

from hallmark import Repo
from hallmark.error import CheckoutError
from hallmark.repo.manifest import file_versions_by_path, iter_manifest_entries
from hallmark.repo.state import State


FMT = "data_{number}.txt"
CHECKSUM = "a" * 40


def _committed_repo(tmp_path, names=("data_1.txt", "data_2.txt")):
    repo = Repo.init(tmp_path / "repo")
    for name in names:
        (repo.worktree / name).write_text(f"original {name}\n")
    repo.add(FMT)
    repo.commit("Original dataset")
    return repo


@pytest.mark.parametrize("stored_path", [
    pytest.param(None, marks=pytest.mark.xfail(
        strict=True, raises=TypeError,
        reason="Legacy rows without a path or pattern crash path formatting")),
    "nested/data_1.txt",
])
def test_manifest_without_format_handles_legacy_and_explicit_paths(stored_path):
    row = {"sha1": CHECKSUM, "number": "1"}
    if stored_path is not None:
        row["path"] = stored_path
    state = State(data=pd.DataFrame([row]))
    expected = [] if stored_path is None else [(Path(stored_path), CHECKSUM)]

    assert list(iter_manifest_entries(state)) == expected
    assert file_versions_by_path(state) == {
        path.as_posix(): checksum for path, checksum in expected}


@pytest.mark.xfail(
    strict=True, raises=TypeError,
    reason="Mixed legacy and explicit-path rows need a missing-pattern fallback")
@pytest.mark.parametrize("explicit_first", [False, True])
def test_manifest_without_format_keeps_explicit_rows_among_legacy_rows(
        explicit_first):
    rows = [
        {"sha1": CHECKSUM, "number": "1"},
        {"sha1": "b" * 40, "number": "2", "path": "nested/data_2.txt"},
    ]
    if explicit_first:
        rows.reverse()
    state = State(data=pd.DataFrame(rows))

    assert list(iter_manifest_entries(state)) == [
        (Path("nested/data_2.txt"), "b" * 40)]
    assert file_versions_by_path(state) == {"nested/data_2.txt": "b" * 40}


@pytest.mark.parametrize("direction", [
    pytest.param("file_to_directory", marks=pytest.mark.xfail(
        strict=True, raises=FileExistsError,
        reason="Old version metadata blocks staging a nested replacement file")),
    pytest.param("directory_to_file", marks=pytest.mark.xfail(
        strict=True, raises=IsADirectoryError,
        reason="Old version metadata directory blocks staging its parent path")),
])
def test_rescan_handles_file_directory_transitions(tmp_path, direction):
    repo = Repo.init(tmp_path / "repo")
    parent = repo.worktree / "data_1.txt"
    nested = parent / "data_2.txt"
    if direction == "file_to_directory":
        parent.write_text("original\n")
    else:
        parent.mkdir()
        nested.write_text("original\n")
    repo.add(FMT)
    repo.commit("Original path shape")

    if direction == "file_to_directory":
        parent.unlink()
        parent.mkdir()
        nested.write_text("replacement\n")
        expected_path = "data_1.txt/data_2.txt"
    else:
        nested.unlink()
        parent.rmdir()
        parent.write_text("replacement\n")
        expected_path = "data_1.txt"

    repo.add(".")
    repo.commit("Replacement path shape")

    reopened = Repo(repo.worktree)
    assert file_versions_by_path(reopened.state) == {
        expected_path: Repo.checksum(repo.worktree / expected_path)}
    assert not reopened.dothm.index.diff("HEAD")


def test_partial_unstage_preserves_author_check_and_other_staged_files(
        tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path)
    original = file_versions_by_path(repo.state)
    for name in original:
        (repo.worktree / name).write_text(f"modified {name}\n")
    repo.add_paths(list(original))
    repo.restore_staged(["data_1.txt"])
    head_before = repo.dothm.head.commit.hexsha
    index_before = repo.dothm.index.entries.copy()

    with monkeypatch.context() as patch:
        patch.setattr(repo.dothm, "effective_identity", lambda: (None, None))
        with pytest.raises(RuntimeError, match="Set your name and email first"):
            repo.commit("Must refuse missing author")

    assert repo.dothm.head.commit.hexsha == head_before
    assert repo.dothm.index.entries == index_before
    assert repo.commit("Commit only the remaining staged file")
    committed = file_versions_by_path(repo.dothm.load_state("HEAD"))
    assert committed["data_1.txt"] == original["data_1.txt"]
    assert committed["data_2.txt"] == Repo.checksum(
        repo.worktree / "data_2.txt")
    assert repo.status()["worktree"]["modified"] == [
        ".hm/data.tsv", "data_1.txt"]


def test_unstaging_while_detached_preserves_files_and_commit_guard(tmp_path):
    repo = _committed_repo(tmp_path, names=("data_1.txt",))
    first_commit = repo.dothm.head.commit.hexsha
    path = repo.worktree / "data_1.txt"
    path.write_text("second commit\n")
    repo.add_paths([path])
    repo.commit("Second dataset")
    repo.checkout(first_commit)
    path.write_text("uncommitted detached change\n")
    repo.add_paths([path])
    repo.restore_staged([path.as_posix()])

    with pytest.raises(RuntimeError, match="not on a branch"):
        repo.commit("Must refuse detached commit")

    assert repo.dothm.head.commit.hexsha == first_commit
    assert repo.branches()["current"] is None
    assert not repo.dothm.index.diff("HEAD")
    assert path.read_text() == "uncommitted detached change\n"
    assert repo.status()["worktree"]["modified"] == [
        ".hm/data.tsv", "data_1.txt"]


def test_removed_version_metadata_allows_branch_round_trip(tmp_path):
    repo = _committed_repo(tmp_path)
    repo.create_branch("experiment")
    repo.checkout("experiment")
    removed = repo.worktree / "data_1.txt"
    removed.unlink()
    repo.add(".")
    repo.commit("Remove one file")

    assert not (repo.dothm.path / "versions/data_1.txt").exists()
    repo.checkout("main")
    assert removed.read_text() == "original data_1.txt\n"
    repo.checkout("experiment")
    assert not removed.exists()
    assert file_versions_by_path(repo.state) == {
        "data_2.txt": Repo.checksum(repo.worktree / "data_2.txt")}


def test_checkout_after_unstaging_preserves_unstaged_metadata(tmp_path):
    repo = _committed_repo(tmp_path, names=("data_1.txt",))
    repo.create_branch("experiment")
    repo.checkout("experiment")
    path = repo.worktree / "data_2.txt"
    path.write_text("experiment payload\n")
    repo.add_paths([path])
    repo.commit("Additional experiment file")
    repo.checkout("main")
    path.write_text("experiment payload\n")
    repo.add_paths([path])
    repo.restore_staged([path.as_posix()])
    assert not repo.dothm.index.diff("HEAD")
    metadata_before = {
        name: (repo.dothm.path / name).read_bytes()
        for name in ("data.tsv", "versions/data_2.txt")}
    index_before = repo.dothm.index.entries.copy()
    head_before = repo.dothm.head.commit.hexsha

    with pytest.raises(CheckoutError, match="state changes|would be overwritten"):
        repo.checkout("experiment")

    assert repo.branches()["current"] == "main"
    assert repo.dothm.head.commit.hexsha == head_before
    assert repo.dothm.index.entries == index_before
    assert path.read_text() == "experiment payload\n"
    assert metadata_before == {
        name: (repo.dothm.path / name).read_bytes()
        for name in metadata_before}
