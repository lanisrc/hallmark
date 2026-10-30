"""Edge cases of the SSH, SFTP, HTTP and WebDAV transports and local auth profiles.

Everything runs offline: SFTP sessions talk to local pipes, scripted replies or
short Python child processes, and HTTP requests go to a loopback server.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
import os
import selectors
import stat
import struct
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest
import requests
import yaml

from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport import secrets as secrets_module
from hallmark.transport.auth import (
    HttpSettings,
    load_auth_file,
    resolve_settings,
    resolve_ssh_settings,
)
from hallmark.transport.base import (
    AccessRefused,
    CapabilityError,
    DownloadError,
    RemoteConfigurationError,
    RemoteEntry,
    RemoteObjectMissing,
    TransferCancelled,
    decode_metadata,
    reject_url_credentials,
)
from hallmark.transport.http import HostBoundAuth, HttpBackend, _declared_charset
from hallmark.transport.index import _parse_directory_listing
from hallmark.transport.secrets import SecretRef
from hallmark.transport.sftp import _MAX_PACKET, SftpMetadata, _Packet
from hallmark.transport.ssh import SshBackend, parse_hash_output, quote_sftp_path
from http_server import LocalServer


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch, tmp_path):
    """Keep the user's auth file and .netrc out of every test."""
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))


@pytest.fixture
def sftp_context():
    with OperationContext(RemoteSpec.from_url("sftp://unused/data")) as context:
        yield context


def never_connect():
    raise AssertionError("This request must fail before connecting")


# --- SFTP packets -----------------------------------------------------------


def sftp_string(value):
    if isinstance(value, str):
        value = value.encode()
    return struct.pack(">I", len(value)) + value


def test_extended_attributes_are_read_past_and_ignored():
    data = (struct.pack(">IQI", 0x80000001, 5, 2)
            + sftp_string("acl") + sftp_string("rw")
            + sftp_string("xattr") + sftp_string(b"\x00\x01"))
    packet = _Packet(data)
    assert packet.read_attributes() == {"size": 5, "mode": None, "mtime": None}
    packet.check_packet_end()


def test_extended_attribute_counts_cannot_exceed_the_packet():
    packet = _Packet(struct.pack(">II", 0x80000000, 1000) + sftp_string("x"))
    with pytest.raises(DownloadError, match="attribute count"):
        packet.read_attributes()


def test_trailing_bytes_and_undecodable_names_are_refused():
    packet = _Packet(sftp_string("name") + b"extra")
    assert packet.read_filename() == "name"
    with pytest.raises(DownloadError, match="trailing"):
        packet.check_packet_end()
    with pytest.raises(DownloadError, match="valid UTF-8"):
        _Packet(sftp_string(b"\xff\xfe")).read_filename()


# --- SFTP requests answered with scripted replies ---------------------------


class ScriptedSftp(SftpMetadata):
    """Answer requests with prepared reply packets instead of a subsystem."""

    def __init__(self, transport, replies):
        super().__init__(transport)
        self.replies = list(replies)
        self.sent = []

    def _send_packet(self, payload):
        self.sent.append(payload)

    def _receive_packet(self):
        kind, body = self.replies.pop(0)
        return kind, _Packet(body)


def status_reply(request_id, code, message="server detail"):
    return 101, (struct.pack(">II", request_id, code) + sftp_string(message)
                 + sftp_string("en"))


def handle_reply(request_id, handle):
    return 102, struct.pack(">I", request_id) + sftp_string(handle)


def name_reply(request_id, names):
    body = struct.pack(">II", request_id, len(names))
    for name in names:
        body += sftp_string(name) + sftp_string("longname") + struct.pack(">I", 0)
    return 104, body


@pytest.mark.parametrize("code, error, message", [
    (2, RemoteObjectMissing, "does not exist"),
    (8, CapabilityError, "does not support"),
    (4, DownloadError, r"failed \(status 4\)"),
])
def test_sftp_status_codes_become_specific_errors(sftp_context, code, error, message):
    session = ScriptedSftp(sftp_context.transport,
                           [status_reply(1, code, "secret detail")])
    with pytest.raises(error, match=message) as raised:
        session.lstat("/data/item")
    # Server diagnostics are untrusted and never reach the message.
    assert "secret" not in str(raised.value)
    assert session.sent[0][0] == 7  # SSH_FXP_LSTAT


def test_canonical_path_replies_must_name_one_decodable_path(sftp_context):
    session = ScriptedSftp(sftp_context.transport, [
        name_reply(1, ["/data", "/other"]),
        name_reply(2, [b"/\xff"]),
        name_reply(3, ["/data/real"]),
    ])
    with pytest.raises(DownloadError, match="canonical path"):
        session.realpath("/data")
    with pytest.raises(DownloadError, match="valid UTF-8"):
        session.realpath("/data")
    assert session.realpath("/data/link") == "/data/real"


