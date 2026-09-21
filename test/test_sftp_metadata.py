"""Structured SFTP discovery, using a local subsystem without any network."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
import stat
import struct
import sys
import time

import pytest

from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport.base import (
    CapabilityError, DownloadError, RemoteConfigurationError,
    RemoteObjectMissing, TransferCancelled,
)
from hallmark.transport.sftp import _Packet


@pytest.fixture
def local_sftp(tmp_path, monkeypatch):
    """Connect the transport to a local SFTP subsystem without networking."""
    server = next((path for path in (
        "/usr/lib/openssh/sftp-server", "/usr/libexec/sftp-server",
    ) if Path(path).exists()), None)
    if server is None:
        pytest.skip("Local OpenSSH SFTP subsystem binary is unavailable")
    root = tmp_path / "source"
    root.mkdir()
    with OperationContext(RemoteSpec.parse(f"sftp://unused{root}/")) as context:
        transport = context.transport
        monkeypatch.setattr(transport, "prepare", lambda: None)
        spawn = transport._spawn

        def spawn_subsystem(argv, **kwargs):
            assert argv[-5:] == ["-T", "-s", "--", "unused", "sftp"]
            assert "BatchMode=yes" in argv
            assert "StrictHostKeyChecking=yes" in argv
            return spawn([server], **kwargs)

        monkeypatch.setattr(transport, "_spawn", spawn_subsystem)
        yield context, root


def test_structured_paths_sizes_progress_and_missing_metadata(local_sftp):
    context, root = local_sftp
    names = ["plain", "a b#?%+ü'\"[]*.fits", "-leading", " leading ", "literal%20"]
    for name in names:
        (root / name).write_text(name)
    (root / "nested").mkdir()
    (root / "nested" / "child").write_bytes(b"child")
    (root / "link").symlink_to(root / "nested", target_is_directory=True)
    directories = []
    iterator = context.transport.iter_entries(on_directory=directories.append)
    first = next(iterator)
    assert directories == [""]
    entries = [first, *iterator]
    expected = {name: len(name.encode()) for name in names}
    expected["nested/child"] = 5
    assert {entry.path: entry.size for entry in entries} == expected
    assert directories == ["", "nested/"]
    assert all(entry.mtime is not None for entry in entries)
    assert len(context.transport._processes) == 0
    assert context.transport.stat("plain").size == 5
    with pytest.raises(RemoteObjectMissing):
        context.transport.stat("missing")
    assert not context.transport._processes


def test_multiple_readdir_packets_and_cancellation_cleanup(local_sftp):
    context, root = local_sftp
    for index in range(1500):
        (root / f"file-{index:04d}").touch()
    assert len(list(context.transport.iter_entries())) == 1500
    iterator = context.transport.iter_entries()
    next(iterator)
    context.cancel()
    with pytest.raises(TransferCancelled):
        next(iterator)
    assert not context.transport._processes


def test_reject_controls_without_silently_omitting_file(local_sftp):
    context, root = local_sftp
    (root / "bad\nname").touch()
    with pytest.raises(RemoteConfigurationError, match="control"):
        list(context.transport.iter_entries())
    assert not context.transport._processes


def test_discovery_excludes_root_repository_metadata_and_objects(local_sftp):
    context, root = local_sftp
    for metadata in (".hm", ".git", ".HM", ".GIT"):
        (root / metadata / "objects").mkdir(parents=True)
        (root / metadata / "objects" / "payload").write_bytes(b"private")
    (root / "nested" / ".hm" / "objects").mkdir(parents=True)
    (root / "nested" / ".hm" / "objects" / "payload").write_bytes(b"private")
    (root / "science.fits").write_bytes(b"science")
    directories = []
    entries = list(context.transport.iter_entries(on_directory=directories.append))
    assert [entry.path for entry in entries] == ["science.fits"]
    assert directories == ["", "nested/"]


def test_metadata_file_size_limit_before_transfer(local_sftp, monkeypatch):
    context, root = local_sftp
    (root / "metadata").write_bytes(b"oversized")

    def unexpected(*args, **kwargs):
        raise AssertionError("Known oversized metadata must not be fetched")

    monkeypatch.setattr(context.transport, "_fetch", unexpected)
    with pytest.raises(DownloadError, match="size limit"):
        context.transport.read_text("metadata", 2)


def _fake_subsystem(monkeypatch, context, script):
    """Use a supplied script when starting an SFTP subsystem process."""
    transport = context.transport
    monkeypatch.setattr(transport, "prepare", lambda: None)
    spawn = transport._spawn
    monkeypatch.setattr(
        transport, "_spawn",
        lambda argv, **kw: spawn([sys.executable, "-c", script], **kw),
    )


@pytest.mark.parametrize("payload, message", [
    (struct.pack(">I", 2 ** 30), "oversized"),
    (struct.pack(">I", 5) + b"\x02\x00", "truncated"),
    (struct.pack(">IBI", 5, 2, 4), "version 3"),
])
def test_bad_handshake_is_bounded_and_cleans_process(monkeypatch, payload, message):
    with OperationContext(RemoteSpec.parse("sftp://unused/data")) as context:
        _fake_subsystem(
            monkeypatch, context,
            "import os; os.read(0, 9); os.write(1, " + repr(payload) + ")",
        )
        with pytest.raises((DownloadError, CapabilityError), match=message):
            with context.transport._metadata():
                pytest.fail("Invalid handshake was accepted")
        assert not context.transport._processes


def test_metadata_timeout_is_per_request_and_cancellable(monkeypatch):
    with OperationContext(RemoteSpec.parse("sftp://unused/data")) as context:
        _fake_subsystem(monkeypatch, context, "import time; time.sleep(30)")
        context.listing_timeout = 0.1
        with pytest.raises(DownloadError, match="time limit"):
            with context.transport._metadata():
                pass
        assert not context.transport._processes
        context.listing_timeout = 30
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(context.transport._metadata().__enter__)
            deadline = time.monotonic() + 2
            while not context.transport._processes and time.monotonic() < deadline:
                time.sleep(0.01)
            context.cancel()
            with pytest.raises(TransferCancelled):
                future.result(timeout=2)
        assert not context.transport._processes


def test_attributes_preserve_unknowns_and_reject_truncation():
    assert _Packet(struct.pack(">I", 0)).attributes() == {
        "size": None, "mode": None, "mtime": None,
    }
    data = struct.pack(">IQIII", 13, 2 ** 40, stat.S_IFREG | 0o600, 123, 456)
    assert _Packet(data).attributes() == {
        "size": 2 ** 40, "mode": stat.S_IFREG | 0o600, "mtime": 456,
    }
    with pytest.raises(DownloadError, match="Truncated"):
        _Packet(data[:-1]).attributes()
    with pytest.raises(DownloadError, match="flags"):
        _Packet(struct.pack(">I", 0x10)).attributes()


def test_missing_type_uses_lstat_and_cannot_escape_root(monkeypatch):
    class Session:
        escape = False

        def realpath(self, path):
            return "/outside" if self.escape and path.endswith("/nested") else path

        def lstat(self, path):
            mode = stat.S_IFREG if path.endswith("/file") else stat.S_IFDIR
            return {"size": 4, "mode": mode, "mtime": 123}

        def iterdir(self, path):
            if path == "/data":
                yield "nested", {"size": None, "mode": None, "mtime": None}
            else:
                yield "file", {"size": None, "mode": None, "mtime": None}

    with OperationContext(RemoteSpec.parse("sftp://unused/data")) as context:
        session = Session()
        monkeypatch.setattr(
            context.transport, "_metadata", lambda: nullcontext(session),
        )
        entries = list(context.transport.iter_entries())
        assert [(entry.path, entry.size) for entry in entries] == [("nested/file", 4)]
        session.escape = True
        with pytest.raises(DownloadError, match="escapes"):
            list(context.transport.iter_entries())


@pytest.mark.parametrize("kind, response_id, message", [
    (105, 999, "identifier"),
    (104, 1, "Unexpected"),
])
def test_wrong_response_cannot_be_treated_as_file_attributes(
    monkeypatch, kind, response_id, message,
):
    response = struct.pack(">BII", kind, response_id, 0)
    script = """import struct, sys
source, output = sys.stdin.buffer, sys.stdout.buffer
source.read(9)
output.write(struct.pack('>IBI', 5, 2, 3)); output.flush()
length = struct.unpack('>I', source.read(4))[0]
source.read(length)
payload = """ + repr(response) + """
output.write(struct.pack('>I', len(payload)) + payload); output.flush()
"""
    with OperationContext(RemoteSpec.parse("sftp://unused/data")) as context:
        _fake_subsystem(monkeypatch, context, script)
        with pytest.raises(DownloadError, match=message):
            with context.transport._metadata() as session:
                session.lstat("/data")
        assert not context.transport._processes
