"""Typed local auth profiles whose secrets come from env, keyring or commands."""

import os
import sys
import types

import pytest
import yaml

from hallmark.transport import OperationContext, RemoteSpec
from hallmark.transport.auth import (
    HttpSettings,
    S3Settings,
    load_auth_file,
    resolve_settings,
    resolve_ssh_settings,
)
from hallmark.transport.base import CapabilityError, RemoteConfigurationError
from hallmark.transport.secrets import Secret, SecretRef


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch, tmp_path):
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def write_auth(monkeypatch, tmp_path, profiles, *, version=2, mode=0o600):
    path = tmp_path / "auth.yml"
    path.write_text(yaml.safe_dump({"version": version, "profiles": profiles}))
    os.chmod(path, mode)
    monkeypatch.setenv("HALLMARK_AUTH_FILE", str(path))
    return path


HTTP = {"type": "http", "hosts": ["Data.Example.org"], "scheme": "basic",
        "username": "alice", "password": {"env": "HM_TEST_PASSWORD"}}


def test_secrets_are_read_on_use_and_never_shown(monkeypatch):
    ref = SecretRef.parse({"env": "HM_TEST_PASSWORD"}, profile="lab", field="password")
    with pytest.raises(RemoteConfigurationError, match="HM_TEST_PASSWORD") as error:
        ref.resolve()
    assert "lab" in str(error.value)
    monkeypatch.setenv("HM_TEST_PASSWORD", "hunter2")
    secret = ref.resolve()
    assert secret.reveal() == "hunter2"
    assert "hunter2" not in repr(secret) + str(secret) + repr([secret])
    assert repr(Secret("x")) == "<redacted>"


@pytest.mark.parametrize("value", [
    "hunter2", {"env": ""}, {"env": "A", "command": ["x"]}, {"file": "x"},
    {"command": "echo hunter2"}, {"keyring": {"service": "s"}},
])
def test_literal_or_malformed_secrets_are_refused_without_echo(value):
    with pytest.raises(RemoteConfigurationError) as error:
        SecretRef.parse(value, profile="lab", field="password")
    assert "hunter2" not in str(error.value)
    assert "'lab'" in str(error.value)


def test_command_secrets_print_one_line_and_hide_stderr(tmp_path):
    def ref(code):
        return SecretRef.parse({"command": [sys.executable, "-c", code]},
                               profile="lab", field="token")

    assert ref("print('s3cr3t')").resolve().reveal() == "s3cr3t"
    with pytest.raises(RemoteConfigurationError, match="failed") as error:
        ref("import sys; sys.stderr.write('leaked'); sys.exit(1)").resolve()
    assert "leaked" not in str(error.value)
    with pytest.raises(RemoteConfigurationError, match="one line"):
        ref("print('a'); print('b')").resolve()
    with pytest.raises(RemoteConfigurationError, match="not found"):
        SecretRef.parse({"command": ["no-such-hallmark-helper"]},
                        profile="lab", field="token").resolve()


def test_keyring_secrets_use_the_optional_package(monkeypatch):
    ref = SecretRef.parse({"keyring": {"service": "hallmark", "username": "zenodo"}},
                          profile="zenodo", field="token")
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(CapabilityError, match=r"hallmark\[keyring\]"):
        ref.resolve()
    store = {("hallmark", "zenodo"): "tok"}
    fake = types.SimpleNamespace(get_password=lambda *key: store.get(key))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert ref.resolve().reveal() == "tok"
    store.clear()
    with pytest.raises(RemoteConfigurationError, match="no entry"):
        ref.resolve()


def test_http_profiles_bind_hosts_and_require_https(monkeypatch, tmp_path):
    write_auth(monkeypatch, tmp_path, {"lab": HTTP})
    settings = resolve_settings(
        RemoteSpec.from_url("https://data.example.org/eht/", "lab"), "http")
    assert isinstance(settings, HttpSettings)
    assert (settings.scheme, settings.username, settings.hosts) == (
        "basic", "alice", ("data.example.org",))
    assert settings.password.source == "env"
    with pytest.raises(RemoteConfigurationError, match="not bound"):
        resolve_settings(RemoteSpec.from_url("https://other.org/", "lab"), "http")
    with pytest.raises(RemoteConfigurationError, match="https"):
        resolve_settings(RemoteSpec.from_url("http://data.example.org/", "lab"),
                         "http")
    assert resolve_settings(RemoteSpec.from_url("https://data.example.org/"),
                            "http") is None


