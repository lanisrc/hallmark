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
from pathlib        import Path
from click.testing  import CliRunner
from git import Repo as GitRepo
from git.exc import GitError
from types import SimpleNamespace

from hallmark import ParaFrame, Repo
from hallmark.cli import hallmark
from hallmark.downloader import DownloadError, BULK_DOWNLOAD_WARNING_FILE_COUNT
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
    repo = SimpleNamespace(worktree=worktree)
    monkeypatch.setattr(cli_module, "Repo", lambda path: repo)
    return repo


def _selection(count):
    """
    Return a list of (Path, None) tuples for testing download selection,
    with the given count of files.
    Args:
        count: The number of files to generate for the selection.
    Returns:
        A list of tuples, each containing a Path object for a file and None.
    """
    return [(Path(f"file-{index:03d}.dat"), None) for index in range(count)]


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


def test_cli_worktree_add_creates_worktree_at_explicit_path():
    """
    Test the hallmark CLI 'worktree add' command. This test initializes a hallmark
    repository with a second branch, links a worktree for it at a chosen path, and
    verifies that the branch's data is restored there and that it is listed.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            Path("a0_i0.h5").write_text("main contents\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "main data"])
            runner.invoke(hallmark, ["checkout", "experiment"])
            Path("a0_i0.h5").write_text("experiment contents\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "."])
            runner.invoke(hallmark, ["commit", "-m", "experiment data"])
            runner.invoke(hallmark, ["checkout", "main"])

            result = runner.invoke(
                hallmark, ["worktree", "add", "../chosen-name", "experiment"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for worktree add, got {result.exit_code}: " \
                f"{result.output}"
            linked_file = Path("../chosen-name/a0_i0.h5")
            assert linked_file.read_text(encoding="utf-8") == "experiment contents\n", \
                f"Expected 'experiment contents\\n', got " \
                f"{linked_file.read_text(encoding='utf-8')}"

            listed = runner.invoke(hallmark, ["worktree", "list"])
            assert "[experiment]" in listed.output, \
                f"Expected the new worktree listed on experiment, got: {listed.output}"


def test_cli_worktree_add_defaults_branch_to_destination_name():
    """
    Test that 'worktree add' without a BRANCH argument creates and checks out a branch
    named after the destination directory, the way 'git worktree add' does.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            Path("a0_i0.h5").write_text("a0_i0.h5\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "main data"])

            result = runner.invoke(hallmark, ["worktree", "add", "../scratch-run"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for worktree add, got {result.exit_code}: " \
                f"{result.output}"
            branches = runner.invoke(hallmark, ["branch"])
            assert "scratch-run" in branches.output, \
                f"Expected a 'scratch-run' branch, got: {branches.output}"


def test_cli_worktree_list_marks_active_worktree():
    """
    Test the hallmark CLI 'worktree list' command. This test initializes a hallmark
    repository, links a worktree for a second branch, and verifies that both worktrees
    are listed with their branches and that the active one is marked.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(hallmark, ["init", "repo"])
        with chdir("repo"):
            Path("a0_i0.h5").write_text("a0_i0.h5\n", encoding="utf-8")
            runner.invoke(hallmark, ["add", "a{a}_i{i}.h5"])
            runner.invoke(hallmark, ["commit", "-m", "add first file"])
            runner.invoke(hallmark, ["checkout", "experiment"])
            runner.invoke(hallmark, ["commit", "-m", "experiment data"])
            runner.invoke(hallmark, ["checkout", "main"])
            Repo(".").add_worktree("experiment")

            result = runner.invoke(hallmark, ["worktree", "list"])

            assert result.exit_code == 0, \
                f"Expected exit code 0 for worktree list, got {result.exit_code}"
            lines = [line for line in result.output.splitlines() if line.strip()]
            assert len(lines) == 2, \
                f"Expected two worktrees listed, got: {result.output}"
            current = [line for line in lines if line.startswith("*")]
            assert len(current) == 1, \
                f"Expected exactly one worktree marked current, got: {result.output}"
            assert "[main]" in current[0], \
                f"Expected the active worktree to be on main, got: {current[0]}"
            assert any("[experiment]" in line for line in lines), \
                f"Expected the linked worktree on experiment, got: {result.output}"


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
    assert "worktree" in result.output, \
        f"Expected 'worktree' command in help output, got: {result.output}"


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
            ["clone", "--no-fetch-data", str(source / ".hm"), str(target)])

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
            "is not an empty directory."), \
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
            ["clone", "--no-fetch-data", str(source / ".hm"), "target"])

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


def test_clone_reports_download_error_cleanly(monkeypatch):
    """
    Test that the hallmark CLI 'clone' command reports a download error cleanly
    when the remote URL is not configured in config.yml.
    This test initializes a hallmark repository, monkeypatches the download function
    to raise a DownloadError, and verifies that the clone command fails with the
    expected error message.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        source = Path("source")
        result = runner.invoke(hallmark, ["init", str(source)])
        assert result.exit_code == 0, \
            f"Expected exit code 0 for init, got {result.exit_code}"

        GitRepo(str(source / ".hm")).index.commit("commit initial hallmark state")
        def boom(*args, **kwargs):
            """Simulate a download error by raising a DownloadError."""
            raise DownloadError("Remote URL not configured in config.yml")
        monkeypatch.setattr("hallmark.cli.download_remote_data", boom)
        monkeypatch.setattr(
            "hallmark.cli.select_download_files",
            lambda *args, **kwargs: [(Path("data.bin"), None)])
        result = runner.invoke(
            hallmark,
            ["clone", str(source / ".hm"), "target"])

        assert result.exit_code != 0, f"Expected non-zero exit code for clone with \
            download error, got {result.exit_code}"
        assert "Remote URL not configured in config.yml" in result.output, \
            f"Expected download error message in output, got: {result.output}"


def test_clone_cli_skips_download_when_no_remote_files(monkeypatch):
    """
    Test that the hallmark CLI 'clone' command skips the download step when there are
    no remote files configured in the source repository.
    This test monkeypatches the Repo class to simulate a source repository with no
    remote files and verifies that the clone command completes successfully without
    attempting to download any files.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    repo = SimpleNamespace()
    class FakeRepo:
        """Fake Repo class to simulate a hallmark repository with no remote files."""
        @staticmethod
        def clone(url, path, fetch_data=False):
            """Simulate cloning a repository by returning a SimpleNamespace."""
            return repo

        @staticmethod
        def lwpaths(path):
            """Return the paths to the .hm directory and the worktree."""
            return Path(path) / ".hm", Path(path)
    monkeypatch.setattr(cli_module, "Repo", FakeRepo)
    monkeypatch.setattr(
        cli_module, "select_download_files", lambda *args, **kwargs: [])
    result = CliRunner().invoke(hallmark, ["clone", "source", "target"])

    assert result.exit_code == 0, f"Expected exit code 0 for clone with no remote \
        files, got {result.exit_code}"
    assert 'Successfully cloned to "target"' in result.output, \
        f"Expected success message in output, got: {result.output}"
    assert "No remote data files are configured." in result.output, \
        f"Expected message about no remote files, got: {result.output}"


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
        (["download"], "Provide one or more file paths, --tsv, or --all"),
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
    """
    Test that the hallmark CLI 'download' command in dry-run mode limits the preview
    of selected files to the first 20 files. This test monkeypatches the repository
    installation and the selection function to simulate a selection of 23 files, and
    verifies that the download command in dry-run mode outputs only the first 20 files
    and indicates that there are more files selected.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    Raises:
        AssertionError: If the download function is called during the dry-run, which
        should not happen.
    """
    _install_repo(monkeypatch)
    selected = _selection(23)
    monkeypatch.setattr(
        cli_module, "select_download_files", lambda *args, **kwargs: selected)
    def should_not_download(*args, **kwargs):
        """Simulate a dry-run by raising an AssertionError if download is attempted."""
        raise AssertionError("dry-run must not download")
    monkeypatch.setattr(cli_module, "download_remote_data", should_not_download)
    result = CliRunner().invoke(hallmark, ["download", "--all", "--dry-run"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for dry-run download, got {result.exit_code}"
    assert "Selected 23 file(s)" in result.output, \
        f"Expected message about 23 selected files, got: {result.output}"
    assert "file-000.dat" in result.output, \
        f"Expected first selected file in output, got: {result.output}"
    assert "file-019.dat" in result.output, \
        f"Expected 20th selected file in output, got: {result.output}"
    assert "file-020.dat" not in result.output, \
        f"Expected 21st selected file not in output, got: {result.output}"
    assert "... 3 more file(s)" in result.output, \
        f"Expected message about 3 more files, got: {result.output}"


def test_download_cli_reports_empty_selection(monkeypatch):
    """
    Test that the hallmark CLI 'download' command reports an empty selection when no
    files are selected for download. This test monkeypatches the repository installation
    and the selection function to simulate an empty selection, and verifies that the
    download command outputs the expected message indicating that no files were selected
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    _install_repo(monkeypatch)
    monkeypatch.setattr(
        cli_module, "select_download_files", lambda *args, **kwargs: [])
    result = CliRunner().invoke(hallmark, ["download", "--all"])

    assert result.exit_code == 0, f"Expected exit code 0 for download with empty \
        selection, got {result.exit_code}"
    assert "No files selected for download." in result.output, \
        f"Expected message about no files selected, got: {result.output}"


def test_download_cli_passes_selection_and_options_to_downloader(monkeypatch):
    """
    Test that the hallmark CLI 'download' command passes the selected files and options
    to the downloader function. This test monkeypatches the repository installation,
    the selection function, and the downloader function to capture the arguments passed
    to them, and verifies that the download command outputs the expected success message
    and that the captured arguments match the expected values.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    repo = _install_repo(monkeypatch)
    selected = [(Path("nested/file.dat"), "abc")]
    captured = {}
    def fake_select(actual_repo, **kwargs):
        """Fake selection function to capture arguments passed to it."""
        captured["select"] = (actual_repo, kwargs)
        return selected

    def fake_download(actual_repo, output_path, **kwargs):
        """Fake downloader function to capture arguments passed to it."""
        captured["download"] = (actual_repo, output_path, kwargs)
        return {
            "succeeded": 1,
            "failed": 0,
            "total_bytes": 1024 * 1024,
            "errors": []}
    monkeypatch.setattr(cli_module, "select_download_files", fake_select)
    monkeypatch.setattr(cli_module, "download_remote_data", fake_download)
    result = CliRunner().invoke(
        hallmark,
        [
            "download",
            "nested/file.dat",
            "--remote",
            "mirror",
            "--max-workers",
            "2",
            "--yes"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for download, got {result.exit_code}"
    assert "Successfully downloaded 1 files (1.0 MB)" in result.output, \
        f"Expected success message in output, got: {result.output}"
    assert captured["select"] == (
        repo,
        {"file_paths": ("nested/file.dat",), "tsv_names": (), "all_files": False}), \
        f"Expected captured selection arguments to match expected values, \
            got: {captured['select']}"
    assert captured["download"] == (
        repo,
        Path("worktree"),
        {
            "max_workers": 2,
            "show_progress": True,
            "selected_files": selected,
            "remote_name": "mirror"}), f"Expected captured download arguments to match \
                expected values, got: {captured['download']}"


def test_download_cli_allows_explicit_output_for_bare_repository(monkeypatch):
    """
    Test that the hallmark CLI 'download' command allows an explicit output path to be
    specified when the repository is bare (i.e., has no worktree). This test
    monkeypatches the repository installation to simulate a bare repository and verifies
    the download command completes successfully when an explicit output path is provided
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    _install_repo(monkeypatch, worktree=None)
    monkeypatch.setattr(
        cli_module, "select_download_files", lambda *args, **kwargs: [])

    result = CliRunner().invoke(
        hallmark, ["download", "--all", "--output", "downloads", "--dry-run"])

    assert result.exit_code == 0, f"Expected exit code 0 for download with explicit \
        output, got {result.exit_code}"
    assert "for downloads" in result.output, \
        f"Expected message about output path, got: {result.output}"


def test_download_cli_converts_selection_errors_to_click_errors(monkeypatch):
    """
    Test that the CLI 'download' command converts selection errors to click errors.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    Raises:
        AssertionError: If the download function is called during the selection error,
        which should not happen.
    """
    _install_repo(monkeypatch)
    def fail_selection(*args, **kwargs):
        """Simulate a selection error by raising a DownloadError."""
        raise DownloadError("bad selection")
    monkeypatch.setattr(cli_module, "select_download_files", fail_selection)
    result = CliRunner().invoke(hallmark, ["download", "--tsv", "missing"])

    assert result.exit_code != 0, f"Expected non-zero exit code for download with \
        selection error, got {result.exit_code}"
    assert "Error: bad selection" in result.output, \
        f"Expected error message about selection error, got: {result.output}"


def test_download_cli_reports_only_first_ten_errors(monkeypatch):
    """
    Test that the hallmark CLI 'download' command reports only the first ten errors
    when there are more than ten failed downloads. This test monkeypatches the repo
    installation, the selection function, and the downloader function to simulate a
    scenario with 12 failed downloads, and verifies that the download command outputs
    only the first ten errors and indicates that there are more errors.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    _install_repo(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "select_download_files",
        lambda *args, **kwargs: [(Path("file.dat"), None)])
    errors = [f"failure-{index}" for index in range(12)]
    monkeypatch.setattr(
        cli_module,
        "download_remote_data",
        lambda *args, **kwargs: {
            "succeeded": 2,
            "failed": 12,
            "total_bytes": 0,
            "errors": errors})
    result = CliRunner().invoke(hallmark, ["download", "file.dat", "--yes"])

    assert result.exit_code != 0, f"Expected non-zero exit code for download with \
        multiple errors, got {result.exit_code}"
    assert "2 succeeded, 12 failed" in result.output, \
        f"Expected summary of succeeded and failed downloads, got: {result.output}"
    assert "failure-0" in result.output, \
        f"Expected first error in output, got: {result.output}"
    assert "failure-9" in result.output, \
        f"Expected tenth error in output, got: {result.output}"
    assert "failure-10" not in result.output, \
        f"Expected eleventh error not in output, got: {result.output}"
    assert "... 2 more error(s)" in result.output, \
        f"Expected message about additional errors, got: {result.output}"
    assert "Failed to download 12 file(s)" in result.output, \
        f"Expected summary of failed downloads, got: {result.output}"


def test_download_cli_prompts_for_bulk_selection(monkeypatch):
    """
    Test that the hallmark CLI 'download' command prompts for confirmation when a bulk
    selection of files is made. This test monkeypatches the repository installation,
    the selection function, and the downloader function to simulate a bulk selection,
    and verifies that the download command prompts for confirmation and aborts when
    the user responds with 'n'.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    Raises:
        AssertionError: If the download function is called during the aborted selection,
        which should not happen.
    """
    _install_repo(monkeypatch)
    selected = _selection(BULK_DOWNLOAD_WARNING_FILE_COUNT)
    monkeypatch.setattr(
        cli_module, "select_download_files", lambda *args, **kwargs: selected)

    def should_not_download(*args, **kwargs):
        """Simulate an aborted selection by raising an AssertionError."""
        raise AssertionError("aborted selection must not download")
    monkeypatch.setattr(cli_module, "download_remote_data", should_not_download)
    result = CliRunner().invoke(
        hallmark, ["download", "--all"], input="n\n")

    assert result.exit_code != 0, f"Expected non-zero exit code for download with \
        aborted selection, got {result.exit_code}"
    assert (
        f"Selected {BULK_DOWNLOAD_WARNING_FILE_COUNT} files for download" in
        result.output), f"Expected message about bulk selection, got: {result.output}"
    assert "Continue? [y/N]" in result.output, \
        f"Expected prompt for confirmation, got: {result.output}"
    assert "Aborted!" in result.output, \
        f"Expected message about aborted download, got: {result.output}"


def test_download_cli_yes_skips_bulk_prompt(monkeypatch):
    """
    Test that the hallmark CLI 'download' command skips the bulk selection prompt when
    the --yes option is provided. This test monkeypatches the repository installation,
    the selection function, and the downloader function to simulate a bulk selection,
    and verifies that the download command completes successfully without prompting for
    confirmation when the --yes option is used.
    Args:
        monkeypatch: pytest fixture for monkeypatching functions and attributes.
    """
    _install_repo(monkeypatch)
    selected = _selection(BULK_DOWNLOAD_WARNING_FILE_COUNT)
    called = {"download": False}
    monkeypatch.setattr(
        cli_module, "select_download_files", lambda *args, **kwargs: selected)
    def fake_download(*args, **kwargs):
        """Fake downloader function to simulate a successful download."""
        called["download"] = True
        return {
            "succeeded": len(selected),
            "failed": 0,
            "total_bytes": 0,
            "errors": []}
    monkeypatch.setattr(cli_module, "download_remote_data", fake_download)
    result = CliRunner().invoke(hallmark, ["download", "--all", "--yes"])

    assert result.exit_code == 0, \
        f"Expected exit code 0 for download with --yes, got {result.exit_code}"
    assert called["download"], \
        "Expected download function to be called with --yes, but it was not"
    assert "Continue?" not in result.output, \
        f"Expected no prompt for confirmation with --yes, got: {result.output}"
