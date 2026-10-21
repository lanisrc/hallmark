"""Edge cases of plans, discovery, sources, adds, clones and the object store."""

import errno
import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from hallmark import Repo
from hallmark.error import CloneError
from hallmark.remote.discovery import discover_remote_files, path_matches
from hallmark.remote.plan import DownloadItem, DownloadPlan
from hallmark.remote.sources import (
    SourceSpec, match_remote, materialize_sources, normalize_sources, rows_of)
from hallmark.repo import objects as objects_module
from hallmark.repo.objects import Objects
from hallmark.transport import OperationContext
from hallmark.transport.base import (
    RemoteEntry, RemoteObjectMissing, RemoteSpec)

ROOT = "https://example.test/data/"


def sha1_of(data):
    return hashlib.sha1(data).hexdigest()


def listing(*names):
    return "<h1>Index of data</h1>" + "".join(
        f'<a href="{name}">{name}</a>' for name in names)


@pytest.fixture
def pages(monkeypatch):
    """Serve pages by full URL and record each one read."""
    served, reads = {}, []

    def read_text(context, path, **_):
        url = context.remote.url.rstrip("/") + "/" + path
        reads.append(url)
        if url not in served:
            raise RemoteObjectMissing("No such metadata object")
        return served[url]

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    return served, reads


# Download plans


@pytest.mark.parametrize("fields, message", [
    ({"remote": 3}, "remote must be a remote name"),
    ({"checksum": 5}, "checksum must be a string or algorithm/value pair"),
    ({"checksum": ("sha256",)}, "checksum must be a string"),
    ({"checksum": ("sha256", 5)}, "checksum must be a string"),
    ({"size_bytes": -1}, "size_bytes must be a nonnegative integer"),
    ({"size_bytes": True}, "size_bytes must be a nonnegative integer"),
    ({"size_bytes": "5"}, "size_bytes must be a nonnegative integer"),
    ({"mtime": 1750000000}, "mtime must be a string or None"),
])
def test_download_items_refuse_malformed_metadata(fields, message):
    with pytest.raises(ValueError, match=message):
        DownloadItem(Path("a.fits"), **fields)


def test_download_items_accept_lists_and_uppercase_digests():
    item = DownloadItem("a.fits", ["sha256", "a" * 64], sha1="A" * 40)
    assert item.checksum == ("sha256", "a" * 64)
    assert item.sha1 == "a" * 40
    assert item.source_path == Path("a.fits")


def test_download_plans_hold_only_download_items(tmp_path):
    with pytest.raises(TypeError, match="items must contain DownloadItem values"):
        DownloadPlan(["a.fits"], ROOT, tmp_path)
    with pytest.raises(TypeError, match="items must contain DownloadItem values"):
        DownloadPlan((), ROOT, tmp_path, skipped_items=[Path("a.fits")])


def test_files_placed_from_the_store_must_name_their_object(tmp_path):
    with pytest.raises(ValueError, match="Cached items must record their SHA-1"):
        DownloadPlan((), ROOT, tmp_path, stored_items=[DownloadItem("a.fits")])
    plan = DownloadPlan((), ROOT, tmp_path,
                        stored_items=[DownloadItem("a.fits", sha1="1" * 40)])
    assert plan.file_count == 1 and plan.has_changes


@pytest.mark.parametrize("fields", [
    {"remote_url": b"https://example.test/"}, {"remote_auth": 7},
    {"remote_name": ["origin"]}, {"remote_backend": 1},
])
def test_plan_remote_fields_must_be_text(tmp_path, fields):
    values = {"remote_url": ROOT, **fields}
    with pytest.raises(TypeError, match="remote fields must be strings or None"):
        DownloadPlan((), values.pop("remote_url"), tmp_path, **values)


# Discovery


def test_every_filter_must_be_a_glob_string():
    assert path_matches("a.fits", filter=("*.txt", "*.fits"))
    with pytest.raises(ValueError, match="Path filters must be glob strings"):
        path_matches("a.fits", filter=["*.fits", 3])


