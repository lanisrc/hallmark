"""Test how existing repositories are located and opened."""

from pathlib import Path

import pytest
from git.exc import GitError

from hallmark import Repo
from hallmark.error import DothmError


def test_find_walks_parent_folders(tmp_path):
    repo = Repo.init(tmp_path / "project")
    nested = repo.worktree / "sub" / "dir"
    nested.mkdir(parents=True)
    for start in (repo.worktree, repo.worktree / "sub", nested):
        found = Repo.find(start)
        assert found.worktree == repo.worktree
        assert found.dothm.path == repo.dothm.path


def test_find_prefers_the_nearest_repository(tmp_path):
    outer = Repo.init(tmp_path / "outer")
    inner = Repo.init(outer.worktree / "inner")
    (inner.worktree / "data").mkdir()
    assert Repo.find(inner.worktree / "data").worktree == inner.worktree
    assert Repo.find(outer.worktree).worktree == outer.worktree


def test_find_opens_bare_repositories(tmp_path):
    bare = Repo.init(tmp_path / "catalog.hm")
    found = Repo.find(bare.dothm.path)
    assert found.worktree is None
    assert found.dothm.path == bare.dothm.path


def test_find_explains_missing_repository(tmp_path):
    folder = tmp_path / "plain" / "folder"
    folder.mkdir(parents=True)
    with pytest.raises(DothmError) as error:
        Repo.find(folder)
    message = str(error.value)
    assert "Not a Hallmark repository (or any parent folder)" in message
    assert "hm init" in message and "hm clone" in message
    assert not (tmp_path / "plain" / ".hm").exists()


def test_repo_path_stays_exact(tmp_path):
    repo = Repo.init(tmp_path / "project")
    (repo.worktree / "sub").mkdir()
    with pytest.raises(GitError):
        Repo(repo.worktree / "sub")
    assert not (repo.worktree / "sub" / ".hm").exists()
    assert Path(Repo(repo.worktree).worktree) == repo.worktree
