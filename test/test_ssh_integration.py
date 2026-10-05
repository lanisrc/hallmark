"""Disposable, loopback-only OpenSSH tests. No external hosts or persistent keys."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import getpass
import hashlib
from pathlib import Path
import time

from click.testing import CliRunner
import pytest
import yaml

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.transport import OperationContext, RemoteSpec
from conftest import download_selection
from hallmark.transport.base import (
    DownloadError, RemoteObjectMissing, TransferCancelled,
)
from hallmark.transport.ssh import SshBackend

pytestmark = pytest.mark.ssh_integration


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
    with OperationContext(RemoteSpec.from_url(url)) as context:
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
    result = download_selection(repo, repo.worktree, [(Path('missing'), None)])
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
    with OperationContext(RemoteSpec.from_url(server["url"])) as context:
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
    with OperationContext(RemoteSpec.from_url(server["url"], "one")) as one:
        with OperationContext(RemoteSpec.from_url(server["url"], "two")) as two:
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
    with OperationContext(RemoteSpec.from_url(server["url"])) as survivor:
        survivor.transport.prepare()
        with OperationContext(RemoteSpec.from_url(server["url"])) as context:
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
    with OperationContext(RemoteSpec.from_url(ssh_server["url"])) as context:
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
def test_sftp_only_init_plans_then_requires_payload_approval(
    ssh_server, tmp_path, monkeypatch, scheme,
):
    root = ssh_server["root"]
    (root / "nested").mkdir()
    (root / "nested" / "science.fits").write_bytes(b"science")
    (root / "notes.txt").write_bytes(b"notes")
    fetched = []
    fetch = SshBackend._download_file

    def record_fetch(self, path, destination, file_limit=None):
        fetched.append(path)
        return fetch(self, path, destination, file_limit)

    def reject_git_probe(*args, **kwargs):
        raise AssertionError("SFTP directory detection must not invoke remote Git")

    monkeypatch.setattr(SshBackend, "_download_file", record_fetch)
    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", reject_git_probe)
    plans = []

    def decline(plan):
        plans.append(plan)
        return False

    repo = Repo.init(tmp_path / "initialized")
    repo.add(ssh_server["url"].replace("ssh:", scheme + ":"), filter="**/*.fits")
    decline(repo.plan_download())
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
    with OperationContext(RemoteSpec.from_url(ssh_server["url"])) as context:
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
    result = download_selection(
        repo, repo.worktree, [(Path(str(i)), None) for i in range(4)], max_workers=4)
    assert result["succeeded"] == 4
    assert result["failed"] == 0


def test_ssh_listing_omits_symlinks_and_rejects_controls(ssh_server, tmp_path):
    root = ssh_server["root"]
    (root / "item").write_bytes(b"data")
    (root / "link").symlink_to(root / "item")
    with OperationContext(RemoteSpec.from_url(ssh_server["url"])) as context:
        assert [entry.path for entry in context.transport.iter_entries()] == ["item"]
    (root / "bad\nname").write_bytes(b"data")
    with OperationContext(RemoteSpec.from_url(ssh_server["url"])) as context:
        with pytest.raises(DownloadError, match="control"):
            list(context.transport.iter_entries())


def test_permission_denied_preserves_existing_file(ssh_server, tmp_path):
    source = ssh_server["root"] / "private"
    source.write_bytes(b"unreadable")
    source.chmod(0)
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url=ssh_server["url"])
    destination = repo.worktree / "private"
    destination.write_bytes(b"original")
    try:
        result = download_selection(repo, repo.worktree, [(Path('private'), None)])
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
    prepare = SshBackend.prepare

    def slow_prepare(self):
        prepare(self)
        self.context.settings = replace(
            self.context.settings, shutdown_timeout=1, transfer_timeout=15
        )
        self.sftp = ["sftp", "-B", "1024", "-R", "1", "-l", "8"]
        if self not in transports:
            transports.append(self)

    monkeypatch.setattr(SshBackend, "prepare", slow_prepare)
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(
            download_selection,
            repo,
            repo.worktree,
            [(Path("large"), None)])
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


def test_add_commit_download_clone_workflow(ssh_server, tmp_path):
    root = ssh_server["root"]
    (root / "nested").mkdir()
    (root / "nested/item_1.dat").write_bytes(b"science")
    (root / "bad_2.dat").write_bytes(b"changed")
    strong = hashlib.sha256(b"science").hexdigest()
    (root / "sha256sums").write_text(
        strong + "  nested/item_1.dat\n" + "a" * 64 + "  bad_2.dat\n")
    repo = Repo.init(tmp_path / "catalog")
    repo.add(ssh_server["url"], filter="**/*.dat")
    assert not (repo.worktree / "nested").exists()
    repo.commit("Record remote files")
    result = repo.download(repo.plan_download(), approved=True)
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert (repo.worktree / "nested/item_1.dat").read_bytes() == b"science"
    assert not (repo.worktree / "bad_2.dat").exists()
    assert not list(repo.worktree.rglob("*.part"))
    clone = Repo.clone(str(repo.dothm.path), tmp_path / "python-clone", download=False)
    assert clone.dothm.head.commit.hexsha == repo.dothm.head.commit.hexsha
    assert not (clone.worktree / "nested").exists()
    result = CliRunner().invoke(hallmark, [
        "clone", str(repo.dothm.path), str(tmp_path / "cli-clone"),
        "--filter", "nested/*.dat"], input="y\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cli-clone/nested/item_1.dat").read_bytes() == b"science"


def test_add_records_profile_reference(ssh_server, tmp_path, monkeypatch):
    auth = tmp_path / "auth.yml"
    auth.write_text("version: 1\nprofiles:\n  lab:\n    hosts: [hm-test]\n")
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(auth))
    (ssh_server["root"] / "item.dat").write_bytes(b"science")
    repo = Repo.init(tmp_path / "catalog")
    repo.add(ssh_server["url"], auth="lab")
    assert Repo(repo.worktree).state.config["remote"][0]["auth"] == "lab"
