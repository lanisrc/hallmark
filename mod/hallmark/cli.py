# Copyright 2025 the Hallmark Authors
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

"""Hallmark CLI entrypoint and command wiring."""

from contextlib import contextmanager
from pathlib import Path

import click
import requests
import yaml
from click import ClickException
from git.exc import GitError

from . import Repo
from .helper_functions import validate_path_component
from .repo_builder import build_repo
from .downloader import (
    BULK_DOWNLOAD_WARNING_FILE_COUNT,
    DownloadError,
    download_remote_data,
    select_download_files)
from .error import CheckoutError, CloneError
from .repo_config import normalize_tsv_name


# use a context manager to translate application errors into clean Click errors
@contextmanager
def _translate_cli_errors(*error_types, prefix=None):
    """
    Used by hallmark, init, info, status, add, set_config, commit, log, branch,
    checkout, download, clone, and build.
    Context manager to translate application errors into clean Click errors.

    Args:
        *error_types: Exception types to catch and translate.
        prefix (str, optional): Optional prefix for the error message.

    Raises:
        ClickException: If an error of the specified types is raised within the context.
    """
    # try to execute the code block within the context manager
    try:
        # yield control to the code block that uses this context manager
        yield
    # handle any specified error types raised within the context manager
    except error_types as exc:
        # construct a user-friendly error message with an optional prefix
        message = (f"{prefix}: {exc}" if prefix else str(exc))
        raise ClickException(message) from exc


# exception types translated to a clean CLI error by the hallmark commands that
# read the repository state (status, log, branch, checkout)
_REPO_READ_ERRORS = (
    GitError,
    RuntimeError,
    ValueError,
    FileNotFoundError,
    CheckoutError)

# exception types translated to a clean CLI error by the hallmark build command
_BUILD_DATASET_ERRORS = (
    RuntimeError,
    ValueError,
    FileNotFoundError,
    FileExistsError,
    GitError,
    yaml.YAMLError)


def _confirm_bulk_download(
    selected_files,
    assume_yes: bool,
    ) -> None:
    """
    Used by _run_download.
    Prompt the user for confirmation if the number of selected files exceeds
    the warning threshold and the user has not opted to assume yes.
    Args:
        selected_files (list): List of files selected for download.
        assume_yes (bool): Flag indicating whether to assume yes for prompts.
    Raises:
        ClickException: If the user does not confirm the download.
    """
    file_count = len(selected_files)
    if (
        file_count >= BULK_DOWNLOAD_WARNING_FILE_COUNT
        and not assume_yes):
        click.echo(
            f"Selected {file_count} files for download.\n"
            "The total size is not recorded and may be very large.")
        click.confirm("Continue?", abort=True)


def _report_download_results(results: dict) -> None:
    """
    Used by _run_download.
    Report the results of the download operation to the user.
    Args:
        results (dict): A dictionary containing the download results, including
            the number of succeeded and failed downloads, total bytes downloaded,
            and any error messages.
    Raises:
        ClickException: If there were failed downloads, indicating the number of
            failed files and providing error messages.
    """
    # Report the results of the download operation to the user.
    succeeded = results["succeeded"]
    failed = results["failed"]
    total_mb = results["total_bytes"] / (1024 * 1024)
    # If there were no failed downloads, report success and exit.
    if failed == 0:
        click.echo(
            f"Successfully downloaded {succeeded} files "f"({total_mb:.1f} MB)")
        # bail out of the function early since there are no errors to report
        return
    # If there were failed downloads, report the number of successes and failures
    click.echo(
        "Download completed with errors: "
        f"{succeeded} succeeded, {failed} failed", err=True)
    errors = results.get("errors", [])
    # print the first 10 errors to the user
    for error in errors[:10]:
        click.echo(f"  - {error}", err=True)
    # if there are more than 10 errors, indicate that there are additional errors
    if len(errors) > 10:
        click.echo(
            f"  - ... {len(errors) - 10} more error(s)", err=True)
    # raise a ClickException to indicate failure
    raise ClickException(f"Failed to download {failed} file(s)")


def _run_download(
    repo,
    selected_files,
    output_path,
    *,
    max_workers,
    assume_yes,
    remote_name=None,
    ) -> None:
    """
    Used by download and clone.
    Confirm, execute, and report one selected download."""
    # confirm with user if the number of selected files exceeds warning threshold
    _confirm_bulk_download(selected_files, assume_yes)
    # download the selected files from the remote repository
    results = download_remote_data(
        repo,
        output_path,
        max_workers=max_workers,
        show_progress=True,
        selected_files=selected_files,
        remote_name=remote_name)
    # report the results of the download operation to the user
    _report_download_results(results)


