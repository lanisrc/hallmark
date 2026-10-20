from pathlib import Path

import pytest
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.error import CheckoutError, DestinationExistsError
from hallmark.transport import OperationContext
from hallmark.transport.base import DownloadError, RemoteObjectMissing


@pytest.fixture
def remote_listing(monkeypatch):
    pages = {"": '<h1>Index of data</h1><a href="M87_001.fits">M87_001.fits</a>'
                   '<a href="M87_002.fits">M87_002.fits</a>'
                   '<a href="notes.txt">notes.txt</a>'}
    reads = []

    def read_text(context, path, **_):
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


def test_remote_add_records_where_checksums_came_from(tmp_path, remote_listing):
    pages, _ = remote_listing
    pages[""] += '<a href="SHA256SUMS">SHA256SUMS</a>'
    pages["SHA256SUMS"] = "a" * 64 + "  M87_001.fits\n"
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/", filter="*.fits")
    for state in (repo.state, Repo(repo.worktree).state):
        rows = state.data.set_index("path")
        assert rows.loc["M87_001.fits", "checksum"] == "a" * 64
        assert rows.loc["M87_001.fits", "checksum_source"] == "published"
        assert rows.loc["M87_002.fits", "checksum_source"] == ""


def test_remote_add_extends_a_catalogue_without_provenance(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/", filter="*001.fits")
    # Catalogues committed before checksum provenance have no such column.
    repo.state.data = repo.state.data.drop(columns="checksum_source")
    repo.dothm.save_state(repo.state)
    repo.commit("Catalogue without provenance")
    repo.add("https://example.test/data/", filter="*002.fits")
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert repo.state.data.checksum_source.tolist() == ["", ""]
    assert repo.status()["staged"]["added"] == ["M87_002.fits"]
    assert repo.plan_download(all_files=True).file_count == 2


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


def test_second_server_needs_its_own_paths(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "remote")
    repo.add("https://example.test/data/")
    before = (repo.dothm.path / "config.yml").read_bytes()
    with pytest.raises(ValueError, match="--prefix DIR") as error:
        repo.add("https://different.test/data/")
    assert "M87_001.fits" in str(error.value)
    assert (repo.dothm.path / "config.yml").read_bytes() == before
    repo.add("https://different.test/data/", prefix="mirror")
    config = repo.state.config
    assert [r["name"] for r in config["remote"]] == ["origin", "different-test"]
    assert config["sources"] == [
        {"name": "origin", "remote": "origin"},
        {"name": "different-test", "remote": "different-test",
         "prefix": "mirror/"}]
    assert repo.state.data.path.tolist() == [
        "M87_001.fits", "M87_002.fits", "mirror/M87_001.fits",
        "mirror/M87_002.fits", "mirror/notes.txt", "notes.txt"]
    assert set(repo.state.data.source) == {"origin", "different-test"}


def test_remote_add_rejects_local_data_before_network(tmp_path, remote_listing):
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
    for field in ("sha1", "source", "size_bytes", "sha256"):
        with pytest.raises(ValueError, match=f"Rename the pattern field.*{field}"):
            repo.add(f"https://example.test/data/{{{field}}}_{{day}}.fits")
    assert repo.state.data.empty
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
        "--filter", "README*", "--fmt", "{name}.fits", "--remote", "lab",
        "--source", "fits", "--prefix", "lab/", "--server-checksum", "sha256"])
    assert result.exit_code == 0, result.output
    assert captured == {
        "value": "ssh://lab-data/export/", "encoding": False,
        "auth": "lab", "backend": "ssh", "backend_options": {"collection": "latest"},
        "filter": ("**/*.fits", "README*"), "remote_fmt": "{name}.fits",
        "remote": "lab", "source": "fits", "prefix": "lab/",
        "server_checksum": "sha256", "progress": True}


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


ROOT = "https://example.test/data/"


def _listing(*names, sizes=None):
    sizes = sizes or {}
    links = "".join(
        f'<a href="{name}"' + (f' data-size="{sizes[name]}"' if name in sizes else "")
        + f">{name}</a>" for name in names)
    return "<h1>Index of data</h1>" + links


def _sums(algorithm, files):
    import hashlib

    return "".join(f"{hashlib.new(algorithm, data).hexdigest()}  {name}\n"
                   for name, data in files.items())


