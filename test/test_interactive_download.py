"""Interactive downloads and default clone review after metadata creation."""

from contextlib import contextmanager
import importlib
from pathlib import Path
import shlex
import sys
from unittest.mock import patch
from urllib.parse import quote

from click import termui
from click.testing import CliRunner
import pandas as pd
import pytest

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.transport import OperationContext
from mock_server import MockServer

cli = importlib.import_module("hallmark.cli")
ROOT = "https://example.test/data/"
README = "README with space,comma.md"


class TerminalRunner(CliRunner):
    """Model a real terminal, including EOF on Click 8.1 / Python 3.9."""

    @contextmanager
    def isolation(self, *args, **kwargs):
        with super().isolation(*args, **kwargs) as streams:
            with patch.object(sys.stdin, "isatty", return_value=True), patch.object(
                    termui, "visible_prompt_func", input):
                yield streams


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / "source catalog")
    repo.state.config = {
        "data": [{"db": "data.tsv", "extraction": {
            "automatic": False, "formats": ["runs/run_{run:03d}.h5"]}}],
        "remote": [{"name": "origin", "url": ROOT}]}
    repo.state.data = pd.DataFrame([
        {"path": "runs/run_001.h5", "size_bytes": 4, "run": "001"},
        {"path": "runs/run_002.h5", "size_bytes": None, "run": "002"},
        {"path": README, "size_bytes": 5, "run": ""},
    ])
    repo.dothm.dump(repo.state)
    repo.dothm.index.add(["config.yml", "data.tsv"])
    repo.dothm.index.commit("Catalog fixture")
    server = MockServer(ROOT)
    for name in ("runs/run_001.h5", "runs/run_002.h5", README, "omitted.bin"):
        server.add_file(quote(name, safe="/"), b"notes" if name == README else b"data")
    transfers = []
    original = server.get

    def get(url, **kwargs):
        transfers.append(url)
        return original(url, **kwargs)

    server.get = get
    monkeypatch.setattr("requests.Session", lambda: server)
    monkeypatch.chdir(repo.worktree)
    return repo, transfers


def metadata(repo):
    return {name: (repo.dothm.path / name).read_bytes()
            for name in ("data.tsv", "config.yml", "meta.yml")}


@pytest.mark.parametrize("answer", ["\n", "skip\n", "", "patterns\n", "all\n",
                                     "all\n\n", "all\nskip\n"])
def test_skip_and_eof_never_transfer(catalog, answer):
    repo, transfers = catalog
    before = metadata(repo)
    result = TerminalRunner().invoke(hallmark, ["download", "--interactive"],
                                      input=answer)
    assert result.exit_code == 0, result.output
    assert "No download started" in result.output
    assert transfers == []
    assert metadata(Repo(repo.worktree)) == before


def test_patterns_are_raw_ored_and_approval_executes_displayed_plan(
        catalog, monkeypatch):
    repo, transfers = catalog
    before = metadata(repo)
    shown, executed = [], []
    display, execute = cli._show_download_plan, Repo.download

    def show(plan, **kwargs):
        assert transfers == []
        shown.append(plan)
        display(plan, **kwargs)

    def download(self, plan, **kwargs):
        assert kwargs == {"approved": True, "max_workers": 2, "progress": True}
        assert plan is shown[-1]
        executed.append(plan)
        return execute(self, plan, **kwargs)

    monkeypatch.setattr(cli, "_show_download_plan", show)
    monkeypatch.setattr(Repo, "download", download)
    result = TerminalRunner().invoke(
        hallmark, ["download", "--interactive", "--max-workers", "2"],
        input=f"patterns\nruns/*001.h5\n{README}\n\ndownload\n")
    assert result.exit_code == 0, result.output
    assert "2 file(s); 9 bytes" in result.output
    assert f"{README} (5 bytes)" in result.output
    assert "Files with unknown size: 0" in result.output
    assert len(transfers) == 2
    assert len(executed) == 1
    assert (repo.worktree / README).read_bytes() == b"notes"
    assert not (repo.worktree / "runs/run_002.h5").exists()
    assert metadata(Repo(repo.worktree)) == before


