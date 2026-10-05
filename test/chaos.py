"""Helpers for fault-injection ("chaos monkey") tests.

``check_invariants`` describes what must hold for a repository after any
operation, failed or not. Problems are grouped by severity:

* corruption: the repository cannot be opened, an object does not match
  its checksum, a committed file has no object, or Git reports damage;
* debris: temporary files that a finished operation left behind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import hashlib
from io import StringIO
import os
import pathlib
from pathlib import Path
import subprocess

import pandas as pd
import pytest

from hallmark import Repo

STATE_FILES = ("config.yml", "meta.yml", "data.tsv")
DEBRIS_PATTERNS = ("**/.*.part", "**/.*.tmp", "**/.hallmark-checkout-*")


@dataclass
class Violations:
    """Invariant violations found in a repository."""
    corruption: list = field(default_factory=list)
    debris: list = field(default_factory=list)

    def __bool__(self):
        return bool(self.corruption or self.debris)

    def __str__(self):
        lines = [f"corruption: {item}" for item in self.corruption]
        lines += [f"debris: {item}" for item in self.debris]
        return "\n".join(lines) or "no violations"


def skip_if_root():
    """Skip permission tests when running as root, which ignores modes."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")


def fail_nth_replace(monkeypatch, nth, *, code=errno.ENOSPC, match=lambda target: True):
    """Make the ``nth`` matching ``Path.replace`` call raise ``OSError(code)``.

    Patch the method on the class: on Python 3.9 and 3.10, pathlib calls a
    reference to ``os.replace`` captured at import, so patching ``os`` misses it.

    Returns:
        list: Targets of the matching calls, in order, for assertions.
    """
    original = pathlib.Path.replace
    calls = []

    def replace(self, target):
        if match(Path(target)):
            calls.append(Path(target))
            if len(calls) == nth:
                raise OSError(code, os.strerror(code), str(target))
        return original(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", replace)
    return calls


class _FailingWriter:
    """File proxy whose writes fail once ``limit`` bytes have been written."""

    def __init__(self, handle, limit, code):
        self._handle, self._limit, self._code = handle, limit, code
        self._written = 0

    def write(self, data):
        if self._written + len(data) > self._limit:
            raise OSError(self._code, os.strerror(self._code))
        self._written += len(data)
        return self._handle.write(data)

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)


def fail_writes_after(monkeypatch, limit, *, code=errno.ENOSPC,
                      match=lambda path: True):
    """Make binary writes to matching paths fail after ``limit`` bytes."""
    original = pathlib.Path.open

    def open_(self, mode="r", *args, **kwargs):
        handle = original(self, mode, *args, **kwargs)
        if "w" in mode and "b" in mode and match(self):
            return _FailingWriter(handle, limit, code)
        return handle

    monkeypatch.setattr(pathlib.Path, "open", open_)


def _git(dothm, *args):
    return subprocess.run(["git", "-C", str(dothm), *args], check=True,
                          capture_output=True, text=True).stdout


def snapshot_hm(repo_path):
    """Capture a repository's tracked state: state files, index and refs."""
    repo_path = Path(repo_path)
    dothm = repo_path if repo_path.name.endswith(".hm") else repo_path / ".hm"
    return {
        "files": {name: (dothm / name).read_bytes() for name in STATE_FILES
                  if (dothm / name).is_file()},
        "index": _git(dothm, "ls-files", "--stage"),
        "refs": _git(dothm, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": subprocess.run(["git", "-C", str(dothm), "symbolic-ref", "-q", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        or _git(dothm, "rev-parse", "HEAD").strip(),
    }


def check_invariants(repo_path, *, fsck=True):
    """Check a repository for corruption and leftover temporary files.

    Args:
        repo_path: Worktree or bare ``.hm`` path.
        fsck (bool): Also run ``git fsck``, which is slower.

    Returns:
        Violations: Empty (falsy) when every invariant holds.
    """
    found = Violations()
    try:
        repo = Repo(repo_path)
        repo.status()
    except Exception as exc:
        found.corruption.append(
            f"repository does not open: {type(exc).__name__}: {exc}")
        return found

    objects = repo.objects.root
    if objects.is_dir():
        for path in objects.glob("??/*"):
            if path.name.startswith("."):
                found.debris.append(str(path))
                continue
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
            if digest != path.parent.name + path.name:
                found.corruption.append(f"object {path} has checksum {digest}")

    dothm = Path(repo.dothm.path)
    for head in repo.dothm.heads:
        try:
            text = _git(dothm, "show", f"{head.name}:data.tsv")
        except subprocess.CalledProcessError:
            continue
        frame = pd.read_csv(StringIO(text), sep="\t", dtype=str,
                            keep_default_na=False)
        if "sha1" in frame.columns:
            for sha1 in frame.sha1:
                if sha1 and not repo.objects.contains(sha1):
                    found.corruption.append(
                        f"branch {head.name} references missing object {sha1}")

    if fsck:
        result = subprocess.run(["git", "-C", str(dothm), "fsck", "--no-dangling"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            found.corruption.append(f"git fsck: {result.stdout}{result.stderr}")

    roots = {Path(repo_path)}
    if repo.worktree is not None:
        roots.add(Path(repo.worktree))
    for root in roots:
        for pattern in DEBRIS_PATTERNS:
            found.debris.extend(str(path) for path in root.glob(pattern))
    found.debris = sorted(set(found.debris))
    return found
