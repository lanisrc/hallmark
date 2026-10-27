"""Edge cases of repository configuration, filename matching and glob debugging."""

from pathlib import Path

import pandas as pd
import pytest

from hallmark import ParaFrame, Repo
from hallmark.repo.config import (
    branch_filename_format,
    fmt_entries_from_config,
    match_file,
    normalize_remotes,
    tracked_filename_format,
    validate_tsv_filename)
from hallmark.repo.state import State


FMT = "a{a}_i{i}.h5"
ORIGIN = {"name": "origin", "url": "https://origin.test/data/"}
MIRROR = {"name": "mirror", "url": "https://mirror.test/data/"}


def _write(root, *names):
    for name in names:
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"contents of {name}\n", encoding="utf-8")


def _saved_config(repo):
    """Reopen the repository to read the configuration later commands see."""
    return Repo(repo.worktree).state.config


### remotes ###

def test_normalize_remotes_names_remotes_given_as_strings():
    assert normalize_remotes(["origin", "mirror"]) == [
        {"name": "origin"}, {"name": "mirror"}]


@pytest.mark.parametrize("remotes,message", [
    (42, "remotes must be a string, dictionary, or sequence"),
    ([42], "remote 0 must be a string or dictionary"),
    (["origin", dict(ORIGIN)],
     "remote 1 duplicates the name of remote 0: 'origin'"),
    ([{"url": ORIGIN["url"]}, {"url": MIRROR["url"]}],
     r"unnamed remote index\(es\): 0, 1"),
], ids=["not_a_sequence", "not_an_entry", "duplicate_name", "unnamed"])
def test_normalize_remotes_rejects_malformed_entries(remotes, message):
    with pytest.raises(ValueError, match=message):
        normalize_remotes(remotes)


