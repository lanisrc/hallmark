"""Test download planning and approval using local catalog metadata."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from hallmark.download_plan import DownloadItem, DownloadPlan
from hallmark.downloader import (
    DownloadError,
    download_remote_data,
    execute_download_plan,
    plan_download,
    select_download_files,
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
    monkeypatch.setattr("hallmark.downloader.OperationContext", reject_network)
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
    plan = plan_download(catalog, file_paths=["nested/a.fits", "unlisted.bin"])
    assert plan.file_count == 2
    assert plan.items[0].checksum == ("sha256", sha256(b"abcdef").hexdigest())
    assert plan.items[0].size_bytes == 6
    assert plan.items[1].checksum is None
    assert plan.items[1].size_bytes is None
    assert select_download_files(catalog, file_paths=["nested/a.fits"]) == [
        (Path("nested/a.fits"), plan.items[0].checksum)]


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
    monkeypatch.setattr("hallmark.downloader.OperationContext", reject_open)
    with pytest.raises(DownloadError, match="approval"):
        execute_download_plan(catalog, plan, approved=approval)
    with pytest.raises(DownloadError, match="approval"):
        download_remote_data(catalog, catalog.worktree, approved=approval)


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

    monkeypatch.setattr("hallmark.downloader.OperationContext", Context)
    monkeypatch.setattr("hallmark.downloader.tqdm", Progress)
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