@pytest.fixture
def two_servers(monkeypatch):
    """Serve one.test and two.test, which publish different checksums."""
    names = ("a.fits", "b.fits", "c.fits", "d.fits", "e.fits")
    pages = {
        "one.test": {
            "": _listing(*names, "SHA256SUMS", "MD5SUMS", sizes={"c.fits": 3}),
            "SHA256SUMS": _sums("sha256", {"a.fits": b"same", "b.fits": b"one"}),
            "MD5SUMS": _sums("md5", {"d.fits": b"md5 only"})},
        "two.test": {
            "": _listing(*names, "SHA256SUMS", "SHA1SUMS", sizes={"c.fits": 4}),
            "SHA256SUMS": _sums("sha256", {"a.fits": b"same", "b.fits": b"two",
                                           "d.fits": b"md5 only"}),
            "SHA1SUMS": _sums("sha1", {"e.fits": b"known"})},
    }

    def read_text(context, path, **_):
        page = pages[context.remote.host].get(path)
        if page is None:
            raise RemoteObjectMissing("No metadata")
        return page

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    return pages


def test_paths_another_source_catalogs_are_classified_by_content(tmp_path,
                                                                 two_servers):
    import hashlib

    repo = Repo.init(tmp_path / "data")
    repo.add("https://one.test/data/", filter="*.fits")
    # A download recorded the SHA-1 of e.fits.
    data = repo.state.data
    data.loc[data.path == "e.fits", "sha1"] = hashlib.sha1(b"known").hexdigest()
    repo.dothm.save_state(repo.state)
    before = (repo.dothm.path / "data.tsv").read_bytes()
    with pytest.raises(ValueError) as error:
        repo.add("https://two.test/data/", filter="*.fits")
    message = str(error.value)
    assert ("5 path(s) already belong to another source (2 with the same content, "
            "2 with different content, 1 that cannot be compared):") in message
    for line in ("a.fits (same content; source 'origin')",
                 "b.fits (different content; source 'origin')",
                 "c.fits (different content; source 'origin')",
                 "d.fits (cannot compare; source 'origin')",
                 "e.fits (same content; source 'origin')"):
        assert f"  {line}\n" in message
    assert message.endswith("Add this source with --prefix DIR, or narrow it with "
                            "--filter; previous catalogue kept.")
    # Overlapping patterns in one folder name the same server file.
    with pytest.raises(ValueError, match=r"a\.fits \(same content; source 'origin'\)"):
        repo.add("https://one.test/data/", filter="a.*")
    assert (repo.dothm.path / "data.tsv").read_bytes() == before


