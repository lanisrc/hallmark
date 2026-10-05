"""Test download planning and approval using local catalog metadata."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from hallmark.remote.plan import DownloadItem, DownloadPlan
from hallmark.remote.download import (
    DownloadError,
    execute_download_plan,
    plan_download,
)


@pytest.fixture
def catalog(tmp_path):
    """Create a local catalog with known and unknown file metadata."""
    metadata = tmp_path / ".hm"
    metadata.mkdir()
    frame = pd.DataFrame([
        {"path": "nested/a.fits", "size_bytes": "6", "mtime": "2026-09-18",
         "sha256": sha256(b"abcdef").hexdigest()},
        {"path": "b.txt", "size_bytes": "", "mtime": "", "sha256": ""},
        {"path": "empty.fits", "size_bytes": "0", "mtime": "", "sha256": ""},
    ])
    frame.to_csv(metadata / "data.tsv", sep="\t", index=False)
    return SimpleNamespace(
        dothm=SimpleNamespace(path=metadata), worktree=tmp_path,
        state=SimpleNamespace(data=frame, config={
            "data": [{"db": "data.tsv"}],
            "remote": {"name": "origin", "url": "https://source.test/data/"},
        }))


def test_plan_reads_path_catalog_offline_and_preserves_unknown_sizes(
        catalog, monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("planning must not contact a server")
    monkeypatch.setattr("hallmark.remote.download.OperationContext", reject_network)
    monkeypatch.setattr("requests.sessions.Session.request", reject_network)
    plan = plan_download(catalog)
    assert plan.file_count == 3
    assert plan.known_bytes == 6
    assert plan.unknown_size_count == 1
    assert plan.total_bytes is None
    assert plan.estimated_seconds is None
    assert "1 file(s) with unknown size" in plan.summary()
    assert "estimated duration: unknown" in plan.summary()
    assert plan.items[0].mtime == "2026-09-18"
    assert plan.items[2].size_bytes == 0


def test_explicit_paths_retain_catalog_checksums_and_metadata(catalog):
    plan = plan_download(catalog, file_paths=["nested/a.fits", "b.txt"])
    assert plan.file_count == 2
    assert plan.items[0].checksum == ("sha256", sha256(b"abcdef").hexdigest())
    assert plan.items[0].size_bytes == 6
    assert plan.items[1].checksum is None
    assert plan.items[1].size_bytes is None
    assert plan.unknown_paths == ()


def _folder_catalog(tmp_path):
    """Create a catalog with nested folders and a sibling sharing a prefix."""
    metadata = tmp_path / ".hm"
    metadata.mkdir()
    frame = pd.DataFrame({"path": [
        "runs/a.h5", "runs/2024/b.h5", "runs/2024/x/c.h5", "runsx/d.h5", "top.h5"]})
    frame.to_csv(metadata / "data.tsv", sep="\t", index=False)
    return SimpleNamespace(
        dothm=SimpleNamespace(path=metadata), worktree=tmp_path,
        state=SimpleNamespace(data=frame, config={
            "data": [{"db": "data.tsv"}],
            "remote": {"name": "origin", "url": "https://source.test/data/"},
        }))


@pytest.mark.parametrize("requested, expected", [
    (["runs"], ["runs/a.h5", "runs/2024/b.h5", "runs/2024/x/c.h5"]),
    (["runs/2024/"], ["runs/2024/b.h5", "runs/2024/x/c.h5"]),
    (["runs/a.h5", "top.h5"], ["runs/a.h5", "top.h5"]),
    (["runs/2024", "runs/2024/x/c.h5"], ["runs/2024/b.h5", "runs/2024/x/c.h5"]),
    (["."], ["runs/a.h5", "runs/2024/b.h5", "runs/2024/x/c.h5", "runsx/d.h5",
             "top.h5"]),
])
def test_folders_select_every_catalogued_file_below_them(
        tmp_path, requested, expected):
    plan = plan_download(_folder_catalog(tmp_path), file_paths=requested)
    assert [item.relative_path.as_posix() for item in plan.items] == expected
    assert plan.unknown_paths == ()


def test_filters_narrow_a_folder_selection(tmp_path):
    plan = plan_download(_folder_catalog(tmp_path), file_paths=["runs"],
                         filter="**/2024/*.h5")
    assert [item.relative_path.as_posix() for item in plan.items] == [
        "runs/2024/b.h5"]


def test_unknown_paths_and_empty_folders_fail_before_contacting_server(
        tmp_path, monkeypatch):
    catalog = _folder_catalog(tmp_path)
    plan = plan_download(catalog, file_paths=[
        "runs/a.h5", "missing.h5", "run", "empty/", "runs/a.h5/inner"])
    assert [item.relative_path.as_posix() for item in plan.items] == ["runs/a.h5"]
    assert plan.unknown_paths == ("missing.h5", "run", "empty", "runs/a.h5/inner")
    assert "4 path(s) not in the catalog" in plan.summary()

    def reject(*args, **kwargs):
        raise AssertionError("unknown paths must fail before contacting a server")

    monkeypatch.setattr("hallmark.remote.download.OperationContext", reject)
    monkeypatch.setattr("hallmark.remote.download.RemoteSpec", SimpleNamespace(
        from_url=reject))
    for approved in (True, False):
        with pytest.raises(DownloadError, match="missing.h5, run, empty") as error:
            execute_download_plan(catalog, plan, approved=approved)
        assert "nothing was downloaded" in str(error.value)
    assert not (tmp_path / "runs").exists()


def test_sizes_survive_nullable_numeric_tsv_serialization(catalog):
    frame = catalog.state.data.copy()
    frame["size_bytes"] = [6, None, 0]
    frame.to_csv(catalog.dothm.path / "data.tsv", sep="\t", index=False)
    plan = plan_download(catalog)
    assert [item.size_bytes for item in plan.items] == [6, None, 0]


def test_plan_filters_without_authorizing_download(catalog):
    plan = plan_download(catalog, filter="**/*.fits")
    assert [item.relative_path.as_posix() for item in plan.items] == [
        "nested/a.fits", "empty.fits"]
    assert plan.total_bytes == 6
    with pytest.raises(DownloadError, match="approval"):
        execute_download_plan(catalog, plan)


def test_duration_requires_supplied_rate_and_complete_sizes(catalog):
    known = plan_download(catalog, file_paths="nested/a.fits",
                          estimated_bytes_per_second=3)
    assert known.estimated_seconds == 2
    unknown = plan_download(catalog, estimated_bytes_per_second=3)
    assert unknown.estimated_seconds is None
    with pytest.raises(ValueError, match="positive rate"):
        replace(known, estimated_bytes_per_second=float("nan"))


def test_plan_is_immutable_and_copies_input_sequences(tmp_path):
    checksum = ["sha256", "a" * 64]
    item = DownloadItem(Path("a.bin"), checksum)
    items = [item]
    plan = DownloadPlan(items, "https://source.test/", tmp_path)
    checksum[1] = "b" * 64
    items.clear()
    assert plan.file_count == 1
    assert plan.items[0].checksum == ("sha256", "a" * 64)
    with pytest.raises(FrozenInstanceError):
        plan.remote_url = "https://different.test/"
    with pytest.raises(FrozenInstanceError):
        plan.items[0].size_bytes = 4


def test_plan_display_redacts_url_credentials_without_changing_source(tmp_path):
    url = "https://user:secret@example.test:8443/data/?token=secret#secret"
    plan = DownloadPlan((DownloadItem(Path("a.bin")),), url, tmp_path)
    assert plan.remote_url == url
    assert "Source: https://example.test:8443/data/" in plan.summary()
    assert "secret" not in plan.summary()
    assert "user" not in plan.summary()
    assert "secret" not in repr(plan)


@pytest.mark.parametrize("approval", [False, None, 1, "yes"])
def test_unapproved_execution_never_opens_transport(catalog, monkeypatch, approval):
    plan = plan_download(catalog)
    def reject_open(*args, **kwargs):
        raise AssertionError("unapproved transfer opened its transport")
    monkeypatch.setattr("hallmark.remote.download.OperationContext", reject_open)
    with pytest.raises(DownloadError, match="approval"):
        execute_download_plan(catalog, plan, approved=approval)


@pytest.mark.parametrize("known_size", [True, False])
@pytest.mark.parametrize("transfer_fails", [False, True])
def test_approved_execution_uses_pinned_source_and_byte_progress(
        catalog, monkeypatch, known_size, transfer_fails):
    plan = plan_download(catalog, file_paths="nested/a.fits")
    if not known_size:
        plan = replace(plan, items=(replace(plan.items[0], size_bytes=None),))
    catalog.state.config["remote"]["url"] = "https://changed.test/"
    (catalog.dothm.path / "data.tsv").write_text("path\nother.bin\n")
    opened = []
    ticks = []
    progress_options = []
    completion_counts = []

    class Progress:
        def __init__(self, **kwargs):
            progress_options.append(kwargs)

        def update(self, count):
            ticks.append(count)

        def set_postfix(self, **kwargs):
            completion_counts.append(kwargs)

        def close(self):
            pass

    class Context:
        def __init__(self, remote, output_root):
            opened.append(remote)
            self.output_root = output_root
            self.transport = self
            self.on_bytes = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def prepare(self):
            pass

        def check_cancelled(self):
            pass

        def fetch(self, relative_path, destination, **kwargs):
            assert relative_path == "nested/a.fits"
            with destination.open("wb") as handle:
                for chunk in (b"abc", b"def"):
                    handle.write(chunk)
                    self.on_bytes(len(chunk))
            if transfer_fails:
                raise DownloadError("interrupted transfer")

    monkeypatch.setattr("hallmark.remote.download.OperationContext", Context)
    monkeypatch.setattr("hallmark.remote.download.tqdm", Progress)
    result = execute_download_plan(catalog, plan, approved=True, show_progress=True)
    assert opened[0].url == "https://source.test/data/"
    assert ticks == [3, 3]
    assert progress_options[0]["total"] == (6 if known_size else None)
    assert progress_options[0]["unit"] == "B"
    assert completion_counts[-1] == {"files": "1/1", "failed": int(transfer_fails)}
    if transfer_fails:
        assert result["failed"] == 1
        assert not (catalog.worktree / "nested/a.fits").exists()
    else:
        assert result["total_bytes"] == 6
        assert (catalog.worktree / "nested/a.fits").read_bytes() == b"abcdef"


def test_execution_rechecks_destination_after_approval(catalog, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    plan = plan_download(catalog, output, file_paths="nested/a.fits")
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DownloadError, match="outside|symbolic link"):
        execute_download_plan(catalog, plan, approved=True)
    assert not (outside / "a.fits").exists()


def test_execution_rejects_changed_output_root(catalog, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    plan = plan_download(catalog, output, file_paths="nested/a.fits")
    output.rmdir()
    output.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DownloadError, match="destination changed"):
        execute_download_plan(catalog, plan, approved=True)


def test_empty_plan_needs_no_approval(catalog):
    plan = plan_download(catalog, filter="**/*.absent")
    assert execute_download_plan(catalog, plan) == {
        "succeeded": 0, "failed": 0, "total_bytes": 0, "errors": []}


def test_bare_catalog_requires_explicit_output(catalog):
    catalog.worktree = None
    with pytest.raises(DownloadError, match="output_path"):
        plan_download(catalog)


def test_plan_pins_backend_and_nested_options_without_loading_plugin(
        catalog, monkeypatch):
    options = {"servers": [{"url": "https://original.test/"}]}
    catalog.state.config["remote"].update(
        backend="external-survey", backend_options=options)

    def reject_network(*args, **kwargs):
        raise AssertionError("planning must not load or contact a backend")

    monkeypatch.setattr("hallmark.remote.download.OperationContext", reject_network)
    plan = plan_download(catalog, file_paths="nested/a.fits")
    options["servers"][0]["url"] = "https://changed.test/"
    catalog.state.config["remote"]["backend"] = "another-survey"
    assert plan.remote_backend == "external-survey"
    assert plan.backend_options["servers"][0]["url"] == "https://original.test/"
    with pytest.raises(TypeError):
        plan.backend_options["servers"][0]["url"] = "https://changed.test/"
    with pytest.raises(TypeError):
        plan.backend_options["extra"] = True
    assert hash(plan) == hash(replace(plan))
    captured = []

    def download(source, *args, **kwargs):
        captured.append(source)
        return {"succeeded": 1, "failed": 0, "total_bytes": 6, "errors": []}

    monkeypatch.setattr("hallmark.remote.download._download_selected", download)
    execute_download_plan(catalog, plan, approved=True)
    assert captured[0].backend == "external-survey"
    assert captured[0].backend_options["servers"][0]["url"] == \
        "https://original.test/"


def test_legacy_remote_pins_default_backend(catalog):
    assert plan_download(catalog).remote_backend == "http"


def test_missing_plugin_can_be_planned_but_not_executed(catalog):
    catalog.state.config["remote"]["backend"] = "missing-survey-plugin"
    plan = plan_download(catalog, file_paths="nested/a.fits")
    with pytest.raises(DownloadError, match="missing-survey-plugin"):
        execute_download_plan(catalog, plan, approved=True)
    assert not (catalog.worktree / "nested/a.fits").exists()


def _reject_server(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("this download must not contact a server")

    monkeypatch.setattr("hallmark.remote.download.OperationContext", reject)
    monkeypatch.setattr("requests.sessions.Session.request", reject)


def test_verified_existing_files_are_skipped_without_a_transfer(
        catalog, monkeypatch):
    (catalog.worktree / "nested").mkdir()
    existing = catalog.worktree / "nested/a.fits"
    existing.write_bytes(b"abcdef")
    _reject_server(monkeypatch)
    plan = plan_download(catalog, file_paths=["nested"])
    assert plan.items == ()
    assert [item.relative_path.as_posix() for item in plan.skipped] == [
        "nested/a.fits"]
    assert plan.conflicts == ()
    assert "1 file(s) already downloaded; skipped" in plan.summary()
    assert execute_download_plan(catalog, plan) == {
        "succeeded": 0, "failed": 0, "total_bytes": 0, "errors": []}
    assert existing.read_bytes() == b"abcdef"


@pytest.mark.parametrize("make_existing, reason", [
    (lambda path: path.write_bytes(b"ABCDEF"), "different contents"),
    (lambda path: path.write_bytes(b"changed"), "7 bytes, but the catalog records 6"),
    (lambda path: path.mkdir(), "not a regular file"),
])
def test_conflicting_existing_file_stops_the_whole_download(
        catalog, monkeypatch, make_existing, reason):
    (catalog.worktree / "nested").mkdir()
    existing = catalog.worktree / "nested/a.fits"
    make_existing(existing)
    plan = plan_download(catalog, file_paths=["nested/a.fits", "empty.fits"])
    assert [item.relative_path.as_posix() for item in plan.items] == ["empty.fits"]
    assert [(conflict.item.relative_path.as_posix(), conflict.reason)
            for conflict in plan.conflicts] == [("nested/a.fits", reason)]
    assert "1 existing file(s) conflict with the catalog" in plan.summary()
    _reject_server(monkeypatch)
    with pytest.raises(DownloadError) as error:
        execute_download_plan(catalog, plan, approved=True)
    assert "nested/a.fits" in str(error.value)
    assert "nothing was downloaded" in str(error.value)
    assert "Delete conflicting files first to replace them" in str(error.value)
    assert not (catalog.worktree / "empty.fits").exists()


def test_existing_file_without_catalog_checksum_is_a_conflict(catalog):
    (catalog.worktree / "b.txt").write_bytes(b"cannot be verified")
    plan = plan_download(catalog, file_paths=["b.txt"])
    assert plan.items == ()
    assert plan.conflicts[0].item.relative_path == Path("b.txt")
    assert plan.conflicts[0].reason == "no catalog checksum or size to verify it"


def test_unverifiable_file_with_the_catalog_size_is_skipped(catalog, monkeypatch):
    present = catalog.worktree / "empty.fits"
    present.write_bytes(b"")
    plan = plan_download(catalog, file_paths=["empty.fits", "b.txt"])
    assert [item.relative_path.as_posix() for item in plan.unverified] == [
        "empty.fits"]
    assert [item.relative_path.as_posix() for item in plan.items] == ["b.txt"]
    assert plan.skipped == () and plan.conflicts == ()
    assert "1 file(s) present (size matches, not verified); skipped" in plan.summary()
    present.write_bytes(b"grown")
    plan = plan_download(catalog, file_paths=["empty.fits"])
    assert [(conflict.item.relative_path.as_posix(), conflict.reason)
            for conflict in plan.conflicts] == [
        ("empty.fits", "5 bytes, but the catalog records 0")]


def test_wrong_size_is_a_conflict_without_reading_the_file(catalog, monkeypatch):
    (catalog.worktree / "nested").mkdir()
    (catalog.worktree / "nested/a.fits").write_bytes(b"x" * 10)

    def reject(*args, **kwargs):
        raise AssertionError("a file of the wrong size must not be hashed")

    monkeypatch.setattr("hallmark.remote.download.calculate_file_checksum", reject)
    plan = plan_download(catalog, file_paths=["nested/a.fits"])
    assert plan.conflicts[0].reason == "10 bytes, but the catalog records 6"


@pytest.mark.parametrize("digest, content, expected", [
    (sha256(b"abcdef").hexdigest(), b"abcdef", "skipped"),
    (sha256(b"abcdef").hexdigest(), b"ABCDEF", "different contents"),
    ("a" * 36, b"abcdef", "unverified"),
])
def test_unknown_checksum_algorithm_is_inferred_from_the_digest_length(
        tmp_path, digest, content, expected):
    catalog = _folder_catalog(tmp_path)
    frame = pd.DataFrame([{"path": "run.h5", "checksum_algorithm": "unknown",
                           "checksum": digest, "size_bytes": "6"}])
    frame.to_csv(catalog.dothm.path / "data.tsv", sep="\t", index=False)
    (tmp_path / "run.h5").write_bytes(content)
    plan = plan_download(catalog, file_paths=["run.h5"])
    if expected == "skipped":
        assert plan.skipped[0].checksum == ("sha256", digest)
    elif expected == "unverified":
        assert plan.unverified[0].checksum is None
    else:
        assert plan.conflicts[0].reason == expected


def test_existing_files_are_hashed_with_a_bounded_pool(catalog, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    workers = []

    def pool(max_workers=None):
        workers.append(max_workers)
        return ThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr("hallmark.remote.download.ThreadPoolExecutor", pool)
    monkeypatch.setattr("hallmark.remote.download.os.cpu_count", lambda: 64)
    (catalog.worktree / "empty.fits").write_bytes(b"")
    plan_download(catalog, file_paths=["empty.fits"])
    assert workers == [8]


def test_file_appearing_after_planning_is_never_replaced(catalog, monkeypatch):
    plan = plan_download(catalog, file_paths=["nested/a.fits"])
    assert plan.file_count == 1
    late = catalog.worktree / "nested/a.fits"

    class Context:
        def __init__(self, remote, output_root):
            self.output_root = output_root
            self.transport = self
            self.on_bytes = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def prepare(self):
            pass

        def check_cancelled(self):
            pass

        def fetch(self, relative_path, destination, **kwargs):
            destination.write_bytes(b"abcdef")
            # Another program creates the file while the transfer runs.
            late.write_bytes(b"written meanwhile")

    monkeypatch.setattr("hallmark.remote.download.OperationContext", Context)
    result = execute_download_plan(catalog, plan, approved=True)
    assert result["succeeded"] == 0 and result["failed"] == 1
    assert "appeared after the download was planned" in result["errors"][0]
    assert late.read_bytes() == b"written meanwhile"
    assert not list(late.parent.glob("*.part"))
