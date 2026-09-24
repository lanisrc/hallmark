"""Named sources, template suggestions and URL templates with in-memory listings."""

from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from hallmark import DataSource, Repo, SourceRelease, register_source
from hallmark import sources
from hallmark.cli import hallmark
from hallmark.discovery import TemplateMatcher
from hallmark.repo_remote import suggest_templates
from hallmark.sources import backend_for_url, get_source, list_sources
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing


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
    assert "hallmark ls-remote URL" in result.output
    assert pages[1] == []


def test_collection_urls_from_sources_feed_add(tmp_path, pages):
    content, reads = pages
    zcatalog = "https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/zcatalog/"
    content[zcatalog] = index("zall-pix-iron.fits", "zall-tilecumulative-iron.fits",
                              "notes.txt", "v1/")
    repo = Repo.init(tmp_path / "desi")
    repo.add(zcatalog + "zall-{kind}-iron.fits")
    assert repo.state.data["kind"].tolist() == ["pix", "tilecumulative"]
    # the template has one segment, so subdirectories are never listed
    assert reads == [zcatalog]
    plan = repo.plan_download(filter="*pix*")
    assert plan.remote_url == zcatalog
    assert [item.relative_path.as_posix() for item in plan.items] == [
        "zall-pix-iron.fits"]


@pytest.mark.parametrize("template", [
    "{", "{path}", "{checksum}", "{sha1}", "{md5}", "{size_bytes}", "{mtime}",
    "{checksum_algorithm}", "{}", "{0}", "{a.b}", "{a[0]}", "{run!r}", "",
    "{run}/",
])
def test_invalid_url_templates_are_rejected_before_access(tmp_path, pages, template):
    repo = Repo.init(tmp_path / "repo")
    with pytest.raises(ValueError):
        repo.add("https://test/data/" + template)
    assert pages[1] == []


def test_remote_template_survives_reload_rename_and_clone(
        tmp_path, pages, monkeypatch):
    from mock_server import MockServer

    root = "https://test/data/"
    pages[0][root] = index("run_001.h5", "RUN_002.h5", "README.md", "omit.bin")
    repo = Repo.init(tmp_path / "repo")
    repo.add(root + "run_{run:03d}.h5")
    repo.commit("Catalog runs")
    before = (repo.dothm.path / "data.tsv").read_bytes()
    repo = Repo(repo.worktree)
    # templates are case-sensitive, and other files are not cataloged
    assert repo.state.data["run"].tolist() == ["001"]
    server = MockServer(root)
    server.add_file("run_001.h5", b"run one")
    monkeypatch.setattr("requests.Session", lambda: server)
    plan = repo.plan_download()
    assert repo.download(plan, approved=True)["succeeded"] == 1
    assert (repo.worktree / "run_001.h5").read_bytes() == b"run one"
    assert (repo.dothm.path / "data.tsv").read_bytes() == before
    # Configuring a mirror and cloning the catalog preserve the template source.
    repo.set_config(remote_name="archive", remote_url="https://mirror.test/data/")
    repo.commit("Add a mirror")
    clone = Repo.clone(str(repo.dothm.path), tmp_path / "copy")
    assert clone.state.config["data"] == repo.state.config["data"]
    assert clone.plan_download().remote_url == root
    assert clone.plan_download(remote_name="archive").remote_url == (
        "https://mirror.test/data/")
    assert not (clone.worktree / "run_001.h5").exists()


def test_source_registration_and_backend_lookup(registry):
    release = SourceRelease("https://test/", {"both": ["a", "a/b"], "one": "a"},
                            backend="cyverse")
    descriptor = DataSource("example", "Example source", {"v1": release})
    register_source(descriptor)
    assert backend_for_url("https://test/a/b/")[0] == "cyverse"
    assert backend_for_url("https://other.test/") is None
    with pytest.raises(ValueError, match="Duplicate"):
        register_source(descriptor)
    with pytest.raises(TypeError):
        descriptor.releases["v2"] = release