def test_revision_does_not_approve_previous_selection(catalog):
    repo, transfers = catalog
    result = TerminalRunner().invoke(
        hallmark, ["download", "--interactive"],
        input="all\nchange\npatterns\nruns/*001.h5\n\ndownload\n")
    assert result.exit_code == 0, result.output
    assert "3 file(s)" in result.output and "1 file(s); 4 bytes" in result.output
    assert "1 file(s) with unknown size" in result.output
    assert "runs/run_002.h5 (size unknown)" in result.output
    assert transfers == [ROOT + "runs/run_001.h5"]
    assert not (repo.worktree / README).exists()


def test_empty_patterns_and_no_matches_return_to_selection(catalog):
    _, transfers = catalog
    result = TerminalRunner().invoke(
        hallmark, ["download", "--interactive"],
        input="patterns\n\npatterns\nomitted*\n\nall\nskip\n")
    assert result.exit_code == 0, result.output
    assert "No cataloged files match" in result.output
    assert "3 file(s)" in result.output
    assert transfers == []


def test_interactive_dry_run_and_remote_override(catalog, monkeypatch, tmp_path):
    repo, transfers = catalog
    repo.set_config(remote_name="mirror", remote_url="https://mirror.test/")
    destination = tmp_path / "uncreated downloads"
    result = TerminalRunner().invoke(
        hallmark, ["download", "--interactive", "--dry-run", "--remote", "mirror",
                   "--output", str(destination)], input="all\n")
    assert result.exit_code == 0, result.output
    assert "https://mirror.test/" in result.output
    assert f"Destination: {destination}" in result.output
    assert "Next action" not in result.output
    assert not destination.exists()
    assert transfers == []


def test_preview_truncates_paths_but_not_totals(catalog):
    repo, transfers = catalog
    repo.state.data = pd.DataFrame([
        {"path": f"file-{number:03d}.dat", "size_bytes": number}
        for number in range(25)])
    repo.dothm.dump(repo.state)
    result = TerminalRunner().invoke(
        hallmark, ["download", "--interactive", "--dry-run"], input="all\n")
    assert result.exit_code == 0, result.output
    assert "25 file(s); 300 bytes" in result.output
    assert "file-019.dat (19 bytes)" in result.output
    assert "file-020.dat" not in result.output
    assert "... 5 more file(s)" in result.output
    assert transfers == []


@pytest.mark.parametrize("selectors", [["runs/run_001.h5"], ["--all"],
                                       ["--filter", "*.h5"], ["--tsv", "data.tsv"]])
def test_interactive_rejects_explicit_selectors(catalog, selectors):
    _, transfers = catalog
    result = TerminalRunner().invoke(
        hallmark, ["download", "--interactive", *selectors])
    assert result.exit_code != 0
    assert "--interactive cannot be combined" in result.output
    assert transfers == []


def test_standalone_interactive_requires_terminal(catalog):
    _, transfers = catalog
    result = CliRunner().invoke(hallmark, ["download", "--interactive"])
    assert result.exit_code != 0
    assert "requires a terminal" in result.output
    assert "--dry-run" in result.output
    assert "Select files" not in result.output
    assert transfers == []


@pytest.mark.parametrize("kind", ["empty", "no-remote"])
def test_unavailable_download_skips_without_prompt(catalog, kind):
    repo, transfers = catalog
    if kind == "empty":
        repo.state.data = repo.state.data.iloc[:0]
    else:
        repo.state.config.pop("remote")
    repo.dothm.dump(repo.state)
    result = TerminalRunner().invoke(hallmark, ["download", "--interactive"])
    assert result.exit_code == 0, result.output
    message = "catalog is empty" if kind == "empty" else "No data remote"
    assert message in result.output
    assert "Select files" not in result.output
    assert transfers == []


