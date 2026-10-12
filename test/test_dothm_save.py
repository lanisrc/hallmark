"""Staging large catalogues in one Git process without pathspec surprises."""

import pandas as pd

from hallmark import Repo


def remote_state(repo, paths, **columns):
    repo.state.config = {"data": [{"db": "data.tsv"}],
                         "remote": [{"name": "origin", "url": "https://x.test/d/"}],
                         "sources": [{"name": "origin", "remote": "origin"}]}
    repo.state.data = pd.DataFrame({"path": paths, "source": "origin", **columns})
    return repo.state


def test_save_stages_every_record_with_one_git_call(tmp_path, monkeypatch):
    from git.cmd import Git

    repo = Repo.init(tmp_path / "repo")
    calls = []
    original = Git._call_process

    def call_process(self, method, *args, **kwargs):
        calls.append(method)
        return original(self, method, *args, **kwargs)

    monkeypatch.setattr(Git, "_call_process", call_process)
    repo.dothm.save_state(remote_state(repo, [f"f/{i}.h5" for i in range(50)]))
    monkeypatch.undo()
    # GitPython's index.add would hash each file in its own process.
    assert calls.count("update_index") == 1
    assert "hash_object" not in calls
    assert len(calls) < 5
    assert len(repo.status()["staged"]["added"]) == 50


def test_saved_paths_are_literal_not_pathspecs(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    paths = ["a*b [1].h5", "ab [1].h5", "ünï code/x?.h5", ":(glob)y.h5"]
    repo.dothm.save_state(remote_state(repo, paths))
    staged = {path for path, _ in repo.dothm.index.entries}
    assert {f"versions/{path}" for path in paths} <= staged
    assert sorted(repo.status()["staged"]["added"]) == sorted(paths)
    assert repo.commit("Literal names")
    assert sorted(Repo(repo.worktree).state.data["path"]) == sorted(paths)


def test_unchanged_records_are_not_rewritten(tmp_path):
    repo = Repo.init(tmp_path / "repo")
    state = remote_state(repo, ["a.h5", "b.h5"], sha1=["", ""])
    repo.dothm.save_state(state)
    versions = repo.dothm.path / "versions"
    before = {name: (versions / name).stat().st_ino for name in ("a.h5", "b.h5")}
    state.data.loc[state.data["path"] == "b.h5", "sha1"] = "1" * 40
    repo.dothm.save_state(state)
    after = {name: (versions / name).stat().st_ino for name in ("a.h5", "b.h5")}
    assert after["a.h5"] == before["a.h5"]
    assert after["b.h5"] != before["b.h5"]
    assert repo.status()["staged"]["added"] == ["a.h5", "b.h5"]
