"""Test catalogs with several data entries, each stored in its own TSV."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from hallmark import Repo
from hallmark.dothm import Dothm
from hallmark.repo_config import (
    add_entry, catalog_entries, catalog_table_names, next_db_name, remove_entry,
    set_config, sole_data_spec)
from hallmark.repo_manifest import (
    catalog_map, iter_catalog_rows, manifest_map, row_fingerprint)
from hallmark.repo_state import load_head_state
from hallmark.state import State


def _template_config():
    return yaml.safe_load(Dothm.config_template())


def test_first_entry_fills_the_init_placeholder():
    config = _template_config()
    assert catalog_entries(config) == []
    assert add_entry(config, {"fmt": "a{a}.h5"}) == 0
    assert config["data"] == [{"fmt": "a{a}.h5", "encoding": None}]
    assert add_entry(config, {"fmt": "{run}.dat", "db": "data-2.tsv"}) == 1
    assert [entry.db for entry in catalog_entries(config)] == [
        "data.tsv", "data-2.tsv"]


def test_remote_entry_drops_the_empty_encoding_placeholder():
    config = _template_config()
    add_entry(config, {"fmt": "{src}.h5", "url": "https://example.test/er2/"})
    assert config["data"] == [
        {"fmt": "{src}.h5", "url": "https://example.test/er2/"}]
    assert catalog_entries(config)[0].is_remote


def test_catalog_entries_skip_placeholders_and_static_files():
    config = {"data": [{"encoding": None}, {"file": "README.md", "md5": "0" * 32},
                       {"fmt": "a{a}.h5", "db": "a"}, {"db": "paths"}]}
    entries = catalog_entries(config)
    assert [(entry.index, entry.fmt, entry.db) for entry in entries] == [
        (2, "a{a}.h5", "a.tsv"), (3, None, "paths.tsv")]
    assert catalog_table_names(config) == ["a.tsv", "paths.tsv"]


def test_catalog_table_names_skip_invalid_names():
    assert catalog_table_names({"data": [{"fmt": "x{a}", "db": "../x"}]}) == []


def test_next_db_name_never_reuses_a_number():
    config = {"data": [{"fmt": "a{a}.h5"}]}
    assert next_db_name({"data": []}) == "data.tsv"
    assert next_db_name(config) == "data-2.tsv"
    assert next_db_name(config, used=["data-2.tsv", "DATA-7.TSV"]) == "data-8.tsv"
    add_entry(config, {"fmt": "{b}.dat", "db": "data-3.tsv"})
    remove_entry(config, 0)
    # data.tsv always exists, so a later first entry takes it back
    assert next_db_name(config) == "data.tsv"


def test_single_template_settings_refuse_several_entries(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"fmt": "a{a}.h5"},
                                 {"fmt": "{b}.dat", "db": "data-2.tsv"}]
    with pytest.raises(RuntimeError, match="several templates"):
        set_config(repo, fmt="c{c}.h5")
    with pytest.raises(RuntimeError, match="several templates"):
        sole_data_spec(repo.state.config)


def test_set_config_refuses_to_change_a_remote_template(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"fmt": "{a}.h5", "url": "https://example.test/"}]
    with pytest.raises(ValueError, match="remote entry"):
        set_config(repo, fmt="{b}.h5")


def test_set_config_updates_the_sole_entry_in_place(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"file": "README.md"}, {"fmt": "a{a}.h5"}]
    set_config(repo, fmt="b{b}.h5", encoding_updates={"b": "x(.*)"})
    assert repo.state.config["data"] == [
        {"file": "README.md"}, {"fmt": "b{b}.h5", "encoding": {"b": "x(.*)"}}]


def test_tables_round_trip_through_dump_load_and_history(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [
        {"fmt": "a{a}.h5", "encoding": None},
        {"fmt": "{src}.h5", "db": "data-2.tsv", "url": "https://example.test/"}]
    repo.state.replace(pd.DataFrame({"sha1": ["1" * 40], "a": ["0"]}))
    remote = pd.DataFrame({"sha1": [None], "checksum_algorithm": ["md5"],
                           "checksum": ["f" * 32], "size_bytes": ["7"],
                           "mtime": [""], "src": ["M87"]})
    repo.state.replace(remote, db="data-2.tsv")
    repo.dothm.dump(repo.state)
    repo.dothm.index.commit("Two templates")

    reopened = Repo(tmp_path / "repo")
    assert reopened.state.data["a"].tolist() == ["0"]
    table = reopened.state.table("data-2.tsv")
    assert table.to_dict("records") == [{
        "sha1": "", "checksum_algorithm": "md5", "checksum": "f" * 32,
        "size_bytes": "7", "mtime": "", "src": "M87"}]
    head = load_head_state(reopened)
    assert head.table("data-2.tsv")["src"].tolist() == ["M87"]


def test_a_configured_table_that_was_never_written_loads_empty(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"db": "later.tsv"}]
    repo.dothm.dump_yml(repo.state.config, "config")
    assert Repo(tmp_path / "repo").state.table("later.tsv").empty


def test_dropped_tables_are_deleted_and_unstaged(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"fmt": "a{a}.h5"},
                                 {"fmt": "{b}.dat", "db": "data-2.tsv"}]
    repo.state.replace(pd.DataFrame({"sha1": ["2" * 40], "b": ["1"]}),
                       db="data-2.tsv")
    repo.dothm.dump(repo.state)
    assert (repo.dothm.path / "data-2.tsv").is_file()
    remove_entry(repo.state.config, 1)
    repo.state.drop_table("data-2.tsv")
    repo.dothm.dump(repo.state)
    assert not (repo.dothm.path / "data-2.tsv").exists()
    assert ("data-2.tsv", 0) not in repo.dothm.index.entries


def test_update_ignores_metadata_columns_when_matching_rows():
    state = State()
    old = pd.DataFrame({"sha1": [""], "checksum": ["a" * 32], "size_bytes": ["1"],
                        "name": ["x"]})
    new = old.assign(checksum="b" * 32, size_bytes="2")
    state.update(old)
    state.update(new)
    assert state.data.to_dict("records") == [
        {"sha1": "", "checksum": "b" * 32, "size_bytes": "2", "name": "x"}]


def test_rows_of_remote_path_and_shared_tables_are_catalog_only():
    state = State(config={"data": [
        {"fmt": "a{a}.h5"},
        {"fmt": "{src}.h5", "db": "remote.tsv", "url": "https://example.test/"},
        {"fmt": "x{x}.fits", "db": "built.tsv"},
        {"fmt": "y{y}.fits", "db": "built.tsv"}]},
        data=pd.DataFrame({"sha1": ["1" * 40], "a": ["0"]}))
    state.tables["remote.tsv"] = pd.DataFrame({"sha1": [""], "src": ["M87"]})
    state.tables["built.tsv"] = pd.DataFrame({
        "path": ["x1.fits", "y2.fits"], "checksum": ["None", "c" * 32],
        "x": ["1", "None"], "y": ["None", "2"]})
    rows = {row.path: row.local for row in iter_catalog_rows(state)}
    assert rows == {"a0.h5": True, "M87.h5": False, "x1.fits": False,
                    "y2.fits": False}
    assert manifest_map(state) == {"a0.h5": "1" * 40}
    assert set(catalog_map(state)) == set(rows)


def test_row_fingerprint_prefers_sha1_then_checksum_then_size():
    assert row_fingerprint({"sha1": "ABC", "checksum": "d"}) == "abc"
    assert row_fingerprint({"sha1": "", "checksum_algorithm": "MD5",
                            "checksum": "D"}) == "md5:d"
    assert row_fingerprint({"checksum": "None", "size_bytes": "3",
                            "mtime": None}) == "size:3;mtime:"


def test_single_template_repository_layout_is_unchanged(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    (repo.worktree / "a1.h5").write_text("one\n", encoding="utf-8")
    repo.add("a{a}.h5")
    assert sorted(path.name for path in repo.dothm.path.glob("*.tsv")) == [
        "data.tsv"]
    assert Path(repo.dothm.path / "config.yml").read_text(encoding="utf-8") == (
        "data:\n- fmt: a{a}.h5\n  encoding: null\nremote: null\n")
