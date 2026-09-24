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


REMOTE = "https://example.test/er2/"


def _write(repo, name, text):
    path = repo.worktree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return repo.checksum(path)


def _commit_local(repo, files, message="local data"):
    """Track files as the branch's only template and commit their contents."""
    repo.state.config["data"] = [{"fmt": "{name}.h5", "encoding": None}]
    repo.state.replace(pd.DataFrame({
        "sha1": [_write(repo, f"{name}.h5", text) for name, text in files.items()],
        "name": list(files)}))
    repo.dothm.dump(repo.state)
    repo.commit(message)


def _catalog_remote(repo, rows, *, keep_local=False):
    """Catalog remote files by name, with their published SHA-1 or ''."""
    entries = [{"fmt": "{name}.h5", "db": "data-2.tsv", "url": REMOTE}]
    if keep_local:
        entries.insert(0, {"fmt": "{run}.dat", "encoding": None})
    else:
        repo.state.drop_table("data.tsv")
    repo.state.config["data"] = entries
    repo.state.replace(pd.DataFrame({
        "sha1": list(rows.values()), "checksum_algorithm": ["md5"] * len(rows),
        "checksum": [str(index) * 32 for index, _ in enumerate(rows)],
        "size_bytes": ["4"] * len(rows), "mtime": [""] * len(rows),
        "name": list(rows)}), db="data-2.tsv")
    repo.dothm.dump(repo.state)


