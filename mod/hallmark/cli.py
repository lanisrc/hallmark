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
import shlex
import sys

import click
import requests
import yaml
from click import ClickException
from git.exc import GitError

from . import Repo
from .helper_functions import validate_path_component
from .repo_builder import build_repo
from .downloader import DownloadError, _select_download_items, _select_remote_config
from .sources import get_source, list_sources
from .error import CheckoutError, CloneError
from .discovery import is_remote_url
from .repo_config import catalog_entries, normalize_tsv_name


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


def _show_download_plan(plan, *, show_paths=False):
    """Display recorded sizes without querying the data server."""
    click.echo(plan.summary())
    if show_paths:
        click.echo(f"Files with unknown size: {plan.unknown_size_count}")
        for item in plan.items[:20]:
            size = (f"{item.size_bytes:,} bytes" if item.size_bytes is not None
                    else "size unknown")
            click.echo(f"  {item.relative_path.as_posix()} ({size})")
        if plan.file_count > 20:
            click.echo(f"  ... {plan.file_count - 20} more file(s)")


def _execute_download(repo, plan, *, max_workers):
    """Execute the exact plan the user approved and report transfer failures."""
    results = repo.download(plan, approved=True, max_workers=max_workers,
                            progress=True)
    _report_download_results(results)


def _confirm_download_plan(plan, *, decline_is_skip=False, show_paths=False):
    """Display a plan and approve it explicitly, optionally treating refusal as skip."""
    _show_download_plan(plan, show_paths=show_paths)
    if not plan.file_count:
        click.echo("No files selected for download.")
        return False
    return click.confirm("Download these files?", default=False,
                         abort=not decline_is_skip)


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
    if _confirm_download_plan(plan):
        _execute_download(repo, plan, max_workers=max_workers)


def _download_output(repo, output):
    """Prompt for the missing destination of a bare catalog without creating it."""
    if repo.worktree is None and output is None:
        return click.prompt(
            "Output directory (relative to the current directory)",
            type=click.Path(file_okay=False))
    return output


def _choose_download_plan(repo, *, output, remote_name, dry_run):
    """Return an approved plan, or None after skipping or an offline preview."""
    while True:
        choice = click.prompt(
            "Select files", type=click.Choice(["patterns", "all", "skip"],
                                             case_sensitive=False), default="skip")
        if choice == "skip":
            return None
        patterns = []
        if choice == "patterns":
            click.echo("Enter one relative-path glob per line, without shell quotes. "
                       "Blank finishes; patterns are ORed. ** matches recursively.")
            while True:
                pattern = click.prompt("Pattern", default="", show_default=False)
                if not pattern:
                    break
                patterns.append(pattern)
            if not patterns:
                continue
        output = _download_output(repo, output)
        plan = repo.plan_download(output, all_files=choice == "all",
                                  include=patterns or None, remote_name=remote_name)
        _show_download_plan(plan, show_paths=True)
        if not plan.file_count:
            click.echo("No cataloged files match. Choose another selection or skip.")
            continue
        if dry_run:
            return None
        action = click.prompt(
            "Next action", type=click.Choice(["download", "change", "skip"],
                                             case_sensitive=False), default="skip")
        if action == "download":
            return plan
        if action == "skip":
            return None


def _download_available(repo, remote_name=None):
    """Explain an empty catalog or absent remote without contacting the server."""
    remote = _select_remote_config(repo, remote_name)
    template_urls = remote_name is None and any(
        entry.is_remote for entry in catalog_entries(repo.state.config))
    if not template_urls and (not remote or not remote.get("url")):
        click.echo("No data remote is configured; skipping download. "
                   "Configure one with hallmark set-config --remote-url URL.")
        return False
    if not _select_download_items(repo, all_files=True, catalog_only=True):
        click.echo("The catalog is empty; nothing to download.")
        return False
    return True