@pytest.mark.parametrize("profile, message", [
    ({**HTTP, "scheme": "digest"}, "scheme"),
    ({**HTTP, "token": {"env": "X"}}, "exactly"),
    ({**HTTP, "password": "hunter2"}, "where its secret"),
    ({key: value for key, value in HTTP.items() if key != "type"}, "type"),
])
def test_invalid_http_profiles_fail_before_any_request(monkeypatch, tmp_path,
                                                       profile, message):
    write_auth(monkeypatch, tmp_path, {"lab": profile})
    with pytest.raises(RemoteConfigurationError, match=message) as error:
        resolve_settings(RemoteSpec.from_url("https://data.example.org/", "lab"),
                         "http")
    assert "hunter2" not in str(error.value)


def test_profiles_are_used_only_for_their_own_type(monkeypatch, tmp_path):
    write_auth(monkeypatch, tmp_path, {
        "lab": HTTP, "hpc": {"type": "ssh", "hosts": ["login"], "user": "bob"}})
    assert resolve_ssh_settings(RemoteSpec.from_url("ssh://login/data", "hpc")).user \
        == "bob"
    with pytest.raises(RemoteConfigurationError, match="is for http, not ssh"):
        resolve_ssh_settings(RemoteSpec.from_url("ssh://login/data", "lab"))
    with pytest.raises(RemoteConfigurationError, match="is for ssh, not http"):
        resolve_settings(RemoteSpec.from_url("https://login/", "hpc"), "http")
    with pytest.raises(RemoteConfigurationError, match="does not use auth"):
        resolve_settings(RemoteSpec.from_url("https://login/", "hpc"), None)


def test_version_two_files_must_be_private(monkeypatch, tmp_path):
    path = write_auth(monkeypatch, tmp_path, {"lab": HTTP}, mode=0o664)
    with pytest.raises(RemoteConfigurationError, match="chmod go-w"):
        load_auth_file()
    os.chmod(path, 0o644)
    assert load_auth_file()["version"] == 2
    monkeypatch.setattr("hallmark.transport.auth.os.getuid", lambda: -1)
    with pytest.raises(RemoteConfigurationError, match="belong to you"):
        load_auth_file()


def test_version_one_files_keep_working(monkeypatch, tmp_path):
    write_auth(monkeypatch, tmp_path, {"lab": {"hosts": ["campus"], "user": "a"}},
               version=1, mode=0o664)
    assert resolve_ssh_settings(RemoteSpec.from_url("ssh://campus/x", "lab")).user \
        == "a"
    write_auth(monkeypatch, tmp_path, {"lab": {"type": "ssh", "hosts": ["campus"]}},
               version=1)
    with pytest.raises(RemoteConfigurationError, match="version: 2"):
        load_auth_file()


def s3_remote(auth="osn", endpoint="https://sdsc.osn.xsede.org"):
    options = {"endpoint_url": endpoint} if endpoint else {}
    return RemoteSpec("s3://ehtc-sims/m87/", "s3", "ehtc-sims", "m87/",
                      auth=auth, backend="s3", backend_options=options)


def test_s3_profiles_bind_buckets_and_endpoints(monkeypatch, tmp_path):
    write_auth(monkeypatch, tmp_path, {
        "osn": {"type": "s3", "buckets": ["ehtc-sims"],
                "endpoints": ["https://sdsc.osn.xsede.org/"],
                "access_key_id": {"env": "KEY"},
                "secret_access_key": {"command": ["pass", "show", "osn"]}},
        "aws": {"type": "s3", "buckets": ["ehtc-sims"], "aws_profile": "research"},
    })
    settings = resolve_settings(s3_remote(), "s3")
    assert isinstance(settings, S3Settings)
    assert settings.endpoints == ("https://sdsc.osn.xsede.org",)
    assert settings.secret_access_key.target == ("pass", "show", "osn")
    with pytest.raises(RemoteConfigurationError, match="endpoint"):
        resolve_settings(s3_remote(endpoint="https://attacker.test"), "s3")
    with pytest.raises(RemoteConfigurationError, match="endpoint"):
        resolve_settings(s3_remote(endpoint=None), "s3")
    assert resolve_settings(s3_remote("aws", endpoint=None), "s3").aws_profile == \
        "research"
    with pytest.raises(RemoteConfigurationError, match="endpoint"):
        resolve_settings(s3_remote("aws"), "s3")


def test_operation_context_reads_profiles_for_the_backend_type(monkeypatch,
                                                               tmp_path):
    from hallmark.remote import backends
    from hallmark.remote.backends import DataBackend

    class Authenticated(DataBackend):
        auth_type = "http"

    monkeypatch.setattr(backends, "_registered", {**backends._registered,
                                                  "authenticated": Authenticated})
    write_auth(monkeypatch, tmp_path, {"lab": HTTP})
    remote = RemoteSpec.from_url("https://data.example.org/", "lab",
                                 backend="authenticated")
    with OperationContext(remote) as context:
        assert isinstance(context.auth, HttpSettings)
    with OperationContext(RemoteSpec.from_url("https://data.example.org/",
                                              backend="authenticated")) as context:
        assert context.auth is None
