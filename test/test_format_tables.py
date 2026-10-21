"""A remote catalogue across format TSVs and file: entries: download, stage, status."""

import hashlib
from pathlib import Path

import pytest

from hallmark import Repo
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing
from hallmark.transport.http import HttpBackend

ROOT = "https://example.test/data/"
FMTS = [{"fmt": "{src}_{day:03d}.fits", "db": "fits"}, {"fmt": "run_{run}.log"}]


def sha1(data):
    return hashlib.sha1(data).hexdigest()


@pytest.fixture
def server(monkeypatch):
    """Serve files per host with an md5sums manifest; record every fetch."""
    files = {"example.test": {"M87_001.fits": b"fits", "run_1.log": b"log",
                              "README.md": b"read me"}}
    fetched = []

    def read_text(context, path, **_):
        hosted = files[context.remote.host]
        if path == "md5sums":
            return "".join(f"{hashlib.md5(data).hexdigest()}  {name}\n"
                           for name, data in hosted.items())
        if path:
            raise RemoteObjectMissing("No metadata")
        return "<h1>Index of data</h1>" + "".join(
            f'<a href="{name}">{name}</a>' for name in [*hosted, "md5sums"])

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        fetched.append(relative_path)
        Path(destination).write_bytes(files[self.context.remote.host][relative_path])

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    return files, fetched


def catalogue(tmp_path):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT, remote_fmt=FMTS, filter=["*.fits", "*.log", "*.md"])
    return repo


def test_every_tsv_and_file_entry_is_downloaded_and_staged(tmp_path, server):
    files, fetched = server
    repo = catalogue(tmp_path)
    assert repo.state.config["data"] == [
        {"name": "readme", "file": "README.md",
         "md5": hashlib.md5(b"read me").hexdigest()},
        {"db": "fits.tsv", "fmt": "{src}_{day:03d}.fits"},
        {"db": "run_{run}.log.tsv", "fmt": "run_{run}.log"}]
    assert repo.status()["staged"]["added"] == [
        "M87_001.fits", "README.md", "run_1.log"]
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    assert (result["succeeded"], result["staged"]) == (3, 3)
    saved = Repo(repo.worktree)
    assert saved.state.tables["fits.tsv"]["sha1"].tolist() == [sha1(b"fits")]
    assert saved.state.tables["run_{run}.log.tsv"]["sha1"].tolist() == [sha1(b"log")]
    assert saved.state.config["data"][0]["sha1"] == sha1(b"read me")
    # Downloaded files are catalogued, not untracked, wherever their rows live.
    assert saved.status()["untracked"] == []
    saved.commit("Catalogue and checksums")
    fetched.clear()
    plan = saved.plan_download(all_files=True)
    assert not plan.has_changes and fetched == []
    assert sorted(item.sha1 for item in plan.skipped_items) == sorted(
        sha1(data) for data in files["example.test"].values())


def test_adding_again_keeps_sha1s_and_lists_changed_entries(tmp_path, server):
    files, fetched = server
    repo = catalogue(tmp_path)
    repo.download(repo.plan_download(all_files=True), approved=True)
    repo.commit("Catalogue and checksums")
    files["example.test"]["README.md"] = b"read me, revised"
    result = repo.add(ROOT)
    assert result.attrs["changed"] == ["README.md"]
    assert "sha1" not in repo.state.config["data"][0]
    assert repo.state.tables["fits.tsv"]["sha1"].tolist() == [sha1(b"fits")]
    # A more specific format takes the fits file; its SHA-1 moves with it.
    repo.add(ROOT, remote_fmt="M87_{day:03d}.fits", resort=True)
    moved = repo.state.tables["M87_{day:03d}.fits.tsv"]
    assert moved["path"].tolist() == ["M87_001.fits"]
    assert moved["sha1"].tolist() == [sha1(b"fits")]
    assert repo.state.tables["fits.tsv"].empty


def test_file_entries_of_several_sources_follow_their_remote(tmp_path, server):
    files, _ = server
    files["mirror.test"] = {"README.md": b"read me"}
    repo = catalogue(tmp_path)
    repo.add("https://mirror.test/data/", prefix="mirror")
    assert repo.state.config["data"][:2] == [
        {"name": "readme", "file": "README.md",
         "md5": hashlib.md5(b"read me").hexdigest(), "catalog_source": "origin"},
        {"name": "readme", "file": "mirror/README.md",
         "md5": hashlib.md5(b"read me").hexdigest(),
         "catalog_source": "mirror-test"}]
    plan = repo.plan_download(file_paths=["README.md", "mirror/README.md"])
    routes = sorted((item.relative_path.as_posix(), item.remote) for item in plan.items)
    assert routes == [("README.md", "origin"), ("mirror/README.md", "mirror-test")]
    result = repo.download(plan, approved=True)
    assert result["staged"] == 2
    assert [entry.get("sha1") for entry in Repo(repo.worktree).state.config["data"][:2]
            ] == [sha1(b"read me")] * 2


def test_a_legacy_catalogue_with_format_tsvs_gains_its_source(tmp_path, server):
    repo = Repo.init(tmp_path / "data")
    repo.state.config = {"data": [{"db": "fits.tsv", "fmt": "{src}_{day:03d}.fits"}],
                         "remote": [{"url": ROOT}]}
    repo.state.tables = {"fits.tsv": __import__("pandas").DataFrame(
        {"path": ["M87_001.fits"], "src": ["M87"], "day": ["001"]})}
    repo.dothm.save_state(repo.state)
    legacy = Repo(repo.worktree)
    assert legacy.state.tables["fits.tsv"]["path"].tolist() == ["M87_001.fits"]
    legacy.add(ROOT)
    assert legacy.state.config["sources"] == [{"name": "origin", "remote": "origin"}]
    assert legacy.state.tables["fits.tsv"]["catalog_source"].tolist() == ["origin"]


def test_a_malformed_table_name_is_reported_when_saving(tmp_path):
    repo = Repo.init(tmp_path / "data")
    repo.state.config["data"] = [{"db": "../escape.tsv", "fmt": "{x}.h5"}]
    repo.dothm.write_yaml(repo.state.config, "config")
    # Loading still works, so status can explain what is wrong.
    assert repo.dothm.load_state().tables == {}
    with pytest.raises(ValueError):
        repo.dothm.save_state(repo.dothm.load_state())


def test_restoring_rows_covers_every_tsv(tmp_path, server):
    files, _ = server
    repo = catalogue(tmp_path)
    repo.commit("Catalogue")
    files["example.test"]["M87_002.fits"] = b"more fits"
    files["example.test"]["run_2.log"] = b"more log"
    repo.add(ROOT)
    staged = {diff.a_path for diff in repo.dothm.index.diff("HEAD")}
    assert {"fits.tsv", "run_{run}.log.tsv"} <= staged
    repo.restore_staged(["M87_002.fits"])
    staged = {diff.a_path for diff in repo.dothm.index.diff("HEAD")}
    assert "fits.tsv" not in staged and "run_{run}.log.tsv" in staged
    with pytest.raises(ValueError, match="file: entry of config.yml"):
        repo.restore_staged(["README.md"])