@click.group()
@click.version_option()
@click.pass_context
def hallmark(ctx):
    """Reproducibility is the hallmark of the scientific method.

    Hallmark is a lightweight package designed to version control and
    manage data products in a complex workflow.
    """
    # if the invoked subcommand is one of the commands that does not require a repo
    if ctx.invoked_subcommand in [None, "init", "clone", "build"]:
        # return early without attempting to open a repository
        return
    # attempt to open the hallmark repository in the current directory
    with _translate_cli_errors(GitError, prefix="Failed to open hallmark repository"):
        ctx.obj = Repo(".")


@hallmark.command(short_help="Initialize a hallmark repository.")
@click.argument("path")
def init(path):
    """Initialize a hallmark repository at PATH.

    If PATH ends with `.hm`, a bare repository is created.
    Otherwise, a `.hm` directory is created inside PATH.
    """
    # attempt to initialize the hallmark repository at the specified path
    with _translate_cli_errors(
        GitError,
        prefix=("Failed to initialize hallmark repository " f'at "{path}"')):
        Repo.init(path)


@hallmark.command(short_help="Show information of the current directory.")
@click.pass_obj
def info(repo):
    """Show hallmark repository information of the current directory.

    Display local `.hm` and worktree locations for the current
    directory.
    """
    click.echo(f'dot-hallmark repo: "{repo.dothm.path}"')
    click.echo(f'hallmark worktree: "{repo.worktree}"')


@hallmark.command(short_help="Show worktree and staged hallmark state.")
@click.pass_obj
def status(repo):
    """Show hallmark status for the current branch and worktree."""
    # attempt to get the status of the hallmark repository, handling any errors
    with _translate_cli_errors(*_REPO_READ_ERRORS):
        snapshot = repo.status()
    # if there is a snapshot of the current branch, display its name to the user
    if snapshot:
        click.echo(f'On branch {snapshot["branch"]}')

    staged = snapshot["staged"]
    worktree = snapshot["worktree"]
    untracked = snapshot["untracked"]

    def emit_section(title, entries, fg):
        if not entries:
            return
        click.echo("")
        click.secho(title, fg=fg)
        for label, paths in entries:
            for path in paths:
                click.echo("  " + click.style(f"{label}:   {path}", fg=fg))

    emit_section(
        "Changes to be committed:",
        [
            ("state", staged["state"]),
            ("new file", staged["added"]),
            ("modified", staged["modified"]),
            ("deleted", staged["deleted"]),
        ],
        "green",
    )
    emit_section(
        "Changes not staged for commit:",
        [
            ("modified", worktree["modified"]),
            ("deleted", worktree["deleted"]),
        ],
        "red",
    )
    if untracked:
        click.echo("")
        click.secho("Untracked files:", fg="red")
        for path in untracked:
            click.echo("  " + click.style(path, fg="red"))

    if not any((staged["state"], staged["added"], staged["modified"], staged["deleted"],
                worktree["modified"], worktree["deleted"], untracked)):
        click.echo("")
        click.echo("nothing to commit, working tree clean")


@hallmark.command(short_help="Add files to hallmark index.")
@click.option(
    "--regex",
    "encoding",
    is_flag=True,
    default=False,
    show_default=True,
    help="Enable regex-based encoding rules from config.yml.")
@click.argument("inputs", nargs=-1, required=True)
@click.pass_obj
def add(repo, encoding, inputs):
    """Add files to the hallmark index.

    `hallmark add [--regex] FORMAT` uses the branch format string workflow.
    `hallmark add "."` rebuilds the manifest from current files that match
    the branch `fmt` in `config.yml`.
    Explicit path inputs such as shell-expanded `*` are not supported yet
    with the parameter-based manifest format.
    """
    # attempt to add the specified files to the hallmark index, handling any errors
    with _translate_cli_errors(RuntimeError, ValueError, FileNotFoundError):
        # if there is only one input, use the add method for a single input
        if len(inputs) == 1:
            pf = repo.add(inputs[0], encoding)
        # oterhwise, use the add_paths method for multiple inputs
        else:
            pf = repo.add_paths(list(inputs))

    if pf.empty:
        click.echo("No files matched the format string.")
    else:
        click.echo("Changes to be committed")
        click.echo(pf.path.to_string(index=False, header=False))