def test_add_inherits_the_backend_of_a_registered_release(tmp_path, pages, registry):
    root = "https://test/release/"
    register_source(DataSource("example", "Example source", {"v1": SourceRelease(
        root, {}, backend="cyverse")}))
    pages[0][root + "runs/"] = index("run_1.h5")
    repo = Repo.init(tmp_path / "repo")
    repo.add(root + "runs/run_{run}.h5")
    assert repo.state.config["data"][0]["backend"] == "cyverse"


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
    "--max-workers", "--with-download", "--from", "--release", "--collection",
    "--filter", "--format",
])
def test_init_removed_options_have_no_alias(tmp_path, pages, option):
    destination = tmp_path / "new"
    result = CliRunner().invoke(hallmark, ["init", str(destination), option, "x"])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert not destination.exists()
    assert pages[1] == []


@pytest.mark.parametrize("kwargs", [
    {"from_url": "https://test/"}, {"auth": "old"}, {"backend": "http"},
    {"include": "*"}, {"extract": "{name}"}, {"fmt": "{name}"}, {"download": True},
    {"source": "https://test/"}, {"release": "dr1"}, {"collections": ["a"]},
    {"filter": "*"}, {"format": "{name}"},
])
def test_init_removed_python_arguments_have_no_shim(tmp_path, kwargs):
    with pytest.raises(TypeError):
        Repo.init(tmp_path / "new", **kwargs)
    assert not (tmp_path / "new").exists()


def test_ls_remote_suggests_templates_from_filenames(tmp_path, pages):
    root = "https://test/data/"
    paths = ["run_001.h5", "run_002.h5", "sgra_20170406_nustar.fits",
             "sgra_20170411_chandra.fits", "README.md", "archive.tar.gz"]
    pages[0][root] = index(*paths)
    base, suggestions, total = suggest_templates(root)
    assert pages[1] == [root]  # Detection uses filenames, never payloads.
    assert (base, total) == (root, len(paths))
    assert suggestions == [(root + "run_{scan}.h5", 2),
                           (root + "sgra_{date}_{p0}.fits", 2)]
    repo = Repo.init(tmp_path / "repo")
    repo.add(suggestions[1][0])
    assert repo.state.data[["date", "p0"]].values.tolist() == [
        ["20170406", "nustar"], ["20170411", "chandra"]]


def test_ls_remote_skips_suggestions_with_reserved_or_literal_braces(pages):
    root = "https://test/data/"
    pages[0][root] = index("{path}_001.h5", "{path}_002.h5", "README.md")
    _, suggestions, total = suggest_templates(root)
    assert total == 3
    for template, _ in suggestions:
        TemplateMatcher(template[len(root):])


def test_cli_ls_remote_then_add_workflow(tmp_path, pages, monkeypatch):
    root = "https://test/data/"
    pages[0][root] = index("run_001.h5", "run_002.h5", "README.md", "omit.bin")
    runner = CliRunner()
    listing = runner.invoke(hallmark, ["ls-remote", root])
    assert listing.exit_code == 0, listing.output
    assert "4 file(s) at https://test/data/" in listing.output
    assert f"hallmark add '{root}run_{{scan}}.h5'  (2)" in listing.output
    assert runner.invoke(hallmark, ["init", str(tmp_path / "cli")]).exit_code == 0
    monkeypatch.chdir(tmp_path / "cli")
    preview = runner.invoke(hallmark, ["add", "-n", root + "run_{run:03d}.h5"])
    assert preview.exit_code == 0, preview.output
    assert preview.stdout.splitlines()[:3] == ["Would add", "run_001.h5",
                                               "run_002.h5"]
    assert not Repo(tmp_path / "cli").state.config["data"][0].get("fmt")
    added = runner.invoke(hallmark, ["add", root + "run_{run:03d}.h5"])
    assert added.exit_code == 0, added.output
    assert set(Repo(tmp_path / "cli").state.data["run"]) == {"001", "002"}


@pytest.mark.parametrize("option", ["--extract", "--format", "--fmt"])
def test_download_rejects_removed_and_initialization_only_flags(
        tmp_path, monkeypatch, option):
    repo = Repo.init(tmp_path / "repo")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["download", option, "value"])
    assert result.exit_code == 2
    assert "No such option" in result.output
