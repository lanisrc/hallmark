"""Resolve local secrets without exposing their values."""

import sys
import types

import pytest

from hallmark.transport.base import CapabilityError, RemoteConfigurationError
from hallmark.transport.secrets import Secret, SecretRef


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
