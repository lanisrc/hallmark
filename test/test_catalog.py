"""Test remote initialization and cloning without downloading dataset payloads."""

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


def test_remote_init_is_recursive_filtered_and_payload_free(
        tmp_path, metadata_server):
    pages, reads = metadata_server
    root = "https://example.test/data/"
    pages[root] = ('<h1>Index of /data/</h1><a href="root.h5">root.h5</a>'
                   '<a href="note.txt">note.txt</a><a href="sub/">sub/</a>')
    pages[root + "sub/"] = '<h1>Index of sub</h1><a href="data.h5">data.h5</a>'
    events = []
    repo = Repo.init(tmp_path / "clone", source=root, filter="**/*.h5",
                     progress=events.append)
    assert repo.state.data["path"].tolist() == ["root.h5", "sub/data.h5"]
    assert repo.state.config["remote"] == [
        {"name": "origin", "url": root, "backend": "http"}]
    assert repo.state.config["data"] == [{"db": "data.tsv", "extraction": {
        "automatic": True, "formats": []}}]
    assert not (repo.worktree / "root.h5").exists()
    assert not any(url.endswith((".h5", ".txt")) for url in reads)
    plan = repo.plan_download()
    assert plan.file_count == 2
    assert plan.unknown_size_count == 2
    assert events[-1]["directories"] == 2
    assert events[-1]["matched"] == 2
    assert events[-1]["files"] == 3