def _interactive_download(repo, *, output=None, remote_name=None, max_workers=4,
                          dry_run=False):
    """Select and approve cataloged files in a terminal, without network preflight."""
    if not sys.stdin.isatty():
        raise ClickException(
            "--interactive requires a terminal. Use --include 'PATTERN' --dry-run "
            "to preview a selection; run in a terminal to approve a download.")
    if not _download_available(repo, remote_name):
        return
    click.echo("Catalog ready. Downloading dataset files can consume substantial "
               "bandwidth and disk space. Nothing transfers until you approve "
               "a plan. Size estimates use recorded catalog metadata.")
    try:
        plan = _choose_download_plan(repo, output=output, remote_name=remote_name,
                                     dry_run=dry_run)
    except click.Abort as exc:
        # Click wraps both EOF and Ctrl+C in Abort. EOF skips this optional
        # step; retain Click's interrupt behavior for Ctrl+C.
        if not isinstance(exc.__context__, EOFError):
            raise
        plan = None
    if plan is None:
        click.echo("No download started; the catalog is unchanged.")
        return
    # Outside the prompt handler: transfer errors must never become a skip.
    _execute_download(repo, plan, max_workers=max_workers)


def _offer_download(repo, *, includes=(), output=None, interactive=False):
    """Review downloads after cloning, or print equivalent commands for later."""
    if not sys.stdin.isatty():
        click.echo("Warning: download approval is unavailable without "
                   "a terminal. The catalog is ready; download was skipped.", err=True)
        try:
            _download_available(repo)
        except (DownloadError, ValueError) as exc:
            # A skipped handoff cannot fail successful catalog creation, even
            # when later downloading will require extra configuration.
            click.echo(f"Before downloading: {exc}", err=True)
        directory = repo.worktree if repo.worktree is not None else repo.dothm.path
        arguments = ["hallmark", "download"]
        if includes:
            for pattern in includes:
                arguments.extend(["--include", pattern])
        else:
            arguments.append("--all")
        if output is not None:
            # The example changes directory. Preserve --output's meaning relative
            # to the original invocation, as plan_download does.
            arguments.extend(["--output", str(Path(output).expanduser().resolve())])
        command = shlex.join(arguments)
        needs_output = repo.worktree is None and output is None
        if needs_output:
            command += " --output '/path/to/downloads'"
        click.echo("Preview this selection locally; use --include 'PATTERN' "
                   "instead of --all to narrow it:")
        click.echo(f"  cd -- {shlex.quote(str(directory))}")
        click.echo(f"  {command} --dry-run")
        click.echo("Run the download in a terminal; explicit approval is required:")
        click.echo(f"  {command}")
        if needs_output:
            click.echo("Replace /path/to/downloads with your output directory.")
        return
    with _translate_cli_errors(DownloadError, ValueError, OSError,
                               prefix="Catalog created; download failed"):
        if interactive:
            _interactive_download(repo, output=output)
            return
        if not _download_available(repo):
            return
        click.echo("Catalog ready. Review the recorded sizes before downloading; "
                   "transfers can consume substantial bandwidth and disk space.")
        try:
            output = _download_output(repo, output)
            plan = repo.plan_download(output, all_files=True, include=includes or None)
            approved = _confirm_download_plan(
                plan, decline_is_skip=True, show_paths=True)
        except click.Abort as exc:
            if not isinstance(exc.__context__, EOFError):
                raise
            approved = False
        if not approved:
            click.echo("No download started; the catalog is unchanged.")
            return
        # Keep transfer errors and interrupts outside the prompt's EOF handler.
        _execute_download(repo, plan, max_workers=4)


@click.group()
@click.version_option()
@click.pass_context
def hallmark(ctx):
    """Reproducibility is the hallmark of the scientific method.

    Hallmark is a lightweight package designed to version control and
    manage data products in a complex workflow.
    """
    # if the invoked subcommand is one of the commands that does not require a repo
    if ctx.invoked_subcommand in [
            None, "init", "clone", "build", "sources", "ls-remote"]:
        # return early without attempting to open a repository
        return
    # attempt to open the hallmark repository in the current directory
    with _translate_cli_errors(
            GitError, ValueError, prefix="Failed to open hallmark repository"):
        ctx.obj = Repo(".")


