"""Crash consistency: hm is killed with SIGKILL partway through a command.

A killed command may leave temporary files, but never a corrupt
repository, and running the same command again must finish the job.
"""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from chaos import check_invariants, snapshot_hm
from hallmark import Repo
from hallmark.repo.dothm import Dothm
from http_server import SlowDrip, write_file
from survey import make_telescope_tree

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.usefixtures("loopback_only"),
    pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs SIGKILL"),
]

CHILD = str(Path(__file__).with_name("chaos_child.py"))
FMT = "{site}/{year:d}/{day:d}.fits"


def _child_env():
    env = dict(os.environ)
    env.pop("HALLMARK_CHAOS_KILL", None)
    # The child runs in the repository, where a relative PYTHONPATH misses.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(Path(__file__).parents[1] / "mod"), env.get("PYTHONPATH")]))
    return env


def _run(cwd, args, kill=None, input=None):
    env = _child_env()
    if kill is not None:
        env["HALLMARK_CHAOS_KILL"] = json.dumps(kill)
    return subprocess.run([sys.executable, CHILD, *args], cwd=cwd, env=env,
                          input=input, capture_output=True, text=True, timeout=120)


def _killed(cwd, args, kill, input=None):
    result = _run(cwd, args, kill, input)
    # Otherwise the kill point was never reached and the test proves nothing.
    assert result.returncode == -signal.SIGKILL, result.stdout + result.stderr
    problems = check_invariants(cwd)
    assert not problems.corruption, problems
    return result


def _rerun(cwd, args, input=None):
    result = _run(cwd, args, input=input)
    assert result.returncode == 0, result.stdout + result.stderr
    problems = check_invariants(cwd)
    assert not problems, problems
    return result


def _replace(nth, match, when="after"):
    return {"target": "pathlib:Path.replace", "nth": nth, "when": when,
            "match": match}


@pytest.fixture
def telescope(tmp_path):
    repo = Repo.init(tmp_path / "obs")
    files = make_telescope_tree(repo.worktree)
    return repo, files


@pytest.mark.parametrize("nth", [1, 2, 3])
def test_killed_add_recovers_on_rerun(tmp_path, telescope, nth):
    repo, _ = telescope
    reference = Repo.init(tmp_path / "reference")
    make_telescope_tree(reference.worktree)
    reference.add(FMT)

    _killed(repo.worktree, ["add", FMT], _replace(nth, "/.hm/"))
    _rerun(repo.worktree, ["add", FMT])
    assert snapshot_hm(repo.worktree)["files"] == \
        snapshot_hm(reference.worktree)["files"]


@pytest.mark.parametrize("kill", [
    _replace(1, "/objects/"),
    _replace(5, "/objects/"),
    {"target": "git.index.base:IndexFile.commit", "nth": 1, "when": "before"},
], ids=["first-object", "fifth-object", "before-git-commit"])
def test_killed_commit_recovers_on_rerun(telescope, kill):
    repo, files = telescope
    repo.add(FMT)
    before = snapshot_hm(repo.worktree)["refs"]
    _killed(repo.worktree, ["commit", "-m", "Ingest"], kill)
    assert snapshot_hm(repo.worktree)["refs"] == before
    _rerun(repo.worktree, ["commit", "-m", "Ingest"])
    repo = Repo(repo.worktree)
    assert "Ingest" in repo.log()
    assert all(repo.objects.contains(hashlib.sha1(data).hexdigest())
               for data in files.values())


@pytest.fixture
def two_branches(telescope):
    """Commit main, then branch exp with two files changed; return to main."""
    repo, files = telescope
    repo.add(FMT)
    repo.commit("main data")
    assert _run(repo.worktree, ["branch", "exp"]).returncode == 0
    assert _run(repo.worktree, ["checkout", "exp"]).returncode == 0
    for path in ("ALMA/2024/95.fits", "SMA/2025/96.fits"):
        (repo.worktree / path).write_bytes(b"exp version of " + path.encode())
    exp = Repo(repo.worktree)
    exp.add(FMT)
    exp.commit("exp data")
    assert _run(repo.worktree, ["checkout", "main"]).returncode == 0
    return repo, files


@pytest.mark.parametrize("nth", [1, 3])
def test_killed_checkout_can_be_completed(two_branches, nth):
    repo, _ = two_branches
    _killed(repo.worktree, ["checkout", "exp"], _replace(nth, ".fits"))
    _rerun(repo.worktree, ["checkout", "exp"])
    assert (repo.worktree / "SMA/2025/96.fits").read_bytes() == \
        b"exp version of SMA/2025/96.fits"