@hallmark.command("set-config", short_help="Update hallmark branch config.")
@click.option("--fmt")
@click.option("--remote-name")
@click.option("--remote-url")
@click.option("--encoding", "encodings", multiple=True)
@click.pass_obj
def set_config(repo, fmt, remote_name, remote_url, encodings):
    """Update the current branch config.yml."""
    # if no config changes are requested, raise a ClickException to inform the user
    if (
    fmt is None and remote_name is None and remote_url is None and not encodings):
        raise ClickException("No config changes requested.")
    encoding_updates = {}
    for item in encodings:
        if "=" not in item:
            raise ClickException('encoding values must use FIELD=REGEX')
        field, regex = item.split("=", 1)
        if not field.strip():
            raise ClickException('encoding values must use FIELD=REGEX')
        encoding_updates[field.strip()] = regex

    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(RuntimeError, ValueError, FileNotFoundError):
        repo.set_config(
            fmt=fmt,
            remote_name=remote_name,
            remote_url=remote_url,
            encoding_updates=encoding_updates or None)

    click.echo("Updated hallmark config.")


@hallmark.command(short_help="Commit changes to the repository.")
@click.option("-m", "message", required=True)
@click.pass_obj
def commit(repo, message):
    """Commit changes in the index to the hallmark repository.

    This is analogous to `git commit -m MESSAGE`.
    """
    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(GitError, RuntimeError, ValueError):
        created = repo.commit(message)

    if created:
        click.echo("Committed staged state changes.")
    else:
        click.echo("No changes added to commit.")


@hallmark.command(short_help="Show hallmark commit history.")
@click.pass_obj
def log(repo):
    """Show commit history for the hallmark state repository."""
    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(*_REPO_READ_ERRORS):
        history = repo.log()
    # if there is a history of commits, display it to the user
    if history:
        click.echo(history)


@hallmark.command(short_help="List hallmark branches.")
@click.pass_obj
def branch(repo):
    """List local hallmark branches."""
    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(*_REPO_READ_ERRORS):
        snapshot = repo.branches()
    # if there is a snapshot of the branches, display them to the user
    if snapshot:
        current = snapshot["current"]

    for name in snapshot["names"]:
        prefix = "*" if name == current else " "
        click.echo(f"{prefix} {name}")


@hallmark.command(short_help="Switch to another branch.")
@click.argument("target_branch")
@click.pass_obj
def checkout(repo, target_branch):
    """Switch branches and rewrite tracked files from branch state.

    This is analogous to `git checkout BRANCH`.
    If the branch does not exist, it is created from the current branch.
    Only hallmark-tracked files are rewritten; unrelated files are left
    alone unless they block restoration of a tracked path.
    """
    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(*_REPO_READ_ERRORS):
        # attempt to switch to the target branch, handling any errors
        switched = repo.checkout(target_branch)

    if switched:
        click.echo(f'Switched to branch "{target_branch}".')


@hallmark.group(short_help="Manage hallmark worktrees.")
def worktree():
    """Manage the worktrees linked to this hallmark repository.

    A worktree is a data directory backed by its own `.hm` git worktree,
    so several branches can be checked out at the same time.
    """


@worktree.command("list", short_help="List linked hallmark worktrees.")
@click.pass_obj
def worktree_list(repo):
    """List the worktrees linked to this hallmark repository.

    The active worktree is marked with `*`.
    """
    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(*_REPO_READ_ERRORS):
        entries = repo.list_worktrees()

    for entry in entries:
        prefix = "*" if entry["current"] else " "
        # show the data directory when there is one; a bare repository has
        # only its ".hm" path to report
        location = entry["path"] or entry["dothm"]
        branch = entry["branch"] or "(detached)"
        click.echo(f"{prefix} {location}  [{branch}]")


@hallmark.command(
    short_help="Download files from the configured data remote.")
@click.argument("files", nargs=-1)
@click.option(
    "--tsv",
    "tsv_names",
    multiple=True,
    help="Download every file represented by a configured TSV. "
         "May be repeated.")
@click.option(
    "--all",
    "download_all",
    is_flag=True,
    help="Download every configured TSV, static file, and metadata file.")
@click.option(
    "--remote",
    "remote_name",
    help="Name of the configured remote to use.")
@click.option(
    "--output",
    type=click.Path(file_okay=False),
    help="Output directory. Defaults to the repository worktree.")
@click.option(
    "--max-workers",
    type=click.IntRange(min=1),
    default=4,
    show_default=True,
    help="Number of concurrent downloads.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the selected files without downloading them.")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip the bulk-download confirmation.")