class Listing:
    """A source whose transport yields fixed entries and whose files are text."""

    def __init__(self, entries, texts=None):
        self.remote = RemoteSpec.from_url("sftp://lab/srv/data/")
        self.texts = texts or {}
        self.reads = []

        def iter_entries(on_directory):
            on_directory("")
            yield from entries

        def server_checksums(paths, algorithm):
            raise AssertionError("no file needs a server checksum")

        self.transport = SimpleNamespace(
            iter_entries=iter_entries, prepare=lambda: None,
            complete_entries=lambda found: found,
            server_checksums=server_checksums)

    def read_text(self, path, **_):
        self.reads.append(path)
        if path not in self.texts:
            raise RemoteObjectMissing("No such file")
        return self.texts[path]

    def check_cancelled(self):
        pass


def test_an_unsupported_server_checksum_is_refused_before_listing():
    source = Listing([RemoteEntry(path="a.fits")])
    with pytest.raises(ValueError, match="Unsupported server checksum 'crc32'"):
        discover_remote_files(source, server_checksum="crc32")
    assert source.reads == []


def test_unusable_manifest_lines_are_ignored():
    names = ["a.fits", "b.fits", "c.fits", "checksums.txt", "SHA256SUMS", "MD5SUMS"]
    source = Listing([RemoteEntry(path=name) for name in names], {
        # A digest of no known length cannot be verified.
        "checksums.txt": "# checksums\n\n" + "a" * 10 + "  a.fits\n",
        # A digest too short for the manifest's algorithm is no SHA-256.
        "SHA256SUMS": "b" * 40 + "  b.fits\n" + "c" * 64 + "  ././c.fits\n",
    })
    entries = {entry.path: entry for entry in discover_remote_files(source)}
    assert entries["a.fits"].checksum is None
    assert entries["b.fits"].checksum is None
    assert (entries["c.fits"].checksum_algorithm, entries["c.fits"].checksum) == (
        "sha256", "c" * 64)
    # MD5SUMS was listed but is gone; discovery goes on without it.
    assert sorted(source.reads) == ["MD5SUMS", "SHA256SUMS", "checksums.txt"]


def test_files_with_a_published_checksum_are_not_hashed_by_the_server():
    entry = RemoteEntry(path="a.fits", checksum_algorithm="sha256",
                        checksum="a" * 64, checksum_source="published")
    assert discover_remote_files(Listing([entry]), server_checksum="md5") == [entry]


def test_a_file_listed_twice_is_counted_once():
    snapshots = []
    entries = discover_remote_files(
        Listing([RemoteEntry(path="a.fits", size=1),
                 RemoteEntry(path="a.fits", size=2)]),
        progress=snapshots.append)
    assert entries == [RemoteEntry(path="a.fits", size=1)]
    assert snapshots[-1]["files"] == 1 and snapshots[-1]["matched"] == 1


# Sources


def test_a_source_folder_must_be_text():
    with pytest.raises(ValueError, match="Source root must be a relative folder"):
        SourceSpec("lab", "origin", root=5)
    with pytest.raises(ValueError, match="Source prefix must be a relative folder"):
        SourceSpec("lab", "origin", prefix=["mirror"])


def test_dot_segments_name_the_root_folder():
    assert SourceSpec("lab", "origin", root="./.").root == ""
    assert SourceSpec("lab", "origin", prefix="././").prefix == ""
    assert SourceSpec("lab", "origin", root="./raw/").root == "raw/"


@pytest.mark.parametrize("folder", [".", "./"])
def test_a_lone_dot_names_the_root_folder(folder):
    assert SourceSpec("lab", "origin", prefix=folder).prefix == ""
    assert SourceSpec("lab", "origin", root=folder).root == ""
    with pytest.raises(ValueError):
        SourceSpec("lab", "origin", root="/")


def test_each_configured_source_must_be_a_mapping():
    with pytest.raises(ValueError, match="Each source must be a mapping"):
        SourceSpec.from_config("lab")
    with pytest.raises(ValueError, match="Each source must be a mapping"):
        normalize_sources(["lab"])


def test_the_deepest_remote_holding_a_folder_is_chosen():
    remotes = [{"name": "unset"},
               {"name": "deep", "url": ROOT + "release/"},
               {"name": "top", "url": ROOT}]
    folder = RemoteSpec.from_url(ROOT + "release/v1/")
    assert match_remote(folder, remotes) == ("deep", "v1/")
    assert match_remote(folder, remotes[::-1]) == ("deep", "v1/")
    assert match_remote(RemoteSpec.from_url("https://other.test/"), remotes) is None


