"""
Create branches, check them out, and add worktrees for them.

``Repo.create_branch``, ``Repo.checkout`` and ``Repo.add_worktree`` call
these functions. A local catalogue restores the target's files from the
object store; a remote catalogue switches only ``.hm`` and leaves dataset
files alone.
"""

import json
import os
import shlex
import stat
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from git.exc import GitCommandError

from ..error import CheckoutError, DestinationExistsError, DothmError
from ..utils import (
    CHECKOUT_PREFIX, calculate_file_checksum, replace_file_on_success,
    resolve_path_in_root)
from .state import is_remote_catalog
from .config import branch_filename_format, row_to_path, single_data_format
from .manifest import iter_manifest_entries
from .history import (
    load_branch_state, load_head_state, find_remote_branch,
    fetch_missing_objects_from_remote, _load_revision_state)
from .changes import ensure_clean_tracked_files, tracked_paths


def current_branch(repo):
    """Return the name of the checked-out branch, or None at a detached HEAD."""
    if repo.dothm.head.is_detached:
        return None
    return repo.dothm.active_branch.name


def current_commit(repo):
    """Return the short id of the HEAD commit, or None before the first commit."""
    if not repo.dothm.head.is_valid():
        return None
    return repo.dothm.head.commit.hexsha[:7]


def create_branch(repo, name):
    """
    Create a branch at the current commit without switching to it.

    Args:
        repo (Repo): Repository to branch.
        name (str): Name for the new branch.

    Returns:
        str: The normalized branch name.

    Raises:
        ValueError: If the repository has no commits, the name is invalid, or
            a branch of that name already exists.
    """
    name = repo._validate_branch_name(name)

    if not repo.dothm.head.is_valid():
        raise ValueError(
            "cannot create a branch before the first commit")

    if name in {head.name for head in repo.dothm.heads}:
        raise ValueError(f'branch already exists: "{name}"')

    repo.dothm.create_head(name)
    return name


def resolve_checkout_target(repo, target):
    """
    Find what a checkout target names: a local branch, a remote branch or a
    commit, tried in that order.

    Args:
        repo (Repo): Repository to look in.
        target (str): Branch name, remote branch name or commit id.

    Returns:
        tuple[str, str]: ``("branch", name)``, ``("remote", "REMOTE/BRANCH")``
        or ``("commit", full_sha1)``.

    Raises:
        CheckoutError: If ``target`` names none of these.
    """
    if target in {head.name for head in repo.dothm.heads}:
        return "branch", target

    remote_revision = find_remote_branch(repo, target)
    if remote_revision is not None:
        return "remote", remote_revision

    try:
        commit = repo.dothm.rev_parse(target)
    except Exception:
        commit = None
    if commit is not None:
        return "commit", commit.hexsha

    raise CheckoutError(f'no branch or commit named "{target}"')


def _refuse_pending_state_changes(repo):
    """
    Used by checkout for remote catalogues.
    Refuse to switch while .hm holds changes, saying how to resolve them.

    Unstaging keeps .hm files changed on disk, as git keeps the working copy,
    so the message names the git command that discards them.
    """
    staged = sorted({diff.a_path or diff.b_path
                     for diff in repo.dothm.index.diff("HEAD")})
    if staged:
        raise CheckoutError(
            "Commit or unstage hallmark state changes before checkout: "
            + ", ".join(staged)
            + " (hm commit -m MESSAGE, or hm restore --staged .)")
    unstaged = sorted({diff.a_path or diff.b_path
                       for diff in repo.dothm.index.diff(None)})
    if unstaged:
        raise CheckoutError(
            "Unstaged changes in .hm block checkout: " + ", ".join(unstaged)
            + ". Discard them with: git -C " + shlex.quote(str(repo.dothm.path))
            + " restore " + " ".join(shlex.quote(path) for path in unstaged))


def checkout_folders(worktree) -> list:
    """Return the folders in ``worktree`` that checkouts work in, sorted."""
    with os.scandir(worktree) as entries:
        return sorted(Path(entry.path) for entry in entries
                      if entry.name.startswith(CHECKOUT_PREFIX)
                      and entry.is_dir(follow_symlinks=False))


