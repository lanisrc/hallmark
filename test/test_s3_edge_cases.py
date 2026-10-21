"""Edge cases of the S3 backend, tested against moto's in-memory S3."""

import base64
import hashlib
import os

import pytest
import yaml

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from botocore.exceptions import (  # noqa: E402
    ClientError, EndpointConnectionError)

from hallmark.transport import OperationContext, RemoteSpec  # noqa: E402
from hallmark.transport.base import (  # noqa: E402
    DownloadError, RemoteConfigurationError, RemoteObjectMissing)
from hallmark.transport.s3 import S3Backend, _hex_digest  # noqa: E402

BUCKET = "ehtc-sims"
URL = f"s3://{BUCKET}/m87/"


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
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.put_object(Bucket=BUCKET, Key="m87/a.h5", Body=b"alpha",
                          ChecksumAlgorithm="SHA256")
        client.put_object(Bucket=BUCKET, Key="m87/b.h5", Body=b"bravo")
        yield client


def write_auth(monkeypatch, tmp_path, profile):
    path = tmp_path / "auth.yml"
    path.write_text(yaml.safe_dump({"version": 2, "profiles": {"lab": profile}}))
    os.chmod(path, 0o600)
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(path))


def credentials(context):
    context.transport.prepare()
    return context.transport._client._request_signer._credentials


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "body detail"}},
                       "GetObject")


@pytest.mark.parametrize("options, message", [
    ({"addressing_style": "dns"}, "addressing_style"),
    ({"anonymous": "yes"}, "anonymous"),
    ({"region": 5}, "region must be a name"),
    ({"page_size": True}, "page_size"),
    ({"endpoint_url": 5}, "endpoint_url"),
])
def test_invalid_backend_options_fail_before_any_request(s3, options, message):
    remote = RemoteSpec.from_url(URL, backend_options=options)
    with pytest.raises(RemoteConfigurationError, match=message):
        OperationContext(remote)


def test_s3_backends_need_an_s3_url(s3):
    with OperationContext(RemoteSpec.from_url("https://data.test/x/")) as context:
        with pytest.raises(RemoteConfigurationError, match="s3://BUCKET/"):
            S3Backend(context)


def test_s3_checksums_must_be_valid_base64():
    digest = hashlib.sha256(b"alpha").digest()
    assert _hex_digest(base64.b64encode(digest).decode()) == digest.hex()
    assert _hex_digest("not base64!") is None
    assert _hex_digest(None) is None


def test_profiles_can_name_an_aws_configuration_profile(s3, monkeypatch,
                                                         tmp_path):
    (tmp_path / "aws-config").write_text(
        "[profile research]\naws_access_key_id = research-key\n"
        "aws_secret_access_key = research-secret\n")
    write_auth(monkeypatch, tmp_path,
               {"type": "s3", "buckets": [BUCKET], "aws_profile": "research"})
    with OperationContext(RemoteSpec.from_url(URL, "lab")) as context:
        assert credentials(context).access_key == "research-key"
        assert sorted(entry.path for entry in context.transport.iter_entries()) \
            == ["a.h5", "b.h5"]


def test_a_missing_aws_profile_is_reported_by_its_error_type(s3, monkeypatch,
                                                              tmp_path):
    write_auth(monkeypatch, tmp_path,
               {"type": "s3", "buckets": [BUCKET], "aws_profile": "absent"})
    with OperationContext(RemoteSpec.from_url(URL, "lab")) as context:
        with pytest.raises(RemoteConfigurationError,
                           match=r"Unable to load S3 credentials \(ProfileNotFound\)"):
            context.transport.prepare()


def test_profile_session_tokens_sign_requests(s3, monkeypatch, tmp_path):
    for name, value in (("HM_KEY", "key"), ("HM_SECRET", "secret"),
                        ("HM_TOKEN", "token")):
        monkeypatch.setenv(name, value)
    write_auth(monkeypatch, tmp_path, {
        "type": "s3", "buckets": [BUCKET],
        "access_key_id": {"env": "HM_KEY"},
        "secret_access_key": {"env": "HM_SECRET"},
        "session_token": {"env": "HM_TOKEN"}})
    with OperationContext(RemoteSpec.from_url(URL, "lab")) as context:
        signed = credentials(context)
        assert (signed.access_key, signed.secret_key, signed.token) == (
            "key", "secret", "token")