@hallmark.command(short_help="List named data sources and available collections.")
@click.argument("name", required=False)
def sources(name):
    """Show supported sources, releases and collection roots without crawling."""
    with _translate_cli_errors(ValueError):
        descriptors = [get_source(name)] if name else list_sources()
        for source in descriptors:
            click.echo(f"{source.name}: {source.description}")
            for release, definition in source.releases.items():
                click.echo(f"  {release}: {definition.description or release}")
                click.echo(f"    {definition.url}")
                if name:
                    for collection, roots in definition.collections.items():
                        description = definition.collection_descriptions.get(
                            collection, "")
                        click.echo(f"    {collection}: {description}")
                        for root in roots:
                            click.echo(f"      {definition.url.rstrip('/')}/{root}/")
            if name:
                click.echo("  Run hallmark ls-remote URL to find filename templates, "
                           "then hallmark add 'URL/TEMPLATE'.")


@hallmark.command(short_help="Initialize an empty local repository.")
@click.argument("path")
def init(path):
    """Initialize an empty hallmark repository at PATH.

    Plain init creates an empty repository without scanning existing files.
    Catalog files afterwards with hallmark add: a filename template such as
    `a{a}_i{i}.h5` tracks local files, and a URL template such as
    `https://host/ER2/{src}_{day}.h5` catalogs remote files without
    downloading them.
    A PATH ending in .hm creates a bare catalog; otherwise metadata lives in PATH/.hm.
    """
    with _translate_cli_errors(
        GitError, ValueError, OSError, yaml.YAMLError,
        prefix=f'Failed to initialize hallmark repository at "{path}"'):
        Repo.init(path)


@hallmark.command("ls-remote",
                  short_help="List a remote directory and suggest templates.")