def test_commit_stores_objects_only_for_local_entries(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    run_sha = _write(repo, "1.dat", "run\n")
    _catalog_remote(repo, {"M87": ""}, keep_local=True)
    repo.state.replace(pd.DataFrame({"sha1": [run_sha], "run": ["1"]}))
    repo.dothm.dump(repo.state)
    assert repo.commit("mixed templates")
    assert repo.objects.contains(run_sha)
    assert len([path for path in repo.objects.root.rglob("*") if path.is_file()]) == 1
    assert repo.status()["staged"]["catalog"] == []


def test_status_summarizes_remote_changes_and_ignores_downloads(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _catalog_remote(repo, {"M87": "", "SGRA": "", "OJ287": ""})
    repo.commit("remote catalog")
    _write(repo, "M87.h5", "downloaded\n")
    snapshot = repo.status()
    assert snapshot["untracked"] == []
    assert snapshot["worktree"] == {"modified": [], "deleted": []}

    table = repo.state.table("data-2.tsv")
    table = table[table["name"] != "OJ287"].copy()
    table.loc[table["name"] == "SGRA", "checksum"] = "e" * 32
    table.loc[len(table)] = ["", "md5", "f" * 32, "4", "", "3C279"]
    repo.state.set_table("data-2.tsv", table)
    repo.dothm.dump(repo.state)
    assert repo.status()["staged"]["catalog"] == [{
        "templates": ["{name}.h5"], "url": REMOTE,
        "added": 1, "modified": 1, "deleted": 1}]


def test_cli_status_prints_remote_catalog_summaries(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from hallmark.cli import hallmark

    repo = Repo.init(tmp_path / "repo")
    _catalog_remote(repo, {"M87": "", "SGRA": ""})
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["status"])
    assert result.exit_code == 0, result.output
    assert f"catalog:   {{name}}.h5 from {REMOTE}: 2 new, 0 modified, 0 removed" \
        in result.output


def test_checkout_removes_local_files_a_remote_branch_catalogs_differently(
        tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _commit_local(repo, {"M87": "local\n"})
    repo.checkout("remote")
    _catalog_remote(repo, {"M87": ""})
    repo.commit("catalog the archive copy")
    repo.checkout("main")
    repo.checkout("remote")
    assert not (repo.worktree / "M87.h5").exists()
    repo.checkout("main")
    assert (repo.worktree / "M87.h5").read_text(encoding="utf-8") == "local\n"


def test_checkout_keeps_local_files_a_remote_branch_catalogs_identically(
        tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _commit_local(repo, {"M87": "same\n"})
    sha1 = repo.checksum(repo.worktree / "M87.h5")
    repo.checkout("remote")
    _catalog_remote(repo, {"M87": sha1})
    repo.commit("catalog the archive copy")
    repo.checkout("main")
    repo.checkout("remote")
    assert (repo.worktree / "M87.h5").read_text(encoding="utf-8") == "same\n"


def test_checkout_rejects_a_different_downloaded_copy_of_a_local_file(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _commit_local(repo, {"M87": "local\n"})
    repo.checkout("remote")
    _catalog_remote(repo, {"M87": ""})
    repo.commit("catalog the archive copy")
    _write(repo, "M87.h5", "downloaded\n")
    with pytest.raises(Exception, match="downloaded copy"):
        repo.checkout("main")
    assert repo.dothm.active_branch.name == "remote"
    _write(repo, "M87.h5", "local\n")
    assert repo.checkout("main")


def test_checkout_between_remote_branches_leaves_downloads_alone(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _catalog_remote(repo, {"M87": ""})
    repo.commit("first archive")
    _write(repo, "M87.h5", "downloaded\n")
    repo.checkout("other")
    _catalog_remote(repo, {"SGRA": ""})
    repo.commit("second archive")
    repo.checkout("main")
    assert (repo.worktree / "M87.h5").read_text(encoding="utf-8") == "downloaded\n"
    assert repo.status()["untracked"] == []


def test_add_worktree_restores_only_local_files(tmp_path):
    repo = Repo.init(tmp_path / "main")
    run_sha = _write(repo, "1.dat", "run\n")
    _catalog_remote(repo, {"M87": ""}, keep_local=True)
    repo.state.replace(pd.DataFrame({"sha1": [run_sha], "run": ["1"]}))
    repo.dothm.dump(repo.state)
    repo.commit("mixed templates")
    assert repo.add_worktree("experiment")
    experiment = tmp_path / "experiment"
    assert (experiment / "1.dat").read_text(encoding="utf-8") == "run\n"
    assert not (experiment / "M87.h5").exists()
    assert Repo(experiment).state.table("data-2.tsv")["name"].tolist() == ["M87"]


def test_templates_accumulate_and_round_trip_through_checkout(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "M87_095.h5", "h5\n")
    _write(repo, "runs/run_1.dat", "dat\n")
    repo.add("{src}_{day}.h5")
    repo.add("runs/run_{run}.dat")
    assert [entry.db for entry in catalog_entries(repo.state.config)] == [
        "data.tsv", "data-2.tsv"]
    repo.commit("two templates")
    repo.checkout("empty")
    repo.rm_cached("{src}_{day}.h5")
    repo.rm_cached("runs/run_{run}.dat")
    repo.commit("nothing tracked")
    repo.checkout("main")
    assert (repo.worktree / "M87_095.h5").read_text(encoding="utf-8") == "h5\n"
    assert (repo.worktree / "runs/run_1.dat").read_text(encoding="utf-8") == "dat\n"
    assert repo.status()["untracked"] == []


def test_new_template_must_match_files_and_leaves_config_unchanged(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    before = (repo.dothm.path / "config.yml").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="did not match any files"):
        repo.add("{src}.fits")
    assert (repo.dothm.path / "config.yml").read_text(encoding="utf-8") == before


def test_new_template_cannot_claim_files_of_another_template(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "M87_095.h5", "h5\n")
    repo.add("{src}_{day}.h5")
    with pytest.raises(ValueError, match=r"tracked by another template \(M87_095.h5\)"):
        repo.add("{name}.h5")
    assert len(catalog_entries(repo.state.config)) == 1


def test_reserved_fields_are_rejected(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "a.h5", "a\n")
    for fmt in ("{path}.h5", "{sha256}.h5", "{size_bytes}.h5"):
        with pytest.raises(ValueError, match="reserved catalog metadata"):
            repo.add(fmt)


def test_local_add_refuses_a_template_tracked_remotely(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _catalog_remote(repo, {"M87": ""})
    _write(repo, "M87.h5", "downloaded\n")
    with pytest.raises(ValueError, match="rm --cached"):
        repo.add("{name}.h5")


def test_dry_run_lists_matches_without_hashing_or_staging(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "M87_095.h5", "h5\n")
    staged = {key: entry.binsha for key, entry in repo.dothm.index.entries.items()}
    monkeypatch.setattr(Repo, "checksum_many",
                        lambda paths: pytest.fail("dry run must not hash"))
    result = repo.add("{src}_{day}.h5", dry_run=True)
    assert result["path"].tolist() == ["M87_095.h5"]
    assert catalog_entries(repo.state.config) == []
    assert {key: entry.binsha
            for key, entry in repo.dothm.index.entries.items()} == staged


def test_dot_rescans_every_local_template_and_skips_downloads(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "M87_095.h5", "h5\n")
    _write(repo, "runs/run_1.dat", "dat\n")
    repo.add("{src}_{day}.h5")
    repo.add("runs/run_{run}.dat")
    _write(repo, "SGRA_096.h5", "new\n")
    (repo.worktree / "runs/run_1.dat").unlink()
    result = repo.add(".")
    assert sorted(result["path"]) == ["M87_095.h5", "SGRA_096.h5"]
    assert repo.state.data["src"].tolist() == ["M87", "SGRA"]
    assert repo.state.table("data-2.tsv").empty


def test_dot_from_a_subdirectory_keeps_rows_outside_it(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "a/1.dat", "a1\n")
    _write(repo, "b/1.dat", "b1\n")
    repo.add("{group}/{run}.dat")
    (repo.worktree / "a/1.dat").unlink()
    _write(repo, "a/2.dat", "a2\n")
    monkeypatch.chdir(repo.worktree / "a")
    assert repo.add(".")["path"].tolist() == ["a/2.dat"]
    rows = repo.state.data.sort_values("group")[["group", "run"]]
    assert rows.values.tolist() == [["a", "2"], ["b", "1"]]


def test_dot_rejects_a_file_matching_two_local_templates(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "x.h5", "x\n")
    _write(repo, "y_1.dat", "y\n")
    repo.add("{name}.h5")
    repo.add("{name}_{run}.dat")
    repo.state.config["data"][1]["fmt"] = "{name}_{run}.{ext}"
    _write(repo, "z_2.h5", "z\n")
    with pytest.raises(ValueError, match="matches templates"):
        repo.add(".")


def test_rm_cached_untracks_a_template_and_keeps_its_files(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "M87_095.h5", "h5\n")
    _write(repo, "runs/run_1.dat", "dat\n")
    repo.add("{src}_{day}.h5")
    repo.add("runs/run_{run}.dat")
    repo.commit("two templates")
    removed = repo.rm_cached("runs/run_{run}.dat")
    assert removed.db == "data-2.tsv"
    assert not (repo.dothm.path / "data-2.tsv").exists()
    assert (repo.worktree / "runs/run_1.dat").exists()
    assert repo.status()["staged"]["deleted"] == ["runs/run_1.dat"]
    repo.commit("drop runs")
    repo.add("runs/run_{run}.dat")
    # the removed table's history is never reused by a new template
    assert find_db(repo, "runs/run_{run}.dat") == "data-3.tsv"


def find_db(repo, fmt):
    return next(entry.db for entry in catalog_entries(repo.state.config)
                if entry.fmt == fmt)


def test_rm_cached_accepts_a_remote_url_template_and_reports_unknown_ones(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _catalog_remote(repo, {"M87": ""})
    with pytest.raises(ValueError,
                       match=r"not tracked; tracked templates: '\{name\}.h5'"):
        repo.rm_cached("{src}.h5")
    repo.rm_cached(REMOTE + "{name}.h5")
    assert catalog_entries(repo.state.config) == []


def test_rm_cached_refuses_tables_shared_by_builder_templates(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.state.config["data"] = [{"fmt": "x{x}.fits", "db": "built.tsv"},
                                 {"fmt": "y{y}.fits", "db": "built.tsv"}]
    with pytest.raises(ValueError, match="shares its catalog table"):
        repo.rm_cached("x{x}.fits")


def test_cli_add_dry_run_and_rm_cached(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from hallmark.cli import hallmark

    repo = Repo.init(tmp_path / "repo")
    _write(repo, "M87_095.h5", "h5\n")
    monkeypatch.chdir(repo.worktree)
    runner = CliRunner()
    preview = runner.invoke(hallmark, ["add", "-n", "{src}_{day}.h5"])
    assert preview.exit_code == 0, preview.output
    assert preview.output.splitlines() == ["Would add", "M87_095.h5"]
    assert runner.invoke(hallmark, ["add", "{src}_{day}.h5"]).exit_code == 0
    refused = runner.invoke(hallmark, ["rm", "{src}_{day}.h5"])
    assert refused.exit_code != 0
    assert "requires --cached" in refused.output
    removed = runner.invoke(hallmark, ["rm", "--cached", "{src}_{day}.h5"])
    assert removed.exit_code == 0, removed.output
    assert "files were left in place" in removed.output
    missing = runner.invoke(hallmark, ["add", "{src}.fits"])
    assert missing.exit_code != 0
    assert "did not match any files" in missing.output


MIRROR = "https://mirror.test/export/"
SIMS = "https://sims.test/runs/"


def _two_archives(repo, *, local=True):
    """Catalog a local run, archive images, and simulation files."""
    entries = [{"fmt": "{name}.h5", "db": "data-2.tsv", "url": REMOTE},
               {"fmt": "{run}.dat", "db": "data-3.tsv", "url": SIMS}]
    if local:
        entries.insert(0, {"fmt": "notes_{n}.txt", "encoding": None})
        repo.state.replace(pd.DataFrame({
            "sha1": [_write(repo, "notes_1.txt", "note\n")], "n": ["1"]}))
    repo.state.config["data"] = entries
    remote_columns = {"sha1": "", "checksum_algorithm": "", "checksum": "",
                      "mtime": ""}
    repo.state.replace(pd.DataFrame([
        {**remote_columns, "size_bytes": "5", "name": "M87"},
        {**remote_columns, "size_bytes": "6", "name": "SGRA"}]), db="data-2.tsv")
    repo.state.replace(pd.DataFrame([
        {**remote_columns, "size_bytes": "7", "run": "001"}]), db="data-3.tsv")
    repo.dothm.dump(repo.state)


def test_plan_downloads_each_template_from_its_own_url(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _two_archives(repo, local=False)
    plan = repo.plan_download(all_files=True)
    assert {item.relative_path.as_posix(): item.source.url
            for item in plan.items} == {
        "M87.h5": REMOTE, "SGRA.h5": REMOTE, "001.dat": SIMS}
    assert [source.url for source in plan.sources] == [REMOTE, SIMS]
    assert plan.remote_url is None
    assert plan.total_bytes == 18
    summary = plan.summary()
    assert f"Sources:\n  {REMOTE} (2 file(s); transport http)" in summary
    assert f"\n  {SIMS} (1 file(s); transport http)" in summary


def test_plan_with_one_source_keeps_the_plan_level_fields(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _catalog_remote(repo, {"M87": ""})
    plan = repo.plan_download()
    assert plan.remote_url == REMOTE
    assert plan.remote_backend == "http"
    assert f"Source: {REMOTE}\nTransport: http" in plan.summary()


def test_local_templates_use_the_default_remote_and_skip_without_one(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _two_archives(repo)
    plan = repo.plan_download(all_files=True)
    assert plan.file_count == 3
    assert plan.unsourced_count == 1
    assert "Skipped 1 cataloged file(s) without a data source" in plan.summary()
    with pytest.raises(Exception, match="No data source is configured for: notes"):
        repo.plan_download(file_paths=["notes_1.txt", "M87.h5"])

    repo.set_config(remote_url=MIRROR)
    plan = repo.plan_download(all_files=True)
    assert {item.relative_path.as_posix(): item.source.url
            for item in plan.items}["notes_1.txt"] == MIRROR
    assert plan.unsourced_count == 0


def test_named_remote_overrides_every_source(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _two_archives(repo)
    repo.set_config(remote_name="mirror", remote_url=MIRROR)
    plan = repo.plan_download(all_files=True, remote_name="mirror")
    assert {item.source.url for item in plan.items} == {MIRROR}
    assert plan.remote_name == "mirror"


def test_nothing_downloadable_reports_the_missing_remote(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    _write(repo, "a1.h5", "one\n")
    repo.add("a{a}.h5")
    with pytest.raises(Exception, match="No remote URL is configured"):
        repo.plan_download(all_files=True)


def test_execute_downloads_each_source_in_turn(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "repo")
    _two_archives(repo, local=False)
    fetched = []

    def fake_fetch(context, relative_path, destination, checksum, chunk_size):
        fetched.append(context.remote.file_url(relative_path.as_posix()))
        return 1

    monkeypatch.setattr("hallmark.downloader._fetch_file", fake_fetch)
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    assert result == {"succeeded": 3, "failed": 0, "total_bytes": 3, "errors": []}
    assert sorted(fetched) == [
        REMOTE + "M87.h5", REMOTE + "SGRA.h5", SIMS + "001.dat"]


def test_cli_download_dry_run_lists_every_source(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from hallmark.cli import hallmark

    repo = Repo.init(tmp_path / "repo")
    _two_archives(repo, local=False)
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["download", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "3 file(s); 18 bytes" in result.output
    assert "Sources:" in result.output and SIMS in result.output
