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
from http_server import SlowDrip, write_file
from survey import make_telescope_tree

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.usefixtures("loopback_only"),
    pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs SIGKILL"),
]

CHILD = str(Path(__file__).with_name("chaos_child.py"))
FMT = "{site}/{year:d}/{day:d}.fits"


def _run(cwd, args, kill=None, input=None):
    env = dict(os.environ)
    env.pop("HALLMARK_CHAOS_KILL", None)
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
    assert _run(repo.worktree, ["checkout", "exp"]).returncode == 0
    for path in ("ALMA/2024/95.fits", "SMA/2025/96.fits"):
        (repo.worktree / path).write_bytes(b"exp version of " + path.encode())
    exp = Repo(repo.worktree)
    exp.add(FMT)
    exp.commit("exp data")
    assert _run(repo.worktree, ["checkout", "main"]).returncode == 0
    return repo, files


@pytest.mark.parametrize("nth", [
    pytest.param(1, marks=pytest.mark.xfail(strict=True, reason=(
        "Bug: a checkout killed after moving a file to its backup folder "
        "cannot be rerun; the file is reported missing (#56)"))),
    pytest.param(3, marks=pytest.mark.xfail(strict=True, reason=(
        "Bug: a checkout killed after moving a file to its backup folder "
        "cannot be rerun; the file is reported missing (#56)"))),
])
def test_killed_checkout_can_be_completed(two_branches, nth):
    repo, _ = two_branches
    _killed(repo.worktree, ["checkout", "exp"], _replace(nth, ".fits"))
    _rerun(repo.worktree, ["checkout", "exp"])
    assert (repo.worktree / "SMA/2025/96.fits").read_bytes() == \
        b"exp version of SMA/2025/96.fits"


@pytest.mark.xfail(strict=True, reason=(
    "Bug: add_worktree returns early when the destination already has .hm, "
    "so a rerun after a crash leaves the worktree without its files (#57)"))
def test_killed_add_worktree_is_completed_by_rerun(telescope):
    repo, files = telescope
    repo.add(FMT)
    repo.commit("main data")
    code = 'Repo(".").add_worktree("obs2")'
    # Killed after Git created the linked worktree, before restoring files.
    _killed(repo.worktree, ["--api", code], {
        "target": "hallmark.repo.objects:Objects.restore", "nth": 1,
        "when": "before"})
    _rerun(repo.worktree, ["--api", code])
    second = repo.worktree.parent / "obs2"
    for path, data in files.items():
        assert (second / path).read_bytes() == data


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

    env = dict(os.environ)
    env.pop("HALLMARK_CHAOS_KILL", None)
    child = subprocess.Popen([sys.executable, CHILD, "download", "--all"],
                             cwd=repo.worktree, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.stdin.write(b"y\n")
    child.stdin.close()
    deadline = time.monotonic() + 30
    parts = repo.worktree.glob
    while not any(path.stat().st_size for path in parts(".big.bin.*.part")):
        assert child.poll() is None and time.monotonic() < deadline, \
            "download finished or stalled before it could be interrupted"
        time.sleep(0.01)
    child.kill()
    child.wait(timeout=10)

    assert not (repo.worktree / "big.bin").exists()
    assert list(repo.worktree.glob(".big.bin.*.part"))
    assert not check_invariants(repo.worktree).corruption
    _run(repo.worktree, ["download", "--all"], input="y\n")
    assert (repo.worktree / "big.bin").read_bytes() == data
