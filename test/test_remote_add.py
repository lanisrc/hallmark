from pathlib import Path

import pytest
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.error import CheckoutError, DestinationExistsError
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
    assert repo.plan_download().file_count == 2


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
    with pytest.raises(ValueError, match="No remote files matched"):
        repo.add(root, filter="absent*")
    assert repo.dothm.index.diff("HEAD") == []
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]


def test_remote_catalog_clone_and_branches_keep_payloads_separate(
        tmp_path, remote_listing):
    source = Repo.init(tmp_path / "source")
    source.add("https://example.test/data/", filter="*.fits")
    source.commit("Remote catalog")
    repo = Repo.clone(str(source.dothm.path), tmp_path / "copy", download=False)
    assert repo.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert repo.status()["worktree"]["deleted"] == []
    original = repo.dothm.active_branch.name
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
    repo.state.config["data"] = [{"db": "other.tsv"}]
    repo.dothm.save_state(repo.state)
    with pytest.raises(ValueError, match="only the data.tsv"):
        repo.add("https://example.test/data/")
    assert repo.state.config["data"] == [{"db": "other.tsv"}]
    assert remote_listing[1] == []


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
    assert captured == {
        "value": "ssh://lab-data/export/", "encoding": False,
        "auth": "lab", "backend": "ssh", "backend_options": {"collection": "latest"},
        "filter": ("**/*.fits", "README*"), "remote_fmt": "{name}.fits",
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


def _state_bytes(repo):
    return {name: (repo.dothm.path / name).read_bytes()
            for name in ("config.yml", "meta.yml", "data.tsv", ".git/index")}


@pytest.mark.parametrize("url, message", [
    ("https://user:secret@example.test/data/{a}.h5", "credentials"),
    ("https://secret@example.test/data/{a}.h5", "credentials"),
    ("http://secret@example.test/data/", "credentials"),
    ("sftp://user:secret@example.test/data/", "credentials"),
    ("https://example.test/data/{a}.h5?sig=secret", "query or fragment"),
    ("https://example.test/data/?secret", "query or fragment"),
    ("https://example.test/data/{a}.h5#secret", "query or fragment"),
])
def test_remote_add_rejects_secrets_in_urls_before_network(
        tmp_path, remote_listing, url, message):
    repo = Repo.init(tmp_path / "data")
    before = _state_bytes(repo)
    with pytest.raises(ValueError, match=message) as error:
        repo.add(url)
    assert "secret" not in str(error.value)
    assert remote_listing[1] == []
    assert _state_bytes(repo) == before


def test_remote_pattern_keeps_ssh_usernames():
    from hallmark.remote.add import split_remote_pattern

    assert split_remote_pattern("ssh://researcher@campus/srv/data/{run}.h5") == (
        "ssh://researcher@campus/srv/data/", "{run}.h5")


@pytest.mark.parametrize("url", [
    "https://user:secret@example.test/data/", "https://secret@example.test/data/",
    "https://example.test/data/?token=secret",
])
def test_set_config_rejects_secrets_in_remote_urls(tmp_path, url):
    repo = Repo.init(tmp_path / "data")
    before = _state_bytes(repo)
    with pytest.raises(ValueError) as error:
        repo.set_config(remote_url=url)
    assert "secret" not in str(error.value)
    assert _state_bytes(repo) == before
    assert Repo(repo.worktree).state.config == repo.state.config


@pytest.mark.parametrize("arguments", [
    ["add", "https://user:hunter2@archive.test/ER2/{a}.h5"],
    ["add", "https://hunter2@archive.test/ER2/"],
    ["set-config", "--remote-url", "https://archive.test/ER2/?sig=hunter2"],
])
def test_cli_rejects_url_secrets_without_echoing_them(
        tmp_path, monkeypatch, remote_listing, arguments):
    repo = Repo.init(tmp_path / "repo")
    monkeypatch.chdir(repo.worktree)
    before = _state_bytes(repo)
    result = CliRunner().invoke(hallmark, arguments)
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert "hunter2" not in result.output
    assert remote_listing[1] == []
    assert _state_bytes(repo) == before


@pytest.mark.parametrize("url", [
    "http://user:SECRET@git.example.test/src.git",
    "https://SECRET@git.example.test/src.git",
    "ssh://git:SECRET@git.example.test/src.git",
    "https://SECRET@catalogs.example.test/published/",
])
def test_clone_rejects_credentials_before_creating_anything(
        tmp_path, monkeypatch, url):
    def reject(*args, **kwargs):
        raise AssertionError("a source with credentials must not be contacted")

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", reject)
    monkeypatch.setattr(OperationContext, "read_text", reject)
    target = tmp_path / "copy"
    with pytest.raises(ValueError, match="credentials") as error:
        Repo.clone(url, target, download=False)
    assert "SECRET" not in str(error.value)
    assert not target.exists()
    result = CliRunner().invoke(hallmark, [
        "clone", url, str(target), "--no-download"])
    assert result.exit_code != 0
    assert "credentials" in result.output
    assert "SECRET" not in result.output
    assert not target.exists()


def test_clone_keeps_ssh_usernames(tmp_path, monkeypatch):
    from hallmark.error import CloneError

    calls = []

    def stop(url, *args, **kwargs):
        calls.append(url)
        raise CloneError("stop after the URL check")

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", stop)
    url = "ssh://git@git.example.test/team/src.git"
    with pytest.raises(CloneError, match="stop after the URL check"):
        Repo.clone(url, tmp_path / "copy", download=False)
    assert calls == [url]


def test_cli_remote_add_matching_nothing_keeps_previous_catalog(
        tmp_path, monkeypatch, remote_listing):
    repo = Repo.init(tmp_path / "repo")
    repo.add("https://example.test/data/", filter="*.fits")
    monkeypatch.chdir(repo.worktree)
    before = _state_bytes(repo)
    result = CliRunner().invoke(hallmark, [
        "add", "https://example.test/data/", "--filter", "absent*"])
    assert result.exit_code == 1
    assert "No remote files matched" in result.output
    assert "absent*" in result.output
    assert "previous catalogue kept" in result.output
    assert _state_bytes(repo) == before


def test_failed_remote_scan_is_explained_and_keeps_previous_catalog(
        tmp_path, monkeypatch, remote_listing):
    from hallmark.transport.base import DownloadError

    repo = Repo.init(tmp_path / "repo")
    repo.add("https://example.test/data/", filter="*.fits")
    before = _state_bytes(repo)

    def refuse(*args, **kwargs):
        raise DownloadError("Cannot establish connection to https://example.test")

    monkeypatch.setattr("hallmark.remote.add.discover_remote_files", refuse)
    with pytest.raises(DownloadError) as error:
        repo.add("https://example.test/data/")
    assert str(error.value) == (
        "Remote scan failed (previous catalogue kept): "
        "Cannot establish connection to https://example.test")
    assert _state_bytes(repo) == before
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]


