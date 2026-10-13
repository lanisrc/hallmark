"""
List and transfer objects in S3-compatible storage with boto3.

AWS S3 and compatible stores (Open Storage Network, MinIO, Wasabi, Ceph)
are reached through ``s3://BUCKET/PREFIX/`` remotes, with ``endpoint_url``
in the backend options for stores other than AWS. boto3 is an optional
dependency: ``pip install hallmark[s3]``.
"""

from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock

from ..utils import CHECKSUM_ALGORITHMS_BY_STRENGTH, names_repository_metadata
from .base import (
    CapabilityError,
    DataBackend,
    DownloadError,
    RemoteConfigurationError,
    RemoteEntry,
    RemoteObjectMissing,
    decode_metadata,
    validate_remote_path,
)

OPTIONS = {"endpoint_url", "region", "addressing_style", "anonymous", "etag_md5",
           "page_size"}
# Object checksums worth a HEAD request; CRC values are not content digests.
_HEAD_CHECKSUMS = {"SHA256": "sha256", "SHA1": "sha1"}
# Concurrent HEAD requests; botocore's default connection pool holds ten.
_HEAD_WORKERS = 10
_MISSING = {"NoSuchKey", "404", "NotFound", "NoSuchBucket"}
_DENIED = {"AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch",
           "ExpiredToken", "InvalidToken"}


def _options(remote):
    """Validate the S3 backend options saved with a remote."""
    options = dict(remote.backend_options)
    unknown = set(options) - OPTIONS
    if unknown:
        raise RemoteConfigurationError(
            "Unsupported S3 backend options: " + ", ".join(sorted(unknown)))
    endpoint = options.get("endpoint_url")
    if endpoint is not None and (not isinstance(endpoint, str)
                                 or not endpoint.startswith(("https://", "http://"))):
        raise RemoteConfigurationError("endpoint_url must be an http(s) URL")
    if options.get("addressing_style", "auto") not in {"auto", "virtual", "path"}:
        raise RemoteConfigurationError("addressing_style must be auto, virtual or path")
    if not isinstance(options.get("anonymous", False), bool):
        raise RemoteConfigurationError("anonymous must be true or false")
    if options.get("etag_md5", "never") not in {"never", "auto"}:
        raise RemoteConfigurationError("etag_md5 must be never or auto")
    page_size = options.get("page_size", 1000)
    if (isinstance(page_size, bool) or not isinstance(page_size, int)
            or not 1 <= page_size <= 1000):
        raise RemoteConfigurationError("page_size must be between 1 and 1000")
    region = options.get("region")
    if region is not None and not isinstance(region, str):
        raise RemoteConfigurationError("region must be a name")
    return options


def _hex_digest(value):
    """Convert an S3 base64 checksum to hexadecimal, or None."""
    try:
        return base64.b64decode(value, validate=True).hex()
    except (binascii.Error, TypeError, ValueError):
        return None


