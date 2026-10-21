"""Edge cases of the path, checksum and encoding helpers in hallmark.utils."""

import hashlib
import os
from pathlib import Path

import pytest

from hallmark import utils
from hallmark.utils import (
    apply_regex_replacement,
    calculate_file_checksum,
    convert_format_value,
    is_valid_checksum,
    iter_repository_files,
    resolve_path_in_root,
    validate_path_name,
    validate_relative_path,
)


### checksums ###

@pytest.mark.parametrize("algorithm, checksum", [
    (None, "a" * 40),
    ("sha1", None),
    ("sha1", 0x1234),
    (b"sha1", "a" * 40),
])
def test_checksum_values_that_are_not_strings_are_invalid(algorithm, checksum):
    assert is_valid_checksum(algorithm, checksum) is False
    assert is_valid_checksum(algorithm, checksum,
                             allow_unknown_algorithm=True) is False


@pytest.mark.parametrize("size", [0, 1, 7, 50])
def test_file_checksum_reads_in_chunks_without_file_digest(tmp_path, monkeypatch,
                                                           size):
    # hashlib.file_digest is new in Python 3.11; older versions read in chunks.
    monkeypatch.delattr(utils.hashlib, "file_digest", raising=False)
    path = tmp_path / "data.bin"
    data = bytes(range(size))
    path.write_bytes(data)
    assert calculate_file_checksum(path, "SHA256 ", chunk_size=7) == \
        hashlib.sha256(data).hexdigest()
    assert calculate_file_checksum(str(path)) == hashlib.sha1(data).hexdigest()


def test_file_checksum_without_file_digest_rejects_unknown_algorithms(
        tmp_path, monkeypatch):
    monkeypatch.delattr(utils.hashlib, "file_digest", raising=False)
    path = tmp_path / "data.bin"
    path.write_bytes(b"data")
    with pytest.raises(ValueError, match="no-such-hash"):
        calculate_file_checksum(path, "no-such-hash")


### repository files ###

def test_repository_files_leave_out_git_and_hm_files(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    # A linked worktree or submodule has a .git file instead of a folder.
    (tmp_path / ".git").write_text("gitdir: ../elsewhere\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / "sub" / ".HM").write_text("not data")
    (tmp_path / ".hm").mkdir()
    (tmp_path / ".hm" / "config.yml").write_text("data: []\n")
    found = sorted(path.relative_to(tmp_path).as_posix()
                   for path in iter_repository_files(tmp_path))
    assert found == ["a.txt", "sub/b.txt"]


### encodings and format values ###

@pytest.mark.parametrize("spec", [
    None,
    {"fmt": "a{a}_i{i}.h5"},
    {"fmt": "a{a}_i{i}.h5", "encoding": {}},
    {"fmt": "a{a}_i{i}.h5", "encoding": {"aspin": ""}},
])
def test_regex_replacement_without_a_regex_keeps_the_value(spec):
    assert apply_regex_replacement("am0.5_i30.h5", spec) == "am0.5_i30.h5"


def test_regex_replacement_prefixes_the_captured_group_with_a_hyphen():
    spec = {"encoding": {"aspin": r"am([0-9.]+)"}}
    assert apply_regex_replacement("am0.5_i30.h5", spec) == "-0.5_i30.h5"


@pytest.mark.parametrize("value, spec, expected", [
    ("7", "", "7"),
    ("7", None, "7"),
    ("7.0", "03d", 7),
    ("2.5", ".2f", 2.5),
    ("2.5", "F", 2.5),
    ("1e3", "e", 1000.0),
    ("1.5E2", "G", 150.0),
    ("M87", "s", "M87"),
    ("M87", ">5", "M87"),
])
def test_format_values_are_converted_by_their_spec_type(value, spec, expected):
    converted = convert_format_value(value, spec)
    assert converted == expected
    assert type(converted) is type(expected)


### path validation ###

def test_relative_paths_with_a_null_byte_are_rejected():
    with pytest.raises(ValueError, match="data path contains a null byte"):
        validate_relative_path("run/a\x00.fits", label="data path")


@pytest.mark.parametrize("value", [None, 3, ["a"], b"name"])
def test_path_names_must_be_strings(value):
    with pytest.raises(ValueError, match="^tsv name must be a string$"):
        validate_path_name(value, label="tsv name")


def test_path_names_accept_a_single_component_as_a_path():
    assert validate_path_name(Path("data.tsv")) == "data.tsv"
    assert validate_path_name("  data.tsv ") == "data.tsv"


def test_a_symlink_missed_while_walking_still_cannot_escape_root(tmp_path,
                                                                 monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "escape")
    # The walk checks each part for a link; a link made after that check, as
    # in a race, is still caught when the final path is resolved.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    with pytest.raises(ValueError, match="resolves outside its root: escape/a"):
        resolve_path_in_root(root, "escape/a.txt")
    assert resolve_path_in_root(root, "inside/a.txt") == \
        root.resolve() / "inside" / "a.txt"
