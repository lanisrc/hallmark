"""Named-source and extraction workflows using only in-memory metadata fixtures."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from hallmark import DataSource, Repo, SourceRelease, register_source
from hallmark import sources
from hallmark.cli import hallmark
from hallmark.sources import get_source, list_sources, resolve_source
from hallmark.transport import OperationContext
from hallmark.transport.base import DownloadError, RemoteObjectMissing


def index(*paths):
    return '<h1>Index of data</h1>' + ''.join(
        f'<a href="{path}">{path}</a>' for path in paths)


@pytest.fixture
def pages(monkeypatch):
    content, reads = {}, []

    def read(context, path):
        url = context.remote.url.rstrip("/") + "/" + path
        reads.append(url)
        if url not in content:
            raise RemoteObjectMissing(f"Unexpected request: {url}")
        return content[url]

    monkeypatch.setattr(OperationContext, "read_text", read)
    return content, reads


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(sources, "_registered", dict(sources._registered))
    monkeypatch.setattr(sources, "_loaded", {})
    monkeypatch.setattr(sources, "_entry_points", lambda: [])


def test_desi_definitions_are_explicit_and_offline(pages):
    descriptor = get_source("desi")
    assert list(descriptor.releases) == ["edr", "dr1"]
    assert descriptor.releases["edr"].collections["redshifts"] == (
        "spectro/redux/fuji/zcatalog",)
    assert "lss" not in descriptor.releases["edr"].collections
    assert descriptor.releases["dr1"].collections["lss"] == (
        "survey/catalogs/dr1/LSS",)
    assert "desi" in [item.name for item in list_sources()]
    result = CliRunner().invoke(hallmark, ["sources", "desi"])
    assert result.exit_code == 0, result.output
    assert "spectro/redux/iron/zcatalog/" in result.output
    assert "entire release" in result.output
    assert pages[1] == []


def test_collection_union_crawls_only_selected_roots(tmp_path, pages):
    content, reads = pages
    root = "https://data.desi.lbl.gov/public/dr1/"
    zcat = "spectro/redux/iron/zcatalog/"
    content[root + zcat] = index("zall.fits", "notes.txt")
    content[root + "vac/"] = index("extra.fits")
    events = []
    repo = Repo.init(tmp_path / "desi", source="desi", release="dr1",
                     collections=["redshifts", "vac", "redshifts"],
                     filter="**/*.fits", progress=events.append)
    assert any(event["files"] == 2 and event["matched"] == 1 for event in events)
    expected = [zcat + "zall.fits", "vac/extra.fits"]
    assert repo.state.data["path"].tolist() == expected
    assert reads == [root + zcat, root + "vac/"]
    stored = yaml.safe_load((repo.dothm.path / "meta.yml").read_text())["source"]
    assert stored == {"name": "desi", "release": "dr1", "url": root,
                      "collections": ["redshifts", "vac"]}
    repo = Repo(repo.worktree)
    plan = repo.plan_download(filter="**/zall.fits")
    assert plan.remote_url == root
    assert [item.relative_path.as_posix() for item in plan.items] == expected[:1]
    assert reads == [root + zcat, root + "vac/"]
    with pytest.raises(DownloadError, match="not in the catalog"):
        repo.plan_download(file_paths=["spectro/omitted.fits"])


def test_omitting_collection_includes_entire_release(tmp_path, pages):
    root = "https://data.desi.lbl.gov/public/edr/"
    pages[0].update({root: index("LICENSE.md", "spectro/", "target/"),
                     root + "spectro/": index("spectra.fits"),
                     root + "target/": index("targets.fits")})
    repo = Repo.init(tmp_path / "all", source="desi", release="edr")
    assert repo.state.data["path"].tolist() == [
        "LICENSE.md", "spectro/spectra.fits", "target/targets.fits"]
    assert not (repo.worktree / "LICENSE.md").exists()


@pytest.mark.parametrize("options, message", [
    ({"source": "desi"}, "requires a release"),
    ({"source": "desi", "release": "dr2"}, "Unknown release"),
    ({"source": "desi", "release": "edr", "collections": ["lss"]},
     "Unknown collection"),
    ({"source": "unknown"}, "Unknown data source"),
    ({"source": "https://test/data/", "release": "dr1"}, "named data source"),
    ({"source": "https://test/data/", "collections": []}, "named data source"),
    ({"filter": "*.fits"}, "require source"),
])
def test_invalid_source_selection_precedes_io(tmp_path, pages, options, message):
    destination = tmp_path / "catalog"
    with pytest.raises(ValueError, match=message):
        Repo.init(destination, **options)
    assert pages[1] == []
    assert not destination.exists()


def test_cli_release_prompt_and_cancellation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(Repo, "init", lambda path, **kw: calls.append((path, kw)))
    runner = CliRunner()
    # Click replaces stdin during invocation, so set isatty on the replacement.
    original = get_source
    cancel = False

    def terminal_source(name):
        import sys
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        if cancel:
            # Click 8.1's runner returns empty strings forever at EOF. Model a
            # real terminal's EOF instead; CliRunner restores this hook on exit.
            from click import termui

            def eof_input(prompt):
                raise EOFError()

            termui.visible_prompt_func = eof_input
        return original(name)

    monkeypatch.setattr("hallmark.cli.get_source", terminal_source)
    destination = str(tmp_path / "repo")
    result = runner.invoke(hallmark, ["init", destination, "--from", "desi"],
                           input="invalid\ndr1\n")
    assert result.exit_code == 0, result.output
    assert calls[0][1]["release"] == "dr1"
    assert calls[0][1]["collections"] is None
    calls.clear()
    cancel = True
    result = runner.invoke(hallmark, ["init", destination, "--from", "desi"], input="")
    assert result.exit_code != 0
    assert calls == []
    assert not Path(destination).exists()


def test_cli_requires_release_without_terminal(tmp_path, pages):
    result = CliRunner().invoke(hallmark, [
        "init", str(tmp_path / "repo"), "--from", "desi"], input="dr1\n")
    assert result.exit_code != 0
    assert "--release is required in noninteractive use" in result.output
    assert not (tmp_path / "repo").exists()
    assert pages[1] == []


@pytest.mark.parametrize("template", [
    "{", "{run:invalid}", "{path}", "{checksum}", "{sha1}",
    "{size_bytes}", "{mtime}", "{checksum_algorithm}", "{}", "{0}",
    "{a.b}", "{a[0]}", "{run!r}", "constant", "",
])
def test_invalid_extraction_precedes_io(tmp_path, pages, template):
    with pytest.raises(ValueError):
        Repo.init(tmp_path / "invalid", source="https://test/data/", format=template)
    assert pages[1] == []
    assert not (tmp_path / "invalid").exists()


def test_extraction_retains_unmatched_files_through_reload_and_download(
        tmp_path, pages, monkeypatch):
    from mock_server import MockServer

    root = "https://test/data/"
    pages[0][root] = index("run_001.h5", "RUN_002.h5", "README.md", "omit.bin")
    repo = Repo.init(tmp_path / "repo", source=root,
                     filter=["*.h5", "README*"], format="run_{run:03d}.h5")
    before = (repo.dothm.path / "data.tsv").read_bytes()
    repo = Repo(repo.worktree)
    rows = repo.state.data.set_index("path")
    assert set(rows.index) == {"run_001.h5", "RUN_002.h5", "README.md"}
    assert float(rows.loc["run_001.h5", "run"]) == 1
    assert rows.loc["README.md", "run"] == ""
    assert rows.loc["RUN_002.h5", "run"] == ""
    server = MockServer(root)
    server.add_file("README.md", b"notes")
    monkeypatch.setattr("requests.Session", lambda: server)
    plan = repo.plan_download(filter="README*")
    assert plan.file_count == 1
    result = repo.download(plan, approved=True)
    assert result["succeeded"] == 1
    assert (repo.worktree / "README.md").read_bytes() == b"notes"
    assert (repo.dothm.path / "data.tsv").read_bytes() == before
    # Configuration changes and a metadata clone preserve extraction and literal paths.
    repo.set_config(remote_name="archive")
    repo.dothm.index.add(["config.yml"])
    repo.dothm.index.commit("Rename data remote")
    clone = Repo.clone(str(repo.dothm.path), tmp_path / "copy")
    assert clone.state.config["data"] == repo.state.config["data"]
    assert clone.plan_download().file_count == 3
    assert not (clone.worktree / "README.md").exists()


def test_empty_catalog_retains_extraction_columns(tmp_path, pages):
    root = "https://test/empty/"
    pages[0][root] = index()
    repo = Repo.init(tmp_path / "empty", source=root, format="run_{run:d}.h5")
    assert "run" in repo.state.data.columns
    assert repo.state.data.empty
    assert repo.plan_download().file_count == 0


def test_source_registration_and_overlapping_roots(registry):
    release = SourceRelease("https://test/", {"both": ["a", "a/b"], "one": "a"})
    descriptor = DataSource("example", "Example source", {"v1": release})
    register_source(descriptor)
    _, roots, provenance = resolve_source("example", "v1", ["both", "one"])
    assert roots == ("a",)
    assert provenance["collections"] == ["both", "one"]
    with pytest.raises(ValueError, match="Duplicate"):
        register_source(descriptor)
    with pytest.raises(TypeError):
        descriptor.releases["v2"] = release


def test_source_plugin_validation(registry, monkeypatch):
    descriptor = DataSource("plugin", "A plugin", {"v1": SourceRelease(
        "https://test/", {})})
    entry = SimpleNamespace(name="plugin", load=lambda: descriptor)
    monkeypatch.setattr(sources, "_entry_points", lambda: [entry])
    assert get_source("plugin") is descriptor
    assert get_source("plugin") is descriptor
    monkeypatch.setattr(sources, "_entry_points", lambda: [entry, entry])
    with pytest.raises(ValueError, match="Duplicate"):
        get_source("plugin")


@pytest.mark.parametrize("operation", ["open", "git-clone", "snapshot-clone"])
def test_old_auth_references_require_explicit_migration(tmp_path, pages, operation):
    repo = Repo.init(tmp_path / "legacy")
    config = {"data": [{"db": "data.tsv"}], "remote": [{
        "name": "origin", "url": "sftp://lab-data/export/", "auth": "old"}]}
    repo.dothm.dump_yml(config, "config")
    repo.dothm.index.add(["config.yml"])
    repo.dothm.index.commit("Legacy profile reference")
    destination = tmp_path / "copy"
    if operation == "snapshot-clone":
        root = "https://test/catalog/"
        pages[0].update({root + "config.yml": yaml.safe_dump(config),
                         root + "meta.yml": "{}\n", root + "data.tsv": "path\n"})
    with pytest.raises(ValueError, match="~/.ssh/config"):
        if operation == "open":
            Repo(repo.worktree)
        elif operation == "git-clone":
            Repo.clone(str(repo.dothm.path), destination)
        else:
            Repo.clone(root, destination, source_type="catalog")
    assert not destination.exists()
    assert yaml.safe_load((repo.dothm.path / "config.yml").read_text()) == config


@pytest.mark.parametrize("option", [
    "--auth", "--backend", "--backend-options", "--include", "--extract", "--fmt",
    "--max-workers", "--with-download",
])
def test_init_removed_options_have_no_alias(tmp_path, pages, option):
    destination = tmp_path / "new"
    result = CliRunner().invoke(hallmark, ["init", str(destination), option])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert not destination.exists()
    assert pages[1] == []


@pytest.mark.parametrize("kwargs", [
    {"from_url": "https://test/"}, {"auth": "old"}, {"backend": "http"},
    {"include": "*"}, {"extract": "{name}"}, {"fmt": "{name}"}, {"download": True},
])
def test_init_removed_python_arguments_have_no_shim(tmp_path, kwargs):
    with pytest.raises(TypeError):
        Repo.init(tmp_path / "new", **kwargs)
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("option", ["--release", "--format"])
def test_empty_remote_option_does_not_silently_initialize_locally(tmp_path, option):
    destination = tmp_path / "new"
    result = CliRunner().invoke(hallmark, ["init", str(destination), option, ""])
    assert result.exit_code != 0
    assert "require source" in result.output
    assert not destination.exists()


def test_automatic_formats_preserve_mixed_catalog_and_downloads(
        tmp_path, pages, monkeypatch):
    from mock_server import MockServer

    root = "https://test/data/"
    paths = ["run_001.h5", "run_002.h5", "sgra_20170406_nustar.fits",
             "sgra_20170411_chandra.fits", "README.md", "archive.tar.gz"]
    pages[0][root] = index(*paths)
    repo = Repo.init(tmp_path / "auto", source=root)
    assert pages[1] == [root]  # Detection uses filenames, never payloads.
    stored = repo.state.config["data"][0]
    assert "fmt" not in stored
    assert stored["extraction"] == {
        "automatic": True,
        "formats": ["run_{scan}.h5", "sgra_{date}_{p0}.fits"],
    }
    repo = Repo(repo.worktree)
    rows = repo.state.data.set_index("path")
    assert set(rows.index) == set(paths)
    assert str(rows.loc["run_001.h5", "scan"]).zfill(3) == "001"
    assert str(rows.loc["sgra_20170406_nustar.fits", "date"]) == "20170406"
    assert rows.loc["sgra_20170406_nustar.fits", "p0"] == "nustar"
    for path in ["README.md", "archive.tar.gz"]:
        assert all(rows.loc[path, field] == "" for field in ("scan", "date", "p0"))
    assert rows.loc["run_001.h5", "date"] == ""
    assert rows.loc["sgra_20170406_nustar.fits", "scan"] == ""

    before = (repo.dothm.path / "data.tsv").read_bytes()
    server = MockServer(root)
    for path in ["run_001.h5", "README.md"]:
        server.add_file(path, b"tiny fixture")
    monkeypatch.setattr("requests.Session", lambda: server)
    plan = repo.plan_download(filter=["run_001.h5", "README*"])
    assert {item.relative_path.as_posix() for item in plan.items} == {
        "run_001.h5", "README.md"}
    assert repo.download(plan, approved=True)["succeeded"] == 2
    assert (repo.dothm.path / "data.tsv").read_bytes() == before
    repo.set_config(remote_name="archive")
    repo.dothm.index.add(["config.yml"])
    repo.dothm.index.commit("Rename remote")
    clone = Repo.clone(str(repo.dothm.path), tmp_path / "copy")
    assert clone.state.config["data"] == [stored]
    assert clone.plan_download().file_count == len(paths)
    assert (clone.dothm.path / "data.tsv").read_bytes() == before
    assert not (clone.worktree / "run_001.h5").exists()


def test_explicit_format_bypasses_detection(tmp_path, pages, monkeypatch):
    def unexpected_detection(*args, **kwargs):
        raise AssertionError("Explicit format must override inference")

    monkeypatch.setattr("hallmark.catalog.detect_fmt", unexpected_detection)
    root = "https://test/data/"
    pages[0][root] = index("run_001.h5", "run_002.h5", "README.md")
    repo = Repo.init(tmp_path / "explicit", source=root, format="run_{run:03d}.h5")
    assert "run" in repo.state.data.columns
    assert "scan" not in repo.state.data.columns
    assert len(repo.state.data) == 3
    assert repo.state.config["data"][0]["extraction"] == {
        "automatic": False, "formats": ["run_{run:03d}.h5"]}


@pytest.mark.parametrize("patterns,expected", [
    (["run_001.h5", "README*"], ["README.md", "run_001.h5"]),
    ("*.absent", []),
])
def test_filter_precedes_automatic_detection(tmp_path, pages, patterns, expected):
    root = "https://test/data/"
    pages[0][root] = index("run_001.h5", "run_002.h5", "README.md")
    repo = Repo.init(tmp_path / "filtered", source=root, filter=patterns)
    assert repo.state.data["path"].tolist() == expected
    # A single run has no sibling from which to infer a varying field.
    assert repo.state.config["data"][0]["extraction"]["formats"] == []
    assert "scan" not in repo.state.data.columns
    assert repo.plan_download().file_count == len(expected)
    assert pages[1] == [root]


def test_inferred_reserved_fields_do_not_replace_metadata(tmp_path, pages):
    root = "https://test/data/"
    pages[0][root] = index("{path}_001.h5", "{path}_002.h5", "README.md")
    repo = Repo.init(tmp_path / "literal", source=root)
    assert set(repo.state.data["path"]) == {
        "{path}_001.h5", "{path}_002.h5", "README.md"}
    assert repo.state.config["data"][0]["extraction"]["formats"] == []
    assert repo.plan_download().file_count == 3


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_filter_and_format_workflow(tmp_path, pages, explicit):
    root = "https://test/data/"
    pages[0][root] = index("run_001.h5", "run_002.h5", "README.md", "omit.bin")
    destination = tmp_path / "cli"
    arguments = ["init", str(destination), "--from", root,
                 "--filter", "*.h5", "--filter", "README*"]
    if explicit:
        arguments += ["--format", "run_{run:03d}.h5"]
    result = CliRunner().invoke(hallmark, arguments)
    assert result.exit_code == 0, result.output
    repo = Repo(destination)
    assert set(repo.state.data["path"]) == {"run_001.h5", "run_002.h5", "README.md"}
    assert ("run" if explicit else "scan") in repo.state.data.columns
    assert pages[1] == [root]


@pytest.mark.parametrize("option", ["--include", "--extract", "--format", "--fmt"])
def test_download_rejects_removed_and_initialization_only_flags(
        tmp_path, monkeypatch, option):
    repo = Repo.init(tmp_path / "repo")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["download", option, "value"])
    assert result.exit_code == 2
    assert "No such option" in result.output
