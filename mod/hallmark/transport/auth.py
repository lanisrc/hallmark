"""Read local SSH settings for profile names stored in repository configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

from ..utils import load_yaml_file
from .secrets import SecretRef
from .base import (
    RemoteConfigurationError,
    validate_profile_name,
    reject_control_characters,
    validate_ssh_host,
    validate_ssh_user,
)


@dataclass(frozen=True)
class SSHSettings:
    """
    Local SSH settings shared by the connections in one operation.

    Timeouts are in seconds. ``transfer_timeout`` limits the total time
    for each file, and ``max_sessions`` limits concurrent SFTP sessions.
    Keepalives probe every ``server_alive_interval`` seconds and drop the
    connection after ``server_alive_count_max`` unanswered probes. Unset
    user, port, identity, and compression values fall back to SSH
    configuration.
    """
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    host_key_policy: str = "strict"
    connect_timeout: int = 10
    transfer_timeout: int = 3600
    shutdown_timeout: int = 2
    max_sessions: int = 4
    server_alive_interval: int = 15
    server_alive_count_max: int = 2
    compression: bool | None = None


# sshd's default MaxSessions; more sessions on one master connection fail.
MAX_SSH_SESSIONS = 10
AUTH_FILE_VERSIONS = (1, 2)
AUTH_TYPES = ("ssh", "http", "s3")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_ssh_settings(values):
    """Validate local SSH options and normalize identity-file paths."""
    allowed = {item.name for item in fields(SSHSettings)}
    if not isinstance(values, dict) or set(values) - allowed:
        raise RemoteConfigurationError("Unsupported local SSH settings fields")
    values = dict(values)
    if "user" in values:
        validate_ssh_user(values["user"])
    for key in (
        "port",
        "connect_timeout",
        "transfer_timeout",
        "shutdown_timeout",
        "max_sessions",
        "server_alive_interval",
        "server_alive_count_max",
    ):
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RemoteConfigurationError(f"SSH {key} must be a positive integer")
        if key == "port" and value > 65535:
            raise RemoteConfigurationError("SSH port must be at most 65535")
        if key == "max_sessions" and value > MAX_SSH_SESSIONS:
            raise RemoteConfigurationError(
                f"SSH max_sessions must be at most {MAX_SSH_SESSIONS}")
    if "compression" in values and not isinstance(values["compression"], bool):
        raise RemoteConfigurationError("SSH compression must be true or false")
    if values.get("host_key_policy", "strict") not in {"strict", "accept-new"}:
        raise RemoteConfigurationError("host_key_policy must be strict or accept-new")
    if "identity_file" in values:
        value = values["identity_file"]
        if not isinstance(value, str) or not value:
            raise RemoteConfigurationError("identity_file must be a local path")
        reject_control_characters(value, "identity_file")
        # OpenSSH expands % tokens and environment substitutions inside paths.
        if "%" in value or "$" in value:
            raise RemoteConfigurationError("identity_file cannot contain expansions")
        values["identity_file"] = str(Path(value).expanduser().absolute())
    return values


def auth_file_path():
    """
    Return the local auth file path and whether it was chosen explicitly.

    ``$HALLMARK_AUTH_FILE`` wins, then ``$XDG_CONFIG_HOME/hallmark/auth.yml``,
    then ``~/.config/hallmark/auth.yml``.
    """
    configured = os.environ.get("HALLMARK_AUTH_FILE")
    if configured:
        return Path(configured).expanduser(), True
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "hallmark" / "auth.yml", False


def _check_owner_only(path: Path) -> None:
    """Refuse a file that another account could change, as OpenSSH does."""
    if os.name != "posix":
        return
    details = path.stat()
    if details.st_uid != os.getuid() or details.st_mode & 0o022:
        raise RemoteConfigurationError(
            "The Hallmark auth file must belong to you and be writable only by "
            "you; run chmod go-w on it")


def load_auth_file(required: bool = False):
    """
    Read and validate the structure of the local auth file.

    Version 1 files hold SSH profiles only. Version 2 profiles declare a
    ``type`` of ``ssh``, ``http`` or ``s3``; because they may run secret
    commands, version 2 files must be writable only by their owner.

    Args:
        required (bool): Fail if the file does not exist.

    Returns:
        dict | None: The document, or None when no file exists and none was
        required or configured.

    Raises:
        RemoteConfigurationError: If the file is unreadable or malformed.
    """
    path, configured = auth_file_path()
    if not path.exists() and not configured and not required:
        return None
    try:
        document = load_yaml_file(path)
    except Exception:
        raise RemoteConfigurationError(
            "Unable to read local Hallmark auth file"
        ) from None
    if (
        not isinstance(document, dict)
        or set(document) - {"version", "profiles", "defaults"}
        or type(document.get("version")) is not int
        or document["version"] not in AUTH_FILE_VERSIONS
    ):
        raise RemoteConfigurationError("Auth file must use version: 1 or version: 2")
    if document["version"] >= 2:
        _check_owner_only(path)
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict):
        raise RemoteConfigurationError("Auth profiles must be a mapping")
    for name, profile in profiles.items():
        validate_profile_name(name)
        if not isinstance(profile, dict):
            raise RemoteConfigurationError("Each auth profile must be a mapping")
        kind = profile.get("type", "ssh" if document["version"] == 1 else None)
        if document["version"] == 1 and "type" in profile:
            raise RemoteConfigurationError(
                "Profile types need version: 2 in the auth file")
        if kind not in AUTH_TYPES:
            raise RemoteConfigurationError(
                f"Auth profile {name!r} needs a type: " + ", ".join(AUTH_TYPES))
    return document


def _profile(document, name, kind):
    """Return a named profile's fields, checking its type."""
    profiles = (document or {}).get("profiles", {})
    if name not in profiles:
        raise RemoteConfigurationError(f"Unresolved auth profile {name!r}")
    profile = dict(profiles[name])
    found = profile.pop("type", "ssh")
    if found != kind:
        raise RemoteConfigurationError(
            f"Auth profile {name!r} is for {found}, not {kind} access")
    return profile