@click.pass_obj
def download(repo, files, tsv_names, download_all, remote_name, output, max_workers,
             dry_run, yes):
    """
    Download files from the configured data remote. Options allow for selecting
    specific files, TSVs, or downloading all files. Supports concurrent downloads
    and dry-run mode for previewing selected files.

    Options:
        --tsv: Download every file represented by a configured TSV. May be repeated.
        --all: Download every configured TSV, static file, and metadata file.
        --remote: Name of the configured remote to use.
        --output: Output directory. Defaults to the repository worktree.
        --max-workers: Number of concurrent downloads. Default is 4.
        --dry-run: Show the selected files without downloading them.
        -y, --yes: Skip the bulk-download confirmation.

    Raises:
        ClickException: If there are any issues with the provided arguments or
            during the download process.
    """
    # do not allow --all to be combined with file paths or --tsv
    if download_all and (files or tsv_names):
        raise ClickException("--all cannot be combined with file paths or --tsv")
    # require at least one of file paths, --tsv, or --all to be provided
    if not files and not tsv_names and not download_all:
        raise ClickException("Provide one or more file paths, --tsv, or --all")

    # if an output directory is specified, use it; otherwise, use the repo worktree
    if output:
        output_path = Path(output).expanduser()
    elif repo.worktree is not None:
        output_path = Path(repo.worktree)
    # output is required when downloading from a bare .hm repository
    else:
        raise ClickException(
            "--output is required when downloading from a bare .hm repository")

    # use the _translate_cli_errors context manager to handle DownloadError exceptions
    with _translate_cli_errors(DownloadError):
        selected_files = select_download_files(
            repo,
            file_paths=files,
            tsv_names=tsv_names,
            all_files=download_all)
        click.echo(f"Selected {len(selected_files)} file(s) "f"for {output_path}")

        # don't actually download the files if --dry-run is specified
        if dry_run:
            # limit the number of files to preview to avoid overwhelming the user
            preview_limit = 20
            # for each selected file, print its relative path to the user
            for rel_path, _sha1 in selected_files[:preview_limit]:
                click.echo(f"  {rel_path.as_posix()}")

            remaining = len(selected_files) - preview_limit
            # if there are more files than the preview limit
            if remaining > 0:
                # indicate how many more files are selected
                click.echo(f"  ... {remaining} more file(s)")
            # bail out of the function early since this is a dry run
            return

        if not selected_files:
            click.echo("No files selected for download.")
            # bail out of the function early since there are no files to download
            return

        # attempt to download the selected files, handling any errors
        _run_download(
            repo,
            selected_files,
            output_path,
            max_workers=max_workers,
            assume_yes=yes,
            remote_name=remote_name)


@hallmark.command(short_help="Clone a hallmark repository from a remote URL.")
@click.argument("url")
@click.argument("path")
@click.option(
    "--no-fetch-data",
    is_flag=True,
    help="Skip downloading remote data files after clone.")
@click.option(
    "--max-workers",
    type=click.IntRange(min=1),
    default=4,
    show_default=True,
    help="Number of concurrent downloads.")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip the bulk-download confirmation.")
def clone(url, path, no_fetch_data, max_workers, yes):
    """
    Clone a hallmark repository from a remote URL to the specified path.

    Options:
        --no-fetch-data: Skip downloading remote data files after clone.
        --max-workers: Number of concurrent downloads. Default is 4.
        -y, --yes: Skip the bulk-download confirmation.

    Raises:
        ClickException: If there are any issues with the provided arguments or
            during the clone process, such as a destination already existing,
            a clone error, or a download error.
    """
    # use context manager to handle DownloadError and GitError exceptions
    with _translate_cli_errors(DownloadError, GitError):
        try:
            repo = Repo.clone(url, path, fetch_data=False)
        # handle CloneError exceptions and provide a user-friendly error message
        except CloneError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1) from exc
        click.echo(f'Successfully cloned to "{path}"')
        # if the user has not opted to skip data fetching
        if not no_fetch_data:
            # get the worktree path of the cloned repository
            _, worktree_path = Repo.lwpaths(path)
            if worktree_path is None:
                click.echo(
                    "Bare repository clone; skipping data download.")
                # bail out of the function early since there is no worktree
                return

            # select the files to download from the cloned repository
            selected_files = select_download_files(repo, all_files=True)
            # if there are no selected files, inform the user and exit
            if not selected_files:
                click.echo("No remote data files are configured.")
                return

            # confirm with user if the num of selected files exceeds warning threshold
            click.echo("Downloading remote data files...")
            _run_download(
                repo,
                selected_files,
                worktree_path,
                max_workers=max_workers,
                assume_yes=yes)