def test_remote_init_does_not_require_filename_inference(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/odd/"
    pages[root] = '<h1>Index of odd</h1><a href="odd%20name">odd name</a>'
    repo = Repo.init(tmp_path / "clone", source=root)
    assert repo.state.data["path"].tolist() == ["odd name"]
    assert repo.plan_download().file_count == 1


def test_empty_filtered_init_is_valid(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root] = '<h1>Index of data</h1><a href="notes.txt">notes.txt</a>'
    repo = Repo.init(tmp_path / "clone", source=root, format="run{run:d}.h5")
    assert repo.state.data["path"].tolist() == ["notes.txt"]
    assert "run" in repo.state.data.columns
    assert repo.state.data.iloc[0]["run"] == ""
    assert repo.plan_download().file_count == 1


def test_template_enriches_without_filtering_inventory(tmp_path, metadata_server):
    pages, _ = metadata_server
    root = "https://example.test/data/"
    pages[root] = ('<h1>Index of data</h1><a href="run2.h5">run2.h5</a>'
                   '<a href="notes.txt">notes.txt</a>')
    repo = Repo.init(tmp_path / "clone", source=root, format="run{run:d}.h5")
    assert repo.state.data["path"].tolist() == ["notes.txt", "run2.h5"]
    assert repo.state.data.iloc[0]["run"] == ""
    assert float(repo.state.data.iloc[1]["run"]) == 2
    assert repo.state.config["data"] == [
        {"db": "data.tsv", "extraction": {
            "automatic": False, "formats": ["run{run:d}.h5"]}}]


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
    plans = []
    repo = Repo.clone(root, tmp_path / "clone")
    plans.append(repo.plan_download(filter="**/*.h5"))
    assert repo.state.data["path"].tolist() == ["a.h5", "b.txt"]
    assert plans[0].total_bytes == 12
    assert repo.plan_download().remote_url == "https://mirror.test/data/"
    assert not any(url.endswith("a.h5") for url in reads)
    assert repo.dothm.head.commit.message.strip() == "Import published catalog snapshot"


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
    plans = []
    repo = Repo.clone(str(source.dothm.path), tmp_path / "clone")
    plans.append(repo.plan_download(filter="run1.h5"))
    assert repo.dothm.head.commit.hexsha == head
    assert repo.dothm.index.diff("HEAD") == []
    assert len(repo.state.data) == 2
    assert plans[0].items[0].relative_path == Path("run1.h5")
    assert plans[0].file_count == 1
    assert not repo.objects.root.exists()
    assert not (repo.worktree / "run1.h5").exists()
    assert repo.dothm.remotes.origin.url == str(source.dothm.path)


def test_clone_requires_callback_before_download_intent(tmp_path, metadata_server):
    _, reads = metadata_server
    with pytest.raises(TypeError, match="download"):
        Repo.clone("https://example.test/data/", tmp_path / "clone", download=True)
    assert reads == []
    assert not (tmp_path / "clone").exists()


def test_init_refusal_keeps_catalog_without_payload(
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
    repo = Repo.init(tmp_path / "clone", source=root)
    decline(repo.plan_download())
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
        Repo.init(tmp_path / "clone", source="https://example.test/data/")
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
        "data:\n- db: data\n  fmt: 'item_{i}.dat'\n"
        "remote:\n- name: origin\n  url: https://data.test/files/\n")
    pages[root + "meta.yml"] = "{}\n"
    pages[root + "data.tsv"] = "i\n1\n2\n"
    plans = []
    repo = Repo.clone(root, tmp_path / "clone")
    plans.append(repo.plan_download(filter="item_1.dat"))
    assert repo.state.data["i"].astype(str).tolist() == ["1", "2"]
    assert [item.relative_path for item in plans[0].items] == [Path("item_1.dat")]


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


@pytest.mark.parametrize("failure", [KeyboardInterrupt, DownloadError])
def test_remote_init_preserves_existing_directory_on_discovery_failure(
        tmp_path, metadata_server, monkeypatch, failure):
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep.h5").write_bytes(b"existing scientific data")

    def fail(*args, **kwargs):
        raise failure("interrupted")

    monkeypatch.setattr("hallmark.catalog.discover", fail)
    with pytest.raises(failure):
        Repo.init(destination, source="https://example.test/data/")
    assert list(destination.iterdir()) == [destination / "keep.h5"]
    assert (destination / "keep.h5").read_bytes() == b"existing scientific data"


def test_remote_init_preserves_existing_payload_and_rejects_existing_catalog(
        tmp_path, metadata_server):
    pages, reads = metadata_server
    root = "https://example.test/data/"
    pages[root] = '<h1>Index of data</h1><a href="keep.h5">keep.h5</a>'
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep.h5").write_bytes(b"local")
    repo = Repo.init(destination, source=root)
    assert repo.state.data["path"].tolist() == ["keep.h5"]
    assert (destination / "keep.h5").read_bytes() == b"local"
    head = repo.dothm.head.commit.hexsha
    reads.clear()
    with pytest.raises(DestinationExistsError):
        Repo.init(destination, source=root)
    assert reads == []
    assert Repo(destination).dothm.head.commit.hexsha == head


def test_remote_init_rejects_bare_symlink_before_access(tmp_path, metadata_server):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("keep")
    alias = tmp_path / "alias.hm"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(DestinationExistsError):
        Repo.init(alias, source="https://example.test/data/")
    assert metadata_server[1] == []
    assert list(target.iterdir()) == [target / "keep"]


def test_remote_init_cleans_only_created_metadata_on_write_failure(
        tmp_path, metadata_server, monkeypatch):
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep").write_text("keep")
    root = "https://example.test/data/"
    metadata_server[0][root] = '<h1>Index of data</h1>'

    def fail(*args, **kwargs):
        raise OSError("cannot write catalog")

    monkeypatch.setattr("hallmark.catalog._write_inventory", fail)
    with pytest.raises(OSError, match="cannot write"):
        Repo.init(destination, source=root)
    assert list(destination.iterdir()) == [destination / "keep"]


@pytest.mark.parametrize("kwargs", [
    {"filter": "*.fits"}, {"fmt": "{name}.fits"}, {"source_type": "directory"},
    {"download": True, "approve": lambda plan: True, "fmt": "{name:invalid}"},
])
def test_clone_invalid_selection_fails_before_source_access(
        tmp_path, metadata_server, kwargs):
    with pytest.raises((TypeError, ValueError)):
        Repo.clone("https://example.test/data/", tmp_path / "clone", **kwargs)
    assert metadata_server[1] == []
    assert not (tmp_path / "clone").exists()


def test_raw_directory_clone_fails_with_init_guidance(
        tmp_path, metadata_server, monkeypatch):
    pages, reads = metadata_server
    root = "https://example.test/data/"
    pages[root] = '<h1>Index of data</h1><a href="a.h5">a.h5</a>'

    def fail(*args, **kwargs):
        raise CloneError("not a Git repository")

    monkeypatch.setattr("hallmark.catalog.Dothm.clone", fail)
    with pytest.raises(CloneError, match="hallmark init PATH --from URL"):
        Repo.clone(root, tmp_path / "clone")
    assert root not in reads
    assert not (tmp_path / "clone").exists()


def test_suffixless_http_git_source_falls_back_after_absent_snapshots(
        tmp_path, metadata_server, monkeypatch):
    source = Repo.init(tmp_path / "source")
    source.dothm.index.commit("Existing complete catalog")
    original = type(source.dothm).clone
    calls = []

    def clone(url, destination, **kwargs):
        calls.append(url)
        return original(str(source.dothm.path), destination, **kwargs)

    monkeypatch.setattr("hallmark.catalog.Dothm.clone", clone)
    url = "https://git.example.test/group/dataset"
    repo = Repo.clone(url, tmp_path / "clone")
    assert calls == [url]
    assert metadata_server[1] == [url + "/config.yml", url + "/.hm/config.yml"]
    assert repo.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha


@pytest.mark.parametrize("url", [
    "ssh://git.example.test/dataset", "git@example.test:group/dataset",
    "example.test:group/dataset", "https://example.test/dataset.git",
])
def test_git_sources_do_not_probe_snapshot_metadata(tmp_path, metadata_server,
                                                   monkeypatch, url):
    calls = []

    def fail(source, *args, **kwargs):
        calls.append(source)
        raise CloneError("test Git failure")

    monkeypatch.setattr("hallmark.catalog.Dothm.clone", fail)
    with pytest.raises(CloneError, match="test Git failure"):
        Repo.clone(url, tmp_path / "clone")
    assert calls == [url]
    assert metadata_server[1] == []


@pytest.mark.parametrize("config,meta,table", [
    ("data: [", "{}", "path\n"),
    ("[]", "{}", "path\n"),
    ("unrelated: config", "{}", "path\n"),
    ("data: []", None, "path\n"),
    ("data: []", "{}", None),
])
def test_malformed_snapshot_never_falls_back_to_git(
        tmp_path, metadata_server, monkeypatch, config, meta, table):
    root = "https://example.test/catalog/"
    for name, content in [("config.yml", config), ("meta.yml", meta),
                          ("data.tsv", table)]:
        if content is not None:
            metadata_server[0][root + name] = content

    def fail(*args, **kwargs):
        raise AssertionError("Malformed metadata must not trigger Git fallback")

    monkeypatch.setattr("hallmark.catalog.Dothm.clone", fail)
    with pytest.raises(CloneError):
        Repo.clone(root, tmp_path / "clone")
    assert not (tmp_path / "clone").exists()


def test_snapshot_clone_does_not_require_payload_authentication(
        tmp_path, metadata_server, monkeypatch):
    root = "https://catalog.example.test/published/"
    remote = {"name": "origin", "url": "ssh://private.example.test/science/",
              "backend": "ssh",
              "backend_options": {"collection": ["release-1"]}}
    metadata_server[0].update({
        root + "config.yml": yaml.safe_dump({"data": [{"db": "data.tsv"}],
                                              "remote": [remote]}),
        root + "meta.yml": "{}\n", root + "data.tsv": "path\na.fits\n"})
    repo = Repo.clone(root, tmp_path / "clone")
    assert repo.state.config["remote"] == [remote]
    assert all(url.startswith(root) for url in metadata_server[1])
    assert repo.plan_download().remote_backend == "ssh"
    assert not repo.dothm.remotes


def test_init_download_requires_callback_before_source_access(
        tmp_path, metadata_server):
    with pytest.raises(TypeError, match="download"):
        Repo.init(tmp_path / "new", source="https://example.test/data/",
                  download=True)
    assert metadata_server[1] == []


def test_empty_init_download_does_not_request_approval(tmp_path, metadata_server):
    root = "https://example.test/empty/"
    metadata_server[0][root] = '<h1>Index of empty</h1>'

    def fail(plan):
        raise AssertionError("Empty plans require no confirmation")

    repo = Repo.init(tmp_path / "new", source=root)
    assert repo.plan_download().file_count == 0
    assert repo.state.data.empty
    assert repo.download_result is None


def test_init_downloads_only_after_approved_catalog_creation(
        tmp_path, metadata_server, monkeypatch):
    import requests
    from mock_server import MockServer

    root = "https://example.test/data/"
    metadata_server[0][root] = '<h1>Index of data</h1><a href="a.fits">a.fits</a>'
    server = MockServer(root)
    server.add_file("a.fits", b"fits")
    monkeypatch.setattr(requests, "Session", lambda: server)
    destination = tmp_path / "new"
    plans = []

    def approve(plan):
        assert Repo(destination).state.data["path"].tolist() == ["a.fits"]
        assert not (destination / "a.fits").exists()
        plans.append(plan)
        return True

    repo = Repo.init(destination, source=root)
    plan = repo.plan_download()
    assert approve(plan)
    repo.download(plan, approved=True)
    assert len(plans) == 1
    assert (destination / "a.fits").read_bytes() == b"fits"
    assert repo.download_result["succeeded"] == 1


def test_snapshot_access_failure_never_falls_back_to_git(tmp_path, monkeypatch):
    def inaccessible(context, path):
        raise DownloadError("Metadata server requires authentication")

    def fail(*args, **kwargs):
        raise AssertionError("Access failure must not trigger Git fallback")

    monkeypatch.setattr(OperationContext, "read_text", inaccessible)
    monkeypatch.setattr("hallmark.catalog.Dothm.clone", fail)
    with pytest.raises(DownloadError, match="requires authentication"):
        Repo.clone("https://catalog.example.test/data/", tmp_path / "clone")
    assert not (tmp_path / "clone").exists()


def test_snapshot_download_uses_independent_payload_server(
        tmp_path, metadata_server, monkeypatch):
    import requests
    from mock_server import MockServer

    catalog_url = "https://catalog.example.test/published/"
    payload_url = "https://data.example.test/science/"
    metadata_server[0].update({
        catalog_url + "config.yml": yaml.safe_dump({
            "data": [{"db": "data.tsv"}],
            "remote": [{"name": "origin", "url": payload_url}]}),
        catalog_url + "meta.yml": "{}\n",
        catalog_url + "data.tsv": "path\tsize_bytes\na.fits\t4\n"})
    server = MockServer(payload_url)
    server.add_file("a.fits", b"fits")
    requests_made = []
    original_get = server.get

    def get(url, **kwargs):
        requests_made.append(url)
        return original_get(url, **kwargs)

    server.get = get
    monkeypatch.setattr(requests, "Session", lambda: server)
    repo = Repo.clone(catalog_url, tmp_path / "clone")
    plan = repo.plan_download()
    assert plan.remote_url == payload_url
    repo.download(plan, approved=True)
    assert requests_made == [payload_url + "a.fits"]
    assert all(url.startswith(catalog_url) for url in metadata_server[1])
    assert (repo.worktree / "a.fits").read_bytes() == b"fits"
    assert repo.download_result["succeeded"] == 1
