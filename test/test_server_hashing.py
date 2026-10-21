"""Opt-in checksums computed by the server for files without published ones."""

import hashlib

import pytest

from hallmark import Repo
from hallmark.remote import backends
from hallmark.remote.backends import DataBackend, RemoteEntry
from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport import ssh as ssh_module
from hallmark.transport.base import (
    CapabilityError, DownloadError, TransferCancelled)
from hallmark.transport.ssh import parse_hash_output

A = hashlib.sha256(b"a").hexdigest()
B = hashlib.sha256(b"b").hexdigest()


def test_hash_output_is_matched_to_the_requested_paths():
    requested = {"/srv/x/a b.h5": "a b.h5", "/srv/x/c.h5": "c.h5"}
    output = f"{A}  /srv/x/a b.h5\n{B} */srv/x/c.h5\n".encode()
    assert parse_hash_output(output, requested, "sha256") == {
        "a b.h5": A, "c.h5": B}


@pytest.mark.parametrize("output, message", [
    (f"\\{A}  /srv/x/a\\nb.h5\n", "backslashes"),
    (f"{A}  /srv/x/other.h5\n", "Unexpected"),
    ("0123  /srv/x/a.h5\n", "Unexpected"),
    ("", "no checksum"),
])
def test_unexpected_hash_output_is_refused(output, message):
    with pytest.raises(DownloadError, match=message):
        parse_hash_output(output.encode(), {"/srv/x/a.h5": "a.h5"}, "sha256")


@pytest.fixture
def ssh_context(monkeypatch):
    calls = []

    def run(self, argv, *, timeout, data=b"", limit=65536, **kwargs):
        calls.append((argv, data))
        names = [name.decode() for name in data.split(b"\0") if name]
        return "".join(f"{A}  {name}\n" for name in names).encode()

    monkeypatch.setattr(ssh_module.SshBackend, "prepare", lambda self: None)
    monkeypatch.setattr(ssh_module.SshBackend, "_run_ssh_command", run)
    with OperationContext(RemoteSpec.from_url("ssh://lab/srv/export/")) as context:
        yield context, calls


def test_ssh_hashing_sends_paths_on_stdin_to_a_fixed_command(ssh_context,
                                                            monkeypatch):
    context, calls = ssh_context
    monkeypatch.setattr(ssh_module, "HASH_BATCH", 2)
    digests = context.transport.server_checksums(
        ["a.h5", "sub/$(id).h5", "c d.h5"], "sha256")
    assert digests == {"a.h5": A, "sub/$(id).h5": A, "c d.h5": A}
    assert len(calls) == 2
    argv, data = calls[0]
    assert argv[-3:] == ["--", "lab", "xargs -0 sha256sum --"]
    assert data == b"/srv/export/a.h5\0/srv/export/sub/$(id).h5\0"


def test_ssh_hashing_failures_are_capability_errors(ssh_context, monkeypatch):
    context, _ = ssh_context

    def refuse(self, *args, **kwargs):
        raise DownloadError("SSH operation failed")

    monkeypatch.setattr(ssh_module.SshBackend, "_run_ssh_command", refuse)
    with pytest.raises(CapabilityError, match="SFTP-only"):
        context.transport.server_checksums(["a.h5"], "sha256")

    def cancelled(self, *args, **kwargs):
        raise TransferCancelled("Download cancelled")

    monkeypatch.setattr(ssh_module.SshBackend, "_run_ssh_command", cancelled)
    with pytest.raises(TransferCancelled):
        context.transport.server_checksums(["a.h5"], "sha256")


def test_other_backends_cannot_hash_on_the_server():
    with OperationContext(RemoteSpec.from_url("https://data.test/")) as context:
        with pytest.raises(CapabilityError, match="on the server"):
            context.transport.server_checksums(["a"], "sha256")


class HashingBackend(DataBackend):
    hashed = []

    def iter_entries(self, on_directory=None):
        yield RemoteEntry("plain.h5", size=1)
        yield RemoteEntry("weak.h5", size=1, checksum_algorithm="md5",
                          checksum="0" * 32, checksum_source="published")
        yield RemoteEntry("strong.h5", size=1, checksum_algorithm="sha512",
                          checksum="1" * 128, checksum_source="published")

    def server_checksums(self, relative_paths, algorithm):
        HashingBackend.hashed.append((list(relative_paths), algorithm))
        return {path: A for path in relative_paths}


@pytest.fixture
def hashing_backend(monkeypatch):
    HashingBackend.hashed = []
    monkeypatch.setattr(backends, "_registered",
                        {**backends._registered, "hashing": HashingBackend})
    return HashingBackend


def test_add_hashes_only_files_without_an_equal_or_stronger_checksum(
        hashing_backend, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.add("https://lab.test/export/", backend="hashing", server_checksum="sha256")
    assert hashing_backend.hashed == [(["plain.h5", "weak.h5"], "sha256")]
    rows = repo.state.data.set_index("path")
    assert rows.loc["plain.h5", ["checksum_algorithm", "checksum",
                                 "checksum_source"]].tolist() == ["sha256", A, "server"]
    assert rows.loc["strong.h5", "checksum_source"] == "published"
    assert repo.state.config["sources"][0]["server_checksum"] == "sha256"
    # Adding the source again repeats its saved choice.
    repo.add("https://lab.test/export/")
    assert len(hashing_backend.hashed) == 2
    with pytest.raises(ValueError, match="server_checksum must be one of"):
        repo.add("https://lab.test/export/", server_checksum="crc32")
