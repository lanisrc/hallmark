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
import yaml
from click import ClickException
from git.exc import GitError

from . import Repo
from .remote.download import DownloadError
from .remote.discovery import path_matches
from .error import CheckoutError, CloneError


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
    """
    Display a download plan, request approval, and report the results.

    Args:
        repo: The hallmark repository object.
        plan (DownloadPlan): Files, source, and destination to display.
        max_workers (int): Maximum concurrent download workers.

    Raises:
        Abort: If a nonempty download is declined.
        ClickException: If any file transfers fail.
        DownloadError: If the download cannot be started.
    """
    click.echo(plan.summary())
    if not plan.file_count:
        click.echo("No files selected for download.")
        return
    click.confirm("Download these files?", default=False, abort=True)
    results = repo.download(plan, approved=True, max_workers=max_workers,
                            progress=True)
    _report_download_results(results)


@click.group(name="hm")
@click.version_option()
@click.pass_context
def hallmark(ctx):
    """Reproducibility is the hallmark of the scientific method.

    Hallmark is a lightweight package designed to version control and
    manage data products in a complex workflow.
    """
    # if the invoked subcommand is one of the commands that does not require a repo
    if ctx.invoked_subcommand in [None, "init", "clone"]:
        # return early without attempting to open a repository
        return
    # attempt to open the hallmark repository in the current directory
    with _translate_cli_errors(GitError, prefix="Failed to open hallmark repository"):
        # cwd is the current working directory
        cwd = Path.cwd()
        # start with the current working directory as the default repository path
        repo_path = cwd
        if cwd.name != ".hm":
            # search for the nearest parent directory containing a ".hm" folder
            repo_path = next((path for path in (cwd, *cwd.parents)
                              if (path / ".hm").exists()), cwd)
        # ctx is the Click context object that holds the repository instance in ctx.obj
        ctx.obj = Repo(repo_path)


def _load_backend_options(path):
    """Read a backend's configuration from a YAML mapping."""
    if path is None:
        return None
    with Path(path).open(encoding="utf-8") as handle:
        options = yaml.safe_load(handle)
    if not isinstance(options, dict):
        raise ValueError("Backend options file must contain a YAML mapping")
    return options