def _branch_files(root, files, branch):
    """Assert that the two files exp changes hold ``branch``'s version."""
    for path in ("ALMA/2024/95.fits", "SMA/2025/96.fits"):
        expected = (b"exp version of " + path.encode() if branch == "exp"
                    else files[path])
        assert (Path(root) / path).read_bytes() == expected, path


def _kill_only(cwd, args, kill):
    """Kill ``hm args`` at ``kill`` without opening the repository after."""
    result = _run(cwd, args, kill)
    assert result.returncode == -signal.SIGKILL, result.stdout + result.stderr


# Path.replace calls that match ".fits": two backups, then two installs.
@pytest.mark.parametrize("kill, head", [
    (_replace(2, ".fits"), "exp"),
    (_replace(4, ".fits"), "exp"),
    ({"target": "hallmark.repo.objects:Objects.restore", "nth": 2,
      "when": "before"}, "main"),
    ({"target": "git.cmd:Git.execute", "nth": 1, "when": "after",
      "match": "checkout"}, "exp"),
], ids=["both-backed-up", "all-installed", "while-staging", "after-git"])
@pytest.mark.parametrize("then", ["exp", "main"])
def test_killed_checkout_is_rolled_back_on_open(two_branches, kill, head, then):
    repo, files = two_branches
    _kill_only(repo.worktree, ["checkout", "exp"], kill)
    assert snapshot_hm(repo.worktree)["head"] == f"refs/heads/{head}"
    assert list(repo.worktree.glob(".hallmark-checkout-*"))
    _rerun(repo.worktree, ["status"])
    assert snapshot_hm(repo.worktree)["head"] == "refs/heads/main"
    _branch_files(repo.worktree, files, "main")
    _rerun(repo.worktree, ["checkout", then])
    _branch_files(repo.worktree, files, then)


# After the journal is gone: before or while the folder is removed.
@pytest.mark.parametrize("kill", [
    {"target": "shutil:rmtree", "nth": 1, "when": "before",
     "match": ".hallmark-checkout-"},
    {"target": "os:rmdir", "nth": 1, "when": "after", "match": "backup"},
    {"target": "os:rmdir", "nth": 1, "when": "after", "match": "staged"},
], ids=["before", "backup-removed", "staged-removed"])
def test_checkout_killed_removing_its_folder_stands(two_branches, kill):
    repo, files = two_branches
    _kill_only(repo.worktree, ["checkout", "exp"], kill)
    assert list(repo.worktree.glob(".hallmark-checkout-*"))
    _rerun(repo.worktree, ["status"])
    assert snapshot_hm(repo.worktree)["head"] == "refs/heads/exp"
    _branch_files(repo.worktree, files, "exp")
    _rerun(repo.worktree, ["checkout", "exp"])


@pytest.mark.parametrize("first", [3, 4])
@pytest.mark.parametrize("second", [1, 2])
def test_killed_recovery_is_finished_by_the_next_open(two_branches, first, second):
    repo, files = two_branches
    _kill_only(repo.worktree, ["checkout", "exp"], _replace(first, ".fits"))
    # The open rolls the checkout back; kill it after moving originals back.
    _kill_only(repo.worktree, ["status"], {
        "target": "os:replace", "nth": second, "when": "after",
        "match": "backup"})
    _rerun(repo.worktree, ["status"])
    assert snapshot_hm(repo.worktree)["head"] == "refs/heads/main"
    _branch_files(repo.worktree, files, "main")
    _rerun(repo.worktree, ["checkout", "exp"])
    _branch_files(repo.worktree, files, "exp")


def test_add_after_a_killed_checkout_keeps_the_original_rows(two_branches):
    repo, _ = two_branches
    before = snapshot_hm(repo.worktree)
    _kill_only(repo.worktree, ["checkout", "exp"], _replace(3, ".fits"))
    _rerun(repo.worktree, ["add", "."])
    after = snapshot_hm(repo.worktree)
    assert after["head"] == "refs/heads/main"
    assert after["index"] == before["index"]
    assert after["files"]["data.tsv"] == before["files"]["data.tsv"]


# exp adds a file of which main has an untracked copy; it is backed up first.
@pytest.mark.parametrize("kill, recovery_kill", [
    ({"target": "git.cmd:Git.execute", "nth": 1, "when": "after",
      "match": "checkout"}, None),
    (_replace(6, ".fits"), {"target": "os:replace", "nth": 3, "when": "after",
                            "match": "backup"}),
], ids=["before-backups", "after-moving-it-back"])
def test_killed_checkout_keeps_an_untracked_copy_of_a_new_file(
        two_branches, kill, recovery_kill):
    repo, files = two_branches
    repo = Repo(repo.worktree)
    repo.checkout("exp")
    copy = write_file(repo.worktree, "VLA/2024/1.fits", b"new in exp")
    repo.add(FMT)
    repo.commit("Add VLA")
    repo.checkout("main")
    write_file(repo.worktree, "VLA/2024/1.fits", b"new in exp")

    _kill_only(repo.worktree, ["checkout", "exp"], kill)
    if recovery_kill:
        _kill_only(repo.worktree, ["status"], recovery_kill)
    _rerun(repo.worktree, ["status"])
    assert snapshot_hm(repo.worktree)["head"] == "refs/heads/main"
    _branch_files(repo.worktree, files, "main")
    assert copy.read_bytes() == b"new in exp"


