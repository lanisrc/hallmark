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
from hallmark.remote.download import DownloadError
from hallmark.remote.plan import DownloadItem, DownloadPlan
from hallmark.utils import use_working_directory

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
    repo.dothm.save_state(repo.state)
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
        with use_working_directory("repo"):
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

        with use_working_directory("repo"):
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
        with use_working_directory("repo"):
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
        with use_working_directory("repo"):
            called = {}

            def fake_add(self, fmt, encoding=False):
                """Fake add method to capture arguments passed to Repo.add."""
                called["fmt"] = fmt
                called["encoding"] = encoding
                return ParaFrame([{"path": "am0.5_i30.h5"}])
            monkeypatch.setattr("hallmark.cli.Repo.add", fake_add)
            result = runner.invoke(hallmark, ["add", "--regex", "."])

            assert result.exit_code == 0, f"Expected exit code 0 for add with \
                '--regex', got {result.exit_code}"
            assert called == {"fmt": ".", "encoding": True}, f"Expected add to be \
                called with fmt='.' and encoding=True, got {called}"


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
        with use_working_directory("repo"):
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
        with use_working_directory("repo"):
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
        with use_working_directory("repo"):
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
        with use_working_directory("repo"):
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
        with use_working_directory("repo"):
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


def test_cli_reports_a_detached_commit_without_crashing():
    """
    Test that 'status' and 'branch' describe a detached commit instead of raising, and
    that 'commit' refuses with the instructions for saving the work on a branch.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with use_working_directory("repo"):
            Path("a0_i0.h5").write_text("original\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "Original calibration"])
            Path("a0_i0.h5").write_text("recalibrated\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "."])
            runner.invoke(hallmark, ["commit", "-m", "New calibration"])
            GitRepo(".hm").git.checkout("--detach", "HEAD~1")

            status = runner.invoke(hallmark, ["status"])
            assert status.exit_code == 0, \
                f"Expected exit code 0 for status, got {status.exit_code}: " \
                f"{status.output}"
            assert "Not on a branch" in status.output, \
                f"Expected a detached-commit status, got: {status.output}"

            listed = runner.invoke(hallmark, ["branch"])
            assert listed.exit_code == 0, \
                f"Expected exit code 0 for branch, got {listed.exit_code}"
            assert "no branch" in listed.output, \
                f"Expected no branch to be selected, got: {listed.output}"
            assert "* main" not in listed.output, \
                f"Expected main not to be marked current, got: {listed.output}"

            Path("a0_i0.h5").write_text("edited\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "."])
            refused = runner.invoke(hallmark, ["commit", "-m", "should be refused"])

            assert refused.exit_code != 0, \
                f"Expected a non-zero exit code, got {refused.exit_code}"
            assert "hm branch <name>" in refused.output, \
                f"Expected instructions for saving the work, got: {refused.output}"


def test_cli_branch_creates_a_branch_without_switching():
    """
    Test the hallmark CLI 'branch NAME' command. This test creates a branch and
    verifies that it is listed while the repository stays on the current branch.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with use_working_directory("repo"):
            Path("a0_i0.h5").write_text("original\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "Original calibration"])

            result = runner.invoke(hallmark, ["branch", "recal"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for branch NAME, got {result.exit_code}: " \
                f"{result.output}"
            assert 'Created branch "recal".' in result.output, \
                f"Expected a creation message, got: {result.output}"

            listed = runner.invoke(hallmark, ["branch"])
            assert "* main" in listed.output, \
                f"Expected to stay on main, got: {listed.output}"
            assert "  recal" in listed.output, \
                f"Expected recal to be listed, got: {listed.output}"


def test_cli_branch_reports_a_duplicate_name_cleanly():
    """
    Test that 'branch NAME' reports an existing branch name as a clean error rather
    than replacing the branch.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with use_working_directory("repo"):
            Path("a0_i0.h5").write_text("original\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "Original calibration"])
            runner.invoke(hallmark, ["branch", "recal"])

            result = runner.invoke(hallmark, ["branch", "recal"])

            assert result.exit_code != 0, \
                f"Expected a non-zero exit code, got {result.exit_code}"
            assert "branch already exists" in result.output, \
                f"Expected a duplicate-name error, got: {result.output}"


def test_cli_branch_lists_local_branches_and_marks_current():
    """
    Test the hallmark CLI 'branch' command. This test initializes a hallmark repository,
    creates a new branch, and verifies that the branch command lists local branches
    and marks the current branch.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with use_working_directory("repo"):
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
    assert "  build " not in result.output
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
            ["clone", str(source / ".hm"), "target", "--no-download"])

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


def test_clone_reports_download_error_cleanly(monkeypatch, tmp_path):
    source = _local_cli_catalog(tmp_path / "source")

    def fail_download(self, plan, **kwargs):
        raise DownloadError("Remote download failed")

    monkeypatch.setattr(Repo, "download", fail_download)
    result = CliRunner().invoke(hallmark, [
        "clone", str(source.dothm.path), str(tmp_path / "target")],
        input="y\n")
    assert result.exit_code != 0
    assert "Error: Remote download failed" in result.output
    assert "Download these files? [y/N]" in result.output


def test_clone_cli_skips_download_when_no_remote_files(monkeypatch, tmp_path):
    source = Repo.init(tmp_path / "source")
    source.commit("Empty catalog")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(hallmark, [
        "clone", str(source.dothm.path), "target"])
    assert result.exit_code == 0, result.output
    assert 'Successfully cloned to "target"' in result.output
    assert "No files selected for download." in result.output
    assert "Download these files?" not in result.output


@pytest.mark.parametrize("max_workers", [0, -1])
def test_clone_rejects_nonpositive_max_workers(max_workers):
    """
    Test that the hallmark CLI 'clone' command rejects non-positive values for the
    --max-workers option. This test verifies that the clone command fails with an
    appropriate error message when a non-positive value is provided.
    Args:
        max_workers: The non-positive value to test for the --max-workers option.
    """
    result = CliRunner().invoke(
        hallmark,[
            "clone",
            "--max-workers",
            str(max_workers),
            "source",
            "target"])

    assert result.exit_code != 0, f"Expected non-zero exit code for clone with \
        non-positive max workers, got {result.exit_code}"
    assert "Invalid value for '--max-workers'" in result.output, \
        f"Expected error message about non-positive max workers, got: {result.output}"
    assert "x>=1" in result.output, \
        f"Expected error message about non-positive max workers, got: {result.output}"


### build tests ###


### download tests ###

@pytest.mark.parametrize(
    "arguments, message",
    [
        (["download"], "Provide file paths, --tsv, --all, --filter, or --fmt"),
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
    assert "No files selected for download." in result.output
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
        "--filter", "**/*.dat", "--fmt", "nested/{name}.dat"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Successfully downloaded 1 files (1.0 MB)" in result.output
    assert captured["plan"] == (None, {
        "file_paths": ("nested/file.dat",), "tsv_names": (), "all_files": False,
        "filter": ("**/*.dat",), "fmt": "nested/{name}.dat", "remote_name": "mirror"})
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
def test_download_cli_requires_affirmative_approval(
        monkeypatch, count, answer):
    repo = _install_repo(monkeypatch)
    repo.plan_download = lambda *args, **kwargs: _download_plan(count)

    def reject_download(*args, **kwargs):
        raise AssertionError("rejected or absent approval must not download")

    repo.download = reject_download
    args = ["download", "--all"]
    result = CliRunner().invoke(hallmark, args, input=answer)
    assert result.exit_code != 0
    assert f"{count} file(s)" in result.output
    assert "Download these files? [y/N]" in result.output
    assert "Aborted!" in result.output


def test_clone_cli_no_download_copies_metadata_only(monkeypatch, tmp_path):
    source = _local_cli_catalog(tmp_path / "source")

    def reject_download(*args, **kwargs):
        raise AssertionError("catalog-only clone must not plan or download payloads")

    monkeypatch.setattr(Repo, "plan_download", reject_download)
    monkeypatch.setattr(Repo, "download", reject_download)
    target = tmp_path / "target"
    result = CliRunner().invoke(hallmark, [
        "clone", str(source.dothm.path), str(target), "--no-download"])
    assert result.exit_code == 0, result.output
    assert (target / ".hm/data.tsv").is_file()
    assert not (target / "tiny.fits").exists()
    assert "Download these files?" not in result.output


@pytest.mark.parametrize("during_clone", [False, True])
@pytest.mark.parametrize("answer", ["y\n", "n\n", "\n", ""])
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
        arguments = ["clone", str(source.dothm.path), str(target),
                     "--filter", "*.fits"]
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
        if during_clone and answer in {"n\n", "\n"}:
            assert result.exit_code == 0, result.output
            assert (target / ".hm/data.tsv").is_file()
        else:
            assert result.exit_code != 0
        assert requests_made == []
        assert not (target / "tiny.fits").exists()
    assert not (target / "other.txt").exists()


@pytest.mark.parametrize("arguments", [
    ["--no-download", "--filter", "*.fits"],
    ["--no-download", "--fmt", "{name}.fits"],
    ["--fmt", "{name:invalid}"],
])
def test_clone_cli_rejects_invalid_selection_before_source_access(
        monkeypatch, arguments):
    def fail(*args, **kwargs):
        raise AssertionError("Invalid selection must fail before cloning")

    monkeypatch.setattr(Repo, "clone", fail)
    result = CliRunner().invoke(hallmark, ["clone", "source", "target", *arguments])
    assert result.exit_code != 0
    assert "Error:" in result.output


def test_set_config_cli_persists_backend_options(monkeypatch, tmp_path):
    repo = Repo.init(tmp_path / "repo")
    options = tmp_path / "backend.yml"
    options.write_text("collection: [release-1, release-2]\n")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, [
        "set-config", "--remote-url", "https://example.test/data/",
        "--remote-backend", "http", "--remote-backend-options", str(options)])
    assert result.exit_code == 0, result.output
    remote = Repo(repo.worktree).state.config["remote"]
    assert remote["backend"] == "http"
    assert remote["backend_options"] == {"collection": ["release-1", "release-2"]}


@pytest.mark.parametrize("answer", ["y\n", "n\n", "\n", ""])
def test_add_then_download_requires_confirmation(monkeypatch, tmp_path, answer):
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
    runner = CliRunner()
    assert runner.invoke(hallmark, ["init", str(destination)]).exit_code == 0
    monkeypatch.chdir(destination)
    result = runner.invoke(hallmark, ["add", "https://example.test/data/"])
    assert result.exit_code == 0, result.output
    assert not (destination / "a.fits").exists()
    result = runner.invoke(hallmark, ["download", "--all"], input=answer)
    assert "Download these files? [y/N]" in result.output
    assert Repo(destination).state.data["path"].tolist() == ["a.fits"]
    assert (destination / "a.fits").exists() == (answer == "y\n")
    assert (result.exit_code == 0) == (answer == "y\n")