def init_pages(monkeypatch):
    pages = {
        ROOT: '<h1>Index of data</h1><a href="runs/">runs/</a>'
              '<a href="README.md">README.md</a><a href="omitted.bin">omitted.bin</a>',
        ROOT + "runs/": '<h1>Index of runs</h1>'
                        '<a href="run_001.h5">run_001.h5</a>'
                        '<a href="run_002.h5">run_002.h5</a>',
    }
    monkeypatch.setattr(OperationContext, "read_text",
                        lambda context, path: pages[context.remote.url + path])


def test_bare_catalog_prompts_validates_output_without_creating_it(
        catalog, tmp_path, monkeypatch):
    source, transfers = catalog
    repo = Repo.clone(str(source.dothm.path), tmp_path / "bare.hm")
    monkeypatch.chdir(repo.dothm.path)
    not_directory = tmp_path / "file"
    not_directory.write_text("keep")
    destination = tmp_path / "download output"
    result = TerminalRunner().invoke(
        hallmark, ["download", "--interactive", "--dry-run"],
        input=f"all\n{not_directory}\n{destination}\n")
    assert result.exit_code == 0, result.output
    assert "is a file" in result.output
    assert f"Destination: {destination}" in result.output
    assert not destination.exists()
    assert not_directory.read_text() == "keep"
    assert transfers == []


@pytest.mark.parametrize("stage", ["selection", "patterns", "output", "approval"])
def test_interrupt_is_not_a_successful_skip(catalog, tmp_path, monkeypatch, stage):
    source, transfers = catalog
    destination = tmp_path / ("copy.hm" if stage == "output" else "copy")
    responses = iter({"selection": [], "patterns": ["patterns"],
                      "output": ["all"], "approval": ["all"]}[stage])
    choose = cli._choose_download_plan

    def interrupted(prompt):
        try:
            return next(responses)
        except StopIteration:
            raise KeyboardInterrupt() from None

    def interruptible_choice(repo, **kwargs):
        with patch.object(termui, "visible_prompt_func", interrupted):
            return choose(repo, **kwargs)

    monkeypatch.setattr(cli, "_choose_download_plan", interruptible_choice)
    result = TerminalRunner().invoke(
        hallmark, ["clone", str(source.dothm.path), str(destination),
                   "--interactive"])
    assert result.exit_code != 0
    assert "Aborted" in result.output
    assert metadata(Repo(destination)) == metadata(source)
    assert transfers == []


def test_all_download_requires_approval_even_with_legacy_yes(catalog):
    repo, transfers = catalog
    result = TerminalRunner().invoke(hallmark, ["download", "--interactive", "--yes"],
                                      input="all\ndownload\n")
    assert result.exit_code == 0, result.output
    assert "still require confirmation" in result.output
    assert "Next action" in result.output
    assert len(transfers) == 3
    assert all((repo.worktree / name).exists()
               for name in ("runs/run_001.h5", "runs/run_002.h5", README))