def test_materializing_a_legacy_source_leaves_empty_tables_alone():
    state = SimpleNamespace(
        config={"data": [{"db": "data.tsv"}], "remote": {"url": ROOT}},
        data=pd.DataFrame({"path": ["a.fits"]}),
        tables={"empty.tsv": pd.DataFrame(),
                "more.tsv": pd.DataFrame({"path": ["b.fits"]})})
    sources = materialize_sources(state)
    assert [source.name for source in sources] == ["origin"]
    assert state.config["remote"] == [{"url": ROOT, "name": "origin"}]
    assert state.config["sources"] == [{"name": "origin", "remote": "origin"}]
    assert state.data["catalog_source"].tolist() == ["origin"]
    assert state.tables["more.tsv"]["catalog_source"].tolist() == ["origin"]
    assert state.tables["empty.tsv"].columns.tolist() == []


def test_rows_without_a_source_belong_to_a_lone_source():
    frame = pd.DataFrame({"path": ["a", "b", "c"],
                          "catalog_source": ["origin", None, "other"]})
    origin = SourceSpec("origin", "origin")
    other = SourceSpec("other", "origin", root="other/")
    assert rows_of(frame, origin, [origin]).tolist() == [True, True, False]
    assert rows_of(frame, origin, [origin, other]).tolist() == [True, False, False]


# Adding remote folders


def test_an_empty_folder_is_not_catalogued(tmp_path, pages):
    served, _ = pages
    served[ROOT] = "<title>Index of /data/</title>"
    repo = Repo.init(tmp_path / "data")
    with pytest.raises(ValueError, match=(
            f"No remote files matched any file under {ROOT}; "
            "previous catalogue kept")):
        repo.add(ROOT)
    assert repo.state.data.empty and "sources" not in repo.state.config


def test_a_named_remote_serves_a_folder_inside_it(tmp_path, pages):
    served, _ = pages
    served[ROOT] = listing("a.fits")
    served[ROOT + "raw/"] = listing("b.fits")
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT)
    repo.add(ROOT + "raw/", remote="origin")
    assert repo.state.config["remote"] == [
        {"name": "origin", "url": ROOT, "backend": "http"}]
    assert repo.state.config["sources"][1] == {
        "name": "origin-raw", "remote": "origin", "root": "raw/"}
    assert repo.state.data["path"].tolist() == ["a.fits", "raw/b.fits"]


def test_a_source_name_cannot_move_to_another_folder(tmp_path, pages):
    served, _ = pages
    served[ROOT] = listing("a.fits")
    served[ROOT + "raw/"] = listing("b.fits")
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, source="science")
    before = (repo.dothm.path / "data.tsv").read_bytes()
    with pytest.raises(ValueError,
                       match="Source 'science' catalogs another folder"):
        repo.add(ROOT + "raw/", source="science")
    assert (repo.dothm.path / "data.tsv").read_bytes() == before


def test_a_prefixed_source_clashing_with_another_is_told_to_change_prefix(
        tmp_path, pages):
    served, _ = pages
    served["https://one.test/data/"] = listing("a.fits")
    served["https://two.test/data/"] = listing("a.fits")
    repo = Repo.init(tmp_path / "data")
    repo.add("https://one.test/data/", prefix="mirror")
    with pytest.raises(ValueError) as error:
        repo.add("https://two.test/data/", prefix="mirror")
    message = str(error.value)
    assert "mirror/a.fits (cannot compare; source 'origin')" in message
    assert message.endswith(
        "Choose a different --prefix, or narrow it with --filter, or leave them "
        "out with --skip-duplicates; previous catalogue kept.")


def test_pruning_the_last_meta_file_removes_the_meta_section(tmp_path, pages):
    served, _ = pages
    served[ROOT] = listing("m87.fits", "meta.yml")
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT + "{name}.fits")
    assert repo.state.config["meta"] == [{"file": "meta.yml"}]
    repo.commit("Catalogue")
    served[ROOT] = listing("m87.fits")
    # The same format again needs no answer about sorting.
    result = repo.add(ROOT + "{name}.fits", prune=True)
    assert result.attrs["removed"] == ["meta.yml"]
    assert "meta" not in repo.state.config
    assert [entry.get("fmt") for entry in repo.state.config["data"]] == [
        "{name}.fits"]
    assert repo.state.data["path"].tolist() == ["m87.fits"]


