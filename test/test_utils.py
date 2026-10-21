import errno
import hashlib
import os

import pytest

from hallmark import utils
from hallmark.utils import (
    calculate_file_checksum,
    calculate_file_checksums,
    publish_no_overwrite,
    replace_file_on_success,
)


def test_several_checksums_match_single_ones_in_one_read(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(os.urandom(3000))
    opened = []
    original = type(path).open

    def counting_open(self, *args, **kwargs):
        opened.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", counting_open)
    digests = calculate_file_checksums(path, ["sha1", "SHA256 ", "md5"], chunk_size=512)
    assert opened == [path]
    assert digests == {name: hashlib.new(name, path.read_bytes()).hexdigest()
                       for name in ("sha1", "sha256", "md5")}
    assert digests["sha1"] == calculate_file_checksum(path)
    with pytest.raises(ValueError):
        calculate_file_checksums(path, ["no-such-hash"])


def test_publish_refuses_an_existing_target(tmp_path):
    source, target = tmp_path / ".a.tmp", tmp_path / "a"
    source.write_text("new")
    target.write_text("old")
    with pytest.raises(FileExistsError):
        publish_no_overwrite(source, target)
    assert target.read_text() == "old"
    target.unlink()
    publish_no_overwrite(source, target)
    assert target.read_text() == "new"
    # A hard link leaves the temporary name for the caller to remove.
    assert source.exists()


@pytest.mark.parametrize("code", [errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK])
def test_publish_renames_where_hard_links_fail(tmp_path, monkeypatch, code):
    def refuse(*args):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(utils.os, "link", refuse)
    source, target = tmp_path / ".a.tmp", tmp_path / "a"
    source.write_text("new")
    publish_no_overwrite(source, target)
    assert target.read_text() == "new"
    assert not source.exists()
    source.write_text("newer")
    with pytest.raises(FileExistsError):
        publish_no_overwrite(source, target)
    assert target.read_text() == "new"


def test_other_link_errors_are_not_hidden(tmp_path, monkeypatch):
    def refuse(*args):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(utils.os, "link", refuse)
    with pytest.raises(PermissionError):
        publish_no_overwrite(tmp_path / "a", tmp_path / "b")


def test_no_overwrite_replacement_keeps_existing_files(tmp_path, monkeypatch):
    target = tmp_path / "a"
    with replace_file_on_success(target, overwrite=False) as temp:
        temp.write_text("first")
    assert target.read_text() == "first"
    with pytest.raises(FileExistsError):
        with replace_file_on_success(target, overwrite=False) as temp:
            temp.write_text("second")
    assert target.read_text() == "first"
    monkeypatch.setattr(utils.os, "link",
                        lambda *a: (_ for _ in ()).throw(OSError(errno.EPERM, "")))
    other = tmp_path / "b"
    with replace_file_on_success(other, overwrite=False) as temp:
        temp.write_text("renamed")
    assert other.read_text() == "renamed"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a", "b"]
