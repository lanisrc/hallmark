"""Edge cases of checkout, worktrees, status, unstaging and history in a Repo."""

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re

import pandas as pd
import pytest
from git.cmd import Git
from git.exc import GitCommandError

from hallmark import Repo
from hallmark.error import CheckoutError, CloneError, DothmError
from hallmark.repo import branches
from hallmark.repo.branches import checkout_folders
from hallmark.repo.changes import (
    ensure_clean_tracked_files,
    find_changed_and_missing_files,
    working_directory_for_repo)
from hallmark.repo.dothm import Dothm
from hallmark.repo.history import (
    fetch_missing_objects_from_remote,
    load_branch_state)


FMT = "a{a}_i{i}.h5"
INITIAL_STATE = ["config.yml", "data.tsv", "meta.yml"]


def _write(root, *names):
    for name in names:
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"contents of {name}\n", encoding="utf-8")


def _committed_repo(tmp_path, names=("a0_i0.h5",)):
    repo = Repo.init(tmp_path / "repo")
    _write(repo.worktree, *names)
    repo.add(FMT)
    repo.commit("First dataset")
    return repo


def _two_branch_repo(tmp_path):
    """Return a repository whose main tracks a0 files and experiment a1 files."""
    repo = _committed_repo(tmp_path, names=("a0_i0.h5", "a0_i30.h5"))
    repo.create_branch("experiment")
    repo.checkout("experiment")
    for name in ("a0_i0.h5", "a0_i30.h5"):
        (repo.worktree / name).unlink()
    _write(repo.worktree, "a1_i45.h5", "a1_i90.h5")
    repo.add(".")
    repo.commit("Experiment dataset")
    repo.checkout("main")
    return repo


def _h5_names(repo):
    return sorted(path.name for path in Path(str(repo.worktree)).glob("*.h5"))


def _fail_staged_install(patch, failing_call=1):
    """Make the n-th move of a prepared file into the worktree fail."""
    original_replace = Path.replace
    calls = 0

    def replace(path, target):
        nonlocal calls
        # Objects.restore also renames inside "staged"; installs move "staged/N".
        if path.parent.name == "staged" and path.name.isdigit():
            calls += 1
            if calls == failing_call:
                raise OSError("simulated install failure")
        return original_replace(path, target)

    patch.setattr(Path, "replace", replace)


def _git_failure(command, *args):
    return GitCommandError(["git", command, *map(str, args)], 1, b"", b"locked")


def _remote_catalog(repo, paths, **columns):
    """Save a remote catalogue of ``paths`` as the staged state."""
    repo.state.config = {
        "data": [{"db": "data.tsv"}],
        "remote": [{"name": "origin", "url": "https://x.test/d/"}],
        "sources": [{"name": "origin", "remote": "origin"}]}
    repo.state.data = pd.DataFrame(
        {"path": list(paths), "catalog_source": "origin", **columns})
    repo.dothm.save_state(repo.state)
    return repo.state


def _clone_with_remote_branch(tmp_path):
    """
    Clone a source catalogue, then give the source an experiment branch that the
    clone only knows as origin/experiment once it has fetched.
    """
    source = Repo.init(tmp_path / "source")
    data_path = source.worktree / "data.txt"
    data_path.write_text("main contents\n", encoding="utf-8")
    source.set_config(fmt="{name}.txt")
    source.add("data.txt")
    source.commit("Initial commit")
    Dothm.clone(str(source.dothm.path), tmp_path / "clone" / ".hm")
    clone = Repo(tmp_path / "clone")
    (clone.worktree / "data.txt").write_text("main contents\n", encoding="utf-8")
    source.create_branch("experiment")
    source.checkout("experiment")
    data_path.write_text("experiment contents\n", encoding="utf-8")
    source.add(".")
    source.commit("Experiment commit")
    clone.dothm.git.fetch("origin")
    return source, clone


def _branch_with_invalid_checksum(repo, branch):
    """Commit a catalogue with a malformed checksum to a branch, staying on main."""
    repo.dothm.git.checkout("-b", branch)
    state = repo.dothm.load_state()
    state.data.loc[0, "sha1"] = "not-a-checksum"
    repo.dothm.save_state(state)
    repo.dothm.index.commit("Corrupt checksum")
    repo.dothm.git.checkout("main")
    return Repo(repo.worktree)


def _repo_on_unborn_branch(tmp_path):
    """Return a repository whose current branch has no commits yet."""
    repo = Repo.init(tmp_path / "repo")
    repo.dothm.git.checkout("--orphan", "fresh")
    return Repo(tmp_path / "repo")


### bare repositories ###

@pytest.fixture
def bare_repo(tmp_path):
    return Repo.init(tmp_path / "catalog.hm")


