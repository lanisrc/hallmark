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

import sys
from contextlib import contextmanager
from pathlib import Path

import click
import yaml
from click import ClickException
from git.exc import GitError

from . import Repo
from .fmt_detection import detect_fmt
from .remote.download import DownloadError
from .remote.discovery import discover_remote_files, path_matches
from .remote.sources import SourceSpec
from .repo.config import normalize_remotes
from .transport import OperationContext, RemoteSpec
from .error import CheckoutError, CloneError, HallmarkError
from .utils import CHECKSUM_ALGORITHMS


# use a context manager to translate application errors into clean Click errors
@contextmanager
def _translate_cli_errors(*error_types, prefix=None):
    """
    Used by hallmark, init, info, status, add, set_config, commit, log, branch,
    checkout, download, and clone.
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
    # A declined prompt raises Abort, itself a RuntimeError; let Click report it.
    except (click.exceptions.Abort, ClickException):
        raise
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
    for path in results.get("skipped", []):
        click.echo(f"Already downloaded: {path}")
    from_store = results.get("from_store", [])
    if from_store:
        click.echo(f"Placed {len(from_store)} file(s) from .hm/objects")
    replaced = results.get("replaced", [])
    if replaced:
        click.echo(f"Replaced {len(replaced)} outdated file(s); "
                   "old versions stay in .hm/objects")
    unchanged = results.get("unchanged", [])
    if unchanged:
        click.echo(f"Fetched {len(unchanged)} file(s) to compare; "
                   "they were already up to date")
    copied = results.get("copied", [])
    if copied:
        click.echo("Copied instead of linked (links unavailable):")
        for path in copied[:10]:
            click.echo(f"  {path}")
    if results.get("catalog_upgraded"):
        click.echo("Recorded this older catalog's source, as hm add URL does")
    if results.get("staged"):
        click.echo(f"Staged SHA-1 for {results['staged']} file(s); "
                   "run hm commit to record them")
    if results.get("staging_note"):
        click.echo(results["staging_note"])
    if results.get("staging_error"):
        click.echo(results["staging_error"], err=True)
        click.echo("Run hm download again to record them.", err=True)
    succeeded = results["succeeded"]
    failed = results["failed"]
    total_mb = results["total_bytes"] / (1024 * 1024)
    # If there were no failed downloads, report success and exit.
    # Files placed from the store were not downloaded again.
    transferred = succeeded - len(from_store)
    if failed == 0:
        if transferred or not (from_store or results.get("staged")):
            click.echo(
                f"Successfully downloaded {transferred} files "f"({total_mb:.1f} MB)")
        if results.get("staging_error"):
            raise ClickException("Downloaded files were not recorded")
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
    if not plan.has_changes:
        click.echo("No files selected for download.")
        if any(item.sha1 for item in plan.skipped_items):
            # Files found complete may still need their SHA-1 recorded.
            results = repo.download(plan)
            if results.get("staged") or results.get("staging_error"):
                _report_download_results(results)
        return
    click.confirm("Download these files?", default=False, abort=True)
    results = repo.download(plan, approved=True, max_workers=max_workers,
                            progress=True)
    _report_download_results(results)


# Errors a user causes or can fix, such as a typo in config.yml, a missing
# file, a full disk or a git failure. Every command reports them in one line;
# anything else is a bug and keeps its traceback.
_USER_ERRORS = (HallmarkError, GitError, OSError, yaml.YAMLError, ValueError)


class _HallmarkGroup(click.Group):
    """Report user-fixable errors from any command without a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.exceptions.Abort, ClickException):
            raise
        except _USER_ERRORS as exc:
            raise ClickException(str(exc) or type(exc).__name__) from exc


@click.group(name="hm", cls=_HallmarkGroup)
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
    with _translate_cli_errors(*_USER_ERRORS,
                               prefix="Failed to open hallmark repository"):
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
        if snapshot["branch"] is None:
            click.echo(f'Not on a branch; at commit {snapshot["commit"]}')
        else:
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


