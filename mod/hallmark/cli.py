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
from .downloader import DownloadError
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
    DownloadError,
    RuntimeError,
    ValueError,
    FileNotFoundError,
    FileExistsError,
    GitError,
    yaml.YAMLError)


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


def _run_download(repo, plan, *, max_workers):
    """Approve and execute exactly the plan displayed by clone or download."""
    click.echo(plan.summary())
    if not plan.file_count:
        click.echo("No files selected for download.")
        return
    click.confirm("Download these files?", default=False, abort=True)
    results = repo.download(plan, approved=True, max_workers=max_workers,
                            progress=True)
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
@click.option("--remote-auth", help="Local SSH profile; empty string removes it.")
@click.option("--encoding", "encodings", multiple=True)
@click.pass_obj
def set_config(repo, fmt, remote_name, remote_url, remote_auth, encodings):
    """Update the current branch config.yml."""
    # if no config changes are requested, raise a ClickException to inform the user
    if (
    fmt is None and remote_name is None and remote_url is None
    and remote_auth is None and not encodings):
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
            encoding_updates=encoding_updates or None,
            **({"remote_auth": remote_auth} if remote_auth is not None else {}))

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


@hallmark.command(short_help="Plan and approve a dataset download.")
@click.argument("files", nargs=-1)
@click.option("--tsv", "tsv_names", multiple=True,
              help="Select a catalog TSV. May be repeated.")
@click.option("--all", "download_all", is_flag=True,
              help="Select all cataloged files.")
@click.option("--filter", "filters", multiple=True,
              help="Relative path glob; ** matches recursively. May be repeated.")
@click.option("--fmt", help="Select a parameterized filename template.")
@click.option("--remote", "remote_name", help="Configured data remote name.")
@click.option("--output", type=click.Path(file_okay=False),
              help="Output directory; defaults to the worktree.")
@click.option("--max-workers", type=click.IntRange(min=1), default=4,
              show_default=True)
@click.option("--dry-run", is_flag=True,
              help="Show the offline transfer plan without downloading.")
@click.option("-y", "--yes", is_flag=True, hidden=True)
@click.pass_obj
def download(repo, files, tsv_names, download_all, filters, fmt, remote_name,
             output, max_workers, dry_run, yes):
    """Inspect sizes and approve every nonempty dataset transfer."""
    if download_all and (files or tsv_names):
        raise ClickException("--all cannot be combined with file paths or --tsv")
    if not files and not tsv_names and not download_all and not filters and not fmt:
        raise ClickException("Provide file paths, --tsv, --all, --filter, or --fmt")
    if repo.worktree is None and output is None:
        raise ClickException("--output is required when downloading from a bare repo")
    if yes:
        click.echo("--yes is deprecated; downloads still require confirmation.",
                   err=True)
    with _translate_cli_errors(DownloadError, ValueError):
        plan = repo.plan_download(
            output, file_paths=files, tsv_names=tsv_names, all_files=download_all,
            filter=filters or None, fmt=fmt, remote_name=remote_name)
        if dry_run:
            click.echo(plan.summary())
            for item in plan.items[:20]:
                click.echo(f"  {item.relative_path.as_posix()}")
            if plan.file_count > 20:
                click.echo(f"  ... {plan.file_count - 20} more file(s)")
            return
        _run_download(repo, plan, max_workers=max_workers)


@hallmark.command(short_help="Clone a catalog or discover a remote dataset.")
@click.argument("url")
@click.argument("path")
@click.option("--auth", help="Optional local SSH authentication profile.")
@click.option("--filter", "filters", multiple=True,
              help="Relative path glob; ** matches recursively. May be repeated.")
@click.option("--fmt", help="Parameterized filename template to catalog.")
@click.option("--source-type", default="auto", show_default=True,
              type=click.Choice(["auto", "git", "directory", "catalog"]),
              help="Override automatic source detection.")
@click.option("--download", "fetch_data", is_flag=True,
              help="Plan and request approval for downloads after catalog creation.")
@click.option("--no-fetch-data", is_flag=True, hidden=True)
@click.option("--max-workers", type=click.IntRange(min=1), default=4,
              show_default=True)
@click.option("-y", "--yes", is_flag=True, hidden=True)
def clone(url, path, auth, filters, fmt, source_type, fetch_data, no_fetch_data,
          max_workers, yes):
    """Prepare local .hm metadata; dataset files are not downloaded by default."""
    if fetch_data and no_fetch_data:
        raise ClickException("--download conflicts with --no-fetch-data")
    if yes:
        click.echo("--yes is deprecated; downloads still require confirmation.",
                   err=True)
    with _translate_cli_errors(DownloadError, GitError, ValueError):
        try:
            repo = Repo.clone(url, path, auth=auth, filter=filters or None, fmt=fmt,
                              source_type=source_type, progress=True,
                              max_workers=max_workers)
        except CloneError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1) from exc
        click.echo(f'Successfully cloned to "{path}"')
        if fetch_data:
            if repo.worktree is None:
                raise ClickException("Use a worktree destination for --download")
            _run_download(repo, repo.plan_download(), max_workers=max_workers)



@hallmark.command(short_help="Deprecated: use clone for remote datasets.")
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
@click.option("--dataset-url",
              help="Exact crawl root, independent of recorded remotes.")
@click.option("--dataset-auth", help="Local SSH profile for the crawl source.")
@click.option("--index-format", type=click.Choice(["cyverse-html"]), hidden=True)
@click.option("--allow-remote-commands", is_flag=True,
              hidden=True, help="Deprecated; discovery uses SFTP.")
@click.option("--remote-hash", is_flag=True,
              hidden=True, help="Unsupported; discovery does not hash dataset files.")
def build(directory, dataset_name, remotes, config_file, fmts, overwrite,
          dataset_url, dataset_auth, index_format, allow_remote_commands, remote_hash):
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
    click.echo("build is deprecated; use hallmark clone URL PATH.", err=True)
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

    source_options = {key: value for key, value in {
        "dataset_url": dataset_url, "dataset_auth": dataset_auth,
        "index_format": index_format, "allow_remote_commands": allow_remote_commands,
        "remote_hash": remote_hash}.items() if value is not None and value is not False}
    # build the hallmark repository with the specified parameters
    try:
        with _translate_cli_errors(*_BUILD_DATASET_ERRORS):
            build_repo(
                repo_path=repo_path,
                dataset_name=dataset_name,
                fmt_entries=fmt_entries,
                config_file=config_file,
                remotes=parsed_remotes or None,
                overwrite=overwrite, **source_options)
    except requests.exceptions.RequestException as exc:
        raise ClickException(
            f"Failed to reach dataset {dataset_name!r}: {exc}") from exc

    click.echo(f'Successfully built hallmark repository at "{repo_path}".')