@click.argument("url")
def ls_remote(url):
    """List the files at a remote directory URL and suggest URL templates.

    Nothing is downloaded and no repository is changed or required. Each
    suggestion is a command that catalogs the matching files.
    """
    from .repo_remote import suggest_templates

    with _translate_cli_errors(DownloadError, ValueError):
        base, suggestions, total = suggest_templates(url, progress=True)
    click.echo(f"{total} file(s) at {base}")
    if not suggestions:
        click.echo("No filename templates detected. Write one with {field} "
                   "placeholders and preview it with hallmark add -n 'URL/TEMPLATE'.")
        return
    click.echo("Suggested templates (files matched):")
    for template, count in suggestions:
        click.echo(f"  hallmark add {shlex.quote(template)}  ({count})")


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
        if not any(paths for _, paths in entries):
            return
        click.echo("")
        click.secho(title, fg=fg)
        for label, paths in entries:
            for path in paths:
                click.echo("  " + click.style(f"{label}:   {path}", fg=fg))

    catalog_lines = []
    for change in staged.get("catalog", []):
        source = f' from {change["url"]}' if change["url"] else ""
        catalog_lines.append(
            f'{", ".join(change["templates"])}{source}: '
            f'{change["added"]} new, {change["modified"]} modified, '
            f'{change["deleted"]} removed')
    emit_section(
        "Changes to be committed:",
        [
            ("state", staged["state"]),
            ("new file", staged["added"]),
            ("modified", staged["modified"]),
            ("deleted", staged["deleted"]),
            ("catalog", catalog_lines),
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
@click.option("-n", "--dry-run", is_flag=True,
              help="List the matching files without staging them.")
@click.argument("inputs", nargs=-1, required=True)
@click.pass_obj
def add(repo, encoding, dry_run, inputs):
    """Add files to the hallmark index.

    `hallmark add [--regex] FORMAT` stages the files matching a filename
    template such as `a{a}_i{i}.h5`. Each new template is tracked alongside
    the existing ones; `hallmark rm --cached FORMAT` stops tracking one.
    A new template must match at least one file.
    `hallmark add "."` rescans every local template within the current
    directory. Explicit path inputs such as shell-expanded `*` are not
    supported yet with the parameter-based manifest format.

    `hallmark add 'URL/{field}...'` catalogs remote files matching a URL
    template, such as `https://host/ER2/{src}_{day}.h5`, without downloading
    them. The URL up to the first segment with a field is recorded as the
    template's source. Adding it again syncs the catalog with the remote
    directory. Use `hallmark ls-remote URL` to find templates.
    """
    remote = len(inputs) == 1 and is_remote_url(inputs[0])
    # attempt to add the specified files to the hallmark index, handling any errors
    with _translate_cli_errors(RuntimeError, ValueError, FileNotFoundError,
                               DownloadError):
        # if there is only one input, use the add method for a single input
        if len(inputs) == 1:
            pf = repo.add(inputs[0], encoding, dry_run=dry_run,
                          **({"progress": True} if remote else {}))
        # oterhwise, use the add_paths method for multiple inputs
        else:
            pf = repo.add_paths(list(inputs))

    if remote:
        _report_remote_add(pf, dry_run=dry_run)
        return
    if pf.empty:
        click.echo("No files matched the format string.")
    else:
        click.echo("Would add" if dry_run else "Changes to be committed")
        click.echo(pf.path.to_string(index=False, header=False))


def _report_remote_add(pf, *, dry_run):
    """Summarize a remote catalog update without listing every file."""
    paths = pf["path"].tolist()
    click.echo("Would add" if dry_run else "Changes to be committed")
    for path in paths[:20]:
        click.echo(path)
    if len(paths) > 20:
        click.echo(f"... {len(paths) - 20} more file(s)")
    mismatched = pf.attrs.get("mismatched", 0)
    if mismatched:
        click.echo(f"Skipped {mismatched} listed file(s) whose names the template "
                   "cannot re-create exactly.")
    if not dry_run:
        click.echo(f"Cataloged {len(paths)} remote file(s); nothing was downloaded. "
                   "Run hallmark commit -m MESSAGE, then hallmark download to "
                   "fetch files.")


@hallmark.command(short_help="Stop tracking a template.")
@click.option("--cached", is_flag=True,
              help="Remove the template from the catalog and keep its files.")
@click.argument("template")
@click.pass_obj
def rm(repo, cached, template):
    """Stop tracking TEMPLATE, like `git rm --cached`.

    The template's catalog is removed from the index; files are left in
    place. TEMPLATE is the tracked filename template, or the full URL
    template of a remote one.
    """
    if not cached:
        raise ClickException(
            "hallmark rm requires --cached; files are never deleted")
    with _translate_cli_errors(RuntimeError, ValueError, GitError):
        entry = repo.rm_cached(template)
    click.echo(f"Stopped tracking {entry.fmt or entry.db}; files were left in place.")


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
    fmt is None and remote_name is None and remote_url is None
    and not encodings):
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


@hallmark.command(short_help="Download files from the configured data remote.")
@click.argument("files", nargs=-1)
@click.option("--tsv", "tsv_names", multiple=True,
              help="Select a catalog TSV. May be repeated.")
@click.option("--all", "download_all", is_flag=True,
              help="Select all cataloged files.")
@click.option("--include", "includes", multiple=True,
              help="Select paths matching a glob. ** matches recursively. "
                   "May be repeated.")
@click.option("--remote", "remote_name",
              help="Name of the configured data remote to use.")
@click.option("--output", type=click.Path(file_okay=False),
              help="Output directory. Defaults to the repository worktree.")
@click.option("--max-workers", type=click.IntRange(min=1), default=4,
              show_default=True)
@click.option("--dry-run", is_flag=True,
              help="Show the download plan using only local catalog metadata.")
@click.option("--interactive", is_flag=True,
              help="Choose cataloged files and review sizes before approval.")
@click.option("-y", "--yes", is_flag=True, hidden=True)
@click.pass_obj
def download(repo, files, tsv_names, download_all, includes, remote_name,
             output, max_workers, dry_run, interactive, yes):
    """
    Download selected files from a configured data remote.

    Preview the selection with --dry-run. Every nonempty download displays
    its plan and asks for confirmation before transferring files.
    Use --interactive to choose patterns or all files in a terminal.
    """
    if interactive:
        if files or tsv_names or download_all or includes:
            raise ClickException(
                "--interactive cannot be combined with paths, --include, "
                "--tsv, or --all")
        if yes:
            click.echo("--yes is deprecated; downloads still require confirmation.",
                       err=True)
        with _translate_cli_errors(DownloadError, ValueError, OSError):
            _interactive_download(repo, output=output, remote_name=remote_name,
                                  max_workers=max_workers, dry_run=dry_run)
        return
    if download_all and (files or tsv_names):
        raise ClickException("--all cannot be combined with file paths or --tsv")
    if not files and not tsv_names and not download_all and not includes:
        raise ClickException("Provide file paths, --tsv, --all, or --include")
    if repo.worktree is None and output is None:
        raise ClickException("--output is required when downloading from a bare repo")
    if yes:
        click.echo("--yes is deprecated; downloads still require confirmation.",
                   err=True)
    with _translate_cli_errors(DownloadError, ValueError):
        plan = repo.plan_download(
            output, file_paths=files, tsv_names=tsv_names, all_files=download_all,
            include=includes or None, remote_name=remote_name)
        if dry_run:
            _show_download_plan(plan, show_paths=True)
            return
        _run_download(repo, plan, max_workers=max_workers)


@hallmark.command(short_help="Clone an existing Hallmark catalog.")
@click.argument("url")
@click.argument("path")
@click.option("--source-type", default="auto", show_default=True,
              type=click.Choice(["auto", "git", "catalog"]),
              help="Override automatic source detection.")
@click.option("--no-download", is_flag=True,
              help="Copy the complete catalog without reviewing or downloading files.")
@click.option("--include", "includes", multiple=True,
              help="Select downloads by relative-path glob; keep the full catalog. "
                   "May be repeated.")
@click.option("--interactive", is_flag=True,
              help="Choose files interactively instead of reviewing the whole catalog.")
@click.option("--output", type=click.Path(file_okay=False),
              help="Download directory. Defaults to the worktree; "
                   "prompted for bare repos.")
def clone(url, path, source_type, no_download, includes, interactive, output):
    """Clone a complete Git catalog or published catalog snapshot at PATH.

    Copy the complete catalog first, then review a download plan and confirm
    transfers. --include selects downloads without trimming the catalog.
    --no-download skips review; --interactive opens the selection chooser.
    Without a terminal, keep the catalog and print download commands for later.
    For raw datasets, use init, then add a URL template.
    """
    if no_download and (interactive or includes or output is not None):
        raise ClickException(
            "--no-download cannot be combined with --interactive, "
            "--include, or --output")
    if interactive and includes:
        raise ClickException("--interactive cannot be combined with --include")
    with _translate_cli_errors(DownloadError, GitError, ValueError):
        try:
            repo = Repo.clone(url, path, source_type=source_type, progress=True)
        except CloneError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1) from exc
        click.echo(f'Successfully cloned to "{path}"')
    if not no_download:
        _offer_download(repo, includes=includes, output=output,
                        interactive=interactive)