@hallmark.command(short_help="Build a hallmark repository from a remote dataset.")
@click.argument("directory")
@click.argument("dataset_name")
@click.option(
    "--remote", "remotes", multiple=True,
    help="Remote to record, as NAME=URL or just NAME. May be repeated "
         "for multiple remotes.")
@click.option(
    "--config-file", "config_file",
    type=click.Path(exists=True, dir_okay=True, file_okay=True),
    help="Path to config.yml or a repository directory containing config.yml. "
         "build_repo loads fmts (and remotes unless --remote is provided).")
@click.option(
    "--fmt", "fmts", multiple=True,
    help="A fmt entry to use directly, as FMT=DB (e.g. "
         "'a{a}_i{i}.h5=data.tsv'). May be repeated for multiple fmts; "
         "skips the prompt entirely.")
@click.option(
    "--overwrite",
    is_flag=True,
    help="Replace the destination repository if it already exists.")
def build(directory, dataset_name, remotes, config_file, fmts, overwrite):
    """
    Build a hallmark repository at DIRECTORY for the remote dataset DATASET_NAME.

    The dataset is fetched from the remote index and stored in a new hallmark
    repository at DIRECTORY. The remotes can be specified with
    --remote NAME=URL or --remote NAME.
    if no remotes are specified, the default remote from the dataset index will be used.
    --config-file: Optional path to an existing config.yml to load fmts and remotes
    --fmt: Optional fmt entries to use directly, specified as FMT=DB. May be repeated
    for multiple fmts.
    --overwrite: Optional flag to replace the destination repo if it already exists.

    Arguments:

        DIRECTORY: The file system path where the hallmark repository will be created.
        DATASET_NAME: The name of the remote dataset to fetch.
        --remote: Optional remote(s) to record, specified as NAME=URL or just NAME.
        May be repeated for multiple remotes.
        --config-file: Optional path to an existing config.yml to load fmts and remotes.
        --fmt: Optional fmt entries to use directly, specified as FMT=DB.

    Raises:
        ClickException: If there is an error during the build process, such as
        a network error, Git error, or invalid dataset name.

    """
    if config_file and fmts:
        raise ClickException("Use only one of --config-file or --fmt, not both.")
    # validate the dataset name to ensure it is a valid path component
    with _translate_cli_errors(ValueError):
        dataset_name = validate_path_component(dataset_name, label="dataset name")

    repo_path = Path(directory) / f"{dataset_name}.hm"
    parsed_remotes = []
    for entry in remotes:
        # if the remote entry contains an "=", it is in the form NAME=URL
        if "=" in entry:
            name, url = entry.split("=", 1)
            parsed_remotes.append({"name": name, "url": url})
        else:
            parsed_remotes.append({"name": entry})

    fmt_entries = None
    if fmts:
        fmt_entries = []
        for entry in fmts:
            # if the fmt entry does not contain an "=", it is invalid
            if "=" not in entry:
                raise ClickException(
                    f"--fmt values must use FMT=DB, got {entry!r}.")
            # split the fmt entry into its format and database name components
            fmt, db = entry.rsplit("=", 1)
            fmt = fmt.strip()
            # Validate that the fmt is not empty or whitespace-only
            if not fmt:
                raise ClickException("--fmt must define a non-empty format")
            # normalize the db name to ensure it is valid and ends with ".tsv"
            try:
                with _translate_cli_errors(*_BUILD_DATASET_ERRORS):
                    db = normalize_tsv_name(db)
            # handle any network-related exceptions raised
            except requests.exceptions.RequestException as exc:
                raise ClickException(
                    f"Failed to reach dataset {dataset_name!r}: {exc}") from exc

            # if the checks pass, append the fmt and db to the fmt_entries list
            fmt_entries.append({"fmt": fmt, "db": db})

    # build the hallmark repository with the specified parameters
    try:
        with _translate_cli_errors(*_BUILD_DATASET_ERRORS):
            build_repo(
                repo_path=repo_path,
                dataset_name=dataset_name,
                fmt_entries=fmt_entries,
                config_file=config_file,
                remotes=parsed_remotes or None,
                overwrite=overwrite)
    except requests.exceptions.RequestException as exc:
        raise ClickException(
            f"Failed to reach dataset {dataset_name!r}: {exc}") from exc

    click.echo(f'Successfully built hallmark repository at "{repo_path}".')