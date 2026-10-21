"""Edge cases of the hm command line: rare options, messages and long listings."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.remote.plan import DownloadItem, DownloadPlan
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing

cli_module = importlib.import_module("hallmark.cli")

ONE, TWO = "https://one.test/data/", "https://two.test/data/"
# Twelve names, so that listings capped at ten have something to cut.
NAMES = [f"f{index:02d}.fits" for index in range(12)]


def _listing(*names, sizes=None):
    """Return an HTML index of *names*, with optional byte sizes."""
    sizes = sizes or {}
    links = "".join(
        f'<a href="{name}"'
        + (f' data-size="{sizes[name]}"' if name in sizes else "")
        + f">{name}</a>" for name in names)
    return "<h1>Index of data</h1>" + links


@pytest.fixture
def servers(monkeypatch):
    """Serve directory listings for one.test and two.test without a network."""
    pages = {"one.test": {"": _listing(*NAMES)},
             "two.test": {"": _listing(*NAMES, "extra.fits")}}

    def read_text(context, path, **_):
        page = pages[context.remote.host].get(path)
        if page is None:
            raise RemoteObjectMissing("No metadata")
        return page

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    return pages


def _fake_download_repo(monkeypatch, plan, results):
    """Install a repository whose plan and download results are fixed."""
    calls = []

    def download(actual_plan, **kwargs):
        calls.append(kwargs)
        return results

    repo = SimpleNamespace(worktree=Path("worktree"),
                           plan_download=lambda *args, **kwargs: plan,
                           download=download)
    monkeypatch.setattr(cli_module, "Repo", lambda path: repo)
    return calls


def _plan(count=1, **kwargs):
    """Return a plan of *count* files from example.test."""
    items = tuple(DownloadItem(Path(f"file-{index:03d}.dat"), size_bytes=8)
                  for index in range(count))
    return DownloadPlan(items, "https://example.test/data/", Path("worktree"),
                        **kwargs)


### hm download: reporting results ###

def test_download_reports_existing_copied_and_unrecorded_files(monkeypatch):
    copied = [f"copy-{index:02d}.dat" for index in range(12)]
    note = "SHA-1s not recorded: this catalog predates sources"
    _fake_download_repo(monkeypatch, _plan(), {
        "succeeded": 1, "failed": 0, "total_bytes": 8, "errors": [],
        "skipped": ["done.dat"], "copied": copied, "staging_note": note})
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Already downloaded: done.dat" in result.output
    assert "Copied instead of linked (links unavailable):" in result.output
    assert "  copy-09.dat" in result.output
    # Only the first ten copies are listed.
    assert "copy-10.dat" not in result.output
    assert note in result.output
    assert "Successfully downloaded 1 files" in result.output


def test_download_lists_every_error_when_there_are_few(monkeypatch):
    _fake_download_repo(monkeypatch, _plan(2), {
        "succeeded": 1, "failed": 1, "total_bytes": 8,
        "errors": ["file-001.dat: connection reset"]})
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code == 1
    assert "1 succeeded, 1 failed" in result.output
    assert "  - file-001.dat: connection reset" in result.output
    assert "more error(s)" not in result.output
    assert "Failed to download 1 file(s)" in result.output


def test_download_of_files_only_placed_from_the_store_claims_no_transfer(
        monkeypatch):
    _fake_download_repo(monkeypatch, _plan(), {
        "succeeded": 1, "failed": 0, "total_bytes": 0, "errors": [],
        "from_store": ["file-000.dat"]})
    result = CliRunner().invoke(hallmark, ["download", "--all"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Placed 1 file(s) from .hm/objects" in result.output
    assert "Successfully downloaded" not in result.output


@pytest.mark.parametrize("results, shown", [
    ({"staged": 1}, "Staged SHA-1 for 1 file(s); run hm commit to record them"),
    ({}, None),
])
def test_download_with_nothing_to_fetch_still_records_known_sha1(
        monkeypatch, results, shown):
    complete = DownloadItem(Path("done.dat"), sha1="a" * 40)
    plan = DownloadPlan((), "https://example.test/data/", Path("worktree"),
                        skipped_items=(complete,))
    calls = _fake_download_repo(monkeypatch, plan, {
        "succeeded": 0, "failed": 0, "total_bytes": 0, "errors": [], **results})
    result = CliRunner().invoke(hallmark, ["download", "--all"])
    assert result.exit_code == 0, result.output
    assert "No files selected for download." in result.output
    assert "Download these files?" not in result.output
    # Nothing is transferred, so the download needs no approval.
    assert calls == [{}]
    assert "Successfully downloaded" not in result.output
    if shown is None:
        assert "Staged SHA-1" not in result.output
    else:
        assert shown in result.output


### opening the repository ###

def test_commands_run_inside_dot_hm_open_it_as_a_bare_repository(tmp_path,
                                                                 monkeypatch):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data" / ".hm")
    result = CliRunner().invoke(hallmark, ["info"])
    assert result.exit_code == 0, result.output
    assert f'dot-hallmark repo: "{(tmp_path / "data" / ".hm").resolve()}"' \
        in result.output
    assert 'hallmark worktree: "None"' in result.output


### hm add: options ###

def test_add_rejects_a_blank_fmt(tmp_path, monkeypatch, servers):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["add", ONE, "--fmt", "   "])
    assert result.exit_code == 1
    assert "--fmt must define a non-empty format" in result.output
    assert "sources" not in Repo(tmp_path / "data").state.config


def test_add_refuses_remote_options_with_several_inputs(tmp_path, monkeypatch):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data")
    Path("a.txt").write_text("a")
    Path("b.txt").write_text("b")
    result = CliRunner().invoke(
        hallmark, ["add", "a.txt", "b.txt", "--filter", "*.txt"])
    assert result.exit_code == 1
    assert "Remote add accepts one URL at a time" in result.output
    assert Repo(tmp_path / "data").state.data.empty


def test_add_without_a_prefix_choice_offers_only_skip_or_abort(
        tmp_path, monkeypatch, servers):
    repo = Repo.init(tmp_path / "data")
    repo.add(ONE, filter="f00.*", source="first")
    repo.add(TWO, filter="*.fits", on_duplicate="skip")
    monkeypatch.chdir(repo.worktree)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    # The existing source keeps its prefix, so no folder can be offered.
    result = CliRunner().invoke(
        hallmark, ["add", ONE, "--filter", "*.fits", "--source", "first"],
        input="s\n")
    assert result.exit_code == 0, result.output
    assert "[s] Skip them, keeping the catalogued rows" in result.output
    assert "[a] Abort; nothing is saved" in result.output
    assert "[p]" not in result.output
    assert "Skipped paths another source already catalogs:" in result.output
    owners = Repo(repo.worktree).state.data.set_index("path")["catalog_source"]
    assert owners["f00.fits"] == "first"
    assert owners["f01.fits"] == "two-test"


### hm add: long listings are cut at ten ###

def test_add_lists_ten_skipped_duplicates_and_counts_the_rest(
        tmp_path, monkeypatch, servers):
    repo = Repo.init(tmp_path / "data")
    repo.add(ONE)
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["add", TWO, "--skip-duplicates"])
    assert result.exit_code == 0, result.output
    listing = result.output.split(
        "Skipped paths another source already catalogs:\n")[1]
    assert listing.count("source 'origin')") == 10
    assert "  ... and 2 more" in listing
    assert "extra.fits" in result.output


@pytest.mark.parametrize("answer, kept", [("keep", True), ("remove", False)])
def test_add_asks_about_many_files_gone_from_the_server(
        tmp_path, monkeypatch, servers, answer, kept):
    repo = Repo.init(tmp_path / "data")
    repo.add(ONE)
    servers["one.test"][""] = _listing("new.fits")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["add", ONE], input=f"{answer}\n")
    assert result.exit_code == 0, result.output
    assert "12 catalogued file(s) are no longer on the server:" in result.output
    assert "  f09.fits\n" in result.output
    assert "  f10.fits\n" not in result.output.split("Remove them")[0]
    assert "  ... and 2 more" in result.output
    paths = Repo(repo.worktree).state.data.path.tolist()
    assert "new.fits" in paths
    if kept:
        assert "though no longer on the server: 12 file(s)" in result.output
        assert set(NAMES) <= set(paths)
    else:
        assert "Removed from the catalog (no longer on the server):" \
            in result.output
        assert not set(NAMES) & set(paths)


def test_add_lists_ten_files_changed_upstream_and_counts_the_rest(
        tmp_path, monkeypatch, servers):
    servers["one.test"][""] = _listing(*NAMES, sizes=dict.fromkeys(NAMES, 3))
    repo = Repo.init(tmp_path / "data")
    repo.add(ONE)
    for name in NAMES:
        (repo.worktree / name).write_text("old")
    servers["one.test"][""] = _listing(*NAMES, sizes=dict.fromkeys(NAMES, 4))
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["add", ONE])
    assert result.exit_code == 0, result.output
    assert ("Changed upstream (12); run hm download to replace the local "
            "copies:") in result.output
    changed = result.output.split("replace the local copies:\n")[1]
    assert "  f09.fits\n" in changed
    assert "f10.fits" not in changed
    assert "  ... and 2 more" in changed


### hm restore ###

def test_restore_requires_the_staged_flag(tmp_path, monkeypatch):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["restore", "a.txt"])
    assert result.exit_code == 1
    assert "Only hm restore --staged <path> works for now." in result.output


def test_restore_staged_requires_a_path(tmp_path, monkeypatch):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["restore", "--staged"])
    assert result.exit_code == 1
    assert "Usage: hm restore --staged <path> [<path> ...]" in result.output


def test_restore_staged_unstages_one_file(tmp_path, monkeypatch):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data")
    for index in range(3):
        Path(f"a{index}.txt").write_text(str(index))
    runner = CliRunner()
    assert runner.invoke(hallmark, ["add", "a{i}.txt"]).exit_code == 0
    result = runner.invoke(hallmark, ["restore", "--staged", "a1.txt"])
    assert result.exit_code == 0, result.output
    staged = Repo(tmp_path / "data").status()["staged"]["added"]
    assert staged == ["a0.txt", "a2.txt"]
    assert Path("a1.txt").read_text() == "1"


def test_restore_staged_rejects_a_path_outside_the_worktree(tmp_path,
                                                            monkeypatch):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["restore", "--staged", "../a.txt"])
    assert result.exit_code == 1
    assert "restore path is outside the worktree: '../a.txt'" in result.output


### hm set-config, commit and log ###

def test_set_config_without_options_changes_nothing(tmp_path, monkeypatch):
    Repo.init(tmp_path / "data")
    config = tmp_path / "data" / ".hm" / "config.yml"
    before = config.read_bytes()
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["set-config"])
    assert result.exit_code == 1
    assert "No config changes requested." in result.output
    assert config.read_bytes() == before


def test_set_config_rejects_an_encoding_without_a_field_name(tmp_path,
                                                             monkeypatch):
    Repo.init(tmp_path / "data")
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["set-config", "--encoding", " =a(.*)"])
    assert result.exit_code == 1
    assert "encoding values must use FIELD=REGEX" in result.output


def test_commit_with_nothing_staged_says_so(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "data")
    repo.commit("Initial state")
    head = repo.dothm.head.commit.hexsha
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["commit", "-m", "Nothing new"])
    assert result.exit_code == 0, result.output
    assert "No changes added to commit." in result.output
    assert Repo(tmp_path / "data").dothm.head.commit.hexsha == head


def test_log_prints_nothing_on_a_branch_without_commits(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "data")
    repo.dothm.git.checkout("--orphan", "fresh")
    monkeypatch.chdir(tmp_path / "data")
    result = CliRunner().invoke(hallmark, ["log"])
    assert result.exit_code == 0, result.output
    assert result.output == ""


### hm detect with a CyVerse remote ###

def _cyverse_table(*rows):
    """Return CyVerse listing markup for (kind, href, size) rows."""
    cells = "".join(
        f'<tr class="object {kind}"><td class="name"><a href="{href}">{href}</a>'
        f'</td><td class="size">{size}</td></tr>' for kind, href, size in rows)
    return f"<html><body><table><tbody>{cells}</tbody></table></body></html>"


def test_detect_reads_a_cyverse_backend_listing(tmp_path, monkeypatch, servers):
    servers["lab.test"] = {
        "": _cyverse_table(("collection", "run/", "")),
        "run/": _cyverse_table(*(("data-object", f"M87_{scan:03d}.fits", "1,024")
                                 for scan in (1, 2, 3)))}
    repo = Repo.init(tmp_path / "data")
    repo.set_config(remote_name="origin", remote_url="https://lab.test/data/",
                    remote_backend="cyverse")
    monkeypatch.chdir(repo.worktree)
    result = CliRunner().invoke(hallmark, ["detect", "origin", "--add"])
    assert result.exit_code == 0, result.output
    assert "Detected 1 fmt(s) from 3 files:" in result.output
    assert "  run/M87_{scan}.fits" in result.output
    data = Repo(repo.worktree).state.data
    assert data["path"].tolist() == [
        "run/M87_001.fits", "run/M87_002.fits", "run/M87_003.fits"]
    # CyVerse rows carry exact sizes, which the catalog keeps.
    assert data["size_bytes"].astype(int).tolist() == [1024] * 3


### hm clone: error messages ###

def test_clone_error_without_a_fatal_line_keeps_the_git_message():
    from git.exc import GitCommandError

    from hallmark.error import CloneError

    error = GitCommandError(["git", "clone"], 128,
                            stderr="could not lock config file")
    message = str(CloneError.from_git_command(error))
    assert "could not lock config file" in message
    assert "fatal:" not in message
    assert str(CloneError.from_git_command(
        GitCommandError(["git", "clone"], 128), fallback="clone failed")) == \
        "clone failed"


def test_clone_error_names_the_clone_path_only_with_a_display_path(tmp_path):
    from git.exc import GitCommandError

    from hallmark.error import CloneError

    target = tmp_path / "copy"
    error = GitCommandError(
        ["git", "clone"], 128,
        stderr=f"fatal: destination path '{target}' already exists")
    assert str(CloneError.from_git_command(error, clone_path=target)) == \
        f"fatal: destination path '{target}' already exists"
    assert str(CloneError.from_git_command(
        error, clone_path=target, display_path="copy")) == \
        "fatal: destination path 'copy' already exists"
