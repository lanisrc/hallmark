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


from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from .branches import checkout, add_worktree
from ..remote.add import add_remote, is_remote_catalog
from .dothm import Dothm
from .state import State
from .worktree import Worktree
from .objects import Objects
from ..paraframe import ParaFrame
from .manifest import build_file_table, file_versions_by_path, iter_manifest_entries
from .history import (
    load_head_state)
from ..error import DestinationExistsError, DothmError
from ..utils import (
    FILE_IO_CHUNK_SIZE,
    use_working_directory,
    iter_repository_files,
    require_nonempty_string,
    resolve_path_in_root)
from .changes import (
    filter_files_in_directory,
    find_changed_and_missing_files)
from .config import (
    branch_encodings,
    branch_filename_format,
    set_config,
    single_data_format)

@dataclass(init=False)
class Repo:
    """
    Hallmark repository.

    This is the Python API boundary.
    It loads the in-memory ``State`` from repository ``Dothm``, and
    potentially populate the ``Worktree``.
    """

    state: State
    dothm: Optional[Dothm] = None
    worktree: Optional[Worktree] = None
    download_result: Optional[dict] = None

    @staticmethod
    def resolve_repo_paths(path: Union[Path, str]) -> Tuple[Path, Optional[Path]]:
        '''
        Resolve repository and worktree paths.

        Args:
            path (Path | str): Path to either a worktree or a
            ``.hm`` repository.

        Returns:
            tuple[Path, Path | None]: A ``(dothm_path, worktree_path)`` tuple.
            If ``path`` refers to a ``.hm`` directory, ``worktree_path`` is ``None``.
        '''
        path = Path(path).resolve()
        if path.name == ".hm" or path.suffix == ".hm":
            return path, None
        return path / ".hm", path

    @staticmethod
    def find_root(start: Union[Path, str] = ".") -> Optional[Path]:
        """
        Locate the repository containing a folder without opening it.

        Args:
            start (Path | str): Folder to search from. Parent folders are
                searched in turn.

        Returns:
            Path | None: The worktree, or the bare ``.hm`` repository, of the
            nearest enclosing repository; None if there is none.
        """
        start = Path(start).expanduser().resolve()
        for folder in (start, *start.parents):
            # A bare repository is itself the ".hm" folder, as in Repo(path).
            if folder.name == ".hm" or (
                    folder.suffix == ".hm" and (folder / ".git").exists()):
                return folder
            # Stop at a damaged ".hm" too, so opening it can explain the damage.
            if (folder / ".hm").exists() or (folder / ".hm").is_symlink():
                return folder
        return None

    @classmethod
    def find(cls, start: Union[Path, str] = ".") -> "Repo":
        """
        Open the repository containing a folder, searching parent folders.

        ``Repo(path)`` opens exactly ``path``; ``find`` lets commands run from
        any folder inside a worktree.

        Args:
            start (Path | str): Folder to search from. Defaults to the current
                folder.

        Returns:
            Repo: The nearest enclosing repository.

        Raises:
            DothmError: If neither the folder nor any parent contains ``.hm``.
        """
        root = cls.find_root(start)
        if root is None:
            raise DothmError(
                f'Not a Hallmark repository (or any parent folder): '
                f'"{Path(start).expanduser().resolve()}"; '
                "run hm init or hm clone")
        return cls(root)

    def __init__(self, path: Union[Path, str]) -> None:
        '''
        Open an existing hallmark repository.

        Args:
            path: path to a worktree or '.hm repository'/
        Returns:
            none.
        '''
        dothm_path, worktree_path = self.resolve_repo_paths(path)
        # Opening only reads: a damaged repository is explained, not repaired.
        try:
            self.dothm = Dothm(dothm_path)
        except InvalidGitRepositoryError as exc:
            raise DothmError(
                f'Repository at "{dothm_path}" is damaged: it is not a Git '
                "worktree; nothing was changed") from exc
        except NoSuchPathError as exc:
            # A missing .hm keeps the GitPython error; a dangling link is damage.
            if not Path(dothm_path).is_symlink():
                raise
            raise DothmError(
                f'Repository at "{dothm_path}" is damaged: it links to a folder '
                "that does not exist; nothing was changed") from exc
        self.worktree = worktree_path and Worktree(worktree_path)
        try:
            self.state = self.dothm.load_state()
        except DothmError as exc:
            raise DothmError(f"{exc}; nothing was changed") from exc
        self.download_result = None

        # Linked worktrees share the object store of the main ".hm".
        self.objects = Objects(Path(self.dothm.common_dir).resolve().parent)

    def _resolve_worktree_path(self, value, *, label: str = "tracked path") -> Path:
        """
        Used commit, checkout, and _calculate_file_checksums.
        Resolve a path relative to the repository's worktree.
        Args:
            value (str | Path): The path to resolve.
            label (str): Label for error messages.
        Returns:
            Path: Resolved path within the worktree.
        Raises:
            RuntimeError: If the repository has no worktree.
        """
        # Validate that the repository has a worktree before resolving paths
        if self.worktree is None:
            raise RuntimeError(
                "cannot resolve paths without a worktree")
        return resolve_path_in_root(self.worktree, value, label=label)

    def _validate_branch_name(self, value) -> str:
        """
        Used by checkout and add_worktree
        Validate and normalize a Git branch name.

        Args:
            value (str): The branch name to validate.

        Returns:
            str: The normalized branch name.

        Raises:
            ValueError: If the branch name is invalid.
        """
        # Normalize the branch name to ensure it is a non-empty string
        branch_name = require_nonempty_string(value, label="branch name")
        # if the branch name starts with a hyphen, raise a ValueError
        if branch_name.startswith("-"):
            raise ValueError(f"invalid branch name: {branch_name!r}")

        # try to validate the branch name using Git's check_ref_format command
        try:
            self.dothm.git.check_ref_format("--branch", branch_name)
        # if Git raises a GitCommandError, re-raise it as a ValueError
        except GitCommandError as exc:
            raise ValueError(f"invalid branch name: {branch_name!r}") from exc

        # if all checks pass, return the normalized branch name
        return branch_name

    def _calculate_file_checksums(self, pf: ParaFrame) -> None:
        """
        Used by add.
        Populate the "sha1" column in a ParaFrame with SHA-1 checksums of the files.
        Args:
            pf (ParaFrame): The ParaFrame containing file paths.
        """
        # If the ParaFrame is empty, there are no files to process, so return early
        if pf.empty:
            return
        # Resolve full paths for all files in the ParaFrame relative to the worktree
        full_paths = [
            self._resolve_worktree_path(path, label="matched data path")
            for path in pf["path"].astype(str)]
        # Compute SHA-1 checksums for all files in parallel using the checksum_many
        checksums = self.checksum_many(full_paths)
        # Populate the "sha1" column in the ParaFrame with the computed checksums
        pf["sha1"] = [checksums[path] for path in full_paths]

    @classmethod
    def init(cls, path: Union[Path, str] = ".") -> "Repo":
        """
        Initialize an empty local repository.

        Existing destination files are preserved; an existing ``.hm``
        is rejected. Initialization does not discover or download data.

        Args:
            path (Path | str): Worktree or bare ``.hm`` repository destination.

        Returns:
            Repo: Initialized repository.

        Raises:
            DestinationExistsError: If initialization would replace a catalog
                or follow a destination symlink.
        """
        if Path(path).is_symlink():
            raise DestinationExistsError(f"Repository destination is a symlink: {path}")
        dothm_path, worktree_path = cls.resolve_repo_paths(path)
        if dothm_path.exists() or dothm_path.is_symlink():
            raise DestinationExistsError(
                f"Hallmark repository already exists: {dothm_path}")
        dothm = Dothm.init(dothm_path)
        (dothm.path / "config.yml").write_text(Dothm.config_template(),
                                               encoding="utf-8")
        dothm.write_yaml({}, "meta")
        dothm.write_tsv(State().data, "data")
        dothm.index.add(["config.yml", "meta.yml", "data.tsv"])
        if worktree_path is not None:
            Worktree.init(worktree_path)
        return cls(path)

    def _download_cloned_files(self, *, approve, max_workers, progress,
                                 filter=None, fmt=None):
        """Run an approved transfer after catalog creation has completed."""
        from ..remote.download import DownloadError

        plan = self.plan_download(filter=filter, fmt=fmt)
        if plan.file_count and approve is not None and not approve(plan):
            return
        self.download_result = self.download(
            plan, approved=True, max_workers=max_workers, progress=progress)
        if self.download_result["failed"]:
            details = "\n".join(self.download_result["errors"][:5])
            raise DownloadError(
                f"Failed to download {self.download_result['failed']} "
                f"file(s):\n{details}\n"
                f'Catalog kept at "{self.dothm.path}". '
                "Successfully downloaded files were kept.")

    @classmethod
    def clone(
        cls,
        url: str,
        path: Union[Path, str],
        *,
        auth: Optional[str] = None,
        filter=None,
        fmt: Optional[str] = None,
        source_type: str = "auto",
        progress: bool = False,
        download: bool = True,
        approve=None,
        max_workers: int = 4,
    ) -> "Repo":
        """
        Clone an existing Hallmark Git catalog or published snapshot.

        Git sources retain their history. Published catalog snapshots
        start new local history. Dataset files download by default after
        catalog creation, without prompting unless an approval callback is supplied.
        Declining approval leaves the catalog available without dataset files.

        Args:
            url (str): Existing Git catalog or published snapshot location.
            path (Path | str): New worktree or bare ``.hm`` repository path.
            auth (str, optional): Local profile for snapshot metadata access.
                Git sources use Git's authentication configuration instead.
            filter (str | list[str], optional): Download selection globs.
                Requires ``download=True``; the catalog remains complete.
            fmt (str, optional): Filename format used to select downloads.
                Requires ``download=True``.
            source_type (str): ``auto``, ``git``, or ``catalog``.
                Defaults to automatic source detection.
            progress (bool): Show download progress. Defaults
                to False.
            download (bool): Request downloads after preparing the catalog.
                Defaults to True. Bare destinations require ``download=False``.
            approve (callable, optional): Called with a nonempty download plan.
                Return True to permit the transfer. Without a callback,
                downloads proceed without prompting. Declining keeps the catalog.
            max_workers (int): Maximum download workers. Defaults to 4.

        Returns:
            Repo: The cloned repository. ``download_result`` contains results
            when a download was attempted, including an empty selection.

        Raises:
            DestinationExistsError: If the destination already exists.
            CloneError: If a requested catalog is missing or invalid.
            ValueError: If source options are invalid or conflict.
            DownloadError: If metadata access or downloading fails, or a download
                is requested for a bare destination.
        """
        from ..remote.clone import clone_catalog
        from ..remote.discovery import path_matches
        from ..remote.download import DownloadError, _require_positive_integer

        _require_positive_integer(max_workers, label="max_workers")
        if (filter is not None or fmt is not None) and not download:
            raise ValueError("clone filter and fmt require download=True; "
                             "use init() followed by add(URL) to select a new catalog")
        if download and cls.resolve_repo_paths(path)[1] is None:
            raise DownloadError(
                "Bare clones require download=False; use a worktree to download data")
        path_matches("validation", filter=filter, fmt=fmt)
        repo = clone_catalog(cls, url, path, auth=auth, source_type=source_type)
        if download:
            repo._download_cloned_files(
                approve=approve, max_workers=max_workers, progress=progress,
                filter=filter, fmt=fmt)
        return repo

    def plan_download(self, output_path=None, *, file_paths=None, tsv_names=None,
                      all_files=False, filter=None, fmt=None, remote_name=None,
                      estimated_bytes_per_second=None):
        """
        Plan a download using the local catalog without contacting a server.

        With no explicit paths or TSVs, select the complete catalog before
        applying any filter or filename format. Planning does not approve
        the transfer.

        Args:
            output_path (Path | str, optional): Destination directory. Defaults
                to the worktree; required for a bare repository.
            file_paths (sequence[str], optional): Remote-relative file paths.
            tsv_names (sequence[str], optional): Catalog TSVs to select.
            all_files (bool): Select all configured files. Cannot be combined
                with explicit paths or TSVs. Defaults to False.
            filter (str | list[str], optional): Relative path glob or globs.
            fmt (str, optional): Filename format that selected paths must match.
            remote_name (str, optional): Configured data remote to use.
            estimated_bytes_per_second (float, optional): Positive transfer
                rate for duration estimates. No rate is measured while planning.

        Returns:
            DownloadPlan: Immutable selection with its source, destination,
            checksums, and available file metadata.

        Raises:
            DownloadError: If the catalog, remote, selection, or destination
                is invalid.
            ValueError: If a filter, format, or supplied rate is invalid.
        """
        from ..remote.download import plan_download

        return plan_download(
            self, output_path, file_paths=file_paths, tsv_names=tsv_names,
            all_files=all_files, filter=filter, fmt=fmt, remote_name=remote_name,
            estimated_bytes_per_second=estimated_bytes_per_second)

    def download(self, plan, *, approved=False, max_workers=4, progress=False):
        """
        Download the files in an approved plan.

        The plan fixes the source and destination even if repository settings
        change. Successful files remain available when another transfer fails.

        Args:
            plan (DownloadPlan): Plan returned by ``plan_download``.
            approved (bool): Must be True for a nonempty transfer. Defaults
                to False; an empty plan requires no approval.
            max_workers (int): Maximum concurrent download workers. Defaults
                to 4; the local SSH session limit may reduce concurrency.
            progress (bool): Show byte progress. Defaults to False.

        Returns:
            dict: Results with ``succeeded``, ``failed``, ``total_bytes``, and
            ``errors`` keys. Also stored in ``download_result``. Individual
            transfer failures are recorded in ``failed`` and ``errors``.

        Raises:
            TypeError: If ``plan`` is not a DownloadPlan.
            DownloadError: If approval is missing, setup fails, or the
                destination is invalid.
        """
        from ..remote.download import execute_download_plan

        result = execute_download_plan(
            self, plan, approved=approved, max_workers=max_workers,
            show_progress=progress)
        self.download_result = result
        return result

    @staticmethod
    def checksum(path: Path, chunk_size: int = FILE_IO_CHUNK_SIZE) -> str:
        """
        Compute a file's SHA-1 checksum.
        Args:
            path (Path): Path to the file.
            chunk_size (int): Size of chunks to read at a time.
        Returns:
            str: SHA-1 checksum of the file.
        """
        return Objects._calculate_sha1(path, chunk_size=chunk_size)

    @staticmethod
    def checksum_many(paths: list[Path]) -> dict[Path, str]:
        """
        Hash multiple files concurrently in a thread pool this method owns, hashing
        each unique path only once even if it appears more than once in paths.
        Args:
            paths (list[Path]): List of file paths to hash.
        Returns:
            dict[Path, str]: Dictionary mapping each file path to its SHA1 checksum.
        """
        # get unique paths to avoid redundant checksum calculations
        unique_paths = list(dict.fromkeys(paths))
        # If the list of paths is empty, return an empty dictionary
        if not unique_paths:
            return {}
        # hash all unique paths concurrently using a fresh thread pool
        with ThreadPoolExecutor() as executor:
            # map the Repo.checksum function to all paths using the executor
            checksums = executor.map(Repo.checksum, unique_paths)
            # pair each path with its corresponding checksum and return as a dictionary
            return dict(zip(unique_paths, checksums))

    def add_paths(self, paths: List[Union[Path, str]]) -> ParaFrame:
        '''
        Add explicit file paths to the repository index. Raises RuntimeError.
        Operation not supported in Hallmark.
        '''
        raise RuntimeError(
            'explicit path add is not supported while data.tsv ' \
            'stores only sha1 plus fmt fields')

    def set_config(
        self,
        *,
        fmt: Optional[str] = None,
        remote_name: Optional[str] = None,
        remote_url: Optional[str] = None,
        encoding_updates: Optional[Dict[str, str]] = None,
        remote_auth: Optional[str] = None,
        remote_backend: Optional[str] = None,
        remote_backend_options: Optional[dict] = None,
    ) -> dict:
        """
        Update repository configuration values.

        Args:
            fmt (str, optional): Data format specification.
            remote_name (str, optional): Name of the remote repository.
            remote_url (str, optional): URL of the remote repository.
            remote_auth (str, optional): Local SSH profile name. An empty string
                removes the reference; None leaves it unchanged.
            remote_backend (str, optional): Registered data backend name.
            remote_backend_options (dict, optional): Backend-specific configuration.
            encoding_updates (dict[str, str], optional): Updates to encoding rules.

        Returns:
            dict: Updated configuration dictionary.
        """
        set_config(
            self,
            fmt=fmt,
            remote_name=remote_name,
            remote_url=remote_url,
            encoding_updates=encoding_updates,
            remote_auth=remote_auth,
            remote_backend=remote_backend,
            remote_backend_options=remote_backend_options)
        self.dothm.save_state(self.state)
        return self.state.config

    def status(self) -> dict[str, object]:
        """
        Return repository status information. Includes staged changes,
        worktree modifications, deletions, and untracked files.

        Args:
            self: Repository instance.

        Returns:
            dict[str, object]: Status summary including:
            - branch (str)
            - staged changes (dict)
            - worktree changes (dict)
            - untracked files (list[str])
        """
        head_state = load_head_state(self)
        head_map = file_versions_by_path(head_state)
        staged_map = file_versions_by_path(self.state)
        state_changes = sorted({
            diff.a_path or diff.b_path
            for diff in self.dothm.index.diff("HEAD")
            if diff.a_path or diff.b_path
        })

        staged_added = sorted(path for path in staged_map if path not in head_map)
        staged_deleted = sorted(path for path in head_map if path not in staged_map)
        staged_modified = sorted(
            path for path in staged_map
            if path in head_map and staged_map[path] != head_map[path]
        )

        remote_catalog = is_remote_catalog(self.state)
        worktree_modified: list[str] = []
        worktree_deleted: list[str] = []
        staged_paths = set(staged_map)

        # If the repository has a worktree, check for modified and missing tracked files
        if self.worktree is not None:
            if not remote_catalog:
                worktree_modified, worktree_deleted = find_changed_and_missing_files(
                    self, staged_map
                )
            worktree_root = Path(self.worktree)
            # generator that yields relative paths of all files in the worktree
            worktree_files = (full_path.relative_to(worktree_root).as_posix()
                              for full_path in iter_repository_files(worktree_root))
            # filter out staged paths from the worktree files
            untracked = sorted(path for path in worktree_files
                               if path not in staged_paths)
        else:
            untracked = []

        return {
            "branch": self.dothm.active_branch.name,
            "staged": {
                "state": state_changes,
                "added": staged_added,
                "modified": staged_modified,
                "deleted": staged_deleted,
            },
            "worktree": {
                "modified": sorted(worktree_modified),
                "deleted": sorted(worktree_deleted),
            },
            "untracked": untracked,
            "remote_catalog": remote_catalog,
        }

    def add(self, fmt: str, encoding: bool = False, *, filter=None,
            remote_fmt=None, auth=None, backend=None, backend_options=None,
            progress=False) -> ParaFrame:
        '''
        Stage local file metadata or discover a remote catalog without downloading.

        Args:
            fmt (string): Local filename format, "." to rescan, or remote URL.
            encoding (boolean): Apply local filename encoding rules.
            filter (string | list[string], optional): Remote path selection globs.
            remote_fmt (string, optional): Remote filename format for parameters.
            auth (string, optional): Local profile for remote authentication.
            backend (string, optional): Registered data backend name.
            backend_options (dict, optional): Backend configuration.
            progress (boolean): Show remote discovery progress.
        Returns:
            ParaFrame: Local parameters without checksums, or remote catalog rows.
        '''
        if isinstance(fmt, str) and "://" in fmt:
            if encoding:
                raise ValueError("--regex is only supported for local data")
            return add_remote(self, fmt, fmt=remote_fmt, filter=filter, auth=auth,
                              backend=backend, backend_options=backend_options,
                              progress=progress)
        if any(value is not None for value in
               (filter, remote_fmt, auth, backend, backend_options)):
            raise ValueError("Remote options require a remote URL")
        if is_remote_catalog(self.state):
            raise ValueError("Use a remote URL to update this catalog")
        if self.worktree is None:
            raise RuntimeError(
                "cannot add files in a bare repository without a worktree")

        # Normalize the format string to ensure it is a non-empty string
        fmt = require_nonempty_string(fmt, label="format")
        # "." means rescan the whole worktree using the already-configured format
        rescanning = fmt == "."
        # use the current branch format; otherwise, use the provided format
        if rescanning:
            resolved_fmt = branch_filename_format(self)
            previous_fmt = resolved_fmt
        else:
            resolved_fmt = fmt
            try:
                previous_fmt = branch_filename_format(self)
            except RuntimeError:
                previous_fmt = None
        # with the working directory set to the worktree, parse files into a ParaFrame
        with use_working_directory(self.worktree):
            pf = ParaFrame.parse(
                resolved_fmt,
                base_path=self.worktree,
                encodings=branch_encodings(self) if encoding else None,
                encoding=encoding)
        # if rescanning, filter to include only files that match the configured format
        if rescanning:
            pf = filter_files_in_directory(self, pf)
        # Compute checksums for all files in the ParaFrame in parallel
        self._calculate_file_checksums(pf)

        manifest = build_file_table(pf, resolved_fmt)
        # if not rescanning, update the repository configuration with the new format
        if not rescanning:
            set_config(self, fmt=resolved_fmt)
        # an explicit fmt replaces only if the format actually changed
        if rescanning or previous_fmt != resolved_fmt:
            self.state.replace(manifest)
        # if the format is unchanged, update the existing state with new entries
        else:
            self.state.update(manifest)
        self.dothm.save_state(self.state)
        # return a ParaFrame without the "sha1" column for display purposes
        return pf.drop(columns=["sha1"], errors="ignore")

    def commit(self, msg: str, allow_empty: bool = False) -> bool:
        '''
        Commit staged changes to the repository. Raises ValueError if commit
        message is empty or invalid.

        Args:
            msg (string): commit message.
            allow_empty (boolean): Allow comitting even if no changes exists.
        Returns:
            boolean: True if a commit was created, false otherwise.
        '''
        # Normalize the commit message to ensure it is a non-empty string
        msg = require_nonempty_string(msg, label="commit message")
        # if allow_empty is False and there are no staged changes, return False
        if (not allow_empty and not self.dothm.index.diff("HEAD")):
            # return early since there are no changes to commit
            return False
        if is_remote_catalog(self.state) or self.state.data.empty:
            self.dothm.index.commit(msg)
            return True
        # get the current format string and the HEAD state of the repository
        current_fmt = branch_filename_format(self)
        head_state = load_head_state(self)
        # get the format string of the HEAD state for comparison
        head_fmt = single_data_format(head_state.config)
        # head entries are the set of (path, sha1) tuples from the HEAD state
        head_entries: set[tuple[Path, str]] = set()

        if head_fmt == current_fmt:
            # populate head_entries with the paths and checksums from the HEAD state
            head_entries = {(relative_path, checksum.lower())
                            for relative_path, checksum
                            in iter_manifest_entries(head_state, fmt=head_fmt)}

        # list of tuples containing (full path, expected sha1) for stored files
        files_to_store: list[tuple[Path, str]] = []
        # for each entry in the current manifest
        for relative_path, checksum in iter_manifest_entries(
                                        self.state, fmt=current_fmt):
            # get the expected SHA1 checksum for the file
            expected_sha1 = checksum.lower()
            # current_entry is a tuple of (relative path, expected sha1) for this file
            current_entry = (relative_path, expected_sha1)
            # if the manifest entry is not in the HEAD entries
            # or the object store does not contain the expected SHA1
            if (current_entry not in head_entries
                 or not self.objects.contains(expected_sha1)):
                # resolve the full path of the file in the worktree for storage
                full_path = self._resolve_worktree_path(
                    relative_path, label="tracked path")
                # append the full path and expected SHA1 to the list of files to store
                files_to_store.append((full_path, expected_sha1))

        # Create list of full paths from tracked_files for checksum calculation
        paths_to_hash = [path for path, _ in files_to_store]
        # Compute SHA-1 checksums for all tracked files in parallel
        actual_checksum_by_path = self.checksum_many(paths_to_hash)
        # Store each tracked file in the object store, verifying checksums
        for path, expected_sha1 in files_to_store:
            self.objects.store(path, expected_sha1,
                                actual_sha1=actual_checksum_by_path[path])
        # Commit the changes to the repository index with the provided message
        self.dothm.index.commit(msg)
        # Return True to indicate that a commit was created
        return True

    def log(self) -> str:
        '''
        Return commit history log.

        Returns:
            string: Git log output, or an empty string if no valid HEAD exists.
        '''
        if not self.dothm.head.is_valid():
            return ""
        return self.dothm.git.log()

    def branches(self) -> dict[str, object]:
        '''
        List repository branches.

        Returns:
            dictionary[string, object]: Dictionary containing:
                - ``current`` (string): Active branch name
                - ``names``: All branch names
        '''
        current = self.dothm.active_branch.name
        names = sorted(head.name for head in self.dothm.heads)
        return {"current": current, "names": names}

    def checkout(self, target_branch: str) -> bool:
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
        return checkout(self, target_branch)

    def add_worktree(self, target_branch: str) -> bool:
        '''
        Create or link a new worktree for a branch. Raises ValueError if branch name
        is invalid.
        Raises RuntimeError if called in a bare repository or worktree creation fails.

        Args:
            target_branch (string): Name of the branch to attach.
        Returns:
            boolean: True if the worktree was successfully created.
        '''
        return add_worktree(self, target_branch)
