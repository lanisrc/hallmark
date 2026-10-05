"""Seeded random operation sequences checked against repository invariants.

A "monkey" edits files, stages, commits, switches branches and downloads
while faults are injected at random. It does not model every rule;
instead it checks properties that must always hold:

* operations succeed or raise a documented error;
* a failed operation changes neither the repository nor the worktree;
* after a checkout, every tracked file matches its catalogued checksum;
* the repository is never corrupt, and successful operations leave no
  temporary files;
* a downloaded file always equals a version the server published.

Set HALLMARK_CHAOS_SEED (comma-separated) and HALLMARK_CHAOS_STEPS to
explore further; nightly CI runs many more steps with fresh seeds.
"""

import errno
import hashlib
import os
import random

import pytest

from chaos import check_invariants, fail_nth_replace, snapshot_hm
from hallmark import Repo
from hallmark.error import CheckoutError
import hallmark.transport.http as http_transport
from hallmark.transport.base import DownloadError
from hallmark.repo.config import row_to_path
from http_server import (BitFlip, NoLengthTruncate, ResetAfter, ShortBody, Status,
                         write_file)

pytestmark = [pytest.mark.chaos, pytest.mark.usefixtures("loopback_only")]

SEEDS = [int(seed) for seed in
         os.environ.get("HALLMARK_CHAOS_SEED", "101,202,303").split(",") if seed]
STEPS = int(os.environ.get("HALLMARK_CHAOS_STEPS", "40"))
FAULT_RATE = float(os.environ.get("HALLMARK_CHAOS_FAULT_RATE", "0.2"))
FMT = "{site}/{year:d}/{day:d}.fits"
# Known bugs the monkey must not trip over until they are fixed:
# * Dothm.save_state is not atomic when a pattern change rewrites
#   config.yml (test_chaos_storage.py), so the pattern never changes here
#   and the add that first sets it runs without faults.
# * A truncated body without Content-Length is accepted when no checksum
#   is published (test_chaos_network.py), so every file has a checksum.
# * Checkout before the first commit raises a raw GitCommandError
#   (test_checkout_before_first_commit_reports_a_documented_error), so the
#   monkey commits once before switching branches.
EXPECTED_ERRORS = (CheckoutError, DownloadError, FileNotFoundError, OSError,
                   RuntimeError, ValueError)


class Monkey:
    """Seeded operation log with a reproducible failure report."""

    def __init__(self, seed, nodeid):
        self.seed, self.nodeid = seed, nodeid
        self.rng = random.Random(seed)
        self.log = []

    def fail(self, step, message):
        recent = "\n".join(f"  {line}" for line in self.log[-30:])
        pytest.fail(
            f"seed {self.seed}, step {step}: {message}\nLast operations:\n{recent}\n"
            f"Reproduce: HALLMARK_CHAOS_SEED={self.seed} "
            f"HALLMARK_CHAOS_STEPS={step + 1} pytest '{self.nodeid}'",
            pytrace=False)


def _worktree_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*.fits") if ".hm" not in path.parts}


def _tracked_files_match_catalog(repo):
    """Return paths whose bytes differ from the current branch's catalog."""
    fmt = repo.state.config["data"][0]["fmt"]
    wrong = []
    for _, row in repo.state.data.iterrows():
        path = repo.worktree / row_to_path(row, fmt)
        if (not path.is_file()
                or hashlib.sha1(path.read_bytes()).hexdigest() != row["sha1"]):
            wrong.append(str(path.relative_to(repo.worktree)))
    return wrong


