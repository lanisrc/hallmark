"""Local add acceptance tests using real repositories and the public CLI."""

from pathlib import Path

from click.testing import CliRunner
import pandas as pd
import pytest

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.transport import OperationContext


FMT = "a{a}_i{i}.h5"
NEW_FMT = "b{b}_i{i}.h5"
INITIAL_STATE = ["config.yml", "data.tsv", "meta.yml"]


@pytest.fixture
def local_repo(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "repo")
    monkeypatch.chdir(repo.worktree)
    return repo


def _write(root, *names):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"contents of {name}\n", encoding="utf-8")


def _invoke(*args, exit_code=0):
    result = CliRunner().invoke(hallmark, list(args))
    assert result.exit_code == exit_code, result.output or repr(result.exception)
    return result.output


def _seed(repo, names=("a0_i0.h5", "a0_i30.h5", "a0_i60.h5"), fmt=FMT):
    _write(repo.worktree, *names)
    _invoke("add", fmt)
    _invoke("commit", "-m", "First dataset")


def _assert_catalog(repo, paths, fmt=FMT):
    """Reopen the repository to check what subsequent commands will actually see."""
    saved = Repo(repo.worktree)
    assert saved.state.config["data"][0]["fmt"] == fmt
    assert sorted(saved.state.data["path"]) == sorted(paths)
    assert saved.state.data["path"].is_unique
    return saved


def _assert_status(repo, *, state=(), added=(), modified=(), deleted=(),
                   worktree_modified=(), worktree_deleted=(), untracked=(),
                   branch="main"):
    """Check exact status categories and their complete CLI rendering."""
    staged = {"state": sorted(state), "added": sorted(added),
              "modified": sorted(modified), "deleted": sorted(deleted)}
    worktree = {"modified": sorted(worktree_modified),
                "deleted": sorted(worktree_deleted)}
    assert Repo(repo.worktree).status() == {
        "branch": branch, "staged": staged, "worktree": worktree,
        "untracked": sorted(untracked), "remote_catalog": False,
    }
    lines = [f"On branch {branch}"]
    for title, entries in (
        ("Changes to be committed:", (("state", state), ("new file", added),
                                     ("modified", modified), ("deleted", deleted))),
        ("Changes not staged for commit:", (("modified", worktree_modified),
                                           ("deleted", worktree_deleted))),
    ):
        if any(paths for _, paths in entries):
            lines.extend(["", title])
            for label, paths in entries:
                lines.extend(f"  {label}:   {path}" for path in sorted(paths))
    if untracked:
        lines.extend(["", "Untracked files:"])
        lines.extend(f"  {path}" for path in sorted(untracked))
    if len(lines) == 1:
        lines.extend(["", "nothing to commit, working tree clean"])
    assert _invoke("status") == "\n".join(lines) + "\n"


def test_pattern_sets_saved_format_and_recursively_adds_matching_files(local_repo):
    repo = local_repo
    paths = ["a0_i30.h5", "results/a0_i30.h5", "results/deep/a1_i60.h5"]
    _write(repo.worktree, *paths, "notes.txt", "results/readme.txt")

    output = _invoke("add", FMT)

    for path in paths:
        assert path in output
    saved = _assert_catalog(repo, paths)
    assert dict(zip(saved.state.data["path"], saved.state.data["sha1"])) == {
        path: Repo.checksum(repo.worktree / path) for path in paths}
    _assert_status(repo, state=INITIAL_STATE, added=paths,
                   untracked=["notes.txt", "results/readme.txt"])
    _invoke("commit", "-m", "First dataset")
    _assert_status(repo, untracked=["notes.txt", "results/readme.txt"])


