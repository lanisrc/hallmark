"""A failed save leaves the files in .hm and the index as they were (#55)."""

import errno
import os
import pathlib
from pathlib import Path
import subprocess
import tempfile
import threading
import time

import pandas as pd
import pytest
from git.exc import GitCommandError
from git.index import IndexFile

from chaos import check_invariants, fail_nth_replace, snapshot_hm
from hallmark import Repo
from hallmark.repo.dothm import Dothm
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing
from survey import make_telescope_tree

FMT = "{site}/{year:d}/{day:d}.fits"


def _in_dothm(target):
    return ".hm" in Path(target).parts


@pytest.fixture
def telescope(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    make_telescope_tree(repo.worktree)
    return repo


def assert_unchanged(repo, before):
    after = snapshot_hm(repo.worktree)
    assert after["files"] == before["files"]
    assert after["index"] == before["index"]
    assert not check_invariants(repo.worktree, fsck=False)


class _DiskFills:
    """Text file whose first write stops halfway with ENOSPC."""

    def __init__(self, handle):
        self.handle = handle

    def write(self, text):
        self.handle.write(text[:len(text) // 2])
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.handle.__exit__(*exc)


@pytest.mark.parametrize("name", ["config.yml", "data.tsv"])
def test_full_disk_while_writing_a_temporary_file_changes_nothing(
        telescope, monkeypatch, name):
    before = snapshot_hm(telescope.worktree)
    created, failed = {}, []
    mkstemp, fdopen = tempfile.mkstemp, os.fdopen

    def mkstemp_(*args, **kwargs):
        descriptor, temp = mkstemp(*args, **kwargs)
        created[descriptor] = Path(temp).name
        return descriptor, temp

    def fdopen_(descriptor, mode="r", *args, **kwargs):
        handle = fdopen(descriptor, mode, *args, **kwargs)
        if "w" in mode and created.get(descriptor, "").startswith(f".{name}."):
            failed.append(descriptor)
            return _DiskFills(handle)
        return handle

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp_)
    monkeypatch.setattr(os, "fdopen", fdopen_)
    with pytest.raises(OSError) as info:
        telescope.add(FMT)
    monkeypatch.undo()
    assert failed and info.value.errno == errno.ENOSPC
    assert_unchanged(telescope, before)


def test_unserializable_state_fails_before_any_file_changes(telescope):
    before = snapshot_hm(telescope.worktree)
    state = telescope.state
    state.config["data"] = [{"fmt": FMT}]
    # YAML cannot represent a lock; saving used to replace config.yml first.
    state.meta = {"observer": threading.Lock()}
    with pytest.raises(TypeError):
        telescope.dothm.save_state(state)
    assert_unchanged(telescope, before)


def test_locked_index_leaves_every_file_as_it_was(telescope):
    before = snapshot_hm(telescope.worktree)
    lock = Path(telescope.dothm.git_dir) / "index.lock"
    lock.write_bytes(b"")
    try:
        with pytest.raises(GitCommandError):
            telescope.add(FMT)
    finally:
        lock.unlink()
    assert_unchanged(telescope, before)
    Repo(telescope.worktree).add(FMT)
    assert Repo(telescope.worktree).state.config["data"][0]["fmt"] == FMT


ROOT = "https://example.test/data/"
FMTS = [{"fmt": "{src}_{day:03d}.fits", "db": "fits"}, {"fmt": "run_{run}.log"}]
FILTER = ["*.fits", "*.log", "*.md"]


@pytest.fixture
def listing(monkeypatch):
    """List names under ROOT; the server publishes no checksums."""
    names = ["M87_001.fits", "run_1.log", "README.md"]

    def read_text(context, path, **_):
        if path:
            raise RemoteObjectMissing("No metadata")
        return "<h1>Index of data</h1>" + "".join(
            f'<a href="{name}">{name}</a>' for name in names)

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    return names


@pytest.mark.parametrize("nth", [2, 3, 4, 5])
def test_failed_rename_leaves_every_format_table(tmp_path, listing, nth):
    repo = Repo.init(tmp_path / "repo")
    repo.add(ROOT, remote_fmt=FMTS, filter=FILTER)
    repo.commit("Catalogue")
    # config.yml gains a file: entry and each table a row.
    listing.extend(["NEWS.md", "M87_002.fits", "run_2.log"])
    before = snapshot_hm(repo.worktree)
    tables = ["fits.tsv", "run_{run}.log.tsv"]
    assert set(tables) <= set(before["files"])

    with pytest.MonkeyPatch.context() as patch:
        calls = fail_nth_replace(patch, nth, match=_in_dothm)
        with pytest.raises(OSError):
            Repo(repo.worktree).add(ROOT, remote_fmt=FMTS, filter=FILTER)
    assert [call.name for call in calls] == [
        "config.yml", "meta.yml", "data.tsv", *tables][:nth]
    assert_unchanged(repo, before)

    Repo(repo.worktree).add(ROOT, remote_fmt=FMTS, filter=FILTER)
    after = snapshot_hm(repo.worktree)["files"]
    assert all(after[name] != before["files"][name]
               for name in ["config.yml", *tables])


def interrupt_after_replace(monkeypatch, nth):
    """Raise KeyboardInterrupt just after the ``nth`` rename into ``.hm``."""
    original = pathlib.Path.replace
    calls = []

    def replace(self, target):
        result = original(self, target)
        if _in_dothm(target):
            calls.append(Path(target).name)
            if len(calls) == nth:
                raise KeyboardInterrupt
        return result

    monkeypatch.setattr(pathlib.Path, "replace", replace)
    return calls


@pytest.mark.parametrize("nth", [1, 3])
def test_interrupt_after_a_rename_restores_every_file(telescope, monkeypatch, nth):
    before = snapshot_hm(telescope.worktree)
    calls = interrupt_after_replace(monkeypatch, nth)
    with pytest.raises(KeyboardInterrupt):
        telescope.add(FMT)
    monkeypatch.undo()
    assert len(calls) == nth
    assert_unchanged(telescope, before)


def test_interrupt_after_staging_restores_the_index(telescope, monkeypatch):
    before = snapshot_hm(telescope.worktree)
    stage = Dothm._add_to_index

    def stage_then_interrupt(self, paths):
        stage(self, paths)
        raise KeyboardInterrupt

    monkeypatch.setattr(Dothm, "_add_to_index", stage_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        telescope.add(FMT)
    monkeypatch.undo()
    assert_unchanged(telescope, before)


def test_index_that_cannot_be_restored_keeps_the_files_it_stages(
        telescope, monkeypatch):
    before = snapshot_hm(telescope.worktree)
    stage = Dothm._add_to_index

    def stage_then_interrupt(self, paths):
        stage(self, paths)
        raise KeyboardInterrupt

    def index_locked(*args, **kwargs):
        raise OSError(errno.EEXIST, "index.lock exists")

    monkeypatch.setattr(Dothm, "_add_to_index", stage_then_interrupt)
    monkeypatch.setattr(IndexFile, "write", index_locked)
    with pytest.raises(KeyboardInterrupt) as info:
        telescope.add(FMT)
    monkeypatch.undo()
    if hasattr(info.value, "add_note"):
        assert "the index" in "\n".join(info.value.__notes__)

    # Restoring the files alone would pair the old config.yml with new rows.
    assert snapshot_hm(telescope.worktree)["index"] != before["index"]
    unstaged = subprocess.run(
        ["git", "-C", str(telescope.dothm.path), "diff", "--name-only"],
        capture_output=True, text=True, check=True).stdout
    assert unstaged == ""
    assert Repo(telescope.worktree).state.config["data"][0]["fmt"] == FMT


def test_interrupt_during_the_rollback_still_restores_every_file(
        telescope, monkeypatch):
    before = snapshot_hm(telescope.worktree)
    calls = fail_nth_replace(monkeypatch, 3, match=_in_dothm)
    replace = os.replace

    def interrupt_restoring_data(source, target, *args, **kwargs):
        if Path(target).name == "data.tsv" and len(calls) >= 3:
            raise KeyboardInterrupt
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", interrupt_restoring_data)
    with pytest.raises(OSError) as info:
        telescope.add(FMT)
    monkeypatch.undo()
    assert info.value.errno == errno.ENOSPC
    assert_unchanged(telescope, before)


@pytest.mark.parametrize("links", [True, False], ids=["hard-link", "copy"])
def test_failed_rollback_keeps_the_original_error(telescope, monkeypatch, links):
    before = snapshot_hm(telescope.worktree)
    if not links:
        def link(*args, **kwargs):
            raise OSError(errno.EPERM, os.strerror(errno.EPERM))

        monkeypatch.setattr(os, "link", link)
    calls = fail_nth_replace(monkeypatch, 3, match=_in_dothm)
    replace = os.replace

    def config_in_use(source, target, *args, **kwargs):
        if Path(target).name == "config.yml" and len(calls) >= 3:
            raise PermissionError(errno.EACCES, "sharing violation", str(target))
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", config_in_use)
    with pytest.raises(OSError) as info:
        telescope.add(FMT)
    monkeypatch.undo()
    assert type(info.value) is OSError and info.value.errno == errno.ENOSPC
    if hasattr(info.value, "add_note"):
        assert "config.yml" in "\n".join(info.value.__notes__)

    after = snapshot_hm(telescope.worktree)
    assert after["files"].pop("config.yml") != before["files"].pop("config.yml")
    assert after["files"] == before["files"]
    assert after["index"] == before["index"]
    assert not check_invariants(telescope.worktree, fsck=False)


def test_threads_sharing_a_repository_take_turns_with_the_state_lock(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    events, held = [], threading.Event()

    def first():
        with repo.dothm.state_lock():
            events.append("first in")
            held.set()
            time.sleep(0.2)
            events.append("first out")

    def second():
        held.wait(10)
        with repo.dothm.state_lock():
            events.append("second in")

    threads = [threading.Thread(target=first, daemon=True),
               threading.Thread(target=second, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert events == ["first in", "first out", "second in"]


def _finishes(target, timeout=30):
    """Run ``target`` in a thread; return whether it ended in time, and its error."""
    errors = []

    def run():
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive(), errors


def test_saving_while_holding_the_state_lock_does_not_wait_for_itself(telescope):
    def save():
        with telescope.dothm.state_lock():
            telescope.add(FMT)
            with telescope.dothm.state_lock():
                pass

    def lock_from_another_handle():
        with Dothm(telescope.dothm.path).state_lock():
            pass

    assert _finishes(save) == (True, [])
    # The file lock was released, so another holder gets it.
    assert _finishes(lock_from_another_handle) == (True, [])


def test_a_second_repo_object_saves_while_the_first_holds_the_state_lock(
        telescope):
    def save_through_another_repo():
        with telescope.dothm.state_lock():
            Repo(telescope.worktree).set_config(
                remote_url="https://example.com/data/")

    assert _finishes(save_through_another_repo, timeout=10) == (True, [])
    assert Repo(telescope.worktree).state.config["remote"]["url"] == \
        "https://example.com/data/"


def test_saving_removes_temporary_files_a_killed_save_left(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    hm = repo.dothm.path
    stale = [hm / ".data.tsv.k1ll3d.tmp", hm / ".a[1].tsv.k1ll3d.tmp"]
    # Matches ".a[1].tsv.*.tmp" unless the brackets are escaped.
    other = hm / ".a1.tsv.k1ll3d.tmp"
    for path in [*stale, other]:
        path.write_text("partial")
    repo.state.config = {"data": [{"db": "a[1].tsv", "fmt": "{name}.h5"}]}
    repo.state.tables["a[1].tsv"] = pd.DataFrame({"path": ["x.h5"], "name": ["x"]})
    repo.dothm.save_state(repo.state)
    assert not any(path.exists() for path in stale)
    assert other.exists()
    assert (hm / "a[1].tsv").read_text().splitlines() == ["path\tname", "x.h5\tx"]