@hallmark.command(short_help="Deprecated: use init, then add a URL template.")
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
         "Load formats and, unless --remote is given, data remotes.")
@click.option(
    "--fmt", "fmts", multiple=True,
    help="Filename format and TSV name, as FMT=DB (e.g. "
         "'a{a}_i{i}.h5=data.tsv'). May be repeated for multiple formats.")
@click.option(
    "--overwrite",
    is_flag=True,
    help="Replace the destination repository if it already exists.")
@click.option("--dataset-url",
              help="Exact dataset URL to search recursively. Recorded remotes "
                   "do not change this root.")
@click.option("--index-format", type=click.Choice(["cyverse-html"]), hidden=True)
@click.option("--allow-remote-commands", is_flag=True,
              hidden=True, help="Deprecated; discovery uses SFTP.")
@click.option("--remote-hash", is_flag=True,
              hidden=True, help="Unsupported; discovery does not hash dataset files.")
def build(directory, dataset_name, remotes, config_file, fmts, overwrite,
          dataset_url, index_format, allow_remote_commands, remote_hash):
    """
    Build a catalog at DIRECTORY/DATASET_NAME.hm.

    This command is deprecated. Use hallmark init PATH, then
    hallmark add 'URL/{field}...' to catalog remote datasets.

    --dataset-url selects the exact discovery root; otherwise DATASET_NAME
    identifies a dataset beneath the CyVerse curated-data directory.
    --remote records data locations without changing the discovery root.

    Supply filename formats with --fmt or --config-file. If neither is
    provided, existing formats are reused or a generic path catalog is
    created. Dataset files are not downloaded during catalog creation.
    """
    click.echo("build is deprecated; use hallmark init PATH, then "
               "hallmark add 'URL/{field}...'.", err=True)
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
        "dataset_url": dataset_url,
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