@pytest.mark.parametrize("inputs", [
    ["a0_i30.h5"], ["a0_i30.h5", "a0_i90.h5"],
])
def test_explicit_files_preserve_other_staged_and_unstaged_changes(local_repo, inputs):
    repo = local_repo
    _seed(repo)
    _write(repo.worktree, "a0_i90.h5", "a1_i90.h5", "notes.txt")
    (repo.worktree / "a0_i0.h5").write_text("previously staged\n")
    _invoke("add", "a0_i0.h5")
    (repo.worktree / "a0_i30.h5").write_text("selected edit\n")
    (repo.worktree / "a0_i60.h5").unlink()
    _assert_status(repo, state=["data.tsv"], modified=["a0_i0.h5"],
                   worktree_modified=["a0_i30.h5"], worktree_deleted=["a0_i60.h5"],
                   untracked=["a0_i90.h5", "a1_i90.h5", "notes.txt"])

    _invoke("add", *inputs)

    added = ["a0_i90.h5"] if len(inputs) > 1 else []
    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5", *added])
    _assert_status(repo, state=["data.tsv"], added=added,
                   modified=["a0_i0.h5", "a0_i30.h5"],
                   worktree_deleted=["a0_i60.h5"],
                   untracked=["a1_i90.h5", "notes.txt",
                              *([] if added else ["a0_i90.h5"])])


def test_new_file_adds_only_named_file(local_repo):
    repo = local_repo
    _seed(repo)
    _write(repo.worktree, "a0_i90.h5", "a1_i90.h5", "notes.txt")

    _invoke("add", "a0_i90.h5")

    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5", "a0_i90.h5"])
    _assert_status(repo, state=["data.tsv"], added=["a0_i90.h5"],
                   untracked=["a1_i90.h5", "notes.txt"])


def test_python_add_paths_accepts_path_objects_and_preserves_staging(local_repo):
    repo = local_repo
    _seed(repo)
    _write(repo.worktree, "a0_i90.h5", "results/a1_i90.h5", "results/notes.txt")
    saved = Repo(repo.worktree)

    first = saved.add("a0_i90.h5")
    result = saved.add_paths([Path("results")])

    assert first["path"].tolist() == ["a0_i90.h5"]
    assert result["path"].tolist() == ["results/a1_i90.h5"]
    assert result.attrs["unmatched"] == ["results/notes.txt"]
    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5",
                           "a0_i90.h5", "results/a1_i90.h5"])
    _assert_status(repo, state=["data.tsv"], added=["a0_i90.h5", "results/a1_i90.h5"],
                   untracked=["results/notes.txt"])


def test_explicit_add_uses_saved_regex_encoding(local_repo):
    repo = local_repo
    fmt = "a{aspin}_i{i}.h5"
    _write(repo.worktree, "am0.5_i30.h5", "a0_i60.h5")
    _invoke("set-config", "--fmt", fmt, "--encoding", "aspin=m([0-9.]+)")

    _invoke("add", "--regex", "am0.5_i30.h5")

    saved = _assert_catalog(repo, ["am0.5_i30.h5"], fmt)
    assert saved.state.data["aspin"].tolist() == ["-0.5"]
    _assert_status(repo, state=INITIAL_STATE, added=["am0.5_i30.h5"],
                   untracked=["a0_i60.h5"])


@pytest.mark.parametrize("folder", ["results", "results/"])
def test_folder_recurses_skips_and_lists_nonmatching_files(local_repo, folder):
    repo = local_repo
    _seed(repo)
    paths = ["results/a0_i30.h5", "results/deep/a0_i30.h5"]
    skipped = ["results/deep/notes.txt", "results/readme.txt"]
    _write(repo.worktree, *paths, *skipped, "a0_i90.h5", "results-other/a1_i90.h5")
    (repo.worktree / "a0_i0.h5").write_text("previously staged\n")
    _invoke("add", "a0_i0.h5")
    (repo.worktree / "a0_i30.h5").write_text("leave unstaged\n")
    (repo.worktree / "a0_i60.h5").unlink()

    output = _invoke("add", folder)

    assert output.split("Skipped files that do not match the branch pattern:\n")[1] \
        == "".join(f"  {path}\n" for path in skipped)
    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5", *paths])
    _assert_status(repo, state=["data.tsv"], added=paths, modified=["a0_i0.h5"],
                   worktree_modified=["a0_i30.h5"], worktree_deleted=["a0_i60.h5"],
                   untracked=[*skipped, "a0_i90.h5", "results-other/a1_i90.h5"])
    before = Repo(repo.worktree).state.data.copy(deep=True)
    _invoke("add", folder, paths[0])
    pd.testing.assert_frame_equal(Repo(repo.worktree).state.data, before)


