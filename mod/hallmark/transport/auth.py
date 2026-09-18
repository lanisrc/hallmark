"""Strict local SSH settings; repository configuration stores references only."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

from ..helper_functions import load_yaml_file
from .base import (
    RemoteConfigurationError,
    profile_name,
    reject_controls,
    ssh_host,
    ssh_user,
)


@dataclass(frozen=True)
class SSHSettings:
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    host_key_policy: str = "strict"
    connect_timeout: int = 10
    transfer_timeout: int = 3600
    shutdown_timeout: int = 2
    max_sessions: int = 4


def _settings(values):
    allowed = {item.name for item in fields(SSHSettings)}
    if not isinstance(values, dict) or set(values) - allowed:
        raise RemoteConfigurationError("Unsupported local SSH settings fields")
    values = dict(values)
    if "user" in values:
        ssh_user(values["user"])
    for key in (
        "port",
        "connect_timeout",
        "transfer_timeout",
        "shutdown_timeout",
        "max_sessions",
    ):
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RemoteConfigurationError(f"SSH {key} must be a positive integer")
        if key == "port" and value > 65535:
            raise RemoteConfigurationError("SSH port must be at most 65535")
    if values.get("host_key_policy", "strict") not in {"strict", "accept-new"}:
        raise RemoteConfigurationError("host_key_policy must be strict or accept-new")
    if "identity_file" in values:
        value = values["identity_file"]
        if not isinstance(value, str) or not value:
            raise RemoteConfigurationError("identity_file must be a local path")
        reject_controls(value, "identity_file")
        # OpenSSH expands % tokens and environment substitutions inside paths.
        if "%" in value or "$" in value:
            raise RemoteConfigurationError("identity_file cannot contain expansions")
        values["identity_file"] = str(Path(value).expanduser().absolute())
    return values


def resolve_settings(remote):
    """Resolve settings once, before any worker or connection starts."""
    settings = SSHSettings()
    if remote.scheme not in {"ssh", "sftp"}:
        return settings
    configured = os.environ.get("HALLMARK_AUTH_FILE")
    path = (
        Path(configured).expanduser()
        if configured
        else Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
        / "hallmark"
        / "auth.yml"
    )
    if not path.exists() and not configured and remote.auth is None:
        return replace(settings, user=remote.user, port=remote.port)
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
        or document["version"] != 1
    ):
        raise RemoteConfigurationError("Auth file must use version: 1")
    defaults = _settings(document.get("defaults", {}))
    # Endpoint/identity defaults must always be host-bound through a profile.
    if set(defaults) & {"user", "port", "identity_file"}:
        raise RemoteConfigurationError("Global SSH defaults cannot select an identity")
    settings = replace(settings, **defaults)
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict):
        raise RemoteConfigurationError("Auth profiles must be a mapping")
    validated = {}
    for name, profile in profiles.items():
        profile_name(name)
        if not isinstance(profile, dict):
            raise RemoteConfigurationError("Each auth profile must be a mapping")
        hosts = profile.get("hosts")
        if (
            not isinstance(hosts, list)
            or not hosts
            or any(not isinstance(host, str) for host in hosts)
        ):
            raise RemoteConfigurationError("Each auth profile requires hosts")
        hosts = [ssh_host(host).lower() for host in hosts]
        values = _settings({k: v for k, v in profile.items() if k != "hosts"})
        validated[name] = (hosts, values)
    if remote.auth is not None:
        if remote.auth not in validated:
            raise RemoteConfigurationError(f"Unresolved auth profile {remote.auth!r}")
        hosts, values = validated[remote.auth]
        if remote.host.lower() not in hosts:
            raise RemoteConfigurationError(
                f"Auth profile {remote.auth!r} is not bound to {remote.host}"
            )
        settings = replace(settings, **values)
    # Profile user/port are defaults; hosts are endpoint constraints.
    return replace(
        settings, user=remote.user or settings.user, port=remote.port or settings.port
    )