@pytest.mark.parametrize("handle", [b"", b"h" * 257])
def test_directory_handles_must_be_short_and_non_empty(sftp_context, handle):
    session = ScriptedSftp(sftp_context.transport, [handle_reply(1, handle)])
    with pytest.raises(DownloadError, match="directory handle"):
        list(session.iterdir("/data"))
    assert len(session.sent) == 1  # An unusable handle is never closed.


def test_empty_directory_batches_are_refused_and_the_handle_closed(sftp_context):
    session = ScriptedSftp(sftp_context.transport, [
        handle_reply(1, b"h"), name_reply(2, []), status_reply(3, 0)])
    with pytest.raises(DownloadError, match="entry count"):
        list(session.iterdir("/data"))
    assert [payload[0] for payload in session.sent] == [11, 12, 4]


def test_failing_to_close_a_finished_listing_is_reported(sftp_context):
    session = ScriptedSftp(sftp_context.transport, [
        handle_reply(1, b"h"), name_reply(2, ["a.fits"]), status_reply(3, 1),
        status_reply(4, 4)])
    names = []
    with pytest.raises(DownloadError, match="status 4"):
        for name, _ in session.iterdir("/data"):
            names.append(name)
    assert names == ["a.fits"]


def test_a_failed_close_does_not_hide_the_listing_error(sftp_context):
    session = ScriptedSftp(sftp_context.transport, [
        handle_reply(1, b"h"), status_reply(2, 4), status_reply(3, 8)])
    with pytest.raises(DownloadError, match="status 4") as raised:
        list(session.iterdir("/data"))
    assert not isinstance(raised.value, CapabilityError)
    assert [payload[0] for payload in session.sent] == [11, 12, 4]


def test_paths_too_long_for_one_request_packet_are_refused(sftp_context):
    session = SftpMetadata(sftp_context.transport)
    with pytest.raises(DownloadError, match="request exceeds its size limit"):
        session.lstat("/" + "a" * _MAX_PACKET)


# --- SFTP sessions attached to local pipes ----------------------------------


@pytest.fixture
def pipe_session(sftp_context):
    """Attach a metadata session to local pipes instead of an SSH process."""
    files = []

    def pipe():
        read, write = os.pipe()
        for fd in (read, write):
            os.set_blocking(fd, False)
        ends = (os.fdopen(read, "rb", buffering=0),
                os.fdopen(write, "wb", buffering=0))
        files.extend(ends)
        return ends

    stdin, stdout, stderr = pipe(), pipe(), pipe()
    session = SftpMetadata(sftp_context.transport)
    session.process = SimpleNamespace(
        stdin=stdin[1], stdout=stdout[0], stderr=stderr[0])
    selector = selectors.DefaultSelector()
    selector.register(stdout[0], selectors.EVENT_READ)
    selector.register(stderr[0], selectors.EVENT_READ)
    session.selector = selector
    session._start_request_timer()
    # The subsystem's ends: what it reads, and where it writes.
    far = SimpleNamespace(stdin=stdin[0], stdout=stdout[1], stderr=stderr[1])
    yield session, far
    selector.close()
    for handle in files:
        handle.close()


def test_subsystem_output_beyond_one_packet_is_refused(pipe_session):
    session, far = pipe_session
    session.buffer = bytearray(_MAX_PACKET + 4)
    far.stdout.write(b"x")
    with pytest.raises(DownloadError, match="packet exceeds its size limit"):
        session._poll_streams()


def test_ssh_diagnostics_are_counted_but_never_buffered(pipe_session):
    session, far = pipe_session
    warning = b"Warning: Permanently added the host key\n"
    far.stderr.write(warning)
    assert session._poll_streams() is False
    assert session.stderr_size == len(warning)
    assert session.buffer == bytearray()
    session.stderr_size = 65536
    far.stderr.write(b"x")
    with pytest.raises(DownloadError, match="diagnostics exceeded"):
        session._poll_streams()


class AlwaysReady:
    """Report one pipe as readable whether or not it holds data."""

    def __init__(self, fileobj):
        self.key = selectors.SelectorKey(
            fileobj, fileobj.fileno(), selectors.EVENT_READ, None)

    def select(self, timeout=None):
        return [(self.key, selectors.EVENT_READ)]


def test_spurious_readiness_of_an_empty_pipe_is_ignored(pipe_session):
    session, _ = pipe_session
    session.selector = AlwaysReady(session.process.stdout)
    assert session._poll_streams() is False
    assert session.buffer == bytearray()