def test_folder_with_no_matches_keeps_catalog_and_reports_skips(local_repo):
    repo = local_repo
    _seed(repo)
    _write(repo.worktree, "results/notes.txt")

    output = _invoke("add", "results/")

    assert "No files matched the format string." in output
    assert "Skipped files that do not match the branch pattern:\n  results/notes.txt" \
        in output
    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5"])
    _assert_status(repo, untracked=["results/notes.txt"])


@pytest.mark.parametrize("inputs", [
    ["notes.txt"], ["a0_i90.h5", "notes.txt"], ["notes.txt", "a0_i90.h5"],
    ["results/", "results/notes.txt"], ["results/notes.txt", "results/"],
])
def test_named_nonmatching_file_errors_without_partial_staging(local_repo, inputs):
    repo = local_repo
    _seed(repo)
    _write(repo.worktree, "a0_i90.h5", "notes.txt",
           "results/a1_i90.h5", "results/notes.txt")
    (repo.worktree / "a0_i0.h5").write_text("previously staged\n")
    _invoke("add", "a0_i0.h5")
    before = Repo(repo.worktree)
    manifest_bytes = (repo.dothm.path / "data.tsv").read_bytes()
    config_bytes = (repo.dothm.path / "config.yml").read_bytes()

    output = _invoke("add", *inputs, exit_code=1)

    invalid = "results/notes.txt" if "results/notes.txt" in inputs else "notes.txt"
    assert output == f"Error: file does not match branch format {FMT!r}: {invalid!r}\n"
    saved = Repo(repo.worktree)
    pd.testing.assert_frame_equal(saved.state.data, before.state.data)
    assert (repo.dothm.path / "data.tsv").read_bytes() == manifest_bytes
    assert (repo.dothm.path / "config.yml").read_bytes() == config_bytes
    _assert_status(repo, state=["data.tsv"], modified=["a0_i0.h5"],
                   untracked=["a0_i90.h5", "notes.txt",
                              "results/a1_i90.h5", "results/notes.txt"])


@pytest.mark.parametrize("cwd", [".", "s1", "s1/deep"])
@pytest.mark.parametrize("fmt", [FMT, "{folder}/a{a}_i{i}.h5"])
def test_dot_stages_whole_project_including_deletions(
        local_repo, monkeypatch, cwd, fmt):
    repo = local_repo
    original = ["s1/a0_i30.h5", "s2/a0_i60.h5", "s1/a0_i0.h5"]
    if fmt == FMT:
        original.append("a0_i0.h5")
    _seed(repo, original, fmt)
    modified = ["s1/a0_i30.h5", "s2/a0_i60.h5"]
    for path in modified:
        (repo.worktree / path).write_text("changed\n")
    deleted = ["s1/a0_i0.h5"]
    (repo.worktree / deleted[0]).unlink()
    added = ["s1/a0_i90.h5", "s2/a1_i90.h5"]
    skipped = ["notes.txt", "s1/deep/readme.txt", "s2/b0_i0.h5"]
    _write(repo.worktree, *added, *skipped)
    monkeypatch.chdir(repo.worktree / cwd)
    _assert_status(repo, worktree_modified=modified, worktree_deleted=deleted,
                   untracked=[*added, *skipped])

    output = _invoke("add", ".")

    assert output.split("Skipped files that do not match the branch pattern:\n")[1] \
        == "".join(f"  {path}\n" for path in sorted(skipped))
    expected_paths = [path for path in original if path not in deleted] + added
    _assert_catalog(repo, expected_paths, fmt)
    _assert_status(repo, state=["data.tsv"], added=added, modified=modified,
                   deleted=deleted, untracked=skipped)
    _invoke("commit", "-m", "Update dataset")
    _assert_status(repo, untracked=skipped)


