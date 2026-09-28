"""Public backend registration without public services."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hallmark import DataBackend, HttpBackend, SshBackend, CyVerseBackend
from hallmark.remote import backends
from hallmark.remote.discovery import discover_remote_files
from hallmark.transport import OperationContext
from hallmark.transport.base import (
    CapabilityError, RemoteConfigurationError, RemoteEntry, RemoteSpec,
    readonly_backend_options, copy_backend_options,
)


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(backends, "_registered", {
        "http": HttpBackend, "ssh": SshBackend, "cyverse": CyVerseBackend})
    monkeypatch.setattr(backends, "_loaded", {})
    monkeypatch.setattr(backends.metadata, "entry_points", lambda: {})


@pytest.mark.parametrize("url, expected", [
    ("https://lab.test/data/", HttpBackend),
    ("http://lab.test/data/", HttpBackend),
    ("ssh://lab/srv/data/", SshBackend),
    ("sftp://lab/srv/data/", SshBackend),
    ("https://data.cyverse.org/dav-anon/data/", CyVerseBackend),
])
def test_builtin_detection(url, expected, monkeypatch, tmp_path):
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with OperationContext(RemoteSpec.from_url(url)) as context:
        assert type(context.transport) is expected


def test_explicit_selection():
    with OperationContext(RemoteSpec.from_url(
            "https://data.cyverse.org/data/", backend="http")) as context:
        assert type(context.transport) is HttpBackend
    with OperationContext(RemoteSpec.from_url(
            "https://lab.test/data/", backend="cyverse")) as context:
        assert type(context.transport) is CyVerseBackend


def test_backend_lifecycle_and_generic_discovery(tmp_path):
    events = []

    class SurveyBackend(DataBackend):
        def iter_entries(self, on_directory=None):
            on_directory("")
            yield RemoteEntry("tile.fits", size=8)

        def prepare(self):
            events.append("prepare")

        def fetch(self, relative_path, destination, *, chunk_size=8192):
            self.context.check_cancelled()
            destination.write_bytes(b"contents")

        def read_text(self, relative_path, limit):
            assert limit == 20
            return "metadata"

        def cancel(self):
            events.append("cancel")

        def close(self):
            events.append("close")

    backends.register_backend("survey", SurveyBackend)
    remote = RemoteSpec.from_url("https://lab.test/", backend="survey")
    with OperationContext(remote) as context:
        context.text_limit = 20
        context.transport.prepare()
        assert context.read_text("info") == "metadata"
        assert discover_remote_files(context) == [RemoteEntry("tile.fits", size=8)]
        context.transport.fetch("tile.fits", tmp_path / "payload")
        assert (tmp_path / "payload").read_bytes() == b"contents"
    assert events == ["prepare", "prepare", "close"]
    with pytest.raises(RuntimeError):
        with OperationContext(remote):
            raise RuntimeError("interrupted")
    assert events[-2:] == ["cancel", "close"]


@pytest.mark.parametrize("name", ["", "Bad", "module.Backend", "bad/name", 12])
def test_invalid_registry_names(name):
    with pytest.raises(RemoteConfigurationError, match="Backend name"):
        backends.register_backend(name, HttpBackend)


@pytest.mark.parametrize("backend_class", [
    object, object(), DataBackend, "module.Class",
])
def test_invalid_registry_classes(backend_class):
    with pytest.raises(RemoteConfigurationError, match="DataBackend subclass"):
        backends.register_backend("invalid", backend_class)


def test_duplicate_registration_rejected():
    with pytest.raises(RemoteConfigurationError, match="already registered"):
        backends.register_backend("http", HttpBackend)
    backends.register_backend("survey", HttpBackend)
    with pytest.raises(RemoteConfigurationError, match="already registered"):
        backends.register_backend("survey", HttpBackend)


def test_plugin_loading_is_lazy_and_cached(monkeypatch):
    loaded = []

    class Entry:
        name = "survey"

        def load(self):
            loaded.append(self.name)
            return HttpBackend

    monkeypatch.setattr(backends.metadata, "entry_points",
                        lambda: {"hallmark.backends": [Entry()]})
    remote = RemoteSpec.from_url("https://lab.test/", backend="survey")
    assert not loaded
    for _ in range(2):
        with OperationContext(remote) as context:
            assert isinstance(context.transport, HttpBackend)
    assert loaded == ["survey"]
    with pytest.raises(RemoteConfigurationError, match="already registered"):
        backends.register_backend("survey", HttpBackend)


def test_modern_entry_point_selection(monkeypatch):
    entry = SimpleNamespace(name="survey", load=lambda: HttpBackend)

    class Entries:
        def select(self, **kwargs):
            assert kwargs == {"group": "hallmark.backends"}
            return [entry]

    monkeypatch.setattr(backends.metadata, "entry_points", Entries)
    assert backends.get_backend("survey") is HttpBackend


def test_duplicate_and_invalid_installed_backends(monkeypatch):
    entry = SimpleNamespace(name="survey", load=lambda: object)
    entries = [entry, entry]
    monkeypatch.setattr(backends.metadata, "entry_points",
                        lambda: {"hallmark.backends": entries})
    with pytest.raises(RemoteConfigurationError, match="Duplicate backend"):
        backends.get_backend("survey")
    entries.pop()
    with pytest.raises(RemoteConfigurationError, match="DataBackend subclass"):
        backends.get_backend("survey")
    entries[:] = [SimpleNamespace(name="http", load=lambda: HttpBackend)]
    with pytest.raises(RemoteConfigurationError, match="Duplicate backend"):
        backends.get_backend("http")


def test_missing_plugin_can_be_parsed_without_loading(monkeypatch):
    def fail():
        raise AssertionError("Parsing must not inspect installed plugins")

    monkeypatch.setattr(backends.metadata, "entry_points", fail)
    remote = RemoteSpec.from_url("https://lab.test/", backend="uninstalled")
    monkeypatch.setattr(backends.metadata, "entry_points", lambda: {})
    with pytest.raises(RemoteConfigurationError, match="not installed or registered"):
        OperationContext(remote)


def test_plugin_load_failure_is_actionable_and_sanitized(monkeypatch):
    def fail():
        raise ImportError("sensitive internal path")

    monkeypatch.setattr(backends.metadata, "entry_points", lambda: {
        "hallmark.backends": [SimpleNamespace(name="survey", load=fail)]})
    with pytest.raises(RemoteConfigurationError, match="Unable to load backend") as exc:
        backends.get_backend("survey")
    assert "sensitive" not in str(exc.value)


def test_options_are_copied_and_deeply_immutable():
    options = {"routes": [{"names": ["first"]}], "enabled": True}
    remote = RemoteSpec.from_url("https://lab.test/", backend_options=options)
    options["routes"][0]["names"].append("later")
    assert remote.backend_options["routes"][0]["names"] == ("first",)
    with pytest.raises(TypeError):
        remote.backend_options["routes"][0]["names"] = ()
    assert copy_backend_options(remote.backend_options) == {
        "routes": [{"names": ["first"]}], "enabled": True}
    assert readonly_backend_options() == {}
    assert copy_backend_options() == {}


@pytest.mark.parametrize("options", [[], {1: "bad"}, {"object": object()}])
def test_invalid_options_are_rejected(options):
    with pytest.raises(RemoteConfigurationError, match="Backend option"):
        RemoteSpec.from_url("https://lab.test/", backend_options=options)


def test_recursive_options_are_rejected():
    options = {}
    options["cycle"] = options
    with pytest.raises(RemoteConfigurationError, match="cycles"):
        readonly_backend_options(options)


def test_base_capabilities():
    backend = DataBackend(SimpleNamespace())
    with pytest.raises(CapabilityError):
        backend.iter_entries()
    with pytest.raises(CapabilityError):
        backend.read_text("index", 10)
    with pytest.raises(NotImplementedError):
        backend.fetch("file", Path("unused"))


def test_constructor_failure_closes_context_sessions(monkeypatch):
    closed = []
    monkeypatch.setattr("hallmark.transport.requests.Session", lambda: SimpleNamespace(
        close=lambda: closed.append(True)))

    class BrokenBackend(DataBackend):
        def __init__(self, context):
            super().__init__(context)
            context.session()
            raise RuntimeError("setup failed")

    backends.register_backend("broken", BrokenBackend)
    with pytest.raises(RuntimeError, match="setup failed"):
        OperationContext(RemoteSpec.from_url("https://lab.test/", backend="broken"))
    assert closed == [True]


def test_cancel_failure_still_closes_resources():
    closed = []

    class BrokenBackend(DataBackend):
        def cancel(self):
            raise RuntimeError("cancel failed")

        def close(self):
            closed.append(True)

    backends.register_backend("broken", BrokenBackend)
    with pytest.raises(RuntimeError, match="cancel failed"):
        with OperationContext(
            RemoteSpec.from_url("https://lab.test/", backend="broken")
        ):
            raise ValueError("operation failed")
    assert closed == [True]