def test_a_full_request_pipe_is_retried_until_the_subsystem_reads(pipe_session):
    session, far = pipe_session
    stdin = session.process.stdin
    # Fill the pipe, as a subsystem that stopped reading would. Single bytes
    # take the last space where a platform refuses partial large writes.
    for size in (65536, 1):
        while True:
            try:
                os.write(stdin.fileno(), b"\0" * size)
            except BlockingIOError:
                break

    def drain():
        while True:
            try:
                os.read(far.stdin.fileno(), 65536)
            except BlockingIOError:
                return

    polls = []

    class Selector:
        def register(self, fileobj, events):
            pass

        def unregister(self, fileobj):
            pass

        def select(self, timeout=None):
            polls.append(timeout)
            if len(polls) == 1:
                return []  # Not yet writable.
            if len(polls) == 3:
                drain()  # The subsystem catches up.
            key = selectors.SelectorKey(
                stdin, stdin.fileno(), selectors.EVENT_WRITE, None)
            return [(key, selectors.EVENT_WRITE)]

    session.selector = Selector()
    session._send_packet(b"\x01request")
    # Poll 2 found the pipe still full; poll 3 wrote the whole packet.
    assert len(polls) == 3
    assert os.read(far.stdin.fileno(), 65536) == struct.pack(">I", 8) + b"\x01request"


def test_a_closed_request_pipe_reports_a_disconnected_subsystem(pipe_session):
    session, far = pipe_session
    far.stdin.close()
    with pytest.raises(DownloadError, match="disconnected"):
        session._send_packet(b"\x01request")


# --- SFTP session lifecycle --------------------------------------------------


HANDSHAKE = """import os, struct, sys
os.read(0, 9)
os.write(1, struct.pack('>IBI', 5, 2, 3))
sys.stdin.buffer.read()
"""


def free_slots(transport, limit):
    """Count session slots that can be taken now, then give them back."""
    taken = 0
    while taken <= limit and transport._slots.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        transport._slots.release()
    return taken


def test_a_failed_subsystem_start_releases_its_session_slot(sftp_context,
                                                            monkeypatch):
    transport = sftp_context.transport
    monkeypatch.setattr(transport, "prepare", lambda: None)

    def fail(argv, **kwargs):
        raise DownloadError("Unable to start the OpenSSH client")

    monkeypatch.setattr(transport, "_start_process", fail)
    with pytest.raises(DownloadError, match="Unable to start"):
        with SftpMetadata(transport):
            pytest.fail("A session without a subsystem was opened")
    limit = sftp_context.settings.max_sessions
    assert free_slots(transport, limit) == limit


def test_waiting_for_a_free_session_slot_stops_when_cancelled(sftp_context,
                                                              monkeypatch):
    transport = sftp_context.transport
    limit = sftp_context.settings.max_sessions
    monkeypatch.setattr(transport, "prepare", lambda: None)
    started = []
    monkeypatch.setattr(transport, "_start_process",
                        lambda argv, **kwargs: started.append(argv))
    for _ in range(limit):
        assert transport._slots.acquire(blocking=False)
    try:
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(SftpMetadata(transport).__enter__)
            time.sleep(0.2)
            assert not waiting.done()
            sftp_context.cancel()
            with pytest.raises(TransferCancelled):
                waiting.result(timeout=5)
    finally:
        for _ in range(limit):
            transport._slots.release()
    assert started == []


def test_closing_a_metadata_session_twice_releases_its_slot_once(sftp_context,
                                                                 monkeypatch):
    transport = sftp_context.transport
    monkeypatch.setattr(transport, "prepare", lambda: None)
    spawn = transport._start_process
    monkeypatch.setattr(
        transport, "_start_process",
        lambda argv, **kwargs: spawn([sys.executable, "-c", HANDSHAKE], **kwargs))
    with SftpMetadata(transport) as session:
        assert session.process.poll() is None
    session.__exit__(None, None, None)
    assert not transport._processes
    limit = sftp_context.settings.max_sessions
    assert free_slots(transport, limit) == limit


# --- SSH commands and connections --------------------------------------------


def test_server_checksum_output_must_be_utf8():
    with pytest.raises(DownloadError, match="not UTF-8"):
        parse_hash_output(b"\xff  /srv/a.h5\n", {"/srv/a.h5": "a.h5"}, "sha256")


def test_sftp_batch_operands_must_be_absolute():
    assert quote_sftp_path("/srv/a b") == "/srv/a\\ b"
    with pytest.raises(RemoteConfigurationError, match="absolute"):
        quote_sftp_path("srv/data")


def test_ssh_backends_need_an_ssh_or_sftp_url():
    with OperationContext(RemoteSpec.from_url("https://data.test/x/")) as context:
        with pytest.raises(RemoteConfigurationError, match="SSH/SFTP URL"):
            SshBackend(context)


def test_an_ssh_command_is_stopped_at_its_time_limit(sftp_context):
    transport = sftp_context.transport
    started = time.monotonic()
    with pytest.raises(DownloadError, match="exceeded its time limit"):
        transport._run_ssh_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
    assert time.monotonic() - started < 10
    assert not transport._processes


def test_a_metadata_download_stops_once_it_passes_its_limit(sftp_context,
                                                            tmp_path):
    transport = sftp_context.transport
    destination = tmp_path / "metadata"
    script = ("import pathlib, sys, time; "
              "pathlib.Path(sys.argv[1]).write_bytes(b'x' * 100); time.sleep(30)")
    with pytest.raises(DownloadError, match="Remote text exceeds"):
        transport._run_ssh_command(
            [sys.executable, "-c", script, str(destination)], timeout=20,
            destination=destination, file_limit=10)
    assert not transport._processes


