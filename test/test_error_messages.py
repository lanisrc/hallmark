"""What users read when downloads, scans and clones fail (#38).

Each scenario runs ``hm`` against a real loopback server and checks the
message: which file failed, at which stage, why, and how to fix access.
"""

import base64
import hashlib
import os
import sys

import pytest
import requests
import yaml
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport.base import (
    AccessRefused, DownloadError, RemoteConfigurationError, RemoteObjectMissing,
    _valid_http_host)
from http_server import LocalServer
from survey import Hm

FILES = {"a.fits": b"alpha" * 20, "x/same.fits": b"one" * 20,
         "y/same.fits": b"two" * 20, "sub/c.fits": b"charlie" * 20,
         "d.fits": b"delta" * 20, "e.fits": b"echo" * 20}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))


@pytest.fixture
def server():
    server = LocalServer()
    yield server
    server.close()


@pytest.fixture
def hm():
    return Hm()


def lab_profile(monkeypatch, tmp_path):
    """Define an HTTP profile ``lab`` for alice; return the header it sends."""
    path = tmp_path / "auth.yml"
    path.write_text(yaml.safe_dump({"version": 2, "profiles": {"lab": {
        "type": "http", "hosts": ["127.0.0.1"], "scheme": "basic",
        "username": "alice", "password": {"env": "HM_TEST_PASSWORD"}}}}))
    os.chmod(path, 0o600)
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(path))
    monkeypatch.setenv("HM_TEST_PASSWORD", "pw")
    return "Basic " + base64.b64encode(b"alice:pw").decode()


def catalogue(hm, server, path, files=FILES, *args):
    server.files.update(files)
    hm("init", path, cwd=path.parent)
    hm("add", server.url, *args, cwd=path)
    hm("commit", "-m", "Catalog", cwd=path)
    return path


