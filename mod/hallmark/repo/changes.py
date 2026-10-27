"""Find worktree files that changed or went missing since they were committed."""

from __future__ import annotations

from pathlib import Path

from ..error import CheckoutError
from ..utils import (
    SymlinkPathError, resolve_path_in_root)
from .config import tracked_filename_format
from .manifest import file_versions_by_path, iter_manifest_entries


def working_directory_for_repo(repo) -> Path:
    '''
    Determine the effective working directory for repository operations.

    Args:
        repo (Repo): Repository object.

    Returns:
        Path: The current working directory if it is inside the repository
        worktree; otherwise, the worktree root.

    Raises:
        RuntimeError: If the repository has no worktree.
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


def tracked_paths(repo) -> set[Path]:
    '''
    Return the set of tracked file paths.

    Args:
        repo (Repo): Repository object.

    Returns:
        set[Path]: Paths of all files tracked in the current
        repository state.
    '''
    # get the filename format of the tracked files; None if there are none
    fmt = tracked_filename_format(repo.state)
    if fmt is None:
        return set()
    # use iter_manifest_entries to iterate over the manifest entries and collect paths
    return {path for path, _ in iter_manifest_entries(repo.state, fmt=fmt)}


def find_changed_and_missing_files(repo, expected_checksums: dict[str, str]
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
            full_path = resolve_path_in_root(
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

    # try to get the modified and missing tracked files using
    # find_changed_and_missing_files
    try:
        # determine the filename format; a state without one tracks no files
        fmt = tracked_filename_format(repo.state)
        # get the expected checksums for all tracked files in the repository
        expected_checksums = ({} if fmt is None
                              else file_versions_by_path(repo.state, fmt=fmt))
        modified, missing = find_changed_and_missing_files(repo, expected_checksums)
    # if the format is missing or a tracked path is invalid, raise a
    # CheckoutError
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