from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
from git.exc import GitCommandError
from ..error import CheckoutError, DestinationExistsError, DothmError
from ..utils import resolve_path_in_root
from .config import branch_filename_format, row_to_path, single_data_format
from .manifest import iter_manifest_entries
from .history import load_branch_state, load_head_state, find_remote_branch, fetch_missing_objects_from_remote
from .changes import ensure_clean_tracked_files, tracked_paths


def checkout(repo, target_branch: str) -> bool:
    '''
    Switch to a different branch and update the worktree. Raises ValueError if
    branch name is invalid. Raises CheckoutError if the workign directoary
    is not clean or checkout can't be completed safely.

    Args:
        target_branch (string): Branch to switch to.
    Returns:
        boolean: True if checkout succeeds.
    Raises:
        CheckoutError: If the checkout cannot be completed safely.
    '''
    # Validate and normalize the target branch name
    target_branch = repo._validate_branch_name(target_branch)

    if repo.worktree is None:
        raise CheckoutError("cannot checkout without a worktree")
    ensure_clean_tracked_files(repo)

    local_branches = {head.name for head in repo.dothm.heads}
    has_local = target_branch in local_branches

    # Look for a matching branch on any configured remote.
    # Returns something like "origin/feature" or None if no remote branch exists.
    remote_revision = find_remote_branch(repo, target_branch)
    has_remote = remote_revision is not None

    create_new_branch = not has_local and not has_remote
    current_tracked = tracked_paths(repo)
    target_state = load_branch_state(repo, target_branch)
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
    original_branch = repo.dothm.active_branch.name
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

    def remove_empty_parents(path: Path) -> None:
        """Recursively remove empty parent directories up to the worktree root."""
        # parent is the immediate parent directory of the given path
        parent = path.parent
        # while the parent directory is not the worktree root and it exists
        while parent != repo.worktree and parent.exists():
            # try to remove the parent directory
            try:
                parent.rmdir()
            # if an OSError occurs (e.g., directory not empty), break the loop
            except OSError:
                break
            # move up to the next parent directory
            parent = parent.parent

    # Use a temporary directory to stage files and create backups for rollback
    with TemporaryDirectory(
        prefix=".hallmark-checkout-", dir=repo.worktree) as temporary_directory:
        # Create paths for staging and backup within the temporary directory
        transaction_root = Path(temporary_directory)
        staged_root = transaction_root / "staged"
        backup_root = transaction_root / "backup"

        # files that will be restored from object store before switching branches
        staged_files = []
        # try to restore the target files from object store into the staging area
        try:
            for index, (relative_path, sha1) in enumerate(changed_target_entries):
                staged_path = staged_root / str(index)
                repo.objects.restore(sha1, staged_path)
                staged_files.append((relative_path, staged_path))
        # raise a CheckoutError if any exception occurs during the restoration
        except Exception as exc:
            raise CheckoutError(
                f'cannot checkout branch "{target_branch}": '
                f"failed to prepare target files: {exc}") from exc

        backups: list[tuple[Path, Path]] = []
        installed_paths: list[Path] = []
        # try to switch branches and update the worktree with the target files
        try:
            # if the target branch is new, create and switch to it
            if has_local:
                repo.dothm.git.checkout(target_branch)

            elif has_remote:
                repo.dothm.git.checkout("--track",remote_revision)

            else:
                repo.dothm.git.checkout("-b", target_branch)

            # Reload the repository state after switching branches
            repo.state = repo.dothm.load_state()

            # paths that are either currently tracked but not in the target branch
            # or paths that are tracked but have changed from the current branch
            affected_paths = sorted((current_tracked - target_tracked
                                     ) | changed_target_paths,
                key=lambda path: (len(path.parts), path.as_posix()), reverse=True)
            for relative_path in affected_paths:
                # absolute path in the worktree for the affected relative path
                path = repo._resolve_worktree_path(relative_path, label="checkout target")
                # create a backup before replacing it with the target file
                if path.exists():
                    backup_path = backup_root / str(len(backups))
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(backup_path)
                    backups.append((path, backup_path))
                    remove_empty_parents(path)

            for relative_path, staged_path in staged_files:
                # Determine the destination path in the worktree for the staged file
                destination = repo._resolve_worktree_path(
                    relative_path, label="checkout target")
                # Create parent directories for the destination path
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Replace the destination path with the staged file
                staged_path.replace(destination)
                # add the destination path to the list of installed paths
                installed_paths.append(destination)
        # attempt to rollback changes if any exception occurs
        except Exception as exc:
            rollback_errors = []

            # try to restore the original branch if it was changed during checkout
            try:
                if repo.dothm.active_branch.name != original_branch:
                    repo.dothm.git.checkout(original_branch)
            # if restoring the original branch fails, record the error for reporting
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"could not restore branch: {rollback_exc}")

            for path in reversed(installed_paths):
                # try to remove the installed files from the failed checkout
                try:
                    # if the path is a file or a symlink, unlink it
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    remove_empty_parents(path)
                # if removing the installed file fails, record error for reporting
                except Exception as rollback_exc:
                    rollback_errors.append(
                        f'could not remove "{path}": {rollback_exc}')

            for original_path, backup_path in reversed(backups):
                # try to restore the original files from the backups
                try:
                    original_path.parent.mkdir(parents=True, exist_ok=True)
                    backup_path.replace(original_path)
                # if restoring the original file fails, record error for reporting
                except Exception as rollback_exc:
                    rollback_errors.append(
                        f'could not restore "{original_path}": '
                        f"{rollback_exc}")

            # if the target branch was newly created and exists in the repository
            if create_new_branch and target_branch in {
                head.name for head in repo.dothm.heads}:
                # try to delete the newly created branch to rollback the checkout
                try:
                    repo.dothm.git.branch("-D", target_branch)
                # if deleting the new branch fails, record error for reporting
                except GitCommandError as rollback_exc:
                    rollback_errors.append(
                        f"could not remove new branch: {rollback_exc}")

            # try to reload the repository state after rollback
            try:
                repo.state = repo.dothm.load_state()
            # if reloading the repository state fails, record error for reporting
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"could not reload repository state: {rollback_exc}")
            # construct a detailed error message for the failed checkout
            message = (f'checkout of branch "{target_branch}" failed: {exc}')
            # if there were any errors during rollback, append them to the message
            if rollback_errors:
                message += "; rollback incomplete: " + "; ".join(rollback_errors)
            # raise a CheckoutError with the detailed message and original exception
            raise CheckoutError(message) from exc
    # if checkout process completes successfully, return True to indicate success
    return True


def add_worktree(repo, target_branch: str) -> bool:
    '''
    Create or link a new worktree for a branch. Raises ValueError if branch name
    is invalid.
    Raises RuntimeError if called in a bare repository or worktree creation fails.

    Args:
        target_branch (string): Name of the branch to attach.
    Returns:
        boolean: True if the worktree was successfully created.
    '''
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
