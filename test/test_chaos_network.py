"""Network faults during remote scans and downloads.

A real loopback HTTP server misbehaves on chosen paths. Each failed file
must leave no partial destination or temporary file, other files must
still arrive intact, and a failed scan must leave the catalog unchanged.
"""

import hashlib
import os

import pytest

from chaos import check_invariants, snapshot_hm
from hallmark import Repo
import hallmark.transport.http as http_transport
from http_server import (BitFlip, NoLengthTruncate, NotAListing, Redirect,
                         ResetAfter, ShortBody, SlowDrip, Stall, Status, write_file)
from survey import Hm

pytestmark = [pytest.mark.chaos, pytest.mark.usefixtures("loopback_only")]

FILES = {f"rel/f{i}.bin": os.urandom(40000) for i in range(4)}

# Faults that the HTTP transport detects without any checksum.
TRANSPORT_FAULTS = {
    "reset": ResetAfter(1000),
    "short-body": ShortBody(1000),
    "500": Status(500),
    "503": Status(503),
    "429": Status(429, (("Retry-After", "1"),)),
    "404": Status(404),
    "stall-before-headers": Stall(5),
    "stall-mid-body": Stall(5, after_headers=True),
}
# Faults that only a published checksum can detect.
CONTENT_FAULTS = {
    "bit-flip": BitFlip(100),
    "unannounced-truncation": NoLengthTruncate(1000),
}


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(http_transport, "REMOTE_REQUEST_TIMEOUT", (2, 0.5))


@pytest.fixture
def release(tmp_path, http_server):
    """Serve FILES; ``publish_checksums`` adds a sha256 manifest."""
    www = tmp_path / "www"
    for path, data in FILES.items():
        write_file(www, path, data)

    def serve(publish_checksums=True, listing_style="apache"):
        if publish_checksums:
            write_file(www, "rel/sha256sum.txt", "".join(
                f"{hashlib.sha256(data).hexdigest()}  {path.split('/')[1]}\n"
                for path, data in FILES.items()))
        return http_server(www, listing_style=listing_style)

    return serve


def _catalog_repo(path, server):
    repo = Repo.init(path)
    repo.add(server.url("rel/") + "{name}.bin")
    repo.commit("Catalog release")
    return repo


def _download_all(repo, max_workers=2):
    plan = repo.plan_download(all_files=True)
    return repo.download(plan, approved=True, max_workers=max_workers)


def _assert_only_faulted_file_failed(repo, result, faulted, server):
    assert result["failed"] == 1, result
    assert result["succeeded"] == len(FILES) - 1, result
    for path, data in FILES.items():
        destination = repo.worktree / path.split("/", 1)[1]
        if path == faulted:
            assert not destination.exists()
        else:
            assert destination.read_bytes() == data
    errors = " ".join(map(str, dict(result["errors"]).values())
                      if isinstance(result["errors"], dict) else result["errors"])
    assert server.url("rel/") not in errors
    problems = check_invariants(repo.worktree)
    assert not problems, problems


@pytest.mark.parametrize("publish_checksums", [True, False],
                         ids=["checksums", "no-checksums"])
@pytest.mark.parametrize("fault", TRANSPORT_FAULTS.values(), ids=TRANSPORT_FAULTS)
def test_transport_fault_fails_only_that_file(tmp_path, release, fault,
                                              publish_checksums):
    server = release(publish_checksums)
    repo = _catalog_repo(tmp_path / "repo", server)
    server.faults.add("rel/f1.bin", fault)
    result = _download_all(repo)
    _assert_only_faulted_file_failed(repo, result, "rel/f1.bin", server)


@pytest.mark.parametrize("fault", CONTENT_FAULTS.values(), ids=CONTENT_FAULTS)
def test_published_checksum_rejects_damaged_content(tmp_path, release, fault):
    server = release(publish_checksums=True)
    repo = _catalog_repo(tmp_path / "repo", server)
    server.faults.add("rel/f2.bin", fault)
    result = _download_all(repo)
    _assert_only_faulted_file_failed(repo, result, "rel/f2.bin", server)


@pytest.mark.xfail(strict=True, reason=(
    "Bug: a body truncated without Content-Length is accepted when the "
    "catalog records the file size but no checksum (#54)"))
