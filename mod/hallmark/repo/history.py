"""
Load the state of other revisions, and fetch their objects from local remotes.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Iterable


from .objects import Objects
from .state import State

def _copy_current_state(repo, *, include_data: bool) -> State:
    """
    Used by load_head_state and load_branch_state.
    Create an independent copy of the repository's current state.
    Args:
        repo: The repository object.
        include_data: Whether to include the current data in the copied state.

    Returns:
        (State) An independent copy of the repository's current state.
    """
    # if include_data is True, copy current data; otherwise, create an empty DataFrame
    data = (repo.state.data.copy() if include_data else State().data.copy())

    # new State object with deep copies of current config and meta, and the copied data
    return State(
        config=deepcopy(repo.state.config), meta=deepcopy(repo.state.meta), data=data)


def _load_revision_state(repo, revision: str) -> State:
    """
    Load the state from a specific Git revision.

    Args:
        repo:     The repository object.
        revision: The Git revision (commit hash, branch name, etc.) to load.

    Returns:
        (State) The state loaded from the specified Git revision.
    """
    return repo.dothm.load_state(revision)


def load_branch_state(repo, branch: str) -> State:
    '''
    Load the state associated with a branch

    Args:
        repo (Repo): repository object
        branch (String): branch name
    Returns:
        (State) A ``State`` constructed from the ``config.yml``, ``meta.yml``,
        and ``data.tsv`` files at the specified branch. If no state can be loaded
        from the branch, returns a state with the current configuration and
        metadata and an empty data table.
    '''
    # get the names of all local branches in the repository
    local_branch_names = {head.name for head in repo.dothm.heads}
    # if the specified branch exists locally, load the state from that branch
    if branch in local_branch_names:
        return _load_revision_state(repo, branch)

    # otherwise, look for a matching remote-tracking branch and load its state
    remote_revision = find_remote_branch(repo, branch)
    if remote_revision is not None:
        return _load_revision_state(repo, remote_revision)

    # if the specified branch does not exist locally or on any remote, return a
    # state with the current configuration and metadata and an empty data table
    return _copy_current_state(repo, include_data=True)


def load_head_state(repo) -> State:
    """
    Load the state associated with the HEAD revision of the repository.

    Args:
        repo: The repository object.

    Returns:
        (State) The state loaded from the HEAD revision. If the HEAD is not valid,
        returns an empty state.
    """
    if not repo.dothm.head.is_valid():
        return State()
    return _load_revision_state(repo, "HEAD")


def find_remote_branch(repo, branch: str) -> str | None:
    """
    Find the first remote reference containing the requested branch.

    Args:
        repo (Repo): Repository object.
        branch (str): Branch name, without a remote prefix.

    Returns:
        str | None: A Git revision such as ``"origin/main"``, or None if the
        branch does not exist on any remote.
    """
    for remote in repo.dothm.remotes:
        for ref in remote.refs:
            if ref.remote_head == branch:
                return f"{remote.name}/{branch}"

    return None


def fetch_missing_objects_from_remote(
    repo, remote_name: str, sha1s: Iterable[str]) -> set[str]:
    """
    Copy any of the given checksums that are available in a local-path git
    remote's object store into the repository's own object store.

    Only remotes backed by a local filesystem ".hm" directory (the layout
    produced by ``Repo.clone``) can be resolved this way; other remotes
    (e.g. a plain git server over ssh/https) are left untouched.

    Args:
        repo: Repository object.
        remote_name: Name of the git remote to resolve (e.g. "origin").
        sha1s: SHA-1 checksums to look for on the remote.
    Returns:
        set[str]: The checksums that are still missing after the attempt.
    """
    still_missing = set(sha1s)
    if not still_missing:
        return still_missing

    try:
        remote = repo.dothm.remotes[remote_name]
    except IndexError:
        return still_missing

    remote_objects = Objects(Path(remote.url))
    for sha1 in list(still_missing):
        if remote_objects.contains(sha1):
            repo.objects.store(remote_objects.object_path(sha1), sha1)
            still_missing.discard(sha1)

    return still_missing
