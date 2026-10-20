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
    plan, result = download(reopened)
    assert fetched == [] and not plan.has_changes
    assert result["staged"] == 3
    assert Repo(repo.worktree).status()["staged"]["modified"] == sorted(FILES)


def test_a_catalogue_changed_after_planning_is_not_staged(repo, server):
    plan = repo.plan_download(file_paths=["b.fits"])
    repo.state.data.loc[repo.state.data.path == "b.fits", "checksum_algorithm"] = "md5"
    repo.state.data.loc[repo.state.data.path == "b.fits", "checksum"] = "0" * 32
    repo.dothm.save_state(repo.state)
    result = repo.download(plan, approved=True)
    assert result["succeeded"] == 1 and "staged" not in result
    assert "sha1" not in Repo(repo.worktree).state.data.columns


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
    assert "Run hm download again; no transfer is needed." in result.output
    monkeypatch.setattr(Dothm, "save_state", original)
    result = CliRunner().invoke(hallmark, ["download", "--all"])
    assert result.exit_code == 0, result.output
    assert "No files selected for download." in result.output
    assert "Staged SHA-1 for 3 file(s); run hm commit to record them" in result.output
    assert "Successfully downloaded" not in result.output


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