@pytest.mark.parametrize("inputs", [
    ["."], ["a0_i30.h5"], ["a0_i30.h5", "a0_i60.h5"], ["results/"],
])
def test_paths_require_saved_pattern(local_repo, inputs):
    repo = local_repo
    files = ["a0_i30.h5", "a0_i60.h5", "results/a0_i90.h5"]
    _write(repo.worktree, *files)

    output = _invoke("add", *inputs, exit_code=1)

    assert output == ('Error: No file pattern set. Run hm add "PATTERN" or '
                      'hm set-config --fmt "PATTERN" first.\n')
    assert Repo(repo.worktree).state.config["data"][0].get("fmt") is None
    _assert_status(repo, state=INITIAL_STATE, untracked=files)


def test_set_config_enables_explicit_add_without_scanning(local_repo):
    repo = local_repo
    _write(repo.worktree, "a0_i30.h5", "a0_i60.h5")

    _invoke("set-config", "--fmt", FMT)
    _assert_status(repo, state=INITIAL_STATE,
                   untracked=["a0_i30.h5", "a0_i60.h5"])
    _invoke("add", "a0_i30.h5")

    _assert_catalog(repo, ["a0_i30.h5"])
    _assert_status(repo, state=INITIAL_STATE, added=["a0_i30.h5"],
                   untracked=["a0_i60.h5"])


@pytest.mark.parametrize("command", [
    ["add", NEW_FMT], ["set-config", "--fmt", NEW_FMT],
])
@pytest.mark.parametrize("committed", [False, True])
def test_pattern_change_is_blocked_by_incompatible_catalog(
        local_repo, command, committed):
    repo = local_repo
    paths = ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5"]
    _write(repo.worktree, *paths)
    _invoke("add", FMT)
    if committed:
        _invoke("commit", "-m", "First dataset")
    _write(repo.worktree, "b0_i90.h5")
    before = Repo(repo.worktree)
    status_before = _invoke("status")

    output = _invoke(*command, exit_code=1)

    assert output == (f"Error: 3 files in the catalog don't fit {NEW_FMT}.\n"
                      "Remove them first, or use a new branch for the new pattern.\n")
    saved = _assert_catalog(repo, paths)
    pd.testing.assert_frame_equal(saved.state.data, before.state.data)
    assert saved.state.config == before.state.config
    assert _invoke("status") == status_before
    _assert_status(repo, state=[] if committed else INITIAL_STATE,
                   added=[] if committed else paths, untracked=["b0_i90.h5"])


def test_pattern_change_counts_only_incompatible_catalog_rows(local_repo):
    repo = local_repo
    _seed(repo)
    (repo.worktree / "a0_i0.h5").unlink()

    output = _invoke("add", "a{a}_i30.h5", exit_code=1)

    assert "2 files in the catalog don't fit a{a}_i30.h5." in output
    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5"])
    _assert_status(repo, worktree_deleted=["a0_i0.h5"])


def test_new_branch_can_stage_removals_then_use_its_own_pattern(local_repo):
    repo = local_repo
    _seed(repo)
    _invoke("checkout", "experiment")
    for path in repo.worktree.glob("*.h5"):
        path.unlink()
    _write(repo.worktree, "b0_i90.h5")
    _invoke("add", NEW_FMT, exit_code=1)
    _invoke("add", ".")
    _assert_catalog(repo, [])
    removed = ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5"]
    _assert_status(repo, branch="experiment", state=["data.tsv"], deleted=removed,
                   untracked=["b0_i90.h5"])

    _invoke("add", NEW_FMT)

    _assert_catalog(repo, ["b0_i90.h5"], NEW_FMT)
    _assert_status(repo, branch="experiment", state=["config.yml", "data.tsv"],
                   added=["b0_i90.h5"], deleted=removed)
    _invoke("commit", "-m", "New pattern on experiment")
    _assert_status(repo, branch="experiment")
    _invoke("checkout", "main")
    _assert_catalog(repo, removed)
    assert all((repo.worktree / path).is_file() for path in removed)
    assert not (repo.worktree / "b0_i90.h5").exists()
    _assert_status(repo)


