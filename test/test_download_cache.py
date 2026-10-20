"""Downloads pass through .hm/objects: Worktree <-> Objects <-> State <-> Dothm."""

import errno
import hashlib
import os
import stat
from pathlib import Path

import pytest

from hallmark import Repo
from hallmark.repo import objects as objects_module
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing
from hallmark.transport.http import HttpBackend

ROOT = "https://example.test/data/"
FILES = {"a.fits": b"alpha", "b.fits": b"bravo", "c.fits": b"alpha"}


def sha1(data):
    return hashlib.sha1(data).hexdigest()


@pytest.fixture
def server(monkeypatch):
    """Serve FILES with sizes in an index and record every payload fetch."""
    files = dict(FILES)
    fetched = []

    def read_text(context, path, **_):
        if path:
            raise RemoteObjectMissing("No metadata")
        return "<h1>Index of data</h1>" + "".join(
            f'<a href="{name}" data-size="{len(data)}">{name}</a>'
            for name, data in files.items())

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        fetched.append(relative_path)
        Path(destination).write_bytes(files[relative_path])

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    return files, fetched


@pytest.fixture
def repo(tmp_path, server):
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT)
    repo.commit("Catalogue")
    return repo


def download(repo, **selection):
    plan = repo.plan_download(**(selection or {"all_files": True}))
    return plan, repo.download(plan, approved=True)


def test_downloads_are_read_only_links_to_cached_objects(repo, server):
    _, result = download(repo)
    assert (result["succeeded"], result["failed"]) == (3, 0)
    for name, data in FILES.items():
        path = repo.worktree / name
        assert path.read_bytes() == data
        assert repo.objects.is_linked(sha1(data), path)
        assert not os.stat(path).st_mode & stat.S_IWUSR
    # Identical content is stored once and linked twice.
    assert repo.objects.object_path(sha1(b"alpha")).stat().st_nlink == 3
    assert list(repo.objects.tmp.iterdir()) == []
    assert sorted(p.name for p in repo.worktree.iterdir()) == [
        ".hm", "a.fits", "b.fits", "c.fits"]


def test_a_rerun_recognises_its_own_files_without_transfers(repo, server):
    download(repo)
    files, fetched = server
    fetched.clear()
    plan, result = download(repo)
    assert plan.items == () and plan.stored_items == ()
    assert sorted(item.relative_path.name for item in plan.skipped_items) == sorted(
        FILES)
    assert {item.relative_path.name: item.sha1 for item in plan.skipped_items} == {
        name: sha1(data) for name, data in FILES.items()}
    assert fetched == []
    assert result == {"succeeded": 0, "failed": 0, "total_bytes": 0, "errors": []}


def test_known_content_is_placed_from_the_cache(repo, server):
    files, fetched = server
    repo.state.data["sha1"] = [sha1(FILES[path]) for path in repo.state.data.path]
    repo.dothm.save_state(repo.state)
    download(repo)
    (repo.worktree / "b.fits").unlink()
    fetched.clear()
    plan, result = download(repo)
    assert [item.relative_path.name for item in plan.stored_items] == ["b.fits"]
    assert plan.items == () and plan.file_count == 1 and plan.has_changes
    assert "Already in .hm/objects: 1 file(s) (no transfer)" in plan.summary()
    assert result["from_store"] == ["b.fits"]
    assert result["succeeded"] == 1
    assert fetched == []
    assert repo.objects.is_linked(sha1(b"bravo"), repo.worktree / "b.fits")


def test_a_vanished_cached_object_is_not_fetched_without_approval(repo, server):
    files, fetched = server
    repo.state.data["sha1"] = [sha1(FILES[path]) for path in repo.state.data.path]
    repo.dothm.save_state(repo.state)
    download(repo)
    (repo.worktree / "b.fits").unlink()
    plan = repo.plan_download(all_files=True)
    obj = repo.objects.object_path(sha1(b"bravo"))
    obj.unlink()
    fetched.clear()
    result = repo.download(plan, approved=True)
    assert result["failed"] == 1
    assert "stored copy of b.fits is missing" in result["errors"][0]
    assert fetched == []
    with pytest.raises(Exception, match="approval"):
        repo.download(plan)


