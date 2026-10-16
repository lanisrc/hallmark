from pathlib import Path

import pytest
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.error import CheckoutError, DestinationExistsError
from hallmark.remote.add import is_remote_catalog
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing


@pytest.fixture
def remote_listing(monkeypatch):
    pages = {"": '<h1>Index of data</h1><a href="M87_001.fits">M87_001.fits</a>'
                   '<a href="M87_002.fits">M87_002.fits</a>'
                   '<a href="notes.txt">notes.txt</a>'}
    reads = []

    def read_text(context, path):
        reads.append(path)
        if path not in pages:
            raise RemoteObjectMissing("No metadata")
        return pages[path]

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    return pages, reads


def test_remote_add_stages_without_payload_or_commit(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    head = repo.dothm.head.commit.hexsha
    repo.state.meta["project"] = "keep"
    repo.dothm.save_state(repo.state)
    frame = repo.add("https://example.test/data/{src}_{day:03d}.fits")
    assert frame.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert repo.dothm.head.commit.hexsha == head
    assert repo.state.meta == {"project": "keep"}
    assert repo.state.data["src"].tolist() == ["M87", "M87"]
    assert repo.status()["staged"]["added"] == frame.path.tolist()
    assert repo.status()["worktree"]["deleted"] == []
    assert not (repo.worktree / "M87_001.fits").exists()
    assert not repo.objects.root.exists()
    assert not any(path.endswith(".fits") for path in remote_listing[1])
    assert repo.commit("Add remote EHT data")
    assert repo.dothm.head.commit.message.strip() == "Add remote EHT data"
    assert not repo.commit("No changes")
    assert repo.plan_download().file_count == 3


def test_remote_add_merges_paths_and_leaves_unmatched_rows(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    root = "https://example.test/data/"
    repo.add(root, filter="*001.fits")
    repo.commit("First selection")
    repo.add(root, filter="*002.fits")
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert repo.status()["staged"]["added"] == ["M87_002.fits"]
    repo.commit("Second selection")
    repo.add(root, filter="*002.fits")
    assert repo.dothm.index.diff("HEAD") == []
    repo.add(root, filter="absent*")
    assert repo.dothm.index.diff("HEAD") == []


def test_remote_catalog_clone_and_branches_keep_payloads_separate(
        tmp_path, remote_listing):
    source = Repo.init(tmp_path / "source")
    source.add("https://example.test/data/", filter="*.fits")
    source.commit("Remote catalog")
    repo = Repo.clone(str(source.dothm.path), tmp_path / "copy", download=False)
    repo.set_identity("Hallmark Tests", "tests@example.invalid")
    assert repo.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert repo.status()["worktree"]["deleted"] == []
    original = repo.dothm.active_branch.name
    repo.create_branch("notes")
    repo.checkout("notes")
    repo.add("https://example.test/data/", filter="*.txt")
    with pytest.raises(CheckoutError, match="Commit"):
        repo.checkout(original)
    repo.commit("Add notes")
    (repo.worktree / "M87_001.fits").write_text("Keep local content")
    repo.checkout(original)
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert (repo.worktree / "M87_001.fits").read_text() == "Keep local content"
    assert not repo.objects.root.exists()


def test_failed_remote_add_preserves_staged_state(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/", filter="*.fits")
    before = {name: (repo.dothm.path / name).read_bytes()
              for name in ("config.yml", "meta.yml", "data.tsv", ".git/index")}
    remote_listing[0].clear()
    with pytest.raises(RuntimeError):
        repo.add("https://example.test/data/")
    assert before == {name: (repo.dothm.path / name).read_bytes() for name in before}


def test_remote_add_rejects_different_root_and_local_data_before_network(
        tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "remote")
    repo.add("https://example.test/data/")
    remote_listing[1].clear()
    with pytest.raises(ValueError, match="one dataset root"):
        repo.add("https://different.test/data/")
    assert remote_listing[1] == []
    local = Repo.init(tmp_path / "local")
    (local.worktree / "run1.txt").write_text("one")
    local.add("run{run:d}.txt")
    with pytest.raises(ValueError, match="separate repository"):
        local.add("https://example.test/data/")
    assert remote_listing[1] == []


def test_init_is_local_and_never_resets_existing_state(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("init must not discover data")

    monkeypatch.setattr("hallmark.remote.add.discover_remote_files", fail)
    repo = Repo.init(tmp_path / "data")
    assert repo.state.data.empty
    assert not repo.objects.root.exists()
    before = (repo.dothm.path / "config.yml").read_bytes()
    with pytest.raises(DestinationExistsError):
        Repo.init(repo.worktree)
    assert (repo.dothm.path / "config.yml").read_bytes() == before
    incomplete = tmp_path / "incomplete" / ".hm"
    incomplete.mkdir(parents=True)
    with pytest.raises(DestinationExistsError):
        Repo.init(incomplete.parent)


def test_cli_init_add_commit_status(tmp_path, monkeypatch, remote_listing):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(hallmark, ["init"]).exit_code == 0
    result = runner.invoke(hallmark, ["add", "https://example.test/data/{src}_{day}.fits"])
    assert result.exit_code == 0, result.output
    assert "M87_001.fits" in result.output
    result = runner.invoke(hallmark, ["commit", "-m", "Remote data"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(hallmark, ["status"])
    assert result.exit_code == 0, result.output
    assert "remote catalog unchanged" in result.output
    assert "Changes not staged" not in result.output
    assert "Changes to be committed" not in result.output
    assert not Path("M87_001.fits").exists()


def test_remote_parameter_cannot_turn_catalog_into_local_objects(
        tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{sha1}_{day}.fits")
    assert "sha1" not in repo.state.data.columns
    assert repo.commit("Record remote files")
    assert not repo.objects.root.exists()


def test_remote_add_preserves_other_catalog_tables(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.state.config["data"] = [{"fmt": "{src}_{day:03d}.fits", "db": "other.tsv"}]
    repo.dothm.save_state(repo.state)
    repo.add("https://example.test/data/")
    assert repo.state.config["data"] == [
        {"file": "notes.txt"}, {"db": "other.tsv", "fmt": "{src}_{day:03d}.fits"}]
    assert repo.dothm.read_tsv("other.tsv")["path"].tolist() == [
        "M87_001.fits", "M87_002.fits"]
    assert repo.state.data.empty
    assert is_remote_catalog(repo.state)


def test_remote_add_sorts_fmts_into_their_own_tsvs(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    frame = repo.add("https://example.test/data/", remote_fmt=[
        {"fmt": "{src}_{day:03d}.fits", "db": "data.tsv"}, {"fmt": "notes.{ext}"}])
    assert repo.state.config["data"] == [
        {"db": "data.tsv", "fmt": "{src}_{day:03d}.fits"},
        {"db": "notes.{ext}.tsv", "fmt": "notes.{ext}"}]
    assert repo.state.data["path"].tolist() == ["M87_001.fits", "M87_002.fits"]
    assert repo.dothm.read_tsv("notes.{ext}.tsv")["ext"].tolist() == ["txt"]
    assert sorted(frame["path"]) == ["M87_001.fits", "M87_002.fits", "notes.txt"]


def test_remote_add_keeps_earlier_rows_in_each_tsv(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.state.config["data"] = [{"fmt": "{src}_{day:03d}.fits", "db": "fits.tsv"}]
    repo.dothm.save_state(repo.state)
    root = "https://example.test/data/"
    repo.add(root, filter="*001.fits")
    repo.add(root, filter="*002.fits")
    assert repo.dothm.read_tsv("fits.tsv")["path"].tolist() == [
        "M87_001.fits", "M87_002.fits"]


def test_cli_add_repeated_fmt_flags(tmp_path, monkeypatch, remote_listing):
    repo = Repo.init(tmp_path / "data")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, [
        "add", "https://example.test/data/",
        "--fmt", "{src}_{day:03d}.fits=fits", "--fmt", "notes.{ext}=notes"])
    assert result.exit_code == 0, result.output
    repo = Repo(repo.worktree)
    assert repo.state.config["data"] == [
        {"db": "fits.tsv", "fmt": "{src}_{day:03d}.fits"},
        {"db": "notes.tsv", "fmt": "notes.{ext}"}]
    assert repo.dothm.read_tsv("notes.tsv")["path"].tolist() == ["notes.txt"]


def test_remote_add_turns_unmatched_files_into_file_entries(tmp_path, remote_listing):
    remote_listing[0][""] = ('<h1>Index of data</h1>'
                             '<a href="M87_001.fits">M87_001.fits</a>'
                             '<a href="README.md">README.md</a>'
                             '<a href="meta.yml">meta.yml</a>'
                             '<a href="notes.txt">notes.txt</a>')
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{src}_{day:03d}.fits")
    assert repo.state.config["data"] == [
        {"name": "readme", "file": "README.md"}, {"file": "notes.txt"},
        {"db": "data.tsv", "fmt": "{src}_{day:03d}.fits"}]
    assert repo.state.config["meta"] == [{"file": "meta.yml"}]
    assert repo.state.data["path"].tolist() == ["M87_001.fits"]
    assert repo.plan_download(all_files=True).file_count == 4


def test_remote_add_keeps_earlier_file_entries(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    root = "https://example.test/data/"
    repo.add(root + "{src}_{day:03d}.fits", filter="*.txt")
    repo.add(root, filter="*.fits")
    assert repo.state.config["data"] == [
        {"file": "notes.txt"}, {"db": "data.tsv", "fmt": "{src}_{day:03d}.fits"}]
    assert repo.state.data["path"].tolist() == ["M87_001.fits", "M87_002.fits"]


def test_remote_add_records_listed_checksum_on_file_entries(tmp_path, remote_listing):
    digest = "0123456789abcdef0123456789abcdef"
    remote_listing[0][""] = ('<h1>Index of data</h1>'
                             '<a href="M87_001.fits">M87_001.fits</a>'
                             '<a href="notes.txt">notes.txt</a>'
                             '<a href="md5sums">md5sums</a>')
    remote_listing[0]["md5sums"] = f"{digest}  notes.txt\n"
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{src}_{day:03d}.fits")
    assert {"file": "notes.txt", "md5": digest} in repo.state.config["data"]


def test_new_fmt_renames_data_tsv_and_sorts_unsorted_files(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{src}_{day:03d}.fits")
    repo.add("https://example.test/data/", remote_fmt=[{"fmt": "notes.{ext}"}],
             resort=False)
    assert repo.state.config["data"] == [
        {"db": "{src}_{day:03d}.fits.tsv", "fmt": "{src}_{day:03d}.fits"},
        {"db": "notes.{ext}.tsv", "fmt": "notes.{ext}"}]
    assert repo.dothm.read_tsv("{src}_{day:03d}.fits.tsv")["path"].tolist() == [
        "M87_001.fits", "M87_002.fits"]
    assert repo.dothm.read_tsv("notes.{ext}.tsv")["path"].tolist() == ["notes.txt"]
    assert repo.state.data.empty
    assert is_remote_catalog(repo.state)


def test_new_fmt_resort_moves_sorted_files_to_a_better_match(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{src}_{day:03d}.fits")
    asked = []
    repo.add("https://example.test/data/", remote_fmt=[{"fmt": "M87_{day:03d}.fits"}],
             resort=lambda count: asked.append(count) or True)
    assert asked == [2]
    assert repo.dothm.read_tsv("{src}_{day:03d}.fits.tsv").empty
    assert repo.dothm.read_tsv("M87_{day:03d}.fits.tsv")["path"].tolist() == [
        "M87_001.fits", "M87_002.fits"]


def test_new_fmt_unsorted_keeps_sorted_files_in_place(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{src}_{day:03d}.fits")
    repo.add("https://example.test/data/", remote_fmt=[{"fmt": "M87_{day:03d}.fits"}],
             resort=False)
    assert repo.dothm.read_tsv("{src}_{day:03d}.fits.tsv")["path"].tolist() == [
        "M87_001.fits", "M87_002.fits"]
    assert repo.dothm.read_tsv("M87_{day:03d}.fits.tsv").empty


def test_new_fmt_without_resort_answer_asks_for_one(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{src}_{day:03d}.fits")
    with pytest.raises(ValueError, match="resort=True"):
        repo.add("https://example.test/data/", remote_fmt=[{"fmt": "notes.{ext}"}])


def test_cli_asks_whether_to_resort_for_a_new_fmt(tmp_path, monkeypatch,
                                                  remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{src}_{day:03d}.fits")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, [
        "add", "https://example.test/data/", "--fmt", "notes.{ext}"],
        input="unsorted\n")
    assert result.exit_code == 0, result.output
    assert "2 files are already sorted" in result.output
    repo = Repo(repo.worktree)
    assert repo.dothm.read_tsv("notes.{ext}.tsv")["path"].tolist() == ["notes.txt"]


@pytest.mark.parametrize("options", ["[]\n", "scalar\n", "options: [\n"])
def test_add_cli_rejects_invalid_backend_options_before_access(
        monkeypatch, tmp_path, options):
    repo = Repo.init(tmp_path / "repo")
    config = tmp_path / "options.yml"
    config.write_text(options)
    monkeypatch.chdir(repo.worktree)

    def fail(*args, **kwargs):
        raise AssertionError("Invalid settings must fail before discovery")

    monkeypatch.setattr("hallmark.remote.add.discover_remote_files", fail)
    result = CliRunner().invoke(hallmark, [
        "add", "https://example.test/data/", "--backend-options", str(config)])
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert Repo(repo.worktree).state.data.empty


def test_add_cli_forwards_backend_and_selection(monkeypatch, tmp_path):
    import pandas as pd

    repo = Repo.init(tmp_path / "repo")
    options = tmp_path / "backend.yml"
    options.write_text("collection: latest\n")
    monkeypatch.chdir(repo.worktree)
    captured = {}

    def add(self, value, encoding, **kwargs):
        captured.update(value=value, encoding=encoding, **kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(Repo, "add", add)
    result = CliRunner().invoke(hallmark, [
        "add", "ssh://lab-data/export/", "--auth", "lab", "--backend", "ssh",
        "--backend-options", str(options), "--filter", "**/*.fits",
        "--filter", "README*", "--fmt", "{name}.fits"])
    assert result.exit_code == 0, result.output
    assert callable(captured.pop("resort"))
    assert captured == {
        "value": "ssh://lab-data/export/", "encoding": False,
        "auth": "lab", "backend": "ssh", "backend_options": {"collection": "latest"},
        "filter": ("**/*.fits", "README*"), "remote_fmt": [{"fmt": "{name}.fits"}],
        "progress": True}


@pytest.mark.parametrize("arguments", [
    ["init", "--from", "https://example.test/data/"],
    ["build"], ["clone", "source", "target", "--no-fetch-data"],
    ["clone", "source", "target", "--download"],
    ["download", "--all", "--yes"],
])
def test_removed_cli_interfaces_fail_clearly(monkeypatch, tmp_path, arguments):
    repo = Repo.init(tmp_path / "repo")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, arguments)
    assert result.exit_code != 0
    assert "No such" in result.output
    assert Repo(repo.worktree).state.data.empty