def test_compatible_pattern_change_preserves_files_and_reparses_fields(local_repo):
    repo = local_repo
    _seed(repo)
    new_fmt = "a{spin}_i{angle}.h5"
    before = Repo(repo.worktree).state.data.set_index("path")["sha1"].to_dict()

    _invoke("add", new_fmt)

    saved = _assert_catalog(repo, list(before), new_fmt)
    assert saved.state.data.set_index("path")["sha1"].to_dict() == before
    assert list(saved.state.data.columns) == ["sha1", "path", "spin", "angle"]
    _assert_status(repo, state=["config.yml", "data.tsv"])


@pytest.mark.parametrize("option", ["--force", "--all"])
def test_add_has_no_force_or_all_option(local_repo, option):
    repo = local_repo
    _seed(repo)
    _write(repo.worktree, "notes.txt")

    output = _invoke("add", option, "notes.txt", exit_code=2)

    assert f"No such option: {option}" in output
    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5"])
    _assert_status(repo, untracked=["notes.txt"])


def test_explicit_file_and_folder_paths_are_relative_to_nested_cwd(
        local_repo, monkeypatch):
    repo = local_repo
    _seed(repo)
    paths = ["s1/a0_i90.h5", "s1/results/a1_i90.h5", "s2/a0_i90.h5"]
    _write(repo.worktree, *paths, "s1/results/notes.txt")
    monkeypatch.chdir(repo.worktree / "s1")

    _invoke("add", "a0_i90.h5", "results/", "../s2/a0_i90.h5")

    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5", *paths])
    _assert_status(repo, state=["data.tsv"], added=paths,
                   untracked=["s1/results/notes.txt"])


@pytest.mark.parametrize("command,expected", [
    (["info"], "dot-hallmark repo:"),
    (["status"], "nothing to commit, working tree clean"),
    (["log"], "First dataset"),
    (["branch"], "main"),
])
def test_repository_commands_find_parents(local_repo, monkeypatch, command, expected):
    repo = local_repo
    _seed(repo)
    nested = repo.worktree / "s1/deep"
    nested.mkdir(parents=True)
    root_output = _invoke(*command)
    monkeypatch.chdir(nested)

    output = _invoke(*command)

    assert expected in output
    assert output == root_output
    _assert_status(repo)