def test_files_are_copied_where_links_are_unavailable(repo, server, monkeypatch):
    real_link = os.link

    def link(source, destination):
        if ".hm" not in str(destination):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_link(source, destination)

    monkeypatch.setattr(objects_module.os, "link", link)
    _, result = download(repo, file_paths=["a.fits"])
    assert result["copied"] == ["a.fits"]
    path = repo.worktree / "a.fits"
    assert path.read_bytes() == b"alpha"
    assert os.stat(path).st_mode & stat.S_IWUSR
    assert not repo.objects.is_linked(sha1(b"alpha"), path)
    assert repo.objects.contains(sha1(b"alpha"))


def test_foreign_files_without_a_digest_block_the_download(repo, server):
    files, fetched = server
    (repo.worktree / "b.fits").write_bytes(b"bravo")
    with pytest.raises(Exception, match="conflict"):
        repo.plan_download(all_files=True)
    assert fetched == []


def test_stale_temporary_files_are_swept_before_downloading(repo, server):
    repo.objects.tmp.mkdir(parents=True)
    stale = repo.objects.tmp / ".a.fits.crashed"
    stale.write_bytes(b"alp")
    old = stale.stat().st_mtime - objects_module.STALE_TEMP_SECONDS - 60
    os.utime(stale, (old, old))
    download(repo, file_paths=["a.fits"])
    assert list(repo.objects.tmp.iterdir()) == []


def test_interrupted_transfers_leave_nothing_in_the_worktree(repo, server,
                                                              monkeypatch):
    def interrupted(self, relative_path, destination, *, chunk_size=8192):
        Path(destination).write_bytes(b"al")
        raise KeyboardInterrupt

    monkeypatch.setattr(HttpBackend, "fetch", interrupted)
    with pytest.raises(KeyboardInterrupt):
        download(repo, file_paths=["a.fits"])
    assert sorted(p.name for p in repo.worktree.iterdir()) == [".hm"]
    assert list(repo.objects.tmp.iterdir()) == []
    assert not repo.objects.contains(sha1(b"alpha"))


def test_output_folders_also_link_to_the_cache(repo, server, tmp_path):
    plan = repo.plan_download(tmp_path / "elsewhere", all_files=True)
    repo.download(plan, approved=True)
    assert repo.objects.is_linked(sha1(b"bravo"), tmp_path / "elsewhere" / "b.fits")


def test_downloads_stage_each_files_sha1_for_commit(repo, server):
    _, result = download(repo)
    assert result["staged"] == 3
    assert repo.status()["staged"]["modified"] == sorted(FILES)
    expected = {name: sha1(data) for name, data in FILES.items()}
    assert dict(zip(repo.state.data.path, repo.state.data.sha1)) == expected
    saved = Repo(repo.worktree)
    assert dict(zip(saved.state.data.path, saved.state.data.sha1)) == expected
    stored = sorted(repo.objects.root.rglob("*"))
    assert repo.commit("Record downloaded content")
    assert Repo(repo.worktree).status()["staged"]["modified"] == []
    # Committing a remote catalogue stores nothing beyond the downloads.
    assert sorted(repo.objects.root.rglob("*")) == stored
    # Later runs skip the linked files without hashing them.
    import hallmark.remote.download as module

    def no_hashing(*args, **kwargs):
        raise AssertionError("linked files need no hashing")

    original = module.calculate_file_checksums
    module.calculate_file_checksums = no_hashing
    try:
        plan, result = download(repo)
    finally:
        module.calculate_file_checksums = original
    assert len(plan.skipped_items) == 3 and "staged" not in result


def test_state_is_saved_once_per_download(repo, server, monkeypatch):
    from hallmark.repo.dothm import Dothm

    saves = []
    original = Dothm.save_state
    monkeypatch.setattr(Dothm, "save_state",
                        lambda self, state: saves.append(1) or original(self, state))
    download(repo)
    assert saves == [1]
    download(repo)
    assert saves == [1]


def test_only_successful_files_are_staged(repo, server, monkeypatch):
    files, _ = server
    files["b.fits"] = b"truncated"[:3]
    _, result = download(repo)
    assert (result["succeeded"], result["failed"], result["staged"]) == (2, 1, 2)
    data = dict(zip(repo.state.data.path, repo.state.data.sha1))
    assert data == {"a.fits": sha1(b"alpha"), "b.fits": "", "c.fits": sha1(b"alpha")}