@pytest.fixture
def two_remote_repo(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["remote"] = [dict(ORIGIN), dict(MIRROR)]
    repo.dothm.save_state(repo.state)
    return repo


def test_set_config_adds_a_new_named_remote_beside_existing_ones(two_remote_repo):
    backup = {"name": "backup", "url": "https://backup.test/data/"}

    two_remote_repo.set_config(remote_name="backup", remote_url=backup["url"])

    assert _saved_config(two_remote_repo)["remote"] == [ORIGIN, MIRROR, backup]


def test_set_config_without_a_remote_name_updates_origin(two_remote_repo):
    two_remote_repo.set_config(remote_url="https://moved.test/data/")

    assert _saved_config(two_remote_repo)["remote"] == [
        {"name": "origin", "url": "https://moved.test/data/"}, MIRROR]


### data entries ###

def test_fmt_entries_from_config_requires_a_mapping():
    with pytest.raises(ValueError, match="config must be a mapping"):
        fmt_entries_from_config([{"fmt": FMT}])


def test_set_config_creates_the_data_entry_when_config_has_none(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    del repo.state.config["data"]

    repo.set_config(encoding_updates={"a": r"m([0-9.]+)"})

    assert _saved_config(repo)["data"] == [{"encoding": {"a": r"m([0-9.]+)"}}]


def test_several_data_entries_leave_no_single_pattern_to_add_files_with(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"fmt": FMT}, {"fmt": "b{b}.h5"}]
    _write(repo.worktree, "a0_i0.h5")

    with pytest.raises(RuntimeError, match='exactly one entry under "data"'):
        branch_filename_format(repo)
    with pytest.raises(ValueError, match="No file pattern set"):
        repo.add_paths(["a0_i0.h5"])
    assert repo.state.data.empty


# One catalogued file of FMT.
ROWS = pd.DataFrame({"a": ["0"], "i": ["0"], "sha1": ["0" * 40]})


@pytest.mark.parametrize("data,rows,expected", [
    ([{}], False, None),
    ([{"fmt": FMT}, {"fmt": "b{b}.h5"}], False, None),
    ([{"fmt": f" {FMT} "}], False, FMT),
    ([{"fmt": FMT}], True, FMT),
], ids=["new-repository", "several-formats", "format-without-files", "files"])
def test_tracked_filename_format_is_none_only_without_files(data, rows, expected):
    state = State(config={"data": data})
    if rows:
        state.data = ROWS

    assert tracked_filename_format(state) == expected


@pytest.mark.parametrize("data,table,message", [
    ([{}], "data.tsv", r"data\[0\]\.fmt must be a non-empty string"),
    ([{"fmt": FMT}, {"fmt": "b{b}.h5"}], "data.tsv",
     'exactly one entry under "data"'),
    ([{"db": "a.tsv"}, {"db": "b.tsv"}], "b.tsv", 'exactly one entry under "data"'),
], ids=["no-format", "several-formats", "rows-in-another-tsv"])
def test_tracked_filename_format_requires_one_format_for_files(data, table, message):
    state = State(config={"data": data})
    if table == "data.tsv":
        state.data = ROWS
    else:
        state.tables = {table: ROWS}

    with pytest.raises(ValueError, match=message):
        tracked_filename_format(state)


def test_set_config_cannot_change_the_pattern_of_a_catalogue_without_one(
        tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo.worktree, "a0_i0.h5")
    repo.add(FMT)
    # As if config.yml were edited by hand and its pattern removed.
    config = repo.state.config
    del config["data"][0]["fmt"]
    repo.dothm.write_yaml(config, "config")
    edited = Repo(repo.worktree)

    with pytest.raises(ValueError, match="Cannot verify catalog without its saved"):
        edited.set_config(fmt="a{a}_i{i}.h5")
    assert "fmt" not in _saved_config(edited)["data"][0]


def test_set_config_encoding_updates_keep_the_pattern_and_merge(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(fmt=FMT)

    repo.set_config(encoding_updates={"a": r"m([0-9.]+)"})
    repo.set_config(encoding_updates={"i": r"x([0-9]+)"})

    assert _saved_config(repo)["data"] == [
        {"fmt": FMT, "encoding": {"a": r"m([0-9.]+)", "i": r"x([0-9]+)"}}]


def test_set_config_pattern_change_keeps_other_data_settings(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"fmt": "b{b}.h5", "db": "data.tsv"}]

    repo.set_config(fmt=FMT)

    # No encoding key is invented for an entry that had none.
    assert _saved_config(repo)["data"] == [{"fmt": FMT, "db": "data.tsv"}]


@pytest.mark.parametrize("value,message", [
    (42, "TSV database name must be a string"),
    (".tsv", "TSV database name cannot be empty"),
    (" .TSV ", "TSV database name cannot be empty"),
])
def test_validate_tsv_filename_rejects_names_without_a_stem(value, message):
    with pytest.raises(ValueError, match=message):
        validate_tsv_filename(value)


### filename matching ###

def test_repeated_field_matches_when_every_copy_holds_the_same_text():
    entries = [{"fmt": "{run}/{run}_{run}.h5"}]

    # The value holds the "_" delimiter, so the plain parse leaks into it.
    assert match_file("r1_x/r1_x_r1_x.h5", entries) == (0, {"run": "r1_x"})
    assert match_file("r1/r1_r2.h5", entries) == (None, None)


def test_repeated_field_behind_a_missing_literal_does_not_match():
    entries = [{"fmt": "obs_{date}/img_{date}.fits"}]

    assert match_file("obs_2020/img_2020.fits", entries) == (0, {"date": "2020"})
    assert match_file("img_2020.fits", entries) == (None, None)


### ParaFrame glob debugging ###

def test_glob_search_debug_reports_a_single_match(tmp_path, capsys):
    _write(tmp_path, "a1.h5")

    files, pattern = ParaFrame.glob_search(
        "a{a}.h5", base_path=tmp_path, debug=True, return_pattern=True)

    assert files == [str(tmp_path / "a1.h5")]
    out = capsys.readouterr().out
    assert f'Pattern: "{pattern}"' in out
    assert f'1 match, i.e., "{tmp_path / "a1.h5"}"' in out


def test_glob_search_debug_reports_no_match(tmp_path, capsys):
    files, _ = ParaFrame.glob_search(
        "a{a}.h5", base_path=tmp_path, debug=True, return_pattern=True)

    assert files == []
    assert "No match; please check format string" in capsys.readouterr().out


def test_add_pattern_in_a_folder_whose_name_has_braces(tmp_path):
    repo = Repo.init(tmp_path / "run_{id}")
    _write(repo.worktree, "a1_i0.h5")
    _write(tmp_path / "run_7", "a2_i0.h5")

    repo.add(FMT)

    assert Repo(repo.worktree).state.data["path"].tolist() == ["a1_i0.h5"]
