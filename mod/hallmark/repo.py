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
from shutil import rmtree
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Tuple, Union
from git.exc import GitCommandError

import pandas as pd

from .dothm import Dothm
from .state import State
from .worktree import Worktree
from .objects import Objects
from .discovery import is_remote_url
from .paraframe import ParaFrame
from .repo_manifest import (
    catalog_map, is_local_table, iter_catalog_rows, manifest_frame_from_pf,
    row_fingerprint, table_groups)
from .repo_state import (
    load_branch_data, load_head_state, find_remote_branch,
    fetch_missing_objects_from_remote)
from .error import CheckoutError, DestinationExistsError, DothmError
from .helper_functions import (
    FILE_IO_CHUNK_SIZE,
    as_list_of_dicts,
    chdir,
    iter_repository_files,
    normalize_nonempty_string,
    resolve_contained_path)
from .repo_worktree import (
    effective_cwd,
    ensure_clean_tracked_files,
    tracked_paths,
    worktree_changes)
from .repo_config import (
    CatalogEntry,
    DEFAULT_DB,
    add_entry,
    catalog_entries,
    check_template_fields,
    entry_encodings,
    find_entry,
    is_placeholder,
    next_db_name,
    normalize_remotes,
    remove_entry,
    row_to_path,
    set_config)

def _encoding_spec(config: dict) -> Optional[dict]:
    """
    Used by add.
    Return the entry holding encoding rules for a template not tracked yet:
    the placeholder, or the only catalog entry.
    """
    entries = as_list_of_dicts(config.get("data")) or []
    placeholder = next((entry for entry in entries if is_placeholder(entry)), None)
    if placeholder is not None:
        return placeholder
    catalog = catalog_entries(config)
    return entries[catalog[0].index] if len(catalog) == 1 else None


def _row_within(record: dict, fmt: str, within_root) -> bool:
    """
    Used by add.
    Return True if a row's file lies within the rescanned subtree, or if the
    row no longer renders with its template and should be replaced.
    """
    try:
        return within_root(row_to_path(record, fmt).as_posix())
    except (KeyError, ValueError):
        return True


def _entry_names(entry: CatalogEntry) -> set[str]:
    """
    Used by rm_cached.
    Return the spellings that identify an entry: its template, and for a remote
    entry its full URL template.
    """
    names = {entry.fmt} if entry.fmt else {entry.db}
    if entry.url and entry.fmt:
        names.add(entry.url.rstrip("/") + "/" + entry.fmt)
    return names