def test_an_interrupted_download_stages_what_finished(repo, server, monkeypatch):
    files, fetched = server

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        if relative_path == "b.fits":
            raise KeyboardInterrupt
        Path(destination).write_bytes(files[relative_path])

    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    plan = repo.plan_download(file_paths=["a.fits", "b.fits"])
    with pytest.raises(KeyboardInterrupt):
        repo.download(plan, approved=True, max_workers=1)
    data = dict(zip(Repo(repo.worktree).state.data.path,
                    Repo(repo.worktree).state.data.sha1))
    assert data["a.fits"] == sha1(b"alpha")
    assert data["b.fits"] == ""


def test_an_interrupt_stages_every_file_that_finished_with_it(repo, server,
                                                              monkeypatch):
    import concurrent.futures

    from hallmark.remote import download as download_module

    files, fetched = server

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        if relative_path == "b.fits":
            raise KeyboardInterrupt
        Path(destination).write_bytes(files[relative_path])

    def wait_for_both(futures, return_when):
        # Both finish before the caller wakes, the interrupted one listed first.
        done, _ = concurrent.futures.wait(futures)
        return sorted(done, key=lambda future: future.exception() is None), set()

    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    monkeypatch.setattr(download_module, "wait", wait_for_both)
    plan = repo.plan_download(file_paths=["a.fits", "b.fits"])
    with pytest.raises(KeyboardInterrupt):
        repo.download(plan, approved=True, max_workers=2)
    data = Repo(repo.worktree).state.data
    assert dict(zip(data.path, data.sha1)) == {
        "a.fits": sha1(b"alpha"), "b.fits": "", "c.fits": ""}


def test_jobs_finishing_while_a_download_stops_are_recorded(tmp_path, monkeypatch):
    from threading import Event

    from hallmark.remote.download import _run_transfers
    from hallmark.transport import RemoteSpec

    released = Event()
    original_cancel = OperationContext.cancel

    def cancel(self):
        released.set()
        original_cancel(self)

    def run_job(context, job):
        if job == "interrupted":
            raise KeyboardInterrupt
        released.wait(timeout=10)
        return job

    recorded = []
    monkeypatch.setattr(OperationContext, "cancel", cancel)
    with pytest.raises(KeyboardInterrupt):
        _run_transfers(RemoteSpec.from_url(ROOT), tmp_path, ["slow", "interrupted"],
                       run_job, lambda *args: recorded.append(args),
                       max_workers=2, show_progress=False)
    assert recorded == [("slow", "slow", None)]


def test_a_failed_save_is_recovered_by_the_next_run(repo, server, monkeypatch):
    from hallmark.repo.dothm import Dothm

    files, fetched = server
    original = Dothm.save_state

    def broken(self, state):
        raise OSError("disk full")

    monkeypatch.setattr(Dothm, "save_state", broken)
    _, result = download(repo)
    assert result["succeeded"] == 3
    assert "disk full" in result["staging_error"]
    assert (repo.worktree / "b.fits").read_bytes() == b"bravo"
    monkeypatch.setattr(Dothm, "save_state", original)
    fetched.clear()
    reopened = Repo(repo.worktree)
    assert "sha1" not in reopened.state.data.columns
    # Without a checksum nothing proves the links current; they are compared.
    plan, result = download(reopened)
    assert len(plan.outdated_items) == 3
    assert sorted(fetched) == sorted(FILES)
    assert sorted(result["unchanged"]) == sorted(FILES) and "replaced" not in result
    assert result["staged"] == 3
    for name, data in FILES.items():
        assert repo.objects.is_linked(sha1(data), repo.worktree / name)
    assert Repo(repo.worktree).status()["staged"]["modified"] == sorted(FILES)


def test_a_catalogue_changed_after_planning_is_not_staged(repo, server):
    plan = repo.plan_download(file_paths=["b.fits"])
    repo.state.data.loc[repo.state.data.path == "b.fits", "checksum_algorithm"] = "md5"
    repo.state.data.loc[repo.state.data.path == "b.fits", "checksum"] = "0" * 32
    repo.dothm.save_state(repo.state)
    result = repo.download(plan, approved=True)
    assert result["succeeded"] == 1 and "staged" not in result
    assert "sha1" not in Repo(repo.worktree).state.data.columns


def test_a_row_changed_during_a_download_is_not_staged(repo, server, monkeypatch):
    files, fetched = server

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        if relative_path == "c.fits":
            # The server changes a.fits, already fetched, and it is added again.
            files["a.fits"] = b"alpha, revised"
            Repo(repo.worktree).add(ROOT)
        Path(destination).write_bytes(files[relative_path])

    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    # One worker fetches in order, so a.fits is complete before it changes.
    plan = repo.plan_download(all_files=True)
    result = repo.download(plan, approved=True, max_workers=1)
    assert result["succeeded"] == 3 and result["staged"] == 2
    data = Repo(repo.worktree).state.data
    assert dict(zip(data.path, data.sha1)) == {
        "a.fits": "", "b.fits": sha1(b"bravo"), "c.fits": sha1(b"alpha")}
    assert data.set_index("path").at["a.fits", "size_bytes"] == "14"