def test_transfer_interrupt_is_not_caught_as_prompt_eof(catalog, monkeypatch):
    repo, transfers = catalog
    before = metadata(repo)

    def interrupt(self, plan, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(Repo, "download", interrupt)
    result = TerminalRunner().invoke(hallmark, ["download", "--interactive"],
                                      input="all\ndownload\n")
    assert result.exit_code != 0
    assert "Aborted" in result.output
    assert "No download started" not in result.output
    assert metadata(Repo(repo.worktree)) == before
    assert transfers == []


@pytest.mark.parametrize("bare", [False, True])
@pytest.mark.parametrize("options", [[], ["--interactive"],
                                    ["--filter", "runs/*001.h5", "--filter", README],
                                    ["--output", "../output 'files'"]])
def test_noninteractive_clone_keeps_catalog_and_prints_equivalent_commands(
        catalog, tmp_path, bare, options):
    source, transfers = catalog
    destination = tmp_path / ("new 'catalog'.hm" if bare else "new 'catalog'")
    cwd = Path.cwd()
    result = CliRunner().invoke(
        hallmark, ["clone", str(source.dothm.path), str(destination), *options],
        input="y\n")
    assert result.exit_code == 0, result.output
    target = Repo(destination)
    assert "download was skipped" in result.output
    assert "Download these files?" not in result.output
    location = target.dothm.path if bare else target.worktree
    assert "cd -- " + shlex.quote(str(location)) in result.output
    commands = [line.strip() for line in result.output.splitlines()
                if line.startswith("  hallmark download")]
    assert len(commands) == 2
    preview, execute = map(shlex.split, commands)
    assert preview == execute + ["--dry-run"]
    if "--filter" in options:
        assert execute[2:6] == options
    else:
        assert execute[2] == "--all"
    if "--output" in options:
        assert execute[-2:] == ["--output", str((cwd / options[1]).resolve())]
    elif bare:
        assert execute[-2:] == ["--output", "/path/to/downloads"]
    else:
        assert "--output" not in execute
    assert "explicit approval is required" in result.output
    assert Path.cwd() == cwd
    assert transfers == []
    assert metadata(target) == metadata(source)
    assert target.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha


@pytest.mark.parametrize("options,input_text,expected", [
    ([], "y\n", ["runs/run_001.h5", "runs/run_002.h5", README]),
    ([], "n\n", []),
    ([], "\n", []),
    ([], "", []),
    (["--filter", "runs/*001.h5", "--filter", README], "y\n",
     ["runs/run_001.h5", README]),
    (["--filter", "omitted*"], "", []),
    (["--interactive"], "patterns\nruns/*001.h5\n\ndownload\n", ["runs/run_001.h5"]),
    (["--interactive"], "skip\n", []),
])
def test_clone_reviews_only_download_selection_after_complete_copy(
        catalog, tmp_path, monkeypatch, options, input_text, expected):
    source, transfers = catalog
    destination = tmp_path / "new catalog"
    before = metadata(source)
    shown, executed = [], []
    show, download = cli._show_download_plan, Repo.download

    def show_after_creation(plan, **kwargs):
        assert transfers == []
        assert metadata(Repo(destination)) == before
        assert (Repo(destination).dothm.head.commit.hexsha
                == source.dothm.head.commit.hexsha)
        assert plan.output_path == destination
        shown.append(plan)
        show(plan, **kwargs)

    def execute(self, plan, **kwargs):
        assert plan is shown[-1]
        assert kwargs == {"approved": True, "max_workers": 4, "progress": True}
        executed.append(plan)
        return download(self, plan, **kwargs)

    monkeypatch.setattr(cli, "_show_download_plan", show_after_creation)
    monkeypatch.setattr(Repo, "download", execute)
    initial_cwd = Path.cwd()
    result = TerminalRunner().invoke(
        hallmark, ["clone", str(source.dothm.path), str(destination), *options],
        input=input_text)
    assert result.exit_code == 0, result.output
    assert result.output.index("Successfully") < result.output.index("Catalog ready")
    if "--interactive" not in options:
        assert "Select files" not in result.output
    assert metadata(Repo(destination)) == before
    assert Path.cwd() == initial_cwd
    assert set(transfers) == {ROOT + quote(name, safe="/") for name in expected}
    assert bool(executed) == bool(expected)
    assert Repo(destination).plan_download().file_count == 3
    assert not (destination / "omitted.bin").exists()
    if not options:
        assert "3 file(s); 9 bytes known; 1 file(s) with unknown size" in result.output
        assert "runs/run_002.h5 (size unknown)" in result.output


@pytest.mark.parametrize("bare", [False, True])
def test_no_download_disables_review_and_planning(catalog, tmp_path, monkeypatch, bare):
    source, transfers = catalog
    destination = tmp_path / ("copy.hm" if bare else "copy")

    def unexpected(*args, **kwargs):
        raise AssertionError("No download planning or review was requested")

    monkeypatch.setattr(cli, "_offer_download", unexpected)
    monkeypatch.setattr(Repo, "plan_download", unexpected)
    result = TerminalRunner().invoke(
        hallmark, ["clone", str(source.dothm.path), str(destination), "--no-download"])
    assert result.exit_code == 0, result.output
    assert "Download these files?" not in result.output
    assert "download was skipped" not in result.output
    assert metadata(Repo(destination)) == metadata(source)
    assert transfers == []


@pytest.mark.parametrize("options", [
    ["--no-download", "--interactive"], ["--no-download", "--filter", "*.fits"],
    ["--no-download", "--output", "output"], ["--interactive", "--filter", "*.fits"],
    ["--with-download"],
])
def test_clone_invalid_options_fail_before_source_access(
        tmp_path, monkeypatch, options):
    def unexpected(*args, **kwargs):
        raise AssertionError("Must reject options before cloning")

    monkeypatch.setattr(Repo, "clone", unexpected)
    destination = tmp_path / "new"
    result = TerminalRunner().invoke(hallmark, ["clone", "source", str(destination),
                                               *options])
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert not isinstance(result.exception, AssertionError)
    assert not destination.exists()


@pytest.mark.parametrize("failure", ["setup", "partial", "interrupt"])
def test_clone_download_failure_preserves_catalog(
        catalog, tmp_path, monkeypatch, failure):
    source, _ = catalog
    destination = tmp_path / "created"

    def fail(self, plan, **kwargs):
        if failure == "setup":
            raise cli.DownloadError("fixture access denied")
        if failure == "interrupt":
            raise KeyboardInterrupt()
        return {"succeeded": 1, "failed": 1, "total_bytes": 4,
                "errors": ["fixture error"]}

    monkeypatch.setattr(Repo, "download", fail)
    result = TerminalRunner().invoke(
        hallmark, ["clone", str(source.dothm.path), str(destination)], input="y\n")
    assert result.exit_code != 0
    assert "Successfully cloned" in result.output
    assert "Failed to clone" not in result.output
    assert "No download started" not in result.output
    assert metadata(Repo(destination)) == metadata(source)
    assert Repo(destination).dothm.head.commit.hexsha == source.dothm.head.commit.hexsha


@pytest.mark.parametrize("provided", [False, True])
@pytest.mark.parametrize("bare", [False, True])
def test_clone_output_and_bare_prompt(catalog, tmp_path, bare, provided):
    source, transfers = catalog
    destination = tmp_path / ("copy.hm" if bare else "copy")
    output = tmp_path / "downloads"
    arguments = ["clone", str(source.dothm.path), str(destination),
                 "--filter", "runs/*001.h5"]
    if provided:
        arguments += ["--output", str(output)]
    answer = (str(output) + "\n" if bare and not provided else "") + "y\n"
    result = TerminalRunner().invoke(hallmark, arguments, input=answer)
    assert result.exit_code == 0, result.output
    assert ("Output directory" in result.output) == (bare and not provided)
    target = output if bare or provided else destination
    assert (target / "runs/run_001.h5").read_bytes() == b"data"
    assert transfers == [ROOT + "runs/run_001.h5"]
    assert metadata(Repo(destination)) == metadata(source)


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("bare", [False, True])
def test_clone_eof_or_interrupt_before_transfer(catalog, tmp_path, monkeypatch,
                                               bare, interrupt):
    source, transfers = catalog
    destination = tmp_path / ("copy.hm" if bare else "copy")
    if interrupt:
        def stop(*args, **kwargs):
            raise KeyboardInterrupt()
        # Default review uses click.confirm for approval and click.prompt for output.
        monkeypatch.setattr(cli, "_confirm_download_plan", stop)
        if bare:
            monkeypatch.setattr(cli, "_download_output", stop)
    result = TerminalRunner().invoke(
        hallmark, ["clone", str(source.dothm.path), str(destination)], input="")
    assert (result.exit_code != 0) == interrupt
    assert ("No download started" in result.output) != interrupt
    assert metadata(Repo(destination)) == metadata(source)
    assert transfers == []


@pytest.mark.parametrize("kind", ["empty", "no-remote"])
def test_clone_without_downloadable_data_completes(catalog, tmp_path, kind):
    source, transfers = catalog
    if kind == "empty":
        source.state.data = source.state.data.iloc[:0]
    else:
        source.state.config.pop("remote")
    source.dothm.dump(source.state)
    source.dothm.index.add(["config.yml", "data.tsv"])
    source.dothm.index.commit("Unavailable download")
    destination = tmp_path / "copy"
    result = TerminalRunner().invoke(
        hallmark, ["clone", str(source.dothm.path), str(destination)])
    assert result.exit_code == 0, result.output
    message = "catalog is empty" if kind == "empty" else "No data remote"
    assert message in result.output
    assert "Download these files?" not in result.output
    assert metadata(Repo(destination)) == metadata(source)
    assert transfers == []


@pytest.mark.parametrize("remote", [False, True])
def test_init_and_remote_add_never_scan_local_files_or_offer_downloads(
        catalog, tmp_path, monkeypatch, remote):
    _, transfers = catalog
    destination = tmp_path / "initialized"
    destination.mkdir()
    local = destination / "existing.h5"
    local.write_bytes(b"untouched")
    if not remote:
        def no_remote_access(*args, **kwargs):
            raise AssertionError("Local init must not contact a remote")
        monkeypatch.setattr(OperationContext, "__enter__", no_remote_access)

    def no_download(*args, **kwargs):
        raise AssertionError("Init and add must not offer, plan, or execute downloads")

    monkeypatch.setattr(cli, "_offer_download", no_download)
    monkeypatch.setattr(Repo, "plan_download", no_download)
    monkeypatch.setattr(Repo, "download", no_download)
    result = TerminalRunner().invoke(hallmark, ["init", str(destination)])
    assert result.exit_code == 0, result.output
    if remote:
        init_pages(monkeypatch)
        monkeypatch.chdir(destination)
        result = TerminalRunner().invoke(
            hallmark, ["add", ROOT + "{group}/run_{run:03d}.h5"])
        assert result.exit_code == 0, result.output
    repo = Repo(destination)
    assert local.read_bytes() == b"untouched"
    assert transfers == []
    assert "Download these files?" not in result.output
    assert "Select files" not in result.output
    if remote:
        assert "then hallmark download" in result.output
        assert repo.state.data[["group", "run"]].values.tolist() == [
            ["runs", "001"], ["runs", "002"]]
    else:
        assert repo.state.data.empty
        assert not list((repo.dothm.path / "objects").rglob("*"))


def test_failed_clone_never_reviews_download(catalog, monkeypatch):
    repo, transfers = catalog

    def unexpected(*args, **kwargs):
        raise AssertionError("No handoff before successful creation")

    monkeypatch.setattr(cli, "_offer_download", unexpected)
    result = TerminalRunner().invoke(hallmark, ["clone", str(repo.dothm.path),
                                               str(repo.worktree)])
    assert result.exit_code != 0
    assert not isinstance(result.exception, AssertionError)
    assert transfers == []


def test_snapshot_clone_reviews_downloads_from_saved_remote(
        catalog, tmp_path, monkeypatch):
    source, transfers = catalog
    snapshot = metadata(source)
    snapshot_url = "https://example.test/published/"
    reads = []

    def read(context, path):
        assert context.remote.url == snapshot_url
        reads.append(path)
        return snapshot[path].decode()

    monkeypatch.setattr(OperationContext, "read_text", read)
    destination = tmp_path / "snapshot"
    result = TerminalRunner().invoke(
        hallmark, ["clone", snapshot_url, str(destination), "--source-type", "catalog",
                   "--filter", "runs/*001.h5"], input="y\n")
    assert result.exit_code == 0, result.output
    assert set(reads) == {"config.yml", "meta.yml", "data.tsv"}
    assert transfers == [ROOT + "runs/run_001.h5"]
    target = Repo(destination)
    assert metadata(target) == snapshot
    assert target.plan_download().file_count == 3
    message = target.dothm.head.commit.message.strip()
    assert message == "Import published catalog snapshot"