@pytest.mark.parametrize("operation,error,message", [
    (lambda repo: repo.checkout("main"), CheckoutError, "without a worktree"),
    (lambda repo: repo.add(FMT), RuntimeError, "in a bare repository"),
    (lambda repo: repo.add_paths(["a0_i0.h5"]), RuntimeError, "without a worktree"),
    (lambda repo: repo.add_worktree("experiment"), RuntimeError,
     "in a bare repository"),
    (lambda repo: repo._resolve_worktree_path("a0_i0.h5"), RuntimeError,
     "without a worktree"),
], ids=["checkout", "add", "add_paths", "add_worktree", "resolve_path"])
def test_bare_repository_refuses_operations_on_data_files(
        bare_repo, operation, error, message):
    with pytest.raises(error, match=message):
        operation(bare_repo)
    assert bare_repo.status()["staged"]["state"] == INITIAL_STATE


@pytest.mark.parametrize("inspect,error", [
    (working_directory_for_repo, RuntimeError),
    (lambda repo: find_changed_and_missing_files(repo, {}), RuntimeError),
    (ensure_clean_tracked_files, CheckoutError),
], ids=["working_directory", "changed_files", "clean_check"])
def test_bare_repository_has_no_files_to_inspect(bare_repo, inspect, error):
    with pytest.raises(error, match="without a worktree"):
        inspect(bare_repo)


def test_bare_repository_status_reports_state_but_no_data_files(bare_repo):
    status = bare_repo.status()

    assert status["branch"] == "main"
    assert status["staged"]["state"] == INITIAL_STATE
    assert status["worktree"] == {"modified": [], "deleted": []}
    assert status["untracked"] == []


### branches without commits ###

def test_status_and_log_of_a_branch_without_commits(tmp_path):
    repo = _repo_on_unborn_branch(tmp_path)

    status = repo.status()

    assert status["branch"] == "fresh"
    assert status["commit"] is None
    # Nothing is committed, so every staged .hm file is a state change.
    assert status["staged"]["state"] == ["README.md", *INITIAL_STATE]
    assert repo.log() == ""
    assert repo.branches() == {
        "current": "fresh", "names": ["main"], "detached_at": None}


def test_unstaging_a_state_file_without_commits_removes_it_from_the_index(
        tmp_path):
    repo = _repo_on_unborn_branch(tmp_path)

    repo.restore_staged([".hm/config.yml"])

    assert repo.status()["staged"]["state"] == [
        "README.md", "data.tsv", "meta.yml"]
    assert (repo.dothm.path / "config.yml").is_file(), \
        "Expected unstaging to keep the file on disk."


def test_unstaging_everything_without_commits_empties_the_index(tmp_path):
    repo = _repo_on_unborn_branch(tmp_path)

    repo.restore_staged(["."])

    assert not repo.dothm.index.entries
    assert repo.status()["staged"]["state"] == []
    assert (repo.dothm.path / "data.tsv").is_file()


### status of .hm and of remote catalogues ###

def test_status_reports_a_deleted_state_file_and_reads_its_staged_copy(tmp_path):
    repo = _committed_repo(tmp_path)
    repo.state.meta = {"title": "Survey"}
    repo.dothm.save_state(repo.state)
    repo.commit("Name the survey")
    (repo.dothm.path / "meta.yml").unlink()

    reopened = Repo(repo.worktree)

    assert reopened.state.meta == {"title": "Survey"}
    assert reopened.status()["worktree"] == {
        "modified": [], "deleted": [".hm/meta.yml"]}


def test_status_does_not_report_legacy_version_records_in_dothm(tmp_path):
    repo = _committed_repo(tmp_path)
    sha1 = repo.state.data.iloc[0]["sha1"]
    # A per-row record of an older layout, staged but absent from disk.
    repo.dothm.stage_text("versions/a0_i0.h5", f'{{"sha1": "{sha1}"}}')

    status = repo.status()

    assert status["worktree"] == {"modified": [], "deleted": []}
    assert status["staged"]["state"] == []


