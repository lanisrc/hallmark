# Copyright 2026 the Hallmark Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
import requests
import yaml
import importlib
import pytest
import pandas as pd
from pathlib        import Path
from click.testing  import CliRunner
from git import Repo as GitRepo
from git.exc import GitError
from types import SimpleNamespace

from hallmark import ParaFrame, Repo
from hallmark.cli import hallmark
from hallmark.downloader import DownloadError
from hallmark.download_plan import DownloadItem, DownloadPlan
from hallmark.helper_functions import chdir

cli_module = importlib.import_module("hallmark.cli")

# files to create for testing, with a variety of a and i values to test regex encoding
files = [f"a{a}_i{i}.h5"
         for a in [0, 0.75, 0.975]
         for i in [0, 30, 60, 90]]


### helper functions ###

def _install_repo(monkeypatch, worktree=Path("worktree")):
    """
    Install a fake hallmark repository for testing, monkeypatching the Repo class
    to return a SimpleNamespace with the given worktree.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        worktree: Path to the worktree directory for the fake repository.
    Returns:
        A SimpleNamespace object representing the fake repository, with a 'worktree'
        attribute set to the given worktree path.
    """
    repo = SimpleNamespace(
        worktree=worktree,
        plan_download=lambda output=None, **kwargs: _download_plan(
            0, output or worktree or "downloads"))
    monkeypatch.setattr(cli_module, "Repo", lambda path: repo)
    return repo


def _download_plan(count, output=Path("worktree")):
    """Create a download plan for CLI display and approval tests."""
    return DownloadPlan(tuple(DownloadItem(Path(f"file-{index:03d}.dat"),
                                           size_bytes=8 if index == 0 else None)
                              for index in range(count)),
                        "https://example.test/data/", Path(output))


def _local_cli_catalog(path):
    """Create a local catalog for a small, simulated HTTP dataset."""
    repo = Repo.init(path)
    repo.state.config = {
        "data": [{"db": "data.tsv"}],
        "remote": {"name": "origin", "url": "https://example.test/data/"}}
    repo.state.data = pd.DataFrame([
        {"path": "tiny.fits", "size_bytes": 4},
        {"path": "other.txt", "size_bytes": 5}])
    repo.dothm.dump(repo.state)
    repo.dothm.index.commit("Catalog two remote files")
    return repo


def parse(result):
    """
    Parse the output of a CLI command that lists files, returning the count and
    a list of the file names.
    Args:
        result: The result object returned by CliRunner.invoke().
    Returns:
        A tuple containing the count of files and a list of file names.
    """
    output = result.output.split('\n')[1:-1]
    return len(output), [f.strip(' ') for f in output]


def test_group_reports_repository_open_error(monkeypatch):
    """
    Test that the CLI reports an error when it fails to open a hallmark repository.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    Raises:
        GitError: Simulated error to test error handling in the CLI.
    """
    def fail_repo(path):
        """ Raise a GitError to simulate a failure to open a hallmark repository."""
        raise GitError("not a hallmark repository")
    monkeypatch.setattr(cli_module, "Repo", fail_repo)
    result = CliRunner().invoke(hallmark, ["info"])

    assert result.exit_code != 0, f"Expected non-zero exit code, got {result.exit_code}"
    assert "Failed to open hallmark repository" in result.output, \
        f"Expected error message in output, got: {result.output}"


