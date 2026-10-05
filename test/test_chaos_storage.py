"""Local storage faults: full disks, I/O errors and read-only folders.

Writes fail partway through staging, committing and downloading. Each
failure must surface as an error and leave the repository consistent:
no corrupt objects, no partial files and no half-updated state.
"""

import errno
import hashlib
import os

import pytest

from chaos import (check_invariants, fail_nth_replace, fail_writes_after,
                   skip_if_root, snapshot_hm)
from hallmark import Repo
from http_server import write_file
from survey import make_telescope_tree

pytestmark = [pytest.mark.chaos, pytest.mark.usefixtures("loopback_only")]

FMT = "{site}/{year:d}/{day:d}.fits"


def _in_dothm(target):
    return ".hm" in target.parts


@pytest.mark.parametrize("nth", [
    1,
    pytest.param(2, marks=pytest.mark.xfail(strict=True, reason=(
        "Bug: Dothm.save_state writes config.yml, meta.yml and data.tsv "
        "separately, so a failed later write leaves mixed generations (#55)"))),
    pytest.param(3, marks=pytest.mark.xfail(strict=True, reason=(
        "Bug: Dothm.save_state writes config.yml, meta.yml and data.tsv "
        "separately, so a failed later write leaves mixed generations (#55)"))),
])
def test_failed_state_write_keeps_one_generation(tmp_path, monkeypatch, nth):
    reference = Repo.init(tmp_path / "reference")
    make_telescope_tree(reference.worktree)
    reference.add(FMT)
    new = snapshot_hm(reference.worktree)["files"]

    repo = Repo.init(tmp_path / "repo")
    make_telescope_tree(repo.worktree)
    old = snapshot_hm(repo.worktree)
    calls = fail_nth_replace(monkeypatch, nth, match=_in_dothm)
    with pytest.raises(OSError):
        repo.add(FMT)
    monkeypatch.undo()
    assert len(calls) == nth

    after = snapshot_hm(repo.worktree)
    generations = {"old" if after["files"][name] == old["files"][name] else
                   "new" if after["files"][name] == new[name] else "other"
                   for name in after["files"]}
    assert generations in ({"old"}, {"new"}), generations
    assert after["index"] == old["index"]
    assert not check_invariants(repo.worktree)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO])
@pytest.mark.parametrize("nth", [1, 3])
def test_failed_object_store_keeps_commit_retryable(tmp_path, monkeypatch, nth, code):
    repo = Repo.init(tmp_path / "repo")
    files = make_telescope_tree(repo.worktree)
    repo.add(FMT)
    before = snapshot_hm(repo.worktree)

    objects = repo.objects.root
    calls = fail_nth_replace(monkeypatch, nth, code=code,
                             match=lambda target: objects in target.parents)
    with pytest.raises(OSError):
        repo.commit("Ingest")
    monkeypatch.undo()
    assert len(calls) == nth
    assert snapshot_hm(repo.worktree)["refs"] == before["refs"]
    assert not check_invariants(repo.worktree)

    repo = Repo(repo.worktree)
    repo.commit("Ingest")
    assert all(repo.objects.contains(hashlib.sha1(data).hexdigest())
               for data in files.values())
    assert not check_invariants(repo.worktree)


@pytest.fixture
def served_release(tmp_path, http_server):
    www = tmp_path / "www"
    files = {f"rel/d{i}/f.bin": os.urandom(20000) for i in range(3)}
    for path, data in files.items():
        write_file(www, path, data)
    write_file(www, "rel/sha256sum.txt", "".join(
        f"{hashlib.sha256(data).hexdigest()}  {path[4:]}\n"
        for path, data in files.items()))
    server = http_server(www)
    repo = Repo.init(tmp_path / "repo")
    repo.add(server.url("rel/") + "d{i:d}/f.bin")
    repo.commit("Catalog release")
    return repo, files


def test_disk_full_mid_download_fails_only_that_file(monkeypatch, served_release):
    repo, files = served_release
    fail_writes_after(monkeypatch, 5000,
                      match=lambda path: path.name.startswith(".f.bin.")
                      and path.parent.name == "d1")
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    monkeypatch.undo()
    assert (result["succeeded"], result["failed"]) == (2, 1), result
    assert not (repo.worktree / "d1/f.bin").exists()
    assert (repo.worktree / "d0/f.bin").read_bytes() == files["rel/d0/f.bin"]
    assert not check_invariants(repo.worktree)


def test_read_only_output_folder_fails_only_its_files(served_release):
    skip_if_root()
    repo, files = served_release
    locked = repo.worktree / "d2"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        result = repo.download(repo.plan_download(all_files=True), approved=True)
    finally:
        locked.chmod(0o755)
    assert (result["succeeded"], result["failed"]) == (2, 1), result
    assert not (locked / "f.bin").exists()
    assert not check_invariants(repo.worktree)
