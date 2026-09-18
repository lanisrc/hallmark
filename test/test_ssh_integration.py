"""Disposable, loopback-only OpenSSH tests. No external hosts or persistent keys."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import getpass
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.parse import quote

from click.testing import CliRunner
import pytest
import yaml

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.downloader import download_remote_data, select_download_files
from hallmark.repo_builder import build_repo
from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport.base import (
    DownloadError, RemoteObjectMissing, TransferCancelled,
)
from hallmark.transport.ssh import SshTransport

pytestmark = pytest.mark.ssh_integration


@pytest.fixture
def ssh_server(tmp_path, monkeypatch, request):
    if os.environ.get("HALLMARK_RUN_SSH_TESTS") != "1":
        pytest.skip("Set HALLMARK_RUN_SSH_TESTS=1 for disposable loopback SSH tests")
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).exists():
        pytest.fail("Opted-in SSH tests require openssh-server")
    for name in ("host", "client", "second", "untrusted"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / name)],
            check=True,
        )
    authorized = tmp_path / "authorized"
    authorized.write_text(
        (tmp_path / "client.pub").read_text() + (tmp_path / "second.pub").read_text()
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "sshd_config"
    config.write_text(f"""ListenAddress 127.0.0.1
Port {port}
HostKey {tmp_path}/host
PidFile {tmp_path}/sshd.pid
AuthorizedKeysFile {authorized}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxSessions 4
Subsystem sftp internal-sftp
""")
    mode = getattr(request, "param", None)
    if mode == "sftp-only":
        config.write_text(config.read_text() + "ForceCommand internal-sftp\n")
    elif mode == "no-sftp":
        config.write_text(
            config.read_text().replace("Subsystem sftp internal-sftp\n", "")
        )
    elif mode == "max-one":
        config.write_text(config.read_text().replace("MaxSessions 4", "MaxSessions 1"))
    log = (tmp_path / "sshd.log").open("wb")
    daemon = subprocess.Popen(
        [sshd, "-D", "-e", "-f", str(config)], stderr=log, start_new_session=True
    )
    known = tmp_path / "known_hosts"
    known.write_text(f"[127.0.0.1]:{port} " + (tmp_path / "host.pub").read_text())
    client_config = tmp_path / "ssh_config"
    client_config.write_text(f"""Host hm-test
    HostName 127.0.0.1
Host *
    User {getpass.getuser()}
    Port {port}
    IdentityFile {tmp_path}/client
    IdentityAgent none
    IdentitiesOnly yes
    UserKnownHostsFile {known}
    GlobalKnownHostsFile /dev/null