def test_a_disconnected_master_is_reported_before_reuse(sftp_context):
    transport = sftp_context.transport
    master = transport._start_process([sys.executable, "-c", "pass"])
    master.wait(timeout=10)
    transport._master = master
    with pytest.raises(DownloadError, match="master disconnected"):
        transport.prepare()


def test_missing_openssh_clients_are_reported_before_connecting(sftp_context,
                                                                monkeypatch):
    monkeypatch.setattr("hallmark.transport.ssh.shutil.which", lambda name: None)
    with pytest.raises(CapabilityError, match="Install OpenSSH"):
        sftp_context.transport.prepare()
    assert sftp_context.transport._socket_dir is None


def test_an_overlong_control_socket_path_is_refused(sftp_context, monkeypatch,
                                                    tmp_path):
    folder = tmp_path / ("long" * 30)
    folder.mkdir()
    make = tempfile.TemporaryDirectory
    monkeypatch.setattr("hallmark.transport.ssh.tempfile.TemporaryDirectory",
                        lambda prefix, dir: make(prefix=prefix, dir=folder))
    backend = SshBackend(sftp_context, ssh=[sys.executable, "-c", "pass"])
    try:
        with pytest.raises(CapabilityError, match="socket path is too long"):
            backend.prepare()
        assert backend._master is None
        assert not backend._processes
    finally:
        backend.close()
    assert list(folder.iterdir()) == []


