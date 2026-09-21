"""Test catalog cloning without downloading dataset files or stored objects."""

from pathlib import Path

import pytest
import yaml

from hallmark import Repo
from hallmark.error import CloneError, DestinationExistsError
from hallmark.transport import OperationContext
from hallmark.transport.base import DownloadError, RemoteObjectMissing


@pytest.fixture
def metadata_server(monkeypatch):
    """Serve registered metadata and record each requested URL."""
    pages, reads = {}, []

    def read_text(context, path):
        url = context.remote.url.rstrip("/") + "/" + path
        reads.append(url)
        if url not in pages:
            raise RemoteObjectMissing("No such metadata object")
        return pages[url]

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    return pages, reads


def test_directory_clone_is_recursive_filtered_and_payload_free(
        tmp_path, metadata_server):
    pages, reads = metadata_server
    root = "https://example.test/data/"
    pages[root] = ('<h1>Index of /data/</h1><a href="root.h5">root.h5</a>'
                   '<a href="note.txt">note.txt</a><a href="sub/">sub/</a>')
    pages[root + "sub/"] = '<h1>Index of sub</h1><a href="data.h5">data.h5</a>'
    events = []
    repo = Repo.clone(root, tmp_path / "clone", filter="**/*.h5",
                      progress=events.append)
    assert repo.state.data["path"].tolist() == ["root.h5", "sub/data.h5"]
    assert repo.state.config["remote"] == [{"name": "origin", "url": root}]
    assert repo.state.config["data"] == [{"db": "data.tsv"}]
    assert not (repo.worktree / "root.h5").exists()
    assert not any(url.endswith((".h5", ".txt")) for url in reads)
    plan = repo.plan_download()
    assert plan.file_count == 2
    assert plan.unknown_size_count == 2
    assert events[-1]["directories"] == 2


def test_url_only_clone_does_not_require_filename_inference(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/odd/"
    pages[root] = '<h1>Index of odd</h1><a href="odd%20name">odd name</a>'
    repo = Repo.clone(root, tmp_path / "clone")
    assert repo.state.data["path"].tolist() == ["odd name"]
    assert repo.plan_download().file_count == 1


def test_empty_filtered_clone_is_valid(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root] = '<h1>Index of data</h1><a href="notes.txt">notes.txt</a>'
    repo = Repo.clone(root, tmp_path / "clone", fmt="run{run:d}.h5")
    assert repo.state.data.empty
    assert repo.plan_download().file_count == 0


def test_template_enriches_and_filters_inventory(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root] = ('<h1>Index of data</h1><a href="run2.h5">run2.h5</a>'
                   '<a href="notes.txt">notes.txt</a>')
    repo = Repo.clone(root, tmp_path / "clone", fmt="run{run:d}.h5")
    assert repo.state.data["path"].tolist() == ["run2.h5"]
    assert str(repo.state.data.iloc[0]["run"]) == "2"


@pytest.mark.parametrize("nested", [False, True])
def test_published_snapshot_preserves_data_remote(tmp_path, metadata_server, nested):
    pages, reads = metadata_server
    root = "https://example.test/published/"
    prefix = root + (".hm/" if nested else "")
    pages[prefix + "config.yml"] = yaml.safe_dump({
        "data": [{"db": "data.tsv"}],
        "remote": [{"name": "origin", "url": "https://mirror.test/data/"}]})
    pages[prefix + "meta.yml"] = "dataset: example\n"
    pages[prefix + "data.tsv"] = "path\tsize_bytes\na.h5\t12\nb.txt\t20\n"
    repo = Repo.clone(root, tmp_path / "clone", filter="**/*.h5")
    assert repo.state.data["path"].tolist() == ["a.h5"]
    assert repo.plan_download().total_bytes == 12
    assert repo.plan_download().remote_url == "https://mirror.test/data/"
    assert not any(url.endswith("a.h5") for url in reads)
    assert repo.dothm.head.commit.message.strip() == "Filter local catalog"


def test_filtered_git_clone_preserves_history_without_objects(tmp_path, monkeypatch):
    source = Repo.init(tmp_path / "source")
    (source.worktree / "run1.h5").write_text("one")
    (source.worktree / "run2.h5").write_text("two")
    source.add("run{run:d}.h5")
    source.set_config(remote_url="https://example.test/data/")
    source.commit("Original scientific data")
    head = source.dothm.head.commit.hexsha
    assert list(source.objects.root.rglob("*"))

    def fail(*args, **kwargs):
        raise AssertionError("Filtered metadata clone must not transfer data")

    monkeypatch.setattr("hallmark.downloader._fetch_file", fail)
    repo = Repo.clone(str(source.dothm.path), tmp_path / "clone", filter="run1.h5")
    assert repo.dothm.head.commit.parents[0].hexsha == head
    assert len(repo.state.data) == 1
    assert repo.plan_download().items[0].relative_path == Path("run1.h5")
    assert not repo.objects.root.exists()
    assert not (repo.worktree / "run1.h5").exists()
    assert repo.dothm.remotes.origin.url == str(source.dothm.path)


def test_clone_requires_callback_before_download_intent(tmp_path, metadata_server):
    _, reads = metadata_server
    with pytest.raises(DownloadError, match="approve"):
        Repo.clone("https://example.test/data/", tmp_path / "clone", download=True)
    assert reads == []
    assert not (tmp_path / "clone").exists()


def test_clone_refusal_keeps_catalog_without_payload(
        tmp_path, metadata_server, monkeypatch):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root] = '<h1>Index of data</h1><a href="a.h5">a.h5</a>'
    plans = []

    def decline(plan):
        plans.append(plan)
        return False

    def fail(*args, **kwargs):
        raise AssertionError("Refused transfer must not start")

    monkeypatch.setattr(Repo, "download", fail)
    repo = Repo.clone(root, tmp_path / "clone", download=True, approve=decline)
    assert len(plans) == 1
    assert plans[0].file_count == 1
    assert repo.download_result is None
    assert not (repo.worktree / "a.h5").exists()