def test_nested_folder_reuses_its_remote_as_a_new_source(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*001.fits")
    repo.add(ROOT + "sub/", filter="*.txt")
    config = repo.state.config
    assert config["remote"] == [{"name": "origin", "url": ROOT, "backend": "http"}]
    assert config["sources"] == [
        {"name": "origin", "remote": "origin", "filter": ["*001.fits"]},
        {"name": "origin-sub", "remote": "origin", "root": "sub/",
         "filter": ["*.txt"]}]
    rows = repo.state.data.set_index("path")["source"].to_dict()
    assert rows == {"M87_001.fits": "origin", "sub/notes.txt": "origin-sub"}
    plan = repo.plan_download(all_files=True)
    assert sorted(item.relative_path.as_posix() for item in plan.items) == [
        "M87_001.fits", "sub/notes.txt"]


def test_two_patterns_in_one_folder_are_two_sources(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT + "{src}_{day:03d}.fits")
    repo.add(ROOT, filter="*.txt")
    assert [s["name"] for s in repo.state.config["sources"]] == ["origin", "origin-2"]
    data = repo.state.data.set_index("path")
    assert data.loc["M87_002.fits", "day"] == "002"
    assert data.loc["notes.txt", "source"] == "origin-2"
    assert data.loc["notes.txt", "day"] == ""
    with pytest.raises(ValueError, match="choose one with --source"):
        repo.add(ROOT)
    # Repeating a selection updates its own source.
    repo.add(ROOT, filter="*.txt")
    assert len(repo.state.config["sources"]) == 2


def test_changing_a_pattern_names_the_source(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT + "{src}_{day:03d}.fits")
    repo.commit("Catalogue")
    with pytest.raises(ValueError, match="--source origin"):
        repo.add(ROOT + "{name}.fits")
    with pytest.raises(ValueError, match="do not fit"):
        repo.add(ROOT + "{src}_001.fits", source="origin")
    assert repo.dothm.index.diff("HEAD") == []
    repo.add(ROOT + "{name}.fits", source="origin")
    assert repo.state.config["sources"] == [
        {"name": "origin", "remote": "origin", "fmt": "{name}.fits"}]
    assert repo.state.data["name"].tolist() == ["M87_001", "M87_002"]
    assert "day" not in repo.state.data.columns
    with pytest.raises(ValueError, match="keeps its prefix"):
        repo.add(ROOT + "{name}.fits", prefix="elsewhere")


def test_readding_a_source_prunes_files_gone_from_the_server(
        tmp_path, remote_listing, monkeypatch):
    pages, _ = remote_listing
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.commit("Catalogue")
    pages[""] = _listing("M87_002.fits", "M87_003.fits")
    result = repo.add(ROOT, filter="*.fits")
    assert result.attrs["removed"] == ["M87_001.fits"]
    assert repo.state.data.path.tolist() == ["M87_002.fits", "M87_003.fits"]
    status = repo.status()["staged"]
    assert (status["added"], status["deleted"]) == (["M87_003.fits"],
                                                   ["M87_001.fits"])
    monkeypatch.chdir(repo.worktree)
    pages[""] = _listing("M87_003.fits")
    output = CliRunner().invoke(hallmark, ["add", ROOT, "--filter", "*.fits"])
    assert output.exit_code == 0, output.output
    assert "no longer on the server" in output.output
    assert "  M87_002.fits" in output.output


def test_known_sha1_is_kept_only_for_unchanged_files(tmp_path, remote_listing):
    pages, _ = remote_listing
    pages[""] = _listing("a.fits", "b.fits", sizes={"a.fits": 4, "b.fits": 5})
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT)
    # Downloads record content hashes this way.
    repo.state.data["sha1"] = ["1" * 40, "2" * 40]
    repo.dothm.save_state(repo.state)
    pages[""] = _listing("a.fits", "b.fits", sizes={"a.fits": 4, "b.fits": 6})
    repo.add(ROOT)
    assert repo.state.data.set_index("path")["sha1"].to_dict() == {
        "a.fits": "1" * 40, "b.fits": ""}
    assert repo.state.data.set_index("path")["size_bytes"].to_dict() == {
        "a.fits": "4", "b.fits": "6"}


def test_readding_a_source_lists_files_changed_on_the_server(tmp_path, remote_listing,
                                                              monkeypatch):
    pages, _ = remote_listing
    names = ("a.fits", "b.fits", "c.fits", "d.fits")
    pages[""] = _listing(*names, sizes=dict.fromkeys(names, 4))
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT)
    repo.state.data["sha1"] = ["1" * 40, "", "", "4" * 40]
    repo.dothm.save_state(repo.state)
    (repo.worktree / "b.fits").write_bytes(b"bbbb")
    # a.fits and b.fits were downloaded, c.fits never was, d.fits is unchanged.
    pages[""] = _listing(*names, sizes={**dict.fromkeys(names, 5), "d.fits": 4})
    assert repo.add(ROOT).attrs["changed"] == ["a.fits", "b.fits"]
    pages[""] = _listing(*names, sizes=dict.fromkeys(names, 6))
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["add", ROOT])
    assert result.exit_code == 0, result.output
    assert ("Changed upstream (2); run hm download to replace the local copies:\n"
            "  b.fits\n  d.fits\n") in result.output


