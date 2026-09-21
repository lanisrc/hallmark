from pathlib import Path

import pandas as pd
import pytest
import os
from git import Repo as GitRepo
from git.exc import GitCommandError

from hallmark import Repo, ParaFrame
from hallmark.objects import Objects
from hallmark.state import State
from hallmark.repo_worktree import worktree_changes
from hallmark.dothm import Dothm
from hallmark.worktree import Worktree
from hallmark.helper_functions import (
    load_yaml,
    iter_repository_files,
    regex_sub)
from hallmark.error import (
    CheckoutError,
    DestinationExistsError,
    DothmError,
    CloneError)
from hallmark.repo_config import (
    row_to_path,
    fmt_entries_from_config,
    single_data_fmt,
    fmt_fields)
from hallmark.repo_manifest import (
    iter_manifest_entries,
    manifest_frame_from_pf,
    manifest_map)
from hallmark.repo_state import (
    _parse_data_tsv,
    load_branch_data,
    load_head_state)

### standard pf tests ###

### Ensure ParaFrame type
def test_standard_pf_is_paraframe(hallmark_test_suite_dictionary):
    assert isinstance(hallmark_test_suite_dictionary["standard_pf"], ParaFrame)

def test_encoded_pf_is_paraframe(hallmark_test_suite_dictionary):
    assert isinstance(hallmark_test_suite_dictionary["encoded_pf"], ParaFrame)