def test_an_unanswered_connection_is_bounded_by_its_timeout(sftp_context):
    sftp_context.settings = replace(sftp_context.settings, connect_timeout=0)
    # Injected clients skip the version check; this one never opens a socket.
    backend = SshBackend(
        sftp_context, ssh=[sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(DownloadError, match="SSH connection exceeded"):
            backend.prepare()
        assert backend._master is None
        assert backend._socket_dir is None
        assert not backend._processes
    finally:
        backend.close()


def test_overlong_sftp_batches_are_refused_before_connecting(sftp_context,
                                                             monkeypatch,
                                                             tmp_path):
    monkeypatch.setattr(sftp_context.transport, "prepare", never_connect)
    with pytest.raises(RemoteConfigurationError, match="client path limit"):
        sftp_context.transport.fetch("deep/" * 1700 + "file", tmp_path / "copy")


def test_ipv6_hosts_are_bracketed_for_the_sftp_client(monkeypatch, tmp_path):
    calls = []
    with OperationContext(RemoteSpec.from_url("sftp://[::1]/srv/data/")) as context:
        transport = context.transport
        monkeypatch.setattr(transport, "prepare", lambda: None)
        monkeypatch.setattr(transport, "_run_ssh_command",
                            lambda argv, **kwargs: calls.append((argv, kwargs)))
        transport.fetch("a b.fits", tmp_path / "copy")
    argv, options = calls[0]
    assert argv[-4:] == ["-b", "-", "--", "[::1]"]
    local = quote_sftp_path(tmp_path / "copy")
    assert options["data"] == f"get /srv/data/a\\ b.fits {local}\n".encode()
    assert options["report_progress"] is True


def test_unknown_server_checksum_algorithms_are_refused(sftp_context,
                                                        monkeypatch):
    monkeypatch.setattr(sftp_context.transport, "prepare", never_connect)
    with pytest.raises(RemoteConfigurationError, match="'crc32'"):
        sftp_context.transport.server_checksums(["a.h5"], "crc32")


def test_metadata_that_grows_during_transfer_is_refused(sftp_context,
                                                        monkeypatch):
    transport = sftp_context.transport
    # The server did not report a size, so only the download can enforce it.
    monkeypatch.setattr(transport, "stat", lambda path: RemoteEntry(path))
    payloads = [b"x" * 5, b"abcd"]

    def download(path, destination, file_limit=None):
        assert file_limit == 4
        destination.write_bytes(payloads.pop(0))

    monkeypatch.setattr(transport, "_download_file", download)
    with pytest.raises(DownloadError, match="exceeds its size limit"):
        transport.read_text("config.yml", 4)
    assert transport.read_text("config.yml", 4) == "abcd"


# --- SFTP discovery with an in-memory session --------------------------------


def attributes(mode, size=None):
    return {"size": size, "mode": mode, "mtime": None if mode is None else 123}


FILE = attributes(stat.S_IFREG | 0o644, 4)
DIRECTORY = attributes(stat.S_IFDIR | 0o755)


class FakeSftp:
    """Answer metadata requests from in-memory directory listings."""

    def __init__(self, listings, overrides=None, realpaths=None):
        self.listings = listings
        self.overrides = overrides or {}
        self.realpaths = realpaths or {}

    def realpath(self, path):
        return self.realpaths.get(path, path)

    def lstat(self, path):
        if path in self.overrides:
            return self.overrides[path]
        return DIRECTORY if path in self.listings else FILE

    def iterdir(self, path):
        yield from self.listings.get(path, [])


def walk(context, monkeypatch, session):
    monkeypatch.setattr(context.transport, "_open_metadata_session",
                        lambda: nullcontext(session))
    directories = []
    entries = context.transport.iter_entries(on_directory=directories.append)
    return [entry.path for entry in entries], directories


@pytest.mark.parametrize("mode", [stat.S_IFDIR | 0o755, None])
def test_stat_accepts_only_regular_files(sftp_context, monkeypatch, mode):
    session = FakeSftp({}, overrides={"/data/item": attributes(mode)})
    monkeypatch.setattr(sftp_context.transport, "_open_metadata_session",
                        lambda: nullcontext(session))
    with pytest.raises(DownloadError, match="regular file"):
        sftp_context.transport.stat("item")


def test_directory_loops_are_listed_once(sftp_context, monkeypatch):
    session = FakeSftp(
        {"/data": [("loop", DIRECTORY), ("file", FILE)], "/data/loop": []},
        realpaths={"/data/loop": "/data"})
    assert walk(sftp_context, monkeypatch, session) == (["file"], [""])


@pytest.mark.parametrize("canonical", ["data", "/data/../etc"])
def test_an_unsafe_canonical_root_is_refused(sftp_context, monkeypatch, canonical):
    session = FakeSftp({"/data": []}, realpaths={"/data": canonical})
    with pytest.raises(DownloadError, match="canonical root"):
        walk(sftp_context, monkeypatch, session)


@pytest.mark.parametrize("listing, overrides, error, message", [
    ([], {"/data": attributes(None)}, CapabilityError, "omitted directory type"),
    ([], {"/data": FILE}, DownloadError, "must be a directory"),
    ([("x", attributes(None))], {"/data/x": attributes(None)}, CapabilityError,
     "omitted file type"),
    ([("x", FILE), ("x", FILE)], {}, DownloadError, "duplicate"),
    ([("a/b", FILE)], {}, DownloadError, "duplicate"),
    ([("", FILE)], {}, DownloadError, "duplicate"),
])
def test_untyped_or_invalid_listings_are_refused(sftp_context, monkeypatch,
                                                 listing, overrides, error,
                                                 message):
    session = FakeSftp({"/data": listing}, overrides=overrides)
    with pytest.raises(error, match=message):
        walk(sftp_context, monkeypatch, session)


# --- Local auth profiles -------------------------------------------------------


def write_auth(monkeypatch, tmp_path, document, mode=0o600):
    path = tmp_path / "auth.yml"
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    path.write_text(text)
    os.chmod(path, mode)
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(path))
    return path


@pytest.mark.parametrize("profile, message", [
    ({"hosts": ["campus"], "port": 70000}, "at most 65535"),
    ({"hosts": ["campus"], "identity_file": ""}, "local path"),
    ({"hosts": ["campus"], "identity_file": ["~/.ssh/id"]}, "local path"),
    ({"hosts": ["fe80::zz"]}, "Invalid IPv6 SSH host"),
])
def test_invalid_ssh_profile_values_fail_before_connecting(monkeypatch, tmp_path,
                                                           profile, message):
    write_auth(monkeypatch, tmp_path, {"version": 1, "profiles": {"lab": profile}})
    with pytest.raises(RemoteConfigurationError, match=message):
        resolve_ssh_settings(RemoteSpec.from_url("ssh://campus/data"))


@pytest.mark.parametrize("defaults", [
    {"user": "alice"}, {"port": 2222}, {"identity_file": "~/.ssh/id"}])
def test_global_defaults_cannot_choose_an_identity(monkeypatch, tmp_path,
                                                   defaults):
    write_auth(monkeypatch, tmp_path, {"version": 1, "defaults": defaults})
    with pytest.raises(RemoteConfigurationError, match="cannot select an identity"):
        resolve_ssh_settings(RemoteSpec.from_url("ssh://campus/data"))


@pytest.mark.parametrize("document, message", [
    ("version: [1\n", "Unable to read"),
    ("- version: 1\n", "Unable to read"),
    ("", "version: 1 or version: 2"),
    ("version: 3\n", "version: 1 or version: 2"),
    ("version: true\n", "version: 1 or version: 2"),
    ("version: 1\nprofile: {}\n", "version: 1 or version: 2"),
    ("version: 1\nprofiles: [lab]\n", "profiles must be a mapping"),
    ("version: 1\nprofiles:\n  lab: campus\n", "Each auth profile must be"),
])
def test_malformed_auth_files_are_refused(monkeypatch, tmp_path, document,
                                          message):
    write_auth(monkeypatch, tmp_path, document)
    with pytest.raises(RemoteConfigurationError, match=message):
        load_auth_file()


def test_a_configured_or_required_auth_file_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(tmp_path / "missing.yml"))
    with pytest.raises(RemoteConfigurationError, match="Unable to read"):
        load_auth_file()
    monkeypatch.delenv("HALLMARK_AUTH_FILE")
    assert load_auth_file() is None
    with pytest.raises(RemoteConfigurationError, match="Unable to read"):
        load_auth_file(required=True)