def test_remote_name_selects_or_creates_a_remote(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    with pytest.raises(ValueError, match="not inside remote 'origin'"):
        repo.add("https://other.test/data/", remote="origin")
    with pytest.raises(ValueError, match="Source names"):
        repo.add("https://other.test/data/", remote="bad name")
    repo.add("https://other.test/data/", remote="mirror", source="copy",
             prefix="mirror", filter="*.txt")
    assert repo.state.config["remote"][1] == {
        "name": "mirror", "url": "https://other.test/data/", "backend": "http"}
    assert repo.state.config["sources"][1] == {
        "name": "copy", "remote": "mirror", "prefix": "mirror/",
        "filter": ["*.txt"]}
    assert "mirror/notes.txt" in repo.state.data.path.tolist()


def test_legacy_catalogue_gains_sources_on_its_next_add(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    rows = repo.add(ROOT + "{src}_{day:03d}.fits").drop(
        columns=["source", "checksum_source"])
    # A catalogue as written before sources existed.
    repo.state.config = {"data": [{"db": "data.tsv", "fmt": "{src}_{day:03d}.fits"}],
                         "remote": [{"name": "origin", "url": ROOT,
                                     "backend": "http"}]}
    repo.state.data = rows
    repo.dothm.save_state(repo.state)
    repo.commit("Legacy catalogue")
    legacy = Repo(repo.worktree)
    assert "sources" not in legacy.state.config
    assert legacy.plan_download(all_files=True).file_count == 2
    legacy.add(ROOT, filter="*.txt")
    assert legacy.state.config["sources"] == [
        {"name": "origin", "remote": "origin", "fmt": "{src}_{day:03d}.fits"},
        {"name": "origin-2", "remote": "origin", "filter": ["*.txt"]}]
    assert legacy.state.config["data"] == [{"db": "data.tsv"}]
    assert legacy.state.data.set_index("path")["source"].to_dict() == {
        "M87_001.fits": "origin", "M87_002.fits": "origin",
        "notes.txt": "origin-2"}


def test_renaming_a_remote_keeps_its_sources(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.add(ROOT + "sub/", filter="*.txt")
    repo.set_config(remote_name="archive")
    repo.dothm.save_state(repo.state)
    reopened = Repo(repo.worktree).state.config
    assert reopened["remote"][0]["name"] == "archive"
    assert [s["remote"] for s in reopened["sources"]] == ["archive", "archive"]
    repo.add(ROOT, filter="*.fits")
    assert [s["name"] for s in repo.state.config["sources"]] == [
        "origin", "origin-sub"]


@pytest.fixture
def fetched(monkeypatch):
    """Record HTTP fetches and write each file's URL as its content."""
    from hallmark.transport.http import HttpBackend

    calls = []

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        url = self.context.remote.file_url(relative_path)
        calls.append(url)
        Path(destination).write_text(url)

    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    return calls


def test_download_fetches_each_source_from_its_remote(
        tmp_path, remote_listing, fetched):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.add(ROOT + "sub/", filter="*.txt")
    repo.add("https://other.test/data/", filter="*.txt", prefix="mirror")
    repo.commit("Three sources")
    plan = repo.plan_download(all_files=True)
    assert set(plan.remotes) == {"origin", "other-test"}
    assert plan.remote_url is None
    summary = plan.summary()
    assert "Sources:\n  origin: https://example.test/data/ (3 file(s))" in summary
    assert "other-test: https://other.test/data/ (1 file(s))" in summary
    # The plan pins its remotes; later configuration changes do not apply.
    repo.set_config(remote_name="origin", remote_url="https://moved.test/data/")
    result = repo.download(plan, approved=True)
    assert (result["succeeded"], result["failed"]) == (4, 0)
    assert sorted(fetched) == [
        ROOT + "M87_001.fits", ROOT + "M87_002.fits", ROOT + "sub/notes.txt",
        "https://other.test/data/notes.txt"]
    assert (repo.worktree / "mirror/notes.txt").read_text() == (
        "https://other.test/data/notes.txt")
    assert (repo.worktree / "sub/notes.txt").read_text() == ROOT + "sub/notes.txt"


def test_named_remote_serves_every_file_at_its_remote_path(
        tmp_path, remote_listing, fetched):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*001.fits")
    repo.add("https://other.test/data/", filter="*.txt", prefix="mirror")
    plan = repo.plan_download(all_files=True, remote_name="other-test")
    assert list(plan.remotes) == ["other-test"]
    assert plan.remote_url == "https://other.test/data/"
    assert [(item.relative_path.as_posix(), item.source_path.as_posix())
            for item in plan.items] == [("M87_001.fits", "M87_001.fits"),
                                        ("mirror/notes.txt", "notes.txt")]
    repo.download(plan, approved=True)
    assert sorted(fetched) == ["https://other.test/data/M87_001.fits",
                               "https://other.test/data/notes.txt"]


def test_plan_summary_counts_cached_files_per_remote(tmp_path):
    from hallmark.remote.plan import DownloadItem, DownloadPlan

    remotes = {"origin": {"url": "https://a.test/"},
               "mirror": {"url": "https://b.test/"}}
    plan = DownloadPlan(
        (DownloadItem("a", remote="origin"),), None, tmp_path, remotes=remotes,
        stored_items=(DownloadItem("b", remote="mirror", sha1="1" * 40),))
    summary = plan.summary()
    assert "origin: https://a.test/ (1 file(s))" in summary
    assert "mirror: https://b.test/ (1 file(s))" in summary
    assert "Already in .hm/objects: 1 file(s) (no transfer)" in summary


def test_plans_reject_items_naming_unrecorded_remotes(tmp_path):
    from hallmark.remote.plan import DownloadItem, DownloadPlan

    with pytest.raises(ValueError, match="does not record"):
        DownloadPlan((DownloadItem("a", remote="mirror"),), None, tmp_path)
    with pytest.raises(TypeError, match="URL"):
        DownloadPlan((), None, tmp_path, remotes={"mirror": {"auth": None}})
    plan = DownloadPlan(
        (DownloadItem("a", remote="mirror", remote_path="b"),), None, tmp_path,
        remotes={"mirror": {"url": "https://m.test/", "backend": "http"}})
    assert plan.items[0].source_path == Path("b")
    with pytest.raises(TypeError):
        plan.remotes["mirror"]["url"] = "https://elsewhere.test/"


def test_download_selects_files_by_source(tmp_path, remote_listing, monkeypatch):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.add(ROOT + "sub/", filter="*.txt")
    plan = repo.plan_download(sources=["origin-sub"])
    assert [item.relative_path.as_posix() for item in plan.items] == ["sub/notes.txt"]
    with pytest.raises(DownloadError, match="Unknown source.*origin, origin-sub"):
        repo.plan_download(sources=["missing"])
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["download", "--source", "origin",
                                           "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "2 file(s)" in result.output
    assert "  M87_001.fits" in result.output


@pytest.mark.parametrize("served", [b"abc", b"abcdefg"])
def test_download_rejects_a_size_that_differs_from_the_catalogue(
        tmp_path, remote_listing, monkeypatch, served):
    # Issue #54: a truncated file was accepted when no checksum was published.
    from hallmark.transport.http import HttpBackend

    pages, _ = remote_listing
    pages[""] = _listing("a.fits", sizes={"a.fits": 5})
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT)
    monkeypatch.setattr(HttpBackend, "fetch", lambda self, path, destination, **kw:
                        Path(destination).write_bytes(served))
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    assert (result["succeeded"], result["failed"]) == (0, 1)
    assert "expected 5 bytes" in result["errors"][0]
    assert sorted(path.name for path in repo.worktree.iterdir()) == [".hm"]


def test_catalogued_checksum_is_verified_before_a_known_sha1(
        tmp_path, remote_listing, monkeypatch):
    import hashlib

    from hallmark.transport.http import HttpBackend

    pages, _ = remote_listing
    pages[""] = _listing("a.fits", "b.fits", "SHA256SUMS")
    pages["SHA256SUMS"] = hashlib.sha256(b"right").hexdigest() + "  a.fits\n"
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.state.data["sha1"] = [hashlib.sha1(b"right").hexdigest(),
                               hashlib.sha1(b"other").hexdigest()]
    repo.dothm.save_state(repo.state)
    plan = repo.plan_download(all_files=True)
    assert [(item.checksum, item.sha1) for item in plan.items] == [
        (("sha256", hashlib.sha256(b"right").hexdigest()),
         hashlib.sha1(b"right").hexdigest()),
        (hashlib.sha1(b"other").hexdigest(), hashlib.sha1(b"other").hexdigest())]
    monkeypatch.setattr(HttpBackend, "fetch", lambda self, path, destination, **kw:
                        Path(destination).write_bytes(b"right"))
    result = repo.download(plan, approved=True)
    assert (result["succeeded"], result["failed"]) == (1, 1)
    assert "b.fits (sha1)" in result["errors"][0]
    assert (repo.worktree / "a.fits").read_bytes() == b"right"


def test_downloads_select_the_staged_catalogue_not_the_file_on_disk(
        tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.commit("FITS files")
    repo.add(ROOT, filter="*.txt", source="notes", prefix="notes")
    repo.restore_staged(["."])
    # Unstaging keeps .hm/data.tsv on disk as it was, like a git working copy.
    assert "notes/notes.txt" in (repo.dothm.path / "data.tsv").read_text()
    plan = repo.plan_download(all_files=True)
    assert sorted(item.relative_path.as_posix() for item in plan.items) == [
        "M87_001.fits", "M87_002.fits"]


def test_unstaging_a_folder_returns_its_rows_to_head(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.commit("FITS files")
    head_tsv = repo.dothm.git.show("HEAD:data.tsv")
    repo.add(ROOT, filter="*.txt", source="notes", prefix="notes")
    assert repo.status()["staged"]["added"] == ["notes/notes.txt"]
    repo.restore_staged(["notes"])
    assert repo.status()["staged"]["added"] == []
    assert repo.dothm.git.show(":data.tsv") == head_tsv
    with pytest.raises(ValueError, match="No staged or committed path matches"):
        repo.restore_staged(["missing"])


def _legacy_catalogue(tmp_path, remote, data):
    """Write a catalogue as Hallmark wrote one before sources existed."""
    repo = Repo.init(tmp_path / "data")
    rows = repo.add(ROOT + "{src}_{day:03d}.fits").drop(
        columns=["source", "checksum_source"])
    repo.state.config = {"data": data, "remote": remote}
    repo.state.data = rows
    repo.dothm.save_state(repo.state)
    repo.commit("Legacy catalogue")
    legacy = Repo(repo.worktree)
    assert "sources" not in legacy.state.config
    return legacy


@pytest.mark.parametrize("remote, data", [
    ([{"name": "origin", "url": ROOT, "backend": "http"}],
     [{"db": "data.tsv", "fmt": "{src}_{day:03d}.fits"}]),
    ({"url": ROOT}, [{"fmt": "{src}_{day:03d}.fits"}]),
])
def test_downloading_a_legacy_catalogue_records_its_source_first(
        tmp_path, remote_listing, fetched, remote, data):
    legacy = _legacy_catalogue(tmp_path, remote, data)
    result = legacy.download(legacy.plan_download(all_files=True), approved=True)
    assert result["staged"] == 2 and result["catalog_upgraded"]
    assert legacy.state.config["sources"] == [
        {"name": "origin", "remote": "origin", "fmt": "{src}_{day:03d}.fits"}]
    assert legacy.state.data["source"].tolist() == ["origin", "origin"]
    # Still a catalogue of server files: committing stores no payload, files
    # never downloaded are not "deleted", and the next add works.
    assert legacy.status()["worktree"]["deleted"] == []
    assert legacy.commit("Record checksums")
    reopened = Repo(legacy.worktree)
    assert reopened.state.data["sha1"].str.len().tolist() == [40, 40]
    reopened.add(ROOT, filter="*.txt")
    assert reopened.state.data["path"].tolist() == [
        "M87_001.fits", "M87_002.fits", "notes.txt"]


def test_a_legacy_catalogue_with_several_remotes_is_left_as_it_was(
        tmp_path, remote_listing, fetched):
    legacy = _legacy_catalogue(
        tmp_path, [{"name": "origin", "url": ROOT},
                   {"name": "mirror", "url": "https://mirror.test/data/"}],
        [{"db": "data.tsv", "fmt": "{src}_{day:03d}.fits"}])
    result = legacy.download(legacy.plan_download(all_files=True), approved=True)
    assert result["succeeded"] == 2 and "staged" not in result
    assert "predates sources" in result["staging_note"]
    assert not legacy.dothm.index.diff("HEAD")
    assert "sha1" not in Repo(legacy.worktree).state.data.columns


@pytest.mark.parametrize("path", [
    "x/.git/config", "x/.GIT/hooks/post-checkout", "a/b/.hm/data.tsv", ".Hm/x"])
def test_catalogue_rows_cannot_plant_repository_metadata(tmp_path, path):
    from hallmark.utils import validate_relative_path

    with pytest.raises(ValueError, match="repository metadata"):
        validate_relative_path(path)
    repo = Repo.init(tmp_path / "data")
    repo.state.config = {"data": [{"db": "data.tsv"}],
                         "remote": [{"name": "origin", "url": ROOT}],
                         "sources": [{"name": "origin", "remote": "origin"}]}
    repo.state.data = __import__("pandas").DataFrame(
        {"path": [path], "source": ["origin"]})
    with pytest.raises(Exception, match="repository metadata"):
        repo.plan_download(all_files=True)


def test_source_prefixes_cannot_name_metadata_in_any_case(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    with pytest.raises(ValueError, match="repository metadata"):
        repo.add(ROOT, filter="*.fits", prefix="data/.Git")
    assert repo.state.data.empty


@pytest.mark.parametrize("url", [
    "https://user:secret@example.test/data/",
    "https://example.test/data/?sig=secret",
    "https://example.test/data/#secret",
])
def test_remote_urls_with_secrets_are_refused_wherever_they_come_from(
        tmp_path, url):
    from hallmark.repo.config import normalize_remotes
    from hallmark.transport import RemoteSpec

    with pytest.raises(ValueError) as error:
        RemoteSpec.from_url(url)
    assert "secret" not in str(error.value)
    # A configuration written by hand or received in a clone is checked too.
    with pytest.raises(ValueError):
        normalize_remotes([{"name": "origin", "url": url}])
    with pytest.raises(ValueError):
        Repo.clone(url, tmp_path / "copy", download=False)
    assert not (tmp_path / "copy").exists()


def test_an_add_never_saves_over_a_change_made_during_its_scan(
        tmp_path, remote_listing, monkeypatch):
    from hallmark.transport import OperationContext

    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    pages, _ = remote_listing
    real_read = OperationContext.read_text

    def read_text(context, path, **_):
        # Another command saves metadata while this scan lists the server.
        other = Repo(repo.worktree)
        other.state.meta["owner"] = "eht"
        other.dothm.save_state(other.state)
        return real_read(context, path, **_)

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    with pytest.raises(ValueError, match="changed while the server was scanned"):
        repo.add(ROOT, filter="*.txt", source="notes")
    saved = Repo(repo.worktree).state
    assert saved.meta == {"owner": "eht"}
    assert "notes.txt" not in set(saved.data["path"])


def test_a_plain_checksums_file_is_verified_by_digest_length(
        tmp_path, remote_listing, fetched):
    import hashlib

    pages, _ = remote_listing
    good = hashlib.sha256((ROOT + "M87_001.fits").encode()).hexdigest()
    pages[""] += '<a href="checksums.txt">checksums.txt</a>'
    pages["checksums.txt"] = (f"{good}  M87_001.fits\n"
                              f"{'0' * 64}  M87_002.fits\n")
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    rows = repo.state.data.set_index("path")
    assert rows.loc["M87_001.fits", "checksum_algorithm"] == "sha256"
    assert rows.loc["M87_001.fits", "checksum_source"] == "published"
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    assert result["succeeded"] == 1
    assert result["errors"] == ["Checksum mismatch for M87_002.fits (sha256)"]


def test_an_unnamed_remote_is_origin_everywhere(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.state.config["remote"] = {"url": ROOT}
    repo.dothm.save_state(repo.state)
    plan = Repo(repo.worktree).plan_download(all_files=True, remote_name="origin")
    assert plan.remote_name == "origin"
    assert list(plan.remotes) == ["origin"]


def test_checkout_explains_how_to_clear_unstaged_hallmark_state(
        tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, filter="*.fits")
    repo.commit("FITS files")
    repo.create_branch("other")
    repo.set_config(remote_name="renamed")
    with pytest.raises(CheckoutError, match="Commit or unstage.*config.yml"):
        repo.checkout("other")
    repo.restore_staged(["."])
    with pytest.raises(CheckoutError) as error:
        repo.checkout("other")
    assert "Unstaged changes in .hm block checkout: config.yml" in str(error.value)
    assert f"git -C {repo.dothm.path} restore config.yml" in str(error.value)
    repo.dothm.git.restore("config.yml")
    reopened = Repo(repo.worktree)
    assert reopened.checkout("other")
    assert reopened.state.config["remote"][0]["name"] == "origin"