def test_status_of_remote_catalogue_skips_rows_under_a_symlinked_folder(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _remote_catalog(repo, ["linked/a.h5"], sha1=["a" * 40])
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere, "a.h5")
    try:
        os.symlink(elsewhere, repo.worktree / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    status = repo.status()

    assert status["remote_catalog"] is True
    assert status["staged"]["added"] == ["linked/a.h5"]
    assert status["worktree"] == {"modified": [], "deleted": []}
    assert status["untracked"] == []


### add ###

def test_add_with_regex_encoding_is_refused_for_a_remote_url(tmp_path):
    repo = Repo.init(tmp_path / "repo")

    with pytest.raises(ValueError, match="--regex is only supported for local data"):
        repo.add("https://example.com/data/", encoding=True)
    assert "sources" not in Repo(repo.worktree).state.config


def test_local_files_cannot_be_added_to_a_remote_catalogue(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _remote_catalog(repo, ["a0_i0.h5"])
    _write(repo.worktree, "a0_i0.h5", "a0_i30.h5")

    with pytest.raises(ValueError, match="Use a remote URL to update this catalog"):
        repo.add(FMT)
    with pytest.raises(ValueError, match="Use a remote URL to update this catalog"):
        repo.add_paths(["a0_i30.h5"])
    assert Repo(repo.worktree).state.data["path"].tolist() == ["a0_i0.h5"]


@pytest.mark.parametrize("paths", [
    [], ["https://example.com/a0_i0.h5"], [FMT]], ids=["none", "url", "pattern"])
def test_add_paths_requires_local_file_or_folder_paths(tmp_path, paths):
    repo = _committed_repo(tmp_path)

    with pytest.raises(ValueError, match="requires local file or directory paths"):
        repo.add_paths(paths)


def test_add_paths_rejects_a_path_outside_the_worktree(tmp_path):
    repo = _committed_repo(tmp_path)
    _write(tmp_path, "a9_i9.h5")

    with pytest.raises(ValueError,
                       match="add path is outside the worktree: '../a9_i9.h5'"):
        repo.add_paths(["../a9_i9.h5"])
    assert repo.status()["staged"]["added"] == []


def test_add_paths_reports_a_missing_file(tmp_path):
    repo = _committed_repo(tmp_path)

    with pytest.raises(FileNotFoundError, match="add path not found: 'a9_i9.h5'"):
        repo.add_paths(["a9_i9.h5"])
    assert repo.status()["staged"]["added"] == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_add_paths_rejects_a_path_that_is_not_a_regular_file(tmp_path):
    repo = _committed_repo(tmp_path)
    os.mkfifo(repo.worktree / "a9_i9.h5")

    with pytest.raises(ValueError, match="add path is not a regular file"):
        repo.add_paths(["a9_i9.h5"])
    assert Repo(repo.worktree).state.data["path"].tolist() == ["a0_i0.h5"]


### restore --staged ###

def test_restore_staged_requires_at_least_one_path(tmp_path):
    repo = _committed_repo(tmp_path)

    with pytest.raises(ValueError, match="Usage: hm restore --staged"):
        repo.restore_staged([])
    with pytest.raises(ValueError, match="Usage: hm restore --staged"):
        repo.dothm.restore_index()


def test_restore_staged_rejects_a_path_outside_the_worktree(tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path)
    monkeypatch.chdir(repo.worktree)

    with pytest.raises(ValueError, match="restore path is outside the worktree"):
        repo.restore_staged(["../elsewhere.h5"])


def test_restore_staged_unstages_a_state_file_and_keeps_it_on_disk(tmp_path):
    repo = _committed_repo(tmp_path)
    repo.set_config(remote_url="https://example.com/data/")
    assert repo.status()["staged"]["state"] == ["config.yml"]

    repo.restore_staged([".hm/config.yml"])

    status = repo.status()
    assert status["staged"]["state"] == []
    assert status["worktree"]["modified"] == [".hm/config.yml"]
    assert "https://example.com/data/" in (
        repo.dothm.path / "config.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [
    ".hm/objects", ".hm/versions.yml", ".hm/versions/a0_i0.h5"])
def test_restore_staged_refuses_the_object_store_and_version_records(
        tmp_path, path):
    repo = _committed_repo(tmp_path)

    with pytest.raises(ValueError,
                       match="Use dataset paths to restore file versions"):
        repo.restore_staged([path])


def test_unstaging_a_row_of_an_older_layout_moves_the_catalogue_to_data_tsv(
        tmp_path):
    repo = Repo.init(tmp_path / "repo")
    state = _remote_catalog(repo, ["a.h5"], sha1=[""])
    versions = repo.dothm.path / "versions"
    versions.mkdir()

    def write_record(path):
        (versions / path).write_text(json.dumps(
            {"catalog_source": "origin", "path": path, "sha1": ""}, sort_keys=True))

    # Commit, then stage, the per-row records older versions wrote.
    write_record("a.h5")
    (repo.dothm.path / "versions.yml").write_text(
        "columns: [path, catalog_source, sha1]\n")
    (repo.dothm.path / "data.tsv").write_text("unused\n")
    repo.dothm.write_yaml(state.config, "config")
    repo.dothm.git.add("config.yml", "data.tsv", "versions.yml", "versions")
    repo.commit("Older layout")
    write_record("b.h5")
    write_record("c.h5")
    repo.dothm.git.add("versions")

    repo.restore_staged(["b.h5"])

    staged = {path for path, _ in repo.dothm.index.entries}
    assert not {path for path in staged
                if path == "versions.yml" or path.startswith("versions/")}
    assert repo.dothm.load_state(staged=True).data["path"].tolist() == [
        "a.h5", "c.h5"]
    assert repo.status()["staged"]["added"] == ["c.h5"]


def test_restore_staged_rejects_an_unknown_state_file_and_keeps_the_index(
        tmp_path):
    repo = _committed_repo(tmp_path)
    repo.set_config(remote_url="https://example.com/data/")
    before = repo.dothm.index.entries.copy()

    with pytest.raises(ValueError,
                       match=r"No staged or committed path matches '\.hm/notes\.txt'"):
        repo.restore_staged([".hm/config.yml", ".hm/notes.txt"])

    assert repo.dothm.index.entries == before
    assert repo.status()["staged"]["state"] == ["config.yml"]


### checkout ###

def test_checkout_refuses_a_target_with_an_invalid_checksum(tmp_path):
    repo = _branch_with_invalid_checksum(_committed_repo(tmp_path), "corrupt")

    with pytest.raises(CheckoutError, match="SHA-1 checksum must be exactly 40"):
        repo.checkout("corrupt")
    assert repo.branches()["current"] == "main"
    assert _h5_names(repo) == ["a0_i0.h5"]


def test_checkout_refuses_a_target_without_one_filename_format(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.commit("Empty catalogue")
    repo.create_branch("empty")
    _write(repo.worktree, "a0_i0.h5")
    repo.add(FMT)
    repo.commit("First dataset")

    with pytest.raises(CheckoutError, match="only repositories with one data format"):
        repo.checkout("empty")
    assert repo.branches()["current"] == "main"
    assert _h5_names(repo) == ["a0_i0.h5"]


def test_checkout_refuses_while_hallmark_state_changes_are_staged(tmp_path):
    repo = _two_branch_repo(tmp_path)
    repo.set_config(remote_url="https://example.com/data/")

    with pytest.raises(CheckoutError, match="uncommitted hallmark state changes"):
        repo.checkout("experiment")
    assert repo.branches()["current"] == "main"
    assert repo.status()["staged"]["state"] == ["config.yml"]
    assert _h5_names(repo) == ["a0_i0.h5", "a0_i30.h5"]


def test_checkout_keeps_a_folder_that_still_holds_untracked_files(tmp_path):
    repo = _committed_repo(tmp_path)
    repo.create_branch("nested")
    repo.checkout("nested")
    _write(repo.worktree, "results/a1_i45.h5")
    repo.add(".")
    repo.commit("Nested dataset")
    _write(repo.worktree, "results/notes.txt")

    repo.checkout("main")

    assert not (repo.worktree / "results" / "a1_i45.h5").exists()
    assert (repo.worktree / "results" / "notes.txt").read_text(
        encoding="utf-8") == "contents of results/notes.txt\n"
    assert _h5_names(repo) == ["a0_i0.h5"]


def test_checkout_stops_before_switching_when_target_files_cannot_be_prepared(
        tmp_path, monkeypatch):
    repo = _two_branch_repo(tmp_path)

    def fail_restore(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(repo.objects, "restore", fail_restore)

    with pytest.raises(CheckoutError, match=(
            'cannot checkout branch "experiment": '
            "failed to prepare target files: disk full")):
        repo.checkout("experiment")
    assert repo.branches()["current"] == "main"
    assert _h5_names(repo) == ["a0_i0.h5", "a0_i30.h5"]


def test_failed_checkout_from_a_detached_commit_returns_to_that_commit(
        tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "a0_i0.h5"
    data_path.write_text("original\n", encoding="utf-8")
    repo.add(FMT)
    repo.commit("Original calibration")
    first_commit = repo.dothm.head.commit.hexsha
    data_path.write_text("recalibrated\n", encoding="utf-8")
    repo.add(".")
    repo.commit("New calibration")
    repo.checkout(first_commit)

    with monkeypatch.context() as patch:
        _fail_staged_install(patch)
        with pytest.raises(CheckoutError,
                           match='checkout of branch "main" failed') as caught:
            repo.checkout("main")

    assert "rollback incomplete" not in str(caught.value)
    reopened = Repo(repo.worktree)
    assert reopened.branches()["current"] is None
    assert reopened.dothm.head.commit.hexsha == first_commit
    assert data_path.read_text(encoding="utf-8") == "original\n"


def test_failed_checkout_reports_rollback_steps_that_also_fail(
        tmp_path, monkeypatch):
    repo = _two_branch_repo(tmp_path)
    original_load_state = repo.dothm.load_state

    def load_state(revision=None, **kwargs):
        if revision is None and not kwargs:
            raise OSError("state unreadable")
        return original_load_state(revision, **kwargs)

    def checkout(git, *args, **kwargs):
        if args == ("main",):
            raise _git_failure("checkout", *args)
        return git._call_process("checkout", *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(repo.dothm, "load_state", load_state)
        patch.setattr(Git, "checkout", checkout, raising=False)
        with pytest.raises(CheckoutError) as caught:
            repo.checkout("experiment")

    message = str(caught.value)
    folder, = checkout_folders(repo.worktree)
    assert message.startswith(
        'checkout of branch "experiment" failed: state unreadable; '
        f"the next open finishes the rollback from {folder}; "
        "rollback incomplete: could not restore branch:")
    assert message.endswith("could not reload repository state: state unreadable")


def test_failed_checkout_reports_files_it_could_not_put_back(tmp_path, monkeypatch):
    repo = _two_branch_repo(tmp_path)
    original_replace = Path.replace
    original_unlink = Path.unlink
    installs = 0

    def replace(path, target):
        nonlocal installs
        if path.parent.name == "staged" and path.name.isdigit():
            installs += 1
            if installs == 2:
                raise OSError("simulated install failure")
        if path.parent.name == "backup":
            raise OSError("backup unreadable")
        return original_replace(path, target)

    def unlink(path, *args, **kwargs):
        if path.name == "a1_i45.h5":
            raise OSError("file busy")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace)
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(CheckoutError) as caught:
            repo.checkout("experiment")

    message = str(caught.value)
    folder, = checkout_folders(repo.worktree)
    assert (f"simulated install failure; originals kept in {folder}; "
            "rollback incomplete:") in message
    assert 'a1_i45.h5": file busy' in message
    assert 'could not restore "' in message
    assert message.count("backup unreadable") == 2
    assert repo.branches()["current"] == "main"
    # The next open removes the file left installed and puts the originals back.
    assert _h5_names(Repo(repo.worktree)) == ["a0_i0.h5", "a0_i30.h5"]
    assert not checkout_folders(repo.worktree)


def _contents(repo):
    return {name: (repo.worktree / name).read_text(encoding="utf-8")
            for name in _h5_names(repo)}


def _refuse_branch_restore(git, *args, **kwargs):
    if args == ("main",):
        raise _git_failure("checkout", *args)
    return git._call_process("checkout", *args, **kwargs)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("step, nth", [
    ("stage", 1), ("move", 1), ("move", 2), ("move", 3),
], ids=["staging", "first-backup", "second-backup", "first-install"])
def test_interrupted_checkout_puts_everything_back(tmp_path, monkeypatch, step,
                                                   nth, when):
    repo = _two_branch_repo(tmp_path)
    before = _contents(repo)
    original_replace = Path.replace
    calls = 0

    def replace(path, target):
        nonlocal calls
        # Files are staged first; then two originals move to "backup" and
        # two files leave "staged".
        if step == "stage":
            counted = Path(target).parent.name == "staged"
        else:
            counted = (Path(target).parent.name == "backup"
                       or path.parent.name == "staged" and path.name.isdigit())
        if not counted:
            return original_replace(path, target)
        calls += 1
        if calls == nth and when == "before":
            raise KeyboardInterrupt
        original_replace(path, target)
        if calls == nth:
            raise KeyboardInterrupt
        return target

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace)
        with pytest.raises(KeyboardInterrupt):
            repo.checkout("experiment")

    assert repo.branches()["current"] == "main"
    assert _contents(repo) == before
    assert not checkout_folders(repo.worktree)
    assert repo.checkout("experiment") is True
    assert _h5_names(repo) == ["a1_i45.h5", "a1_i90.h5"]


def _interrupt_second_reload(patch, repo):
    """Interrupt the reload after a rollback, which leaves the journal as written."""
    original_load_state = repo.dothm.load_state
    loads = 0

    def load_state(*args, **kwargs):
        nonlocal loads
        loads += not args and not kwargs
        if loads == 2:
            raise KeyboardInterrupt
        return original_load_state(*args, **kwargs)

    patch.setattr(repo.dothm, "load_state", load_state)


@pytest.mark.parametrize("stop", ["branch", "interrupt"])
def test_next_open_finishes_a_rollback_and_keeps_the_originals(
        tmp_path, monkeypatch, stop):
    repo = _committed_repo(tmp_path, names=("a0_i0.h5", "a0_i30.h5"))
    repo.create_branch("experiment")
    repo.checkout("experiment")
    for name in ("a0_i0.h5", "a0_i30.h5"):
        (repo.worktree / name).write_text("recalibrated\n", encoding="utf-8")
    repo.add(".")
    repo.commit("Recalibrated")
    repo.checkout("main")
    before = _contents(repo)

    with monkeypatch.context() as patch:
        _fail_staged_install(patch, failing_call=2)
        if stop == "branch":
            patch.setattr(Git, "checkout", _refuse_branch_restore,
                          raising=False)
            expected = pytest.raises(
                CheckoutError, match="the next open finishes the rollback")
        else:
            _interrupt_second_reload(patch, repo)
            expected = pytest.raises(KeyboardInterrupt)
        with expected:
            repo.checkout("experiment")

    # The originals are back and the folder is kept for the next open, which
    # must not take them for the files the checkout installed.
    assert _contents(repo) == before
    assert checkout_folders(repo.worktree)
    reopened = Repo(repo.worktree)
    assert reopened.branches()["current"] == "main"
    assert _contents(reopened) == before
    assert not checkout_folders(repo.worktree)
    assert reopened.status()["worktree"]["modified"] == []


def test_next_open_keeps_an_untracked_copy_of_an_installed_file(tmp_path,
                                                                 monkeypatch):
    repo = _two_branch_repo(tmp_path)
    copy = repo.worktree / "a1_i45.h5"
    copy.write_text("contents of a1_i45.h5\n", encoding="utf-8")

    with monkeypatch.context() as patch:
        _fail_staged_install(patch, failing_call=2)
        _interrupt_second_reload(patch, repo)
        with pytest.raises(KeyboardInterrupt):
            repo.checkout("experiment")

    # The copy moved back has the content of the file installed there.
    assert checkout_folders(repo.worktree)
    assert _h5_names(Repo(repo.worktree)) == ["a0_i0.h5", "a0_i30.h5", "a1_i45.h5"]
    assert copy.read_text(encoding="utf-8") == "contents of a1_i45.h5\n"
    assert not checkout_folders(repo.worktree)


def test_next_open_keeps_a_file_in_the_way_of_an_original(tmp_path, monkeypatch):
    repo = _two_branch_repo(tmp_path)
    original_replace = Path.replace
    installs = 0

    def replace(path, target):
        nonlocal installs
        if path.parent.name == "staged" and path.name.isdigit():
            installs += 1
            if installs == 2:
                raise OSError("simulated install failure")
        if path.parent.name == "backup":
            raise OSError("backup unreadable")
        return original_replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace)
        with pytest.raises(CheckoutError, match="originals kept in"):
            repo.checkout("experiment")
    (repo.worktree / "a0_i0.h5").write_text("written since\n", encoding="utf-8")

    with pytest.raises(CheckoutError, match='"a0_i0.h5" is in the way'):
        Repo(repo.worktree)
    assert (repo.worktree / "a0_i0.h5").read_text(
        encoding="utf-8") == "written since\n"

    (repo.worktree / "a0_i0.h5").unlink()
    reopened = Repo(repo.worktree)
    assert reopened.branches()["current"] == "main"
    assert _contents(reopened) == {
        name: f"contents of {name}\n" for name in ("a0_i0.h5", "a0_i30.h5")}


def test_rollback_never_takes_a_folder_for_an_installed_file(tmp_path):
    folder = tmp_path / "a0_i0.h5"
    folder.mkdir()
    found = folder.stat()
    assert not branches._is_installed(
        folder, "0" * 40, [found.st_dev, found.st_ino])


def test_rollback_tells_an_install_by_content_only_when_asked(tmp_path):
    # As where a rename does not keep the inode.
    _write(tmp_path, "a0_i0.h5")
    path = tmp_path / "a0_i0.h5"
    sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
    assert branches._is_installed(path, sha1, [0, 0])
    assert not branches._is_installed(path, None, [0, 0])
    assert not branches._is_installed(path, "0" * 40, [0, 0])


def test_interrupted_checkout_names_the_folder_left_to_finish(tmp_path,
                                                              monkeypatch):
    repo = _two_branch_repo(tmp_path)
    original_replace = Path.replace

    def replace(path, target):
        if path.parent.name == "staged" and path.name.isdigit():
            raise KeyboardInterrupt
        return original_replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace)
        patch.setattr(Git, "checkout", _refuse_branch_restore, raising=False)
        with pytest.raises(KeyboardInterrupt) as caught:
            repo.checkout("experiment")

    folder, = checkout_folders(repo.worktree)
    if hasattr(caught.value, "add_note"):
        note, = caught.value.__notes__
        assert note.startswith(
            'checkout of branch "experiment" interrupted; the next open '
            f"finishes the rollback from {folder}; rollback incomplete: "
            "could not restore branch:")
    assert Repo(repo.worktree).branches()["current"] == "main"


def test_failed_checkout_reports_a_journal_it_could_not_update(tmp_path,
                                                               monkeypatch):
    repo = _two_branch_repo(tmp_path)
    original_write_journal = branches._write_journal
    writes = 0

    def write_journal(path, journal):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        original_write_journal(path, journal)

    with monkeypatch.context() as patch:
        _fail_staged_install(patch)
        patch.setattr(Git, "checkout", _refuse_branch_restore, raising=False)
        patch.setattr(branches, "_write_journal", write_journal)
        with pytest.raises(CheckoutError) as caught:
            repo.checkout("experiment")

    folder, = checkout_folders(repo.worktree)
    assert str(caught.value).endswith(
        f"could not update {folder / 'journal.json'}: disk full")
    # The journal written first still describes the checkout.
    assert _h5_names(Repo(repo.worktree)) == ["a0_i0.h5", "a0_i30.h5"]
    assert not checkout_folders(repo.worktree)


@pytest.mark.parametrize("code", [errno.EDEADLK, errno.EACCES])
def test_open_takes_the_lock_only_to_roll_back_a_checkout(tmp_path, monkeypatch,
                                                          code):
    repo = _committed_repo(tmp_path)

    @contextmanager
    def fail(self):
        # EDEADLK is what msvcrt.locking raises once it gives up.
        raise OSError(code, os.strerror(code))
        yield

    monkeypatch.setattr(Dothm, "state_lock", fail)
    assert Repo(repo.worktree).branches()["current"] == "main"
    (repo.worktree / ".hallmark-checkout-running").mkdir()
    if code == errno.EDEADLK:
        expected = pytest.raises(CheckoutError, match=re.escape(
            f"another hm checkout is running in {repo.worktree}"))
    else:
        expected = pytest.raises(PermissionError)
    with expected:
        Repo(repo.worktree)


def test_add_and_status_skip_a_checkout_folder(tmp_path):
    repo = _committed_repo(tmp_path)
    _write(repo.worktree, ".hallmark-checkout-kept/a0_i5.h5",
           ".hallmark-checkout-kept/backup/0")

    repo.add(".")

    assert repo.state.data["path"].tolist() == ["a0_i0.h5"]
    assert repo.status()["untracked"] == []


def test_failed_checkout_of_a_remote_branch_removes_the_new_local_branch(
        tmp_path, monkeypatch):
    _, clone = _clone_with_remote_branch(tmp_path)

    with monkeypatch.context() as patch:
        _fail_staged_install(patch)
        with pytest.raises(CheckoutError,
                           match='checkout of branch "experiment" failed') as caught:
            clone.checkout("experiment")

    assert "rollback incomplete" not in str(caught.value)
    assert clone.branches()["names"] == ["main"]
    assert clone.branches()["current"] == "main"
    assert (clone.worktree / "data.txt").read_text(
        encoding="utf-8") == "main contents\n"


def test_failed_checkout_of_a_remote_branch_reports_a_branch_left_behind(
        tmp_path, monkeypatch):
    _, clone = _clone_with_remote_branch(tmp_path)

    def refuse_branch(git, *args, **kwargs):
        raise _git_failure("branch", *args)

    with monkeypatch.context() as patch:
        _fail_staged_install(patch)
        patch.setattr(Git, "branch", refuse_branch, raising=False)
        with pytest.raises(CheckoutError, match=(
                "rollback incomplete: could not remove new branch")):
            clone.checkout("experiment")

    assert "experiment" in clone.branches()["names"]
    assert clone.branches()["current"] == "main"


def test_checkout_of_a_remote_branch_fails_when_no_store_has_its_objects(tmp_path):
    source, clone = _clone_with_remote_branch(tmp_path)
    sha1 = source.state.data.iloc[0]["sha1"]
    source.objects.object_path(sha1).unlink()

    with pytest.raises(CheckoutError, match=f"missing object\\(s\\): {sha1}"):
        clone.checkout("experiment")
    assert clone.branches()["names"] == ["main"]
    assert (clone.worktree / "data.txt").read_text(
        encoding="utf-8") == "main contents\n"

    with pytest.raises(CheckoutError, match='no branch or commit named "nowhere"'):
        clone.checkout("nowhere")


def test_checkout_of_a_remote_catalogue_commit_detaches_head(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _remote_catalog(repo, ["a.h5"])
    repo.commit("Catalogue a.h5")
    first_commit = repo.dothm.head.commit.hexsha
    _remote_catalog(repo, ["a.h5", "b.h5"])
    repo.commit("Catalogue b.h5")
    main_commit = repo.dothm.head.commit.hexsha

    assert repo.checkout(first_commit[:7]) is True

    assert repo.branches()["current"] is None
    assert repo.dothm.head.commit.hexsha == first_commit
    assert repo.state.data["path"].tolist() == ["a.h5"]
    assert repo.dothm.heads["main"].commit.hexsha == main_commit


def test_checkout_refuses_to_switch_between_local_files_and_a_remote_catalogue(
        tmp_path):
    repo = _committed_repo(tmp_path)
    repo.create_branch("catalogue")
    repo.checkout("catalogue")
    (repo.worktree / "a0_i0.h5").unlink()
    _remote_catalog(repo, ["a.h5"])
    repo.commit("Catalogue a server instead")

    with pytest.raises(CheckoutError, match=(
            "Cannot switch between local files and remote catalogs")):
        repo.checkout("main")
    assert repo.branches()["current"] == "catalogue"
    assert not (repo.worktree / "a0_i0.h5").exists()


### worktrees ###

def test_add_worktree_for_an_existing_worktree_changes_nothing(tmp_path):
    repo = _two_branch_repo(tmp_path)
    assert repo.add_worktree("experiment") is True
    linked_file = tmp_path / "experiment" / "a1_i45.h5"
    linked_file.write_text("edited in the linked worktree\n", encoding="utf-8")

    assert repo.add_worktree("experiment") is True

    assert linked_file.read_text(
        encoding="utf-8") == "edited in the linked worktree\n"


def test_add_worktree_reports_an_invalid_checksum_as_a_missing_file(tmp_path):
    repo = _branch_with_invalid_checksum(_committed_repo(tmp_path), "corrupt")

    with pytest.raises(FileNotFoundError, match="SHA-1 checksum must be exactly 40"):
        repo.add_worktree("corrupt")
    assert not (tmp_path / "corrupt").exists()


@pytest.mark.parametrize("prune_fails", [False, True])
def test_add_worktree_cleans_up_when_git_cannot_remove_the_failed_worktree(
        tmp_path, monkeypatch, prune_fails):
    repo = _committed_repo(tmp_path)
    calls = []

    def worktree(git, *args, **kwargs):
        calls.append(args[0])
        if args[0] == "remove" or (args[0] == "prune" and prune_fails):
            raise _git_failure("worktree", *args)
        return git._call_process("worktree", *args, **kwargs)

    def fail_restore(*args, **kwargs):
        raise OSError("disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(Git, "worktree", worktree, raising=False)
        patch.setattr(repo.objects, "restore", fail_restore)
        with pytest.raises(RuntimeError, match=(
                'failed to populate worktree for branch "experiment": '
                "disk failure")):
            repo.add_worktree("experiment")

    assert calls == ["add", "remove", "prune"]
    assert not (tmp_path / "experiment").exists()
    if not prune_fails:
        assert "experiment" not in repo.branches()["names"]
        assert str(tmp_path / "experiment") not in repo.dothm.git.worktree("list")


def test_add_worktree_failure_is_reported_even_if_its_branch_stays(
        tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path)

    def refuse_branch(git, *args, **kwargs):
        raise _git_failure("branch", *args)

    def fail_restore(*args, **kwargs):
        raise OSError("disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(Git, "branch", refuse_branch, raising=False)
        patch.setattr(repo.objects, "restore", fail_restore)
        with pytest.raises(RuntimeError, match="failed to populate worktree"):
            repo.add_worktree("experiment")

    assert "experiment" in repo.branches()["names"]
    assert not (tmp_path / "experiment").exists()


def test_link_worktree_reports_an_unknown_branch(tmp_path):
    repo = _committed_repo(tmp_path)
    target = tmp_path / "linked" / ".hm"

    with pytest.raises(DothmError, match=r'(?s)Failed to link ".*linked.*": .*missing'):
        repo.dothm.link_worktree(target, "missing")
    assert not target.exists()


### clone and history ###

def test_clone_of_a_missing_catalogue_names_the_source_and_creates_nothing(
        tmp_path):
    missing = tmp_path / "missing"
    destination = tmp_path / "copy"

    with pytest.raises(CloneError, match="does not exist") as caught:
        Repo.clone(str(missing), destination, download=False)

    assert str(missing) in str(caught.value)
    assert "For a raw dataset, use hm init PATH" in str(caught.value)
    assert not destination.exists()


def test_load_branch_state_reads_a_branch_known_only_on_the_remote(tmp_path):
    source, clone = _clone_with_remote_branch(tmp_path)

    state = load_branch_state(clone, "experiment")

    assert state.data["sha1"].tolist() == source.state.data["sha1"].tolist()
    assert state.data["sha1"].tolist() != clone.state.data["sha1"].tolist()
    assert "experiment" not in clone.branches()["names"]


def test_fetching_missing_objects_needs_checksums_and_a_known_remote(tmp_path):
    repo = _committed_repo(tmp_path)
    missing = {"0" * 40}

    assert fetch_missing_objects_from_remote(repo, "origin", []) == set()
    assert fetch_missing_objects_from_remote(repo, "origin", missing) == missing
