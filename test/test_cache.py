"""The download cache: rsync-style temporary files, read-only objects, links."""

import errno
import hashlib
import os
import stat

import pytest

from hallmark import Repo
from hallmark.repo import cache as cache_module
from hallmark.repo.cache import Cache


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def cached(cache, data=b"payload", name="a.fits"):
    with cache.temp_file(name) as temp:
        temp.write_bytes(data)
        return cache.publish(temp, sha1_of(data))


def writable(path):
    return bool(os.stat(path).st_mode & stat.S_IWUSR)


def test_publish_names_the_object_by_content_and_leaves_no_temp(tmp_path):
    cache = Cache(tmp_path)
    with cache.temp_file("a.fits") as temp:
        assert temp.parent == tmp_path / "cache" / "tmp"
        assert temp.name.startswith(".a.fits.")
        temp.write_bytes(b"payload")
        target = cache.publish(temp, sha1_of(b"payload"))
    digest = sha1_of(b"payload")
    assert target == tmp_path / "cache" / digest[:2] / digest[2:]
    assert target.read_bytes() == b"payload"
    assert not writable(target)
    assert list(cache.tmp.iterdir()) == []
    with pytest.raises(ValueError):
        cache.object_path("not-a-sha1")


def test_republishing_keeps_the_existing_object_and_its_links(tmp_path):
    cache = Cache(tmp_path)
    target = cached(cache)
    linked = tmp_path / "linked"
    cache.materialize(sha1_of(b"payload"), linked)
    inode = target.stat().st_ino
    assert cached(cache) == target
    assert target.stat().st_ino == inode
    assert cache.is_linked(sha1_of(b"payload"), linked)


def test_an_object_edited_through_a_link_is_replaced(tmp_path):
    cache = Cache(tmp_path)
    target = cached(cache)
    os.chmod(target, 0o600)
    target.write_bytes(b"PAYLOAD")
    cached(cache)
    assert target.read_bytes() == b"payload"
    assert not writable(target)


def test_materialize_links_read_only_and_never_replaces(tmp_path):
    cache = Cache(tmp_path)
    target = cached(cache)
    destination = tmp_path / "work" / "a.fits"
    destination.parent.mkdir()
    assert cache.materialize(sha1_of(b"payload"), destination) == "linked"
    assert destination.stat().st_ino == target.stat().st_ino
    assert target.stat().st_nlink == 2
    assert not writable(destination)
    assert cache.is_linked(sha1_of(b"payload"), destination)
    with pytest.raises(FileExistsError):
        cache.materialize(sha1_of(b"payload"), destination)
    with pytest.raises(FileNotFoundError):
        cache.materialize("0" * 40, tmp_path / "work" / "missing")


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM, errno.EMLINK,
                                  errno.EOPNOTSUPP])
def test_materialize_copies_where_links_are_unavailable(tmp_path, monkeypatch,
                                                        code):
    cache = Cache(tmp_path)
    target = cached(cache)
    real_link = os.link

    def link(source, destination):
        # Publishing into the cache still links; only the worktree refuses.
        if "work" in str(destination):
            raise OSError(code, os.strerror(code))
        return real_link(source, destination)

    monkeypatch.setattr(cache_module.os, "link", link)
    monkeypatch.setattr("hallmark.utils.os.link", link)
    destination = tmp_path / "work" / "a.fits"
    destination.parent.mkdir()
    assert cache.materialize(sha1_of(b"payload"), destination) == "copied"
    assert destination.read_bytes() == b"payload"
    assert destination.stat().st_ino != target.stat().st_ino
    assert writable(destination)
    assert not cache.is_linked(sha1_of(b"payload"), destination)
    with pytest.raises(FileExistsError):
        cache.materialize(sha1_of(b"payload"), destination)
    assert sorted(path.name for path in destination.parent.iterdir()) == ["a.fits"]


def test_windows_copies_instead_of_linking(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "LINK_INTO_WORKTREE", False)
    cache = Cache(tmp_path)
    cached(cache)
    destination = tmp_path / "a.fits"
    assert cache.materialize(sha1_of(b"payload"), destination) == "copied"
    assert writable(destination)


def test_only_a_hard_link_to_the_object_counts_as_linked(tmp_path):
    cache = Cache(tmp_path)
    target = cached(cache)
    digest = sha1_of(b"payload")
    copy = tmp_path / "copy"
    copy.write_bytes(b"payload")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target)
    assert not cache.is_linked(digest, copy)
    assert not cache.is_linked(digest, symlink)
    assert not cache.is_linked(digest, tmp_path / "missing")
    assert not cache.is_linked("0" * 40, copy)


def test_only_stale_temporary_files_are_swept(tmp_path):
    cache = Cache(tmp_path)
    assert cache.cleanup_stale_temps() == 0
    cache.tmp.mkdir(parents=True)
    old, fresh = cache.tmp / ".old.fits.x", cache.tmp / ".new.fits.y"
    old.write_bytes(b"partial")
    fresh.write_bytes(b"partial")
    day_ago = old.stat().st_mtime - cache_module.STALE_TEMP_SECONDS - 60
    os.utime(old, (day_ago, day_ago))
    assert cache.cleanup_stale_temps() == 1
    assert sorted(path.name for path in cache.tmp.iterdir()) == [".new.fits.y"]


def test_repositories_share_one_cache_and_create_it_lazily(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    assert repo.cache.root == repo.dothm.path / "cache"
    assert not repo.cache.root.exists()
    (repo.worktree / "data_1.txt").write_text("one\n")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.create_branch("experiment")
    repo.add_worktree("experiment")
    linked = Repo(tmp_path / "experiment")
    assert linked.cache.root == repo.cache.root
    assert not repo.cache.root.exists()