def test_a_hand_written_file_entry_is_kept_when_every_source_is_sorted(
        tmp_path, pages):
    served, _ = pages
    served[ROOT] = listing("M87_001.fits", "M87_002.fits")
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT + "{src}_{day:03d}.fits", filter="M87_001*")
    repo.add(ROOT, filter="M87_002*")
    # Someone lists a file by hand, naming no source.
    repo.state.config["data"].append({"file": "README.md"})
    repo.dothm.save_state(repo.state)
    repo.add(ROOT, filter="M87_002*", remote_fmt="M87_{day:03d}.fits",
             resort="all")
    assert {"file": "README.md"} in repo.state.config["data"]
    assert repo.state.tables["M87_{day:03d}.fits.tsv"]["path"].tolist() == [
        "M87_001.fits", "M87_002.fits"]


def test_an_unknown_prune_choice_is_refused_before_scanning(tmp_path, pages):
    repo = Repo.init(tmp_path / "data")
    with pytest.raises(ValueError, match="prune must be True, False or a callable"):
        repo.add(ROOT, prune="yes")
    assert pages[1] == []


@pytest.mark.parametrize("data", ["data.tsv", ["data.tsv"]])
def test_a_malformed_data_section_is_refused_before_scanning(tmp_path, pages,
                                                             data):
    repo = Repo.init(tmp_path / "data")
    repo.state.config["data"] = data
    with pytest.raises(ValueError, match="Catalog data must be a mapping"):
        repo.add(ROOT)
    assert pages[1] == []


# Cloning published catalogues


def publish(served, root, config, meta="{}\n", tables=None):
    served[root + "config.yml"] = (config if isinstance(config, str)
                                   else yaml.safe_dump(config))
    served[root + "meta.yml"] = meta
    for name, text in (tables or {"data.tsv": "path\n"}).items():
        served[root + name] = text


REMOTE = [{"name": "origin", "url": "https://data.test/files/"}]


def test_a_snapshot_whose_data_section_is_not_a_mapping_is_refused(tmp_path,
                                                                    pages):
    publish(pages[0], ROOT, "data: 5\n")
    with pytest.raises(CloneError, match="Catalog data must be a mapping"):
        Repo.clone(ROOT, tmp_path / "clone", download=False)
    assert not (tmp_path / "clone").exists()


def test_a_snapshot_file_entry_must_be_a_safe_path(tmp_path, pages):
    publish(pages[0], ROOT, {"data": [{"file": "../escape.txt"}],
                             "remote": REMOTE})
    with pytest.raises(ValueError, match="safe relative path"):
        Repo.clone(ROOT, tmp_path / "clone", download=False)
    assert not (tmp_path / "clone").exists()


def test_a_snapshot_with_several_tables_is_cloned_whole(tmp_path, pages):
    served, reads = pages
    publish(served, ROOT, {
        "data": [{"file": "README.md"},
                 {"db": "science.tsv", "fmt": "{field}_{run}.fits"},
                 {"db": "science.tsv", "fmt": "run{run}.h5"}],
        "remote": REMOTE}, tables={"data.tsv": "path\n",
                                   "science.tsv": "run\n1\n2\n"})
    repo = Repo.clone(ROOT, tmp_path / "clone", download=False)
    assert ROOT + "science.tsv" in reads
    assert (repo.dothm.path / "science.tsv").read_text() == "run\n1\n2\n"
    assert repo.state.tables["science.tsv"]["run"].astype(str).tolist() == [
        "1", "2"]
    plan = repo.plan_download()
    assert sorted(item.relative_path.as_posix() for item in plan.items) == [
        "README.md", "run1.h5", "run2.h5"]


def test_a_snapshot_row_that_names_two_files_is_refused(tmp_path, pages):
    publish(pages[0], ROOT, {
        "data": [{"db": "data.tsv", "fmt": "item_{i}.dat"},
                 {"db": "data.tsv", "fmt": "run_{i}.h5"}],
        "remote": REMOTE}, tables={"data.tsv": "i\n1\n"})
    with pytest.raises(CloneError, match="unique file path"):
        Repo.clone(ROOT, tmp_path / "clone", download=False)
    assert not (tmp_path / "clone").exists()