S3 = {"type": "s3", "buckets": ["ehtc-sims"]}
KEYS = {"access_key_id": {"env": "HM_KEY"}, "secret_access_key": {"env": "HM_SECRET"}}


def s3_remote():
    return RemoteSpec.from_url("s3://ehtc-sims/m87/", "osn")


@pytest.mark.parametrize("profile, message", [
    ({"type": "s3"}, "needs buckets as a list"),
    ({**S3, "buckets": [""]}, "needs buckets as a list"),
    ({**S3, "buckets": "ehtc-sims"}, "needs buckets as a list"),
    ({**S3, "endpoints": "https://osn.test"}, "needs endpoints as a list"),
    ({**S3, "buckets": ["other-bucket"]}, "not bound to bucket ehtc-sims"),
    ({**S3, "aws_profile": ""}, "aws_profile must be a name"),
    ({**S3, "region": "us-east-1"}, "Unsupported fields .*: region"),
    ({**S3, **KEYS, "aws_profile": "research"}, "either aws_profile or keys"),
    ({**S3, "access_key_id": {"env": "HM_KEY"}}, "needs both"),
    ({**S3, "secret_access_key": {"env": "HM_SECRET"}}, "needs both"),
])
def test_invalid_s3_profiles_fail_before_any_request(monkeypatch, tmp_path,
                                                     profile, message):
    write_auth(monkeypatch, tmp_path, {"version": 2, "profiles": {"osn": profile}})
    with pytest.raises(RemoteConfigurationError, match=message):
        resolve_settings(s3_remote(), "s3")


def test_s3_profiles_may_add_a_session_token(monkeypatch, tmp_path):
    write_auth(monkeypatch, tmp_path, {"version": 2, "profiles": {"osn": {
        **S3, **KEYS, "session_token": {"env": "HM_TOKEN"}}}})
    settings = resolve_settings(s3_remote(), "s3")
    assert settings.session_token.target == ("HM_TOKEN",)
    assert (settings.endpoints, settings.aws_profile) == ((), None)


# --- Secret references -----------------------------------------------------------


def command_ref(code):
    return SecretRef.parse({"command": [sys.executable, "-c", code]},
                           profile="lab", field="token")


def test_keyring_failures_are_reported_without_their_details(monkeypatch):
    def broken(service, username):
        raise RuntimeError("backend detail: hunter2")

    monkeypatch.setitem(sys.modules, "keyring",
                        SimpleNamespace(get_password=broken))
    ref = SecretRef.parse({"keyring": {"service": "hallmark", "username": "lab"}},
                          profile="lab", field="token")
    with pytest.raises(RemoteConfigurationError, match="could not provide") as error:
        ref.resolve()
    assert "hunter2" not in str(error.value)


def test_a_hung_secret_command_is_stopped(monkeypatch):
    monkeypatch.setattr(secrets_module, "COMMAND_TIMEOUT", 0.5)
    started = time.monotonic()
    with pytest.raises(RemoteConfigurationError, match="did not finish"):
        command_ref("import time; time.sleep(30)").resolve()
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("code, message", [
    ("print('x' * 70000)", "printed too much"),
    ("import sys; sys.stdout.buffer.write(b'\\xff\\n')", "must print UTF-8 text"),
])
def test_secret_commands_must_print_one_short_text_line(code, message):
    with pytest.raises(RemoteConfigurationError, match=message) as error:
        command_ref(code).resolve()
    assert "xxxx" not in str(error.value)


# --- Remote URLs and metadata text -------------------------------------------


def test_unparseable_urls_are_refused_without_echo():
    with pytest.raises(RemoteConfigurationError, match="Invalid remote URL") as error:
        reject_url_credentials("https://[token-123/data")
    assert "token-123" not in str(error.value)


def test_metadata_text_uses_the_declared_charset():
    assert decode_metadata(b"caf\xe9", "latin-1") == "café"
    with pytest.raises(DownloadError, match="unknown charset 'x-hallmark'"):
        decode_metadata(b"data", "x-hallmark")