@pytest.mark.parametrize("seed", SEEDS)
def test_local_monkey_keeps_repository_consistent(tmp_path, monkeypatch, request,
                                                   seed):
    monkey = Monkey(seed, request.node.nodeid)
    rng = monkey.rng
    root = tmp_path / "obs"
    Repo.init(root)
    branches = {"main"}
    patterned = committed = False

    for step in range(STEPS):
        repo = Repo(root)
        name = rng.choice(["write", "write", "delete", "add", "add", "commit",
                           "commit", "checkout"])
        if name == "checkout" and not committed:
            name = "commit"
        path = f"{rng.choice(['ALMA', 'SMA'])}/{rng.choice([2024, 2025])}/" \
               f"{rng.randrange(1, 4)}.fits"
        target = rng.choice(sorted(branches) + [f"b{step}"])
        fault = None
        if name in {"add", "commit", "checkout"} and rng.random() < FAULT_RATE:
            fault = (rng.randrange(1, 4), rng.choice([errno.ENOSPC, errno.EIO]))
            if name == "add" and not patterned:
                fault = None
        monkey.log.append(f"{step}: {name} path={path} target={target} "
                          f"fault={fault}")

        if name == "write":
            write_file(root, path, rng.randbytes(rng.randrange(1, 200)))
            continue
        if name == "delete":
            files = sorted(_worktree_files(root))
            if files:
                (root / rng.choice(files)).unlink()
            continue

        before, before_files = snapshot_hm(root), _worktree_files(root)
        if fault is not None:
            fail_nth_replace(monkeypatch, fault[0], code=fault[1])
        try:
            if name == "add":
                repo.add(FMT if not patterned else ".")
                patterned = True
            elif name == "commit":
                committed = repo.commit(f"step {step}") or committed
            else:
                repo.checkout(target)
        except EXPECTED_ERRORS as exc:
            monkeypatch.undo()
            monkey.log[-1] += f" -> {type(exc).__name__}: {exc}"
            after = snapshot_hm(root)
            if name == "commit":
                # Objects stored before the failure are harmless extras.
                unchanged = after["refs"] == before["refs"] and \
                    after["files"] == before["files"]
            else:
                unchanged = after == before
            if not unchanged:
                monkey.fail(step, f"failed {name} changed repository state")
            if _worktree_files(root) != before_files and name != "commit":
                monkey.fail(step, f"failed {name} changed worktree files")
            problems = check_invariants(root, fsck=False)
            if problems.corruption:
                monkey.fail(step, str(problems))
            continue
        except Exception as exc:
            monkeypatch.undo()
            monkey.fail(step, f"undocumented {type(exc).__name__}: {exc}")
        monkeypatch.undo()
        monkey.log[-1] += " -> ok"
        if name == "checkout":
            branches.add(target)
            wrong = _tracked_files_match_catalog(Repo(root))
            if wrong:
                monkey.fail(step, f"checkout left wrong tracked files: {wrong}")
        problems = check_invariants(root, fsck=step % 10 == 9)
        if problems:
            monkey.fail(step, str(problems))
    problems = check_invariants(root)
    if problems.corruption:
        monkey.fail(STEPS, str(problems))


@pytest.mark.xfail(strict=True, reason=(
    "Bug: before the first commit, checkout raises GitCommandError because "
    "init leaves data.tsv uncommitted"))
def test_checkout_before_first_commit_reports_a_documented_error(tmp_path):
    repo = Repo.init(tmp_path / "obs")
    try:
        repo.checkout("main")
    except EXPECTED_ERRORS:
        pass


@pytest.mark.parametrize("seed", SEEDS)
def test_remote_monkey_never_lands_unpublished_bytes(tmp_path, monkeypatch,
                                                      http_server, request, seed):
    monkeypatch.setattr(http_transport, "REMOTE_REQUEST_TIMEOUT", (2, 0.5))
    monkey = Monkey(seed, request.node.nodeid)
    rng = monkey.rng
    www = tmp_path / "www"
    names = [f"d{i}/f{j}.bin" for i in range(2) for j in range(3)]
    published = {}

    def write_manifest(directory):
        write_file(www, f"rel/{directory}/sha256sum.txt", "".join(
            f"{hashlib.sha256((www / 'rel' / n).read_bytes()).hexdigest()}  "
            f"{n.split('/')[1]}\n" for n in names if n.startswith(directory + "/")))

    def publish(name, stale=False):
        data = rng.randbytes(rng.randrange(1000, 20000))
        write_file(www, f"rel/{name}", data)
        published.setdefault(name, set()).add(hashlib.sha256(data).hexdigest())
        if not stale:
            write_manifest(name.split("/")[0])

    for name in names:
        publish(name, stale=True)
    for directory in ("d0", "d1"):
        write_manifest(directory)
    server = http_server(www)
    url = server.url("rel/") + "{d}/{f}.bin"
    repo = Repo.init(tmp_path / "repo")
    repo.add(url)
    repo.commit("Catalog")
    faults = [ResetAfter(100), ShortBody(100), Status(500), Status(404),
              BitFlip(7), NoLengthTruncate(100)]

    for step in range(STEPS):
        name = rng.choice(["publish", "publish-stale", "rescan", "commit",
                           "download", "download", "delete"])
        target = rng.choice(names)
        monkey.log.append(f"{step}: {name} {target}")
        try:
            if name == "publish":
                publish(target)
            elif name == "publish-stale":
                publish(target, stale=True)
            elif name == "rescan":
                repo.add(url)
            elif name == "commit":
                repo.commit(f"step {step}")
            elif name == "delete":
                (repo.worktree / target).unlink(missing_ok=True)
            else:
                if rng.random() < 0.5:
                    server.faults.add(f"rel/{target}", rng.choice(faults))
                result = repo.download(repo.plan_download(all_files=True),
                                       approved=True, max_workers=3)
                monkey.log[-1] += f" -> {result['succeeded']} ok, " \
                                  f"{result['failed']} failed"
        except EXPECTED_ERRORS as exc:
            monkey.log[-1] += f" -> {type(exc).__name__}: {exc}"
        except Exception as exc:
            monkey.fail(step, f"undocumented {type(exc).__name__}: {exc}")
        finally:
            server.faults.clear()
            repo = Repo(repo.worktree)

        for name_ in names:
            path = repo.worktree / name_
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() \
                    not in published[name_]:
                monkey.fail(step, f"{name_} holds bytes the server never published")
        problems = check_invariants(repo.worktree, fsck=False)
        if problems.corruption or (name.startswith("download") and problems.debris):
            monkey.fail(step, str(problems))