def test_interrupted_discovery_removes_incomplete_destination(
        tmp_path, metadata_server, monkeypatch):
    def fail(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr("hallmark.catalog.discover", fail)
    with pytest.raises(KeyboardInterrupt):
        Repo.clone("https://example.test/data/", tmp_path / "clone",
                   source_type="directory")
    assert not (tmp_path / "clone").exists()


def test_existing_destination_is_never_changed(tmp_path, metadata_server):
    destination = tmp_path / "clone"
    destination.mkdir()
    (destination / "keep").write_text("keep")
    with pytest.raises(DestinationExistsError):
        Repo.clone("https://example.test/data/", destination)
    assert (destination / "keep").read_text() == "keep"
    assert metadata_server[1] == []


def test_missing_snapshot_does_not_publish_empty_success(tmp_path, metadata_server):
    with pytest.raises(CloneError, match="No published"):
        Repo.clone("https://example.test/data/", tmp_path / "clone",
                   source_type="catalog")
    assert not (tmp_path / "clone").exists()


def test_snapshot_rejects_external_catalog_paths(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root + "config.yml"] = "data:\n- db: ../escape.tsv\n"
    pages[root + "meta.yml"] = "{}\n"
    pages[root + "data.tsv"] = "path\n"
    with pytest.raises(ValueError):
        Repo.clone(root, tmp_path / "clone")
    assert not (tmp_path / "clone").exists()


def test_snapshot_filter_normalizes_legacy_db_names(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root + "config.yml"] = (
        "data:\n- db: data\n  fmt: 'item_{i}.dat'\n")
    pages[root + "meta.yml"] = "{}\n"
    pages[root + "data.tsv"] = "i\n1\n2\n"
    repo = Repo.clone(root, tmp_path / "clone", filter="item_1.dat")
    assert repo.state.data["i"].astype(str).tolist() == ["1"]


@pytest.mark.parametrize("table", [
    "<html>Login required</html>\n", "", "path\n../escape\n",
    "path\n.hm/config.yml\n", "unrelated\nrecord\n",
])
def test_snapshot_validates_tables_without_filter(tmp_path, metadata_server, table):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root + "config.yml"] = "data:\n- db: data.tsv\n"
    pages[root + "meta.yml"] = "{}\n"
    pages[root + "data.tsv"] = table
    with pytest.raises((CloneError, ValueError)):
        Repo.clone(root, tmp_path / "clone")
    assert not (tmp_path / "clone").exists()