def test_repository_discovery_selects_nearest_repository(local_repo, monkeypatch):
    outer = local_repo
    _seed(outer)
    inner = Repo.init(outer.worktree / "inner")
    _write(inner.worktree, "b0_i30.h5")
    nested = inner.worktree / "s1/deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    _invoke("set-config", "--fmt", NEW_FMT)
    _invoke("add", ".")

    _assert_catalog(inner, ["b0_i30.h5"], NEW_FMT)
    _assert_status(inner, state=INITIAL_STATE, added=["b0_i30.h5"])
    _assert_catalog(outer, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5"])


@pytest.mark.parametrize("inputs", [
    ["https://example.test/data/{src}_{day:03d}.txt"],
    ["https://example.test/data/", "--fmt", "{src}_{day:03d}.txt"],
])
def test_nested_remote_add_keeps_url_workflow_and_download_discovery(
        local_repo, monkeypatch, inputs):
    repo = local_repo
    nested = repo.worktree / "s1/deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    reads = []

    def read_text(context, path):
        reads.append(path)
        assert path == "", "Remote add must only read the directory listing"
        return ('<h1>Index of data</h1>'
                '<a href="M87_001.txt">M87_001.txt</a>'
                '<a href="notes.txt">notes.txt</a>')

    monkeypatch.setattr(OperationContext, "read_text", read_text)

    _invoke("add", *inputs)

    saved = Repo(repo.worktree)
    assert saved.state.data["path"].tolist() == ["M87_001.txt"]
    assert saved.status()["staged"] == {
        "state": INITIAL_STATE, "added": ["M87_001.txt"],
        "modified": [], "deleted": [],
    }
    assert saved.status()["worktree"] == {"modified": [], "deleted": []}
    assert saved.status()["untracked"] == []
    assert not (repo.worktree / "M87_001.txt").exists()
    _invoke("commit", "-m", "Remote catalog from subfolder")
    assert "nothing to commit, remote catalog unchanged" in _invoke("status")
    assert "1 file(s)" in _invoke("download", "--all", "--dry-run")
    assert reads == [""]


@pytest.mark.parametrize("inputs", [[FMT], ["a0_i30.h5"], ["."]])
def test_commit_rejects_edit_after_add_and_succeeds_after_readd(
        local_repo, monkeypatch, inputs):
    repo = local_repo
    _seed(repo)
    path = repo.worktree / "a0_i30.h5"
    path.write_text("staged version\n")
    _invoke("add", *inputs)
    head_before = Repo(repo.worktree).dothm.head.commit.hexsha
    path.write_text("edited after add\n")
    nested = repo.worktree / "s1/deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    output = _invoke("commit", "-m", "Must not commit stale checksum", exit_code=1)
    assert "changed after it was added" in output
    assert "hm add" in output
    assert Repo(repo.worktree).dothm.head.commit.hexsha == head_before
    _assert_status(repo, state=["data.tsv"], modified=["a0_i30.h5"],
                   worktree_modified=["a0_i30.h5"])

    _invoke("add", "../../a0_i30.h5")
    _assert_status(repo, state=["data.tsv"], modified=["a0_i30.h5"])
    _invoke("commit", "-m", "Current file contents")
    _invoke("checkout", "experiment")
    _assert_status(repo, branch="experiment")


@pytest.mark.parametrize("command", ["add", "set-config"])
def test_numeric_pattern_change_preserves_zero_padded_paths(local_repo, command):
    repo = local_repo
    paths = ["a01.h5", "results/a01.h5"]
    _seed(repo, paths, "a{a}.h5")
    before = Repo(repo.worktree).state.data.set_index("path")["sha1"].to_dict()
    fmt = "a{number:d}.h5"

    if command == "add":
        _invoke("add", fmt)
    else:
        _invoke("set-config", "--fmt", fmt)

    saved = _assert_catalog(repo, paths, fmt)
    assert saved.state.data.set_index("path")["sha1"].to_dict() == before
    assert saved.state.data["number"].astype(int).tolist() == [1, 1]
    assert list(saved.state.data.columns) == ["sha1", "path", "number"]
    _assert_status(repo, state=["config.yml", "data.tsv"])

    _invoke("commit", "-m", "Keep zero-padded paths")
    _assert_status(repo)

@pytest.mark.parametrize("command", ["add", "set-config"])
def test_pattern_change_is_case_sensitive_and_atomic(local_repo, command):
    repo = local_repo
    paths = ["a01.h5", "results/a01.h5"]
    old_fmt = "a{a}.h5"
    _seed(repo, paths, old_fmt)
    before = Repo(repo.worktree)
    metadata = {name: (repo.dothm.path / name).read_bytes()
                for name in INITIAL_STATE}
    fmt = "A{a}.h5"

    if command == "add":
        output = _invoke("add", fmt, exit_code=1)
    else:
        output = _invoke("set-config", "--fmt", fmt, exit_code=1)

    assert output == (
        f"Error: 2 files in the catalog don't fit {fmt}.\n"
        "Remove them first, or use a new branch for the new pattern.\n"
    )
    saved = _assert_catalog(repo, paths, old_fmt)
    pd.testing.assert_frame_equal(saved.state.data, before.state.data)
    assert saved.state.config == before.state.config
    assert metadata == {name: (repo.dothm.path / name).read_bytes()
                        for name in INITIAL_STATE}
    _assert_status(repo)


def test_remote_format_update_preserves_catalog_without_sha1(
        local_repo, monkeypatch):
    repo = local_repo
    reads = []

    def read_text(context, path):
        reads.append(path)
        assert path == ""
        return ('<h1>Index of data</h1>'
                '<a href="M87_001.fits">M87_001.fits</a>'
                '<a href="M87_002.fits">M87_002.fits</a>')

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    _invoke("add", "https://example.test/data/{src}_{day:03d}.fits")
    _invoke("commit", "-m", "Remote catalog")
    before = Repo(repo.worktree)
    assert "sha1" not in before.state.data.columns

    _invoke("set-config", "--fmt", "{name}.fits")

    saved = Repo(repo.worktree)
    pd.testing.assert_frame_equal(saved.state.data, before.state.data)
    assert saved.state.meta == before.state.meta
    assert saved.state.config["remote"] == before.state.config["remote"]
    assert saved.state.config["data"][0]["fmt"] == "{name}.fits"
    assert saved.status() == {
        "branch": "main",
        "staged": {
            "state": ["config.yml"],
            "added": [],
            "modified": [],
            "deleted": [],
        },
        "worktree": {"modified": [], "deleted": []},
        "untracked": [],
        "remote_catalog": True,
    }
    assert _invoke("status") == (
        "On branch main\n\nChanges to be committed:\n"
        "  state:   config.yml\n"
    )
    assert reads == [""]
    assert not saved.objects.root.exists()
    assert not any((repo.worktree / path).exists()
                   for path in saved.state.data["path"])


def test_overlapping_add_paths_hash_each_matching_file_once(
        local_repo, monkeypatch):
    repo = local_repo
    _seed(repo)
    paths = ["results/a0_i90.h5", "results/deep/a1_i90.h5"]
    _write(repo.worktree, *paths, "results/notes.txt")
    saved = Repo(repo.worktree)
    original_checksum = saved.checksum
    calls = []

    def record_checksum(path):
        calls.append(path.relative_to(repo.worktree).as_posix())
        return original_checksum(path)

    monkeypatch.setattr(saved, "checksum", record_checksum)

    result = saved.add_paths([
        Path("results"),
        Path(paths[0]),
        Path("results/deep"),
        Path("results"),
    ])

    assert sorted(calls) == paths
    assert result["path"].tolist() == paths
    assert result.attrs["unmatched"] == ["results/notes.txt"]
    _assert_catalog(repo, ["a0_i0.h5", "a0_i30.h5", "a0_i60.h5", *paths])
    _assert_status(
        repo,
        state=["data.tsv"],
        added=paths,
        untracked=["results/notes.txt"],
    )


def test_equal_contents_at_different_paths_stay_independent(local_repo):
    repo = local_repo
    paths = ["s1/a0_i30.h5", "s2/a0_i30.h5"]
    _write(repo.worktree, *paths)
    for path in paths:
        (repo.worktree / path).write_text("shared contents\n", encoding="utf-8")
    _invoke("set-config", "--fmt", FMT)

    _invoke("add", paths[0])
    _assert_catalog(repo, [paths[0]])
    _assert_status(
        repo,
        state=INITIAL_STATE,
        added=[paths[0]],
        untracked=[paths[1]],
    )
    _invoke("add", paths[1])

    saved = _assert_catalog(repo, paths)
    versions = saved.state.data.set_index("path")["sha1"].to_dict()
    assert versions[paths[0]] == versions[paths[1]]
    _assert_status(repo, state=INITIAL_STATE, added=paths)
    _invoke("commit", "-m", "Track both equal-content paths")
    _assert_status(repo)

    (repo.worktree / paths[0]).write_text("changed contents\n", encoding="utf-8")
    _invoke("add", paths[0])

    saved = _assert_catalog(repo, paths)
    changed = saved.state.data.set_index("path")["sha1"].to_dict()
    assert changed[paths[0]] != versions[paths[0]]
    assert changed[paths[1]] == versions[paths[1]]
    _assert_status(repo, state=["data.tsv"], modified=[paths[0]])