def _bound_hosts(profile):
    hosts = profile.pop("hosts", None)
    if (not isinstance(hosts, list) or not hosts
            or any(not isinstance(host, str) for host in hosts)):
        raise RemoteConfigurationError("Each auth profile requires hosts")
    return hosts


def resolve_ssh_settings(remote):
    """
    Resolve local SSH settings before opening a connection.

    URL user and port values override a host-bound profile, which overrides
    global timeout and concurrency defaults. Remaining values use SSH config.

    Args:
        remote (RemoteSpec): Source URL and optional local profile name.

    Returns:
        SSHSettings: Validated settings, or defaults for an HTTP source.

    Raises:
        RemoteConfigurationError: If the auth file, settings, or host binding
            is invalid, or a referenced profile is missing.
    """
    settings = SSHSettings()
    if remote.scheme not in {"ssh", "sftp"}:
        return settings
    document = load_auth_file(required=remote.auth is not None)
    if document is None:
        return replace(settings, user=remote.user, port=remote.port)
    defaults = _validate_ssh_settings(document.get("defaults", {}))
    # User, port, and identity settings require a profile bound to specific hosts.
    if set(defaults) & {"user", "port", "identity_file"}:
        raise RemoteConfigurationError("Global SSH defaults cannot select an identity")
    settings = replace(settings, **defaults)
    validated = {}
    for name, profile in document.get("profiles", {}).items():
        kind = profile.get("type", "ssh")
        if kind != "ssh":
            continue
        values = {k: v for k, v in profile.items() if k != "type"}
        hosts = [validate_ssh_host(host).lower()
                 for host in _bound_hosts(values)]
        validated[name] = (hosts, _validate_ssh_settings(values))
    if remote.auth is not None:
        if remote.auth not in validated:
            # Report a missing profile, or one of another type.
            _profile(document, remote.auth, "ssh")
        hosts, values = validated[remote.auth]
        if remote.host.lower() not in hosts:
            raise RemoteConfigurationError(
                f"Auth profile {remote.auth!r} is not bound to {remote.host}"
            )
        settings = replace(settings, **values)
    # URL user and port override profile defaults after checking the host binding.
    return replace(
        settings, user=remote.user or settings.user, port=remote.port or settings.port
    )


@dataclass(frozen=True)
class HttpSettings:
    """
    Credentials for an HTTP(S) profile, resolved when a request needs them.

    Attributes:
        profile (str): Profile name, for messages.
        hosts (tuple[str, ...]): Lowercase host names the credentials may reach.
        scheme (str): ``basic`` or ``bearer``.
        username (str | SecretRef, optional): Basic user name.
        password (SecretRef, optional): Basic password.
        token (SecretRef, optional): Bearer token.
    """

    profile: str
    hosts: tuple
    scheme: str
    username: object = None
    password: object = None
    token: object = None