def test_killed_checkout_of_a_remote_branch_deletes_the_branch_it_made(
        two_branches, tmp_path):
    source, files = two_branches
    clone = tmp_path / "clone"
    Dothm.clone(str(source.dothm.path), clone / ".hm")
    make_telescope_tree(clone)
    objects = Repo(clone).objects
    for path, data in files.items():
        objects.store(clone / path, hashlib.sha1(data).hexdigest())

    _kill_only(clone, ["checkout", "exp"], _replace(3, ".fits"))
    assert "refs/heads/exp " in snapshot_hm(clone)["refs"]
    _rerun(clone, ["status"])
    after = snapshot_hm(clone)
    assert after["head"] == "refs/heads/main"
    assert "refs/heads/exp " not in after["refs"]
    _branch_files(clone, files, "main")
    _rerun(clone, ["checkout", "exp"])
    _branch_files(clone, files, "exp")


def test_killed_checkout_from_a_detached_head_detaches_it_again(two_branches):
    repo, files = two_branches
    commit = repo.dothm.head.commit.hexsha
    Repo(repo.worktree).checkout(commit)
    _kill_only(repo.worktree, ["checkout", "exp"], _replace(3, ".fits"))
    _rerun(repo.worktree, ["status"])
    assert snapshot_hm(repo.worktree)["head"] == commit
    _branch_files(repo.worktree, files, "main")


def _restore(nth, when):
    return {"target": "hallmark.repo.objects:Objects.restore", "nth": nth,
            "when": when}


@pytest.mark.parametrize("existing_branch,kill", [
    (False, _restore(1, "before")),
    (True, _restore(2, "after")),
    (True, _restore(5, "after")),
    (False, _replace(3, ".fits", when="before")),
    (True, {"target": "hallmark.repo.dothm:Dothm.link_worktree", "nth": 1,
            "when": "before"}),
], ids=["before-first-restore", "after-second-restore", "after-fifth-restore",
        "mid-restore", "before-link-existing-branch"])
def test_killed_add_worktree_is_completed_by_rerun(telescope, existing_branch, kill):
    repo, files = telescope
    repo.add(FMT)
    repo.commit("main data")
    if existing_branch:
        assert _run(repo.worktree, ["branch", "obs2"]).returncode == 0
    code = 'Repo(".").add_worktree("obs2")'
    _killed(repo.worktree, ["--api", code], kill)
    _rerun(repo.worktree, ["--api", code])
    second = repo.worktree.parent / "obs2"
    for path, data in files.items():
        assert (second / path).read_bytes() == data
    # The marker and any half-written copy are gone.
    problems = check_invariants(second)
    assert not problems, problems
    assert Repo(second).branches()["current"] == "obs2"


def test_killed_download_keeps_partial_data_separate(tmp_path, http_server):
    www = tmp_path / "www"
    data = os.urandom(400_000)
    write_file(www, "rel/big.bin", data)
    write_file(www, "rel/sha256sum.txt",
               f"{hashlib.sha256(data).hexdigest()}  big.bin\n")
    server = http_server(www)
    repo = Repo.init(tmp_path / "repo")
    repo.add(server.url("rel/") + "{name}.bin")
    repo.commit("Catalog")
    server.faults.add("rel/big.bin", SlowDrip(chunk=4096, delay=0.02), times=1)

    child = subprocess.Popen([sys.executable, CHILD, "download", "--all"],
                             cwd=repo.worktree, env=_child_env(), stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.stdin.write(b"y\n")
    child.stdin.close()
    deadline = time.monotonic() + 30
    partials = repo.objects.tmp.glob
    while not any(path.stat().st_size for path in partials(".big.bin.*")):
        assert child.poll() is None and time.monotonic() < deadline, \
            "download finished or stalled before it could be interrupted"
        time.sleep(0.01)
    child.kill()
    child.wait(timeout=10)

    # The partial transfer stays in the store's temporary folder, never at
    # the destination; the rerun completes and verifies the file.
    assert not (repo.worktree / "big.bin").exists()
    assert list(partials(".big.bin.*"))
    assert not check_invariants(repo.worktree).corruption
    _run(repo.worktree, ["download", "--all"], input="y\n")
    assert (repo.worktree / "big.bin").read_bytes() == data