def recover_interrupted_checkout(repo):
    """
    Roll back every checkout that was killed partway, so it can be run again.

    Before a checkout switches branch or moves a file, it writes in its folder
    a journal of the files it will move and install, and it removes the
    journal before the folder once it is done. A folder without a journal
    changed nothing, or belongs to a finished checkout, and is removed.
    Otherwise each installed file that is still the one installed is removed,
    the originals are moved back, the original branch is checked out again
    and a branch the checkout created is deleted. Every step can be repeated,
    so a recovery that is itself killed is finished by the next one. Callers
    hold the state lock.

    Raises:
        CheckoutError: If a checkout cannot be rolled back; its folder is kept.
    """
    if repo.worktree is None:
        return
    folders = checkout_folders(repo.worktree)
    for folder in folders:
        journal_path = folder / "journal.json"
        if journal_path.is_file():
            try:
                _undo_checkout(repo, folder, json.loads(
                    journal_path.read_text(encoding="utf-8")))
            except Exception as exc:
                raise CheckoutError(
                    "cannot roll back the checkout interrupted in "
                    f"{folder}: {exc}") from exc
            journal_path.unlink()
        rmtree(folder, ignore_errors=True)
    if folders:
        repo.state = repo.dothm.load_state()


def _undo_checkout(repo, folder, journal):
    """Used by recover_interrupted_checkout. Undo what a journal records."""
    backups = [
        (relative_path, resolve_path_in_root(folder, backup, label="checkout backup"))
        for relative_path, backup in journal["backups"]]
    # Content tells an install only where its original is kept here: an
    # original never moved, or moved back, can be an untracked file with
    # the very same content.
    kept = {relative_path for relative_path, backup_path in backups
            if os.path.lexists(backup_path)}
    for relative_path, _, sha1, identity in journal["installs"]:
        path = repo._resolve_worktree_path(relative_path, label="checkout target")
        # Never an original moved back or a file written since.
        if _is_installed(path, sha1 if relative_path in kept else None, identity):
            path.unlink()
            _remove_empty_parents(repo.worktree, path)
    for relative_path, backup_path in reversed(backups):
        if not os.path.lexists(backup_path):
            continue
        path = repo._resolve_worktree_path(relative_path, label="checkout target")
        if os.path.lexists(path):
            raise FileExistsError(f'"{relative_path}" is in the way of its '
                                  f"original, kept in {backup_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup_path, path)
    if not _head_is(repo, journal["branch"], journal["commit"]):
        _restore_head(repo, journal["branch"], journal["commit"])
    if journal["created_branch"] and journal["target"] in {
            head.name for head in repo.dothm.heads}:
        repo.dothm.git.branch("-D", journal["target"])


def _is_installed(path: Path, sha1, identity) -> bool:
    """
    Used by _undo_checkout.
    Return whether ``path`` is the file a checkout installed: the same inode,
    or, where inodes are not kept and ``sha1`` is given, the same content.
    """
    try:
        found = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(found.st_mode):
        return False
    if found.st_ino and [found.st_dev, found.st_ino] == list(identity):
        return True
    return sha1 is not None and calculate_file_checksum(path) == sha1


def _head_is(repo, branch, commit) -> bool:
    """Return whether HEAD is on ``branch``, or at ``commit`` if it is None."""
    if branch is not None:
        return current_branch(repo) == branch
    return repo.dothm.head.is_detached and repo.dothm.head.commit.hexsha == commit


def _restore_head(repo, branch, commit) -> None:
    """Check out ``branch`` again, or detach at ``commit`` if it is None."""
    if branch is None:
        repo.dothm.git.checkout("--detach", commit)
    else:
        repo.dothm.git.checkout(branch)


def _remove_empty_parents(root, path: Path) -> None:
    """Remove the folders above ``path`` that are left empty, up to ``root``."""
    parent = path.parent
    while parent != root and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _write_journal(path: Path, journal: dict) -> None:
    """Write a checkout's journal; a reader finds the old one or the new one."""
    with replace_file_on_success(path) as temp_path:
        temp_path.write_text(json.dumps(journal), encoding="utf-8")


def checkout(repo, target_branch):
    """
    Switch to a branch, remote branch or commit, and update the worktree.

    A remote branch gets a local tracking branch; a commit leaves ``HEAD``
    detached. A remote catalogue switches only ``.hm`` and refuses while it
    holds uncommitted changes. A local catalogue restores the target's files
    from the object store, first fetching missing objects for a remote
    branch, and rolls everything back if a step fails. The state lock is
    held throughout, and a checkout that another process left unfinished is
    rolled back first.

    Args:
        repo (Repo): Repository with a worktree.
        target_branch (str): Branch name, remote branch name or commit id.

    Returns:
        bool: True when the checkout succeeds.

    Raises:
        ValueError: If the target name is invalid.
        CheckoutError: If there is no worktree, the target is unknown, the
            worktree or ``.hm`` has changes, files or objects are missing,
            the switch is between local files and a remote catalogue, the
            target has no single filename format, or a step fails.
    """
    with repo.dothm.state_lock():
        recover_interrupted_checkout(repo)
        return _checkout(repo, target_branch)


def _checkout(repo, target_branch):
    # Validate and normalize the target branch name
    target_branch = repo._validate_branch_name(target_branch)

    if repo.worktree is None:
        raise CheckoutError("cannot checkout without a worktree")
    # Resolve the target once and load the state of exactly that revision.
    target_kind, resolved_target = resolve_checkout_target(repo, target_branch)
    target_state = _load_revision_state(repo, resolved_target)
    if is_remote_catalog(repo.state) or is_remote_catalog(target_state):
        if any(not state.data.empty and not is_remote_catalog(state)
               for state in (repo.state, target_state)):
            raise CheckoutError(
                "Cannot switch between local files and remote catalogs")
        _refuse_pending_state_changes(repo)
        if target_kind == "commit":
            repo.dothm.git.checkout("--detach", resolved_target)
        else:
            repo.dothm.git.checkout(target_branch)
        repo.state = repo.dothm.load_state()
        return True
    ensure_clean_tracked_files(repo)

    has_local = target_kind == "branch"
    has_remote = target_kind == "remote"
    remote_revision = resolved_target if has_remote else None

    current_tracked = tracked_paths(repo)
    # Get the data format string for the target branch configuration
    target_fmt = single_data_format(target_state.config)
    if target_fmt is None:
        # Raise an error if the target branch does not meet the expected criteria
        raise CheckoutError(
            "checkout currently supports only repositories with "
            "one data format and data.tsv")

    # list of (relative path, sha1) tuples for all entries in the target branch
    target_entries_raw = list(iter_manifest_entries(target_state, fmt=target_fmt))
    # get the set of relative paths for all tracked files in the target branch
    target_tracked = {path for path, _ in target_entries_raw}

    # try to identify any missing objects in the target branch that are not present
    # in the object store
    try:
        missing_objects = repo.objects.missing_checksums(
            sha1 for _, sha1 in target_entries_raw)
    # if a ValueError occurs during object existence check, raise a CheckoutError
    except ValueError as exc:
        raise CheckoutError(str(exc)) from exc

    # if switching to a remote-tracking branch, try to fetch any missing
    # objects from that remote before giving up
    if missing_objects and has_remote:
        remote_name = remote_revision.split("/", 1)[0]
        missing_objects = sorted(fetch_missing_objects_from_remote(
            repo, remote_name, missing_objects))

    # if there are missing objects, raise a CheckoutError with details
    if missing_objects:
        raise CheckoutError(
            "cannot checkout; missing object(s): "
            + ", ".join(missing_objects))

    # conflict_candidates are candidate paths that exist in the worktree but
    # are not tracked, and may conflict
    conflict_candidates: list[tuple[Path, Path, str]] = []
    for rel_path, sha1 in target_entries_raw:
        # try to resolve the relative path to an absolute path in the worktree
        try:
            target_path = repo._resolve_worktree_path(rel_path, label="checkout target")
        # if the relative path is invalid or outside worktree, raise a CheckoutError
        except ValueError as exc:
            raise CheckoutError(str(exc)) from exc

        # skip paths that are already tracked or do not exist in the worktree
        if (rel_path in current_tracked or not target_path.exists()):
            continue
        # if the target path exists but is not a file, raise a CheckoutError
        if not target_path.is_file():
            raise CheckoutError(
                f'target tracked path "{rel_path}" '
                "already exists as an untracked non-file")
        # add the candidate path to the list for further checksum verification
        conflict_candidates.append((rel_path, target_path, sha1.lower()))

    # if there are any conflict candidates
    if conflict_candidates:
        # create a list of full paths from the conflict candidates
        conflict_paths = [full_path for _, full_path, _ in conflict_candidates]
        # compute SHA1 checksums for all conflict candidate paths in parallel
        conflict_checksums = repo.checksum_many(conflict_paths)

        # for each conflict candidate
        for (rel_path, full_path, expected_sha1) in conflict_candidates:
            # if the computed checksum does not match the expected checksum
            if (conflict_checksums[full_path] != expected_sha1):
                raise CheckoutError(
                    f'target tracked path "{rel_path}" '
                    "already exists as an untracked file")

    # Store the name of the currently active branch before switching
    original_branch = current_branch(repo)
    # Get the current format string from the branch configuration
    current_fmt = branch_filename_format(repo)
    # Create a mapping of current tracked paths to their SHA1 checksums
    current_sha_by_path = {
        row_to_path(row, current_fmt): str(row["sha1"]).lower()
        for _, row in repo.state.data.iterrows()}
    # tuples of (relative path, sha1) for all files in the target branch
    target_entries = [(path, sha1.lower()) for path, sha1 in target_entries_raw]
    # changed_target_entries are the files in the target branch that have different
    # SHA1 checksums compared to the current branch, indicating they will be updated
    changed_target_entries = [
        (relative_path, sha1)
        for relative_path, sha1 in target_entries
        if current_sha_by_path.get(relative_path) != sha1]
    # changed_target_paths is a set of relative paths for files that will be updated
    changed_target_paths = {
        relative_path
        for relative_path, _ in changed_target_entries}
    # paths that are either currently tracked but not in the target branch
    # or paths that are tracked but have changed from the current branch
    affected_paths = sorted((current_tracked - target_tracked
                             ) | changed_target_paths,
        key=lambda path: (len(path.parts), path.as_posix()), reverse=True)

    # What the next open needs to roll back this checkout if it is killed.
    journal = {
        "branch": original_branch,
        "commit": repo.dothm.head.commit.hexsha,
        "target": target_branch,
        "created_branch": has_remote,
        "backups": [[path.as_posix(), f"backup/{index}"]
                    for index, path in enumerate(affected_paths)],
        "installs": []}
    # Stage the target files, and later keep the originals, in a folder in the
    # worktree, so that both stay on its filesystem and survive a crash.
    transaction_root = Path(mkdtemp(prefix=CHECKOUT_PREFIX, dir=repo.worktree))
    journal_path = transaction_root / "journal.json"

    # files that will be restored from object store before switching branches
    staged_files = []
    # try to restore the target files from object store into the staging area
    try:
        for index, (relative_path, sha1) in enumerate(changed_target_entries):
            staged_path = transaction_root / "staged" / str(index)
            # restore checks that the copy has this sha1
            repo.objects.restore(sha1, staged_path)
            staged = staged_path.stat()
            staged_files.append((relative_path, staged_path))
            journal["installs"].append([
                relative_path.as_posix(), f"staged/{index}", sha1,
                [staged.st_dev, staged.st_ino]])
        _write_journal(journal_path, journal)
    # nothing has changed yet, so the folder is all there is to remove
    except BaseException as exc:
        rmtree(transaction_root, ignore_errors=True)
        if not isinstance(exc, Exception):
            raise
        raise CheckoutError(
            f'cannot checkout branch "{target_branch}": '
            f"failed to prepare target files: {exc}") from exc

    backups: list[tuple[Path, Path]] = []
    installed_paths: list[tuple[str, Path]] = []
    # try to switch branches and update the worktree with the target files
    try:
        # if the target branch is new, create and switch to it
        if has_local:
            repo.dothm.git.checkout(target_branch)

        elif has_remote:
            repo.dothm.git.checkout("--track",remote_revision)

        else:
            repo.dothm.git.checkout("--detach", resolved_target)

        # Reload the repository state after switching branches
        repo.state = repo.dothm.load_state()

        for index, relative_path in enumerate(affected_paths):
            # absolute path in the worktree for the affected relative path
            path = repo._resolve_worktree_path(
                relative_path, label="checkout target")
            # create a backup before replacing it with the target file
            if path.exists():
                backup_path = transaction_root / "backup" / str(index)
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                # Listed first: an interrupt can arrive just after the move.
                backups.append((path, backup_path))
                path.replace(backup_path)
                _remove_empty_parents(repo.worktree, path)

        for relative_path, staged_path in staged_files:
            # Determine the destination path in the worktree for the staged file
            destination = repo._resolve_worktree_path(
                relative_path, label="checkout target")
            # Create parent directories for the destination path
            destination.parent.mkdir(parents=True, exist_ok=True)
            installed_paths.append((relative_path.as_posix(), destination))
            # Replace the destination path with the staged file
            staged_path.replace(destination)
        # The checkout is done once its journal is gone.
        journal_path.unlink()
    # attempt to rollback changes if anything, Ctrl-C included, stops it
    except BaseException as exc:
        rollback_errors = []
        head_restored = True

        # try to restore the original branch if it was changed during checkout
        try:
            if not _head_is(repo, original_branch, journal["commit"]):
                _restore_head(repo, original_branch, journal["commit"])
        # if restoring the original branch fails, record the error for reporting
        except Exception as rollback_exc:
            head_restored = False
            rollback_errors.append(
                f"could not restore branch: {rollback_exc}")

        kept_installs = set()
        for relative_path, path in reversed(installed_paths):
            # try to remove the installed files from the failed checkout
            try:
                # if the path is a file or a symlink, unlink it
                if path.is_file() or path.is_symlink():
                    path.unlink()
                _remove_empty_parents(repo.worktree, path)
            # if removing the installed file fails, record error for reporting
            except Exception as rollback_exc:
                kept_installs.add(relative_path)
                rollback_errors.append(
                    f'could not remove "{path}": {rollback_exc}')

        for original_path, backup_path in reversed(backups):
            # the move was interrupted before it happened
            if not os.path.lexists(backup_path):
                continue
            # try to restore the original files from the backups
            try:
                original_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.replace(original_path)
            # if restoring the original file fails, record error for reporting
            except Exception as rollback_exc:
                rollback_errors.append(
                    f'could not restore "{original_path}": '
                    f"{rollback_exc}")

        branch_left = False
        # a remote-tracking checkout creates a local branch, so remove it
        if has_remote and target_branch in {
            head.name for head in repo.dothm.heads}:
            # try to delete the newly created branch to rollback the checkout
            try:
                repo.dothm.git.branch("-D", target_branch)
            # if deleting the new branch fails, record error for reporting
            except GitCommandError as rollback_exc:
                branch_left = True
                rollback_errors.append(
                    f"could not remove new branch: {rollback_exc}")

        # try to reload the repository state after rollback
        try:
            repo.state = repo.dothm.load_state()
        # if reloading the repository state fails, record error for reporting
        except Exception as rollback_exc:
            rollback_errors.append(
                f"could not reload repository state: {rollback_exc}")

        # Keep what is left to undo for the next open to finish.
        journal["backups"] = [
            entry for entry in journal["backups"]
            if os.path.lexists(transaction_root / entry[1])]
        journal["installs"] = [
            entry for entry in journal["installs"] if entry[0] in kept_installs]
        journal["created_branch"] = branch_left
        details = ""
        if (head_restored and not branch_left and not journal["backups"]
                and not journal["installs"]):
            rmtree(transaction_root, ignore_errors=True)
        else:
            try:
                _write_journal(journal_path, journal)
            except Exception as journal_exc:
                rollback_errors.append(
                    f"could not update {journal_path}: {journal_exc}")
            details = (f"; originals kept in {transaction_root}"
                       if journal["backups"] else
                       f"; the next open finishes the rollback from "
                       f"{transaction_root}")
        # if there were any errors during rollback, append them to the message
        if rollback_errors:
            details += "; rollback incomplete: " + "; ".join(rollback_errors)
        if not isinstance(exc, Exception):
            if details and hasattr(exc, "add_note"):
                exc.add_note(f'checkout of branch "{target_branch}" '
                             f"interrupted{details}")
            raise
        # raise a CheckoutError with the detailed message and original exception
        raise CheckoutError(
            f'checkout of branch "{target_branch}" failed: {exc}{details}'
        ) from exc
    rmtree(transaction_root, ignore_errors=True)
    # if checkout process completes successfully, return True to indicate success
    return True


def add_worktree(repo, target_branch):
    """
    Add a worktree for a branch, in a folder named after it next to the
    current worktree.

    An existing branch is linked; a new name branches from ``HEAD``. Files
    are restored from the object store. A folder that already holds a
    Hallmark worktree is left as it is.

    Args:
        repo (Repo): Repository with a worktree.
        target_branch (str): Branch to attach.

    Returns:
        bool: True when the worktree is ready.

    Raises:
        ValueError: If the name is invalid or names the current worktree.
        DestinationExistsError: If the folder exists and is not a Hallmark
            worktree.
        FileNotFoundError: If objects the branch needs are missing.
        RuntimeError: If the repository is bare, the branch has no single
            filename format, or the worktree or its files cannot be created.
    """
    # Validate and normalize the target branch name
    target_branch = repo._validate_branch_name(target_branch)

    if repo.worktree is None:
        raise RuntimeError("cannot add a worktree in a bare " \
        "repository without a worktree")

    # source is the current worktree path, target is the new worktree path
    source = Path(repo.worktree).resolve()
    target = resolve_path_in_root(source.parent, target_branch,
                                    label="worktree destination")
    # if the target path is the same as the source, raise a ValueError
    if target == source:
        raise ValueError("worktree destination cannot be the current worktree")
    # the dothm path for the target worktree is the ".hm" directory
    target_dothm = target / ".hm"
    # if the target path exists and is not a hallmark worktree, raise an error
    if target.exists() and not target_dothm.exists():
        raise DestinationExistsError(
            f'worktree destination "{target}" already exists '
            "and is not a Hallmark worktree")

    # existing_branches is a set of all branch names in the current repository
    existing_branches = {head.name for head in repo.dothm.heads}
    # create a boolean flag indicating whether the target branch is new or existing
    created_branch = target_branch not in existing_branches
    if not created_branch:
        # load the state of the target branch if it already exists
        target_state = load_branch_state(repo, target_branch)
    # if the target branch does not exist yet, load the current HEAD state
    else:
        target_state = load_head_state(repo)

    # get the target fmt from the target branch configuration
    target_fmt = single_data_format(target_state.config)
    # if target branch does not have exactly one data format, raise a RuntimeError
    if target_fmt is None:
        raise RuntimeError(
            "add_worktree requires exactly one data entry "
            "with a non-empty fmt")

    # if the target worktree does not exist
    if not target_dothm.exists():
        # check for missing objects in the target state that are not present in
        # the object store
        try:
            missing_objects = repo.objects.missing_checksums(
                row["sha1"] for _, row in target_state.data.iterrows())
        # if a ValueError occurs during object existence check, raise cleanly
        except ValueError as exc:
            raise FileNotFoundError(str(exc)) from exc
        # if there are missing objects, raise a FileNotFoundError with details
        if missing_objects:
            raise FileNotFoundError(
                "cannot create worktree; missing object(s): "
                + ", ".join(missing_objects))

        # create the target directory and its parents if they do not exist
        target.mkdir(parents=True, exist_ok=True)
        try:
            # if the target branch already exists, link the new worktree to it
            if not created_branch:
                repo.dothm.link_worktree(target_dothm, target_branch)
            # if the target branch does not exist, create a new worktree and branch
            else:
                repo.dothm.git.worktree("add", "-b", target_branch,
                                        str(target_dothm))
        # if the worktree creation fails, raise a RuntimeError with details
        except (GitCommandError, DothmError) as exc:
            # rmtree the target directory to clean up any partial worktree creation
            rmtree(target, ignore_errors=True)
            raise RuntimeError(f'failed to create worktree for branch '
                               f'"{target_branch}": {exc}') from exc

        # iterate over the target state data and restore files from the object store
        try:
            for _, row in target_state.data.iterrows():
                rel_path = row_to_path(row, target_fmt)
                # resolve relative path to an absolute path in the target worktree
                destination = resolve_path_in_root(
                    target,
                    rel_path,
                    label="worktree data path")
                # ensure the parent directory exists before restoring the file
                destination.parent.mkdir(parents=True, exist_ok=True)
                # restore the file from the object store using its SHA-1 checksum
                repo.objects.restore(row["sha1"], destination)

        # if any exception occurs during file restoration, attempt a clean up
        except Exception as exc:
            # Attempt to remove the worktree from Git and clean up the directory
            try:
                repo.dothm.git.worktree("remove", "--force", str(target_dothm))
            # if the worktree removal fails, attempt to prune the worktree list
            except GitCommandError:
                # remove the target directory before pruning
                rmtree(target, ignore_errors=True)
                try:
                    repo.dothm.git.worktree("prune")
                except GitCommandError:
                    # pass if pruning fails, already handling a restoration error
                    pass
            # If the target directory still exists after cleanup attempts, remove it
            else:
                rmtree(target, ignore_errors=True)

            # If a new branch was created and the restoration failed
            if created_branch:
                # try to delete the newly created branch
                try:
                    repo.dothm.git.branch("-D", target_branch)
                # if the branch deletion fails, pass since already handling an error
                except GitCommandError:
                    pass
            # raise RuntimeError with details about failure to populate the worktree
            raise RuntimeError(
                f'failed to populate worktree for branch '
                f'"{target_branch}": {exc}') from exc

    return True