### Test Standard pf
def test_standard_pf_shape(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    assert pf.shape == (12, 3)

def test_standard_pf_column_names(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    assert set(pf.columns) == {"path", "a", "i"}

def test_standard_pf_column_value_types(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    assert pd.api.types.is_float_dtype(pf["a"])
    # i values are whole numbers so they convert to int64, not float64
    assert pd.api.types.is_numeric_dtype(pf["i"])

def test_standard_pf_values(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    assert set(pf["a"].unique()) == {0.0, 0.75, 0.975}
    assert set(pf["i"].unique()) == {0.0, 30.0, 60.0, 90.0}

def test_standard_pf_paths_match_created_files(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    assert sorted(pf["path"]) == sorted(
        hallmark_test_suite_dictionary["standard_files"])

def test_standard_pf_supports_pandas_methods(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    assert isinstance(pf.head(), pd.DataFrame)

def test_standard_glob_pattern_created_properly(hallmark_test_suite_dictionary):
    pattern = hallmark_test_suite_dictionary["standard_glob_pattern"].replace("\\", "/")
    assert pattern.endswith("/a*_i*.h5")

def test_standard_glob_returns_expected_files(hallmark_test_suite_dictionary):
    files = hallmark_test_suite_dictionary["standard_globbed_files"]
    assert len(files) == 12

def test_standard_pf_single_filter_argument(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    filtered = pf(a=0.75)
    assert len(filtered) == 4
    assert set(filtered["a"].unique()) == {0.75}

def test_standard_pf_filter_multiple_values(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    filtered = pf(a=[0.75, 0.975])
    assert len(filtered) == 8
    assert set(filtered["a"].unique()) == {0.75, 0.975}

def test_standard_pf_filter_multiple_conditions(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    filtered = pf(a=0.75)(i=0)
    assert len(filtered) == 1
    assert set(filtered["a"].unique()) == {0.75}
    assert set(filtered["i"].unique()) == {0}

### encoded pf tests ###

def test_encoded_pf_shape(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    assert pf.shape == (4, 3)

def test_encoded_pf_column_names(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    assert set(pf.columns) == {"path", "aspin", "i"}

def test_encoded_pf_has_custom_spin_type(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    assert pd.api.types.is_float_dtype(pf["aspin"])

def test_encoded_pf_spin_values(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    assert set(pf["aspin"].unique()) == {-0.5}

def test_encoded_pf_i_values(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    assert set(pf["i"].unique()) == {0.0, 30.0, 60.0, 90.0}

def test_encoded_pf_filter_single_value(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    filtered = pf(aspin=-0.5)
    assert len(filtered) == 4
    assert set(filtered["aspin"].unique()) == {-0.5}

def test_encoded_pf_filter_multiple_values(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    filtered = pf(aspin=[-0.5, 0.0])
    assert len(filtered) == 4
    assert set(filtered["aspin"].unique()) == {-0.5}

def test_encoded_pf_filter_multiple_conditions(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    filtered = pf(aspin=-0.5)(i=0)
    assert len(filtered) == 1
    assert set(filtered["aspin"].unique()) == {-0.5}
    assert set(filtered["i"].unique()) == {0}

def test_encoded_glob_pattern_created_properly(hallmark_test_suite_dictionary):
    pattern = hallmark_test_suite_dictionary["encoded_glob_pattern"].replace("\\", "/")
    assert pattern.endswith("/a*_i*.h5")

def test_encoded_glob_returns_expected_files(hallmark_test_suite_dictionary):
    files = hallmark_test_suite_dictionary["encoded_globbed_files"]
    assert len(files) == 4

# new tests for the two different subdirectories of encoded vs standard files
def test_encoded_pf_paths_are_in_encoded_subdirectory(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["encoded_pf"]
    assert all(path.startswith("encoded/") for path in pf["path"])

def test_standard_pf_not_in_encoded_subdir(hallmark_test_suite_dictionary):
    pf = hallmark_test_suite_dictionary["standard_pf"]
    assert not any(path.startswith("encoded/") for path in pf["path"])

def test_standard_and_encoded_no_overlap(hallmark_test_suite_dictionary):
    standard_paths = set(hallmark_test_suite_dictionary["standard_pf"]["path"])
    encoded_paths = set(hallmark_test_suite_dictionary["encoded_pf"]["path"])
    assert standard_paths.isdisjoint(encoded_paths)

### Test repo behavior
def test_repo_init_created_dot_hm(hallmark_test_suite_dictionary):
    repo_path = hallmark_test_suite_dictionary["repo_path"]
    assert (repo_path / ".hm").is_dir()


def test_repo_initializes_download_result(tmp_path):
    """
    Test that Repo.init() initializes the download_result attribute to None.
    This test creates a temporary directory, initializes a repository in it, and checks
    that the download_result attribute of the Repo instance is None after initialization
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")

    assert repo.download_result is None, \
        "Expected download_result to be None after initialization"

def test_repo_commit_succeeds(hallmark_test_suite_dictionary):
    assert hallmark_test_suite_dictionary["commit_result"] is True

def test_data_tsv_and_worktree_reconstruction(hallmark_test_suite_dictionary):
    repo = Repo(hallmark_test_suite_dictionary["repo_path"])
    assert len(repo.state.data) == 12
    assert repo.worktree.stem == "repo"


def _write_files(root, names):
    for name in names:
        (root / name).write_text(f"{name}\n", encoding="utf-8")


### Repo.add tests ###

def test_repo_add_result_has_expected_length(hallmark_test_suite_dictionary):
    result = hallmark_test_suite_dictionary["add_result"]
    assert len(result) == 12


def test_repo_add_result_paths_match_standard_files(hallmark_test_suite_dictionary):
    result = hallmark_test_suite_dictionary["add_result"]
    assert sorted(result["path"]) == sorted(
        hallmark_test_suite_dictionary["standard_files"])


def test_repo_add_rejects_symlink_escape(tmp_path):
    """
    Test that adding a symlink that points outside the worktree raises a ValueError.
    This test creates a symlink inside the repository that points to a file outside
    the repository, and then attempts to add it using the Repo.add() method. The test
    should raise a ValueError.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If the symlink points outside the repository worktree.
    """
    repo = Repo.init(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_files(outside, ["a0_i0.h5"])
    link = repo.worktree / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")

    with pytest.raises(ValueError, match="symbolic link"):
        repo.add("linked/a{a}_i{i}.h5")


def test_repo_add_format_hashes_files_in_one_batch(monkeypatch, tmp_path):
    """
    Test that the Repo.add() method calls checksum_many once for all files in one batch.
    This test creates a repository, adds files to it, and then calls the add() method.
    It uses monkeypatching to replace the checksum_many method with a custom function
    that records the calls made to it. The test then checks that checksum_many was
    called exactly once with all the expected file paths.
    Args:
        monkeypatch: pytest fixture that allows for monkeypatching.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["data_1.txt", "data_2.txt"])
    original_checksum_many = repo.checksum_many
    calls = []
    def record_checksum_many(paths):
        """
        Record calls to checksum_many and delegate to the original implementation.
        """
        calls.append(list(paths))
        return original_checksum_many(paths)
    monkeypatch.setattr(repo, "checksum_many", record_checksum_many)
    repo.add("data_{number}.txt")

    assert len(calls) == 1, \
        "Expected checksum_many to be called once for all files in one batch"
    assert set(calls[0]) == {
        repo.worktree / "data_1.txt", repo.worktree / "data_2.txt"}, \
        "Expected checksum_many to be called once with both files in one batch"


def test_repo_add_persists_only_sha1_and_path(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])

    result = repo.add("a{a}_i{i}.h5")

    assert list(result.columns) == ["path", "a", "i"]
    persisted = repo.dothm.load_tsv("data")
    assert repo.state.config["data"] == [{"fmt": "a{a}_i{i}.h5", "encoding": None}]
    assert list(persisted.columns) == ["sha1", "a", "i"]
    assert persisted.to_dict(orient="records") == [
        {"sha1": Repo.checksum(repo.worktree / "a0_i0.h5"), "a": "0", "i": "0"},
        {"sha1": Repo.checksum(repo.worktree / "a0_i30.h5"), "a": "0", "i": "30"}]


def test_repo_add_dot_replaces_manifest_with_current_tree(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])
    repo.add("a{a}_i{i}.h5")
    (repo.worktree / "a1_i45.h5").write_text("a1_i45.h5\n", encoding="utf-8")
    result = repo.add(".")

    assert sorted(result["path"]) == ["a0_i0.h5", "a0_i30.h5", "a1_i45.h5"]
    persisted = repo.dothm.load_tsv("data")
    assert persisted.to_dict(orient="records") == [
        {"sha1": Repo.checksum(repo.worktree / "a0_i0.h5"), "a": "0", "i": "0"},
        {"sha1": Repo.checksum(repo.worktree / "a0_i30.h5"), "a": "0", "i": "30"},
        {"sha1": Repo.checksum(repo.worktree / "a1_i45.h5"), "a": "1", "i": "45"}]


def test_repo_add_dot_removes_deleted_files_from_manifest(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])
    repo.add("a{a}_i{i}.h5")
    (repo.worktree / "a0_i0.h5").unlink()
    repo.add(".")

    assert repo.state.data.to_dict(orient="records") == [
        {"sha1": Repo.checksum(repo.worktree / "a0_i30.h5"), "a": "0", "i": "30"}]


def test_repo_add_dot_from_nested_directory_uses_path_components(monkeypatch, tmp_path):
    """
    Test that adding files from a nested directory correctly uses the relative path
    components in the ParaFrame. This test creates a repository with a nested directory,
    adds files to it, and then changes the current working directory to the nested
    directory. It then calls the add() method with "." and checks that the resulting
    ParaFrame contains the correct relative paths.

    Args:
        monkeypatch: pytest fixture that allows for monkeypatching.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    nested = repo.worktree / "nested"
    similarly_named = repo.worktree / "nested-other"
    nested.mkdir()
    similarly_named.mkdir()
    (nested / "data_1.txt").write_text("included\n", encoding="utf-8")
    (similarly_named / "data_2.txt").write_text("excluded\n", encoding="utf-8")
    repo.set_config(fmt="{folder}/data_{number}.txt")
    monkeypatch.chdir(nested)
    result = repo.add(".")

    assert result["path"].tolist() == ["nested/data_1.txt"], f"Expected only the file \
        in the nested directory to be added, got {result['path'].tolist()}"
    assert repo.state.data["number"].tolist() == ["1"], f"Expected 'number' column to \
        contain only '1', got {repo.state.data['number'].tolist()}"


def test_repo_add_pattern_keeps_deleted_manifest_rows(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])
    repo.add("a{a}_i{i}.h5")
    original_sha = repo.state.data.loc[
        (repo.state.data["a"] == "0") & (repo.state.data["i"] == "0"),
        "sha1",
    ].iloc[0]

    (repo.worktree / "a0_i0.h5").unlink()
    repo.add("a{a}_i{i}.h5")

    assert repo.state.data.to_dict(orient="records") == [
        {"sha1": original_sha, "a": "0", "i": "0"},
        {"sha1": Repo.checksum(repo.worktree / "a0_i30.h5"), "a": "0", "i": "30"},
    ]


def test_repo_add_pattern_replaces_manifest_when_fmt_changes(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0.4_i30_w3.h5", "b0.4_i30_w3.h5"])

    repo.add("a{a}_i{i}_w{w}.h5")
    repo.commit("main a data")
    repo.checkout("experiment")
    repo.add("b{a}_i{i}_w{w}.h5")

    assert repo.state.config["data"] == [{"fmt": "b{a}_i{i}_w{w}.h5", "encoding": None}]
    assert repo.state.data.to_dict(orient="records") == [
        {
            "sha1": Repo.checksum(repo.worktree / "b0.4_i30_w3.h5"),
            "a": "0.4",
            "i": "30",
            "w": "3",
        }
    ]


def test_repo_add_paths_is_not_supported_yet(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5", "b0_i45.h5"])
    repo.add("a{a}_i{i}.h5")

    with pytest.raises(RuntimeError, match="explicit path add is not supported"):
        repo.add_paths(["b0_i45.h5"])


def test_repo_add_preserves_config_order_and_remote_key(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5"])

    repo.add("a{a}_i{i}.h5")

    config_text = (repo.dothm.path / "config.yml").read_text(encoding="utf-8")
    assert "data:\n- fmt: a{a}_i{i}.h5\n  encoding: null\n" in config_text
    assert "remote: null\n" in config_text


def test_repo_add_parse_failure_preserves_existing_format(monkeypatch, tmp_path):
    """
    Test that if ParaFrame.parse() fails during Repo.add(), the existing format in the
    repository's configuration is preserved. This test initializes a repository, sets an
    initial format, and then monkeypatches ParaFrame.parse() to raise a ValueError.
    It then attempts to add a new format and checks that the original format remains in
    the configuration.
    Args:
        monkeypatch: pytest fixture that allows for monkeypatching.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If ParaFrame.parse() is called and raises a ValueError.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(fmt="old_{number}.txt")
    def fail_parse(*args, **kwargs):
        """Simulate a failure in ParaFrame.parse() by raising a ValueError."""
        raise ValueError("invalid format")
    monkeypatch.setattr("hallmark.repo.ParaFrame.parse", fail_parse)

    with pytest.raises(ValueError, match="invalid format"):
        repo.add("new_{number}.txt")
    assert repo.state.config["data"][0]["fmt"] == ("old_{number}.txt"), \
        "Expected the original format to be preserved in the config file"
    assert repo.dothm.load_yml("config")["data"][0]["fmt"] == "old_{number}.txt", \
        "Expected the original format to be preserved in the config file"


@pytest.mark.parametrize("fmt", ["", "   ", None])
def test_repo_add_rejects_empty_format(tmp_path, fmt):
    """
    Test that Repo.add() raises a ValueError when given an empty or None format string.
    This test initializes a repository and attempts to add files using an empty string,
    a string with only whitespace, and None as the format. It checks that a ValueError
    is raised in each case.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        fmt: The format string to test (empty string, whitespace, or None).
    Raises:
        ValueError: If the format string is empty, whitespace, or None.
    """
    repo = Repo.init(tmp_path / "repo")

    with pytest.raises(ValueError, match="format must be a non-empty string"):
        repo.add(fmt)


### Repo.set_config tests ###

def test_repo_set_config_updates_only_requested_fields(tmp_path):
    repo = Repo.init(tmp_path / "repo")

    repo.set_config(fmt="b{a}_i{i}.h5", remote_name="origin")

    assert repo.state.config == {
        "data": [{"fmt": "b{a}_i{i}.h5", "encoding": None}],
        "remote": {"name": "origin"},
    }


def test_repo_set_config_preserves_encoding_and_updates_remote(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config = {
        "data": [
            {
                "fmt": "{mag:d}a{aspin}_w{win:d}.h5",
                "encoding": {
                    "aspin": r"m([0-9]+(\.[0-9]+)?|\.[0-9]+)"
                },
            }
        ],
        "remote": {"name": "origin"},
    }
    repo.dothm.dump(repo.state)

    repo.set_config(fmt="b{a}_i{i}.h5", remote_url="https://example.com/path")

    assert repo.state.config == {
        "data": [
            {
                "fmt": "b{a}_i{i}.h5",
                "encoding": {
                    "aspin": r"m([0-9]+(\.[0-9]+)?|\.[0-9]+)"
                },
            }
        ],
        "remote": {"name": "origin", "url": "https://example.com/path"},
    }


def test_repo_set_config_creates_or_updates_encoding_map(tmp_path):
    repo = Repo.init(tmp_path / "repo")

    repo.set_config(encoding_updates={"aspin": r"m([0-9]+(\.[0-9]+)?|\.[0-9]+)"})

    assert repo.state.config == {
        "data": [
            {
                "encoding": {
                    "aspin": r"m([0-9]+(\.[0-9]+)?|\.[0-9]+)"
                },
            }
        ],
        "remote": None,
    }


def test_repo_set_config_updates_selected_remote_in_list(tmp_path):
    """
    Test that Repo.set_config() updates the specified remote in a list of remotes
    while preserving the other remotes. This test initializes a repository with a list
    of remotes, updates one of them, and checks that the other remote remains unchanged.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["remote"] = [
        {"name": "origin", "url": "https://origin.test/data"},
        {"name": "mirror", "url": "https://old-mirror.test/data"}]
    repo.dothm.dump(repo.state)
    repo.set_config(remote_name="mirror", remote_url="https://new-mirror.test/data")

    assert repo.state.config["remote"] == [
        {"name": "origin", "url": "https://origin.test/data"},
        {"name": "mirror", "url": "https://new-mirror.test/data"}], "Expected the \
            'mirror' remote to be updated while preserving the 'origin' remote"


def test_repo_set_config_rejects_nonlist_nondict_remote_config(tmp_path):
    """
    Test that Repo.set_config() raises a ValueError when the existing "remote"
    config value is neither a mapping nor a list (e.g. a bare string), since it
    cannot be normalized into a remote list to update.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If the existing "remote" config value is not a mapping or list.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["remote"] = "not-a-mapping-or-list"
    repo.dothm.dump(repo.state)

    with pytest.raises(ValueError, match="Invalid remote configuration"):
        repo.set_config(remote_url="https://example.test/data")


def test_repo_set_config_rejects_unknown_remote_name_without_url(tmp_path):
    """
    Test that Repo.set_config() raises a ValueError when multiple remotes are
    configured, remote_name does not match any of them, and no remote_url is
    given to create a new remote entry.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If remote_name does not match an existing remote and no
        remote_url is provided.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["remote"] = [
        {"name": "origin", "url": "https://origin.test/data"},
        {"name": "mirror", "url": "https://mirror.test/data"}]
    repo.dothm.dump(repo.state)

    with pytest.raises(ValueError, match="'missing' is not configured"):
        repo.set_config(remote_name="missing")


def test_repo_set_config_requires_remote_name_when_no_origin(tmp_path):
    """
    Test that Repo.set_config() raises a ValueError when multiple remotes are
    configured, no remote_name is given, and none of the remotes is named
    "origin" to default to.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If multiple remotes are configured without a default "origin"
        and remote_name is not specified.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["remote"] = [
        {"name": "mirror-a", "url": "https://mirror-a.test/data"},
        {"name": "mirror-b", "url": "https://mirror-b.test/data"}]
    repo.dothm.dump(repo.state)

    with pytest.raises(ValueError, match="specify --remote-name"):
        repo.set_config(remote_url="https://new.test/data")


@pytest.mark.parametrize("fmt", ["", "   ", 123])
def test_repo_set_config_rejects_invalid_format(tmp_path, fmt):
    """
    Test that Repo.set_config() raises a ValueError when given an invalid format
    string. This test initializes a repository and attempts to set the configuration
    with an empty string, a string with only whitespace, and a non-string value
    (integer). It checks that a ValueError is raised in each case.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        fmt: The format string to test (empty string, whitespace, or non-string).
    Raises:
        ValueError: If the format string is empty, whitespace, or not a string.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(fmt="data_{number}.txt")

    with pytest.raises(ValueError, match="fmt must be a non-empty string"):
        repo.set_config(fmt=fmt)
    assert repo.state.config["data"][0]["fmt"] == ("data_{number}.txt"), \
        "Expected the original format to be preserved in the config file"
    assert repo.dothm.load_yml("config")["data"][0]["fmt"] == ("data_{number}.txt"), \
        "Expected the original format to be preserved in the config file"


@pytest.mark.parametrize(
    "encoding_updates, message",[
        ([], "must be a dictionary"),
        ({"": r"[0-9]+"}, "field names"),
        ({"number": ""}, "must be a non-empty string"),
        ({"number": None}, "must be a non-empty string")])
def test_repo_set_config_rejects_invalid_encoding_updates(tmp_path, encoding_updates,
                                                          message):
    """
    Test that Repo.set_config() raises a ValueError when given invalid encoding updates.
    This test initializes a repository and attempts to set the configuration with
    various invalid encoding updates, such as an empty dictionary, a dictionary with an
    empty key, and a dictionary with an empty or None value. It checks that a ValueError
    is raised in each case and that the original configuration is preserved.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        encoding_updates: The encoding updates to test (invalid cases).
        message: The expected error message to match in the ValueError.
    Raises:
        ValueError: If the encoding updates are invalid.
    """
    repo = Repo.init(tmp_path / "repo")
    original_config = repo.dothm.load_yml("config")

    with pytest.raises(ValueError, match=message):
        repo.set_config(encoding_updates=encoding_updates)
    assert repo.state.config == original_config, \
        "Expected the original config to be preserved after invalid encoding updates"
    assert repo.dothm.load_yml("config") == original_config, \
        "Expected the original config to be preserved after invalid encoding updates"


def test_repo_set_config_normalizes_encoding_updates(tmp_path):
    """
    Test that Repo.set_config() normalizes encoding updates by stripping whitespace
    from the keys and values. This test initializes a repo and sets encoding updates
    with keys and values that contain leading and trailing whitespace. It then checks if
    the resulting configuration has the whitespace stripped from both keys and values.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(encoding_updates={" number ": r" [0-9]+ "})

    assert repo.state.config["data"][0]["encoding"] == {"number": r"[0-9]+"}, \
        "Expected the encoding updates to be normalized in the config file"


@pytest.mark.parametrize(
    "keyword, value, message",[(
            "remote_name",
            "",
            "remote_name must be a non-empty string"),
        (
            "remote_name",
            123,
            "remote_name must be a non-empty string"),
        (
            "remote_url",
            "   ",
            "remote_url must be a non-empty string"),
        (
            "remote_url",
            Path("remote"),
            "remote_url must be a non-empty string")])
def test_repo_set_config_rejects_invalid_remote_values(
    tmp_path,
    keyword,
    value,
    message):
    """
    Test that Repo.set_config() raises a ValueError when given invalid remote_name or
    remote_url values. This test initializes a repository and attempts to set the
    configuration with various invalid remote_name and remote_url values, such as an
    empty string, a string with only whitespace, a non-string value (integer), and a
    Path object. It checks that a ValueError is raised in each case and the original
    configuration is preserved.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        keyword: The keyword argument to test (remote_name or remote_url).
        value: The value to test for the given keyword (invalid cases).
        message: The expected error message to match in the ValueError.
    Raises:
        ValueError: If the remote_name or remote_url values are invalid.
    """
    repo = Repo.init(tmp_path / "repo")
    original_config = repo.dothm.load_yml("config")

    with pytest.raises(ValueError, match=message):
        repo.set_config(**{keyword: value})
    assert repo.state.config == original_config, \
        "Expected the original config to be preserved after invalid remote values"
    assert repo.dothm.load_yml("config") == original_config, \
        "Expected the original config to be preserved after invalid remote values"


def test_repo_set_config_strips_remote_values(tmp_path):
    """
    Test that Repo.set_config() strips whitespace from remote_name and remote_url.
    This test initializes a repository and sets the remote_name and remote_url with
    leading and trailing whitespace. It then checks that the resulting configuration has
    the whitespace stripped from both values.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_name=" origin ", remote_url=" https://example.test/data/ ")

    assert repo.state.config["remote"] == {
        "name": "origin", "url": "https://example.test/data/"}, \
        "Expected the remote values to be stripped of whitespace in the config file"


def test_repo_set_config_rejects_nonmapping_config(tmp_path):
    """
    Test that Repo.set_config() raises a ValueError when the repository config is not a
    mapping (dictionary). This test initializes a repository, sets the config to an
    empty list, and then attempts to set the configuration with a valid format.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If the repository config is not a mapping.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config = []

    with pytest.raises(ValueError, match="repository config must be a mapping"):
        repo.set_config(fmt="data_{number}.txt")


### Repo.status tests ###

def test_repo_status_reports_staged_worktree_and_untracked_changes(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")

    (repo.worktree / "a0_i0.h5").write_text("changed\n", encoding="utf-8")
    (repo.worktree / "a0_i30.h5").unlink()
    (repo.worktree / "extra.h5").write_text("extra\n", encoding="utf-8")

    snapshot = repo.status()

    assert snapshot["branch"] == "main"
    assert snapshot["staged"] == {
        "state": [],
        "added": [],
        "modified": [],
        "deleted": [],
    }
    assert snapshot["worktree"]["modified"] == ["a0_i0.h5"]
    assert snapshot["worktree"]["deleted"] == ["a0_i30.h5"]
    assert snapshot["untracked"] == ["extra.h5"]


def test_repo_status_reports_staged_manifest_changes(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")

    (repo.worktree / "a1_i45.h5").write_text("a1_i45.h5\n", encoding="utf-8")
    repo.add(".")

    snapshot = repo.status()

    assert snapshot["staged"]["added"] == ["a1_i45.h5"]
    assert snapshot["staged"]["modified"] == []
    assert snapshot["staged"]["deleted"] == []


def test_repo_status_hashes_tracked_files_in_one_batch(monkeypatch, tmp_path):
    """
    Test that the Repo.status() method calls checksum_many once for all tracked files.
    This test creates a repository with two tracked files, modifies one of them, and
    then calls Repo.status(). It uses monkeypatch to replace the checksum_many method
    with a custom function that records the calls made to it. The test then checks that
    checksum_many was called exactly once with the correct set of tracked files.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["data_1.txt", "data_2.txt"])
    repo.add("data_{number}.txt")
    repo.commit("main data")
    original_checksum_many = repo.checksum_many
    calls = []
    def record_checksum_many(paths):
        """Record calls to checksum_many. Return the original checksum_many result."""
        calls.append(list(paths))
        return original_checksum_many(paths)
    monkeypatch.setattr(repo, "checksum_many", record_checksum_many)
    snapshot = repo.status()

    assert snapshot["worktree"] == {"modified": [], "deleted": []}, \
        "worktree should have no modified or deleted files"
    assert len(calls) == 1, "checksum_many should be called once for all tracked files"
    assert set(calls[0]) == {
        repo.worktree / "data_1.txt", repo.worktree / "data_2.txt"}, \
        f"checksum_many should be called with all tracked files, got {calls[0]}"


def test_repo_status_lists_all_untracked_files_from_nested_cwd(monkeypatch, tmp_path):
    """
    Test that Repo.status() lists all untracked files relative to the repository root,
    even when called from a nested directory within the worktree.
    This test creates a repository with a nested directory, adds some tracked files,
    and creates untracked files both in the root and nested directories. It then changes
    the current working directory to the nested directory and calls Repo.status().
    The test checks that the untracked files are listed relative to the repository root.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    tracked_path = repo.worktree / "data_1.txt"
    tracked_path.write_text("tracked\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    nested = repo.worktree / "nested"
    nested.mkdir()
    (repo.worktree / "root-note.txt").write_text("root\n", encoding="utf-8")
    (nested / "nested-note.txt").write_text("nested\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    snapshot = repo.status()

    assert snapshot["untracked"] == ["nested/nested-note.txt", "root-note.txt"], \
        "Expected untracked files to be listed relative to the repository root, even \
            when called from a nested directory"


def test_repo_status_does_not_walk_dothm_directory(monkeypatch, tmp_path):
    """
    Repo.status() should prune .hm before recursively searching for
    untracked files.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    (repo.worktree / "visible.txt").write_text("visible\n", encoding="utf-8")
    internal_directory = repo.dothm.path / "status-test"
    internal_directory.mkdir()
    (internal_directory / "internal.txt").write_text("internal\n", encoding="utf-8")
    original_walk = os.walk
    walked_directories = []
    def recording_walk(root):
        """Record the directories walked by os.walk and yield the results from the
        original os.walk."""
        for current, directories, files in original_walk(root):
            walked_directories.append(Path(current))
            yield current, directories, files
    monkeypatch.setattr("hallmark.helper_functions.os.walk", recording_walk)
    snapshot = repo.status()

    assert snapshot["untracked"] == ["visible.txt"], "Expected only visible.txt to be \
        reported as untracked, but got: " f"{snapshot['untracked']}"
    assert not any(
        path == repo.dothm.path or repo.dothm.path in path.parents
        for path in walked_directories), \
        "Expected .hm directory to be pruned from os.walk, but it was walked"


### Repo.checkout tests ###

def test_checkout_rewrites_tracked_files_and_shares_objects(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")

    main_objects = sorted(p.relative_to(repo.dothm.path)
            for p in (repo.dothm.path / "objects").rglob("*") if p.is_file())
    assert len(main_objects) == 2

    repo.checkout("experiment")
    (repo.worktree / "a0_i0.h5").unlink()
    (repo.worktree / "a0_i30.h5").unlink()
    _write_files(repo.worktree, ["a1_i45.h5", "a1_i90.h5"])
    repo.add(".")
    repo.commit("experiment data")

    experiment_files = sorted(path.name
                for path in Path(str(repo.worktree)).glob("*.h5"))
    assert experiment_files == ["a1_i45.h5", "a1_i90.h5"]

    repo.checkout("main")
    main_files = sorted(path.name
                for path in Path(str(repo.worktree)).glob("*.h5"))
    assert main_files == ["a0_i0.h5", "a0_i30.h5"]

    objects_after = [p for p in (repo.dothm.path /
                            "objects").rglob("*") if p.is_file()]
    assert len(objects_after) == 4

    repo.checkout("experiment")
    roundtrip_files = sorted(path.name
                            for path in Path(str(repo.worktree)).glob("*.h5"))
    assert roundtrip_files == ["a1_i45.h5", "a1_i90.h5"]


def test_checkout_leaves_untracked_files(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")

    repo.checkout("experiment")
    (repo.worktree / "a0_i0.h5").unlink()
    _write_files(repo.worktree, ["a1_i45.h5"])
    repo.add(".")
    repo.commit("experiment data")
    (repo.worktree / "notes.txt").write_text("keep me\n", encoding="utf-8")

    repo.checkout("main")

    assert (repo.worktree / "notes.txt").read_text(encoding="utf-8") == "keep me\n"
    assert sorted(path.name
            for path in Path(str(repo.worktree)).glob("*.h5")) == ["a0_i0.h5"]


def test_checkout_rebuilds_worktree_for_branch_specific_nested_fmt(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    (repo.worktree / "main").mkdir()
    _write_files(repo.worktree, ["main/a0_i0.h5"])
    repo.add("main/a{a}_i{i}.h5")
    repo.commit("main data")

    repo.checkout("experiment")
    (repo.worktree / "exp" / "run1").mkdir(parents=True)
    _write_files(repo.worktree, ["exp/run1/b0_i0.h5"])
    repo.add("exp/run{run}/b{a}_i{i}.h5")
    repo.commit("experiment data")

    repo.checkout("main")
    root = Path(str(repo.worktree))
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*.h5")) == [
        "main/a0_i0.h5",
    ]
    assert not (repo.worktree / "exp").exists()

    repo.checkout("experiment")
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*.h5")) == [
        "exp/run1/b0_i0.h5",
    ]
    assert not (repo.worktree / "main").exists()


def test_checkout_aborts_on_dirty_tracked_file(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")
    repo.checkout("experiment")
    repo.checkout("main")
    (repo.worktree / "a0_i0.h5").write_text("changed\n", encoding="utf-8")

    with pytest.raises(RuntimeError,
                match='tracked file "a0_i0.h5" has uncommitted changes'):
        repo.checkout("experiment")


def test_checkout_aborts_on_untracked_path_conflict(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")

    repo.checkout("experiment")
    (repo.worktree / "a0_i0.h5").unlink()
    _write_files(repo.worktree, ["a1_i45.h5"])
    repo.add(".")
    repo.commit("experiment data")
    repo.checkout("main")

    (repo.worktree / "a1_i45.h5").write_text("untracked blocker\n", encoding="utf-8")

    with pytest.raises(RuntimeError,
        match='target tracked path "a1_i45.h5" already exists as an untracked file'):
        repo.checkout("experiment")


def test_checkout_allows_return_to_branch_when_target_files_already_match(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "b0_i0.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")

    repo.checkout("experiment")
    repo.add("b{a}_i{i}.h5")
    repo.commit("experiment data")

    repo.checkout("main")

    assert sorted(path.name for path in Path(str(repo.worktree)).glob("*.h5")) == [
        "a0_i0.h5",
    ]


def test_checkout_rejects_symlink_destination_escape(tmp_path):
    """
    Test that checking out a branch fails if a symlink in the worktree points outside
    the repository and would be overwritten by a tracked file from the target branch.
    This test creates a symlink inside the repository that points to a directory outside
    the repository, and then attempts to check out a branch that has a tracked file with
    the same name as the symlink.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        CheckoutError: If the symlink points outside the repository worktree and would
        be overwritten by a tracked file from the target branch.
    """
    repo = Repo.init(tmp_path / "repo")
    (repo.worktree / "main").mkdir()
    _write_files(repo.worktree, ["main/a0_i0.h5"])
    repo.add("main/a{a}_i{i}.h5")
    repo.commit("main data")
    repo.checkout("experiment")
    (repo.worktree / "main/a0_i0.h5").unlink()
    (repo.worktree / "exp").mkdir()
    _write_files(repo.worktree, ["exp/a1_i45.h5"])
    repo.add("exp/a{a}_i{i}.h5")
    repo.commit("experiment data")
    repo.checkout("main")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo.worktree / "exp"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")

    with pytest.raises(CheckoutError, match="symbolic link"):
        repo.checkout("experiment")
    assert repo.dothm.active_branch.name == "main", f"Expected active branch to be \
        'main', but found: {repo.dothm.active_branch.name}"
    assert list(outside.iterdir()) == [], \
        f"Expected outside directory to be empty, but found: {list(outside.iterdir())}"


def test_checkout_hashes_tracked_files_in_one_batch(monkeypatch, tmp_path):
    """
    Test that the Repo.checkout() method calls checksum_many once for all tracked files.
    This test creates a repository with two tracked files, checks out a new branch, and
    then calls Repo.checkout() again to switch back to the original branch. It uses
    monkeypatch to replace the checksum_many method with a custom function that records
    the calls made to it. The test then checks that checksum_many was called exactly
    once with the correct set of tracked files.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["data_1.txt", "data_2.txt"])
    repo.add("data_{number}.txt")
    repo.commit("main data")
    original_checksum_many = repo.checksum_many
    calls = []
    def record_checksum_many(paths):
        """Record calls to checksum_many. Return the original checksum_many result."""
        calls.append(list(paths))
        return original_checksum_many(paths)
    monkeypatch.setattr(repo, "checksum_many", record_checksum_many)
    repo.checkout("experiment")

    assert len(calls) == 1, "checksum_many should be called once for all tracked files"
    assert set(calls[0]) == {
        repo.worktree / "data_1.txt", repo.worktree / "data_2.txt"}, \
        f"checksum_many should be called with all tracked files, got {calls[0]}"


@pytest.mark.parametrize(
    "branch_name",[
        "",
        "   ",
        "-f",
        "../escape",
        "bad name",
        "bad..name",
        "branch~1"])
def test_checkout_rejects_invalid_branch_names(tmp_path, branch_name):
    """
    Test that the Repo.checkout() method raises a ValueError for invalid branch names.
    This test attempts to check out branches with various invalid names and expects a
    ValueError to be raised in each case.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        branch_name: A parameterized invalid branch name to test.
    Raises:
        ValueError: If the branch name is invalid according to the repository rules.
    """
    repo = Repo.init(tmp_path / "repo")

    with pytest.raises(ValueError, match="branch name"):
        repo.checkout(branch_name)


def test_checkout_checks_target_objects_before_switching_branch(tmp_path):
    """
    Test that the Repo.checkout() method checks for the existence of target objects
    in the object store before switching branches. This test creates a repository,
    adds a file, commits it, checks out a new branch, modifies the file, and commits
    the changes. It then deletes the object corresponding to the modified file and
    attempts to check out the experiment branch.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        CheckoutError: If the target object for the file in the experiment branch is
        missing from the object store.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("main contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.checkout("experiment")
    data_path.write_text("experiment contents\n", encoding="utf-8")
    repo.add(".")
    repo.commit("experiment data")
    experiment_sha1 = repo.state.data.iloc[0]["sha1"]
    repo.checkout("main")
    assert data_path.read_text(encoding="utf-8") == "main contents\n", \
        "Expected worktree to contain main branch contents after checkout"

    stored_object = (repo.objects.root / experiment_sha1[:2] / experiment_sha1[2:])
    stored_object.unlink()
    with pytest.raises(CheckoutError, match="missing object"):
        repo.checkout("experiment")
    assert repo.dothm.active_branch.name == "main", f"Expected active branch to be \
        'main', but found: {repo.dothm.active_branch.name}"
    assert data_path.read_text(encoding="utf-8") == "main contents\n", \
        "Expected worktree to remain unchanged after failed checkout"


def test_checkout_rejects_directory_at_target_file_path(tmp_path):
    """
    Test that the Repo.checkout() method raises a CheckoutError when a directory exists
    at the path of a tracked file in the target branch. This test creates a repository,
    adds a file, commits it, checks out a new branch, deletes the file, creates
    a directory at the same path, and attempts to check out the new branch.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        CheckoutError: If a directory exists at the path of a tracked file in the target
        branch.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("main\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.checkout("experiment")
    data_path.unlink()
    experiment_path = (repo.worktree / "experiment_1.txt")
    experiment_path.write_text("experiment\n", encoding="utf-8")
    repo.add("experiment_{number}.txt")
    repo.commit("experiment data")
    repo.checkout("main")
    experiment_path.mkdir()

    with pytest.raises(CheckoutError, match="untracked non-file"):
        repo.checkout("experiment")
    assert repo.dothm.active_branch.name == "main", f"Expected active branch to be \
        'main', but found: {repo.dothm.active_branch.name}"
    assert experiment_path.is_dir(), \
        f"Expected {experiment_path} to remain a directory after failed checkout"


def test_checkout_rolls_back_after_install_failure(monkeypatch, tmp_path):
    """
    Test that Repo.checkout() rolls back the worktree to its original state if an
    OSError occurs during the installation of tracked files from the target branch.
    This test creates a repository with two branches, modifies the worktree, and then
    monkeypatches Path.replace to simulate an OSError during the second file
    installation. It checks that a CheckoutError is raised and that the worktree is
    restored to its original state after the failed checkout.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        CheckoutError: If an OSError occurs during the installation of tracked files
        from the target branch.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["a0_i0.h5", "a0_i30.h5"])
    repo.add("a{a}_i{i}.h5")
    repo.commit("main data")
    repo.checkout("experiment")
    (repo.worktree / "a0_i0.h5").unlink()
    (repo.worktree / "a0_i30.h5").unlink()
    _write_files(repo.worktree, ["a1_i45.h5", "a1_i90.h5"])
    repo.add(".")
    repo.commit("experiment data")
    repo.checkout("main")
    original_replace = Path.replace
    install_count = 0

    def fail_second_install(path, target):
        """Simulate a failure during the second file installation by raising an OSError.
        This function replaces Path.replace and raises an OSError on the second call
        when the path is in the "staged" directory and the filename is a digit."""
        nonlocal install_count
        if path.parent.name == "staged" and path.name.isdigit():
            install_count += 1
            if install_count == 2:
                raise OSError("simulated install failure")
        return original_replace(path, target)
    monkeypatch.setattr(Path, "replace", fail_second_install)

    with pytest.raises(CheckoutError, match="checkout .* failed"):
        repo.checkout("experiment")

    root = Path(str(repo.worktree))
    assert repo.branches()["current"] == "main", \
        f"Expected active branch to be 'main', but found: {repo.branches()['current']}"
    assert sorted(path.name for path in root.glob("*.h5")) == [
        "a0_i0.h5",
        "a0_i30.h5"], \
        "Expected worktree to be restored to main branch contents after failed checkout"
    assert not list(root.glob(".hallmark-checkout-*")), \
        "Expected no temporary checkout directories to remain after failed checkout"


def test_checkout_restores_only_changed_target_files(monkeypatch, tmp_path):
    """
    Test that Repo.checkout() restores only the changed files from the target branch
    and does not unnecessarily restore unchanged files. This test creates a repository
    with two branches, modifies one file in the target branch, and checks out the target
    branch. It uses monkeypatch to replace the objects.restore method to record calls
    made to it. The test then checks that only the changed file was restored and that
    unchanged files remain intact.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["data_1.txt", "data_2.txt"])
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.checkout("experiment")
    changed_path = repo.worktree / "data_1.txt"
    changed_path.write_text("experiment contents\n", encoding="utf-8")
    repo.add(".")
    repo.commit("experiment data")
    repo.checkout("main")
    restore_calls = []
    original_restore = repo.objects.restore
    def record_restore(sha1, destination):
        """Record calls to objects.restore. Return the original restore result."""
        restore_calls.append((sha1, destination))
        return original_restore(sha1, destination)
    monkeypatch.setattr(repo.objects, "restore", record_restore)
    repo.checkout("experiment")

    assert len(restore_calls) == 1, \
        f"Expected only one restore call for changed file, got {len(restore_calls)}"
    assert changed_path.read_text(encoding="utf-8") == "experiment contents\n", \
        "Expected data_1.txt to be restored to experiment contents after checkout"
    assert (repo.worktree / "data_2.txt").is_file(), \
        "Expected data_2.txt to remain unchanged after checkout"


### Repo.clone() tests ###

def test_repo_clone_downloads_remote_data_by_default(monkeypatch, tmp_path):
    source = Repo.init(tmp_path / "source")
    _write_files(source.worktree, ["a0_i0.h5"])
    source.add("a{a}_i{i}.h5")
    source.set_config(remote_url="https://example.com/data/")
    expected_sha1 = Repo.checksum(source.worktree / "a0_i0.h5")
    source.commit("add source data")

    captured = {}

    def fake_download_file(url, destination, sha1, chunk_size=8192):
        captured["url"] = url
        captured["sha1"] = sha1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("downloaded\n", encoding="utf-8")
        return destination.stat().st_size

    monkeypatch.setattr("hallmark.downloader._download_file", fake_download_file)

    clone = Repo.clone(str(source.dothm.path), tmp_path / "clone")

    assert captured == {
        "url": "https://example.com/data/a0_i0.h5",
        "sha1": expected_sha1,
    }
    assert (clone.worktree / "a0_i0.h5").read_text(encoding="utf-8") == \
        "downloaded\n"
    assert clone.download_result["succeeded"] == 1
    assert clone.download_result["failed"] == 0


def test_repo_clone_can_skip_remote_data_download(monkeypatch, tmp_path):
    source = Repo.init(tmp_path / "source")
    _write_files(source.worktree, ["a0_i0.h5"])
    source.add("a{a}_i{i}.h5")
    source.set_config(remote_url="https://example.com/data/")
    source.commit("add source data")

    def fail_download(*args, **kwargs):
        raise AssertionError("download should not be attempted")

    monkeypatch.setattr("hallmark.downloader._download_file", fail_download)

    clone = Repo.clone(str(source.dothm.path), tmp_path / "clone", fetch_data=False)

    assert not (clone.worktree / "a0_i0.h5").exists()
    assert clone.download_result is None


def test_repo_clone_removes_incomplete_destination(tmp_path):
    """
    Test that Repo.clone() removes the destination directory if the clone operation
    fails due to missing required files. This test initializes a source repository,
    removes the required meta.yml file, and attempts to clone it. It checks that a
    CloneError is raised and that the destination directory does not exist after the
    failed clone attempt.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        CloneError: If the clone operation fails due to missing required files.
    """
    source = Repo.init(tmp_path / "source")
    meta_path = source.dothm.path / "meta.yml"
    meta_path.unlink()
    source.dothm.index.remove(["meta.yml"])
    source.dothm.index.commit("remove required metadata")
    destination = tmp_path / "clone"

    with pytest.raises(CloneError, match="missing required file"):
        Repo.clone(str(source.dothm.path), destination, fetch_data=False)
    assert not destination.exists(), f"Expected incomplete clone destination \
        {destination} to be removed after failed clone attempt"


### dothm tests ###

def test_dothm_yaml_round_trip(tmp_path):
    """
    Test that the Dothm class can correctly dump and load YAML data.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    """
    repo = Repo.init(tmp_path / "repo")
    expected = {
        "dataset": "example",
        "description": "first line\nsecond line\n",
        "values": ["a", "b"]}
    repo.dothm.dump_yml(expected, "meta")

    assert repo.dothm.load_yml("meta") == expected, \
        f"Expected {expected}, got {repo.dothm.load_yml('meta')}"

def test_dothm_load_treats_empty_yaml_as_empty_mapping(tmp_path):
    """
    Test that the Dothm.load() method treats an empty YAML file as an empty mapping.
    This test creates a repository, writes an empty meta.yml file, and then calls
    Dothm.load() to load the state. It checks that the loaded meta attribute is
    an empty dictionary.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    """
    repo = Repo.init(tmp_path / "repo")
    meta_path = repo.dothm.path / "meta.yml"
    meta_path.write_text("", encoding="utf-8")
    loaded_state = repo.dothm.load()

    assert loaded_state.meta == {}, \
        f"Expected loaded meta to be an empty dict, got {loaded_state.meta}"
    assert isinstance(loaded_state.meta, dict), \
        f"Expected loaded meta to be a dict, got {type(loaded_state.meta)}"


def test_dump_yml_preserves_existing_file_when_serialization_fails(monkeypatch,
                                                                   tmp_path):
    """
    Test that the Dothm.dump_yml() method preserves the original file if serialization
    fails. This test creates a repository, writes an initial config.yml file, and then
    monkeypatch the yaml.dump function to simulate a serialization failure. It checks
    that the original config.yml remains unchanged and that no temporary files are left
    behind after the failed dump_yml call.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        RuntimeError: If the custom dump function simulates a serialization failure.
    """
    repo = Repo.init(tmp_path / "repo")
    config_path = repo.dothm.path / "config.yml"
    original_text = config_path.read_text(encoding="utf-8")
    def fail_dump(data, handle, **kwargs):
        """A custom dump function that simulates a serialization failure by writing
        partial output to the file and then raising a RuntimeError."""
        handle.write("partial output")
        raise RuntimeError("serialization failed")
    monkeypatch.setattr("hallmark.dothm.yaml.dump", fail_dump)

    with pytest.raises(RuntimeError, match="serialization failed"):
        repo.dothm.dump_yml({"data": []}, "config")
    assert config_path.read_text(encoding="utf-8") == original_text, \
        "Expected original config.yml to remain unchanged after failed dump_yml"
    assert list(repo.dothm.path.glob(".config.yml.*.tmp")) == [], \
        "Expected no temporary files to remain after failed dump_yml"


def test_dump_tsv_supports_missing_value_representation(tmp_path):
    """
    Test that the Dothm.dump_tsv() method correctly represents missing values in the
    output TSV file. This test creates a repository, constructs a DataFrame with missing
    values, and calls dump_tsv() with a custom na_rep argument. It checks that
    the output TSV file is created and that the missing values are represented as
    specified in the na_rep argument.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    """
    repo = Repo.init(tmp_path / "repo")
    frame = pd.DataFrame({
        "path": ["first.dat", "second.dat"], "value": ["present", None]})
    repo.dothm.dump_tsv(frame, "custom.TSV", na_rep="None")
    output_path = repo.dothm.path / "custom.TSV"

    assert output_path.is_file(), \
        f"Expected TSV file {output_path} to be created, but it does not exist"
    assert output_path.read_text(encoding="utf-8") == (
        "path\tvalue\n""first.dat\tpresent\n""second.dat\tNone\n"), \
        f"Expected TSV output with 'None' for missing values, \
            got:\n{output_path.read_text(encoding='utf-8')}"


@pytest.mark.parametrize(
    "source", ["- first\n- second\n", "plain text\n", "123\n", "true\n"])
def test_load_yaml_rejects_non_mapping_documents(source):
    """
    Test that load_yaml() raises a ValueError when the YAML document is not a mapping.
    This test uses parameterization to check various non-mapping YAML documents, such as
    sequences and plain text. It expects a ValueError to be raised with a specific error
    message indicating that the YAML document must contain a mapping.
    Args:
        source: A string representing a non-mapping YAML document to be tested.
    Raises:
        ValueError: If the YAML document does not contain a mapping.
    """
    with pytest.raises(ValueError, match="YAML document must contain a mapping"):
        load_yaml(source)


def test_load_yaml_accepts_mapping_and_empty_document():
    """
    Test that load_yaml() correctly loads a YAML mapping and treats an empty document
    as an empty mapping. This test checks that a valid YAML mapping is loaded into a
    dictionary and that an empty YAML document is treated as an empty dictionary.
    """
    assert load_yaml("name: hallmark\n") == {"name": "hallmark"}, \
        "Expected YAML mapping to be loaded correctly"
    assert load_yaml("") == {}, \
        "Expected empty YAML document to be treated as an empty mapping"


def test_load_tsv_preserves_na_tokens_and_blank_values(tmp_path):
    """
    Test that Dothm.load_tsv correctly preserves 'NA' tokens and blank values when
    loading a TSV file. This test creates a repository, writes a TSV file with 'NA'
    and blank values, and then loads it using Dothm.load_tsv. It checks that the
    resulting DataFrame has the expected values.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    table_path = repo.dothm.path / "literal.tsv"
    table_path.write_text("sha1\tname\n""first\tNA\n""second\t\n", encoding="utf-8")
    frame = repo.dothm.load_tsv("literal")

    assert frame["name"].tolist() == ["NA", ""], \
        f"Expected ['NA', ''], got {frame['name'].tolist()}"


def test_dothm_init_does_not_overwrite_existing_readme(tmp_path):
    """
    Test that Dothm.init does not overwrite an existing README.md file in the repository
    This test creates a Dothm repository, writes custom contents to the README.md file,
    and then calls Dothm.init again. It checks that the README.md file remains unchanged
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    path = tmp_path / "repo.hm"
    dothm = Dothm.init(path)
    readme_path = dothm.path / "README.md"
    readme_path.write_text("custom contents\n", encoding="utf-8")
    Dothm.init(path)

    assert readme_path.read_text(encoding="utf-8") == "custom contents\n", f"Expected \
        README.md to remain unchanged, got {readme_path.read_text(encoding='utf-8')}"


@pytest.mark.parametrize("stem", ["../outside", "/tmp/outside", "nested/file"])
def test_dothm_storage_rejects_noncomponent_names(tmp_path, stem):
    """
    Test that Dothm.load_yml raises a ValueError when given a stem that is not a valid
    component name. This test checks that the method correctly identifies invalid stems
    that attempt to escape the repository structure or use absolute paths.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        stem: A parameterized invalid stem to test.
    Raises:
        ValueError: If the stem is not a valid component name
        (e.g., contains path separators or is absolute).
    """
    repo = Repo.init(tmp_path / "repo")

    with pytest.raises(ValueError, match="storage name"):
        repo.dothm.load_yml(stem)


def test_dothm_init_rejects_bare_repository(tmp_path):
    """
    Test that Dothm.init raises a DothmError when asked to create a bare git
    repository, since a ".hm" directory must be a valid git worktree.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        DothmError: If bare=True is passed to Dothm.init.
    """
    with pytest.raises(DothmError, match="must not be a bare"):
        Dothm.init(tmp_path / "repo.hm", bare=True)


def test_dothm_rejects_opening_bare_git_repository(tmp_path):
    """
    Test that opening an existing bare git repository as a Dothm raises a
    DothmError, since a ".hm" directory must be a valid git worktree.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        DothmError: If the path is a bare git repository with no working tree.
    """
    path = tmp_path / "bare.hm"
    GitRepo.init(path, bare=True)

    with pytest.raises(DothmError, match="valid git worktree"):
        Dothm(path)


### iter_repository_files tests ###

def test_iter_repository_files_excludes_symlinks(tmp_path):
    """
    Test that iter_repository_files() excludes symbolic links from the list of files
    in the repository. This test creates a temporary directory with a regular file and
    a symbolic link pointing to a file outside the repository. It checks that
    iter_repository_files() returns only the regular file and does not include the
    symbolic link.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    root = tmp_path / "root"
    root.mkdir()
    regular_file = root / "regular.dat"
    regular_file.write_text("regular\n", encoding="utf-8")
    outside_file = tmp_path / "outside.dat"
    outside_file.write_text("outside\n", encoding="utf-8")
    (root / "linked.dat").symlink_to(outside_file)

    assert list(iter_repository_files(root)) == [regular_file], \
        "Expected iter_repository_files to exclude symlinks, but it included: "\
        f"{list(iter_repository_files(root))}"


#### regex_sub tests ###

def test_regex_sub_replaces_all_matches_in_one_pass():
    """
    Test that regex_sub() replaces all matches in a single pass, rather than
    performing multiple passes. This test uses a regex pattern to match numbers in a
    string and replaces them with their negated values. It checks that all matches are
    replaced correctly in one pass, without any unintended side effects.
    """
    encoding = {"encoding": {"aspin": r"m([0-9]+(?:\.[0-9]+)?)"}}

    assert regex_sub("source_m0.5_m12", encoding) == "source_-0.5_-12", \
        "Expected regex_sub to replace all matches in one pass"


### tracked_paths tests ###

def test_tracked_file_replaced_by_directory_is_reported_missing(tmp_path):
    """
    Test that if a tracked file is replaced by a directory in the worktree, the Repo
    class reports it as missing and raises a CheckoutError when attempting to switch
    branches. This test creates a repository, adds a file, commits it, then replaces the
    file with a directory of the same name and checks the status and checkout behavior.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    Raises:
        CheckoutError: If the tracked file is replaced by a directory and a checkout is
        attempted.
    """
    repo = Repo.init(tmp_path / "repo")
    tracked_path = repo.worktree / "data_1.txt"
    tracked_path.write_text("contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    tracked_path.unlink()
    tracked_path.mkdir()
    snapshot = repo.status()

    assert snapshot["worktree"]["deleted"] == ["data_1.txt"], f"Expected deleted files \
        to include 'data_1.txt', got {snapshot['worktree']['deleted']}"
    assert snapshot["worktree"]["modified"] == [], \
        f"Expected no modified files, got {snapshot['worktree']['modified']}"
    with pytest.raises(CheckoutError, match="is missing"):
        repo.checkout("experiment")


def test_tracked_file_replaced_by_symlink_is_reported_missing(tmp_path):
    """
    Test that if a tracked file is replaced by a symbolic link in the worktree, the
    Repo class reports it as missing and raises CheckoutError when attempting to switch
    branches. This test creates a repository, adds a file, commits it, then replaces the
    file with a symbolic link to an outside file and checks the status and checkout
    behavior.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    Raises:
        CheckoutError: If the tracked file is replaced by a symbolic link and a checkout
        is attempted.
    """
    repo = Repo.init(tmp_path / "repo")
    tracked_path = repo.worktree / "data_1.txt"
    tracked_path.write_text("tracked\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("outside\n", encoding="utf-8")
    tracked_path.unlink()
    try:
        tracked_path.symlink_to(outside_path)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    snapshot = repo.status()

    assert snapshot["worktree"] == {"modified": [], "deleted": ["data_1.txt"]}
    with pytest.raises(CheckoutError, match="is missing"):
        repo.checkout("experiment")
    assert outside_path.read_text(encoding="utf-8") == "outside\n", f"Expected outside \
        file to remain unchanged, got {outside_path.read_text(encoding='utf-8')}"


### object store tests ###

def test_object_store_is_independent_from_worktree_files(tmp_path):
    """
    Test that the Objects class can store and restore files independently of worktree.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    """
    objects = Objects(tmp_path / "repo")
    source = tmp_path / "source.dat"
    source.write_text("original data\n", encoding="utf-8")
    sha1 = Repo.checksum(source)
    stored = objects.store(source, sha1)
    source.write_text("changed source\n", encoding="utf-8")

    assert stored.read_text(encoding="utf-8") == "original data\n", \
        f"Expected 'original data\\n', got {stored.read_text(encoding='utf-8')}"
    assert Repo.checksum(stored) == sha1, \
        f"Expected SHA1 {sha1}, got {Repo.checksum(stored)}"

    destination = tmp_path / "restored.dat"
    objects.restore(sha1, destination)
    destination.write_text("changed destination\n", encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == "original data\n", \
        f"Expected 'original data\\n', got {stored.read_text(encoding='utf-8')}"
    assert Repo.checksum(stored) == sha1, \
        f"Expected SHA1 {sha1}, got {Repo.checksum(stored)}"


@pytest.mark.parametrize(
    "invalid_sha1",[
        "",
        "abc123",
        "../outside",
        "../../etc/passwd",
        "g" * 40,
        "a" * 39,
        "a" * 41,
        None,
        123])
def test_object_store_rejects_invalid_sha1(tmp_path, invalid_sha1):
    """
    Test that the Objects class raises a ValueError for invalid SHA-1 checksums.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
        invalid_sha1 (str): An invalid SHA-1 checksum to test.
    Raises:
        ValueError: If the checksum is not exactly 40 hexadecimal characters.
    """
    objects = Objects(tmp_path / "repo")
    source = tmp_path / "source.dat"
    source.write_text("data\n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="SHA-1 checksum must be exactly 40 hexadecimal characters"):
        objects.store(source, invalid_sha1)


def test_object_store_normalizes_sha1_case(tmp_path):
    """
    Test that Objects class normalizes SHA-1 checksums to lowercase when storing files.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    """
    objects = Objects(tmp_path / "repo")
    source = tmp_path / "source.dat"
    source.write_text("data\n", encoding="utf-8")

    sha1 = Repo.checksum(source)
    stored = objects.store(source, sha1.upper())

    assert stored == objects.root / sha1[:2] / sha1[2:], \
        f"Expected stored path {objects.root / sha1[:2] / sha1[2:]}, got {stored}"
    assert stored.is_file(), \
        f"Expected stored file at {stored}, but it does not exist."


def test_object_store_rejects_corrupted_object_during_restore(tmp_path):
    """
    Test that the Objects class raises a ValueError when attempting to restore a file
    from a corrupted object in the object store. This test creates a source file, stores
    it in the object store, corrupts the stored object, and then attempts to restore it.
    It expects a ValueError to be raised due to the checksum mismatch, and checks that
    the destination file remains unchanged after the failed restore attempt.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    Raises:
        ValueError: If the restored file's SHA-1 checksum does not match the expected
        value due to corruption in the object store.
    """
    objects = Objects(tmp_path / "repo")
    source = tmp_path / "source.dat"
    source.write_text("original\n", encoding="utf-8")
    sha1 = Repo.checksum(source)
    stored = objects.store(source, sha1)
    stored.write_text("corrupted\n", encoding="utf-8")
    destination = tmp_path / "destination.dat"
    destination.write_text("existing\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        objects.restore(sha1, destination)
    assert destination.read_text(encoding="utf-8") == "existing\n", f"Expected \
    destination file to remain unchanged, got {destination.read_text(encoding='utf-8')}"


def test_object_store_verifies_precomputed_sha1_while_copying(tmp_path):
    """
    Test that the Objects class verifies the precomputed SHA-1 checksum of a source
    file when storing it. This test creates a source file, computes its SHA-1 checksum,
    modifies the source file, and then attempts to store it with the original checksum.
    It expects a ValueError to be raised due to the checksum mismatch, and checks that
    the object store does not contain the invalid object after the failed store attempt.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    Raises:
        ValueError: If the source file's SHA-1 checksum does not match the provided
        checksum.
    """
    objects = Objects(tmp_path / "repo")
    source = tmp_path / "source.dat"
    source.write_text("original\n", encoding="utf-8")
    sha1 = Repo.checksum(source)
    source.write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        objects.store(source, sha1, actual_sha1=sha1)
    assert not objects.contains(sha1), \
        f"Expected object store to not contain {sha1} after failed store attempt"


def test_object_store_reports_sorted_unique_missing_checksums(tmp_path):
    """
    Test that the Objects class reports missing checksums in sorted order and without
    duplicates. This test creates a source file, stores it in the object store, and then
    checks for missing checksums including the stored checksum and two additional
    checksums that are not present. It expects the missing checksums to be returned in
    sorted order and without duplicates.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    """
    objects = Objects(tmp_path / "repo")
    source = tmp_path / "source.dat"
    source.write_text("stored contents\n", encoding="utf-8")
    stored_sha1 = Repo.checksum(source)
    objects.store(source, stored_sha1)
    first_missing = "0" * 40
    second_missing = "f" * 40
    missing = objects.missing([
        second_missing, stored_sha1, first_missing, second_missing])

    assert missing == [first_missing, second_missing], \
        f"Expected missing checksums to be sorted and unique, got {missing}"


@pytest.mark.parametrize("actual_sha1", ["invalid", " " + ("a" * 40), 123])
def test_object_store_rejects_invalid_precomputed_sha1(tmp_path, actual_sha1):
    """
    Test that the Objects class raises a ValueError when provided with an invalid
    precomputed SHA-1 checksum during the store operation. This test creates a source
    file and attempts to store it with various invalid precomputed checksums. It expects
    a ValueError to be raised for each invalid checksum, indicating that the checksum
    must be exactly 40 hexadecimal characters.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
        actual_sha1 (str): An invalid precomputed SHA-1 checksum to test.
    Raises:
        ValueError: If the provided precomputed SHA-1 checksum is not exactly 40
        hexadecimal characters.
    """
    objects = Objects(tmp_path / "repo")
    source = tmp_path / "source.dat"
    source.write_text("contents\n", encoding="utf-8")
    expected_sha1 = Repo.checksum(source)

    with pytest.raises(
        ValueError,
        match="SHA-1 checksum must be exactly 40 hexadecimal characters"):
        objects.store(source, expected_sha1, actual_sha1=actual_sha1)


### Repo.commit tests ###

def test_commit_rejects_file_changed_after_add(tmp_path):
    """
    Test that the Repo class raises a ValueError if a file is changed after being added.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    Raises:
        ValueError: If the file's SHA-1 checksum does not match the expected value.
    """
    repo = Repo.init(tmp_path / "repo")
    path = repo.worktree / "data_1.txt"
    path.write_text("original data\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    path.write_text("changed after add\n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="changed after it was added"):
        repo.commit("commit staged data")


def test_commit_checks_source_when_staged_object_already_exists(tmp_path):
    """
    Test that the Repo class checks the source file's SHA-1 checksum when committing
    if the staged object already exists in the object store.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    Raises:
        ValueError: If source file's SHA-1 checksum does not match the expected value.
    """
    repo = Repo.init(tmp_path / "repo")
    path = repo.worktree / "data_1.txt"
    path.write_text("first version\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("first version")
    path.write_text("second version\n", encoding="utf-8")
    repo.add(".")
    repo.commit("second version")
    path.write_text("first version\n", encoding="utf-8")
    repo.add(".")
    path.write_text("changed after add\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after it was added"):
        repo.commit("restore first version")


def test_commit_hashes_tracked_files_once_in_parallel(monkeypatch, tmp_path):
    """
    Test that the Repo.commit() method calls checksum_many once for all tracked files
    and does not recalculate checksums for files that already exist in the object store.
    This test creates a repository with two tracked files, adds them, and then commits.
    It uses monkeypatch to replace the checksum_many method with a custom function that
    records the calls made to it. It also replaces the _calculate_sha1 method in the
    Objects class during the test.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        AssertionError: If the _calculate_sha1 method is called during the test, which
        would indicate that the commit recalculated a source checksum instead of using
        the existing one.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["data_1.txt", "data_2.txt"])
    repo.add("data_{number}.txt")
    original_checksum_many = repo.checksum_many
    calls = []
    def record_checksum_many(paths):
        """Record calls to checksum_many. Return the original checksum_many result."""
        calls.append(list(paths))
        return original_checksum_many(paths)
    def unexpected_sequential_hash(*args, **kwargs):
        """This function should not be called during the test.
        If it is, raise an AssertionError."""
        raise AssertionError("Objects.store recalculated a source checksum")
    monkeypatch.setattr(repo, "checksum_many", record_checksum_many)
    monkeypatch.setattr(repo.objects, "_calculate_sha1", unexpected_sequential_hash)

    assert repo.commit("initial data") is True, \
        "Expected commit to succeed for initial data"
    assert len(calls) == 1, \
        "Expected checksum_many to be called once for all tracked files"
    assert set(calls[0]) == {
        repo.worktree / "data_1.txt", repo.worktree / "data_2.txt"}, f"Expected \
            checksum_many to be called once with all tracked files, got {calls[0]}"
    assert all(repo.objects.contains(row["sha1"])
               for _, row in repo.state.data.iterrows()), \
        "All staged objects should exist in the object store after commit."


def test_commit_hashes_only_manifest_rows_changed_since_head(monkeypatch, tmp_path):
    """
    Test that the Repo.commit() method only calls checksum_many for manifest rows that
    have changed since the last commit. This test creates a repository with two tracked
    files, adds them, and commits. It then modifies one of the files, adds it,
    and commits again. It uses monkeypatch to replace the checksum_many method with a
    custom function that records the calls made to it.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["data_1.txt", "data_2.txt"])
    repo.add("data_{number}.txt")
    repo.commit("initial data")
    changed_path = repo.worktree / "data_1.txt"
    changed_path.write_text("changed contents\n", encoding="utf-8")
    repo.add(".")
    calls = []
    original_checksum_many = repo.checksum_many
    def record_checksum_many(paths):
        """Record calls to checksum_many. Return the original checksum_many result."""
        paths = list(paths)
        calls.append(paths)
        return original_checksum_many(paths)
    monkeypatch.setattr(repo, "checksum_many", record_checksum_many)
    repo.commit("update one file")

    assert calls == [[changed_path]], \
        f"Expected checksum_many to be called only for the changed file, got {calls}"


def test_checksum_many_hashes_duplicate_paths_once(monkeypatch, tmp_path):
    """
    Test that the Repo.checksum_many() method only hashes each unique path once, even if
    the same path is provided multiple times. This test creates two files, calls
    checksum_many with a list containing duplicate paths, and uses monkeypatch to
    replace the checksum method with a custom function that records the calls made to it
    It checks that the result contains the correct checksums and that each unique path
    was hashed only once.
    Args:
        monkeypatch: pytest fixture that allows for dynamic modification of classes
        and functions.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    first = tmp_path / "first.dat"
    second = tmp_path / "second.dat"
    calls = []
    def fake_checksum(path):
        """Fake checksum function that returns the filename as the checksum and records
        the calls made to it."""
        calls.append(path)
        return path.name
    monkeypatch.setattr(Repo, "checksum", staticmethod(fake_checksum))
    result = Repo.checksum_many([first, first, second])

    assert result == {first: "first.dat", second: "second.dat"}, \
        f"Expected checksum_many to return correct checksums, got {result}"
    assert calls.count(first) == 1, \
        f"Expected checksum to be called once for each unique path, got calls: {calls}"
    assert calls.count(second) == 1, \
        f"Expected checksum to be called once for each unique path, got calls: {calls}"


def test_readding_changed_file_allows_commit(tmp_path):
    """
    Test that re-adding a changed file allows the commit to succeed.
    Args:
        tmp_path (Path): A temporary directory provided by pytest.
    """
    repo = Repo.init(tmp_path / "repo")
    path = repo.worktree / "data_1.txt"
    path.write_text("original data\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    path.write_text("changed after add\n", encoding="utf-8")
    repo.add("data_{number}.txt")

    assert repo.commit("commit updated data") is True, \
        "Expected commit to succeed after re-adding changed file."


### row_to_path tests ###

def test_row_to_path_returns_safe_relative_path():
    """
    Test that row_to_path returns a safe relative Path object based on the provided
    row and format string.
    """
    path = row_to_path({
            "directory": "science",
            "source": "M87"},
        "{directory}/{source}.fits")

    assert path == Path("science/M87.fits"), \
        f"Expected Path('science/M87.fits'), got {path}"


@pytest.mark.parametrize(
    ("fmt", "row"),[
        ("../../outside/{name}", {"name": "data"}),
        ("/absolute/{name}", {"name": "data"}),
        ("C:/outside/{name}", {"name": "data"}),
        (r"..\outside\{name}", {"name": "data"}),
        ("{directory}/{name}", {"directory": "..", "name": "data"}),
        (".hm/{name}", {"name": "config.yml"}),
        (".git/{name}", {"name": "config"})])
def test_row_to_path_rejects_unsafe_paths(fmt, row):
    """
    Test that row_to_path raises a ValueError for unsafe paths.
    Args:
        fmt (str): The format string for the path.
        row (dict): The row dictionary containing values to format into the path.
    Raises:
        ValueError: If the resulting path is unsafe (e.g., absolute, outside the repo,
        or in .hm/.git).
    """
    with pytest.raises(ValueError):
        row_to_path(row, fmt)


### fmt_entries_from_config tests ###

def test_fmt_entries_from_config_accepts_mapping_or_list():
    """
    Test that fmt_entries_from_config accepts a mapping or a list of mappings for the
    "data" key in the configuration. It should return a list of valid mapping entries.
    """
    entry = {"fmt": "data_{number}.txt", "db": "data.tsv"}

    assert fmt_entries_from_config({"data": entry}) == [entry], f"Expected list with \
        single mapping entry, got {fmt_entries_from_config({'data': entry})}"
    assert fmt_entries_from_config({
        "data": [{"file": "README.md"}, entry, ]}) == [entry], \
        f"Expected list with only valid mapping entries, \
            got {fmt_entries_from_config({'data': [{'file': 'README.md'}, entry]})}"
    assert fmt_entries_from_config({}) == [], \
        f"Expected empty list for missing 'data' key, got {fmt_entries_from_config({})}"


@pytest.mark.parametrize(
    "data, message",[
        ("invalid", 'config "data" must be a mapping or list'),
        ([{"fmt": "data_{number}.txt"}, "invalid"], "data entry 1")])
def test_fmt_entries_from_config_rejects_invalid_sections(data, message):
    """
    Test that fmt_entries_from_config raises a ValueError for invalid data sections.
    Args:
        data: The invalid data section to test.
        message: The expected error message to match in the ValueError.
    Raises:
        ValueError: If the data section is not a mapping or list, or if any entry
        in the list is not a mapping.
    """
    with pytest.raises(ValueError, match=message):
        fmt_entries_from_config({"data": data})


### fmt_fields tests ###

def test_fmt_fields_returns_unique_fields_in_original_order():
    """
    Test that fmt_fields returns a list of unique field names in the order they first
    appear in the format string. It checks that the function correctly identifies and
    returns the fields without duplicates.
    """
    fmt = "{source}/{source}_{scan:03d}.{format}"

    assert fmt_fields(fmt) == ["source", "scan", "format"], \
        f"Expected unique fields in original order, got {fmt_fields(fmt)}"


### single_data_fmt tests ###

@pytest.mark.parametrize(
    ("config", "expected"),[(
        {"data": [{"fmt": " data_{number}.fits "}]}, "data_{number}.fits"), ({}, None),
        ({"data": []}, None),
        ({"data": [{"fmt": "first"}, {"fmt": "second"}]}, None),
        ({"data": [{"fmt": "   "}]}, None),
        ({"data": [{"fmt": 123}]}, None),
        ({"data": "invalid"}, None),
        (None, None),
        ([], None),
        ({"data": [{}]}, None),])
def test_single_data_fmt(config, expected):
    """
    Test that single_data_fmt returns the expected format string or None based on the
    provided configuration. It checks various cases, including valid single entries,
    empty lists, multiple entries, and invalid formats.
    Args:
        config: The configuration dictionary to test.
        expected: The expected return value from single_data_fmt.
    """
    assert single_data_fmt(config) == expected, f"Expected single_data_fmt({config}) \
        to be {expected}, got {single_data_fmt(config)}"


### Repo.add_worktree tests ###

def test_add_worktree_restores_target_branch_data(tmp_path):
    """
    Test that adding a worktree for a branch restores the correct data for that branch.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("main contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.checkout("experiment")
    data_path.write_text("experiment contents\n", encoding="utf-8")
    repo.add(".")
    repo.commit("experiment data")
    repo.checkout("main")

    assert data_path.read_text(encoding="utf-8") == "main contents\n", \
        f"Expected 'main contents\\n', got {data_path.read_text(encoding='utf-8')}"

    repo.add_worktree("experiment")
    linked_file = tmp_path / "experiment" / "data_1.txt"
    assert linked_file.read_text(encoding="utf-8") == "experiment contents\n", \
     f"Expected 'experiment contents\\n', got {linked_file.read_text(encoding='utf-8')}"


def test_add_worktree_rejects_destination_escape(tmp_path):
    """
    Test that adding a worktree with a destination path outside the repository
    raises a ValueError.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If the destination path is outside the repository worktree.
    """
    repo = Repo.init(tmp_path / "repo")
    outside_name = f"{tmp_path.name}-outside"
    outside = tmp_path.parent / outside_name

    assert not outside.exists(), \
        f"Expected outside directory {outside} to not exist, but it does."

    with pytest.raises(ValueError, match="invalid branch name"):
        repo.add_worktree(f"../{outside_name}")
    assert not outside.exists(), \
        f"Expected outside directory {outside} to not exist, but it does."


def test_add_worktree_rejects_current_worktree_destination(tmp_path):
    """
    Test that adding a worktree with the same name as the current worktree
    raises a ValueError.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        ValueError: If the destination path is the same as the current worktree.
    """
    repo = Repo.init(tmp_path / "repo")

    with pytest.raises(ValueError, match="cannot be the current worktree"):
        repo.add_worktree("repo")


def test_add_worktree_accepts_an_explicit_destination_path(tmp_path):
    """
    Test that an explicit destination path is honored, including one that is neither
    a sibling of the current worktree nor named after the branch.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("main contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.checkout("experiment")
    data_path.write_text("experiment contents\n", encoding="utf-8")
    repo.add(".")
    repo.commit("experiment data")
    repo.checkout("main")

    destination = tmp_path / "nested" / "chosen-name"
    repo.add_worktree("experiment", path=destination)

    linked_file = destination / "data_1.txt"
    assert linked_file.read_text(encoding="utf-8") == "experiment contents\n", \
        f"Expected 'experiment contents\\n', got " \
        f"{linked_file.read_text(encoding='utf-8')}"
    # the default sibling destination must not have been created as well
    assert not (tmp_path / "experiment").exists(), \
        f"Expected no worktree at {tmp_path / 'experiment'}, but one exists."


@pytest.mark.parametrize("destination", ["inside", "inside/deeper"])
def test_add_worktree_rejects_destination_nested_in_current_worktree(
        tmp_path, destination):
    """
    Test that a destination inside the current worktree is rejected, since the outer
    worktree would otherwise index the nested worktree's data files.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        destination: relative destination path inside the current worktree.
    """
    repo = Repo.init(tmp_path / "repo")
    nested = repo.worktree / destination

    with pytest.raises(ValueError, match="nested inside the current worktree"):
        repo.add_worktree("experiment", path=nested)
    assert not nested.exists(), \
        f"Expected nested directory {nested} to not exist, but it does."


def test_add_worktree_preserves_unrelated_existing_destination(tmp_path):
    """
    Test that adding a worktree to an existing directory that is not a worktree
    raises a DestinationExistsError and does not overwrite unrelated files.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        DestinationExistsError: If the destination directory already exists and
        is not a worktree.
    """
    repo = Repo.init(tmp_path / "repo")
    destination = tmp_path / "experiment"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(DestinationExistsError, match="already exists"):
        repo.add_worktree("experiment")
    assert sentinel.read_text(encoding="utf-8") == "do not overwrite\n", \
        f"Expected sentinel file to remain unchanged, but got: \
            {sentinel.read_text(encoding='utf-8')}"
    assert not (destination / ".hm").exists(), \
        f"Expected no .hm directory in {destination}, but found one."


def test_add_worktree_rejects_invalid_data_config_before_creation(tmp_path):
    """
    Test that adding a worktree fails if the repo has an invalid data configuration.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        RuntimeError: If the repository's data configuration is invalid
        (not exactly one entry).
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = []
    repo.dothm.dump(repo.state)
    repo.dothm.index.commit("invalid data configuration")
    destination = tmp_path / "experiment"

    with pytest.raises(RuntimeError, match="requires exactly one data entry"):
        repo.add_worktree("experiment")
    assert not destination.exists(), \
        f"Expected destination {destination} to not exist, but it does."


def test_list_worktrees_reports_only_the_active_worktree_initially(tmp_path):
    """
    Test that a freshly initialized repository lists exactly one worktree, its own.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")

    entries = repo.list_worktrees()

    assert len(entries) == 1, f"Expected exactly one worktree, got {len(entries)}"
    entry = entries[0]
    assert entry["current"] is True, "Expected the only worktree to be the active one."
    assert entry["branch"] == "main", \
        f"Expected branch 'main', got {entry['branch']}"
    assert Path(entry["path"]).resolve() == Path(repo.worktree).resolve(), \
        f"Expected path {repo.worktree}, got {entry['path']}"


def test_list_worktrees_reports_linked_worktrees_and_their_branches(tmp_path):
    """
    Test that a linked worktree is reported with its own branch and data directory,
    and that only the active worktree is marked as current.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("main contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.checkout("experiment")
    data_path.write_text("experiment contents\n", encoding="utf-8")
    repo.add(".")
    repo.commit("experiment data")
    repo.checkout("main")
    repo.add_worktree("experiment")

    entries = repo.list_worktrees()
    by_branch = {entry["branch"]: entry for entry in entries}

    assert set(by_branch) == {"main", "experiment"}, \
        f"Expected branches main and experiment, got {sorted(by_branch)}"
    assert by_branch["main"]["current"] is True, \
        "Expected the main worktree to be the active one."
    assert by_branch["experiment"]["current"] is False, \
        "Expected the linked worktree to not be the active one."
    # the reported path is the data directory, not the ".hm" directory git tracks
    assert Path(by_branch["experiment"]["path"]).resolve() \
        == (tmp_path / "experiment").resolve(), \
        f"Expected path {tmp_path / 'experiment'}, " \
        f"got {by_branch['experiment']['path']}"
    assert Path(by_branch["experiment"]["dothm"]).name == ".hm", \
        f"Expected a \".hm\" dothm path, got {by_branch['experiment']['dothm']}"


def test_add_worktree_wraps_existing_branch_link_failure(monkeypatch, tmp_path):
    """
    Test that adding a worktree wraps a failure in the dothm.link method with a
    RuntimeError.
    Args:
        monkeypatch: pytest fixture for temporarily modifying attributes.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        RuntimeError: If the dothm.link method fails during worktree creation.
        DothmError: The underlying cause of the failure, wrapped by RuntimeError.
    """
    repo = Repo.init(tmp_path / "repo")
    _write_files(repo.worktree, ["data_1.txt"])
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.dothm.git.branch("experiment")
    def fail_link(*args, **kwargs):
        """Simulate a failure in the dothm.link method."""
        raise DothmError("link failed")
    monkeypatch.setattr(repo.dothm, "link", fail_link)

    with pytest.raises(RuntimeError, match="failed to create worktree") as exc_info:
        repo.add_worktree("experiment")
    assert isinstance(exc_info.value.__cause__, DothmError), \
        f"Expected cause to be DothmError, but got {type(exc_info.value.__cause__)}"
    assert not (tmp_path / "experiment").exists(), \
        f"Expected destination {tmp_path / 'experiment'} to not exist, but it does."


def test_add_worktree_checks_for_missing_objects_before_creation(tmp_path):
    """
    Test that adding a worktree checks for missing objects in the object store before
    creating the worktree. If an object is missing, it should raise a FileNotFoundError
    and not create the worktree.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        FileNotFoundError: If object required by worktree is missing from object store.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    sha1 = repo.state.data.iloc[0]["sha1"]
    stored_object = (repo.objects.root / sha1[:2] / sha1[2:])
    stored_object.unlink()
    destination = tmp_path / "experiment"

    with pytest.raises(FileNotFoundError, match="missing object"):
        repo.add_worktree("experiment")
    assert not destination.exists(), \
        f"Expected destination {destination} to not exist, but it does."


@pytest.mark.parametrize(
    "branch_name",[
        "",
        "   ",
        "-b",
        "../escape",
        "bad name",
        "bad..name",
        "branch~1"])
def test_add_worktree_rejects_invalid_branch_names(tmp_path, branch_name):
    """
    Test that adding a worktree with an invalid branch name raises a ValueError.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
        branch_name: A parameterized invalid branch name to test.
    Raises:
        ValueError: If the branch name is invalid according to the repository rules.
    """
    repo = Repo.init(tmp_path / "repo")

    with pytest.raises(ValueError, match="branch name"):
        repo.add_worktree(branch_name)


def test_add_worktree_cleans_up_after_restore_failure(monkeypatch, tmp_path):
    """
    Test that if the objects.restore method fails during worktree creation, the
    add_worktree method cleans up the partially created worktree.
    Args:
        monkeypatch: pytest fixture for temporarily modifying attributes.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        RuntimeError: If the objects.restore method fails, wrapped in a RuntimeError.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    repo.dothm.git.branch("experiment")
    def fail_restore(*args, **kwargs):
        """Simulate a failure in the objects.restore method."""
        raise OSError("disk failure")
    monkeypatch.setattr(repo.objects, "restore", fail_restore)
    target = tmp_path / "experiment"

    with pytest.raises(RuntimeError, match="failed to populate worktree") as exc_info:
        repo.add_worktree("experiment")
    assert isinstance(exc_info.value.__cause__, OSError), \
        f"Expected cause to be OSError, but got {type(exc_info.value.__cause__)}"
    assert not target.exists(), \
        f"Expected destination {target} to not exist, but it does."

    worktree_listing = repo.dothm.git.worktree("list", "--porcelain")
    assert str(target / ".hm") not in worktree_listing, f"Expected worktree \
        {target} to be removed from .hm/worktrees, but found: {worktree_listing}"


def test_add_worktree_removes_new_branch_after_restore_failure(monkeypatch, tmp_path):
    """
    Test that if the objects.restore method fails during worktree creation, the
    add_worktree method removes the newly created branch from the repository.
    Args:
        monkeypatch: pytest fixture for temporarily modifying attributes.
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        RuntimeError: If the objects.restore method fails, wrapped in a RuntimeError.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data_1.txt"
    data_path.write_text("contents\n", encoding="utf-8")
    repo.add("data_{number}.txt")
    repo.commit("main data")
    def fail_restore(*args, **kwargs):
        """Simulate a failure in the objects.restore method."""
        raise OSError("disk failure")
    monkeypatch.setattr(repo.objects, "restore", fail_restore)

    with pytest.raises(RuntimeError, match="failed to populate worktree"):
        repo.add_worktree("experiment")
    assert "experiment" not in {head.name for head in repo.dothm.heads}, \
        f"Expected branch 'experiment' to be removed after failed worktree creation, \
            but found: {[head.name for head in repo.dothm.heads]}"
    assert not (tmp_path / "experiment").exists(), \
        f"Expected destination {tmp_path / 'experiment'} to not exist, but it does."


### state tests ###

def test_state_replace_empty_uses_incoming_schema():
    """
    Test that State.replace() uses the schema of the incoming DataFrame when the
    current state is empty. This test creates a State object with an initial DataFrame,
    then replaces it with an empty DataFrame that has a different schema. It checks that
    the resulting state has the schema of the incoming DataFrame.
    """
    state = State(
        data=pd.DataFrame([{"sha1": "a" * 40, "old_parameter": "old"}]))
    replacement = pd.DataFrame(columns=["sha1", "path", "new_parameter"])
    state.replace(replacement)

    assert state.data.empty, "Expected state.data to be empty after replacement"
    assert list(state.data.columns) == ["sha1", "new_parameter"], \
        f"Expected columns ['sha1', 'new_parameter'], got {list(state.data.columns)}"


def test_state_update_static_format_keeps_only_latest_row():
    """
    Test that State.update() with a static format keeps only the latest row for each
    unique identifier. This test creates a State object with an initial row, then
    updates it with a new row having the same unique identifier.
    It checks that the resulting state contains only the latest row.
    """
    state = State(data=pd.DataFrame([{"sha1": "a" * 40}]))
    replacement = pd.DataFrame([{"sha1": "b" * 40, "path": "README.txt"}])
    state.update(replacement)
    state.update(replacement)

    assert state.data.to_dict("records") == [{"sha1": "b" * 40}], \
        f"Expected only the latest row to be kept, got {state.data.to_dict('records')}"


def test_state_normalizes_missing_parameters_to_blank_strings():
    """
    Test that State.replace() normalizes missing values in the incoming DataFrame to
    blank strings. This test creates a State object and replaces it with a DataFrame
    containing missing values (None, pd.NA, and "NA"). It checks that the resulting
    state has these values normalized to blank strings.
    """
    incoming = pd.DataFrame({
            "sha1": ["first", "second", "third"],
            "path": ["first.dat", "second.dat", "third.dat"],
            "value": [None, pd.NA, "NA"]})
    state = State()
    state.replace(incoming)

    assert state.data["value"].tolist() == ["", "", "NA"], f"Expected missing values to\
          be normalized to blank strings, got {state.data['value'].tolist()}"


def test_state_update_resets_index_after_replacing_row():
    """
    Test that State.update() resets the index after replacing a row with a new one.
    This test creates a State object with an initial row, then updates it with a new row
    having the same unique identifier. It checks that the resulting DataFrame has its
    index reset to start from 0."""
    state = State(
        data=pd.DataFrame({"sha1": ["old"], "number": ["1"]}, index=[7]))
    incoming = pd.DataFrame({"sha1": ["new"], "number": ["1"]})
    state.update(incoming)

    assert state.data.to_dict("records") == [{"sha1": "new", "number": "1"}], \
      f"Expected state.data to contain the new row, got {state.data.to_dict('records')}"
    assert list(state.data.index) == [0], \
        f"Expected index to be reset to [0], got {list(state.data.index)}"


def test_state_rejects_nonempty_data_without_sha1():
    """
    Test that State.replace() raises a ValueError when the incoming DataFrame does not
    contain a "sha1" column. This test creates a State object and attempts to replace
    it with such a DataFrame.
    Raises:
        ValueError: If the incoming DataFrame does not contain a "sha1" column.
    """
    state = State()
    incoming = pd.DataFrame({"number": ["1"]})

    with pytest.raises(ValueError, match='must contain a "sha1" column'):
        state.replace(incoming)


def test_state_update_and_replace_share_data_normalization():
    """
    Test that State.update() and State.replace() share the same data normalization logic
    This test creates a State object and updates it with incoming data, then replaces it
    with the same incoming data. It checks that both methods produce the same normalized
    DataFrame."""
    incoming = pd.DataFrame({"sha1": ["abc123"], "path": ["item_1.dat"], "number": [1]})
    updated = State()
    updated.update(incoming)
    replaced = State()
    replaced.replace(incoming)
    expected = pd.DataFrame({"sha1": ["abc123"], "number": ["1"]})

    pd.testing.assert_frame_equal(updated.data, expected, check_dtype=False)
    pd.testing.assert_frame_equal(replaced.data, expected, check_dtype=False)


### repo_manifest tests ###

def test_manifest_frame_normalizes_missing_and_integral_float_values():
    """
    Test that manifest_frame_from_pf normalizes missing values and integral float values
    to the expected string representations. This test creates a DataFrame with missing
    and integral float values, then calls manifest_frame_from_pf to normalize it.
    It checks that the resulting DataFrame has the expected normalized values.
    """
    frame = pd.DataFrame({
            "sha1": ["first", "second", "third"],
            "value": [pd.NA, 1.0, float("inf")]})
    result = manifest_frame_from_pf(frame, "{value}.dat")
    values = result["value"].tolist()

    assert pd.isna(values[0]), f"Expected first value to be NaN, got {values[0]}"
    assert values[1:] == ["1", "inf"], f"Expected values ['1', 'inf'], got {values[1:]}"


def test_manifest_entries_share_canonical_path_generation():
    """
    Test that iter_manifest_entries and manifest_map share the same canonical path
    generation logic. This test creates a State object with a specific configuration and
    data, then checks that both functions produce consistent results for the manifest
    entries and mapping.
    """
    state = State(
        config={"data": [{"fmt": "{folder}/{name}.dat"}]},
        data=pd.DataFrame({"sha1": ["ABC123"], "folder": ["nested"], "name": ["item"]}))

    assert list(iter_manifest_entries(state)) == [(Path("nested/item.dat"), "ABC123")],\
      f"Expected iter_manifest_entries to yield [(Path('nested/item.dat'), 'ABC123')],\
            got {list(iter_manifest_entries(state))}"
    assert manifest_map(state) == {"nested/item.dat": "ABC123"}, \
        f"Expected manifest_map to return {{'nested/item.dat': 'ABC123'}}, \
            got {manifest_map(state)}"


def test_manifest_entries_empty_when_config_has_no_data_fmt():
    """
    If config has no fmt entries, iter_manifest_entries should produce no rows and
    manifest_map should be empty, even when state.data has rows.
    """
    state = State(config={"data": [{"file": "README.md"}]},
                  data=pd.DataFrame({"sha1": ["ABC123"], "name": ["item"]}))

    assert list(iter_manifest_entries(state)) == [], f"Expected no manifest entries \
        when no data fmt exists, got {list(iter_manifest_entries(state))}"
    assert manifest_map(state) == {}, f"Expected empty manifest map when no data fmt \
        exists, got {manifest_map(state)}"


def test_manifest_map_uses_explicit_fmt_override():
    """
    manifest_map should honor an explicit fmt argument even if config does not define
    a data fmt.
    """
    state = State(
        config={},
        data=pd.DataFrame({
            "sha1": ["ABC123"],
            "folder": ["nested"],
            "name": ["item"]}))
    actual = manifest_map(state, fmt="{folder}/{name}.dat")

    assert actual == {"nested/item.dat": "ABC123"}, \
        f"Expected explicit fmt override mapping, got {actual}"


### repo_worktree tests ###

def test_worktree_changes_accepts_uppercase_expected_checksum(tmp_path):
    """
    Test that worktree_changes accepts an uppercase expected checksum and correctly
    identifies that there are no modified or missing files. This test creates a repo,
    adds a file, computes its checksum, and then calls worktree_changes with the
    uppercase version of the checksum. It checks that the returned modified and missing
    lists are empty.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    data_path = repo.worktree / "data.dat"
    data_path.write_text("contents\n", encoding="utf-8")
    checksum = repo.checksum(data_path)
    modified, missing = worktree_changes(repo, {"data.dat": checksum.upper()})

    assert modified == [], f"Expected no modified files, got {modified}"
    assert missing == [], f"Expected no missing files, got {missing}"


### repo_state tests ###

def test_parse_data_tsv_preserves_na_tokens_and_blank_values():
    """
    Test that _parse_data_tsv correctly preserves 'NA' tokens and blank values when
    parsing a TSV string. This test provides a TSV string with 'NA' and blank values
    and checks that the resulting DataFrame has the expected values.
    """
    frame = _parse_data_tsv("sha1\tname\n""first\tNA\n""second\t\n")

    assert frame["name"].tolist() == ["NA", ""], \
        f"Expected ['NA', ''], got {frame['name'].tolist()}"


def test_parse_data_tsv_empty_text_returns_empty_state_schema():
    """
    Empty TSV text should parse to an empty DataFrame with the canonical State schema.
    """
    frame = _parse_data_tsv("   \n")

    assert frame.empty, "Expected parsed frame to be empty for blank TSV text"
    assert list(frame.columns) == ["sha1"], \
        f"Expected canonical columns ['sha1'], got {list(frame.columns)}"


def test_load_head_state_without_commits_uses_current_config_and_empty_data(tmp_path):
    """
    With no commits in .hm, load_head_state should return a copy of current config/meta
    and an empty data table.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config = {"data": [{"fmt": "data_{number}.txt"}]}
    repo.state.meta = {"nested": {"value": "original"}}
    repo.state.data = pd.DataFrame({"sha1": ["abc123"], "number": ["1"]})
    head_state = load_head_state(repo)
    head_state.config["data"][0]["fmt"] = "changed"
    head_state.meta["nested"]["value"] = "changed"

    assert head_state.data.empty, \
        f"Expected empty data for no-commit HEAD fallback, got {head_state.data}"
    assert list(head_state.data.columns) == ["sha1"], \
        f"Expected canonical columns ['sha1'], got {list(head_state.data.columns)}"
    assert repo.state.config["data"][0]["fmt"] == "data_{number}.txt", \
        f"Expected original repo config to remain unchanged, \
            got {repo.state.config['data'][0]['fmt']}"
    assert repo.state.meta["nested"]["value"] == "original", f"Expected original repo \
        meta to remain unchanged, got {repo.state.meta['nested']['value']}"


def test_new_branch_state_is_independent_from_current_state(tmp_path):
    """
    Test that the state of a new branch is independent from the current branch's state.
    This test creates a repository, sets up an initial state, and then loads the state
    for a new branch. It checks that modifications to the new branch's state do not
    affect the original branch's state.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    repo = Repo.init(tmp_path / "repo")
    repo.state.config = {"data": [{"fmt": "data_{number}.txt"}]}
    repo.state.meta = {"nested": {"value": "original"}}
    repo.state.data = pd.DataFrame({"sha1": ["abc123"], "number": ["1"]})
    copied = load_branch_data(repo, "new-branch")
    copied.config["data"][0]["fmt"] = "changed"
    copied.meta["nested"]["value"] = "changed"
    copied.data.loc[0, "number"] = "2"

    assert repo.state.config["data"][0]["fmt"] == ("data_{number}.txt"), f"Expected \
        config fmt 'data_{{number}}.txt', got {repo.state.config['data'][0]['fmt']}"
    assert repo.state.meta["nested"]["value"] == "original", \
        f"Expected meta value 'original', got {repo.state.meta['nested']['value']}"
    assert repo.state.data.loc[0, "number"] == "1", \
        f"Expected number '1', got {repo.state.data.loc[0, 'number']}"


### Worktree tests ###

def test_worktree_rejects_existing_file(tmp_path):
    """
    Test that Worktree raises a NotADirectoryError when attempting to create a worktree
    at a path that is an existing file. This test creates a temporary file and then
    attempts to create a Worktree object with that file path,
    expecting the error to be raised.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        NotADirectoryError: If the provided path is an existing file.
    """
    file_path = tmp_path / "data.txt"
    file_path.write_text("contents\n", encoding="utf-8")

    with pytest.raises(NotADirectoryError, match="is not a directory"):
        Worktree(file_path)


def test_worktree_init_rejects_existing_file(tmp_path):
    """
    Test that Worktree.init() raises a NotADirectoryError when attempting to initialize
    a worktree at a path that is an existing file. This test creates a temporary file
    and then calls Worktree.init() with that file path, expecting the error to be raised
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        NotADirectoryError: If the provided path is an existing file.
    """
    file_path = tmp_path / "data.txt"
    file_path.write_text("contents\n", encoding="utf-8")

    with pytest.raises(NotADirectoryError, match="is not a directory"):
        Worktree.init(file_path)


def test_worktree_join_returns_unvalidated_path(tmp_path):
    """
    Test that Worktree.join() returns a Path object without validating its existence.
    This test creates a Worktree object and calls the join method with a filename that
    does not exist. It checks that the returned object is a Path and that it matches the
    expected path, even though the file has not been created yet.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    """
    worktree = Worktree(tmp_path)
    child = worktree / "not-created-yet.dat"

    assert isinstance(child, Path), \
        f"Expected child to be a Path, got {type(child)}"
    assert not isinstance(child, Worktree), \
        f"Expected child to be a Path, got {type(child)}"
    assert child == tmp_path / "not-created-yet.dat", \
        f"Expected {tmp_path / 'not-created-yet.dat'}, got {child}"


def test_worktree_rejects_missing_path(tmp_path):
    """
    Test that Worktree raises a FileNotFoundError when initialized with a path that
    does not exist. This test creates a temporary directory and attempts to create a
    Worktree object with a subdirectory that has not been created, expecting the error
    to be raised.
    Args:
        tmp_path: pytest fixture that provides a temporary directory for the test.
    Raises:
        FileNotFoundError: If the provided path does not exist.
    """
    missing_path = tmp_path / "missing"

    with pytest.raises(FileNotFoundError, match="not found"):
        Worktree(missing_path)


### error.py tests ###

def test_clone_error_replaces_complete_resolved_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    clone_path = Path("destination/.hm")
    resolved_clone_path = clone_path.resolve()
    error = GitCommandError("git clone", 128,
             stderr=(f"fatal: destination path '{resolved_clone_path}' already exists"))
    result = CloneError.from_git_command(
        error, clone_path=clone_path, display_path="destination")

    assert str(result) == ("fatal: destination path 'destination' already exists"), \
        f"Expected error message to replace resolved path with display path, \
            got: {str(result)}"


def test_checkout_remote_branch(tmp_path):
    '''
    Test that checkout can create a local branch from a remote branch.
    '''


    # Create a source repository that will act as the remote repository.
    source = Repo.init(tmp_path / "source")

    (source.worktree / "data.txt").write_text(
        "main contents\n",
        encoding="utf-8",
    )

    # Configure the repository's data format and commit the initial state.
    source.add("data.txt")
    source.commit("Initial commit")


    # Clone the source while it only has the main branch.
    clone = Repo.clone(
        str(source.dothm.path),
        tmp_path / "clone",
        fetch_data=False,
    )

    # The cloned repository should have the tracked file in its worktree.
    (clone.worktree / "data.txt").write_text(
        "main contents\n",
        encoding="utf-8",
    )

    # Create the experiment branch in the source repository AFTER cloning.
    source.checkout("experiment")

    # Change the file on the experiment branch and commit it.
    (source.worktree / "data.txt").write_text(
        "experiment contents\n",
        encoding="utf-8",
    )
    source.add(".")
    source.commit("Experiment commit")
    # Change the file on the experiment branch and commit it.

    print("SOURCE OBJECTS AFTER EXPERIMENT COMMIT:")
    for p in (source.dothm.path / "objects").rglob("*"):
        if p.is_file():
            print(p)

    print("CLONE OBJECTS BEFORE FETCH:")
    for p in (clone.dothm.path / "objects").rglob("*"):
        if p.is_file():
            print(p)

    # Fetch the newly-created remote branch into the clone.
    clone.dothm.git.fetch("origin")

    print("CLONE OBJECTS AFTER FETCH:")
    for p in (clone.dothm.path / "objects").rglob("*"):
        if p.is_file():
            print(p)

    print("REMOTE EXPERIMENT DATA:")
    print(clone.dothm.git.show("origin/experiment:data.tsv"))

    # The experiment branch should exist remotely but not locally.
    local_branches = {head.name for head in clone.dothm.heads}
    assert "experiment" not in local_branches

    remote_branches = {
        ref.remote_head
        for remote in clone.dothm.remotes
        for ref in remote.refs
        if ref.remote_head != "HEAD"
    }
    assert "experiment" in remote_branches
    print("\n=== CLONE BRANCHES ===")
    print(clone.dothm.git.branch("-a"))

    print("\n=== REMOTE REFS ===")
    for remote in clone.dothm.remotes:
        for ref in remote.refs:
            print(ref)

    print("\n=== REMOTE EXPERIMENT COMMIT ===")
    print(clone.dothm.git.rev_parse("origin/experiment"))

    print("\n=== REMOTE EXPERIMENT FILES ===")
    print(clone.dothm.git.ls_tree("-r", "--name-only", "origin/experiment"))

    # This is the behavior we are testing:
    # checkout should create a local tracking branch from the remote branch.
    clone.checkout("experiment")

    # Checkout should have created and switched to the local branch.
    assert clone.dothm.active_branch.name == "experiment"

    # The worktree should now contain the version from the remote branch.
    assert (clone.worktree / "data.txt").read_text(
        encoding="utf-8"
    ) == "experiment contents\n"