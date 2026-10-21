"""HTTP(S) auth profiles against a real loopback server."""

import base64
import os
import sys

import pytest
import yaml

from hallmark import Repo
from hallmark.transport.base import DownloadError, RemoteConfigurationError
from http_server import LocalServer


@pytest.fixture
def server():
    server = LocalServer()
    server.files.update({"a.fits": b"alpha", "sub/b.fits": b"bravo"})
    yield server
    server.close()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))


def write_auth(monkeypatch, tmp_path, profiles):
    path = tmp_path / "auth.yml"
    path.write_text(yaml.safe_dump({"version": 2, "profiles": profiles}))
    os.chmod(path, 0o600)
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(path))


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_basic_profile_authenticates_discovery_and_download(
        server, monkeypatch, tmp_path):
    server.authorization = basic("alice", "pw")
    monkeypatch.setenv("HM_DATA_PASSWORD", "pw")
    write_auth(monkeypatch, tmp_path, {"lab": {
        "type": "http", "hosts": ["127.0.0.1"], "scheme": "basic",
        "username": "alice", "password": {"env": "HM_DATA_PASSWORD"}}})
    repo = Repo.init(tmp_path / "repo")
    repo.add(server.url, auth="lab")
    assert repo.state.data.path.tolist() == ["a.fits", "sub/b.fits"]
    assert repo.state.config["remote"][0]["auth"] == "lab"
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    assert result["succeeded"] == 2
    assert (repo.worktree / "sub/b.fits").read_bytes() == b"bravo"
    assert {header for _, _, header in server.requests} == {basic("alice", "pw")}


def test_bearer_token_from_a_helper_command(server, monkeypatch, tmp_path):
    server.authorization = "Bearer tok-123"
    write_auth(monkeypatch, tmp_path, {"api": {
        "type": "http", "hosts": ["127.0.0.1"], "scheme": "bearer",
        "token": {"command": [sys.executable, "-c", "print('tok-123')"]}}})
    repo = Repo.init(tmp_path / "repo")
    repo.add(server.url, auth="api", filter="*.fits")
    assert repo.state.data.path.tolist() == ["a.fits"]


def test_unauthenticated_requests_explain_how_to_authenticate(server, tmp_path):
    server.authorization = basic("alice", "pw")
    repo = Repo.init(tmp_path / "repo")
    with pytest.raises(DownloadError, match="HTTP 401.*--auth"):
        repo.add(server.url)


def test_missing_secrets_fail_before_any_request(server, monkeypatch, tmp_path):
    monkeypatch.delenv("HM_DATA_PASSWORD", raising=False)
    write_auth(monkeypatch, tmp_path, {"lab": {
        "type": "http", "hosts": ["127.0.0.1"], "scheme": "basic",
        "username": "alice", "password": {"env": "HM_DATA_PASSWORD"}}})
    repo = Repo.init(tmp_path / "repo")
    with pytest.raises(RemoteConfigurationError, match="HM_DATA_PASSWORD"):
        repo.add(server.url, auth="lab")
    with pytest.raises(RemoteConfigurationError, match="not bound"):
        repo.add(server.url.replace("127.0.0.1", "localhost"), auth="lab")
    assert server.requests == []


def test_credentials_do_not_follow_redirects_to_other_servers(
        server, monkeypatch, tmp_path):
    mirror = LocalServer()
    try:
        mirror.files["b.fits"] = b"bravo"
        server.authorization = basic("alice", "pw")
        server.redirects["/data/sub/b.fits"] = mirror.url + "b.fits"
        monkeypatch.setenv("HM_DATA_PASSWORD", "pw")
        write_auth(monkeypatch, tmp_path, {"lab": {
            "type": "http", "hosts": ["127.0.0.1"], "scheme": "basic",
            "username": "alice", "password": {"env": "HM_DATA_PASSWORD"}}})
        repo = Repo.init(tmp_path / "repo")
        repo.add(server.url, auth="lab")
        result = repo.download(repo.plan_download(file_paths=["sub/b.fits"]),
                               approved=True)
        assert result["succeeded"] == 1
        # Another port is another server: the header is dropped on redirect.
        assert mirror.requests == [("GET", "/data/b.fits", None)]
    finally:
        mirror.close()


def test_downloads_keep_a_files_content_encoding(tmp_path):
    import gzip
    from hallmark.transport import OperationContext, RemoteSpec

    payload = gzip.compress(b"visibilities\n")
    server = LocalServer()
    headers = []

    def get(handler, path):
        headers.append(handler.headers.get("Accept-Encoding"))
        handler.send_response(200)
        # Apache's AddEncoding labels .gz files this way.
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    server.handlers["GET"] = get
    try:
        with OperationContext(RemoteSpec.from_url(server.url)) as context:
            context.transport.fetch("a.fits.gz", tmp_path / "a.fits.gz")
    finally:
        server.close()
    assert (tmp_path / "a.fits.gz").read_bytes() == payload
    assert headers == ["identity"]


@pytest.mark.parametrize("content_type, body, text", [
    ("text/plain", "ünï.fits\n".encode(), "ünï.fits\n"),
    ("text/plain; charset=ISO-8859-1", "ünï.fits\n".encode("latin-1"), "ünï.fits\n"),
])
def test_metadata_uses_the_declared_charset_or_utf8(content_type, body, text):
    from hallmark.transport import OperationContext, RemoteSpec

    server = LocalServer()

    def get(handler, path):
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    server.handlers["GET"] = get
    try:
        with OperationContext(RemoteSpec.from_url(server.url)) as context:
            assert context.read_text("SHA256SUMS") == text
    finally:
        server.close()


def test_metadata_that_is_not_text_is_refused_not_garbled():
    from hallmark.transport import OperationContext, RemoteSpec

    server = LocalServer()
    server.files["SHA256SUMS"] = b"\xff\xfe broken\n"
    try:
        with OperationContext(RemoteSpec.from_url(server.url)) as context:
            with pytest.raises(DownloadError, match="not valid utf-8"):
                context.read_text("SHA256SUMS")
    finally:
        server.close()