def test_cli_reports_staging_and_recovers_on_a_quiet_rerun(repo, server,
                                                           monkeypatch):
    from click.testing import CliRunner

    from hallmark.cli import hallmark
    from hallmark.repo.dothm import Dothm

    monkeypatch.chdir(repo.worktree)
    original = Dothm.save_state

    def broken(self, state):
        raise OSError("disk full")

    monkeypatch.setattr(Dothm, "save_state", broken)
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code == 1
    assert "could not be recorded: disk full" in result.output
    assert "Run hm download again to record them." in result.output
    monkeypatch.setattr(Dothm, "save_state", original)
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Outdated: 3 file(s) will be replaced" in result.output
    assert "3 without a catalogued checksum are fetched to compare" in result.output
    assert ("Fetched 3 file(s) to compare; they were already up to date"
            in result.output)
    assert "Staged SHA-1 for 3 file(s); run hm commit to record them" in result.output
    assert "Replaced" not in result.output


def test_dry_run_lists_files_coming_from_the_cache(repo, server, monkeypatch):
    from click.testing import CliRunner

    from hallmark.cli import hallmark

    download(repo)
    repo.commit("Record downloaded content")
    (repo.worktree / "b.fits").unlink()
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["download", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Already in .hm/objects: 1 file(s) (no transfer)" in result.output
    assert "  b.fits (from .hm/objects)" in result.output
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Placed 1 file(s) from .hm/objects" in result.output
    assert "Successfully downloaded" not in result.output


def later(path, seconds=10):
    """Move a file's modification time forward, as an edit made later would."""
    info = os.stat(path)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + seconds * 10**9))


def test_a_download_edited_in_place_is_reported_and_fetched_again(repo, server):
    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    path = repo.worktree / "b.fits"
    os.chmod(path, 0o644)
    path.write_bytes(b"BRAVO")
    later(path)
    assert repo.status()["worktree"]["modified"] == ["b.fits"]
    with pytest.raises(Exception, match=r"b\.fits \(edited in place"):
        repo.plan_download(all_files=True)
    # The changed object was dropped, so it can never be linked again.
    assert not repo.objects.contains(sha1(b"bravo"))
    path.unlink()
    fetched.clear()
    plan, result = download(repo)
    assert [item.relative_path.name for item in plan.items] == ["b.fits"]
    assert plan.stored_items == ()
    assert fetched == ["b.fits"] and result["failed"] == 0
    assert path.read_bytes() == b"bravo"
    assert repo.objects.is_linked(sha1(b"bravo"), path)
    assert repo.status()["worktree"]["modified"] == []


def test_a_changed_object_is_transferred_again_not_relinked(repo, server):
    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    (repo.worktree / "b.fits").unlink()
    obj = repo.objects.object_path(sha1(b"bravo"))
    os.chmod(obj, 0o644)
    obj.write_bytes(b"BRAVO")
    later(obj)
    fetched.clear()
    plan, result = download(repo)
    assert plan.stored_items == ()
    assert fetched == ["b.fits"] and result["failed"] == 0
    assert (repo.worktree / "b.fits").read_bytes() == b"bravo"


def test_a_change_saved_during_a_download_is_kept(repo, server, monkeypatch):
    files, fetched = server
    real_fetch = HttpBackend.fetch
    changed = []

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        if not changed:
            # Another command catalogues a second source mid-download.
            changed.append(True)
            Repo(repo.worktree).add(ROOT, filter="b.fits", source="extra",
                                    prefix="extra")
        return real_fetch(self, relative_path, destination, chunk_size=chunk_size)

    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    _, result = download(repo)
    assert result["staged"] == 3
    saved = Repo(repo.worktree).state.data.set_index("path")
    assert "extra/b.fits" in saved.index
    assert saved.loc["a.fits", "sha1"] == sha1(b"alpha")