def serve_with_faults(server, faults):
    """
    Answer GETs for a path in ``faults`` with a status, half its body, or
    (``"drop"``) a closed connection and no reply.
    """
    def get(handler, path):
        relative = path[len("/data/"):]
        fault, body = faults.get(relative), server.files.get(relative)
        if isinstance(fault, int) or body is None:
            handler.send_error(fault or 404)
        elif fault == "drop":
            handler.close_connection = True
        elif fault == "truncate":
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body[:len(body) // 2])
            handler.close_connection = True
        else:
            handler._send(body)

    server.handlers["GET"] = get


def publish(repo, server, skip=()):
    """Serve a repository's catalog files as a snapshot; return its URL."""
    for path in (repo / ".hm").iterdir():
        if path.is_file() and path.name not in skip:
            server.files["cat/" + path.name] = path.read_bytes()
    return server.url + "cat/"


def error_lines(output):
    return [line for line in output.splitlines() if line.startswith("  - ")]


def test_each_failed_file_is_named_with_its_cause(hm, server, tmp_path):
    files = dict(FILES, SHA256SUMS="".join(
        f"{hashlib.sha256(data).hexdigest()}  {path}\n"
        for path, data in FILES.items()).encode())
    repo = catalogue(hm, server, tmp_path / "repo", files)
    host = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    server.files["e.fits"] = b"ECHO" * 20
    serve_with_faults(server, {"y/same.fits": 404, "a.fits": 500,
                               "d.fits": "truncate"})
    result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
    lines = error_lines(result.output)
    assert sorted(lines) == sorted([
        f"  - a.fits: Download from {host} failed: server error (HTTP 500); "
        "try again later",
        f"  - y/same.fits: Download from {host} failed: not found on the server",
        # urllib3 2 reports the short body; older releases leave it to the
        # published checksum.
        next(line for line in lines if line.startswith("  - d.fits: ")),
        "  - e.fits: Checksum mismatch (sha256)",
    ]), result.output
    assert "Error: Failed to download 4 file(s)" in result.output
    assert "Traceback" not in result.output and "auth profile" not in result.output
    # The nested file and its same-named sibling still arrive.
    assert (repo / "x/same.fits").read_bytes() == FILES["x/same.fits"]
    assert (repo / "sub/c.fits").read_bytes() == FILES["sub/c.fits"]
    assert not (repo / "y/same.fits").exists()


def test_a_nested_file_downloads_beside_a_missing_sibling(hm, server, tmp_path):
    # The store's temporary file is named after the file alone: a "/" in its
    # prefix would fail every nested file.
    repo = catalogue(hm, server, tmp_path / "repo",
                     {"sub/b.fits": b"bravo", "sub/c.fits": b"charlie"})
    del server.files["sub/c.fits"]
    result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
    assert (repo / "sub/b.fits").read_bytes() == b"bravo"
    assert error_lines(result.output) == [
        f"  - sub/c.fits: Download from {RemoteSpec.from_url(server.url).display} "
        "failed: not found on the server"]
    assert "Staged SHA-1 for 1 file(s); run hm commit" in result.output


def test_an_unreachable_server_is_named_without_a_class_name(hm, server, tmp_path):
    repo = catalogue(hm, server, tmp_path / "repo", {"a.fits": b"alpha"})
    server.close()
    result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
    assert error_lines(result.output) == [
        f"  - a.fits: Download from {RemoteSpec.from_url(server.url).display} "
        "failed: could not connect; check the URL and network"]
    result = hm("add", server.url, cwd=repo, expect=1)
    assert ("Remote scan failed (previous catalogue kept): Listing the dataset "
            "root on http://127.0.0.1:") in result.output
    assert "ConnectionError" not in result.output


def test_a_connection_closed_before_replying_is_a_reset(hm, server, tmp_path):
    repo = catalogue(hm, server, tmp_path / "repo", {"a.fits": b"a", "b.fits": b"b"})
    serve_with_faults(server, {"a.fits": "drop"})
    result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
    assert error_lines(result.output) == [
        f"  - a.fits: Download from {RemoteSpec.from_url(server.url).display} "
        "failed: the connection was reset or the body was truncated"]
    assert (repo / "b.fits").read_bytes() == b"b"


def test_listings_name_the_folder_that_failed(hm, server, tmp_path):
    repo = tmp_path / "repo"
    hm("init", repo, cwd=tmp_path)
    server.files["sub/c.fits"] = b"charlie"
    display = RemoteSpec.from_url(server.url).display
    result = hm("add", server.url + "missing/", cwd=repo, expect=1)
    assert (f"Error: Remote scan failed (previous catalogue kept): Listing the "
            f"dataset root on {display} failed: not found on the server"
            ) in result.output
    server.handlers["GET"] = lambda handler, path: (
        handler.send_error(404) if path.endswith("/sub/")
        else handler._send(b'<a href="sub/">sub/</a>', "text/html"))
    result = hm("add", server.url, cwd=repo, expect=1)
    assert f"Listing sub/ on {display} failed: not found" in result.output


def test_download_suggests_an_auth_profile_once(hm, server, tmp_path):
    repo = catalogue(hm, server, tmp_path / "repo", {"a.fits": b"a", "b.fits": b"b"})
    server.authorization = "Basic secret"
    result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
    display = RemoteSpec.from_url(server.url).display
    assert sorted(error_lines(result.output)) == [
        f"  - {name}: Download from {display} failed: access refused (HTTP 401)"
        for name in ("a.fits", "b.fits")]
    hint = ("Set an auth profile for remote 'origin': hm set-config "
            "--remote-name origin --remote-auth PROFILE (or use ~/.netrc)")
    assert result.output.count(hint) == 1
    assert "--auth PROFILE" not in result.output


def test_following_the_download_hint_works(hm, server, tmp_path, monkeypatch):
    repo = catalogue(hm, server, tmp_path / "repo", {"a.fits": b"a"})
    server.authorization = lab_profile(monkeypatch, tmp_path)
    hm("set-config", "--remote-name", "origin", "--remote-auth", "lab", cwd=repo)
    hm("download", "--all", cwd=repo, input="y\n")
    assert (repo / "a.fits").read_bytes() == b"a"


def test_a_refused_profile_is_named(hm, server, tmp_path, monkeypatch):
    repo = catalogue(hm, server, tmp_path / "repo", {"a.fits": b"a"})
    server.authorization = lab_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("HM_TEST_PASSWORD", "wrong")
    hm("set-config", "--remote-auth", "lab", cwd=repo)
    result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
    assert "Check that auth profile 'lab' grants access" in result.output
    result = hm("add", server.url, "--auth", "lab", cwd=repo, expect=1)
    assert "access refused (HTTP 401)" in result.output
    assert "Check that auth profile 'lab' grants access" in result.output
    # Without --auth, add uses the profile the remote already names.
    result = hm("add", server.url, cwd=repo, expect=1)
    assert result.output.endswith(
        "access refused (HTTP 401)\nCheck that auth profile 'lab' grants access\n"
    ), result.output


def test_only_the_refusing_remote_is_named(hm, server, tmp_path):
    mirror = LocalServer()
    try:
        repo = catalogue(hm, server, tmp_path / "repo", {"a.fits": b"a"})
        mirror.files["b.fits"] = b"b"
        hm("add", mirror.url, "--remote", "mirror", cwd=repo)
        hm("commit", "-m", "Mirror", cwd=repo)
        mirror.authorization = "Basic secret"
        result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
        assert (repo / "a.fits").read_bytes() == b"a"
        assert result.output.count("Set an auth profile") == 1
        assert ("Set an auth profile for remote 'mirror': hm set-config "
                "--remote-name mirror --remote-auth PROFILE") in result.output
    finally:
        mirror.close()


def test_add_suggests_its_auth_option(hm, server, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    hm("init", repo, cwd=tmp_path)
    server.files["a.fits"] = b"a"
    server.authorization = lab_profile(monkeypatch, tmp_path)
    result = hm("add", server.url, cwd=repo, expect=1)
    assert result.output.endswith(
        "failed: access refused (HTTP 401)\nName an auth profile: rerun hm add "
        "with --auth PROFILE (or use ~/.netrc)\n"), result.output
    hm("add", server.url, "--auth", "lab", cwd=repo)


def test_clone_suggests_auth_for_the_catalog_server(hm, server, tmp_path,
                                                    monkeypatch):
    source = catalogue(hm, server, tmp_path / "source", {"a.fits": b"a"})
    catalog = LocalServer()
    try:
        url = publish(source, catalog)
        catalog.authorization = lab_profile(monkeypatch, tmp_path)
        target = tmp_path / "copy"
        result = hm("clone", url, target, "--no-download", cwd=tmp_path, expect=1)
        assert result.output.startswith(
            f"Could not read the published catalog at {url}: Reading config.yml "
            f"from {RemoteSpec.from_url(url).display} failed: access refused "
            "(HTTP 401)\nName an auth profile for the catalog server: hm clone "
            "--auth PROFILE URL PATH (or use ~/.netrc)\n"), result.output
        assert "download" not in result.output.lower()
        assert not target.exists()
        hm("clone", url, target, "--no-download", "--auth", "lab", cwd=tmp_path)
    finally:
        catalog.close()


def test_clone_names_a_catalog_server_error_without_downloading(
        hm, server, tmp_path):
    source = catalogue(hm, server, tmp_path / "source", {"a.fits": b"a"})
    url = publish(source, server)
    serve_with_faults(server, {"cat/config.yml": 500})
    result = hm("clone", url, tmp_path / "copy", "--no-download", cwd=tmp_path,
                expect=1)
    assert result.output == (
        f"Could not read the published catalog at {url}: Reading config.yml "
        f"from {RemoteSpec.from_url(url).display} failed: server error "
        "(HTTP 500); try again later\n")


@pytest.mark.parametrize("missing", ["runs.tsv", "meta.yml", "data.tsv"])
def test_clone_names_a_missing_catalog_file(hm, server, tmp_path, missing):
    source = catalogue(hm, server, tmp_path / "source",
                       {"run1.h5": b"1", "run2.h5": b"2"}, "--fmt", "run{n}.h5=runs")
    url = publish(source, server, skip={missing})
    result = hm("clone", url, tmp_path / "copy", "--no-download", cwd=tmp_path,
                expect=1)
    assert result.output == f"Published catalog at {url} is missing {missing}\n"


def test_clone_reports_partial_downloads_like_download(hm, server, tmp_path):
    files = {f"f{index:02d}.fits": b"x%d" % index for index in range(12)}
    source = catalogue(hm, server, tmp_path / "source", files)
    url = publish(source, server)
    for name in list(files)[1:]:
        del server.files[name]
    target = tmp_path / "copy"
    result = hm("clone", url, target, cwd=tmp_path, input="y\n", expect=1)
    output = result.output
    assert "Staged SHA-1 for 1 file(s); run hm commit to record them" in output
    assert "Download completed with errors: 1 succeeded, 11 failed" in output
    assert "  - f01.fits: Download from " in output
    assert "  - ... 1 more error(s)" in output
    assert (f'Catalog kept at "{target / ".hm"}"; run hm download --all from '
            f'"{target}" to retry.') in output
    assert output.rstrip().endswith("Error: Failed to download 11 file(s)")
    assert (target / "f00.fits").read_bytes() == b"x0"


def test_clone_data_stage_suggests_set_config(hm, server, tmp_path, monkeypatch):
    source = catalogue(hm, server, tmp_path / "source", {"a.fits": b"a"})
    catalog = LocalServer()
    try:
        url = publish(source, catalog)
        server.authorization = lab_profile(monkeypatch, tmp_path)
        target = tmp_path / "copy"
        result = hm("clone", url, target, cwd=tmp_path, input="y\n", expect=1)
        assert (f'Set an auth profile for remote \'origin\': cd "{target}", run '
                "hm set-config --remote-name origin --remote-auth PROFILE, then "
                "hm download --all") in result.output
        assert "hm clone --auth" not in result.output
        hm("set-config", "--remote-name", "origin", "--remote-auth", "lab",
           cwd=target)
        hm("download", "--all", cwd=target, input="y\n")
        assert (target / "a.fits").read_bytes() == b"a"
    finally:
        catalog.close()


def test_clone_data_stage_names_a_published_profile(hm, server, tmp_path,
                                                    monkeypatch):
    source = catalogue(hm, server, tmp_path / "source", {"a.fits": b"a"})
    hm("set-config", "--remote-auth", "lab", cwd=source)
    hm("commit", "-m", "Profile", cwd=source)
    catalog = LocalServer()
    try:
        url = publish(source, catalog)
        server.authorization = lab_profile(monkeypatch, tmp_path)
        monkeypatch.setenv("HM_TEST_PASSWORD", "wrong")
        result = hm("clone", url, tmp_path / "copy", cwd=tmp_path, input="y\n",
                    expect=1)
        assert "Check that auth profile 'lab' grants access" in result.output
        assert "Set an auth profile" not in result.output
    finally:
        catalog.close()


def test_a_clone_download_error_keeps_its_results(server, tmp_path):
    from hallmark import Repo

    source = Repo.init(tmp_path / "source")
    server.files["a.fits"] = b"a"
    source.add(server.url)
    source.commit("Catalog")
    del server.files["a.fits"]
    with pytest.raises(DownloadError) as error:
        Repo.clone(str(source.dothm.path), tmp_path / "copy", approved=True)
    assert error.value.result["failed"] == 1
    assert error.value.result["errors"][0].startswith("a.fits: Download from ")


@pytest.mark.parametrize("url", ["http://re mote/data/", "https://a..b/data/",
                                 "http://ex!ample/data/"])
def test_an_invalid_host_is_refused(hm, tmp_path, url):
    with pytest.raises(RemoteConfigurationError, match="Invalid host in URL"):
        RemoteSpec.from_url(url)
    hm("init", tmp_path / "repo", cwd=tmp_path)
    result = hm("add", url, cwd=tmp_path / "repo", expect=1)
    assert "Error: Invalid host in URL" in result.output


def test_ipv6_hosts_must_be_addresses():
    # Python 3.11 and newer already refuse a bracketed host that is not one.
    assert _valid_http_host("::1") and _valid_http_host("fe80::1")
    assert not _valid_http_host("::zz")


@pytest.mark.parametrize("url, display", [
    ("http://127.0.0.1:8080/data/", "http://127.0.0.1:8080"),
    ("https://example.org/data/", "https://example.org"),
    ("https://bücher.example/data/", "https://bücher.example"),
    ("http://[::1]:8080/data/", "http://[::1]:8080"),
    ("sftp://alice@lab:2222/srv/data/", "sftp://lab:2222"),
    ("s3://bucket/prefix/", "s3://bucket"),
])
def test_display_shows_the_port_but_no_user_or_path(url, display):
    assert RemoteSpec.from_url(url).display == display


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError("https://user:secret@x.test/?token=secret",
                              response=response)


@pytest.mark.parametrize("exc, kind, cause", [
    (http_error(404), RemoteObjectMissing, "not found on the server"),
    (http_error(401), AccessRefused, "access refused (HTTP 401)"),
    (http_error(403), AccessRefused, "access refused (HTTP 403)"),
    (http_error(503), DownloadError, "server error (HTTP 503); try again later"),
    (http_error(429), DownloadError, "HTTP 429"),
    (requests.ReadTimeout("secret"), DownloadError, "no response in time"),
    (ReadTimeoutError(None, "secret", "read timed out"), DownloadError,
     "no response in time"),
    (requests.ConnectionError(ReadTimeoutError(None, "secret", "timed out")),
     DownloadError, "no response in time"),
    (requests.exceptions.SSLError("secret"), DownloadError,
     "certificate could not be verified"),
    (requests.ConnectionError("https://user:secret@x.test/"), DownloadError,
     "could not connect; check the URL and network"),
    (ProtocolError("secret"), DownloadError,
     "the connection was reset or the body was truncated"),
    # A server that accepts the connection, then closes it before replying.
    (requests.ConnectionError(ProtocolError(
        "Connection aborted.", ConnectionResetError(104, "secret"))), DownloadError,
     "the connection was reset or the body was truncated"),
    (requests.exceptions.ChunkedEncodingError("secret"), DownloadError,
     "the connection was reset or the body was truncated"),
    (requests.TooManyRedirects("secret"), DownloadError, "TooManyRedirects"),
])
def test_http_failures_are_described_by_kind(exc, kind, cause):
    with OperationContext(RemoteSpec.from_url("http://127.0.0.1:9/data/")) as context:
        error = context.transport._download_error(exc, "Download from here")
    assert type(error) is kind
    assert str(error) == f"Download from here failed: {cause}"


def test_a_failed_sftp_transfer_names_its_path(monkeypatch, tmp_path):
    with OperationContext(RemoteSpec.from_url("sftp://lab:2222/srv/data/")) as context:
        transport = context.transport
        monkeypatch.setattr(transport, "prepare", lambda: None)
        transport.sftp = [sys.executable, "-c", "import sys; sys.exit(1)"]
        with pytest.raises(DownloadError, match="^SSH transfer of sub/c.fits failed "
                                                "on sftp://lab:2222; check access"):
            transport.fetch("sub/c.fits", tmp_path / "copy")


@pytest.mark.parametrize("error", [
    requests.ConnectionError("https://user:secret@x.test/?token=secret"),
    ProtocolError("https://user:secret@x.test/?token=secret"),
])
def test_stray_network_errors_never_echo_their_url(hm, tmp_path, monkeypatch,
                                                   error):
    from hallmark import Repo

    def fail(*args, **kwargs):
        raise error

    hm("init", tmp_path / "repo", cwd=tmp_path)
    monkeypatch.setattr(Repo, "status", fail)
    monkeypatch.setattr(Repo, "add", fail)
    name = type(error).__name__
    for args in (["status"], ["add", "https://x.test/data/"]):
        result = hm(*args, cwd=tmp_path / "repo", expect=1)
        assert result.output == f"Error: {name} (network error)\n"


def test_a_local_profile_problem_is_not_a_catalog_read_error(
        server, tmp_path, monkeypatch):
    from hallmark import Repo

    lab_profile(monkeypatch, tmp_path)
    monkeypatch.delenv("HM_TEST_PASSWORD")
    with pytest.raises(RemoteConfigurationError, match="HM_TEST_PASSWORD"):
        Repo.clone(server.url + "cat/", tmp_path / "copy", auth="lab",
                   download=False)


def test_auth_advice_names_the_remote_and_its_command(capsys):
    from click import ClickException

    from hallmark.cli import _refusal_hint, _remote_auth, _report_download_results

    results = {"succeeded": 0, "failed": 1, "total_bytes": 0,
               "errors": ["a.fits: refused"], "access_refused": {"origin": "lab"}}
    with pytest.raises(ClickException):
        _report_download_results(results)
    assert capsys.readouterr().err.splitlines()[-1] == "  - a.fits: refused"
    with pytest.raises(ClickException):
        _report_download_results(
            results, lambda name, profile: f"Advice for {name} using {profile}")
    assert capsys.readouterr().err.splitlines()[-1] == "Advice for origin using lab"

    assert _remote_auth(None) == (
        "the data remote", "hm set-config --remote-auth PROFILE")
    assert _remote_auth("lab data") == (
        "remote 'lab data'",
        "hm set-config --remote-name 'lab data' --remote-auth PROFILE")
    assert _refusal_hint(None, "Fix it", "s3://bucket/") == "Fix it"
    assert _refusal_hint(None, "Fix it", "HTTPS://x/") == "Fix it (or use ~/.netrc)"
    # hm add takes webdav(s):// URLs, which are HTTP(S) requests too.
    assert _refusal_hint(None, "Fix it", "webdavs://x/") == "Fix it (or use ~/.netrc)"
    assert _refusal_hint("lab", "Fix it") == (
        "Check that auth profile 'lab' grants access")