""")
    original = SshTransport._options
    monkeypatch.setattr(
        SshTransport,
        "_options",
        lambda self, **kw: ["-F", str(client_config)] + original(self, **kw),
    )
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "export"
    root.mkdir()
    try:
        deadline = time.monotonic() + 5
        while True:
            if daemon.poll() is not None:
                log.flush()
                pytest.fail((tmp_path / "sshd.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("Local SSH test server did not start")
                time.sleep(0.05)
        yield {
            "root": root,
            "port": port,
            "known": known,
            "base": tmp_path,
            "config": client_config,
            "daemon": daemon,
            "url": "ssh://hm-test" + quote(str(root), safe="/") + "/",
        }
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)
        log.close()


@pytest.mark.parametrize("scheme", ["ssh", "sftp"])
def test_parallel_exact_files_and_master_cleanup(ssh_server, tmp_path, scheme):
    root = ssh_server["root"]
    names = [
        "a b#?%+ü.h5",
        "quotes'\"[x]*?.h5",
        "-leading.h5",
        " tail  ",
        "literal%20.h5",
        "nested/data.bin",
    ]
    for name in names:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(name)
    url = ssh_server["url"].replace("ssh:", scheme + ":")
    with OperationContext(RemoteSpec.parse(url)) as context:
        transport = context.transport
        transport.prepare()
        master = transport._master
        control = transport._socket
        assert Path(control).stat().st_mode & 0o077 == 0
        output = tmp_path / "output"
        output.mkdir()
        with ThreadPoolExecutor(4) as pool:
            tasks = [
                pool.submit(transport.fetch, name, output / str(i))
                for i, name in enumerate(names)
            ]
            for task in tasks:
                task.result()
        assert transport._master is master
        assert len(transport._processes) == 1
        for i, name in enumerate(names):
            assert (output / str(i)).read_text() == name
    assert master.poll() is not None
    assert not Path(control).exists()


def test_explicit_endpoint_and_missing_file(ssh_server, tmp_path):
    server = ssh_server
    url = f"sftp://{getpass.getuser()}@127.0.0.1:{server['port']}{server['root']}/"
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url=url)
    output = repo.worktree / "missing"
    output.write_bytes(b"keep")
    result = download_remote_data(
        repo, repo.worktree, selected_files=[(Path("missing"), None)]
    , approved=True)
    assert result["failed"] == 1
    assert output.read_bytes() == b"keep"
    assert not list(Path(repo.worktree).glob("*.part"))


@pytest.mark.parametrize("trust", ["unknown", "changed", "no-key"])
def test_host_trust_and_auth_fail_preflight(ssh_server, tmp_path, trust):
    server = ssh_server
    if trust == "unknown":
        server["known"].write_text("")
    elif trust == "changed":
        server["known"].write_text(
            f"[127.0.0.1]:{server['port']} "
            + (server["base"] / "untrusted.pub").read_text()
        )
    else:
        # Both keys are ephemeral; the replacement is not authorized on the server.
        config = server["config"].read_text().replace("/client\n", "/untrusted\n")
        server["config"].write_text(config)
    with OperationContext(RemoteSpec.parse(server["url"])) as context:
        with pytest.raises(DownloadError, match="Cannot establish"):
            context.transport.prepare()
        assert not context.transport._processes
    assert not (tmp_path / "download").exists()


def test_simultaneous_profiles_have_separate_masters(ssh_server, monkeypatch):
    server = ssh_server
    (server["root"] / "item").write_bytes(b"data")
    auth = server["base"] / "auth.yml"
    auth.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "profiles": {
                    name: {
                        "hosts": ["hm-test"],
                        "identity_file": str(server["base"] / key),
                    }
                    for name, key in [("one", "client"), ("two", "second")]
                },
            }
        )
    )
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(auth))
    with OperationContext(RemoteSpec.parse(server["url"], "one")) as one:
        with OperationContext(RemoteSpec.parse(server["url"], "two")) as two:
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda context: context.transport.prepare(), [one, two]))
            assert one.transport._socket != two.transport._socket
            assert one.transport._master.pid != two.transport._master.pid
            first = one.transport._master
            second = two.transport._master
        assert second.poll() is not None
        assert first.poll() is None
        one.transport.fetch("item", server["base"] / "copy")
    assert first.poll() is not None


def test_build_manifest_download_clone_workflow(ssh_server, tmp_path):
    root = ssh_server["root"]
    (root / "nested").mkdir()
    (root / "nested/item_1.dat").write_bytes(b"science")
    (root / "README.md").write_bytes(b"notes")
    (root / "bad_2.dat").write_bytes(b"changed")
    strong = hashlib.sha256(b"science").hexdigest()
    (root / "export.sha256sums").write_text(
        strong + "  nested/item_1.dat\n" + "a" * 64 + "  bad_2.dat\n"
    )
    repo = build_repo(
        tmp_path / "catalog",
        "lab",
        [
            {"fmt": "nested/item_{i}.dat", "db": "data.tsv"},
            {"fmt": "bad_{i}.dat", "db": "bad.tsv"},
        ],
        dataset_url=ssh_server["url"],
    )
    assert repo.state.config["remote"] == [{"name": "origin", "url": ssh_server["url"]}]
    static = next(
        entry for entry in repo.state.config["data"] if entry.get("file") == "README.md"
    )
    assert not any(key in static for key in ("md5", "sha1", "sha256"))
    assert static.get("checksum") in (None, "unknown")
    destination = tmp_path / "downloads"
    files = select_download_files(repo, all_files=True)
    result = download_remote_data(
        repo, destination, selected_files=files, approved=True,
    )
    assert result["succeeded"] == 3
    assert result["failed"] == 1
    assert (destination / "nested/item_1.dat").read_bytes() == b"science"
    assert not (destination / "bad_2.dat").exists()
    assert not list(destination.rglob("*.part"))

    # Single-catalog public Python clone and CLI clone both route SSH downloads.
    single = build_repo(
        tmp_path / "single",
        "lab",
        [{"fmt": "nested/item_{i}.dat", "db": "data.tsv"}],
        dataset_url=ssh_server["url"],
    )
    # Remove the intentionally incorrect static entry before cloning.
    single.state.config["data"] = [
        entry
        for entry in single.state.config["data"]
        if entry.get("file") != "bad_2.dat"
    ]
    single.dothm.dump(single.state)
    single.dothm.index.add(["config.yml"])
    single.dothm.index.commit("Remove deliberately invalid test entry")
    clone = Repo.clone(
        str(single.dothm.path), tmp_path / "python-clone",
        download=True, approve=lambda plan: True,
    )
    assert (clone.worktree / "nested/item_1.dat").read_bytes() == b"science"
    result = CliRunner().invoke(
        hallmark,
        ["clone", str(single.dothm.path), str(tmp_path / "cli-clone"), "--download"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cli-clone/nested/item_1.dat").read_bytes() == b"science"


def _wait_for_partial_file(directory, pattern, total_size, task):
    """Wait for actual SFTP bytes before interrupting a transfer."""
    directory = Path(directory)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if task.done():
            task.result()  # Surface a transfer error instead of a polling timeout.
            pytest.fail("Transfer completed before it could be interrupted")
        for path in directory.glob(pattern):
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if 0 < size < total_size:
                return
        time.sleep(0.01)
    pytest.fail("SFTP did not write a partial file before the deadline")


def test_real_cancel_transfer_and_preserve_other_master(ssh_server, tmp_path):
    server = ssh_server
    payload = b"x" * 128 * 1024
    (server["root"] / "large").write_bytes(payload)
    with OperationContext(RemoteSpec.parse(server["url"])) as survivor:
        survivor.transport.prepare()
        with OperationContext(RemoteSpec.parse(server["url"])) as context:
            context.settings = replace(
                context.settings, shutdown_timeout=1, transfer_timeout=15
            )
            context.transport.prepare()
            # Bound requests as well as bandwidth: a small file can otherwise
            # finish inside the client's initial buffering window on macOS.
            context.transport.sftp = ["sftp", "-B", "1024", "-R", "1", "-l", "8"]
            target = tmp_path / "partial"
            progress = []
            context.on_bytes = progress.append
            with ThreadPoolExecutor(1) as pool:
                task = pool.submit(context.transport.fetch, "large", target)
                try:
                    _wait_for_partial_file(tmp_path, "partial", len(payload), task)
                    deadline = time.monotonic() + 2
                    while not progress and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert 0 < sum(progress) < len(payload)
                    assert all(delta > 0 for delta in progress)
                    context.cancel()
                    with pytest.raises(TransferCancelled):
                        task.result(timeout=5)
                finally:
                    context.cancel()
            assert not context.transport._processes
        assert survivor.transport._master.poll() is None
        survivor.transport.fetch("large", tmp_path / "survivor-copy")
        assert (tmp_path / "survivor-copy").read_bytes() == payload


@pytest.mark.parametrize("ssh_server", ["sftp-only"], indirect=True)
def test_sftp_only_discovers_metadata_and_fetches(ssh_server, tmp_path):
    root = ssh_server["root"]
    (root / "item").write_bytes(b"data")
    (root / "nested").mkdir()
    name = "nested/a b#?%+ü'\"[*].fits"
    (root / name).write_bytes(b"science")
    with OperationContext(RemoteSpec.parse(ssh_server["url"])) as context:
        directories = []
        entries = list(context.transport.iter_entries(on_directory=directories.append))
        assert {entry.path: entry.size for entry in entries} == {"item": 4, name: 7}
        assert directories == ["", "nested/"]
        assert all(entry.mtime is not None for entry in entries)
        assert context.read_text("item") == "data"
        with pytest.raises(RemoteObjectMissing):
            context.read_text("absent-config.yml")
        transferred = []
        context.on_bytes = transferred.append
        context.transport.fetch("item", tmp_path / "copy")
        assert (tmp_path / "copy").read_bytes() == b"data"
        assert sum(transferred) == 4


@pytest.mark.parametrize("ssh_server", ["sftp-only"], indirect=True)
@pytest.mark.parametrize("scheme", ["ssh", "sftp"])
def test_sftp_only_clone_plans_then_requires_payload_approval(
    ssh_server, tmp_path, monkeypatch, scheme,
):
    root = ssh_server["root"]
    (root / "nested").mkdir()
    (root / "nested" / "science.fits").write_bytes(b"science")
    (root / "notes.txt").write_bytes(b"notes")
    fetched = []
    fetch = SshTransport._fetch

    def record_fetch(self, path, destination, file_limit=None):
        fetched.append(path)
        return fetch(self, path, destination, file_limit)

    def reject_git_probe(*args, **kwargs):
        raise AssertionError("SFTP directory detection must not invoke remote Git")

    monkeypatch.setattr(SshTransport, "_fetch", record_fetch)
    monkeypatch.setattr("hallmark.catalog.Dothm.clone", reject_git_probe)
    plans = []

    def decline(plan):
        plans.append(plan)
        return False

    repo = Repo.clone(
        ssh_server["url"].replace("ssh:", scheme + ":"), tmp_path / "clone",
        filter="**/*.fits", download=True, approve=decline,
    )
    assert fetched == []
    assert repo.state.data["path"].tolist() == ["nested/science.fits"]
    assert len(plans) == 1
    assert plans[0].total_bytes == 7
    assert not (repo.worktree / "nested").exists()
    with pytest.raises(DownloadError, match="approval"):
        repo.download(plans[0])
    assert fetched == []
    result = repo.download(plans[0], approved=True)
    assert result["succeeded"] == 1
    assert fetched == ["nested/science.fits"]
    assert (repo.worktree / "nested/science.fits").read_bytes() == b"science"


@pytest.mark.parametrize("ssh_server", ["no-sftp"], indirect=True)
def test_disabled_subsystem_fails(ssh_server, tmp_path):
    (ssh_server["root"] / "item").write_bytes(b"data")
    with OperationContext(RemoteSpec.parse(ssh_server["url"])) as context:
        with pytest.raises(DownloadError):
            context.transport.fetch("item", tmp_path / "copy")
        assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("ssh_server", ["max-one"], indirect=True)
def test_local_session_limit(ssh_server, tmp_path, monkeypatch):
    for i in range(4):
        (ssh_server["root"] / str(i)).write_bytes(b"data")
    auth = tmp_path / "auth.yml"
    auth.write_text("version: 1\ndefaults:\n  max_sessions: 1\n")
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(auth))
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url=ssh_server["url"])
    result = download_remote_data(
        repo,
        repo.worktree,
        max_workers=4,
        selected_files=[(Path(str(i)), None) for i in range(4)],
     approved=True)
    assert result["succeeded"] == 4
    assert result["failed"] == 0


def test_profile_source_recording_and_explicit_output(
    ssh_server, tmp_path, monkeypatch
):
    auth = tmp_path / "auth.yml"
    auth.write_text("version: 1\nprofiles:\n  lab:\n    hosts: [hm-test]\n")
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(auth))
    repo = build_repo(
        tmp_path / "catalog",
        "label",
        [],
        dataset_url=ssh_server["url"],
        dataset_auth="lab",
    )
    assert repo.state.config["remote"][0]["auth"] == "lab"
    explicit = build_repo(
        tmp_path / "explicit",
        "label",
        [],
        remotes=[{"name": "mirror", "url": "https://example.test/data"}],
        dataset_url=ssh_server["url"],
        dataset_auth="lab",
    )
    assert explicit.state.config["remote"] == [
        {"name": "mirror", "url": "https://example.test/data"}
    ]


def test_ssh_listing_omits_symlinks_and_rejects_controls(ssh_server, tmp_path):
    root = ssh_server["root"]
    (root / "item").write_bytes(b"data")
    (root / "link").symlink_to(root / "item")
    with OperationContext(RemoteSpec.parse(ssh_server["url"])) as context:
        assert context.transport.list_entries() == ["item"]
    (root / "bad\nname").write_bytes(b"data")
    with OperationContext(RemoteSpec.parse(ssh_server["url"])) as context:
        with pytest.raises(DownloadError, match="control"):
            context.transport.list_entries()


def test_permission_denied_preserves_existing_file(ssh_server, tmp_path):
    source = ssh_server["root"] / "private"
    source.write_bytes(b"unreadable")
    source.chmod(0)
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url=ssh_server["url"])
    destination = repo.worktree / "private"
    destination.write_bytes(b"original")
    try:
        result = download_remote_data(
            repo, repo.worktree, selected_files=[(Path("private"), None)]
        , approved=True)
        assert result["failed"] == 1
        assert destination.read_bytes() == b"original"
        assert not list(Path(repo.worktree).glob("*.part"))
    finally:
        source.chmod(0o600)


def test_disconnected_master_cleans_partial_transfer(ssh_server, tmp_path, monkeypatch):
    payload = b"x" * 128 * 1024
    (ssh_server["root"] / "large").write_bytes(payload)
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url=ssh_server["url"])
    destination = repo.worktree / "large"
    destination.write_bytes(b"original")
    transports = []
    prepare = SshTransport.prepare

    def slow_prepare(self):
        prepare(self)
        self.context.settings = replace(
            self.context.settings, shutdown_timeout=1, transfer_timeout=15
        )
        self.sftp = ["sftp", "-B", "1024", "-R", "1", "-l", "8"]
        if self not in transports:
            transports.append(self)

    monkeypatch.setattr(SshTransport, "prepare", slow_prepare)
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(
            download_remote_data,
            repo,
            repo.worktree,
            selected_files=[(Path("large"), None)],
         approved=True)
        try:
            _wait_for_partial_file(repo.worktree, "*.part", len(payload), task)
            assert len(transports) == 1
            transports[0]._master.terminate()
            result = task.result(timeout=5)
        finally:
            for transport in transports:
                transport.context.cancel()
    assert result["failed"] == 1
    assert destination.read_bytes() == b"original"
    assert not list(Path(repo.worktree).glob("*.part"))
    assert not transports[0]._processes