def test_listing_a_missing_bucket_reports_it_missing(s3):
    remote = RemoteSpec.from_url("s3://no-such-bucket/m87/")
    with OperationContext(remote) as context:
        with pytest.raises(RemoteObjectMissing, match="NoSuchBucket"):
            list(context.transport.iter_entries())


@pytest.mark.parametrize("key", ["odd/a//b.h5", "odd/./b.h5"])
def test_keys_that_normalize_to_another_path_are_refused(s3, key):
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
    with OperationContext(RemoteSpec.from_url(f"s3://{BUCKET}/odd/")) as context:
        with pytest.raises(DownloadError, match="unsafe key"):
            list(context.transport.iter_entries())


@pytest.mark.parametrize("reply", [
    {"ChecksumType": "COMPOSITE",
     "ChecksumSHA256": base64.b64encode(b"x" * 32).decode()},
    {"ChecksumType": "FULL_OBJECT", "ChecksumSHA256": "not base64!"},
    {},
])
def test_unusable_object_checksums_leave_the_entry_unchanged(s3, reply):
    with OperationContext(RemoteSpec.from_url(URL)) as context:
        transport = context.transport
        entries = sorted(transport.iter_entries(), key=lambda entry: entry.path)
        heads = []

        def head_object(**kwargs):
            heads.append(kwargs["Key"])
            return reply

        transport._client.head_object = head_object
        assert transport.complete_entries(entries) == entries
    assert heads == ["m87/a.h5"]
    assert entries[0].checksum is None


def test_an_object_deleted_after_listing_is_reported_missing(s3):
    with OperationContext(RemoteSpec.from_url(URL)) as context:
        entries = list(context.transport.iter_entries())
        s3.delete_object(Bucket=BUCKET, Key="m87/a.h5")
        with pytest.raises(RemoteObjectMissing, match="Not found"):
            context.transport.complete_entries(entries)


def test_fetch_writes_the_object_without_a_progress_callback(s3, tmp_path):
    with OperationContext(RemoteSpec.from_url(URL)) as context:
        assert context.on_bytes is None
        context.transport.fetch("b.h5", tmp_path / "b.h5")
    assert (tmp_path / "b.h5").read_bytes() == b"bravo"


def test_metadata_objects_are_read_within_their_limit(s3):
    with OperationContext(RemoteSpec.from_url(URL)) as context:
        assert context.transport.read_text("a.h5", 5) == "alpha"
        with pytest.raises(DownloadError, match="exceeds the configured limit"):
            context.transport.read_text("a.h5", 3)
        with pytest.raises(RemoteObjectMissing, match="NoSuchKey"):
            context.transport.read_text("absent.yml", 5)


@pytest.mark.parametrize("error, kind, message", [
    (client_error("SlowDown"), DownloadError, "failed: SlowDown"),
    (client_error("AccessDenied"), DownloadError, r"refused \(AccessDenied\)"),
    (EndpointConnectionError(endpoint_url="https://osn.test"), DownloadError,
     "S3 request failed: EndpointConnectionError"),
])
def test_request_failures_are_described_by_code_only(s3, tmp_path, error, kind,
                                                     message):
    with OperationContext(RemoteSpec.from_url(URL)) as context:
        context.transport.prepare()

        def fail(**kwargs):
            raise error

        context.transport._client.get_object = fail
        for call in (lambda: context.transport.read_text("a.h5", 5),
                     lambda: context.transport.fetch("a.h5", tmp_path / "a.h5")):
            with pytest.raises(kind, match=message) as raised:
                call()
            assert "body detail" not in str(raised.value)
            assert not isinstance(raised.value, RemoteObjectMissing)
