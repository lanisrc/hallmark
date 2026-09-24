"""Persist backend settings without importing plugins or sharing mutable options."""

import pytest

from hallmark import Repo
from hallmark import backends
from hallmark.backends import DataBackend, RemoteEntry, register_backend
from hallmark.repo_builder import build_repo
from hallmark.repo_config import normalize_remotes


def test_backend_config_roundtrip_and_selective_updates(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    options = {"servers": [{"url": "https://data.test/", "prefix": "north"}]}
    repo.set_config(remote_url="https://api.test/", remote_backend="survey",
                    remote_backend_options=options)
    options["servers"][0]["prefix"] = "changed"
    reopened = Repo(repo.worktree)
    remote = reopened.state.config["remote"]
    assert remote["backend"] == "survey"
    assert remote["backend_options"]["servers"][0]["prefix"] == "north"
    reopened.set_config(remote_name="archive")
    assert Repo(repo.worktree).state.config["remote"]["backend"] == "survey"
    reopened.set_config(remote_backend="", remote_backend_options={})
    remote = Repo(repo.worktree).state.config["remote"]
    assert "backend" not in remote
    assert remote["backend_options"] == {}


@pytest.mark.parametrize("update", [
    {"remote_backend": "arbitrary.module:Class"},
    {"remote_backend_options": ["not", "a", "mapping"]},
    {"remote_backend_options": {"nested": object()}},
])
def test_invalid_backend_update_preserves_existing_config(tmp_path, update):
    repo = Repo.init(tmp_path / "repo")
    repo.set_config(remote_url="https://data.test/")
    before = (repo.dothm.path / "config.yml").read_bytes()
    with pytest.raises(ValueError):
        repo.set_config(**update)
    assert (repo.dothm.path / "config.yml").read_bytes() == before
    assert "backend" not in repo.state.config["remote"]


def test_normalization_copies_nested_backend_options():
    source = {"name": "archive", "backend": "survey",
              "backend_options": {"releases": [1, 2]}}
    normalized = normalize_remotes(source)
    source["backend_options"]["releases"].clear()
    assert normalized[0]["backend_options"]["releases"] == [1, 2]


def test_builder_passes_backend_settings_to_discovery(monkeypatch, tmp_path):
    captured = []

    def build(*args):
        captured.append(args[-1].remote)

    # No backend installation or network is required to inspect this hand-off.
    class Context:
        def __init__(self, remote):
            self.remote = remote

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("hallmark.repo_builder.OperationContext", Context)
    monkeypatch.setattr("hallmark.repo_builder._build_repo", build)
    with pytest.warns(DeprecationWarning, match="Repo.init"):
        build_repo(tmp_path / "data.hm", "dataset", dataset_url="https://api.test/",
                   backend="survey", backend_options={"release": 3})
    assert captured[0].backend == "survey"
    assert captured[0].backend_options["release"] == 3


@pytest.mark.parametrize("mirror", [None, "https://mirror.test/data/"])
def test_builder_persists_backend_only_for_its_data_source(
        monkeypatch, tmp_path, mirror):
    class SurveyBackend(DataBackend):
        def iter_entries(self, on_directory=None):
            assert self.context.remote.backend_options["release"] == 3
            yield RemoteEntry("tile.fits", size=12)

    monkeypatch.setattr(backends, "_registered", dict(backends._registered))
    monkeypatch.setattr(backends.metadata, "entry_points", lambda: {})
    register_backend("builder-survey", SurveyBackend)
    with pytest.warns(DeprecationWarning):
        repo = build_repo(
            tmp_path / "survey.hm", "survey", dataset_url="https://api.test/",
            backend="builder-survey", backend_options={"release": 3},
            remotes=[{"name": "mirror", "url": mirror}] if mirror else None)
    remote = Repo(repo.dothm.path).state.config["remote"][0]
    if mirror:
        assert remote == {"name": "mirror", "url": mirror}
    else:
        assert remote["backend"] == "builder-survey"
        assert remote["backend_options"] == {"release": 3}