def _catalog_changes(head_state: State, state: State) -> list[dict]:
    """
    Used by status.
    Summarize staged changes to catalog-only files for each catalog table.

    Args:
        head_state (State): The committed state.
        state (State): The staged state.

    Returns:
        list[dict]: For each changed table, its ``templates``, ``url`` and
        counts of ``added``, ``modified`` and ``deleted`` rows.
    """
    def rows_by_table(source):
        tables: dict[str, dict[str, str]] = {}
        for row in iter_catalog_rows(source, local=False):
            tables.setdefault(row.db, {})[row.path] = row_fingerprint(row.record)
        return tables

    head_tables, staged_tables = rows_by_table(head_state), rows_by_table(state)
    groups = {**table_groups(head_state.config), **table_groups(state.config)}
    changes = []
    for name, entries in groups.items():
        head, staged = head_tables.get(name, {}), staged_tables.get(name, {})
        counts = {
            "added": sum(path not in head for path in staged),
            "modified": sum(path in head and head[path] != fingerprint
                            for path, fingerprint in staged.items()),
            "deleted": sum(path not in staged for path in head)}
        if any(counts.values()):
            changes.append({
                "templates": [entry.fmt or name for entry in entries],
                "url": entries[0].url, **counts})
    return changes


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
    def lwpaths(path: Union[Path, str]) -> Tuple[Path, Optional[Path]]:
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
        if path.suffix == ".hm":
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
        dothm_path, worktree_path = self.lwpaths(path)
        self.dothm = Dothm(dothm_path)
        self.worktree = worktree_path and Worktree(worktree_path)
        self.state = self.dothm.load()
        normalize_remotes(self.state.config.get("remote"))
        self.paraframe_cls = ParaFrame
        self.download_result = None

        common = Path(self.dothm.common_dir).resolve().parent
        self.objects = Objects(common)
        dothm_objects = Path(dothm_path) / "objects"
        main_objects = common / "objects"
        if dothm_objects.resolve() != main_objects.resolve() \
        and not dothm_objects.exists():
            dothm_objects.symlink_to(main_objects)

    def _worktree_path(self, value, *, label: str = "tracked path") -> Path:
        """
        Used commit, checkout, and _populate_checksums.
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
        return resolve_contained_path(self.worktree, value, label=label)

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
        branch_name = normalize_nonempty_string(value, label="branch name")
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

    def _populate_checksums(self, pf: ParaFrame) -> None:
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
            self._worktree_path(path, label="matched data path")
            for path in pf["path"].astype(str)]
        # Compute SHA-1 checksums for all files in parallel using the checksum_many
        checksums = self.checksum_many(full_paths)
        # Populate the "sha1" column in the ParaFrame with the computed checksums
        pf["sha1"] = [checksums[path] for path in full_paths]

    @classmethod
    def init(cls, path: Union[Path, str]) -> "Repo":
        """Initialize an empty local repository.

        Catalog local or remote files afterwards with ``add``; a URL template
        such as ``https://host/ER2/{src}_{day}.h5`` catalogs remote files
        without downloading them.

        Args:
            path (Path | str): Worktree or bare ``.hm`` repository destination.

        Returns:
            Repo: The initialized repository.
        """
        dothm_path, worktree_path = cls.lwpaths(path)
        dothm = Dothm.init(dothm_path)
        (dothm.path / "config.yml").write_text(Dothm.config_template(),
                                               encoding="utf-8")
        dothm.dump_yml({}, "meta")
        dothm.dump_tsv(State().data, "data")
        dothm.index.add(["config.yml", "meta.yml", "data.tsv"])
        if worktree_path is not None:
            Worktree.init(worktree_path)
        return cls(path)

    @classmethod
    def clone(cls, url: str, path: Union[Path, str], *,
              source_type: str = "auto", progress: bool = False) -> "Repo":
        """Clone a complete Hallmark Git catalog or published snapshot.

        Git sources retain their history. Published snapshots start new history.
        Dataset files are never downloaded; plan and execute transfers separately.

        Args:
            url (str): Existing Git catalog or published snapshot location.
            path (Path | str): New worktree or bare ``.hm`` repository path.
            source_type (str): ``auto``, ``git``, or ``catalog``.
            progress (bool): Reserved for metadata progress reporting.

        Returns:
            Repo: Complete cloned catalog without downloaded data files.

        Raises:
            DestinationExistsError: If the destination already exists.
            CloneError: If a requested catalog is missing or invalid.
            ValueError: If source options are invalid.
            DownloadError: If metadata access fails.
        """
        from .catalog import clone_catalog

        return clone_catalog(cls, url, path, source_type=source_type)

    def plan_download(self, output_path=None, *, file_paths=None, tsv_names=None,
                      all_files=False, filter=None, remote_name=None,
                      estimated_bytes_per_second=None):
        """
        Plan a download using the local catalog without contacting a server.

        With no explicit paths or TSVs, select the complete catalog before
        applying inclusion globs. Planning does not approve
        the transfer.

        Args:
            output_path (Path | str, optional): Destination directory. Defaults
                to the worktree; required for a bare repository.
            file_paths (sequence[str], optional): Remote-relative file paths.
            tsv_names (sequence[str], optional): Catalog TSVs to select.
            all_files (bool): Select all configured files. Cannot be combined
                with explicit paths or TSVs. Defaults to False.
            filter (str | list[str], optional): Relative path glob or globs.
            remote_name (str, optional): Configured data remote to use.
            estimated_bytes_per_second (float, optional): Positive transfer
                rate for duration estimates. No rate is measured while planning.

        Returns:
            DownloadPlan: Immutable selection with its source, destination,
            checksums, and available file metadata.

        Raises:
            DownloadError: If the catalog, remote, selection, or destination
                is invalid.
            ValueError: If an inclusion pattern or supplied rate is invalid.
        """
        from .downloader import plan_download

        return plan_download(
            self, output_path, file_paths=file_paths, tsv_names=tsv_names,
            all_files=all_files, filter=filter, remote_name=remote_name,
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
        from .downloader import execute_download_plan

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
        remote_backend: Optional[str] = None,
        remote_backend_options: Optional[dict] = None,
    ) -> dict:
        """
        Update repository configuration values.

        Args:
            fmt (str, optional): Data format specification.
            remote_name (str, optional): Name of the remote repository.
            remote_url (str, optional): URL of the remote repository.
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
            **({"remote_backend": remote_backend}
               if remote_backend is not None else {}),
            **({"remote_backend_options": remote_backend_options}
               if remote_backend_options is not None else {}))
        self.dothm.dump(self.state)
        return self.state.config

    def status(self) -> dict[str, object]:
        """
        Return repository status information. Includes staged changes,
        worktree modifications, deletions, and untracked files.

        Local files are listed individually. Changes to catalog-only files,
        such as those of a remote template, are summarized per template.
        Their absent or downloaded copies are never reported as deleted,
        modified, or untracked.

        Args:
            self: Repository instance.

        Returns:
            dict[str, object]: Status summary including:
            - branch (str)
            - staged changes (dict), including per-template ``catalog``
              summaries
            - worktree changes (dict)
            - untracked files (list[str])
        """
        head_state = load_head_state(self)
        head_rows = catalog_map(head_state)
        staged_rows = catalog_map(self.state)
        head_map = {path: row_fingerprint(row.record)
                    for path, row in head_rows.items() if row.local}
        staged_map = {path: row_fingerprint(row.record)
                      for path, row in staged_rows.items() if row.local}
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

        worktree_modified: list[str] = []
        worktree_deleted: list[str] = []

        # If the repository has a worktree, check for modified and missing tracked files
        if self.worktree is not None:
            local_checksums = {path: str(row.record["sha1"])
                               for path, row in staged_rows.items() if row.local}
            worktree_modified, worktree_deleted = worktree_changes(
                self, local_checksums)
            worktree_root = Path(self.worktree)
            # generator that yields relative paths of all files in the worktree
            worktree_files = (full_path.relative_to(worktree_root).as_posix()
                              for full_path in iter_repository_files(worktree_root))
            # cataloged files, including downloaded copies, are not untracked
            untracked = sorted(path for path in worktree_files
                               if path not in staged_rows)
        else:
            untracked = []

        return {
            "branch": self.dothm.active_branch.name,
            "staged": {
                "state": state_changes,
                "added": staged_added,
                "modified": staged_modified,
                "deleted": staged_deleted,
                "catalog": _catalog_changes(head_state, self.state),
            },
            "worktree": {
                "modified": sorted(worktree_modified),
                "deleted": sorted(worktree_deleted),
            },
            "untracked": untracked,
        }

    def add(self, fmt: str, encoding: bool = False, *,
            dry_run: bool = False, progress=False, backend: Optional[str] = None,
            backend_options: Optional[dict] = None) -> ParaFrame:
        '''
        Stage the files matching a template, or rescan the tracked templates.

        A new template becomes another data entry with its own catalog table;
        existing templates are kept. Adding a tracked template again merges
        its current files. ``"."`` rescans every local template within the
        current directory, dropping rows of files that no longer exist there.

        A URL template such as ``https://host/ER2/{src}_{day}.h5`` catalogs
        the matching remote files without downloading them. The URL up to the
        first segment with a field is recorded on the data entry. Adding the
        URL template again syncs it with the remote directory.

        Args:
            fmt (string): Format string, URL template, or "." for full
                directory scan.
            encoding (boolean): Whether to apply encoding rules.
            dry_run (boolean): List matching files without hashing or staging.
            progress (bool | callable): Display remote discovery progress or
                receive updates.
            backend (str, optional): Registered backend for a URL template.
            backend_options (dict, optional): Backend configuration for a URL
                template.
        Returns:
            paraframe Parsed and filtered file index (without checksums).
        Raises:
            ValueError: If the template is invalid, matches no files when new,
                or matches files tracked by another template.
            RuntimeError: If a local template is added to a repository without
                a worktree.
            DownloadError: If remote discovery fails.
        '''
        if is_remote_url(fmt):
            if encoding:
                raise ValueError("--regex encoding rules apply to local files only")
            from .repo_remote import add_remote_template

            return add_remote_template(
                self, fmt, dry_run=dry_run, progress=progress, backend=backend,
                backend_options=backend_options)
        if backend is not None or backend_options is not None:
            raise ValueError("backend options apply to URL templates only")
        if self.worktree is None:
            raise RuntimeError(
                "cannot add files in a bare repository without a worktree")

        # Normalize the format string to ensure it is a non-empty string
        fmt = normalize_nonempty_string(fmt, label="format")
        # "." means rescan the worktree using the already-configured templates
        if fmt == ".":
            return self._rescan_local(encoding=encoding, dry_run=dry_run)

        check_template_fields(fmt)
        config = self.state.config
        entry = find_entry(config, fmt)
        if entry is not None and entry.is_remote:
            raise ValueError(
                f"template {fmt!r} is tracked from a remote URL; run "
                f"hallmark rm --cached {fmt!r} before adding local files")
        encodings = None
        if encoding:
            spec = (config["data"][entry.index] if entry is not None
                    else _encoding_spec(config))
            encodings = entry_encodings(spec, fmt)
        # with the working directory set to the worktree, parse files into a ParaFrame
        with chdir(self.worktree):
            pf = ParaFrame.parse(
                fmt,
                base_path=self.worktree,
                encodings=encodings,
                encoding=encoding)
        if dry_run:
            return pf
        if entry is None:
            # like git add, a new pathspec must match something
            if pf.empty:
                raise ValueError(f"template {fmt!r} did not match any files")
            self._check_ownership(fmt, pf["path"])
        # Compute checksums for all files in the ParaFrame in parallel
        self._populate_checksums(pf)
        manifest = manifest_frame_from_pf(pf, fmt)
        if entry is None:
            db = next_db_name(config, used=self._used_table_names())
            add_entry(config, {"fmt": fmt, **({"db": db} if db != DEFAULT_DB else {})})
            self.state.replace(manifest, db=db)
        # adding a tracked template again merges its current files
        else:
            self.state.update(manifest, db=entry.db)
        self.dothm.dump(self.state)
        # return a ParaFrame without the "sha1" column for display purposes
        return pf.drop(columns=["sha1"], errors="ignore")

    def _rescan_local(self, *, encoding: bool, dry_run: bool) -> ParaFrame:
        """
        Used by add.
        Rescan every local template within the current directory's subtree.

        Rows of files outside the subtree are kept. Files cataloged by a remote
        template, such as downloaded copies, are skipped.

        Args:
            encoding (bool): Apply the encoding rules of templates defining them.
            dry_run (bool): List matching files without hashing or staging.

        Returns:
            ParaFrame: The matching files of every local template.

        Raises:
            RuntimeError: If no local template is tracked.
            ValueError: If a file matches more than one local template.
        """
        config = self.state.config
        local = [entries[0] for name, entries in table_groups(config).items()
                 if is_local_table(entries, self.state.table(name))]
        if not local:
            raise RuntimeError(
                "no local templates to rescan; run hallmark add TEMPLATE first")
        with_encodings = [entry for entry in local
                          if entry_encodings(config["data"][entry.index], entry.fmt)]
        if encoding and not with_encodings:
            raise ValueError("no encoding rules are configured; use hallmark "
                             "set-config --encoding FIELD=REGEX")
        worktree = Path(self.worktree).resolve()
        relative_root = effective_cwd(self).relative_to(worktree)
        catalog_only = {row.path for row in iter_catalog_rows(self.state, local=False)}

        def within_root(path) -> bool:
            return relative_root == Path(".") or relative_root in Path(path).parents

        scans, claimed = [], {}
        for entry in local:
            use_encoding = encoding and entry in with_encodings
            with chdir(self.worktree):
                pf = ParaFrame.parse(
                    entry.fmt, base_path=self.worktree,
                    encodings=(entry_encodings(config["data"][entry.index], entry.fmt)
                               if use_encoding else None),
                    encoding=use_encoding)
            if not pf.empty:
                pf = pf[pf["path"].map(within_root) & ~pf["path"].isin(catalog_only)]
            for path in pf["path"] if not pf.empty else ():
                if path in claimed:
                    raise ValueError(
                        f'"{path}" matches templates {claimed[path]!r} and '
                        f"{entry.fmt!r}; use more specific templates")
                claimed[path] = entry.fmt
            scans.append((entry, pf))
        frames = [pf for _, pf in scans if not pf.empty]
        combined = (ParaFrame(pd.concat(frames, ignore_index=True)) if frames
                    else ParaFrame(columns=["path"]))
        if dry_run:
            return combined
        for entry, pf in scans:
            self._populate_checksums(pf)
            manifest = manifest_frame_from_pf(pf, entry.fmt)
            if relative_root != Path("."):
                # keep rows of files outside the rescanned subtree
                kept = [record for record in
                        self.state.table(entry.db).to_dict(orient="records")
                        if not _row_within(record, entry.fmt, within_root)]
                manifest = pd.concat([pd.DataFrame(kept, columns=manifest.columns),
                                      manifest], ignore_index=True)
            self.state.replace(manifest, db=entry.db)
        self.dothm.dump(self.state)
        return combined.drop(columns=["sha1"], errors="ignore")

    def _check_ownership(self, fmt: str, paths) -> None:
        """
        Used by add.
        Reject a new template that matches files tracked by another template.

        Args:
            fmt (str): The new template.
            paths: Relative POSIX paths the template matches.

        Raises:
            ValueError: If any path is already cataloged.
        """
        owned = catalog_map(self.state)
        overlap = sorted(path for path in set(paths) if path in owned)
        if overlap:
            shown = ", ".join(overlap[:5])
            more = f" and {len(overlap) - 5} more" if len(overlap) > 5 else ""
            raise ValueError(
                f"template {fmt!r} matches files tracked by another template "
                f"({shown}{more}); use a more specific template or run "
                "hallmark rm --cached TEMPLATE first")

    def _used_table_names(self) -> set[str]:
        """
        Used by add.
        Return catalog TSV names present now or at any point in history, so a
        new template never reuses the history of a removed one.
        """
        names = {path.name for path in self.dothm.path.glob("*.tsv")}
        try:
            history = self.dothm.git.log(
                "--all", "--format=", "--name-only", "--", "*.tsv")
        # a repository without commits has no history to consult
        except GitCommandError:
            history = ""
        names.update(line.strip() for line in history.splitlines() if line.strip())
        return names

    def rm_cached(self, template: str) -> CatalogEntry:
        '''
        Stop tracking a template without deleting any files, like
        ``git rm --cached``.

        Args:
            template (str): The tracked template, or a remote template's full
                URL template.

        Returns:
            CatalogEntry: The removed entry.

        Raises:
            ValueError: If the template is not tracked or shares its catalog
                table with another template.
        '''
        template = normalize_nonempty_string(template, label="template")
        entries = catalog_entries(self.state.config)
        entry = next((candidate for candidate in entries
                      if template in _entry_names(candidate)), None)
        if entry is None:
            tracked = ", ".join(repr(candidate.fmt) for candidate in entries
                                if candidate.fmt) or "none"
            raise ValueError(
                f"template {template!r} is not tracked; tracked templates: {tracked}")
        if any(other.db == entry.db for other in entries if other is not entry):
            raise ValueError(
                f"template {entry.fmt!r} shares its catalog table {entry.db} with "
                "another template; edit config.yml to remove it")
        remove_entry(self.state.config, entry.index)
        self.state.drop_table(entry.db)
        self.dothm.dump(self.state)
        return entry

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
        msg = normalize_nonempty_string(msg, label="commit message")
        # if allow_empty is False and there are no staged changes, return False
        if (not allow_empty and not self.dothm.index.diff("HEAD")):
            # return early since there are no changes to commit
            return False
        head_state = load_head_state(self)
        # head entries are the set of (path, sha1) tuples of HEAD's local files
        head_entries = {(row.path, str(row.record["sha1"]).lower())
                        for row in iter_catalog_rows(head_state, local=True)}

        # list of tuples containing (full path, expected sha1) for stored files
        files_to_store: list[tuple[Path, str]] = []
        # for each local file in the current catalog; catalog-only files, such
        # as remote files, are committed as metadata without objects
        for row in iter_catalog_rows(self.state, local=True):
            relative_path = row.path
            # get the expected SHA1 checksum for the file
            expected_sha1 = str(row.record["sha1"]).lower()
            # current_entry is a tuple of (relative path, expected sha1) for this file
            current_entry = (relative_path, expected_sha1)
            # if the manifest entry is not in the HEAD entries
            # or the object store does not contain the expected SHA1
            if (current_entry not in head_entries
                 or not self.objects.contains(expected_sha1)):
                # resolve the full path of the file in the worktree for storage
                full_path = self._worktree_path(relative_path, label="tracked path")
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
        # Validate and normalize the target branch name
        target_branch = self._validate_branch_name(target_branch)

        if self.worktree is None:
            raise CheckoutError("cannot checkout without a worktree")
        ensure_clean_tracked_files(self)

        local_branches = {head.name for head in self.dothm.heads}
        has_local = target_branch in local_branches

        # Look for a matching branch on any configured remote.
        # Returns something like "origin/feature" or None if no remote branch exists.
        remote_revision = find_remote_branch(self, target_branch)
        has_remote = remote_revision is not None

        create_new_branch = not has_local and not has_remote
        current_tracked = tracked_paths(self)
        target_state = load_branch_data(self, target_branch)

        # checkout restores and removes local files only; catalog-only files,
        # such as downloaded copies of remote files, are left in place
        try:
            target_rows = list(iter_catalog_rows(target_state))
            current_rows = list(iter_catalog_rows(self.state))
        except ValueError as exc:
            raise CheckoutError(str(exc)) from exc
        # list of (relative path, sha1) tuples for the target branch's local files
        target_entries_raw = [(Path(row.path), str(row.record["sha1"]))
                              for row in target_rows if row.local]
        # get the set of relative paths for all tracked files in the target branch
        target_tracked = {path for path, _ in target_entries_raw}
        # the target's catalog-only files and the current branch's downloaded copies
        target_catalog_sha = {Path(row.path): str(row.record.get("sha1", "")).lower()
                              for row in target_rows if not row.local}
        current_catalog_only = {Path(row.path) for row in current_rows
                                if not row.local}

        # try to identify any missing objects in the target branch that are not present
        # in the object store
        try:
            missing_objects = self.objects.missing(
                sha1 for _, sha1 in target_entries_raw)
        # if a ValueError occurs during object existence check, raise a CheckoutError
        except ValueError as exc:
            raise CheckoutError(str(exc)) from exc

        # if switching to a remote-tracking branch, try to fetch any missing
        # objects from that remote before giving up
        if missing_objects and has_remote:
            remote_name = remote_revision.split("/", 1)[0]
            missing_objects = sorted(fetch_missing_objects_from_remote(
                self, remote_name, missing_objects))

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
                target_path = self._worktree_path(rel_path, label="checkout target")
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
            conflict_checksums = self.checksum_many(conflict_paths)

            # for each conflict candidate
            for (rel_path, full_path, expected_sha1) in conflict_candidates:
                # if the computed checksum does not match the expected checksum
                if (conflict_checksums[full_path] != expected_sha1):
                    if rel_path in current_catalog_only:
                        raise CheckoutError(
                            f'target tracked path "{rel_path}" conflicts with '
                            "a downloaded copy cataloged by the current branch")
                    raise CheckoutError(
                        f'target tracked path "{rel_path}" '
                        "already exists as an untracked file")

        # Store the name of the currently active branch before switching
        original_branch = self.dothm.active_branch.name
        # Create a mapping of current local tracked paths to their SHA1 checksums
        current_sha_by_path = {
            Path(row.path): str(row.record["sha1"]).lower()
            for row in current_rows if row.local}
        # a local file that the target catalogs remotely with the same content
        # stays in place instead of being removed and downloaded again
        kept_paths = {
            path for path in current_tracked - target_tracked
            if target_catalog_sha.get(path)
            and target_catalog_sha[path] == current_sha_by_path.get(path)}
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
            while parent != self.worktree and parent.exists():
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
            prefix=".hallmark-checkout-", dir=self.worktree) as temporary_directory:
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
                    self.objects.restore(sha1, staged_path)
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
                    self.dothm.git.checkout(target_branch)

                elif has_remote:
                    self.dothm.git.checkout("--track",remote_revision)

                else:
                    self.dothm.git.checkout("-b", target_branch)

                # Reload the repository state after switching branches
                self.state = self.dothm.load()

                # paths that are either currently tracked but not in the target branch
                # or paths that are tracked but have changed from the current branch
                affected_paths = sorted((current_tracked - target_tracked
                                         - kept_paths) | changed_target_paths,
                    key=lambda path: (len(path.parts), path.as_posix()), reverse=True)
                for relative_path in affected_paths:
                    # absolute path in the worktree for the affected relative path
                    path = self._worktree_path(relative_path, label="checkout target")
                    # create a backup before replacing it with the target file
                    if path.exists():
                        backup_path = backup_root / str(len(backups))
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        path.replace(backup_path)
                        backups.append((path, backup_path))
                        remove_empty_parents(path)

                for relative_path, staged_path in staged_files:
                    # Determine the destination path in the worktree for the staged file
                    destination = self._worktree_path(
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
                    if self.dothm.active_branch.name != original_branch:
                        self.dothm.git.checkout(original_branch)
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
                    head.name for head in self.dothm.heads}:
                    # try to delete the newly created branch to rollback the checkout
                    try:
                        self.dothm.git.branch("-D", target_branch)
                    # if deleting the new branch fails, record error for reporting
                    except GitCommandError as rollback_exc:
                        rollback_errors.append(
                            f"could not remove new branch: {rollback_exc}")

                # try to reload the repository state after rollback
                try:
                    self.state = self.dothm.load()
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
        # Validate and normalize the target branch name
        target_branch = self._validate_branch_name(target_branch)

        if self.worktree is None:
            raise RuntimeError("cannot add a worktree in a bare " \
            "repository without a worktree")

        # source is the current worktree path, target is the new worktree path
        source = Path(self.worktree).resolve()
        target = resolve_contained_path(source.parent, target_branch,
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
        existing_branches = {head.name for head in self.dothm.heads}
        # create a boolean flag indicating whether the target branch is new or existing
        created_branch = target_branch not in existing_branches
        if not created_branch:
            # load the state of the target branch if it already exists
            target_state = load_branch_data(self, target_branch)
        # if the target branch does not exist yet, load the current HEAD state
        else:
            target_state = load_head_state(self)

        # the new worktree receives the target's local files; catalog-only
        # files, such as remote files, are downloaded separately
        target_local = [(row.path, str(row.record["sha1"]))
                        for row in iter_catalog_rows(target_state, local=True)]

        # if the target worktree does not exist
        if not target_dothm.exists():
            # check for missing objects in the target state that are not present in
            # the object store
            try:
                missing_objects = self.objects.missing(
                    sha1 for _, sha1 in target_local)
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
                    self.dothm.link(target_dothm, target_branch)
                # if the target branch does not exist, create a new worktree and branch
                else:
                    self.dothm.git.worktree("add", "-b", target_branch,
                                            str(target_dothm))
            # if the worktree creation fails, raise a RuntimeError with details
            except (GitCommandError, DothmError) as exc:
                # rmtree the target directory to clean up any partial worktree creation
                rmtree(target, ignore_errors=True)
                raise RuntimeError(f'failed to create worktree for branch '
                                   f'"{target_branch}": {exc}') from exc

            # iterate over the target state data and restore files from the object store
            try:
                for rel_path, sha1 in target_local:
                    # resolve relative path to an absolute path in the target worktree
                    destination = resolve_contained_path(
                        target,
                        rel_path,
                        label="worktree data path")
                    # ensure the parent directory exists before restoring the file
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    # restore the file from the object store using its SHA-1 checksum
                    self.objects.restore(sha1, destination)

            # if any exception occurs during file restoration, attempt a clean up
            except Exception as exc:
                # Attempt to remove the worktree from Git and clean up the directory
                try:
                    self.dothm.git.worktree("remove", "--force", str(target_dothm))
                # if the worktree removal fails, attempt to prune the worktree list
                except GitCommandError:
                    # remove the target directory before pruning
                    rmtree(target, ignore_errors=True)
                    try:
                        self.dothm.git.worktree("prune")
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
                        self.dothm.git.branch("-D", target_branch)
                    # if the branch deletion fails, pass since already handling an error
                    except GitCommandError:
                        pass
                # raise RuntimeError with details about failure to populate the worktree
                raise RuntimeError(
                    f'failed to populate worktree for branch '
                    f'"{target_branch}": {exc}') from exc

        return True