def test_catalogued_size_rejects_unannounced_truncation(tmp_path, release):
    server = release(publish_checksums=False, listing_style="apache")
    repo = _catalog_repo(tmp_path / "repo", server)
    server.faults.add("rel/f3.bin", NoLengthTruncate(1000))
    result = _download_all(repo)
    _assert_only_faulted_file_failed(repo, result, "rel/f3.bin", server)


def test_concurrent_faults_fail_independently(tmp_path, release):
    server = release(publish_checksums=True)
    repo = _catalog_repo(tmp_path / "repo", server)
    server.faults.add("rel/f0.bin", ResetAfter(10))
    server.faults.add("rel/f2.bin", BitFlip(0))
    server.faults.add("rel/f3.bin", SlowDrip(chunk=4096, delay=0.01))
    result = _download_all(repo, max_workers=4)
    assert (result["succeeded"], result["failed"]) == (2, 2), result
    assert (repo.worktree / "f3.bin").read_bytes() == FILES["rel/f3.bin"]
    assert not check_invariants(repo.worktree)


def test_rerun_after_transient_failure_completes(tmp_path, release):
    server = release(publish_checksums=True)
    repo = _catalog_repo(tmp_path / "repo", server)
    server.faults.add("rel/f1.bin", Status(503), times=1)
    assert _download_all(repo)["failed"] == 1
    plan = repo.plan_download(file_paths=["f1.bin"])
    assert repo.download(plan, approved=True)["succeeded"] == 1
    for path, data in FILES.items():
        assert (repo.worktree / path.split("/", 1)[1]).read_bytes() == data
    assert not check_invariants(repo.worktree)


SCAN_FAULTS = {
    "listing-500": ("rel/sub/", Status(500)),
    "listing-reset": ("rel/sub/", ResetAfter(10)),
    "listing-stall": ("rel/sub/", Stall(5)),
    "not-a-listing": ("rel/sub/", NotAListing()),
    "redirect-off-host": ("rel/sub/", Redirect("http://127.0.0.2:9/elsewhere/")),
    "redirect-outside-root": ("rel/sub/", Redirect("/other/")),
    "manifest-500": ("rel/sub/sha256sum.txt", Status(500)),
    "manifest-reset": ("rel/sub/sha256sum.txt", ResetAfter(5)),
}


@pytest.mark.parametrize("pattern, fault", SCAN_FAULTS.values(), ids=SCAN_FAULTS)
def test_failed_rescan_keeps_catalog_and_reports_cleanly(
        tmp_path, http_server, monkeypatch, pattern, fault):
    www = tmp_path / "www"
    for name in ("top.bin", "sub/a.bin", "sub/b.bin"):
        write_file(www, f"rel/{name}", os.urandom(64))
    write_file(www, "rel/sub/sha256sum.txt", "".join(
        f"{hashlib.sha256((www / 'rel/sub' / n).read_bytes()).hexdigest()}  {n}\n"
        for n in ("a.bin", "b.bin")))
    server = http_server(www)
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    hm = Hm()
    repo = tmp_path / "repo"
    hm("init", repo, cwd=tmp_path)
    url = server.url("rel/") + "{name}.bin"
    hm("add", url, cwd=repo)
    hm("commit", "-m", "Catalog release", cwd=repo)
    before = snapshot_hm(repo)

    server.faults.add(pattern, fault)
    result = hm("add", url, cwd=repo, expect=None)
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output
    assert snapshot_hm(repo) == before
    assert not check_invariants(repo)


@pytest.mark.ssh_integration
def test_unreadable_remote_folder_stops_ssh_scan(tmp_path, ssh_server):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read folders without permission")
    root = ssh_server["root"]
    for name in ("a/1.h5", "b/2.h5"):
        write_file(root, name, os.urandom(32))
    repo = Repo.init(tmp_path / "repo")
    repo.add(ssh_server["url"] + "{group}/{n:d}.h5")
    repo.commit("Catalog export")
    before = snapshot_hm(repo.worktree)
    (root / "b").chmod(0)
    try:
        with pytest.raises(Exception) as raised:
            repo.add(ssh_server["url"] + "{group}/{n:d}.h5")
    finally:
        (root / "b").chmod(0o755)
    assert "Traceback" not in str(raised.value)
    assert snapshot_hm(repo.worktree) == before