def test_cli_init_reports_git_error_with_prefix(monkeypatch):
    """
    Test that the hallmark CLI 'init' command translates a GitError raised by
    Repo.init into a prefixed, clean error message.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    def fail_init(path):
        """Raise a GitError to simulate a failure during repository initialization."""
        raise GitError("simulated failure")
    monkeypatch.setattr("hallmark.cli.Repo.init", fail_init)
    result = CliRunner().invoke(hallmark, ["init", "repo"])

    assert result.exit_code != 0, \
        f"Expected non-zero exit code for init failure, got {result.exit_code}"
    assert 'Failed to initialize hallmark repository at "repo"' in result.output, \
        f"Expected prefixed error message, got: {result.output}"


def test_cli_info_shows_dothm_and_worktree_paths():
    """
    Test that the hallmark CLI 'info' command displays the .hm and worktree paths
    for the current repository.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            result = runner.invoke(hallmark, ["info"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for info, got {result.exit_code}"
            assert "dot-hallmark repo:" in result.output, \
                f"Expected dot-hallmark repo line in output, got: {result.output}"
            assert "hallmark worktree:" in result.output, \
                f"Expected hallmark worktree line in output, got: {result.output}"
            assert str(Path(".hm").resolve()) in result.output, \
                f"Expected resolved .hm path in output, got: {result.output}"


def test_cli():
    """
    Test the hallmark CLI commands for basic functionality.
    This test initializes a hallmark repository, adds files, commits changes,
    and tests basic CLI functionality.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(hallmark, ["init", "repo"])
        assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}"

        with chdir("repo"):
            assert Path(".hm").is_dir(), "Expected .hm directory to exist after init"

            for file in files:
                Path(file).write_text("test\n", encoding="utf-8")
            result = runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for add, got {result.exit_code}"

            c, ls = parse(result)
            assert c == 12, f"Expected 12 files, got {c}"
            assert sorted(ls) == sorted(files), \
                f"Expected files {sorted(files)}, got {sorted(ls)}"

            result = runner.invoke(hallmark, ["commit", "-m", "Commit test"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for commit, got {result.exit_code}"
            assert "Committed staged state changes." in result.output

            result = runner.invoke(hallmark, ["checkout", "experiment"])
            result = runner.invoke(hallmark, ["checkout", "experiment"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for checkout, got {result.exit_code}"
            assert 'Switched to branch "experiment".' in result.output, \
                f"Expected branch switch message, got: {result.output}"

            Path("a0_i0.h5").unlink()
            Path("a0_i30.h5").unlink()
            Path("a0_i60.h5").unlink()
            Path("a0_i90.h5").unlink()
            Path("a0.75_i0.h5").unlink()
            Path("a0.75_i30.h5").unlink()
            Path("a0.75_i60.h5").unlink()
            Path("a0.75_i90.h5").unlink()
            Path("a0.975_i0.h5").unlink()
            Path("a0.975_i30.h5").unlink()
            Path("a0.975_i60.h5").unlink()
            Path("a0.975_i90.h5").unlink()
            Path("a1_i45.h5").write_text("a1_i45.h5\n", encoding="utf-8")
            result = runner.invoke(hallmark, ["add", "."])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for add, got {result.exit_code}"
            result = runner.invoke(hallmark, ["commit", "-m", "Commit experiment"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for commit, got {result.exit_code}"

            result = runner.invoke(hallmark, ["checkout", "main"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for checkout, got {result.exit_code}"
            assert not Path("a1_i45.h5").exists(), \
                "Expected a1_i45.h5 to be removed after checkout to main"

            Path("a0_i0.h5").write_text("dirty\n", encoding="utf-8")
            result = runner.invoke(hallmark, ["checkout", "experiment"])
            assert result.exit_code != 0, f"Expected non-zero exit code for checkout \
                with uncommitted changes, got {result.exit_code}"
            assert "has uncommitted changes" in result.output, \
                f"Expected uncommitted changes message, got: {result.output}"


def test_cli_add_dot_and_explicit_paths():
    """
    Test the hallmark CLI 'add' command with '.' and explicit paths.
    This test initializes a hallmark repository, adds files using both '.'
    and explicit paths, and verifies the behavior of the add command.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            Path("a0_i0.h5").write_text("a0_i0.h5\n", encoding="utf-8")
            Path("a0_i30.h5").write_text("a0_i30.h5\n", encoding="utf-8")

            result = runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for add, got {result.exit_code}"

            Path("a0_i0.h5").unlink()
            Path("a1_i45.h5").write_text("a1_i45.h5\n", encoding="utf-8")
            result = runner.invoke(hallmark, ["add", "."])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for add with '.', got {result.exit_code}"

            manifest = Path(".hm/data.tsv").read_text(encoding="utf-8")
            assert "a0_i0.h5" not in manifest, \
                "Expected a0_i0.h5 to be removed from manifest"
            assert "\t1\t45" in manifest or ",1,45" not in manifest, \
                "Expected encoding information for a1_i45.h5 in manifest"

            Path("top1.h5").write_text("top1.h5\n", encoding="utf-8")
            Path("top2.h5").write_text("top2.h5\n", encoding="utf-8")
            result = runner.invoke(hallmark, ["add", "top1.h5", "top2.h5"])
            assert result.exit_code != 0, f"Expected non-zero exit code for add with \
                explicit paths, got {result.exit_code}"
            assert "explicit path add is not supported" in result.output, \
                f"Expected explicit path add error message, got: {result.output}"


def test_cli_add_regex_flag(monkeypatch):
    """
    Test the hallmark CLI 'add' command with the '--regex' flag. This test initializes
    a hallmark repository, monkeypatches the Repo.add method, and verifies that the
    add command is called with the correct arguments when using '--regex'.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            called = {}

            def fake_add(self, fmt, encoding=False, *, dry_run=False):
                """Fake add method to capture arguments passed to Repo.add."""
                called["fmt"] = fmt
                called["encoding"] = encoding
                called["dry_run"] = dry_run
                return ParaFrame([{"path": "am0.5_i30.h5"}])
            monkeypatch.setattr("hallmark.cli.Repo.add", fake_add)
            result = runner.invoke(hallmark, ["add", "--regex", "."])

            assert result.exit_code == 0, f"Expected exit code 0 for add with \
                '--regex', got {result.exit_code}"
            assert called == {"fmt": ".", "encoding": True, "dry_run": False}, \
                f"Expected add to be called with fmt='.' and encoding=True, \
                got {called}"


def test_cli_status():
    """
    Test the hallmark CLI 'status' command. This test initializes a hallmark repository,
    adds files, commits changes, modifies files, and verifies that the status command
    correctly reports the state of the repository, including modified, deleted, and
    untracked files.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            Path("a0_i0.h5").write_text("a0_i0.h5\n", encoding="utf-8")
            Path("a0_i30.h5").write_text("a0_i30.h5\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "Commit test"])
            Path("a0_i0.h5").write_text("changed\n", encoding="utf-8")
            Path("a0_i30.h5").unlink()
            Path("untracked.h5").write_text("untracked\n", encoding="utf-8")
            result = runner.invoke(hallmark, ["status"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for status, got {result.exit_code}"
            assert "On branch main" in result.output, \
                f"Expected branch information in status output, got: {result.output}"
            assert "Changes not staged for commit:" in result.output, f"Expected \
                changes not staged message in status output, got: {result.output}"
            assert "modified:   a0_i0.h5" in result.output, f"Expected modified file \
                a0_i0.h5 in status output, got: {result.output}"
            assert "deleted:   a0_i30.h5" in result.output, f"Expected deleted file \
                a0_i30.h5 in status output, got: {result.output}"
            assert "Untracked files:" in result.output, f"Expected untracked files \
                message in status output, got: {result.output}"
            assert "untracked.h5" in result.output, f"Expected untracked file \
                untracked.h5 in status output, got: {result.output}"


def test_cli_set_config_and_add_dot():
    """
    Test the hallmark CLI 'set-config' command and subsequent 'add' command.
    This test initializes a hallmark repository, sets configuration options, adds files,
    and verifies that the configuration is correctly updated and that the files are
    added to the repository.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            result = runner.invoke(
                hallmark,
                [
                    "set-config",
                    "--fmt", "b{a}_i{i}.h5",
                    "--remote-name", "origin",
                    "--remote-url", "https://example.com/path",
                    "--encoding", r"aspin=m([0-9]+(\.[0-9]+)?|\.[0-9]+)",
                ],
            )
            assert result.exit_code == 0, f"Expected exit code 0 for set-config, \
                got {result.exit_code}"
            assert "Updated hallmark config." in result.output, \
                f"Expected config update message, got: {result.output}"

            Path("b0_i0.h5").write_text("b0_i0.h5\n", encoding="utf-8")
            Path("b0_i30.h5").write_text("b0_i30.h5\n", encoding="utf-8")
            result = runner.invoke(hallmark, ["add", "."])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for add, got {result.exit_code}"

            manifest = Path(".hm/data.tsv").read_text(encoding="utf-8")
            assert "sha1\ta\ti" in manifest, "Expected encoding information in manifest"

            config = Path(".hm/config.yml").read_text(encoding="utf-8")
            assert "fmt: b{a}_i{i}.h5" in config, "Expected fmt entry in config"
            assert "name: origin" in config, "Expected remote name in config"
            assert "url: https://example.com/path" in config, \
                "Expected remote URL in config"
            assert r"aspin: m([0-9]+(\.[0-9]+)?|\.[0-9]+)" in config, \
                "Expected encoding regex in config"


def test_cli_set_config_validates_explicit_empty_format():
    """
    Test that the hallmark CLI 'set-config' command rejects an explicit empty format.
    This test initializes a hallmark repository and attempts to set an empty format.
    It verifies that the command fails and provides an appropriate error message.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(hallmark, ["init", "repo"]).exit_code == 0, \
            "Failed to initialize hallmark repository for testing"
        original = Path.cwd()
        try:
            os.chdir("repo")
            result = runner.invoke(hallmark, ["set-config", "--fmt", ""])
        finally:
            os.chdir(original)

    assert result.exit_code != 0, f"Expected non-zero exit code for empty format, \
        got {result.exit_code}"
    assert "fmt must be a non-empty string" in result.output, \
        f"Expected error message about empty format, got: {result.output}"
    assert "No config changes requested" not in result.output, \
        f"Expected error message about empty format, got: {result.output}"


def test_cli_status_shows_staged_state_after_set_config():
    """
    Test that the hallmark CLI 'status' command shows staged state changes after
    running 'set-config'.
    This test initializes a hallmark repository, sets configuration options, and
    verifies that the status command reflects the staged changes.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            result = runner.invoke(hallmark, ["set-config", "--fmt", "b{a}_i{i}.h5"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for set-config, got {result.exit_code}"

            result = runner.invoke(hallmark, ["status"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for status, got {result.exit_code}"
            assert "Changes to be committed:" in result.output, \
               f"Expected staged changes message in status output, got: {result.output}"
            assert "state:   config.yml" in result.output, \
                f"Expected config.yml in staged changes, got: {result.output}"
            assert "nothing to commit, working tree clean" not in result.output, \
                f"Expected working tree not clean, got: {result.output}"

            result = runner.invoke(hallmark, ["commit", "-m", "config only"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for commit, got {result.exit_code}"
            assert "Committed staged state changes." in result.output, \
                f"Expected commit message, got: {result.output}"


def test_cli_set_config_rejects_malformed_encoding():
    """
    Test that the hallmark CLI 'set-config' command rejects malformed encoding values.
    This test initializes a hallmark repository and attempts to set a malformed encoding
    value, verifying that the command fails and provides an appropriate error message.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            result = runner.invoke(hallmark, ["set-config", "--encoding", "aspin"])

            assert result.exit_code != 0, f"Expected non-zero exit code for malformed \
                encoding, got {result.exit_code}"
            assert "FIELD=REGEX" in result.output, \
                f"Expected FIELD=REGEX error message, got: {result.output}"


def test_cli_log():
    """
    Test the hallmark CLI 'log' command. This test initializes a hallmark repository,
    adds files, commits changes, and verifies that the log command shows the correct
    commit history.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            result = runner.invoke(hallmark, ["log"])
            assert result.exit_code == 0, \
                f"Expected exit code 0 for log, got {result.exit_code}"
            expected = GitRepo(".hm").git.log()
            assert result.output.strip() == expected.strip(), \
                f"Expected log output to match git log, got: {result.output.strip()}"

            Path("a0_i0.h5").write_text("a0_i0.h5\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "add first file"])
            Path("a0_i30.h5").write_text("a0_i30.h5\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "."])
            runner.invoke(hallmark, ["commit", "-m", "add second file"])
            result = runner.invoke(hallmark, ["log"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for log, got {result.exit_code}"
            expected = GitRepo(".hm").git.log()
            assert result.output.strip() == expected.strip(), \
                f"Expected log output to match git log, got: {result.output.strip()}"


def test_cli_branch_lists_local_branches_and_marks_current():
    """
    Test the hallmark CLI 'branch' command. This test initializes a hallmark repository,
    creates a new branch, and verifies that the branch command lists local branches
    and marks the current branch.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            Path("a0_i0.h5").write_text("a0_i0.h5\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "add first file"])
            runner.invoke(hallmark, ["checkout", "experiment"])
            result = runner.invoke(hallmark, ["branch"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for branch, got {result.exit_code}"
            assert "  main" in result.output, \
                f"Expected 'main' branch in output, got: {result.output}"
            assert "* experiment" in result.output, \
                f"Expected '* experiment' to mark current branch, got: {result.output}"


def test_cli_help_lists_commands():
    """
    Test that the hallmark CLI '--help' command lists all available commands.
    This test invokes the CLI with the '--help' flag and verifies that the output
    includes the expected commands.
    """
    result = CliRunner().invoke(hallmark, ["--help"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for help, got {result.exit_code}"
    assert "add" in result.output, \
        f"Expected 'add' command in help output, got: {result.output}"
    assert "branch" in result.output, \
        f"Expected 'branch' command in help output, got: {result.output}"
    assert "checkout" in result.output, \
        f"Expected 'checkout' command in help output, got: {result.output}"
    assert "clone" in result.output, \
        f"Expected 'clone' command in help output, got: {result.output}"
    assert "commit" in result.output, \
        f"Expected 'commit' command in help output, got: {result.output}"
    assert "info" in result.output, \
        f"Expected 'info' command in help output, got: {result.output}"
    assert "init" in result.output, \
        f"Expected 'init' command in help output, got: {result.output}"
    assert "log" in result.output, \
        f"Expected 'log' command in help output, got: {result.output}"
    assert "set-config" in result.output, \
        f"Expected 'set-config' command in help output, got: {result.output}"
    assert "status" in result.output, \
        f"Expected 'status' command in help output, got: {result.output}"
    assert "build" in result.output, \
        f"Expected 'build' command in help output, got: {result.output}"
    assert "download" in result.output, \
        f"Expected 'download' command in help output, got: {result.output}"


### clone tests ###

def test_clone_existing_destination_fails_with_plain_git_stderr():
    """
    Test that the hallmark CLI 'clone' command reports a plain git error message
    when the destination directory already exists and is not empty.
    This test initializes a hallmark repository, creates a non-empty target directory,
    and verifies that the clone command fails with the expected error message.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        source = Path("source")
        result = runner.invoke(hallmark, ["init", str(source)])
        assert result.exit_code == 0, \
            f"Expected exit code 0 for init, got {result.exit_code}"

        target = Path("repo3")
        target.mkdir(parents=True)
        (target / "placeholder.txt").write_text("test\n", encoding="utf-8")
        result = runner.invoke(
            hallmark,
            ["clone", str(source / ".hm"), str(target)])

        assert result.exit_code != 0, \
            f"Expected non-zero exit code for clone, got {result.exit_code}"
        assert not result.output.startswith("Error:"), \
            f"Expected no 'Error:' prefix in output, got: {result.output}"
        assert "stderr:" not in result.output, \
            f"Expected no 'stderr:' in output, got: {result.output}"
        assert "Clone failed:" not in result.output, \
            f"Expected no 'Clone failed:' in output, got: {result.output}"
        assert (
            result.output.strip()
            == "fatal: destination path 'repo3' already exists and "
            "is not empty."), \
            f"Expected git error message, got: {result.output.strip()}"


def test_cli_commit_reports_empty_message_cleanly():
    """
    Test that the hallmark CLI 'commit' command reports an error when an empty commit
    message is provided. This test initializes a repository in an isolated filesystem,
    changes the working directory to the repository, and verifies that the commit
    command fails with the expected error message when an empty commit message is given.
    """
    runner = CliRunner()

    with runner.isolated_filesystem():
        assert runner.invoke(hallmark, ["init", "repo"]).exit_code == 0, \
            "Failed to initialize repository for commit test"
        original = Path.cwd()
        try:
            os.chdir("repo")
            result = runner.invoke(hallmark, ["commit", "-m", "   "])
        finally:
            os.chdir(original)
    assert result.exit_code != 0, f"Expected non-zero exit code for commit with empty \
        message, got {result.exit_code}"
    assert "commit message must be a non-empty string" in (result.output), \
        f"Expected error message about empty commit message, got: {result.output}"


def test_clone_copies_committed_hallmark_state():
    """
    Test that the hallmark CLI 'clone' command copies the committed hallmark state
    from the source repository to the target directory.
    This test initializes a hallmark repository, commits the initial state, and verifies
    that the clone command copies the state to the target directory.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        source = Path("source")
        result = runner.invoke(hallmark, ["init", str(source)])
        assert result.exit_code == 0, \
            f"Expected exit code 0 for init, got {result.exit_code}"

        GitRepo(str(source / ".hm")).index.commit("commit initial hallmark state")
        result = runner.invoke(
            hallmark,
            ["clone", str(source / ".hm"), "target"])

        assert result.exit_code == 0, \
            f"Expected exit code 0 for clone, got {result.exit_code}"
        assert 'Successfully cloned to "target"' in result.output, \
            f"Expected success message in output, got: {result.output}"
        assert Path("target/.hm").is_dir(), \
            "Expected .hm directory to exist in target after clone"
        assert Path("target/.hm/config.yml").exists(), \
            "Expected config.yml to exist in target after clone"
        assert Path("target/.hm/meta.yml").exists(), \
            "Expected meta.yml to exist in target after clone"
        assert Path("target/.hm/data.tsv").exists(), \
            "Expected data.tsv to exist in target after clone"


def test_clone_rejects_removed_download_option(tmp_path):
    result = CliRunner().invoke(hallmark, [
        "clone", "source", str(tmp_path / "target"), "--download"])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert not (tmp_path / "target").exists()


def test_clone_cli_skips_download_when_no_remote_files(monkeypatch, tmp_path):
    repo = Repo.init(tmp_path / "empty")

    class FakeRepo:
        lwpaths = Repo.lwpaths

        @staticmethod
        def clone(url, path, **kwargs):
            return repo

    monkeypatch.setattr(cli_module, "Repo", FakeRepo)
    result = CliRunner().invoke(hallmark, [
        "clone", "source", "target"])
    assert result.exit_code == 0, result.output
    assert 'Successfully cloned to "target"' in result.output
    assert "Download these files?" not in result.output


@pytest.mark.parametrize("max_workers", [0, -1, 4])
def test_clone_rejects_removed_max_workers(max_workers):
    result = CliRunner().invoke(hallmark, [
        "clone", "source", "target", "--max-workers", str(max_workers)])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_build_cli_parses_fmts_remotes_and_db_suffix(monkeypatch):
    """
    Test that the hallmark CLI 'build' command correctly parses format entries,
    remote entries, and database suffixes from the command line arguments.
    This test monkeypatches the build_repo function to capture the arguments passed
    to it and verifies that the parsed values match the expected values.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    captured = {}
    def fake_build_repo(**kwargs):
        """Fake build_repo function to capture arguments passed to it."""
        captured.update(kwargs)
    monkeypatch.setattr(cli_module, "build_repo", fake_build_repo)
    result = CliRunner().invoke(
        hallmark,
        [
            "build",
            "repositories",
            "EHTC_TEST",
            "--fmt",
            "images/{source}.fits=science",
            "--fmt",
            "README.{format}=readme.tsv",
            "--remote",
            "origin=https://origin.test/data",
            "--remote",
            "mirror"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for build, got {result.exit_code}"
    assert captured == {
        "repo_path": Path("repositories/EHTC_TEST.hm"),
        "dataset_name": "EHTC_TEST",
        "fmt_entries": [
            {"fmt": "images/{source}.fits", "db": "science.tsv"},
            {"fmt": "README.{format}", "db": "readme.tsv"}],
        "config_file": None,
        "remotes": [
            {"name": "origin", "url": "https://origin.test/data"},
            {"name": "mirror"}],
        "overwrite": False}, \
        f"Expected captured arguments to match expected values, got: {captured}"
    assert "Successfully built hallmark repository" in result.output, \
        f"Expected success message in output, got: {result.output}"


def test_build_cli_rejects_conflicting_format_sources(monkeypatch, tmp_path):
    """
    Test that the hallmark CLI 'build' command rejects conflicting format sources
    when both --config-file and --fmt are provided. This test creates a temporary
    config file and verifies that the build command fails with expected error message.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
    """
    config_path = tmp_path / "config.yml"
    config_path.write_text("data: []\n", encoding="utf-8")
    result = CliRunner().invoke(
        hallmark,
        [
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--config-file",
            str(config_path),
            "--fmt",
            "{name}.fits=data"])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with \
        conflicting format sources, got {result.exit_code}"
    assert "Use only one of --config-file or --fmt" in result.output, \
        f"Expected error message about conflicting format sources, got: {result.output}"


def test_build_cli_rejects_malformed_fmt():
    """
    Test that the hallmark CLI 'build' command rejects malformed format entries
    when the --fmt argument does not include a database suffix. This test verifies
    that the build command fails with expected error message when a malformed format
    entry is provided.
    """
    result = CliRunner().invoke(
        hallmark,
        ["build", "repositories", "EHTC_TEST", "--fmt", "missing-db"])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with \
        malformed fmt, got {result.exit_code}"
    assert "--fmt values must use FMT=DB" in result.output, \
        f"Expected error message about malformed fmt, got: {result.output}"


def test_build_cli_forwards_config_file_to_builder(monkeypatch, tmp_path):
    """
    Test that the hallmark CLI 'build' command forwards the --config-file argument
    to the build_repo function. This test creates a temporary config file, monkeypatches
    the build_repo function to capture the arguments passed to it, and verifies that
    the config_file argument is correctly forwarded.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
    """
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump({
                "data": [
                    {"file": "README.md"},
                    {"fmt": "{name}.fits", "db": "science.tsv"},],
                "remote": [{"name": "origin", "url": "https://example.test"}]}
            ),encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_repo", lambda **kwargs: captured.update(kwargs))
    result = CliRunner().invoke(
        hallmark, [
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--config-file",
            str(config_path)])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for build, got {result.exit_code}"
    assert captured["config_file"] == str(config_path), \
        f"Expected config_file to be forwarded, got: {captured.get('config_file')}"
    assert captured["fmt_entries"] is None, \
        f"Expected fmt_entries to remain None, got: {captured['fmt_entries']}"
    assert captured["remotes"] is None, f"Expected remotes to remain None when \
        --remote is absent, got: {captured['remotes']}"


def test_build_cli_remote_option_overrides_config_remotes(monkeypatch, tmp_path):
    """
    Test that the hallmark CLI 'build' command allows the --remote option to override
    remote entries specified in the configuration file. This test creates a temporary
    config file with a remote entry, monkeypatches the build_repo function to capture
    the arguments passed to it, and verifies that the parsed values match the expected
    values when the --remote option is provided.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
    """
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump({
                "data": [{"fmt": "{name}.fits", "db": "science.tsv"}],
                "remote": [{"name": "origin", "url": "https://old.test"}]}
            ),encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_repo", lambda **kwargs: captured.update(kwargs))
    result = CliRunner().invoke(
        hallmark, [
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--config-file",
            str(config_path),
            "--remote",
            "mirror=https://new.test"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for build, got {result.exit_code}"
    assert captured["config_file"] == str(config_path), \
        f"Expected config_file to be forwarded, got: {captured.get('config_file')}"
    assert captured["remotes"] == [
        {"name": "mirror", "url": "https://new.test"}], f"Expected remotes to be \
            overridden by --remote option, got: {captured['remotes']}"


def test_build_cli_rejects_config_without_fmts(tmp_path):
    """
    Test that the hallmark CLI 'build' command rejects a configuration file that does
    not contain any format entries. This test creates a temporary config file without
    any format entries and verifies that the build command fails with the expected
    error message.
    Args:
        tmp_path: pytest fixture for creating a temporary directory.
    """
    config_path = tmp_path / "config.yml"
    config_path.write_text("data:\n- file: README.md\n", encoding="utf-8")
    result = CliRunner().invoke(
        hallmark,
        [
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--config-file",
            str(config_path)])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with config \
        without fmt, got {result.exit_code}"
    assert "No fmt entries found" in result.output, \
        f"Expected error message about missing fmt entries, got: {result.output}"


@pytest.mark.parametrize(
    "error, expected",
    [
        (RuntimeError("runtime failure"), "runtime failure"),
        (ValueError("value failure"), "value failure"),
        (FileNotFoundError("missing file"), "missing file"),
        (GitError("git failure"), "git failure"),
        (
            requests.ConnectionError("offline"),
            "Failed to reach dataset 'EHTC_TEST': offline")])
def test_build_cli_translates_builder_errors(monkeypatch, error, expected):
    """
    Test that the hallmark CLI 'build' command translates various builder errors into
    user-friendly error messages. This test monkeypatches the build_repo function to
    raise different types of errors and verifies that the build command fails with the
    expected error message.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        error: The error to be raised by the build_repo function.
        expected: The expected error message to be found in the build command output.
    """
    def fail_build(**kwargs):
        """Fake build_repo function that raises the specified error."""
        raise error
    monkeypatch.setattr(cli_module, "build_repo", fail_build)
    result = CliRunner().invoke(
        hallmark,
        ["build", "repositories", "EHTC_TEST", "--fmt", "{name}.fits=data"])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with error \
        {error}, got {result.exit_code}"
    assert expected in result.output, \
        f"Expected error message '{expected}' in output, got: {result.output}"


def test_build_cli_forwards_overwrite(monkeypatch, tmp_path):
    """
    Test that the hallmark CLI 'build' command forwards the --overwrite option to the
    build_repo function. This test monkeypatches the build_repo function to capture the
    arguments passed to it and verifies that the overwrite argument is set to True when
    the --overwrite option is provided.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
    """
    captured = {}
    def fake_build_repo(**kwargs):
        """Fake build_repo function to capture arguments passed to it."""
        captured.update(kwargs)
    monkeypatch.setattr("hallmark.cli.build_repo", fake_build_repo)
    runner = CliRunner()
    result = runner.invoke(
        hallmark,[
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--fmt",
            "data_{number}.txt=data.tsv",
            "--overwrite"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for build with overwrite, got {result.exit_code}"
    assert captured["overwrite"] is True, \
        f"Expected overwrite to be True, got {captured['overwrite']}"


@pytest.mark.parametrize(
    "dataset_name",[
        "../escape",
        "nested/dataset",
        "/absolute",
        "C:/outside",
        r"C:\outside",
        "",
        "."])
def test_build_cli_rejects_unsafe_dataset_name(monkeypatch, tmp_path, dataset_name):
    """
    Test that the hallmark CLI 'build' command rejects unsafe dataset names that could
    lead to directory traversal or other security issues. This test monkeypatches the
    build_repo function to ensure it is not called and verifies that the build command
    fails with the expected error message when an unsafe dataset name is provided.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
        dataset_name: The unsafe dataset name to be tested.
    Raises:
        AssertionError: If the build_repo function is called, which should not happen
        for unsafe dataset names.
    """
    def unexpected_build(**_kwargs):
        """Fake build_repo function that should not be called."""
        raise AssertionError("build_repo should not be called")
    monkeypatch.setattr(cli_module, "build_repo", unexpected_build)
    result = CliRunner().invoke(
        hallmark,[
            "build",
            str(tmp_path),
            dataset_name,
            "--fmt",
            "data_{number}.txt=data.tsv"])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with unsafe \
        dataset name, got {result.exit_code}"
    assert "dataset name" in result.output, \
        f"Expected error message about unsafe dataset name, got: {result.output}"


@pytest.mark.parametrize(
    "db_name",[
        "../../outside",
        "/absolute/data.tsv",
        "nested/data.tsv",
        r"..\outside.tsv",
        "",
        "."])
def test_build_cli_rejects_unsafe_tsv_name(monkeypatch, tmp_path, db_name):
    """
    Test that the hallmark CLI 'build' command rejects unsafe TSV database name that
    could lead to directory traversal or other security issues. This test monkeypatches
    the build_repo function to ensure it is not called and verifies that the build
    command fails with the expected error message when an unsafe TSV name is provided.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
        db_name: The unsafe TSV database name to be tested.
    Raises:
        AssertionError: If the build_repo function is called, which should not happen
        for unsafe TSV names.
    """
    def unexpected_build(**_kwargs):
        """Fake build_repo function that should not be called."""
        raise AssertionError("build_repo should not be called")
    monkeypatch.setattr(cli_module, "build_repo", unexpected_build)
    result = CliRunner().invoke(
        hallmark,[
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--fmt",
            f"data_{{number}}.txt={db_name}"])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with unsafe \
        TSV name, got {result.exit_code}"
    assert "TSV database name" in result.output, \
        f"Expected error message about unsafe TSV name, got: {result.output}"


@pytest.mark.parametrize(
    "contents, expected", [
        ("- first\n- second\n", "YAML document must contain a mapping"),
        ("data: [\n", "while parsing")])
def test_build_cli_reports_invalid_config_file(tmp_path, contents, expected):
    """
    Test that the hallmark CLI 'build' command reports an error when the provided
    configuration file is invalid or malformed. This test creates a temporary config
    file with invalid contents and verifies that the build command fails with the
    expected error message.
    Args:
     tmp_path: pytest fixture for creating a temporary directory.
        contents: The invalid contents to be written to the config file.
    """
    config_path = tmp_path / "config.yml"
    config_path.write_text(contents, encoding="utf-8")
    result = CliRunner().invoke(
        hallmark,
        ["build", str(tmp_path), "EHTC_TEST", "--config-file", str(config_path)])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with invalid \
        config file, got {result.exit_code}"
    assert expected in result.output, \
        f"Expected error message fragment {expected!r}, got: {result.output}"


def test_build_cli_forwards_config_directory_to_builder(monkeypatch, tmp_path):
    """
    Test that the hallmark CLI 'build' command forwards a directory passed via
    --config-file to build_repo unchanged, letting the builder resolve config.yml.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
    """
    config_dir = tmp_path / "source.hm"
    config_dir.mkdir()
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_repo", lambda **kwargs: captured.update(kwargs))
    result = CliRunner().invoke(
        hallmark, [
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--config-file",
            str(config_dir)])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for build, got {result.exit_code}"
    assert captured["config_file"] == str(config_dir), f"Expected config directory path\
          to be forwarded, got: {captured.get('config_file')}"
    assert captured["fmt_entries"] is None, \
        f"Expected fmt_entries to remain None, got: {captured['fmt_entries']}"


def test_build_cli_remote_name_only_with_config_file(monkeypatch, tmp_path):
    """
    Test that --remote NAME (without URL) is forwarded as a named remote when
    --config-file is also provided.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        tmp_path: pytest fixture for creating a temporary directory.
    """
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump(
            {"data": [{"fmt": "{name}.fits", "db": "science.tsv"}]}), encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        cli_module, "build_repo", lambda **kwargs: captured.update(kwargs))
    result = CliRunner().invoke(
        hallmark, [
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--config-file",
            str(config_path),
            "--remote",
            "mirror"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for build, got {result.exit_code}"
    assert captured["config_file"] == str(config_path), \
        f"Expected config_file to be forwarded, got: {captured.get('config_file')}"
    assert captured["remotes"] == [{"name": "mirror"}], \
        f"Expected name-only remote to be forwarded, got: {captured['remotes']}"


def test_build_cli_reports_missing_config_yml_in_directory(tmp_path):
    """
    Test that build reports a clear error when --config-file points to a directory
    that does not contain config.yml.
    Args:
        tmp_path: pytest fixture for creating a temporary directory.
    """
    config_dir = tmp_path / "missing-config.hm"
    config_dir.mkdir()
    result = CliRunner().invoke(
        hallmark, [
            "build",
            str(tmp_path),
            "EHTC_TEST",
            "--config-file",
            str(config_dir)])

    assert result.exit_code != 0, f"Expected non-zero exit code for build with missing \
        config.yml in directory, got {result.exit_code}"
    assert "Config file does not exist" in result.output, \
        f"Expected missing config file error, got: {result.output}"


### download tests ###

@pytest.mark.parametrize(
    "arguments, message",
    [
        (["download"], "Provide file paths, --tsv, --all, or --filter"),
        (["download", "file.dat", "--all"], "--all cannot be combined"),
        (["download", "--tsv", "data", "--all"], "--all cannot be combined")])
def test_download_cli_rejects_invalid_selection_combinations(
    monkeypatch, arguments, message):
    """
    Test that the hallmark CLI 'download' command rejects invalid combinations of
    selection arguments. This test monkeypatches the repository installation and
    verifies that the download command fails with the expected error message for each
    invalid combination of arguments.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
        arguments: The list of command line arguments to be tested.
        message: The expected error message to be found in the download command output.
    """
    _install_repo(monkeypatch)
    result = CliRunner().invoke(hallmark, arguments)

    assert result.exit_code != 0, f"Expected non-zero exit code for download with \
        arguments {arguments}, got {result.exit_code}"
    assert message in result.output, \
        f"Expected error message '{message}' in output, got: {result.output}"


def test_download_cli_requires_output_for_bare_repository(monkeypatch):
    """
    Test that the hallmark CLI 'download' command requires an explicit output path
    when the repository is bare (i.e., has no worktree). This test monkeypatches the
    repository installation to simulate a bare repository and verifies that the download
     command fails with the expected error message when no output path is provided.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    _install_repo(monkeypatch, worktree=None)
    result = CliRunner().invoke(hallmark, ["download", "--all"])

    assert result.exit_code != 0, f"Expected non-zero exit code for download with bare \
        repository, got {result.exit_code}"
    assert "--output is required" in result.output, \
        f"Expected error message about missing --output, got: {result.output}"


def test_download_cli_rejects_nonpositive_worker_count(monkeypatch):
    """
    Test that the hallmark CLI 'download' command rejects non-positive values for the
    --max-workers option. This test monkeypatches the repository installation and
    verifies that the download command fails with the expected error message when a
    non-positive value is provided for --max-workers.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    _install_repo(monkeypatch)
    result = CliRunner().invoke(
        hallmark, ["download", "--all", "--max-workers", "0"])

    assert result.exit_code == 2, f"Expected exit code 2 for download with non-positive\
          max-workers, got {result.exit_code}"
    assert "0 is not in the range" in result.output, \
        f"Expected error message about non-positive max-workers, got: {result.output}"


def test_download_cli_dry_run_limits_preview(monkeypatch):
    repo = _install_repo(monkeypatch)
    plan = _download_plan(23)
    repo.plan_download = lambda *args, **kwargs: plan

    def reject_download(*args, **kwargs):
        raise AssertionError("dry-run must not download")

    repo.download = reject_download
    result = CliRunner().invoke(hallmark, ["download", "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "23 file(s)" in result.output
    assert "8 bytes known; 22 file(s) with unknown size" in result.output
    assert "estimated duration: unknown" in result.output
    assert "Source: https://example.test/data/" in result.output
    assert f"Destination: {plan.output_path}" in result.output
    assert "file-000.dat" in result.output
    assert "file-019.dat" in result.output
    assert "file-020.dat" not in result.output
    assert "... 3 more file(s)" in result.output
    assert "Download these files?" not in result.output


def test_download_cli_reports_empty_selection(monkeypatch):
    _install_repo(monkeypatch)
    result = CliRunner().invoke(hallmark, ["download", "--all"])
    assert result.exit_code == 0, result.output
    assert "Download these files?" not in result.output


def test_download_cli_passes_selection_and_options_to_downloader(monkeypatch):
    repo = _install_repo(monkeypatch)
    plan = _download_plan(1)
    captured = {}

    def fake_plan(output, **kwargs):
        captured["plan"] = (output, kwargs)
        return plan

    def fake_download(actual_plan, **kwargs):
        captured["download"] = (actual_plan, kwargs)
        return {"succeeded": 1, "failed": 0, "total_bytes": 1024 * 1024,
                "errors": []}

    repo.plan_download = fake_plan
    repo.download = fake_download
    result = CliRunner().invoke(hallmark, [
        "download", "nested/file.dat", "--remote", "mirror", "--max-workers", "2",
        "--filter", "**/*.dat"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Successfully downloaded 1 files (1.0 MB)" in result.output
    assert captured["plan"] == (None, {
        "file_paths": ("nested/file.dat",), "tsv_names": (), "all_files": False,
        "filter": ("**/*.dat",), "remote_name": "mirror"})
    assert captured["download"][0] is plan
    assert captured["download"][1] == {
        "max_workers": 2, "progress": True, "approved": True}


def test_download_cli_allows_explicit_output_for_bare_repository(monkeypatch):
    _install_repo(monkeypatch, worktree=None)
    result = CliRunner().invoke(hallmark, [
        "download", "--all", "--output", "downloads", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert f"Destination: {Path('downloads').absolute()}" in result.output


def test_download_cli_converts_selection_errors_to_click_errors(monkeypatch):
    repo = _install_repo(monkeypatch)

    def fail_plan(*args, **kwargs):
        raise DownloadError("bad selection")

    repo.plan_download = fail_plan
    result = CliRunner().invoke(hallmark, ["download", "--tsv", "missing"])
    assert result.exit_code != 0
    assert "Error: bad selection" in result.output


def test_download_cli_reports_only_first_ten_errors(monkeypatch):
    repo = _install_repo(monkeypatch)
    repo.plan_download = lambda *args, **kwargs: _download_plan(14)
    repo.download = lambda *args, **kwargs: {
        "succeeded": 2, "failed": 12, "total_bytes": 0,
        "errors": [f"failure-{index}" for index in range(12)]}
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code != 0
    assert "2 succeeded, 12 failed" in result.output
    assert "failure-0" in result.output
    assert "failure-9" in result.output
    assert "failure-10" not in result.output
    assert "... 2 more error(s)" in result.output
    assert "Failed to download 12 file(s)" in result.output


@pytest.mark.parametrize("count", [1, 100])
@pytest.mark.parametrize("answer", ["n\n", "", "\n"])
@pytest.mark.parametrize("legacy_yes", [False, True])
def test_download_cli_requires_affirmative_approval(
        monkeypatch, count, answer, legacy_yes):
    repo = _install_repo(monkeypatch)
    repo.plan_download = lambda *args, **kwargs: _download_plan(count)

    def reject_download(*args, **kwargs):
        raise AssertionError("rejected or absent approval must not download")

    repo.download = reject_download
    args = ["download", "--all"] + (["--yes"] if legacy_yes else [])
    result = CliRunner().invoke(hallmark, args, input=answer)
    assert result.exit_code != 0
    assert f"{count} file(s)" in result.output
    assert "Download these files? [y/N]" in result.output
    assert "Aborted!" in result.output
    if legacy_yes:
        assert "--yes is deprecated" in result.output


@pytest.mark.parametrize("arguments", [[], ["--no-download"]])
def test_clone_cli_skips_download_without_terminal_or_when_disabled(
        monkeypatch, tmp_path, arguments):
    source = _local_cli_catalog(tmp_path / "source")

    def reject_download(*args, **kwargs):
        raise AssertionError("noninteractive or disabled download must not run")

    monkeypatch.setattr(Repo, "plan_download", reject_download)
    monkeypatch.setattr(Repo, "download", reject_download)
    target = tmp_path / "target"
    result = CliRunner().invoke(hallmark, [
        "clone", str(source.dothm.path), str(target), *arguments])
    assert result.exit_code == 0, result.output
    assert (target / ".hm/data.tsv").is_file()
    assert not (target / "tiny.fits").exists()
    assert "Download these files?" not in result.output


def test_init_cli_forwards_discovery_options(monkeypatch, tmp_path):
    captured = {}

    def initialize(path, **kwargs):
        captured.update(path=path, **kwargs)

    monkeypatch.setattr(Repo, "init", initialize)
    result = CliRunner().invoke(hallmark, [
        "init", str(tmp_path / "target"), "--from", "desi", "--release", "dr1",
        "--collection", "redshifts", "--filter", "**/*.fits",
        "--filter", "README*", "--format", "{name}.fits"])
    assert result.exit_code == 0, result.output
    assert captured == {
        "source": "desi", "path": str(tmp_path / "target"), "release": "dr1",
        "collections": ("redshifts",), "filter": ("**/*.fits", "README*"),
        "format": "{name}.fits", "progress": True}


def test_clone_cli_rejects_conflicting_download_aliases():
    result = CliRunner().invoke(hallmark, [
        "clone", "source", "target", "--no-fetch-data"])
    assert result.exit_code != 0
    assert "No such option" in result.output


@pytest.mark.parametrize("during_clone", [False, True])
@pytest.mark.parametrize("answer", ["y\n", "n\n", ""])
def test_cli_downloads_only_approved_selected_payload(
        monkeypatch, tmp_path, during_clone, answer):
    from mock_server import MockServer

    source = _local_cli_catalog(tmp_path / "source")
    server = MockServer("https://example.test/data/")
    server.add_file("tiny.fits", b"fits")
    server.add_file("other.txt", b"notes")
    requests_made = []
    original_get = server.get

    def capture_get(url, **kwargs):
        requests_made.append(url)
        return original_get(url, **kwargs)

    server.get = capture_get
    monkeypatch.setattr(requests, "Session", lambda: server)
    if during_clone:
        target = tmp_path / "target"
        cloned = CliRunner().invoke(hallmark, [
            "clone", str(source.dothm.path), str(target)])
        assert cloned.exit_code == 0, cloned.output
        monkeypatch.chdir(target)
        arguments = ["download", "--filter", "*.fits"]
    else:
        target = source.worktree
        monkeypatch.chdir(target)
        arguments = ["download", "--filter", "*.fits"]
    result = CliRunner().invoke(hallmark, arguments, input=answer)
    assert "1 file(s); 4 bytes" in result.output
    assert "Download these files? [y/N]" in result.output
    assert "Source: https://example.test/data/" in result.output
    if answer == "y\n":
        assert result.exit_code == 0, result.output
        assert requests_made == ["https://example.test/data/tiny.fits"]
        assert (target / "tiny.fits").read_bytes() == b"fits"
    else:
        assert result.exit_code != 0
        assert requests_made == []
        assert not (target / "tiny.fits").exists()
    assert not (target / "other.txt").exists()


@pytest.mark.parametrize("options", ["[]\n", "scalar\n", "options: [\n"])
def test_init_cli_rejects_invalid_backend_options_before_access(
        monkeypatch, tmp_path, options):
    config = tmp_path / "options.yml"
    config.write_text(options)

    def fail(*args, **kwargs):
        raise AssertionError("Invalid options must be rejected before initialization")

    monkeypatch.setattr(Repo, "init", fail)
    result = CliRunner().invoke(hallmark, [
        "init", str(tmp_path / "target"), "--from", "https://example.test/data/",
        "--backend-options", str(config)])
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("arguments", [
    ["--interactive", "--filter", "*.fits"], ["--fmt", "{name}.fits"],
    ["--with-download", "--fmt", "{name:invalid}"],
])
def test_clone_cli_rejects_invalid_selection_before_source_access(
        monkeypatch, arguments):
    def fail(*args, **kwargs):
        raise AssertionError("Invalid selection must fail before cloning")

    monkeypatch.setattr(Repo, "clone", fail)
    result = CliRunner().invoke(hallmark, ["clone", "source", "target", *arguments])
    assert result.exit_code != 0
    assert "Error:" in result.output


def test_init_cli_success_does_not_echo_source_credentials(monkeypatch, tmp_path):
    def initialize(path, **kwargs):
        return SimpleNamespace(worktree=Path(path))

    monkeypatch.setattr(Repo, "init", initialize)
    result = CliRunner().invoke(hallmark, [
        "init", str(tmp_path / "target"), "--from",
        "https://user:secret@example.test/data/?token=private"])
    assert result.exit_code == 0, result.output
    assert "Successfully initialized" in result.output
    assert "secret" not in result.output
    assert "private" not in result.output


@pytest.mark.parametrize("option", ["--remote-backend", "--remote-backend-options"])
def test_set_config_cli_rejects_backend_flags(monkeypatch, tmp_path, option):
    repo = Repo.init(tmp_path / "repo")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["set-config", option, "unused"])
    assert result.exit_code == 2
    assert "No such option" in result.output


@pytest.mark.parametrize("answer", ["y\n", "n\n", ""])
def test_init_cli_downloads_only_after_confirmation(monkeypatch, tmp_path, answer):
    from hallmark.transport import OperationContext
    from hallmark.transport.base import RemoteObjectMissing
    from mock_server import MockServer

    def metadata(context, path):
        if path == "":
            return '<h1>Index of data</h1><a href="a.fits">a.fits</a>'
        raise RemoteObjectMissing("Missing metadata")

    server = MockServer("https://example.test/data/")
    server.add_file("a.fits", b"fits")
    monkeypatch.setattr(OperationContext, "read_text", metadata)
    monkeypatch.setattr(requests, "Session", lambda: server)
    destination = tmp_path / "target"
    result = CliRunner().invoke(hallmark, [
        "init", str(destination), "--from", "https://example.test/data/"])
    assert result.exit_code == 0, result.output
    assert "Download these files?" not in result.output
    monkeypatch.chdir(destination)
    result = CliRunner().invoke(hallmark, ["download", "--all"], input=answer)
    assert "Download these files? [y/N]" in result.output
    assert Repo(destination).state.data["path"].tolist() == ["a.fits"]
    assert (destination / "a.fits").exists() == (answer == "y\n")
    assert (result.exit_code == 0) == (answer == "y\n")
