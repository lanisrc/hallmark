"""The object store: rsync-style temporary files, read-only objects, links."""

import errno
import hashlib
import os
import stat

import pytest

from hallmark import Repo
from hallmark.repo import objects as objects_module
from hallmark.repo.objects import Objects


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def published(store, data=b"payload", name="a.fits"):
    with store.temp_file(name) as temp:
        temp.write_bytes(data)
        return store.publish(temp, sha1_of(data))


def writable(path):
    return bool(os.stat(path).st_mode & stat.S_IWUSR)


def test_publish_names_the_object_by_content_and_leaves_no_temp(tmp_path):
    store = Objects(tmp_path)
    with store.temp_file("a.fits") as temp:
        assert temp.parent == tmp_path / "objects" / "tmp"
        assert temp.name.startswith(".a.fits.")
        temp.write_bytes(b"payload")
        target = store.publish(temp, sha1_of(b"payload"))
    digest = sha1_of(b"payload")
    assert target == tmp_path / "objects" / digest[:2] / digest[2:]
    assert target.read_bytes() == b"payload"
    assert not writable(target)
    assert list(store.tmp.iterdir()) == []
    with pytest.raises(ValueError):
        store.object_path("not-a-sha1")


def test_republishing_keeps_the_existing_object_and_its_links(tmp_path):
    store = Objects(tmp_path)
    target = published(store)
    linked = tmp_path / "linked"
    store.materialize(sha1_of(b"payload"), linked)
    inode = target.stat().st_ino
    assert published(store) == target
    assert target.stat().st_ino == inode
    assert store.is_linked(sha1_of(b"payload"), linked)


def test_an_object_edited_through_a_link_is_replaced(tmp_path):
    store = Objects(tmp_path)
    target = published(store)
    os.chmod(target, 0o600)
    target.write_bytes(b"PAYLOAD")
    published(store)
    assert target.read_bytes() == b"payload"
    assert not writable(target)


def test_materialize_links_read_only_and_never_replaces(tmp_path):
    store = Objects(tmp_path)
    target = published(store)
    destination = tmp_path / "work" / "a.fits"
    destination.parent.mkdir()
    assert store.materialize(sha1_of(b"payload"), destination) == "linked"
    assert destination.stat().st_ino == target.stat().st_ino
    assert target.stat().st_nlink == 2
    assert not writable(destination)
    assert store.is_linked(sha1_of(b"payload"), destination)
    with pytest.raises(FileExistsError):
        store.materialize(sha1_of(b"payload"), destination)
    with pytest.raises(FileNotFoundError):
        store.materialize("0" * 40, tmp_path / "work" / "missing")


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM, errno.EMLINK,
                                  errno.EOPNOTSUPP])
def test_materialize_copies_where_links_are_unavailable(tmp_path, monkeypatch,
                                                        code):
    store = Objects(tmp_path)
    target = published(store)
    real_link = os.link

    def link(source, destination):
        # Publishing into the store still links; only the worktree refuses.
        if "work" in str(destination):
            raise OSError(code, os.strerror(code))
        return real_link(source, destination)

    monkeypatch.setattr(objects_module.os, "link", link)
    monkeypatch.setattr("hallmark.utils.os.link", link)
    destination = tmp_path / "work" / "a.fits"
    destination.parent.mkdir()
    assert store.materialize(sha1_of(b"payload"), destination) == "copied"
    assert destination.read_bytes() == b"payload"
    assert destination.stat().st_ino != target.stat().st_ino
    assert writable(destination)
    assert not store.is_linked(sha1_of(b"payload"), destination)
    with pytest.raises(FileExistsError):
        store.materialize(sha1_of(b"payload"), destination)
    assert sorted(path.name for path in destination.parent.iterdir()) == ["a.fits"]


def test_windows_copies_instead_of_linking(tmp_path, monkeypatch):
    monkeypatch.setattr(objects_module, "LINK_INTO_WORKTREE", False)
    store = Objects(tmp_path)
    published(store)
    destination = tmp_path / "a.fits"
    assert store.materialize(sha1_of(b"payload"), destination) == "copied"
    assert writable(destination)


def test_only_a_hard_link_to_the_object_counts_as_linked(tmp_path):
    store = Objects(tmp_path)
    target = published(store)
    digest = sha1_of(b"payload")
    copy = tmp_path / "copy"
    copy.write_bytes(b"payload")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target)
    assert not store.is_linked(digest, copy)
    assert not store.is_linked(digest, symlink)
    assert not store.is_linked(digest, tmp_path / "missing")
    assert not store.is_linked("0" * 40, copy)


def test_only_stale_temporary_files_are_swept(tmp_path):
    store = Objects(tmp_path)
    assert store.cleanup_stale_temps() == 0
    store.tmp.mkdir(parents=True)
    old, fresh = store.tmp / ".old.fits.x", store.tmp / ".new.fits.y"
    old.write_bytes(b"partial")
    fresh.write_bytes(b"partial")
    day_ago = old.stat().st_mtime - objects_module.STALE_TEMP_SECONDS - 60
    os.utime(old, (day_ago, day_ago))
    assert store.cleanup_stale_temps() == 1
    assert sorted(path.name for path in store.tmp.iterdir()) == [".new.fits.y"]


def test_linked_worktrees_share_one_store(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    assert repo.objects.root == repo.dothm.path / "objects"
    (repo.worktree / "data_1.txt").write_text("one\n")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.create_branch("experiment")
    repo.add_worktree("experiment")
    linked = Repo(tmp_path / "experiment")
    assert linked.objects.root.resolve() == repo.objects.root.resolve()


def test_stored_objects_are_read_only_and_restore_writable(tmp_path):
    store = Objects(tmp_path)
    source = tmp_path / "local.txt"
    source.write_bytes(b"payload")
    stored = store.store(source, sha1_of(b"payload"))
    assert not writable(stored)
    restored = store.restore(sha1_of(b"payload"), tmp_path / "work" / "local.txt")
    assert restored.read_bytes() == b"payload"
    assert writable(restored)
    assert restored.stat().st_ino != stored.stat().st_ino


def test_committed_and_downloaded_content_share_one_object(tmp_path):
    store = Objects(tmp_path)
    downloaded = published(store)
    source = tmp_path / "local.txt"
    source.write_bytes(b"payload")
    assert store.store(source, sha1_of(b"payload")) == downloaded
    assert downloaded.stat().st_nlink == 1
    assert len([path for path in store.root.rglob("*") if path.is_file()]) == 1


def test_storing_replaces_an_object_edited_in_place(tmp_path):
    store = Objects(tmp_path)
    target = published(store)
    os.chmod(target, 0o600)
    target.write_bytes(b"PAYLOAD")
    source = tmp_path / "local.txt"
    source.write_bytes(b"payload")
    assert store.store(source, sha1_of(b"payload")) == target
    assert target.read_bytes() == b"payload"
    assert not writable(target)


def test_checkout_restores_local_files_as_writable_copies(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    path = repo.worktree / "data_1.txt"
    path.write_text("first\n")
    repo.add("data_{number}.txt")
    repo.commit("first")
    repo.create_branch("second")
    repo.checkout("second")
    path.write_text("second\n")
    repo.add("data_1.txt")
    repo.commit("second")
    repo.checkout("main")
    assert path.read_text() == "first\n"
    assert writable(path)
    assert path.stat().st_ino != repo.objects.object_path(
        sha1_of(b"first\n")).stat().st_ino
    assert not writable(repo.objects.object_path(sha1_of(b"first\n")))
