"""Transport boundaries, local policy, HTTP compatibility and process ownership."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import hashlib
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
from urllib.parse import quote

import pytest
import requests
import yaml

from hallmark import Repo
from hallmark.downloader import _download_file, download_remote_data
from hallmark.repo_builder import _manifest_matches, build_repo
from hallmark.repo_config import normalize_remotes
from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport.auth import resolve_settings
from hallmark.transport.base import (
    CapabilityError,
    DownloadError,
    RemoteConfigurationError,
    TransferCancelled,
    literal_path,
)
from hallmark.transport.ssh import SshTransport, batch_argument


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch, tmp_path):
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.mark.parametrize("scheme", ["ssh", "sftp"])
@pytest.mark.parametrize(
    "name",
    [
        "has space#tag.h5",
        "a?b%+ü.h5",
        "quotes'\"[]*.h5",
        "-weird.fits",
        "literal%20.h5",
        " leading ",
    ],
)
def test_literal_url_round_trip(scheme, name):
    remote = RemoteSpec.parse(f"{scheme}://user@campus:2222/srv/a%20b/")
    url = remote.file_url(name)
    assert RemoteSpec.parse(url).root == "/srv/a b/" + name
    assert remote.pathname(name) == "/srv/a b/" + name
    assert quote(name, safe="/") in url


@pytest.mark.parametrize(
    "url",
    [
        "campus:/data",
        "file:///etc/passwd",
        "ssh:///data",
        "ssh://-o/data",
        "ssh://u:secret@campus/data",
        "ssh://campus/data?secret",
        "ssh://campus/data#x",
        "ssh://campus/../data",
        "ssh://campus/%2e%2e/data",
        "ssh://campus",
        "ssh://campus:0/data",
        "ssh://campus:65536/data",
        "ssh://campus:/data",
        "ssh://campus/%0adata",
        "ssh://campus/d\nata",
        "\tssh://campus/data",
        "ssh://u%24%28id%29@campus/data",
        "ssh://campus$(id)/data",
        "ssh://[::1/data",
        "ssh://campus/%xx",
        "ssh://campus/%ff",
        "ssh://campus//data",
        "ssh://campus/data\\file",
    ],
)
def test_bad_remote_fails_without_echoing_secrets(url):
    with pytest.raises(RemoteConfigurationError) as error:
        RemoteSpec.parse(url)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("host", ["campus_alias", "a.b-c", "[::1]", "[2001:db8::1]"])
def test_hosts_and_aliases(host):
    assert RemoteSpec.parse(f"ssh://{host}/").host == host.strip("[]")


@pytest.mark.parametrize("path", ["bad\nget /secret /tmp/leak", "a\tb", "a\rb", "a\0b"])
def test_controls_rejected_at_all_path_boundaries(path, tmp_path):
    with pytest.raises(RemoteConfigurationError):
        literal_path(path)
    with pytest.raises(RemoteConfigurationError):
        batch_argument("/" + path)
    with pytest.raises(RemoteConfigurationError):
        _download_file("ssh://campus/" + path, tmp_path / "out")


def write_auth(monkeypatch, tmp_path, profiles, defaults=None):
    path = tmp_path / "auth.yml"
    path.write_text(
        yaml.safe_dump({"version": 1, "profiles": profiles, "defaults": defaults or {}})
    )
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(path))
    return path


def test_profile_precedence_binding_and_isolation(monkeypatch, tmp_path):
    write_auth(
        monkeypatch,
        tmp_path,
        {
            "one": {
                "hosts": ["campus"],
                "user": "alice",
                "port": 2200,
                "identity_file": "~/key-one",
            },
            "two": {
                "hosts": ["campus"],
                "user": "bob",
                "port": 2201,
                "identity_file": "~/key-two",
            },
        },
        {"transfer_timeout": 77},
    )
    first = resolve_settings(RemoteSpec.parse("ssh://carol@campus:2222/data", "one"))
    second = resolve_settings(RemoteSpec.parse("sftp://campus/data", "two"))
    assert (first.user, first.port, first.transfer_timeout) == ("carol", 2222, 77)
    assert (second.user, second.port) == ("bob", 2201)
    assert first.identity_file != second.identity_file
    with pytest.raises(RemoteConfigurationError, match="not bound"):
        resolve_settings(RemoteSpec.parse("ssh://other/data", "one"))
    with pytest.raises(RemoteConfigurationError, match="Unresolved"):
        resolve_settings(RemoteSpec.parse("ssh://campus/data", "missing"))


@pytest.mark.parametrize(
    "profile",
    [
        {"hosts": ["campus"], "password": "secret"},
        {"hosts": ["campus"], "ssh_options": {"ProxyCommand": "anything"}},
        {"hosts": ["campus"], "port": True},
        {"hosts": ["campus"], "host_key_policy": "no"},
        {"hosts": ["campus"], "identity_file": "%d/key"},
        {"hosts": []},
        {"hosts": ["$(id)"]},
    ],
)
def test_invalid_profiles_fail_preflight(monkeypatch, tmp_path, profile):
    write_auth(monkeypatch, tmp_path, {"campus": profile})
    with pytest.raises(RemoteConfigurationError):
        OperationContext(RemoteSpec.parse("ssh://campus/data", "campus"))


def test_profile_xdg_and_removal(monkeypatch, tmp_path):
    folder = tmp_path / "config" / "hallmark"
    folder.mkdir(parents=True)
    (folder / "auth.yml").write_text(
        "version: 1\nprofiles:\n  lab:\n    hosts: [campus]\n    user: alice\n"
    )
    assert (
        resolve_settings(RemoteSpec.parse("ssh://campus/data", "lab")).user == "alice"
    )
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="ssh://campus/data", remote_auth="lab")
    assert repo.state.config["remote"]["auth"] == "lab"
    repo.set_config(remote_auth="")
    assert "auth" not in Repo(repo.worktree).state.config["remote"]
    before = repo.state.config.copy()
    with pytest.raises(ValueError):
        repo.set_config(remote_auth="invalid.name", fmt="changed-{x}")
    assert repo.state.config == before


@pytest.mark.parametrize(
    "entry",
    [
        {"url": "ssh://campus/data", "token": "secret"},
        {"url": "ssh://campus/data", "auth": 3},
    ],
)
def test_remote_schema(entry):
    with pytest.raises(ValueError):
        normalize_remotes(entry)


def test_netrc_is_preserved(monkeypatch, tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine example.test login scientist password test-value\n")
    monkeypatch.setenv("NETRC", str(netrc))
    with OperationContext(RemoteSpec.parse("https://example.test/data")) as context:
        prepared = context.session().prepare_request(
            requests.Request("GET", "https://example.test/data/file")
        )
        assert prepared.headers["Authorization"].startswith("Basic ")
        assert context.session() is context.session()


def test_http_error_redacts_exception_and_cause(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("https://user:secret@example.test/?token=secret")

    monkeypatch.setattr(requests, "get", fail)
    url = "https://user:secret@example.test/data?token=secret"
    with pytest.raises(DownloadError) as error:
        _download_file(url, tmp_path / "out")
    formatted = traceback.format_exception(
        type(error.value), error.value, error.value.__traceback__
    )
    assert "secret" not in "".join(formatted)
    assert "secret" not in repr(error.value)


def test_symlink_swap_after_planning(monkeypatch, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="https://example.test/")
    folder = repo.worktree / "sub"
    folder.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap(self):
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr("hallmark.transport.http.HttpTransport.prepare", swap)
    result = download_remote_data(
        repo, repo.worktree, selected_files=[(Path("sub/data"), None)]
    )
    assert result["failed"] == 1
    assert list(outside.iterdir()) == []


def test_builder_requires_explicit_capabilities(tmp_path):
    with pytest.raises(CapabilityError, match="index-format"):
        build_repo(
            tmp_path / "repo", "lab", [], dataset_url="https://example.test/data"
        )
    with pytest.raises(CapabilityError, match="allow-remote-commands"):
        build_repo(tmp_path / "repo", "lab", [], dataset_url="ssh://campus/data")
    assert not (tmp_path / "repo").exists()


def test_manifest_preserves_whitespace_and_percent():
    text = "a" * 64 + "   leading%20 name  \n"
    assert _manifest_matches(text, "sha256") == [("a" * 64, " leading%20 name  ")]
    with pytest.raises(ValueError, match="escaped"):
        _manifest_matches("\\" + "a" * 64 + "  line\\nbreak\n", "sha256")


@pytest.mark.ssh_client
@pytest.mark.parametrize(
    "name",
    ["a b#?%+ü.h5", "quotes'\"[x]*?.h5", "-leading.h5", " tail  ", "literal%20.h5"],
)
def test_real_sftp_parser_without_network(tmp_path, name):
    import shutil

    server = next(
        (
            p
            for p in ["/usr/lib/openssh/sftp-server", "/usr/libexec/sftp-server"]
            if Path(p).exists()
        ),
        None,
    )
    if not server or not shutil.which("sftp"):
        pytest.skip("OpenSSH client and local SFTP server binary required")
    source = tmp_path / name
    source.write_text(name)
    output = tmp_path / ("output-" + name)
    # A glob collision must never produce two matches or replace another file.
    (tmp_path / "a b#X%+ü.h5").write_text("decoy")
    batch = f"get {batch_argument(source)} {batch_argument(output)}\n"
    result = subprocess.run(
        ["sftp", "-D", server, "-b", "-"],
        input=batch.encode(),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == source.read_bytes()


@pytest.fixture
def fake_process(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text("""import os, signal, subprocess, sys, time
mode = sys.argv[1]
if mode == "fail":
    os.write(2, b"secret-invalid-utf8-\\xff")
    sys.exit(1)