@hallmark.command(short_help="Initialize an empty local repository.")
@click.argument("path", default=".")
def init(path):
    with _translate_cli_errors(
        GitError, ValueError, OSError,
        prefix=f'Failed to initialize hallmark repository at "{path}"'):
        Repo.init(path)
    click.echo(f'Successfully initialized "{path}"')


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

    def print_section(title, entries, fg):
        if not any(paths for _, paths in entries):
            return
        click.echo("")
        click.secho(title, fg=fg)
        for label, paths in entries:
            for path in paths:
                click.echo("  " + click.style(f"{label}:   {path}", fg=fg))

    print_section(
        "Changes to be committed:",
        [
            ("state", staged["state"]),
            ("new file", staged["added"]),
            ("modified", staged["modified"]),
            ("deleted", staged["deleted"]),
        ],
        "green",
    )
    print_section(
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
        if snapshot.get("remote_catalog"):
            click.echo("nothing to commit, remote catalog unchanged")
        else:
            click.echo("nothing to commit, working tree clean")


@hallmark.command(short_help="Add files to hallmark index.")
@click.option(
    "--regex",
    "encoding",
    is_flag=True,
    default=False,
    show_default=True,
    help="Enable regex-based encoding rules from config.yml.")
@click.option("--auth", help="Local SSH authentication profile.")
@click.option("--backend", help="Registered remote data backend.")
@click.option("--backend-options", type=click.Path(exists=True, dir_okay=False),
              help="YAML mapping of backend-specific options.")
@click.option("--filter", "filters", multiple=True, help="Select remote paths by glob.")
@click.option("--fmt", "remote_fmt",
              help="Remote filename format; otherwise use a URL pattern.")
@click.argument("inputs", nargs=-1, required=True)
@click.pass_obj
def add(repo, encoding, inputs, auth, backend, backend_options, filters, remote_fmt):
    """Add files to the hallmark index.

    A remote URL or URL pattern stages a catalog without downloading files
    or committing. Use --fmt and --filter to select remote paths.

    `hm add [--regex] FORMAT` uses the branch format string workflow.
    `hm add "."` rebuilds the manifest from current files that match
    the branch `fmt` in `config.yml`.
    Files and directories are checked against the saved branch pattern.
    """
    with _translate_cli_errors(ValueError, OSError, yaml.YAMLError):
        options = _load_backend_options(backend_options)
    remote_options = dict(auth=auth, backend=backend,
                          backend_options=options,
                          filter=filters or None, remote_fmt=remote_fmt)
    if len(inputs) != 1 and any(value is not None for value in remote_options.values()):
        raise ClickException("Remote add accepts one URL at a time")
    # attempt to add the specified files to the hallmark index, handling any errors
    with _translate_cli_errors(RuntimeError, ValueError, OSError, yaml.YAMLError):
        # if there is only one input, use the add method for a single input
        if len(inputs) == 1:
            remote = "://" in inputs[0]
            if remote or any(value is not None for value in remote_options.values()):
                pf = repo.add(inputs[0], encoding, progress=True, **remote_options)
            else:
                pf = repo.add(inputs[0], encoding)
        # oterhwise, use the add_paths method for multiple inputs
        else:
            pf = repo.add_paths(list(inputs), encoding=encoding)

    if pf.empty:
        click.echo("No files matched the format string.")
    else:
        click.echo("Changes to be committed")
        click.echo(pf.path.to_string(index=False, header=False))

    # display unmatched files that were skipped
    unmatched = pf.attrs.get("unmatched", [])
    if unmatched:
        click.echo("Skipped files that do not match the branch pattern:")
        # print each unmatched path
        for path in unmatched:
            click.echo(f"  {path}")


@hallmark.command(short_help="Unstage paths without changing files.")
@click.option("--staged", is_flag=True)
@click.argument("paths", nargs=-1)
@click.pass_obj
def restore(repo, staged, paths):
    """
    Restore staged files to their state in the HEAD commit.
    Only supports restoring staged files for now, will add other support later.

    Args:
        repo: Repository instance.
        staged: Boolean flag indicating if the restore is for staged files.
        paths: List of file paths to restore.

    Raises:
        ClickException: If the --staged flag is not provided or no paths are specified.
    """
    if not staged:
        raise ClickException(
            "Only hm restore --staged <path> works for now."
        )
    if not paths:
        raise ClickException(
            "Usage: hm restore --staged <path> [<path> ...]"
        )
    with _translate_cli_errors(*_REPO_READ_ERRORS, OSError):
        repo.restore_staged(list(paths))


@hallmark.command("set-config", short_help="Update hallmark branch config.")
@click.option("--fmt")
@click.option("--remote-name")
@click.option("--remote-url")
@click.option("--remote-auth",
              help="Local SSH profile name. An empty string removes the reference.")
@click.option("--remote-backend", help="Registered data backend name.")
@click.option("--remote-backend-options", type=click.Path(exists=True, dir_okay=False),
              help="YAML mapping of backend-specific options.")
@click.option("--encoding", "encodings", multiple=True)
@click.pass_obj
def set_config(repo, fmt, remote_name, remote_url, remote_auth, remote_backend,
               remote_backend_options, encodings):
    """Update the current branch config.yml."""
    # if no config changes are requested, raise a ClickException to inform the user
    if (
    fmt is None and remote_name is None and remote_url is None
    and remote_auth is None and remote_backend is None
    and remote_backend_options is None and not encodings):
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
    with _translate_cli_errors(RuntimeError, ValueError, OSError, yaml.YAMLError):
        options = _load_backend_options(remote_backend_options)
        repo.set_config(
            fmt=fmt,
            remote_name=remote_name,
            remote_url=remote_url,
            encoding_updates=encoding_updates or None,
            remote_auth=remote_auth,
            remote_backend=remote_backend,
            remote_backend_options=options)

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


@hallmark.command(short_help="Download files from the configured data remote.")
@click.argument("files", nargs=-1)
@click.option("--tsv", "tsv_names", multiple=True,
              help="Select a catalog TSV. May be repeated.")
@click.option("--all", "download_all", is_flag=True,
              help="Select all cataloged files.")
@click.option("--filter", "filters", multiple=True,
              help="Select paths matching a glob. ** matches recursively. "
                   "May be repeated.")
@click.option("--fmt", help="Select paths matching a filename format.")
@click.option("--remote", "remote_name",
              help="Name of the configured data remote to use.")
@click.option("--output", type=click.Path(file_okay=False),
              help="Output directory. Defaults to the repository worktree.")
@click.option("--max-workers", type=click.IntRange(min=1), default=4,
              show_default=True)
@click.option("--dry-run", is_flag=True,
              help="Show the download plan using only local catalog metadata.")
@click.pass_obj
def download(repo, files, tsv_names, download_all, filters, fmt, remote_name,
             output, max_workers, dry_run):
    """
    Download selected files from a configured data remote.

    Preview the selection with --dry-run. Every nonempty download displays
    its plan and asks for confirmation before transferring files.
    """
    if download_all and (files or tsv_names):
        raise ClickException("--all cannot be combined with file paths or --tsv")
    if not files and not tsv_names and not download_all and not filters and not fmt:
        raise ClickException("Provide file paths, --tsv, --all, --filter, or --fmt")
    if repo.worktree is None and output is None:
        raise ClickException("--output is required when downloading from a bare repo")
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


@hallmark.command(short_help="Clone an existing Hallmark catalog.")
@click.argument("url")
@click.argument("path")
@click.option("--auth", help="Optional local SSH authentication profile.")
@click.option("--filter", "filters", multiple=True,
              help="Select paths matching a glob. ** matches recursively. "
                   "May be repeated.")
@click.option("--fmt",
              help="Select downloads using a filename format; "
                   "cannot be combined with --no-download.")
@click.option("--source-type", default="auto", show_default=True,
              type=click.Choice(["auto", "git", "catalog"]),
              help="Override automatic source detection.")
@click.option("--no-download", is_flag=True,
              help="Clone only the catalog and history, without downloading data.")
@click.option("--max-workers", type=click.IntRange(min=1), default=4,
              show_default=True)
def clone(url, path, auth, filters, fmt, source_type, no_download, max_workers):
    """
    Clone an existing Git catalog or published catalog snapshot at PATH.

    By default, copy the catalog then ask before downloading dataset files.
    Declining keeps the catalog and exits successfully. --no-download skips data
    and the prompt; bare destinations require it. --filter and --fmt narrow the
    download and cannot be combined with --no-download. The complete catalog and
    Git history are preserved. Use init followed by add for raw datasets.
    """
    if (filters or fmt is not None) and no_download:
        raise ClickException("--filter and --fmt cannot be used with --no-download")
    if not no_download and Repo.resolve_repo_paths(path)[1] is None:
        raise ClickException("Bare clones require --no-download")

    def approve(plan):
        click.echo(plan.summary())
        return click.confirm("Download these files?", default=False)

    with _translate_cli_errors(DownloadError, GitError, ValueError):
        path_matches("validation", filter=filters or None, fmt=fmt)
        try:
            repo = Repo.clone(url, path, auth=auth,
                              source_type=source_type, progress=True,
                              download=not no_download, approve=approve,
                              filter=filters or None, fmt=fmt,
                              max_workers=max_workers)
        except CloneError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1) from exc
        click.echo(f'Successfully cloned to "{path}"')
        if not no_download and repo.download_result is None:
            click.echo(f'Skipped download; run hm download --all from "{path}" later.')
        if repo.download_result is not None:
            if repo.download_result["succeeded"] + repo.download_result["failed"] == 0:
                click.echo("No files selected for download.")
            else:
                _report_download_results(repo.download_result)
