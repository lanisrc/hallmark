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


def _snapshot(root):
    """Return the bytes of every file below root, keyed by relative path."""
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(Path(root).rglob("*")) if path.is_file()}


def _committed_local_repo(path):
    repo = Repo.init(path)
    (repo.worktree / "run1.txt").write_text("one")
    repo.add("run{run:d}.txt")
    repo.commit("Record run")
    return repo


DAMAGE = {
    "missing data.tsv": (lambda hm: (hm / "data.tsv").unlink(), "data.tsv"),
    "missing config.yml": (lambda hm: (hm / "config.yml").unlink(), "config.yml"),
    "missing meta.yml": (lambda hm: (hm / "meta.yml").unlink(), "meta.yml"),
    "invalid config.yml": (
        lambda hm: (hm / "config.yml").write_text("data: [\n"), "config.yml"),
    "meta.yml list": (lambda hm: (hm / "meta.yml").write_text("- a\n"), "meta.yml"),
    "ragged data.tsv": (
        lambda hm: (hm / "data.tsv").write_text("sha1\trun\nx\t1\na\tb\tc\n"),
        "data.tsv"),
    "binary data.tsv": (
        lambda hm: (hm / "data.tsv").write_bytes(b"sha1\n\xff\xfe\n"), "data.tsv"),
    "empty data.tsv": (lambda hm: (hm / "data.tsv").write_bytes(b""), "data.tsv"),
    "not a Git worktree": (
        lambda hm: __import__("shutil").rmtree(hm / ".git"), "Git"),
    "dangling .hm link": (
        lambda hm: (__import__("shutil").rmtree(hm),
                    hm.symlink_to(hm.parent / "moved-away")),
        "links to a folder that does not exist"),
}


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_damaged_repository_is_explained_and_left_unchanged(tmp_path, damage):
    repo = _committed_local_repo(tmp_path / "project")
    apply_damage, detail = DAMAGE[damage]
    apply_damage(repo.dothm.path)
    before = _snapshot(repo.worktree)
    with pytest.raises(DothmError) as error:
        Repo(repo.worktree)
    message = str(error.value)
    assert "is damaged" in message
    assert detail in message
    assert "nothing was changed" in message
    assert _snapshot(repo.worktree) == before


@pytest.mark.parametrize("damage", [
    "missing data.tsv", "invalid config.yml", "dangling .hm link"])
def test_cli_status_stops_on_damaged_repository(tmp_path, monkeypatch, damage):
    from click.testing import CliRunner
    from hallmark.cli import hallmark

    repo = _committed_local_repo(tmp_path / "project")
    DAMAGE[damage][0](repo.dothm.path)
    (repo.worktree / "sub").mkdir()
    monkeypatch.chdir(repo.worktree / "sub")
    before = _snapshot(repo.worktree)
    result = CliRunner().invoke(hallmark, ["status"])
    assert result.exit_code == 1
    assert "is damaged" in result.output
    assert DAMAGE[damage][1] in result.output
    assert "Traceback" not in result.output
    assert _snapshot(repo.worktree) == before


def test_opening_a_linked_worktree_writes_nothing(tmp_path):
    repo = _committed_local_repo(tmp_path / "project")
    repo.add_worktree("experiment")
    linked = tmp_path / "experiment"
    objects = linked / ".hm" / "objects"
    assert objects.is_symlink()
    assert objects.resolve() == (repo.dothm.path / "objects").resolve()
    objects.unlink()
    opened = Repo(linked)
    assert not objects.exists() and not objects.is_symlink()
    assert opened.objects.contains(opened.state.data.iloc[0]["sha1"])
