"""Test cataloging remote files with URL templates in hallmark add."""

import hashlib

import pytest
import requests
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.discovery import TemplateMatcher, split_url_template
from hallmark.repo_config import catalog_entries
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing

ROOT = "https://archive.test/2026MOVIE/ER2/"
FILES = {"M87_095.h5": b"m87-095", "M87_096.h5": b"m87-096",
         "SGRA_095.h5": b"sgra-095"}


def _index(entries):
    """Render an Apache-style index with sizes for files and directories."""
    links = "".join(
        f'<a href="{name}">{name}</a>' if name.endswith("/")
        else f'<a href="{name}" data-size="{size}">{name}</a>'
        for name, size in entries)
    return f"<h1>Index of listing</h1>{links}"


@pytest.fixture
def archive(monkeypatch):
    """Serve an archive listing with a checksum manifest; record listing reads."""
    pages, reads = {}, []

    def read_text(context, path):
        url = context.remote.url.rstrip("/") + "/" + path
        reads.append(url)
        if url not in pages:
            raise RemoteObjectMissing("No such metadata object")
        return pages[url]

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    sums = "".join(f"{hashlib.md5(data).hexdigest()}  {name}\n"
                   for name, data in FILES.items())
    pages[ROOT] = _index([*((name, len(data)) for name, data in FILES.items()),
                          ("md5sums.txt", len(sums)), ("README.md", 5),
                          ("calibration/", None), (".hidden_1.h5", 3)])
    pages[ROOT + "md5sums.txt"] = sums
    pages[ROOT + "calibration/"] = _index([("M87_cal.h5", 4)])
    return pages, reads


@pytest.fixture
def repo(tmp_path):
    return Repo.init(tmp_path / "repo")


def test_split_url_template_separates_directory_and_template():
    assert split_url_template(ROOT + "{src}_{day}.h5") == (ROOT, "{src}_{day}.h5")
    assert split_url_template("https://h/a%20b/{run}/x%7B%25.dat") == (
        "https://h/a%20b/", "{run}/x{{%.dat")
    assert split_url_template("ssh://campus/srv/export/{run}.h5") == (
        "ssh://campus/srv/export/", "{run}.h5")
    assert split_url_template(ROOT) == (ROOT, None)
    assert split_url_template(ROOT + "README.md") == (ROOT, "README.md")


@pytest.mark.parametrize("url, message", [
    ("https://user:secret@h/d/{a}.h5", "credentials"),
    ("https://token@h/d/{a}.h5", "credentials"),
    ("https://h/d/{a}.h5?sig=1", "query or fragment"),
    ("https://h/d/{a}.h5#part", "query or fragment"),
    ("https://{host}/d/{a}.h5", "only in the URL path"),
    ("https://h/d/{a!r}.h5", "conversions"),
    ("https://h/d/{a.h5", "Invalid filename template"),
])
def test_split_url_template_rejects_unsafe_urls(url, message):
    with pytest.raises(ValueError, match=message):
        split_url_template(url)


def test_template_matcher_keeps_fields_within_segments():
    matcher = TemplateMatcher("{src}/{day}_[x].h5")
    assert matcher.glob == "*/*_[[]x[]].h5"
    assert matcher.match("M87/095_[x].h5") == {"src": "M87", "day": "095"}
    assert matcher.match("M87/sub/095_[x].h5") is None
    assert matcher.match(".cache/095_[x].h5") is None
    assert matcher.may_contain("M87/")
    assert not matcher.may_contain("M87/095/")
    assert TemplateMatcher("{a}{b}.h5").glob == "*.h5"
    with pytest.raises(ValueError, match="reserved"):
        TemplateMatcher("{size_bytes}.h5")
    with pytest.raises(ValueError, match="naming files"):
        TemplateMatcher("{src}/")


def test_add_catalogs_matching_remote_files_without_payloads(repo, archive):
    pages, reads = archive
    result = repo.add(ROOT + "{src}_{day}.h5")
    assert result["path"].tolist() == sorted(FILES)
    assert repo.state.config["data"] == [{"fmt": "{src}_{day}.h5", "url": ROOT}]
    rows = repo.state.data.to_dict("records")
    assert rows[0] == {
        "sha1": "", "checksum_algorithm": "md5",
        "checksum": hashlib.md5(FILES["M87_095.h5"]).hexdigest(),
        "size_bytes": "7", "mtime": "", "src": "M87", "day": "095"}
    # only the root and the manifest were read; subdirectories were pruned
    assert reads == [ROOT, ROOT + "md5sums.txt"]
    assert not any((repo.worktree / name).exists() for name in FILES)
    reopened = Repo(repo.worktree)
    assert reopened.state.data.to_dict("records") == rows


def test_remote_catalog_commits_without_objects_and_status_stays_clean(
        repo, archive):
    repo.add(ROOT + "{src}_{day}.h5")
    snapshot = repo.status()
    assert snapshot["staged"]["catalog"] == [{
        "templates": ["{src}_{day}.h5"], "url": ROOT,
        "added": 3, "modified": 0, "deleted": 0}]
    assert repo.commit("Add remote EHT data")
    assert not any(path.is_file() for path in repo.objects.root.rglob("*"))
    snapshot = repo.status()
    assert snapshot["staged"]["catalog"] == []
    assert snapshot["worktree"] == {"modified": [], "deleted": []}
    assert snapshot["untracked"] == []