@pytest.mark.parametrize("url, message", [
    ("s3://user@ehtc-sims/m87/", "only a bucket and prefix"),
    ("s3://ehtc-sims:9000/m87/", "only a bucket and prefix"),
    ("s3://ehtc-sims/m87/?versionId=1", "query or fragment"),
    ("s3://ehtc-sims/m87/#part", "query or fragment"),
    ("s3://eh/m87/", "Invalid S3 bucket"),
    ("s3://ehtc..sims/m87/", "Invalid S3 bucket"),
    ("s3://ehtc_sims/m87/", "Invalid S3 bucket"),
    ("s3://ehtc-sims/m87%ff/", "UTF-8"),
    ("s3://ehtc-sims/m87/../secret/", "traversal"),
    ("s3://ehtc-sims//m87/", "traversal"),
    ("s3://ehtc-sims/m87\\x/", "traversal"),
])
def test_s3_urls_name_only_a_bucket_and_a_safe_prefix(url, message):
    with pytest.raises(RemoteConfigurationError, match=message):
        RemoteSpec.from_url(url)


def test_s3_urls_keep_their_literal_prefix():
    assert RemoteSpec.from_url("s3://ehtc-sims").root == "/"
    assert RemoteSpec.from_url("s3://ehtc-sims/m87%20run/").root == "/m87 run/"


@pytest.mark.parametrize("url", [None, "", "   ", 42])
def test_remote_urls_must_be_non_empty_strings(url):
    with pytest.raises(RemoteConfigurationError, match="non-empty string"):
        RemoteSpec.from_url(url)


# --- HTTP ------------------------------------------------------------------------


def test_declared_charsets_are_found_among_other_parameters():
    assert _declared_charset('text/html; boundary=x; Charset="latin-1"') == "latin-1"
    assert _declared_charset("text/html; charset=") is None
    assert _declared_charset(None) is None


def test_profile_credentials_reach_only_their_hosts_over_https(monkeypatch):
    monkeypatch.setenv("HM_EDGE_USER", "alice")
    monkeypatch.setenv("HM_EDGE_PASSWORD", "pw")
    auth = HostBoundAuth(HttpSettings(
        profile="lab", hosts=("data.example.org",), scheme="basic",
        username=SecretRef("lab", "username", "env", ("HM_EDGE_USER",)),
        password=SecretRef("lab", "password", "env", ("HM_EDGE_PASSWORD",))))

    def header(url):
        request = auth(requests.Request("GET", url).prepare())
        return request.headers.get("Authorization")

    expected = "Basic " + base64.b64encode(b"alice:pw").decode()
    assert header("https://data.example.org/a.fits") == expected
    assert header("https://elsewhere.example.org/a.fits") is None
    assert header("http://data.example.org/a.fits") is None


def test_http_backends_need_an_http_url(sftp_context):
    with pytest.raises(RemoteConfigurationError, match="HTTP"):
        HttpBackend(sftp_context)


@pytest.fixture
def server():
    server = LocalServer()
    yield server
    server.close()


def serve_pages(server, pages):
    def get(handler, path):
        if path in pages:
            handler._send(pages[path].encode(), "text/html")
        else:
            handler.send_error(404)

    server.handlers["GET"] = get


def requested_paths(server):
    return [path for _, path, _ in server.requests]


def test_a_listing_redirected_to_a_visited_directory_is_not_repeated(server):
    server.files.update({"a.fits": b"alpha", "alias/hidden.fits": b"x"})
    server.redirects["/data/alias/"] = "/data/"
    directories = []
    with OperationContext(RemoteSpec.from_url(server.url)) as context:
        entries = [entry.path for entry in
                   context.transport.iter_entries(on_directory=directories.append)]
    assert entries == ["a.fits"]
    assert directories == [""]
    assert requested_paths(server) == ["/data/", "/data/alias/", "/data/"]


def test_links_back_to_listed_directories_are_not_followed_again(server):
    serve_pages(server, {
        "/data/": "<h1>Index of /data/</h1><a href='sub/'>sub/</a>"
                  "<a href='a.fits'>a.fits</a>",
        "/data/sub/": "<h1>Index of /data/sub/</h1><a href='/data/sub/'>.</a>"
                      "<a href='/data/'>up</a><a href='b.fits'>b.fits</a>",
    })
    with OperationContext(RemoteSpec.from_url(server.url)) as context:
        entries = sorted(entry.path for entry in context.transport.iter_entries())
    assert entries == ["a.fits", "sub/b.fits"]
    assert requested_paths(server) == ["/data/", "/data/sub/"]


def test_a_listing_redirected_into_repository_metadata_is_refused(server):
    server.files.update({"sub/a.fits": b"a", ".hm/config.yml": b"x"})
    server.redirects["/data/sub/"] = "/data/.hm/"
    with OperationContext(RemoteSpec.from_url(server.url)) as context:
        with pytest.raises(DownloadError, match="outside the dataset root"):
            list(context.transport.iter_entries())


def test_metadata_redirects_to_undecodable_paths_are_refused(server):
    server.redirects["/data/config.yml"] = "/data/%ff.yml"
    with OperationContext(RemoteSpec.from_url(server.url)) as context:
        with pytest.raises(DownloadError, match="Invalid metadata redirect URL"):
            context.read_text("config.yml")
    assert requested_paths(server) == ["/data/config.yml"]