def test_new_pattern_that_drops_catalogued_files_is_refused_offline(
        tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    root = "https://example.test/data/"
    repo.add(root + "{src}_{day:03d}.fits")
    repo.commit("Record fits files")
    remote_listing[1].clear()
    before = _state_bytes(repo)
    with pytest.raises(ValueError) as error:
        repo.add(root + "run{run:d}.fits")
    message = str(error.value)
    assert "M87_001.fits" in message and "M87_002.fits" in message
    assert "no force option" in message
    assert remote_listing[1] == []
    assert _state_bytes(repo) == before
    assert repo.state.config["data"] == [{"db": "data.tsv",
                                         "fmt": "{src}_{day:03d}.fits"}]


def test_broader_pattern_is_accepted_and_kept_rows_keep_their_information(
        tmp_path, remote_listing):
    pages, _ = remote_listing
    digest = "a" * 64
    pages[""] += '<a href="SHA256SUMS">SHA256SUMS</a>'
    pages["SHA256SUMS"] = f"{digest}  M87_001.fits\n"
    repo = Repo.init(tmp_path / "data")
    root = "https://example.test/data/"
    repo.add(root + "M87_{day:03d}.fits", filter="*001*")
    repo.commit("Record the first file")
    # The first file disappears from the server before the broader scan.
    pages[""] = pages[""].replace(
        '<a href="M87_001.fits">M87_001.fits</a>', "")
    repo.add(root + "{src}_{day:03d}.fits")
    assert repo.state.config["data"] == [{"db": "data.tsv",
                                         "fmt": "{src}_{day:03d}.fits"}]
    rows = repo.state.data.set_index("path")
    assert rows.index.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert rows.loc["M87_001.fits", "checksum"] == digest
    assert rows.loc["M87_001.fits", "checksum_algorithm"] == "sha256"
    assert rows.loc["M87_001.fits", "src"] == "M87"
    assert rows.loc["M87_002.fits", "src"] == "M87"
    assert rows["day"].tolist() == ["1", "2"]


def test_bare_url_reuses_the_saved_pattern(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    root = "https://example.test/data/"
    repo.add(root + "{src}_{day:03d}.fits")
    repo.commit("Record fits files")
    repo.add(root)
    assert repo.state.config["data"] == [{"db": "data.tsv",
                                         "fmt": "{src}_{day:03d}.fits"}]
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert repo.dothm.index.diff("HEAD") == []


def test_pattern_refusal_lists_only_a_few_files():
    import pandas as pd
    from hallmark.repo.config import check_pattern_keeps_catalogue
    from hallmark.repo.state import State

    state = State(data=pd.DataFrame({"path": [f"f{i}.txt" for i in range(5)]}))
    with pytest.raises(ValueError, match=r"5 catalogued file\(s\) "
                       r"\(f0.txt, f1.txt, f2.txt and 2 more\)"):
        check_pattern_keeps_catalogue(state, "{name}.fits")
    check_pattern_keeps_catalogue(state, "{name}.txt")
    check_pattern_keeps_catalogue(state, None)
