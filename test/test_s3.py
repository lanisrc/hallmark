"""S3-compatible remotes, tested against moto's in-memory S3."""

import hashlib
import os
import sys

import pytest
import yaml

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from botocore import UNSIGNED  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from hallmark import Repo  # noqa: E402
from hallmark.remote.discovery import discover_remote_files  # noqa: E402
from hallmark.transport import OperationContext, RemoteSpec  # noqa: E402
from hallmark.transport.base import (  # noqa: E402
    CapabilityError, DownloadError, RemoteConfigurationError, RemoteObjectMissing)
from hallmark.transport.s3 import S3Backend  # noqa: E402

BUCKET = "ehtc-sims"
ENDPOINT = "https://osn.example.org"


@pytest.fixture
def s3(monkeypatch, tmp_path):
    """Isolate AWS settings and serve an in-memory bucket."""
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "HALLMARK_AUTH_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-creds"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPOINT)
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.put_object(Bucket=BUCKET, Key="m87/a.h5", Body=b"alpha",
                          ChecksumAlgorithm="SHA256")
        client.put_object(Bucket=BUCKET, Key="m87/sub/b.h5", Body=b"bravo")
        client.put_object(Bucket=BUCKET, Key="m87/sub/", Body=b"")
        client.put_object(Bucket=BUCKET, Key="m87/.hm/config.yml", Body=b"x")
        client.put_object(Bucket=BUCKET, Key="other/c.h5", Body=b"charlie")
        yield client


def entries(url, **options):
    remote = RemoteSpec.from_url(url, backend_options=options)
    with OperationContext(remote) as context:
        return {entry.path: entry for entry in discover_remote_files(context)}


def test_listing_reads_sizes_and_full_object_checksums(s3):
    found = entries(f"s3://{BUCKET}/m87/")
    assert sorted(found) == ["a.h5", "sub/b.h5"]
    assert found["a.h5"].size == 5
    assert (found["a.h5"].checksum_algorithm, found["a.h5"].checksum_source) == (
        "sha256", "server")
    assert found["a.h5"].checksum == hashlib.sha256(b"alpha").hexdigest()
    assert found["sub/b.h5"].checksum is None
    assert isinstance(found["sub/b.h5"].mtime, int)


def test_etags_count_as_md5_only_when_requested(s3):
    found = entries(f"s3://{BUCKET}/m87/", etag_md5="auto")
    assert (found["sub/b.h5"].checksum_algorithm, found["sub/b.h5"].checksum) == (
        "md5", hashlib.md5(b"bravo").hexdigest())


def test_listing_pages_through_large_prefixes(s3):
    for index in range(7):
        s3.put_object(Bucket=BUCKET, Key=f"many/{index}.h5", Body=b"x")
    assert len(entries(f"s3://{BUCKET}/many/", page_size=2)) == 7


def test_add_and_download_verify_through_the_cache(s3, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    repo.add(f"s3://{BUCKET}/m87/{{name}}.h5")
    assert repo.state.config["remote"] == [
        {"name": "origin", "url": f"s3://{BUCKET}/m87/", "backend": "s3"}]
    repo.add(f"s3://{BUCKET}/other/", prefix="other")
    assert repo.state.data.path.tolist() == ["a.h5", "other/c.h5", "sub/b.h5"]
    result = repo.download(repo.plan_download(all_files=True), approved=True)
    assert (result["succeeded"], result["failed"]) == (3, 0)
    assert (repo.worktree / "a.h5").read_bytes() == b"alpha"
    assert (repo.worktree / "other/c.h5").read_bytes() == b"charlie"
    assert repo.objects.is_linked(hashlib.sha1(b"alpha").hexdigest(),
                                repo.worktree / "a.h5")
    s3.put_object(Bucket=BUCKET, Key="m87/a.h5", Body=b"ALPHA")
    (repo.worktree / "a.h5").unlink()
    repo.objects.object_path(hashlib.sha1(b"alpha").hexdigest()).unlink()
    result = repo.download(repo.plan_download(file_paths=["a.h5"]), approved=True)
    assert result["failed"] == 1 and "a.h5 (sha256)" in result["errors"][0]


def test_profiles_bind_keys_to_buckets_and_endpoints(s3, monkeypatch, tmp_path):
    auth = tmp_path / "auth.yml"
    auth.write_text(yaml.safe_dump({"version": 2, "profiles": {"osn": {
        "type": "s3", "buckets": [BUCKET], "endpoints": [ENDPOINT],
        "access_key_id": {"env": "OSN_KEY"},
        "secret_access_key": {"command": [sys.executable, "-c", "print('secret')"]},
    }}}))
    os.chmod(auth, 0o600)
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(auth))
    monkeypatch.setenv("OSN_KEY", "osn-key")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name)
    remote = RemoteSpec.from_url(f"s3://{BUCKET}/m87/", "osn",
                                 backend_options={"endpoint_url": ENDPOINT})
    with OperationContext(remote) as context:
        assert sorted(entry.path for entry in discover_remote_files(context)) == [
            "a.h5", "sub/b.h5"]
        credentials = context.transport._client._request_signer._credentials
        assert credentials.access_key == "osn-key"
        assert credentials.secret_key == "secret"
    elsewhere = RemoteSpec.from_url(f"s3://{BUCKET}/m87/", "osn",
                                    backend_options={"endpoint_url": "https://x.test"})
    with pytest.raises(RemoteConfigurationError, match="endpoint"):
        OperationContext(elsewhere)


def test_requests_are_unsigned_without_any_credentials(s3, monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name)
    with OperationContext(RemoteSpec.from_url(f"s3://{BUCKET}/m87/")) as context:
        context.transport.prepare()
        assert context.transport._client.meta.config.signature_version is UNSIGNED


def test_missing_objects_and_refusals_are_reported_by_code(s3, tmp_path):
    with OperationContext(RemoteSpec.from_url(f"s3://{BUCKET}/m87/")) as context:
        with pytest.raises(RemoteObjectMissing, match="NoSuchKey"):
            context.transport.fetch("gone.h5", tmp_path / "gone.h5")
        denied = ClientError({"Error": {"Code": "AccessDenied",
                                        "Message": "secret details"}}, "GetObject")
        error = context.transport._error(denied)
        assert "--auth" in str(error) and "secret details" not in str(error)


def test_unsafe_keys_and_options_are_refused(s3):
    s3.put_object(Bucket=BUCKET, Key="bad/a/../b.h5", Body=b"x")
    with pytest.raises(DownloadError, match="unsafe"):
        entries(f"s3://{BUCKET}/bad/")
    for options in ({"color": "blue"}, {"page_size": 5000}, {"etag_md5": "yes"},
                    {"endpoint_url": "ftp://x"}):
        with pytest.raises(RemoteConfigurationError):
            OperationContext(RemoteSpec.from_url(f"s3://{BUCKET}/",
                                                 backend_options=options))


def test_boto3_is_optional(s3, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    backend = S3Backend.__new__(S3Backend)
    with OperationContext(RemoteSpec.from_url(f"s3://{BUCKET}/")) as context:
        backend.__init__(context)
        with pytest.raises(CapabilityError, match=r"hallmark\[s3\]"):
            backend.prepare()