def test_metadata_redirects_need_a_location(server):
    def moved(handler, path):
        handler.send_response(302)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    server.handlers["GET"] = moved
    with OperationContext(RemoteSpec.from_url(server.url)) as context:
        with pytest.raises(DownloadError, match="no location"):
            context.read_text("config.yml")


def test_metadata_beyond_its_byte_limit_is_refused(server):
    server.files["config.yml"] = b"x" * 100
    with OperationContext(RemoteSpec.from_url(server.url)) as context:
        context.text_limit = 10
        with pytest.raises(DownloadError, match="exceeds the configured limit"):
            context.read_text("config.yml")
        context.text_limit = 100
        assert context.read_text("config.yml") == "x" * 100


def test_index_size_columns_must_hold_whole_numbers():
    page = ("<h1>Index of /data/</h1><table>"
            "<tr><td><a href='a.fits'>a.fits</a></td><td class='size'>-</td></tr>"
            "<tr><td><a href='b.fits'>b.fits</a></td>"
            "<td class='size'>1,024</td></tr></table>")
    links = _parse_directory_listing(page, "https://data.test/data/", "")
    assert links == [("a.fits", False, None, None), ("b.fits", False, 1024, None)]


# --- WebDAV ----------------------------------------------------------------------


def multistatus(*responses):
    return ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:oc="http://owncloud.org/ns">' + "".join(responses)
            + "</d:multistatus>").encode()


def member(href, *, collection=False, props=""):
    kind = "<d:collection/>" if collection else ""
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:resourcetype>{kind}</d:resourcetype>{props}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


@pytest.fixture
def dav(server):
    server.listings = {}

    def propfind(handler, path):
        body = server.listings.get(path)
        if body is None:
            handler.send_error(404)
            return
        handler.send_response(207)
        handler.send_header("Content-Type", "application/xml")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    server.handlers["PROPFIND"] = propfind
    return server


def dav_entries(server, **options):
    remote = RemoteSpec.from_url(server.url, backend="webdav")
    with OperationContext(remote) as context:
        return {entry.path: entry
                for entry in context.transport.iter_entries(**options)}


def test_unusable_webdav_checksums_sizes_and_dates_are_left_empty(dav):
    dav.listings["/data/"] = multistatus(
        member("/data/", collection=True),
        member("/data/a.fits", props=(
            "<d:getcontentlength>5</d:getcontentlength>"
            "<d:getlastmodified>yesterday</d:getlastmodified>"
            "<oc:checksums><oc:checksum>ADLER32:0a1b2c3d MD5:xyz</oc:checksum>"
            "</oc:checksums>")),
        member("/data/b.fits", props=(
            "<d:getlastmodified/><d:getcontentlength>n/a</d:getcontentlength>")))
    entries = dav_entries(dav)
    first, second = entries["a.fits"], entries["b.fits"]
    assert (first.size, first.mtime, first.checksum, first.checksum_source) == (
        5, None, None, None)
    assert (second.size, second.mtime) == (None, None)


def test_collections_are_listed_once_however_often_they_are_linked(dav):
    dav.listings["/data/"] = multistatus(
        member("/data/", collection=True),
        member("/data/a/", collection=True),
        member("/data/a/", collection=True),
        member("/data/b/", collection=True))
    dav.listings["/data/a/"] = multistatus(
        member("/data/a/", collection=True), member("/data/a/x.fits"))
    dav.listings["/data/b/"] = multistatus(
        member("/data/b/", collection=True), member("/data/a/", collection=True))
    assert sorted(dav_entries(dav)) == ["a/x.fits"]
    assert requested_paths(dav) == ["/data/", "/data/a/", "/data/b/"]


def test_a_collection_redirected_to_a_listed_one_is_skipped(dav):
    dav.listings["/data/"] = multistatus(
        member("/data/", collection=True),
        member("/data/alias/", collection=True),
        member("/data/a.fits"))
    dav.redirects["/data/alias/"] = "/data/"
    directories = []
    assert sorted(dav_entries(dav, on_directory=directories.append)) == ["a.fits"]
    assert directories == [""]


def test_a_plain_success_reply_is_not_a_webdav_listing(dav):
    dav.handlers["PROPFIND"] = lambda handler, path: handler._send(
        b"<html></html>", "text/html")
    with pytest.raises(CapabilityError, match="no WebDAV listing"):
        dav_entries(dav)


def test_refused_webdav_listings_are_refused_access(dav):
    dav.handlers["PROPFIND"] = lambda handler, path: handler.send_error(403)
    with pytest.raises(AccessRefused, match=r"Listing .* failed: access refused "
                                            r"\(HTTP 403\)"):
        dav_entries(dav)


def test_members_without_a_location_or_properties_are_tolerated(dav):
    dav.listings["/data/"] = multistatus(
        "<d:response><d:propstat><d:prop/>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
        "<d:response><d:href>/data/a.fits</d:href><d:propstat>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
    entries = dav_entries(dav)
    assert list(entries) == ["a.fits"]
    assert entries["a.fits"].size is None