elif mode == "flood":
    os.write(2, b"x" * 100000)
elif mode == "partial":
    open(sys.argv[2], "wb").write(b"partial")
    sys.exit(1)
elif mode == "sleep":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(sys.argv[2], "w").write(str(os.getpid()) + " " + str(child.pid))
    time.sleep(60)
else:
    raise RuntimeError("Unexpected fake request")
""")
    return [sys.executable, str(script)]


@pytest.mark.parametrize(
    "mode, message", [("fail", "SSH operation failed"), ("flood", "size limit")]
)
def test_bounded_process_errors(fake_process, mode, message):
    with OperationContext(RemoteSpec.parse("ssh://unused/data")) as context:
        transport = context.transport
        with pytest.raises(DownloadError, match=message) as error:
            transport._run(fake_process + [mode], timeout=5)
        assert "secret" not in str(error.value)
        assert not transport._processes


def test_process_start_failure():
    with OperationContext(RemoteSpec.parse("ssh://unused/data")) as context:
        with pytest.raises(DownloadError, match="Unable to start"):
            context.transport._run(["/nonexistent/hallmark-test"], timeout=1)


def test_cleanup_exited_process_group_permission_error(monkeypatch):
    with OperationContext(RemoteSpec.parse("ssh://unused/data")) as context:
        transport = context.transport
        process = transport._spawn([sys.executable, "-c", "pass"])
        process.wait(timeout=5)
        signals = []

        def zombie_group(pid, signum):
            assert pid == process.pid
            signals.append(signum)
            raise PermissionError(errno.EPERM, "Operation not permitted")

        with monkeypatch.context() as patch:
            patch.setattr("hallmark.transport.ssh.os.killpg", zombie_group)
            transport._stop(process)
        # Exited leaders may still have proxy children: attempt both signals.
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert process.returncode == 0
        assert not transport._processes


def test_cleanup_live_process_permission_error_is_not_suppressed(monkeypatch):
    with OperationContext(RemoteSpec.parse("ssh://unused/data")) as context:
        transport = context.transport
        process = transport._spawn(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )

        def denied(pid, signum):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        with monkeypatch.context() as patch:
            patch.setattr("hallmark.transport.ssh.os.killpg", denied)
            with pytest.raises(PermissionError):
                transport._stop(process)
            assert process.poll() is None
            assert process in transport._processes
        # Restore real signals before the context closes and reaps this child.
    assert process.poll() is not None
    assert not transport._processes


def test_cancel_active_process_group(fake_process, tmp_path):
    pids = tmp_path / "pids"
    with OperationContext(RemoteSpec.parse("ssh://unused/data")) as context:
        context.settings = replace(context.settings, shutdown_timeout=1)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(
                context.transport._run, fake_process + ["sleep", str(pids)], timeout=30
            )
            deadline = time.monotonic() + 5
            while not pids.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pids.exists()
            started = time.monotonic()
            context.cancel()
            with pytest.raises(TransferCancelled):
                future.result(timeout=5)
            assert time.monotonic() - started < 4
        assert not context.transport._processes
        for pid in map(int, pids.read_text().split()):
            proc = Path(f"/proc/{pid}/stat")
            assert not proc.exists() or proc.read_text().split()[2] == "Z"


def test_partial_failure_preserves_destination(monkeypatch, fake_process, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="ssh://unused/data")
    destination = repo.worktree / "data.bin"
    destination.write_bytes(b"original")
    monkeypatch.setattr(SshTransport, "prepare", lambda self: None)

    def fetch(self, path, destination, **kwargs):
        self._run(fake_process + ["partial", str(destination)], timeout=5)

    monkeypatch.setattr(SshTransport, "fetch", fetch)
    result = download_remote_data(
        repo, repo.worktree, selected_files=[(Path("data.bin"), None)]
    )
    assert result["failed"] == 1
    assert destination.read_bytes() == b"original"
    assert not list(Path(repo.worktree).glob("*.part"))


def test_checksum_failure_is_atomic(monkeypatch, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="ssh://unused/data")
    destination = repo.worktree / "data.bin"
    destination.write_bytes(b"original")
    monkeypatch.setattr(SshTransport, "prepare", lambda self: None)
    monkeypatch.setattr(
        SshTransport, "fetch", lambda self, path, dest, **kw: dest.write_bytes(b"wrong")
    )
    result = download_remote_data(
        repo,
        repo.worktree,
        selected_files=[
            (Path("data.bin"), ("sha256", hashlib.sha256(b"right").hexdigest()))
        ],
    )
    assert result["failed"] == 1
    assert destination.read_bytes() == b"original"
    assert not list(Path(repo.worktree).glob("*.part"))


def test_cancel_before_publish(monkeypatch, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="ssh://unused/data")
    destination = repo.worktree / "item"
    destination.write_bytes(b"original")
    monkeypatch.setattr(SshTransport, "prepare", lambda self: None)

    def fetch(self, path, dest, **kwargs):
        dest.write_bytes(b"complete")
        self.context.cancel()

    monkeypatch.setattr(SshTransport, "fetch", fetch)
    result = download_remote_data(
        repo, repo.worktree, selected_files=[(Path("item"), None)]
    )
    assert result["failed"] == 1
    assert destination.read_bytes() == b"original"
    assert not list(Path(repo.worktree).glob("*.part"))


def test_keyboard_interrupt_stops_owned_workers(monkeypatch, tmp_path, fake_process):
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="ssh://unused/data")
    transports = []
    ready = tmp_path / "pids"
    monkeypatch.setattr(SshTransport, "prepare", lambda self: None)

    def fetch(self, path, destination, **kwargs):
        transports.append(self)
        destination.write_bytes(b"partial")
        self._run(fake_process + ["sleep", str(ready)], timeout=30)

    def interrupt(*args, **kwargs):
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(SshTransport, "fetch", fetch)
    monkeypatch.setattr("hallmark.downloader.wait", interrupt)
    with pytest.raises(KeyboardInterrupt):
        download_remote_data(
            repo, repo.worktree, max_workers=1, selected_files=[(Path("item"), None)]
        )
    assert not (repo.worktree / "item").exists()
    assert not list(Path(repo.worktree).glob("*.part"))
    assert all(not transport._processes for transport in transports)


def test_cli_dry_run_does_not_resolve_auth_or_start_clients(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from hallmark.cli import hallmark

    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="ssh://campus/data", remote_auth="missing")
    monkeypatch.chdir(repo.worktree)

    def fail(*args, **kwargs):
        raise AssertionError("Dry-run must not create an operation context")

    monkeypatch.setattr("hallmark.downloader.OperationContext", fail)
    result = CliRunner().invoke(hallmark, ["download", "item.dat", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "item.dat" in result.output


def test_cli_profile_roundtrip(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from hallmark.cli import hallmark

    repo = Repo.init(tmp_path / "repo")
    monkeypatch.chdir(repo.worktree)
    runner = CliRunner()
    result = runner.invoke(
        hallmark,
        ["set-config", "--remote-url", "ssh://campus/data", "--remote-auth", "lab"],
    )
    assert result.exit_code == 0, result.output
    assert Repo(repo.worktree).state.config["remote"]["auth"] == "lab"
    result = runner.invoke(hallmark, ["set-config", "--remote-auth", ""])
    assert result.exit_code == 0, result.output
    assert "auth" not in Repo(repo.worktree).state.config["remote"]


def test_ssh_hash_budgets_skip_without_commands(monkeypatch):
    with OperationContext(
        RemoteSpec.parse("ssh://unused/data"),
        allow_remote_commands=True,
        remote_hash=True,
    ) as context:
        transport = context.transport
        transport._entries = {"big": (context.hash_file_limit + 1, 0), "small": (1, 0)}
        transport._hash_bytes = context.hash_total_limit

        def fail(*args, **kwargs):
            raise AssertionError("Budgeted-out files must not run a command")

        monkeypatch.setattr(transport, "_command", fail)
        assert transport.checksum_small("big") == ("unknown", "unknown")
        assert transport.checksum_small("small") == ("unknown", "unknown")


def test_http_manifest_auth_failure_is_not_optional(monkeypatch):
    from hallmark.repo_builder import list_remote_files
    from mock_server import MockServer

    server = MockServer("https://example.test/data/")
    server.add_directory("", [("data-object", "export.sha256sums")])
    get = server.get

    def unauthorized(url, **kwargs):
        if url.endswith("sha256sums"):
            response = requests.Response()
            response.status_code = 401
            raise requests.HTTPError("credentials-must-not-leak", response=response)
        return get(url, **kwargs)

    server.get = unauthorized
    monkeypatch.setattr(requests, "Session", lambda: server)
    with pytest.raises(DownloadError, match="HTTP 401"):
        list_remote_files(server.base_url)


@pytest.mark.parametrize(
    "url",
    [
        "ssh://[fe80::1%25eth0]/data",
        "ssh://[fe80::1%25$(id)]/data",
        "ssh://campus/bad\udcff",
    ],
)
def test_reject_scoped_ipv6_and_invalid_unicode(url):
    with pytest.raises(RemoteConfigurationError):
        RemoteSpec.parse(url)


def test_minimum_openssh_is_checked_before_connection(monkeypatch):
    with OperationContext(RemoteSpec.parse("ssh://unused/data")) as context:
        monkeypatch.setattr("hallmark.transport.ssh.shutil.which", lambda name: name)
        monkeypatch.setattr(
            context.transport, "_run", lambda *a, **kw: b"OpenSSH_9.5p1"
        )
        with pytest.raises(CapabilityError, match="9.6"):
            context.transport.prepare()
        assert context.transport._master is None
        assert context.transport._socket_dir is None


def test_short_socket_ignores_long_tempdir(monkeypatch, tmp_path):
    # Long macOS TMPDIR/worktree paths can exceed AF_UNIX limits.
    monkeypatch.setenv("TMPDIR", str(tmp_path / ("long" * 35)))
    with OperationContext(RemoteSpec.parse("ssh://unused/data")) as context:
        monkeypatch.setattr(
            context.transport, "_run", lambda *a, **kw: b"OpenSSH_9.6p1"
        )
        monkeypatch.setattr("hallmark.transport.ssh.shutil.which", lambda name: name)

        def fail_start(*args, **kwargs):
            assert len(context.transport._socket.encode()) <= 100
            assert context.transport._socket.startswith("/tmp/hm-")
            raise DownloadError("deliberate startup failure")

        monkeypatch.setattr(context.transport, "_spawn", fail_start)
        with pytest.raises(DownloadError, match="deliberate"):
            context.transport.prepare()
        assert context.transport._socket_dir is None


def test_manifest_strength_and_conflict():
    from hallmark.repo_builder import _record_checksum

    checksums = {}
    _record_checksum(checksums, "file", "sha256", "a" * 64)
    _record_checksum(checksums, "file", "md5", "b" * 32)
    assert checksums["file"] == ("sha256", "a" * 64)
    with pytest.raises(DownloadError, match="Conflicting"):
        _record_checksum(checksums, "file", "sha256", "c" * 64)


def test_explicit_http_source_is_exact_and_keeps_output_remotes(monkeypatch, tmp_path):
    from mock_server import MockServer

    server = MockServer("https://example.test/already-the-dataset/")
    server.add_directory("", [("data-object", "README.md")])
    server.add_file("README.md", b"notes")
    monkeypatch.setattr(requests, "Session", lambda: server)
    repo = build_repo(
        tmp_path / "catalog",
        "label",
        [],
        dataset_url=server.base_url,
        index_format="cyverse-html",
        remotes=[{"name": "mirror", "url": "https://elsewhere.test/data"}],
    )
    assert repo.state.config["remote"][0]["url"] == "https://elsewhere.test/data"
    assert repo.state.config["data"][0]["md5"] == hashlib.md5(b"notes").hexdigest()


def test_build_cli_passes_source_controls(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from hallmark.cli import hallmark

    calls = []
    monkeypatch.setattr("hallmark.cli.build_repo", lambda **kw: calls.append(kw))
    result = CliRunner().invoke(
        hallmark,
        [
            "build",
            str(tmp_path),
            "lab",
            "--fmt",
            "run_{i}.h5=data.tsv",
            "--dataset-url",
            "ssh://campus/data",
            "--dataset-auth",
            "lab",
            "--allow-remote-commands",
            "--remote-hash",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["dataset_url"] == "ssh://campus/data"
    assert calls[0]["dataset_auth"] == "lab"
    assert calls[0]["allow_remote_commands"] is True
    assert calls[0]["remote_hash"] is True