def _http_settings(remote, document):
    name = remote.auth
    profile = _profile(document, name, "http")
    hosts = tuple(host.lower() for host in _bound_hosts(profile))
    if remote.host.lower() not in hosts:
        raise RemoteConfigurationError(
            f"Auth profile {name!r} is not bound to {remote.host}")
    scheme = profile.pop("scheme", None)
    fields_by_scheme = {"basic": {"username", "password"}, "bearer": {"token"}}
    if scheme not in fields_by_scheme:
        raise RemoteConfigurationError(
            f"Auth profile {name!r} needs scheme: basic or scheme: bearer")
    if set(profile) != fields_by_scheme[scheme]:
        raise RemoteConfigurationError(
            f"Auth profile {name!r} with scheme {scheme} needs exactly: "
            + ", ".join(sorted(fields_by_scheme[scheme])))
    # Credentials travel only over HTTPS, except to this computer.
    if remote.scheme != "https" and remote.host.lower() not in LOOPBACK_HOSTS:
        raise RemoteConfigurationError(
            f"Auth profile {name!r} needs an https:// URL; "
            "credentials are not sent over plain HTTP")
    values = {"profile": name, "hosts": hosts, "scheme": scheme}
    for field in fields_by_scheme[scheme]:
        value = profile[field]
        if field == "username" and isinstance(value, str) and value:
            reject_control_characters(value, "username")
            values[field] = value
        else:
            values[field] = SecretRef.parse(value, profile=name, field=field)
    return HttpSettings(**values)


@dataclass(frozen=True)
class S3Settings:
    """
    Credentials for an S3-compatible profile, resolved when a client starts.

    Attributes:
        profile (str): Profile name, for messages.
        buckets (tuple[str, ...]): Buckets the credentials may reach.
        endpoints (tuple[str, ...]): Endpoint URLs the credentials may reach;
            empty for AWS itself.
        aws_profile (str, optional): Named profile in the AWS configuration.
        access_key_id (SecretRef, optional): Access key reference.
        secret_access_key (SecretRef, optional): Secret key reference.
        session_token (SecretRef, optional): Session token reference.
    """

    profile: str
    buckets: tuple
    endpoints: tuple = ()
    aws_profile: str | None = None
    access_key_id: object = None
    secret_access_key: object = None
    session_token: object = None


def _string_list(profile, key, name, required):
    values = profile.pop(key, None)
    if values is None and not required:
        return ()
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value for value in values)):
        raise RemoteConfigurationError(
            f"Auth profile {name!r} needs {key} as a list of names")
    return tuple(values)


def _s3_settings(remote, document):
    name = remote.auth
    profile = _profile(document, name, "s3")
    buckets = _string_list(profile, "buckets", name, True)
    endpoints = tuple(endpoint.rstrip("/") for endpoint in
                      _string_list(profile, "endpoints", name, False))
    if remote.host not in buckets:
        raise RemoteConfigurationError(
            f"Auth profile {name!r} is not bound to bucket {remote.host}")
    endpoint = remote.backend_options.get("endpoint_url")
    endpoint = endpoint.rstrip("/") if isinstance(endpoint, str) else None
    # A shared catalogue must not redirect your keys to another server.
    if (endpoint or None) not in (endpoints or (None,)):
        raise RemoteConfigurationError(
            f"Auth profile {name!r} is not bound to this S3 endpoint")
    values = {"profile": name, "buckets": buckets, "endpoints": endpoints}
    aws_profile = profile.pop("aws_profile", None)
    if aws_profile is not None:
        if not isinstance(aws_profile, str) or not aws_profile:
            raise RemoteConfigurationError(
                f"Auth profile {name!r} aws_profile must be a name")
        values["aws_profile"] = aws_profile
    for field in ("access_key_id", "secret_access_key", "session_token"):
        if field in profile:
            values[field] = SecretRef.parse(profile.pop(field), profile=name,
                                            field=field)
    if profile:
        raise RemoteConfigurationError(
            f"Unsupported fields in auth profile {name!r}: "
            + ", ".join(sorted(map(str, profile))))
    if aws_profile is not None and "access_key_id" in values:
        raise RemoteConfigurationError(
            f"Auth profile {name!r} uses either aws_profile or keys, not both")
    if ("access_key_id" in values) != ("secret_access_key" in values):
        raise RemoteConfigurationError(
            f"Auth profile {name!r} needs both access_key_id and secret_access_key")
    return S3Settings(**values)


def resolve_settings(remote, auth_type):
    """
    Resolve the local credentials a backend uses for a remote.

    No secret is read here; HTTP and S3 settings hold references that are
    resolved when a request needs them.

    Args:
        remote (RemoteSpec): Remote and its optional profile name.
        auth_type (str, optional): ``http`` or ``s3``, as declared by the
            backend; SSH settings are resolved separately.

    Returns:
        HttpSettings | S3Settings | None: Settings, or None without a profile.

    Raises:
        RemoteConfigurationError: If the profile is missing, of another type,
            not bound to the remote, or unsupported by the backend.
    """
    if remote.auth is None or auth_type == "ssh":
        return None
    if auth_type not in {"http", "s3"}:
        raise RemoteConfigurationError(
            f"The {remote.backend} backend does not use auth profiles")
    document = load_auth_file(required=True)
    if auth_type == "http":
        return _http_settings(remote, document)
    return _s3_settings(remote, document)