def _ask_about_resort(count, others=0):
    """Ask whether files already sorted are sorted again with a new fmt."""
    if not others:
        choice = click.prompt(
            f"{count} files are already sorted. Sort them again with the new fmt, "
            "or only sort the unsorted files?",
            type=click.Choice(["resort", "unsorted"]))
    else:
        choice = click.prompt(
            f"{count} files of this source and {others} of other sources are "
            "already sorted. Sort this source's files again (resort), every "
            "source's files (all), or only the unsorted files (unsorted)?",
            type=click.Choice(["resort", "all", "unsorted"]))
    return {"resort": True, "all": "all", "unsorted": False}[choice]


def _ask_about_missing(paths):
    """Ask whether catalogued files gone from the server leave the catalog."""
    click.echo(f"{len(paths)} catalogued file(s) are no longer on the server:")
    for path in paths[:10]:
        click.echo(f"  {path}")
    if len(paths) > 10:
        click.echo(f"  ... and {len(paths) - 10} more")
    return click.prompt(
        "Remove them from the catalog, or keep them?",
        type=click.Choice(["remove", "keep"])) == "remove"


def _interactive() -> bool:
    """Return whether someone at a terminal can answer prompts."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_about_duplicates(duplicates):
    """Ask what to do with paths another source already catalogs."""
    click.echo(duplicates.describe())
    choices = ["s", "p", "a"] if duplicates.can_prefix else ["s", "a"]
    click.echo("  [s] Skip them, keeping the catalogued rows")
    if duplicates.can_prefix:
        click.echo("  [p] Keep both, with this source's files under a folder")
    click.echo("  [a] Abort; nothing is saved")
    choice = click.prompt("Choose", type=click.Choice(choices), default="a",
                          show_choices=False)
    if choice == "s":
        return "skip"
    if choice == "p":
        def folder(value):
            try:
                return SourceSpec(duplicates.source, "remote", prefix=value).prefix
            except ValueError as exc:
                raise click.BadParameter(str(exc)) from exc

        return "prefix", click.prompt("Folder for this source's files",
                                      value_proc=folder)
    raise click.Abort()


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
@click.option("--fmt", "remote_fmts", multiple=True,
              help="Remote filename format, as FMT or FMT=DB. May be repeated "
                   "for multiple fmts; otherwise use a URL pattern.")
@click.option("--remote", "remote_name",
              help="Data remote to catalog under; matched by URL by default.")
@click.option("--source", help="Catalog source to update or create.")
@click.option("--prefix", help="Local folder for this source's files.")
@click.option("--server-checksum",
              type=click.Choice(CHECKSUM_ALGORITHMS),
              help="Have an SSH server hash files that have no published "
                   "checksum. Reads every such file on the server.")
@click.option("--skip-duplicates", is_flag=True,
              help="Leave out paths another source already catalogs, keeping "
                   "its rows.")
@click.option("--prune/--keep-missing", default=None,
              help="Remove catalogued files gone from the server, or keep "
                   "them. Without either, add asks.")
@click.argument("inputs", nargs=-1, required=True)
@click.pass_obj
def add(repo, encoding, inputs, auth, backend, backend_options, filters, remote_fmts,
        remote_name, source, prefix, server_checksum, skip_duplicates, prune):
    """Add files to the hallmark index.

    A remote URL or URL pattern stages a catalog without downloading files
    or committing. Use --filter to select remote paths. Filename formats,
    from the URL pattern or --fmt FMT[=DB] (repeatable), sort the files: each
    format's files are rows of its own TSV, and files no format matches are
    listed as file entries in config.yml. A new format applies to the whole
    catalog; when files are already sorted, add asks whether to sort them
    again. A scan that fails or matches no files keeps the previously staged
    catalog.

    Each remote selection is a source of the catalog. Adding the same URL and
    filters again updates its source: new files are added, and add asks
    whether files gone from the server leave the catalog (--prune) or stay
    (--keep-missing). Different filters add another source; use
    --source NAME to change an existing source's filters instead. Files from
    another server keep their paths beneath its URL; use --prefix DIR when
    those paths are already catalogued. When paths clash, add says whether
    each has the same content in both places; at a terminal it asks whether
    to skip them, keep both under a folder, or abort, and elsewhere
    --skip-duplicates leaves them out.

    `hm add [--regex] FORMAT` uses the branch format string workflow.
    `hm add "."` rebuilds the manifest from current files that match
    the branch `fmt` in `config.yml`.
    Files and directories are checked against the saved branch pattern.
    """
    with _translate_cli_errors(ValueError, OSError, yaml.YAMLError):
        options = _load_backend_options(backend_options)
    fmt_entries = []
    for entry in remote_fmts:
        # split the fmt entry into its format and database name components
        fmt, separator, db = entry.rpartition("=")
        # without "=DB" the whole value is the fmt, and its db is chosen later
        if not separator or any(char in db for char in "{}/"):
            fmt, db = entry, None
        fmt = fmt.strip()
        # Validate that the fmt is not empty or whitespace-only
        if not fmt:
            raise ClickException("--fmt must define a non-empty format")
        fmt_entries.append({"fmt": fmt, "db": db} if db else {"fmt": fmt})
    remote_options = dict(auth=auth, backend=backend,
                          backend_options=options,
                          filter=filters or None, remote_fmt=fmt_entries or None,
                          remote=remote_name, source=source, prefix=prefix,
                          server_checksum=server_checksum,
                          on_duplicate="skip" if skip_duplicates else None,
                          prune=prune)
    if len(inputs) != 1 and any(value is not None for value in remote_options.values()):
        raise ClickException("Remote add accepts one URL at a time")
    # attempt to add the specified files to the hallmark index, handling any errors
    with _translate_cli_errors(RuntimeError, ValueError, OSError, yaml.YAMLError):
        # if there is only one input, use the add method for a single input
        if len(inputs) == 1:
            remote = "://" in inputs[0]
            if remote or any(value is not None for value in remote_options.values()):
                if remote and not skip_duplicates and _interactive():
                    remote_options["on_duplicate"] = _ask_about_duplicates
                if remote and prune is None:
                    remote_options["prune"] = _ask_about_missing
                pf = repo.add(inputs[0], encoding, progress=True, **remote_options,
                              resort=_ask_about_resort)
            else:
                pf = repo.add(inputs[0], encoding)
        # oterhwise, use the add_paths method for multiple inputs
        else:
            pf = repo.add_paths(list(inputs), encoding=encoding)

    if pf.empty:
        click.echo("No files matched the format string.")
    else:
        click.echo("Changes to be committed")
        for path in pf.path:
            click.echo(path)
    case_clashes = pf.attrs.get("case_clashes", [])
    if case_clashes:
        click.echo("Warning: these paths differ only in letter case, so they "
                   "would be one file on macOS and Windows:", err=True)
        for group in case_clashes[:10]:
            click.echo("  " + ", ".join(group), err=True)
    skipped = pf.attrs.get("skipped", [])
    if skipped:
        click.echo("Skipped paths another source already catalogs:")
        for clash in skipped[:10]:
            click.echo(f"  {clash.label()}")
        if len(skipped) > 10:
            click.echo(f"  ... and {len(skipped) - 10} more")
    removed = pf.attrs.get("removed", [])
    if removed:
        click.echo("Removed from the catalog (no longer on the server):")
        for path in removed:
            click.echo(f"  {path}")
    kept = pf.attrs.get("kept_missing", [])
    if kept:
        click.echo(f"Kept in the catalog, though no longer on the server: "
                   f"{len(kept)} file(s)")
    changed = pf.attrs.get("changed", [])
    if changed:
        click.echo(f"Changed upstream ({len(changed)}); run hm download to "
                   "replace the local copies:")
        for path in changed[:10]:
            click.echo(f"  {path}")
        if len(changed) > 10:
            click.echo(f"  ... and {len(changed) - 10} more")

    # display unmatched files that were skipped
    unmatched = pf.attrs.get("unmatched", [])
    if unmatched:
        click.echo("Skipped files that do not match the branch pattern:")
        # print each unmatched path
        for path in unmatched:
            click.echo(f"  {path}")


@hallmark.command(short_help="Detect filename formats in a remote folder.")
@click.argument("remote")
@click.option("--filter", "filters", multiple=True, help="Select remote paths by glob.")
@click.option("--add", "add_fmts", is_flag=True,
              help="Catalog the files with the detected fmts, as hm add does.")
@click.pass_context
def detect(ctx, remote, filters, add_fmts):
    """Detect the filename formats of a remote's files.

    Requires a configured remote or a URL inside one.
    Optionally, the detected filename formats can be immediately added to the catalog
    with the --add flag.
    Filters are used to select specific remote paths by glob patterns.

    Raises:
        ClickException: If the remote is not configured or the URL is not inside a
        configured remote.
    """
    # get the list of configured remotes from this repository's configuration
    remotes = normalize_remotes(ctx.obj.state.config.get("remote"))
    # determine if the remote argument is a name or a URL
    if "://" not in remote:
        # check that the remote name exists in the configured remotes
        entry = next((entry for entry in remotes
                      if entry.get("name") == remote), None)
        # if the remote name does not exist in the configured remotes, raise an error
        if entry is None:
            raise ClickException(f"No remote named {remote!r} in config.yml")
        # otherwise, use the URL from the configured remote entry
        url = entry["url"]
    # otherwise, treat the remote argument as a URL
    else:
        # a URL can point at a subfolder of a configured remote
        url = remote
        # find the configured remote entry that matches the URL, if any
        entry = next((entry for entry in remotes
                      if entry.get("url") and url.startswith(entry["url"])), None)
        # if no matching configured remote was found for the URL, raise an error
        if entry is None:
            raise ClickException(f"{url} is not inside a configured remote")
    # use translated CLI errors for better user feedback
    with _translate_cli_errors(RuntimeError, ValueError, OSError):
        source = RemoteSpec.from_url(url, entry.get("auth"),
                                     backend=entry.get("backend"),
                                     backend_options=entry.get("backend_options"))
        # use the operation context to discover remote files
        with OperationContext(source) as context:
            # discover remote files based on the operation context and filters
            paths = [file.path for file in discover_remote_files(
                context, filter=filters or None, progress=True)]
        # detect the filename formats of the discovered remote files
        fmts = detect_fmt(paths, include_drives=True)
    if not fmts:
        raise ClickException(
            "No fmts could be automatically detected from this dataset.")
    click.echo(f"Detected {len(fmts)} fmt(s) from {len(paths)} files:")
    for fmt in fmts:
        click.echo(f"  {fmt}")
    # catalog from the remote's root, so a subfolder URL never changes the remote;
    # the subfolder becomes part of each fmt and of the selection
    remote_url = entry["url"]
    prefix = url[len(remote_url):]
    fmts = [prefix + fmt for fmt in fmts]
    filters = tuple(prefix + pattern for pattern in filters) or (
        (prefix + "**",) if prefix else ())
    # call the add command if the --add flag is set, otherwise show instructions
    if add_fmts:
        ctx.invoke(add, inputs=(remote_url,), filters=filters, remote_fmts=tuple(fmts))
    else:
        click.echo("To catalog with them:")
        click.echo(f"  hm add {remote_url} "
                   + " ".join(f'--fmt "{fmt}"' for fmt in fmts)
                   + "".join(f' --filter "{pattern}"' for pattern in filters))


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


_IDENTITY_KEYS = {"user.name": "name", "user.email": "email"}

_IDENTITY_USAGE = (
    'Usage: hm config user.name "Your Name"'
    ' or hm config user.email "you@example.com"')


@hallmark.command(short_help="Set the commit author for this repository.")
@click.argument("key", required=False)
@click.argument("value", required=False)
@click.pass_obj
def config(repo, key, value):
    """Save KEY as VALUE for this repository only.

    KEY is `user.name` or `user.email`. These name the author of commits
    made here. They are not shared by `hm clone`.
    """
    if key not in _IDENTITY_KEYS:
        raise ClickException(_IDENTITY_USAGE)

    if value is None:
        with _translate_cli_errors(*_REPO_READ_ERRORS):
            current = dict(zip(
                ("user.name", "user.email"), repo.effective_identity()))[key]
        if current is None:
            raise ClickException(
                f'{key} is not set. Set it with: hm config {key} "..."')
        click.echo(current)
        return

    if not value.strip():
        raise ClickException(_IDENTITY_USAGE)

    with _translate_cli_errors(*_REPO_READ_ERRORS):
        repo.set_identity(**{_IDENTITY_KEYS[key]: value.strip()})

    click.echo(f"Set {key} for this repository.")


@hallmark.command(short_help="List or create hallmark branches.")
@click.argument("name", required=False)
@click.pass_obj
def branch(repo, name):
    """List local hallmark branches, or create NAME at the current commit.

    Creating a branch does not switch to it; use `hm checkout NAME` for that.
    """
    if name is not None:
        with _translate_cli_errors(*_REPO_READ_ERRORS):
            created = repo.create_branch(name)
        click.echo(f'Created branch "{created}".')
        return

    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(*_REPO_READ_ERRORS):
        snapshot = repo.branches()
    # if there is a snapshot of the branches, display them to the user
    if snapshot:
        current = snapshot["current"]

    if current is None:
        click.echo(f'* (no branch; at commit {snapshot["detached_at"]})')

    for name in snapshot["names"]:
        prefix = "*" if name == current else " "
        click.echo(f"{prefix} {name}")


@hallmark.command(short_help="Switch to another branch.")
@click.argument("target_branch")
@click.pass_obj
def checkout(repo, target_branch):
    """Switch to TARGET and rewrite tracked files from its state.

    TARGET may be an existing branch or a commit id. A commit id leaves you on
    no branch, so use `hm branch NAME` to create one before committing again.
    An unrecognized TARGET is an error and changes nothing.
    Only hallmark-tracked files are rewritten; unrelated files are left
    alone unless they block restoration of a tracked path.
    """
    # use the _translate_cli_errors context manager to handle specific exceptions
    with _translate_cli_errors(*_REPO_READ_ERRORS):
        # attempt to switch to the target branch, handling any errors
        switched = repo.checkout(target_branch)

    if switched:
        snapshot = repo.branches()
        if snapshot["current"] is None:
            click.echo(
                f'Now at commit {snapshot["detached_at"]}; not on a branch.')
        else:
            click.echo(f'Switched to branch "{snapshot["current"]}".')


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
              help="Download every selected file from this data remote, "
                   "instead of each source's own remote.")
@click.option("--source", "sources", multiple=True,
              help="Select the files of a catalog source. May be repeated.")
@click.option("--output", type=click.Path(file_okay=False),
              help="Output directory. Defaults to the repository worktree.")
@click.option("--max-workers", type=click.IntRange(min=1), default=4,
              show_default=True)
@click.option("--dry-run", is_flag=True,
              help="Show the download plan using only local catalog metadata.")
@click.pass_obj
def download(repo, files, tsv_names, download_all, filters, fmt, remote_name,
             sources, output, max_workers, dry_run):
    """
    Download selected files from the catalog's data remotes.

    Each file comes from the remote of the source that cataloged it.
    Preview the selection with --dry-run. Every nonempty download displays
    its plan and asks for confirmation before transferring files.
    """
    if download_all and (files or tsv_names):
        raise ClickException("--all cannot be combined with file paths or --tsv")
    if (not files and not tsv_names and not download_all and not filters
            and not fmt and not sources):
        raise ClickException(
            "Provide file paths, --tsv, --all, --source, --filter, or --fmt")
    if repo.worktree is None and output is None:
        raise ClickException("--output is required when downloading from a bare repo")
    with _translate_cli_errors(DownloadError, ValueError):
        plan = repo.plan_download(
            output, file_paths=files, tsv_names=tsv_names, all_files=download_all,
            filter=filters or None, fmt=fmt, remote_name=remote_name,
            sources=sources or None)
        if dry_run:
            click.echo(plan.summary())
            listed = [(item, []) for item in plan.items] + [
                (item, ["from .hm/objects"]) for item in plan.stored_items]
            for item, notes in listed[:20]:
                if item.replaces:
                    notes = notes + ["replaces an outdated file"]
                note = f" ({', '.join(notes)})" if notes else ""
                click.echo(f"  {item.relative_path.as_posix()}{note}")
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
