"""Persist backend settings without importing plugins or sharing mutable options."""

import pytest

from hallmark import Repo
from hallmark.repo.config import normalize_remotes


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