@pytest.mark.parametrize("meta, message", [
    ("project: [", "Invalid published catalog metadata"),
    ("- project\n", "meta.yml must be a mapping"),
])
def test_snapshot_metadata_must_be_a_yaml_mapping(tmp_path, pages, meta, message):
    publish(pages[0], ROOT, {"data": [{"db": "data.tsv"}], "remote": REMOTE},
            meta=meta)
    with pytest.raises(CloneError, match=message):
        Repo.clone(ROOT, tmp_path / "clone", download=False)
    assert not (tmp_path / "clone").exists()


def test_a_git_source_is_cloned_without_reading_snapshot_metadata(
        tmp_path, pages, monkeypatch):
    calls = []

    def fail(url, *args, **kwargs):
        calls.append(url)
        raise CloneError("not a Git repository")

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", fail)
    with pytest.raises(CloneError, match="hm init PATH, then hm add URL"):
        Repo.clone(ROOT, tmp_path / "clone", source_type="git", download=False)
    assert calls == [ROOT] and pages[1] == []
    assert not (tmp_path / "clone").exists()


@pytest.fixture
def lab_profile(tmp_path, monkeypatch):
    """Configure an HTTP auth profile named ``lab`` for example.test."""
    path = tmp_path / "auth.yml"
    path.write_text(yaml.safe_dump({"version": 2, "profiles": {"lab": {
        "type": "http", "hosts": ["example.test"], "scheme": "basic",
        "username": "alice", "password": {"env": "HM_TEST_PASSWORD"}}}}))
    os.chmod(path, 0o600)
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(path))
    monkeypatch.setenv("HM_TEST_PASSWORD", "pw")
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))


@pytest.mark.parametrize("url", ["ssh://git.example.test/dataset",
                                 "https://example.test/dataset"])
def test_auth_profiles_are_refused_for_git_sources(tmp_path, pages, monkeypatch,
                                                   lab_profile, url):
    def fail(*args, **kwargs):
        raise AssertionError("Git must not be run with an auth profile")

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", fail)
    with pytest.raises(ValueError, match="Git cloning uses Git/SSH authentication"):
        Repo.clone(url, tmp_path / "clone", auth="lab", download=False)
    assert not (tmp_path / "clone").exists()
    # An HTTP URL is first probed for a published catalogue.
    assert pages[1] == ([] if url.startswith("ssh") else [
        url + "/config.yml", url + "/.hm/config.yml"])


# The object store


def test_a_store_never_written_lists_no_objects(tmp_path):
    store = Objects(tmp_path)
    assert store.checksums() == []
    assert store.missing_checksums(["1" * 40]) == ["1" * 40]
    assert not store.root.exists()


def test_a_missing_object_is_not_reported_as_changed(tmp_path):
    store = Objects(tmp_path)
    digest = sha1_of(b"payload")
    assert not store.changed(digest)
    with store.temp_file("a.fits") as temp:
        temp.write_bytes(b"payload")
        store.publish(temp, digest)
    store.object_path(digest).unlink()
    assert not store.changed(digest)
    assert not store.verify(digest)


def test_restoring_a_missing_object_fails_clearly(tmp_path):
    store = Objects(tmp_path)
    destination = tmp_path / "restored.fits"
    with pytest.raises(FileNotFoundError, match="not found in objects store"):
        store.restore(sha1_of(b"payload"), destination)
    assert not destination.exists()


def test_placing_a_missing_object_without_links_fails_clearly(tmp_path,
                                                              monkeypatch):
    def link(source, destination):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(objects_module.os, "link", link)
    store = Objects(tmp_path)
    destination = tmp_path / "a.fits"
    with pytest.raises(FileNotFoundError, match="not found in the object store"):
        store.materialize(sha1_of(b"payload"), destination)
    assert not destination.exists()


def test_a_temporary_file_removed_during_cleanup_is_skipped(tmp_path,
                                                            monkeypatch):
    store = Objects(tmp_path)
    store.tmp.mkdir(parents=True)
    old = time.time() - 2 * objects_module.STALE_TEMP_SECONDS
    for name in (".a.fits.gone", ".b.fits.left"):
        (store.tmp / name).write_bytes(b"partial")
        os.utime(store.tmp / name, (old, old))
    real_scandir = os.scandir

    def scandir(path):
        entries = list(real_scandir(path))
        # Another cleanup removes this file once the folder has been listed.
        (store.tmp / ".a.fits.gone").unlink()
        return iter(entries)

    monkeypatch.setattr(objects_module.os, "scandir", scandir)
    assert store.cleanup_stale_temps() == 1
    assert os.listdir(store.tmp) == []