def test_download_verifies_published_checksums(repo, archive, monkeypatch):
    from mock_server import MockServer

    repo.add(ROOT + "{src}_{day}.h5")
    repo.commit("Add remote EHT data")
    server = MockServer(ROOT)
    for name, data in FILES.items():
        server.add_file(name, data)
    monkeypatch.setattr(requests, "Session", lambda: server)
    plan = repo.plan_download()
    assert plan.total_bytes == sum(map(len, FILES.values()))
    result = repo.download(plan, approved=True)
    assert result["succeeded"] == 3 and result["failed"] == 0
    assert (repo.worktree / "SGRA_095.h5").read_bytes() == b"sgra-095"
    snapshot = repo.status()
    assert snapshot["untracked"] == []
    assert snapshot["worktree"] == {"modified": [], "deleted": []}


def test_readding_a_remote_template_syncs_removals(repo, archive):
    pages, _ = archive
    repo.add(ROOT + "{src}_{day}.h5")
    repo.commit("Add remote EHT data")
    pages[ROOT] = _index([("M87_095.h5", 7), ("M87_097.h5", 9)])
    del pages[ROOT + "md5sums.txt"]
    repo.add(ROOT + "{src}_{day}.h5")
    assert repo.state.data["day"].tolist() == ["095", "097"]
    assert repo.status()["staged"]["catalog"] == [{
        "templates": ["{src}_{day}.h5"], "url": ROOT,
        "added": 1, "modified": 1, "deleted": 2}]


def test_no_match_is_an_error_and_changes_nothing(repo, archive):
    before = (repo.dothm.path / "config.yml").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="did not match any files"):
        repo.add(ROOT + "{src}.fits")
    assert (repo.dothm.path / "config.yml").read_text(encoding="utf-8") == before


def test_files_the_template_cannot_recreate_are_counted(repo, archive):
    with pytest.raises(ValueError, match="3 listed file.s. fit the pattern"):
        repo.add(ROOT + "{src}_{day:02d}.h5")
    pages, _ = archive
    pages[ROOT] = _index([("M87_7.h5", 1), ("M87_07.h5", 1)])
    result = repo.add(ROOT + "{src}_{run:02d}.h5")
    assert result["path"].tolist() == ["M87_07.h5"]
    assert result.attrs["mismatched"] == 1


def test_template_segments_and_dry_run(repo, archive):
    pages, reads = archive
    pages[ROOT + "calibration/"] = _index([("M87_cal.h5", 4)])
    preview = repo.add(ROOT + "calibration/{src}_cal.h5", dry_run=True)
    assert preview["path"].tolist() == ["M87_cal.h5"]
    assert catalog_entries(repo.state.config) == []
    repo.add(ROOT + "{kind}/{src}_cal.h5")
    assert repo.state.data.to_dict("records")[0]["kind"] == "calibration"


def test_conflicting_templates_are_rejected(repo, archive):
    (repo.worktree / "local_1.h5").write_text("x\n", encoding="utf-8")
    repo.add(ROOT + "{src}_{day}.h5")
    with pytest.raises(ValueError, match="tracked by another template"):
        repo.add(ROOT + "{name}.h5")
    with pytest.raises(ValueError, match="already tracked from"):
        repo.add("https://mirror.test/ER2/{src}_{day}.h5")
    with pytest.raises(ValueError, match="local files only"):
        repo.add(ROOT + "{src}_{day}.h5", encoding=True)
    repo.add("local_{n}.h5")
    with pytest.raises(ValueError, match="already tracked from the worktree"):
        repo.add(ROOT + "local_{n}.h5")


def test_remote_and_local_templates_share_a_branch(repo, archive):
    (repo.worktree / "runs").mkdir()
    (repo.worktree / "runs/run_1.dat").write_text("run\n", encoding="utf-8")
    repo.add(ROOT + "{src}_{day}.h5")
    repo.add("runs/run_{run}.dat")
    assert [(entry.db, entry.url) for entry in catalog_entries(repo.state.config)] \
        == [("data.tsv", ROOT), ("data-2.tsv", None)]
    assert repo.commit("remote images and local runs")
    assert len([path for path in repo.objects.root.rglob("*")
                if path.is_file()]) == 1
    assert repo.add(".")["path"].tolist() == ["runs/run_1.dat"]


def test_directory_url_points_to_ls_remote(repo, archive):
    with pytest.raises(ValueError, match="hallmark ls-remote"):
        repo.add(ROOT)


def test_remote_add_works_in_a_bare_repository(tmp_path, archive):
    bare = Repo.init(tmp_path / "catalog.hm")
    bare.add(ROOT + "{src}_{day}.h5")
    assert bare.commit("bare catalog")
    assert bare.plan_download(tmp_path / "out").file_count == 3


def test_explicit_backend_is_recorded_on_the_entry(repo, archive):
    repo.add(ROOT + "{src}_{day}.h5", backend="cyverse")
    assert repo.state.config["data"][0]["backend"] == "cyverse"
    assert repo.plan_download().remote_backend == "cyverse"


def test_cli_add_url_summarizes_and_hides_credentials(repo, archive, monkeypatch):
    monkeypatch.chdir(repo.worktree)
    runner = CliRunner()
    result = runner.invoke(hallmark, ["add", ROOT + "{src}_{day}.h5"])
    assert result.exit_code == 0, result.output
    assert "Cataloged 3 remote file(s); nothing was downloaded." in result.output
    assert "SGRA_095.h5" in result.output
    secret = runner.invoke(
        hallmark, ["add", "https://user:hunter2@archive.test/ER2/{a}.h5"])
    assert secret.exit_code != 0
    assert "hunter2" not in secret.output
    directory = runner.invoke(hallmark, ["add", ROOT])
    assert directory.exit_code != 0
    assert "ls-remote" in directory.output
