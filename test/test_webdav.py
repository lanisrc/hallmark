"""WebDAV discovery with PROPFIND against a loopback server."""

import hashlib
from urllib.parse import quote

import pytest

from hallmark import Repo
from hallmark.remote.discovery import discover_remote_files
from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport.base import CapabilityError, DownloadError
from http_server import LocalServer

MTIME = "Mon, 05 Oct 2026 12:00:00 GMT"


def multistatus(*responses):
    return ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:oc="http://owncloud.org/ns">' + "".join(responses)
            + "</d:multistatus>").encode()


def member(href, *, collection=False, size=None, checksums=None, status=200):
    props = ("<d:resourcetype><d:collection/></d:resourcetype>" if collection
             else "<d:resourcetype/>")
    missing = ""
    if size is not None:
        props += f"<d:getcontentlength>{size}</d:getcontentlength>"
    props += f"<d:getlastmodified>{MTIME}</d:getlastmodified>"
    if checksums:
        props += f"<oc:checksums><oc:checksum>{checksums}</oc:checksum></oc:checksums>"
    else:
        missing = ("<d:propstat><d:prop><oc:checksums/></d:prop>"
                   "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>")
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}"
            f"</d:prop><d:status>HTTP/1.1 {status} OK</d:status></d:propstat>"
            f"{missing}</d:response>")


@pytest.fixture
def dav():
    server = LocalServer()
    files = {"a b#1.fits": b"alpha", "sub/c.fits": b"charlie"}
    server.files.update(files)
    server.listings = {}

    def propfind(handler, path):
        if handler.headers.get("Depth") != "1":
            handler.send_error(400)
            return
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
    sha1 = hashlib.sha1(b"alpha").hexdigest()
    md5 = hashlib.md5(b"alpha").hexdigest()
    port = server.httpd.server_address[1]
    server.listings["/data/"] = multistatus(
        member("/data/", collection=True),
        member("/data/" + quote("a b#1.fits"), size=5,
               checksums=f"SHA1:{sha1} MD5:{md5} ADLER32:0a1b2c3d"),
        member(f"http://127.0.0.1:{port}/data/sub/", collection=True),
        member("/elsewhere/secret.fits", size=1),
        member("/data/.hm/", collection=True),
    )
    server.listings["/data/sub/"] = multistatus(
        member("/data/sub/", collection=True),
        member("c.fits", size=7),
    )
    yield server
    server.close()


def test_propfind_lists_collections_with_sizes_and_server_checksums(dav):
    remote = RemoteSpec.from_url(dav.url, backend="webdav")
    with OperationContext(remote) as context:
        entries = {entry.path: entry for entry in discover_remote_files(context)}
    assert sorted(entries) == ["a b#1.fits", "sub/c.fits"]
    first = entries["a b#1.fits"]
    assert (first.size, first.checksum_algorithm, first.checksum_source) == (
        5, "sha1", "server")
    assert first.checksum == hashlib.sha1(b"alpha").hexdigest()
    assert first.mtime == 1791201600
    assert entries["sub/c.fits"].checksum is None
    assert [method for method, _, _ in dav.requests] == ["PROPFIND", "PROPFIND"]


def test_webdav_urls_add_and_download_through_the_backend(dav, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.add(dav.url.replace("http://", "webdav://"), filter="**/*.fits")
    assert repo.state.config["remote"][0] == {
        "name": "origin", "url": dav.url, "backend": "webdav"}
    data = repo.state.data.set_index("path")
    assert data.loc["a b#1.fits", "checksum_source"] == "server"
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    assert result["succeeded"] == 2
    assert (repo.worktree / "a b#1.fits").read_bytes() == b"alpha"
    with pytest.raises(ValueError, match="webdav backend"):
        repo.add(dav.url.replace("http://", "webdav://"), backend="http")


def test_servers_without_webdav_are_reported(dav):
    dav.handlers.pop("PROPFIND")

    def unsupported(handler, path):
        handler.send_error(405)

    dav.handlers["PROPFIND"] = unsupported
    with OperationContext(RemoteSpec.from_url(dav.url, backend="webdav")) as context:
        with pytest.raises(CapabilityError, match="WebDAV"):
            discover_remote_files(context)


@pytest.mark.parametrize("body, message", [
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
     "document type"),
    (b"<d:multistatus xmlns:d='DAV:'><d:response>", "not valid XML"),
    (multistatus(member("/data/x", size=1)) * 400, "size limit"),
])
def test_unsafe_or_oversized_listings_are_refused(dav, body, message):
    dav.listings["/data/"] = body
    with OperationContext(RemoteSpec.from_url(dav.url, backend="webdav")) as context:
        context.text_limit = 4096
        with pytest.raises(DownloadError, match=message):
            discover_remote_files(context)


def test_unsafe_hrefs_are_rejected(dav):
    dav.listings["/data/"] = multistatus(member("/data/%2e%2e/secret", size=1))
    with OperationContext(RemoteSpec.from_url(dav.url, backend="webdav")) as context:
        with pytest.raises(DownloadError, match="unsafe"):
            discover_remote_files(context)


def test_cyverse_selects_webdav_for_signed_in_access():
    assert RemoteSpec.from_url(
        "https://data.cyverse.org/dav/iplant/home/alice/").backend == "webdav"
    assert RemoteSpec.from_url(
        "https://data.cyverse.org/dav-anon/iplant/commons/").backend == "cyverse"