def serve_index(monkeypatch, files, *, mtimes=None, sums=False):
    """Serve the index with modification times, and a SHA256SUMS file if asked."""
    mtimes = mtimes or {}

    def read_text(context, path, **_):
        if path == "SHA256SUMS" and sums:
            return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                           for name, data in files.items())
        if path:
            raise RemoteObjectMissing("No metadata")
        links = "".join(
            f'<a href="{name}" data-size="{len(data)}"'
            + (f' data-mtime="{mtimes[name]}"' if name in mtimes else "")
            + f">{name}</a>" for name, data in files.items())
        if sums:
            links += '<a href="SHA256SUMS">SHA256SUMS</a>'
        return "<h1>Index of data</h1>" + links

    monkeypatch.setattr(OperationContext, "read_text", read_text)


def links_fail(monkeypatch):
    """Refuse hard links into the worktree, as another filesystem would."""
    real_link = os.link

    def link(source, destination):
        if ".hm" not in str(destination):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_link(source, destination)

    monkeypatch.setattr(objects_module.os, "link", link)


def staged_sha1(repo):
    data = Repo(repo.worktree).state.data
    return dict(zip(data.path, data.sha1))


def test_a_file_changed_upstream_is_replaced_and_the_old_version_kept(repo, server):
    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT)
    fetched.clear()
    plan = repo.plan_download(all_files=True)
    assert [(item.relative_path.name, item.replaces)
            for item in plan.outdated_items] == [("b.fits", sha1(b"bravo"))]
    assert "Outdated: 1 file(s) will be replaced" in plan.summary()
    result = repo.download(plan, approved=True)
    assert fetched == ["b.fits"] and result["replaced"] == ["b.fits"]
    path = repo.worktree / "b.fits"
    assert path.read_bytes() == b"bravo, revised"
    assert repo.objects.is_linked(sha1(b"bravo, revised"), path)
    assert not os.stat(path).st_mode & stat.S_IWUSR
    # The old version stays, so the previous commit can still be placed.
    assert repo.objects.contains(sha1(b"bravo"))
    assert staged_sha1(repo)["b.fits"] == sha1(b"bravo, revised")
    assert list(repo.worktree.glob(".b.fits.*")) == []


@pytest.mark.parametrize("content, outcome", [(b"bravo", "unchanged"),
                                              (b"BRAVO", "replaced")])
def test_a_file_touched_upstream_is_compared_by_its_content(repo, server,
                                                            monkeypatch, content,
                                                            outcome):
    files, fetched = server
    serve_index(monkeypatch, files, mtimes=dict.fromkeys(files, 1750000000))
    repo.add(ROOT)
    download(repo)
    repo.commit("Record downloaded content")
    path = repo.worktree / "b.fits"
    inode = os.stat(path).st_ino
    files["b.fits"] = content
    serve_index(monkeypatch, files,
                mtimes={**dict.fromkeys(files, 1750000000), "b.fits": 1760000000})
    repo.add(ROOT)
    fetched.clear()
    plan, result = download(repo)
    assert [item.relative_path.name for item in plan.outdated_items] == ["b.fits"]
    assert "1 without a catalogued checksum are fetched to compare" in plan.summary()
    assert fetched == ["b.fits"] and result[outcome] == ["b.fits"]
    assert path.read_bytes() == content
    assert (os.stat(path).st_ino == inode) == (outcome == "unchanged")
    assert staged_sha1(repo)["b.fits"] == sha1(content)


def test_switching_branches_replaces_downloads_from_the_store(repo, server):
    files, fetched = server
    download(repo)
    repo.commit("Version 1")
    repo.create_branch("v2")
    repo.checkout("v2")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT)
    download(repo)
    repo.commit("Version 2")
    repo.checkout("main")
    fetched.clear()
    plan, result = download(repo)
    assert [(item.relative_path.name, item.replaces) for item in plan.stored_items] \
        == [("b.fits", sha1(b"bravo, revised"))]
    assert fetched == []
    assert result["from_store"] == ["b.fits"] and result["replaced"] == ["b.fits"]
    assert repo.objects.is_linked(sha1(b"bravo"), repo.worktree / "b.fits")


def published_sums_repo(tmp_path, monkeypatch, files):
    """Catalogue the files with checksums from a published SHA256SUMS."""
    serve_index(monkeypatch, files, sums=True)
    repo = Repo.init(tmp_path / "sums")
    repo.add(ROOT, filter="*.fits")
    return repo


