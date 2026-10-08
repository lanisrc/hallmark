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
import os
import parse

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from git.exc import GitCommandError

from .branches import (
    checkout, create_branch, current_branch, current_commit, add_worktree)
from ..remote.add import add_remote, is_remote_catalog
from .dothm import Dothm
from .state import State
from .worktree import Worktree
from .objects import Objects
from ..paraframe import ParaFrame
from .manifest import build_file_table, file_versions_by_path, iter_manifest_entries
from .history import (
    load_head_state)
from ..error import DestinationExistsError
from ..utils import (
    FILE_IO_CHUNK_SIZE,
    iter_repository_files,
    require_nonempty_string,
    resolve_path_in_root,
    apply_regex_replacement,
    try_numeric_conversion,
    validate_relative_path)
from .changes import find_changed_and_missing_files, working_directory_for_repo
from .config import (
    branch_encodings,
    branch_filename_format,
    set_config,
    single_data_format,
    filename_fields)

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

    def __init__(self, path: Union[Path, str]) -> None:
        '''
        Open an existing hallmark repository.

        Args:
            path: path to a worktree or '.hm repository'/
        Returns:
            none.
        '''
        dothm_path, worktree_path = self.resolve_repo_paths(path)
        self.dothm = Dothm(dothm_path)
        self.worktree = worktree_path and Worktree(worktree_path)
        self.state = self.dothm.load_state()
        self.download_result = None

        common = Path(self.dothm.common_dir).resolve().parent
        self.objects = Objects(common)
        dothm_objects = Path(dothm_path) / "objects"
        main_objects = common / "objects"
        if dothm_objects.resolve() != main_objects.resolve() \
        and not dothm_objects.exists():
            dothm_objects.symlink_to(main_objects)

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


    def _parse_local_files(self, fmt, encoding, paths=None):
        root = Path(self.worktree).resolve()
        encodings = branch_encodings(self) if encoding else None

        if "/" in fmt:
            return ParaFrame.parse(
                fmt, base_path=root, encodings=encodings, encoding=encoding
            )

        if paths is None:
            paths = (path.relative_to(root) for path in iter_repository_files(root))

        rows = []
        for parent in sorted({path.parent for path in paths}):
            found = ParaFrame.parse(
                fmt, base_path=root / parent,
                encodings=encodings, encoding=encoding
            )
            for row in found.to_dict(orient="records"):
                row["path"] = (parent / row["path"]).as_posix()
                rows.append(row)

        return ParaFrame(rows, base_path=root, encodings=encodings)


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


    def add_paths(self, paths: List[Union[Path, str]], encoding: bool = False
                  ) -> ParaFrame:

        # if the repository is a remote catalog, adding local paths is not allowed
        if is_remote_catalog(self.state):
            raise ValueError("Use a remote URL to update this catalog")
        # if the repository has no worktree, adding local paths is not possible
        if self.worktree is None:
            raise RuntimeError("cannot add files without a worktree")
        # ensure that the provided paths are local and not empty
        if not paths or any(
            "://" in str(path) or filename_fields(str(path))
            for path in paths
        ):
            raise ValueError("add_paths requires local file or directory paths")

        # parse the branch filename format for the repository
        try:
            fmt = branch_filename_format(self)
        # raise a ValueError if the branch filename does not have a valid format
        except RuntimeError as exc:
            raise ValueError(
                'No file pattern set. Run hm add "PATTERN" or '
                'hm set-config --fmt "PATTERN" first.'
            ) from exc

        # root is the absolute path to the repository's worktree
        root = Path(self.worktree).resolve()
        # base is the working directory for the repository
        base = working_directory_for_repo(self)
        # get the branch encodings if encoding is enabled
        encodings = branch_encodings(self) if encoding else None
        # find the encoding settings and parse format for the branch
        yaml_encodings, parse_fmt = ParaFrame._find_encoding_settings(
            fmt, encodings, encoding=encoding)
        # compile the parse format into a parser object
        parser = parse.compile(parse_fmt.lstrip("/"), case_sensitive=True)
        matched = {}
        unmatched = set()
        add_all = False

        for value in paths:
            # raw is the string representation of the path to be added
            raw = require_nonempty_string(str(value), label="add path")
            # if the raw path is ".", it represents the root of the repo's worktree
            if Path(raw) == Path("."):
                candidate = root
                add_all = True
            # otherwise, the candidate path is resolved relative to the base directory
            else:
                candidate = Path(os.path.normpath(base / raw))

            # check if the candidate path is within the repository's worktree
            try:
                relative = candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"add path is outside the worktree: {raw!r}"
                ) from exc

            # full_path is the absolute path to the candidate within the worktree
            full_path = (
                root if relative == Path(".")
                else self._resolve_worktree_path(relative, label="add path"))
            # if the full path is a directory, iterate over all files within it
            if full_path.is_dir():
                files = iter_repository_files(full_path)
                named_file = False
            # if the path is a regular file, treat it as a single file to be added
            elif full_path.is_file():
                files = (full_path,)
                named_file = True
            # if the path does not exist, raise an error
            elif not full_path.exists():
                raise FileNotFoundError(f"add path not found: {raw!r}")
            # if the path exists but is neither a file nor a directory, raise an error
            else:
                raise ValueError(f"add path is not a regular file: {raw!r}")

            for path in files:
                # get the relative path of the file with respect to the repository root
                relative = path.relative_to(root).as_posix()
                # if the relative path has already been matched, skip it
                if relative in matched:
                    continue
                # if the path contains a dir separator, use the full relative path
                # otherwise, use just the file name
                target = relative if "/" in fmt else path.name
                # apply encoding transformations to the target if specified
                if encoding:
                    target = apply_regex_replacement(target, yaml_encodings)

                # parse the target using the branch format parser
                match = parser.parse(target)
                # if the target does not match the branch format, handle it accordingly
                if match is None:
                    # if the file was explicitly named and doesn't match, raise an error
                    if named_file:
                        raise ValueError(
                            f"file does not match branch format {fmt!r}: "
                            f"{relative!r}")
                    # add the unmatched relative path to the set of unmatched paths
                    unmatched.add(relative)
                # if the target matches the format, add it to the matched dictionary
                else:
                    matched[relative] = {
                        **match.named,
                        "path": relative,
                        "sha1": self.checksum(path)}

        pf = ParaFrame(
            [matched[path] for path in sorted(matched)],
            columns=["path", "sha1", *filename_fields(fmt)],
            base_path=root,
            encodings=encodings,
        )
        for column in pf.columns:
            if column not in {"path", "sha1"}:
                pf[column] = try_numeric_conversion(pf[column])
        # build the file table manifest based on the parsed files
        manifest = build_file_table(pf, fmt)
        # update the repository configuration with the current format
        set_config(self, fmt=fmt)

        # update the manifest in the repository state
        if add_all:
            self.state.replace(manifest)
        else:
            self.state.update(manifest)
        self.dothm.save_state(self.state)

        # return the result DataFrame without the "sha1" column
        result = pf.drop(columns=["sha1"], errors="ignore")
        # attach the list of unmatched paths as an attribute to the result DataFrame
        result.attrs["unmatched"] = sorted(unmatched)
        return result

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
        staged_state = self.dothm.load_state(staged=True)
        head_state = load_head_state(self)
        head_map = file_versions_by_path(head_state)
        staged_map = file_versions_by_path(staged_state)

        if self.dothm.head.is_valid():
            changed_paths = {
                diff.a_path or diff.b_path
                for diff in self.dothm.index.diff("HEAD")
            }
        else:
            changed_paths = {
                path for path, stage in self.dothm.index.entries
            }

        state_changes = sorted(
            path for path in changed_paths
            if path and path != "versions.yml"
            and not path.startswith("versions/")
        )

        staged_added = sorted(path for path in staged_map if path not in head_map)
        staged_deleted = sorted(path for path in head_map if path not in staged_map)
        staged_modified = sorted(
            path for path in staged_map
            if path in head_map and staged_map[path] != head_map[path]
        )

        remote_catalog = is_remote_catalog(staged_state)
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

        for diff in self.dothm.index.diff(None):
            path = diff.a_path or diff.b_path
            if not path or Path(path).parts[0] in {
                "objects", "versions", "versions.yml"
            }:
                continue
            if diff.deleted_file:
                worktree_deleted.append(f".hm/{path}")
            else:
                worktree_modified.append(f".hm/{path}")

        untracked.extend(
            f".hm/{path}" for path in self.dothm.untracked_files
            if Path(path).parts[0] not in {
                "objects", "versions", "versions.yml"
            }
        )

        return {
            "branch": current_branch(self),
            "commit": current_commit(self),
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
            "untracked": sorted(untracked),
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
        # if the format string has no filename fields, treat it as a literal path
        if not filename_fields(fmt):
            # return early since there are no filename fields to parse
            return self.add_paths([fmt], encoding=encoding)
        pf = self._parse_local_files(fmt, encoding)

        # Compute checksums for all files in the ParaFrame in parallel
        self._calculate_file_checksums(pf)
        # Build the file table (manifest) from the ParaFrame using the resolved format
        manifest = build_file_table(pf, fmt)

        # Update the repository configuration with the new format
        set_config(self, fmt=fmt)
        # Update the repository state with the new manifest
        self.state.update(manifest)
        # Save the updated repository state
        self.dothm.save_state(self.state)
        # Return the ParaFrame without the "sha1" column for display purposes
        return pf.drop(columns=["sha1"], errors="ignore")


    def commit(self, msg: str, allow_empty: bool = False) -> bool:
        """
        Commit the staged changes to the repository. Staged changes are those that have
        been added to the index but not yet committed.

        Args:
            msg (str): The commit message.
            allow_empty (bool): Whether to allow empty commits.

        Returns:
            bool: True if a commit was made, False otherwise.
        """
        msg = require_nonempty_string(msg, label="commit message")
        if current_branch(self) is None:
            raise RuntimeError(
                "You're not on a branch. Run hm branch <name> then "
                "hm checkout <name> to save changes.")
        if not all(self.dothm.effective_identity()):
            raise RuntimeError(
                'Set your name and email first: hm config user.name "..." '
                'and hm config user.email "..."')
        changes = (
            self.dothm.index.diff("HEAD")
            if self.dothm.head.is_valid() else self.dothm.index.entries
        )
        if not allow_empty and not changes:
            return False

        staged_state = self.dothm.load_state(staged=True)
        if is_remote_catalog(staged_state) or staged_state.data.empty:
            self.dothm.index.commit(msg)
            return True

        current_fmt = single_data_format(staged_state.config)
        head_state = load_head_state(self)
        head_fmt = single_data_format(head_state.config)
        head_entries = set()

        if head_fmt == current_fmt:
            head_entries = {
                (relative_path, checksum.lower())
                for relative_path, checksum
                in iter_manifest_entries(head_state, fmt=head_fmt)
            }

        files_to_store = []
        for relative_path, checksum in iter_manifest_entries(
            staged_state, fmt=current_fmt
        ):
            expected_sha1 = checksum.lower()
            if (
                (relative_path, expected_sha1) not in head_entries
                or not self.objects.contains(expected_sha1)
            ):
                full_path = self._resolve_worktree_path(
                    relative_path, label="tracked path"
                )
                files_to_store.append((full_path, expected_sha1))

        actual_checksum_by_path = self.checksum_many(
            [path for path, _ in files_to_store]
        )
        for path, expected_sha1 in files_to_store:
            self.objects.store(
                path, expected_sha1,
                actual_sha1=actual_checksum_by_path[path],
            )

        self.dothm.index.commit(msg)
        return True

    def restore_staged(self, paths: list[str]) -> None:
        """
        Restore the staged files for the specified paths.

        Args:
            paths (list[str]): List of file paths to restore from the staging area.

        Raises:
            ValueError: If no paths are provided or specified path is outside worktree.
        """
        if not paths:
            raise ValueError("Usage: hm restore --staged <path> [<path> ...]")

        root = (
            Path(self.worktree).resolve()
            if self.worktree is not None else self.dothm.path.resolve().parent
        )
        base = Path.cwd().resolve()
        if not base.is_relative_to(root):
            base = root

        targets = []
        for value in paths:
            raw = require_nonempty_string(str(value), label="restore path")
            candidate = root if Path(raw) == Path(".") else Path(
                os.path.normpath(base / raw)
            )
            if not candidate.is_relative_to(root):
                raise ValueError(
                    f"restore path is outside the worktree: {raw!r}"
                )

            if candidate in (root, self.dothm.path):
                targets.append(".")
            elif candidate.is_relative_to(self.dothm.path):
                relative = candidate.relative_to(self.dothm.path)
                relative = validate_relative_path(
                    relative, label="restore path"
                )
                if relative.parts[0] in {
                    "objects", "versions", "versions.yml"
                }:
                    raise ValueError(
                        "Use dataset paths to restore file versions"
                    )
                targets.append(relative.as_posix())
            else:
                relative = validate_relative_path(
                    candidate.relative_to(root), label="restore path"
                )
                targets.append(f"versions/{relative.as_posix()}")

        self.dothm.restore_index(targets)
        self.state = self.dothm.load_state()

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
        current = current_branch(self)
        names = sorted(head.name for head in self.dothm.heads)
        detached_at = None if current is not None else current_commit(self)
        return {
            "current": current,
            "names": names,
            "detached_at": detached_at}

    def set_identity(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        '''
        Save the commit author name and email for this repository only.

        Args:
            name (string | None): Author name to store, if given.
            email (string | None): Author email to store, if given.
        '''
        self.dothm.set_identity(name=name, email=email)

    def identity(self) -> Tuple[Optional[str], Optional[str]]:
        '''
        Return the commit author name and email set for this repository.

        Returns:
            tuple[string | None, string | None]: The stored name and email,
            each ``None`` when it has not been set for this repository.
        '''
        return self.dothm.identity()

    def effective_identity(self) -> Tuple[Optional[str], Optional[str]]:
        '''
        Return the commit author name and email git will actually sign with.

        Values stored for this repository take precedence, falling back to the
        user's global and system git configuration.

        Returns:
            tuple[string | None, string | None]: The resolved name and email,
            each ``None`` when it is not configured at any level.
        '''
        return self.dothm.effective_identity()

    def create_branch(self, name: str) -> str:
        '''
        Create a branch at the current commit without switching to it.

        Args:
            name (string): Name for the new branch.
        Returns:
            string: The created branch name.
        Raises:
            ValueError: If the repository has no commits, the name is
                invalid, or a branch of that name already exists.
        '''
        return create_branch(self, name)

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