class S3Backend(DataBackend):
    """
    Discover and transfer objects beneath an S3 bucket prefix.

    One thread-safe boto3 client serves every worker of an operation.
    Credentials come from the remote's ``s3`` auth profile, otherwise from
    boto3's usual sources (environment, AWS configuration, instance roles);
    when none exists, requests are unsigned, as public buckets expect.
    """

    auth_type = "s3"

    def __init__(self, context):
        super().__init__(context)
        if context.remote.scheme != "s3":
            raise RemoteConfigurationError("S3 backends require an s3://BUCKET/ URL")
        self.options = _options(context.remote)
        self.bucket = context.remote.host
        root = context.remote.root.strip("/")
        self.prefix = root + "/" if root else ""
        self._client = None
        self._lock = Lock()
        # Keys whose listing names a SHA checksum only a HEAD request returns.
        self._checksum_keys = {}

    def prepare(self):
        """Create the client, reading profile secrets once."""
        with self._lock:
            if self._client is not None:
                return
            try:
                import boto3
                from botocore import UNSIGNED
                from botocore.config import Config
            except ImportError:
                raise CapabilityError(
                    "S3 remotes need boto3; install hallmark[s3]") from None
            auth = getattr(self.context, "auth", None)
            session_options = {}
            if auth is not None and auth.aws_profile:
                session_options["profile_name"] = auth.aws_profile
            if auth is not None and auth.access_key_id is not None:
                session_options["aws_access_key_id"] = \
                    auth.access_key_id.resolve().reveal()
                session_options["aws_secret_access_key"] = \
                    auth.secret_access_key.resolve().reveal()
                if auth.session_token is not None:
                    session_options["aws_session_token"] = \
                        auth.session_token.resolve().reveal()
            try:
                session = boto3.Session(**session_options)
            except Exception as exc:
                raise RemoteConfigurationError(
                    f"Unable to load S3 credentials ({type(exc).__name__})") from None
            unsigned = self.options.get("anonymous", False) or (
                auth is None and session.get_credentials() is None)
            config = Config(
                connect_timeout=10, read_timeout=30,
                retries={"max_attempts": 5, "mode": "standard"},
                s3={"addressing_style": self.options.get("addressing_style", "auto")},
                **({"signature_version": UNSIGNED} if unsigned else {}))
            self._client = session.client(
                "s3", endpoint_url=self.options.get("endpoint_url"),
                region_name=self.options.get("region"), config=config)

    def _error(self, exc, key=None):
        """Describe an S3 failure by its error code, never its response body."""
        from botocore.exceptions import ClientError

        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            where = f"s3://{self.bucket}"
            if code in _MISSING:
                return RemoteObjectMissing(f"Not found in {where}: {code}")
            if code in _DENIED:
                hint = (f"check auth profile {self.context.remote.auth!r}"
                        if self.context.remote.auth else
                        "name an s3 auth profile with --auth")
                return DownloadError(f"Access to {where} was refused ({code}); {hint}")
            return DownloadError(f"S3 request to {where} failed: {code}")
        return DownloadError(f"S3 request failed: {type(exc).__name__}")

    def _key(self, relative_path):
        return self.prefix + validate_remote_path(relative_path).as_posix()

    def iter_entries(self, on_directory=None):
        """List objects beneath the prefix, page by page, without reading them."""
        from botocore.exceptions import BotoCoreError, ClientError

        self.prepare()
        folders = set()
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.prefix,
                PaginationConfig={"PageSize": self.options.get("page_size", 1000)})
            for page in pages:
                for item in page.get("Contents", ()):
                    self.context.check_cancelled()
                    entry = self._entry(item)
                    if entry is None:
                        continue
                    folder = entry.path.rpartition("/")[0]
                    if on_directory is not None and folder not in folders:
                        folders.add(folder)
                        on_directory(folder + "/" if folder else "")
                    yield entry
        except (BotoCoreError, ClientError) as exc:
            raise self._error(exc) from None

    def _entry(self, item):
        key = item["Key"]
        relative = key[len(self.prefix):]
        # Keys ending in "/" are folder placeholders created by consoles.
        if not relative or relative.endswith("/"):
            return None
        if names_repository_metadata(relative):
            return None
        try:
            path = validate_remote_path(relative).as_posix()
        except ValueError:
            raise DownloadError("S3 listing contains an unsafe key") from None
        if path != relative:
            raise DownloadError("S3 listing contains an unsafe key")
        listed = {name.upper() for name in item.get("ChecksumAlgorithm", ())}
        wanted = [name for name in _HEAD_CHECKSUMS if name in listed]
        if wanted:
            self._checksum_keys[path] = (key, wanted)
        algorithm, digest = self._etag_checksum(item)
        return RemoteEntry(
            path=path, size=item.get("Size"),
            mtime=int(item["LastModified"].timestamp()) if item.get("LastModified")
            else None,
            checksum_algorithm=algorithm, checksum=digest,
            checksum_source="server" if digest else None)

    def complete_entries(self, entries):
        """Ask for the SHA checksums of selected objects, several at a time."""
        from botocore.exceptions import BotoCoreError, ClientError

        strength = {name: index for index, name in
                    enumerate(CHECKSUM_ALGORITHMS_BY_STRENGTH)}
        needed = []
        for entry in entries:
            key, wanted = self._checksum_keys.get(entry.path, (None, ()))
            best = min((strength[_HEAD_CHECKSUMS[name]] for name in wanted),
                       default=None)
            # A published checksum at least as strong needs no request.
            if best is None or (entry.checksum and best >= strength.get(
                    entry.checksum_algorithm, len(strength))):
                continue
            needed.append((entry, key, wanted))
        if not needed:
            return entries

        def head(job):
            entry, key, wanted = job
            self.context.check_cancelled()
            response = self._client.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
            # Multipart uploads report a checksum of part checksums instead.
            if response.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
                return entry
            for name in wanted:
                digest = _hex_digest(response.get(f"Checksum{name}"))
                if digest:
                    return replace(entry, checksum_algorithm=_HEAD_CHECKSUMS[name],
                                   checksum=digest, checksum_source="server")
            return entry

        try:
            workers = min(len(needed), _HEAD_WORKERS)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                done = {result.path: result for result in pool.map(head, needed)}
        except (BotoCoreError, ClientError) as exc:
            raise self._error(exc) from None
        return [done.get(entry.path, entry) for entry in entries]

    def _etag_checksum(self, item):
        """Return a listed object's ETag as MD5 where the remote allows it."""
        etag = str(item.get("ETag", "")).strip('"').lower()
        # A single-part upload's ETag is its MD5 unless the object is encrypted
        # with KMS or customer keys, so it is used only when the remote says so.
        if (self.options.get("etag_md5", "never") == "auto" and len(etag) == 32
                and all(char in "0123456789abcdef" for char in etag)):
            return "md5", etag
        return None, None

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        """Stream one object to the destination, checking for cancellation."""
        from botocore.exceptions import BotoCoreError, ClientError

        self.prepare()
        try:
            response = self._client.get_object(
                Bucket=self.bucket, Key=self._key(relative_path))
            body = response["Body"]
            try:
                with destination.open("wb") as handle:
                    for chunk in body.iter_chunks(chunk_size=max(chunk_size, 65536)):
                        self.context.check_cancelled()
                        handle.write(chunk)
                        if self.context.on_bytes is not None:
                            self.context.on_bytes(len(chunk))
            finally:
                body.close()
        except (BotoCoreError, ClientError) as exc:
            raise self._error(exc) from None

    def read_text(self, relative_path, limit):
        """Read a small metadata object, such as a checksum file."""
        from botocore.exceptions import BotoCoreError, ClientError

        self.prepare()
        try:
            response = self._client.get_object(
                Bucket=self.bucket, Key=self._key(relative_path),
                Range=f"bytes=0-{limit}")
            with response["Body"] as body:
                content = body.read(limit + 1)
        except ClientError as exc:
            # A ranged read of an empty object has no bytes to return.
            if exc.response.get("Error", {}).get("Code") == "InvalidRange":
                return ""
            raise self._error(exc) from None
        except BotoCoreError as exc:
            raise self._error(exc) from None
        if len(content) > limit:
            raise DownloadError("Remote text exceeds the configured limit")
        return decode_metadata(content)

    def close(self):
        client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            client.close()