def test_an_outdated_copy_is_replaced_where_links_are_unavailable(tmp_path, server,
                                                                  monkeypatch):
    files, fetched = server
    repo = published_sums_repo(tmp_path, monkeypatch, files)
    links_fail(monkeypatch)
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT, filter="*.fits")
    plan = repo.plan_download(all_files=True)
    assert "without a catalogued checksum" not in plan.summary()
    result = repo.download(plan, approved=True)
    assert result["replaced"] == ["b.fits"] and result["copied"] == ["b.fits"]
    path = repo.worktree / "b.fits"
    assert path.read_bytes() == b"bravo, revised"
    assert os.stat(path).st_mode & stat.S_IWUSR


@pytest.mark.parametrize("linked", [True, False])
def test_an_edited_outdated_file_is_a_conflict(tmp_path, server, monkeypatch,
                                               linked):
    files, fetched = server
    repo = published_sums_repo(tmp_path, monkeypatch, files)
    if not linked:
        links_fail(monkeypatch)
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT, filter="*.fits")
    path = repo.worktree / "b.fits"
    os.chmod(path, 0o644)
    path.write_bytes(b"my notes")
    later(path)
    fetched.clear()
    with pytest.raises(Exception, match="conflict"):
        repo.plan_download(all_files=True)
    assert path.read_bytes() == b"my notes" and fetched == []


def test_an_outdated_file_changed_during_the_download_is_kept(repo, server,
                                                              monkeypatch):
    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT)
    path = repo.worktree / "b.fits"
    real_fetch = HttpBackend.fetch

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        os.chmod(path, 0o644)
        path.write_bytes(b"my notes")
        later(path)
        return real_fetch(self, relative_path, destination, chunk_size=chunk_size)

    monkeypatch.setattr(HttpBackend, "fetch", fetch)
    _, result = download(repo)
    assert result["failed"] == 1
    assert "b.fits changed during the download; it was kept" in result["errors"][0]
    assert path.read_bytes() == b"my notes"
    assert repo.objects.contains(sha1(b"bravo, revised"))
    assert staged_sha1(repo)["b.fits"] == ""


def test_a_replacement_needs_the_plan_that_listed_it(repo, server):
    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT)
    path = repo.worktree / "b.fits"
    path.unlink()
    plan = repo.plan_download(all_files=True)
    assert plan.outdated_items == ()
    repo.objects.materialize(sha1(b"bravo"), path)
    fetched.clear()
    result = repo.download(plan, approved=True)
    assert result["failed"] == 1 and fetched == []
    assert "changed since the plan was approved; it was kept" in result["errors"][0]
    assert path.read_bytes() == b"bravo"


def test_an_outdated_file_under_another_letter_case_is_a_conflict(repo, server,
                                                                  monkeypatch):
    from hallmark.remote import download as download_module

    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT)
    real_listdir = os.listdir

    def listdir(path="."):
        # As if the folder held B.fits, which b.fits names where case is ignored.
        names = real_listdir(path)
        if Path(path) == repo.worktree:
            return ["B.fits" if name == "b.fits" else name for name in names]
        return names

    monkeypatch.setattr(download_module.os, "listdir", listdir)
    with pytest.raises(Exception, match=r"b\.fits \(another path differing only"):
        repo.plan_download(all_files=True)


def test_paths_differing_in_case_never_replace_each_other(tmp_path, monkeypatch):
    probe = tmp_path / "Probe"
    probe.touch()
    if not (tmp_path / "probe").exists():
        pytest.skip("This filesystem tells letter cases apart")
    files = {"B.fits": b"upper", "b.fits": b"lower"}
    serve_index(monkeypatch, files)
    monkeypatch.setattr(
        HttpBackend, "fetch",
        lambda self, path, destination, **_: Path(destination).write_bytes(files[path]))
    repo = Repo.init(tmp_path / "data")
    repo.add(ROOT)
    _, result = download(repo)
    assert result["succeeded"] == 1 and result["failed"] == 1
    with pytest.raises(Exception, match="differing only in letter case"):
        repo.plan_download(all_files=True)


def test_cli_lists_and_reports_replaced_files(repo, server, monkeypatch):
    from click.testing import CliRunner

    from hallmark.cli import hallmark

    files, fetched = server
    download(repo)
    repo.commit("Record downloaded content")
    files["b.fits"] = b"bravo, revised"
    repo.add(ROOT)
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["download", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "  b.fits (replaces an outdated file)" in result.output
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Outdated: 1 file(s) will be replaced" in result.output
    assert ("Replaced 1 outdated file(s); old versions stay in .hm/objects"
            in result.output)
