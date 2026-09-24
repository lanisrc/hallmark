from __future__ import annotations

from pathlib import Path

from .error import CheckoutError
from .helper_functions import (
    SymlinkPathError, resolve_contained_path, validate_relative_path)
from .repo_manifest import manifest_map, iter_manifest_entries


def effective_cwd(repo) -> Path:
    '''
    Determine the effective working directory for repository operations.
    Raises RuntimeError if the repository has no worktree

    Args:
        repo (repo): repository object
    Returns:
        Path: The current working directory if it is inside the repository
        worktree; otherwise, the worktree root.
    '''
    if repo.worktree is None:
        raise RuntimeError("cannot inspect files in a bare repository " \
        "without a worktree")

    cwd = Path.cwd().resolve()
    worktree = Path(repo.worktree).resolve()
    try:
        cwd.relative_to(worktree)
    except ValueError:
        return worktree
    return cwd


def filtered_paraframe(repo, pf):
    '''
    Filter a ``ParaFrame`` to include only files that are within the effective working
    directory of the repository. Raise ValueError if the repository has no worktree.

    Args:
        repo (Repo): Repository object.
        pf (ParaFrame): ParaFrame to filter.

    Returns:
        ParaFrame: Filtered containing only files within the effective working directory

    Raises:
        ValueError: If the repository has no worktree.
    '''
    # Determine the effective working directory for repository operations
    root = effective_cwd(repo)
    # resolve the repository's worktree path to an absolute path
    worktree = Path(repo.worktree).resolve()
    # if the effective working directory is the same as the worktree root
    if root == worktree:
        # return the original paraframe without filtering
        return pf

    # get the relative path of the working directory with respect to the worktree root
    relative_root = root.relative_to(worktree)

    def is_within_root(value) -> bool:
        """Check if a given path is within the effective working directory."""
        # try to validate the relative path
        try:
            relative_path = validate_relative_path(value, label="matched data path")
            # check if it is within the effective working directory
            relative_path.relative_to(relative_root)
        # if the path is not valid, return False
        except ValueError:
            return False
        # if the check passes, return True
        return True

    # filter the paraframe to include only paths that are within the working directory
    return pf[pf["path"].map(is_within_root)]


def tracked_paths(repo) -> set[Path]:
    '''
    Return the set of tracked file paths.

    Args:
        repo (Repo): Repository object.

    Returns:
        set[Path]: Paths of all local files tracked in the current
        repository state. Catalog-only files, such as remote files, are
        not managed in the worktree and are excluded.
    '''
    # use iter_manifest_entries to iterate over the local entries and collect paths
    return {path for path, _ in iter_manifest_entries(repo.state)}


def worktree_changes(repo, expected_checksums: dict[str, str]
                     ) -> tuple[list[str], list[str]]:
    """
    Return modified and missing tracked worktree paths.

    Args:
        repo: Repository being inspected.
        expected_checksums: Mapping of relative paths to expected SHA-1
            checksums.

    Returns:
        tuple[list[str], list[str]]: Modified paths followed by missing
        paths.
    """
    # if the repository has no worktree, raise a RuntimeError
    if repo.worktree is None:
        raise RuntimeError("cannot inspect files without a worktree")

    existing_files: list[tuple[str, Path, str]] = []
    missing: list[str] = []
    # for each relative path and its expected checksum
    for relative_path, expected_sha1 in expected_checksums.items():
        try:
            # resolve the relative path to an absolute path within the worktree
            full_path = resolve_contained_path(
                repo.worktree,
                relative_path,
                label="tracked path")
        # if the path crosses a symbolic link, add it to the missing list and continue
        except SymlinkPathError:
            missing.append(relative_path)
            continue

        # if the full path does not exist as a file, add it to the missing list
        if not full_path.is_file():
            missing.append(relative_path)
            # skip since the file is missing and cannot be checked for modifications
            continue
        # add the relative path, full path, and expected checksum to existing_files
        existing_files.append((relative_path, full_path, str(expected_sha1).lower()))

    # create a list of full paths from existing_files for checksum calculation
    full_paths = [
        full_path
        for _, full_path, _ in existing_files]
    # compute actual checksums for all existing files in parallel
    actual_checksums = repo.checksum_many(full_paths)
    # modified contains relative paths of files whose actual checksum does
    # not match the expected checksum
    modified = [
        relative_path
        for relative_path, full_path, expected_sha1
        in existing_files
        if actual_checksums[full_path] != expected_sha1]

    return modified, missing


def ensure_clean_tracked_files(repo) -> None:
    '''
    Verify that tracked files and repository state are clean. No returns.
    Raises CheckoutError if the reposiotry has no worktree, a tracked
    file is missing, a tracked file has uncommited changes, or the
    hallmark state contains uncommitted changes.

    Args:
        repo (Repo): Repository object
    Returns:
        None.
    '''
    # if the repository has no worktree, raise a CheckoutError
    if repo.worktree is None:
        raise CheckoutError("cannot checkout without a worktree")

    # get the expected checksums for all local tracked files in the repository
    expected_checksums = manifest_map(repo.state)
    # try to get the modified and missing tracked files using worktree_changes
    try:
        modified, missing = worktree_changes(repo, expected_checksums)
    # if a ValueError occurs during the worktree_changes call, raise a CheckoutError
    except ValueError as exc:
        raise CheckoutError(str(exc)) from exc

    modified_set = set(modified)
    missing_set = set(missing)
    for relative_path in expected_checksums:
        # if the relative path is in the missing set, raise a CheckoutError
        if relative_path in missing_set:
            raise CheckoutError(
                f'tracked file "{relative_path}" is missing; '
                "commit or restore it before checkout")
        # if the relative path is in the modified set, raise a CheckoutError
        if relative_path in modified_set:
            raise CheckoutError(
                f'tracked file "{relative_path}" has uncommitted '
                "changes; commit them before checkout")

    # if the repository's hallmark state has uncommitted changes, raise a CheckoutError
    if repo.dothm.index.diff("HEAD"):
        raise CheckoutError(
            "you have uncommitted hallmark state changes — " \
            "commit them before checkout")