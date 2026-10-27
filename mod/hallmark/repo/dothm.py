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

"""
Keep a repository's state in ``.hm``, a git worktree of its own.

``Dothm`` writes and stages ``config.yml``, ``meta.yml``, ``data.tsv`` and
the other TSVs that ``config.yml`` names, and loads them back from the
index or any commit.
"""

from __future__ import annotations

from configparser import NoOptionError, NoSectionError
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import Optional, Union
from io import BytesIO, StringIO
import glob
import json
import os
import tempfile
import threading

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from git import Repo
from git.exc import GitCommandError
from shutil import rmtree
from tempfile import TemporaryFile

import pandas as pd
import yaml

from ..error import CloneError, DothmError
from ..utils import (replace_file_on_success,
                     load_yaml_file,
                     validate_path_name,
                     validate_relative_path,
                     load_yaml)
from .catalog import file_entries, table_names as _table_names
from .state import State, normalize_rows
from .config import row_to_path, single_data_format


class _HallmarkYamlDumper(yaml.Dumper):
    """
    Used by write_yaml.
    Custom YAML dumper for Hallmark that preserves the order of keys in dictionaries
    and uses literal block style for multi-line strings.
    Args:
        yaml.Dumper: The base YAML dumper class to extend. YAML dumper is
            responsible for converting Python objects into YAML format.
    """


def _format_yaml_string(dumper, data):
    """
    Used by _HallmarkYamlDumper.
    Use literal block style ('|') for multi-line strings so they render
    as clean, readable text instead of PyYAML's default folded/escaped style.

    Arguments:
        dumper: The YAML dumper instance.
        data: The string data to be represented in YAML.

    Returns:
        YAML representation of the string, using literal block style if it has newlines
    """
    # Use literal block style ('|') for multi-line strings if they contain newlines,
    # otherwise use the default style.
    style = "|" if "\n" in data else None
    # Use the dumper to represent the string with the chosen style.
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


# use hallmark's dumper to avoid leaking format choices into unrelated code
_HallmarkYamlDumper.add_representer(str, _format_yaml_string)

def write_yaml(data, handle) -> None:
    """
    Dump a dictionary to a YAML file, preserving key order and using
    literal block style for multi-line strings.

    Args:
        data (dict): The dictionary to be dumped to YAML.
        handle: The file handle to write the YAML content to.
    """
    yaml.dump(
        data,
        handle,
        Dumper=_HallmarkYamlDumper,
        sort_keys=False,
        width=float("inf"))


def _yaml_text(data) -> str:
    """Return ``data`` as the YAML text ``write_yaml`` writes."""
    handle = StringIO()
    write_yaml(data, handle)
    return handle.getvalue()


def _read_tsv_blob(blob, empty: pd.DataFrame) -> pd.DataFrame:
    """Read a staged or committed TSV; an empty file gives ``empty``."""
    content = blob.data_stream.read()
    if not content.strip():
        return empty
    return pd.read_csv(BytesIO(content), sep="\t", dtype=str, keep_default_na=False)


# One mutex and depth per lock file, shared by every Dothm open on it in this
# process: a second flock on the same file would wait for the first.
_STATE_LOCKS: dict = {}
_STATE_LOCKS_GUARD = threading.Lock()


def _process_state_lock(lock_path: Path):
    """Return the in-process mutex and per-thread depth for ``lock_path``."""
    key = os.path.realpath(lock_path)
    with _STATE_LOCKS_GUARD:
        if key not in _STATE_LOCKS:
            _STATE_LOCKS[key] = (threading.RLock(), threading.local())
        return _STATE_LOCKS[key]


def data_tsv_text(data: pd.DataFrame) -> str:
    """Return catalogue rows as ``data.tsv`` text: normalized, sorted, LF endings."""
    return normalize_rows(data).to_csv(sep="\t", index=False, lineterminator="\n")


class Dothm(Repo):
    """Local ``.hm`` storage backend.

    The backend version controls the hallmark ``State`` database files on
    disk: ``config.yml``, ``meta.yml``, ``data.tsv`` and the other TSVs that
    ``config.yml``'s ``data`` entries name. It is itself a git worktree, and
    takes the arguments of ``git.Repo``.

    Raises:
        DothmError: If the repository is bare, with no worktree.
    """
    def _storage_path(self, stem: Union[Path, str], suffix: str) -> Path:
        """
        Used by read_yaml, write_yaml, read_tsv, and write_tsv.
        Get the full path to a storage file in the ``.hm`` directory.
        Args:
            stem (Union[Path, str]): The stem of the file name (without extension).
            suffix (str): The file extension (e.g., ".yml", ".tsv").
        Returns:
            Path: The full path to the storage file with the correct suffix.
        """
        # validate the name of the storage file to ensure it is a valid path component
        name = validate_path_name(stem, label="storage name")
        path = self.path / name
        # ensure the path has the correct suffix and is in the correct directory
        if path.suffix.lower() != suffix.lower():
            path = path.with_suffix(suffix)
        return path

    @cached_property
    def path(self) -> Path:
        """Directory of this ``.hm`` worktree."""
        return Path(self.working_tree_dir)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.working_tree_dir is None:
            raise DothmError('The ".hm" directory must be a valid git ' \
            'worktree.')

    @classmethod
    def init(cls, *args, **kwargs) -> "Dothm":
        """
        Create a ``.hm`` git repository, with ``main`` as its first branch.

        A repository without commits gets a ``README.md`` and an initial
        commit, so it always has a ``HEAD``.

        Args:
            *args: Positional arguments of ``git.Repo.init``, such as the path.
            **kwargs: Keyword arguments of ``git.Repo.init``;
                ``initial_branch`` defaults to ``"main"``.

        Returns:
            Dothm: The initialized repository.

        Raises:
            DothmError: If ``bare`` is True.
        """
        if kwargs.get('bare', False):
            raise DothmError('A ".hm" directory must not be a bare git ' \
            'repository')
        kwargs.setdefault("initial_branch", "main")
        dothm = super().init(*args, **kwargs)
        dothm.exclude_objects()

        # if no commits exist, create a README.md file to initialize the repository
        if not dothm.heads:
            readme_path = dothm.path / "README.md"
            readme_path.write_text(
                """# Local `.hm` Repository

    This is a dot-hallmark repository.
    It is a git-version-controlled dataset index used by `hallmark`.
    See https://l6a.github.io/hallmark/ for `hallmark` usage.
    """,
                encoding="utf-8")
            # add the README.md file to the git index and commit it
            dothm.index.add([readme_path])
            dothm.index.commit("Initial commit: local `.hm` repository")

        return dothm

    @staticmethod
    def config_template() -> str:
        """Return the commented ``config.yml`` that a new repository starts with."""
        return """# Edit this file only if your branch needs regex substitutions.
# For simple names, you can just run: hm add "a{a}_i{i}.h5"
data:
  -
    # fmt: "{release}_{source}_{year}_{doy:03d}_{band}.uvfits"
    encoding:
      # aspin: m([0-9]+(\\.[0-9]+)?|\\.[0-9]+)
remote:
  # name: origin
  # url: https://example.com/path/to/data/
  # SSH URL example: ssh://campus/srv/export/ (requires a trusted host key)
  # auth: campus  # Optional local profile name. Credentials stay local.
"""

    @classmethod
    def clone(
        cls,
        url: str,
        to_path: Union[Path, str],
        display_path: Optional[Union[Path, str]] = None,
    ) -> "Dothm":
        """
        Clone a ``.hm`` git repository.

        A clone of a repository that holds only its initial ``README.md``
        commit gets a ``config.yml`` from ``config_template``, an empty
        ``meta.yml`` and an empty ``data.tsv``, staged but not committed.

        Args:
            url (str): Git URL or path of the ``.hm`` repository to clone.
            to_path (Path | str): Destination ``.hm`` directory.
            display_path (Path | str, optional): Path shown in error messages
                in place of ``to_path``.

        Returns:
            Dothm: The cloned repository.

        Raises:
            CloneError: If git cannot clone ``url``, or the clone lacks
                ``config.yml``, ``meta.yml`` or ``data.tsv``.
        """
        to_path = Path(to_path)

        try:
            super().clone_from(url, str(to_path))
            dothm = cls(str(to_path))
            dothm.exclude_objects()

            commit = dothm.head.commit
            initial_message = "Initial commit: local `.hm` repository"
            if (not commit.parents
                    and commit.message.strip() == initial_message
                    and {item.path for item in commit.tree} == {"README.md"}):
                (dothm.path / "config.yml").write_text(
                    cls.config_template(), encoding="utf-8")
                dothm.write_yaml({}, "meta")
                dothm.write_tsv(State().data, "data")
                dothm.index.add(["config.yml", "meta.yml", "data.tsv"])

            required_files = ["config.yml", "meta.yml", "data.tsv"]
            for file in required_files:
                if not (dothm.path / file).exists():
                    raise CloneError(
                        f'Cloned repository missing required file: {file}'
                    )
            return dothm
        except GitCommandError as exc:
            # If cloning fails, raise a CloneError with a helpful message.
            raise CloneError.from_git_command(
                exc,
                fallback=f'Failed to clone from "{url}"',
                clone_path=to_path,
                display_path=display_path) from exc

    def exclude_objects(self) -> None:
        """
        Exclude the object store from Git, which must never commit or clean it.

        Linked worktrees share the exclude file, and there ``objects`` is a
        symlink, so the pattern has no trailing slash. A tracked
        ``.gitignore`` would change the initial commit ``clone`` recognises.
        """
        exclude = Path(self.common_dir) / "info" / "exclude"
        try:
            text = exclude.read_bytes()
        except FileNotFoundError:
            text = b""
        # Git trims trailing spaces from a pattern, but not tabs.
        if any(line.rstrip(b" ") == b"/objects" for line in text.splitlines()):
            return
        exclude.parent.mkdir(exist_ok=True)
        with exclude.open("ab") as handle:
            handle.write(b"/objects\n" if not text or text.endswith(b"\n")
                         else b"\n/objects\n")

    def link_worktree(self, path: Union[Path, str], branch: Optional[str] = None):
        """
        Add a linked git worktree of this ``.hm`` repository.

        Args:
            path (Path | str): Directory of the new worktree; it is made
                absolute.
            branch (str, optional): Branch to check out there. Without one,
                git uses a branch named after the directory, creating it from
                ``HEAD`` if needed.

        Returns:
            Dothm: The new worktree.

        Raises:
            DothmError: If git cannot add the worktree.
        """
        path = Path(path).resolve()  # use absolute path
        # try to add the specified path as a git worktree
        try:
            self.git.worktree("add", path, branch)
        # if adding the worktree fails, raise a DothmError with a helpful message
        except GitCommandError as exc:
            raise DothmError(f'Failed to link "{path}": {exc}')
        return Dothm(path)

    def set_identity(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        """
        Save the commit author name and email for this repository only.

        Args:
            name (str, optional): Value for ``user.name``; None leaves it
                unchanged.
            email (str, optional): Value for ``user.email``; None leaves it
                unchanged.
        """
        with self.config_writer() as writer:
            if name is not None:
                writer.set_value("user", "name", name)
            if email is not None:
                writer.set_value("user", "email", email)

    def _read_identity(self, reader) -> tuple[Optional[str], Optional[str]]:
        values = []
        for key in ("name", "email"):
            try:
                value = str(reader.get_value("user", key)).strip()
            except (NoSectionError, NoOptionError):
                value = ""
            values.append(value or None)
        return values[0], values[1]

    def identity(self) -> tuple[Optional[str], Optional[str]]:
        """
        Return the commit author name and email set for this repository.

        Only this repository's own git config is read.

        Returns:
            tuple[str | None, str | None]: The name and email, each None when
            it is unset or blank.
        """
        return self._read_identity(self.config_reader("repository"))

    def effective_identity(self) -> tuple[Optional[str], Optional[str]]:
        """
        Return the commit author name and email git will commit with.

        Values set for this repository take precedence over the user's global
        and system git configuration.

        Returns:
            tuple[str | None, str | None]: The name and email, each None when
            it is not configured at any level, or is blank.
        """
        return self._read_identity(self.config_reader())

    def load_state(self, revision=None, *, staged=False) -> State:
        """
        Load the state from the index, or from a commit.

        With no ``revision``, the TSVs come from the index, and ``config.yml``
        and ``meta.yml`` from the files in ``.hm`` unless ``staged`` is True.
        Older commits that keep per-row ``versions/`` records are read from
        those records. A TSV that ``config.yml`` names but that is missing
        loads as an empty table, and a ``config.yml`` with an invalid ``db``
        name loads with no other TSVs.

        Args:
            revision (str, optional): Commit to read, such as ``"HEAD"`` or a
                branch name; None reads the index.
            staged (bool): With no ``revision``, read ``config.yml`` and
                ``meta.yml`` from the index as well.

        Returns:
            State: The loaded state, with the other TSVs in ``tables``.
        """
        if revision is None:
            blobs = {
                blob.path: blob
                for stage, blob in self.index.iter_blobs()
                if stage == 0
            }
        else:
            blobs = {
                blob.path: blob
                for blob in self.commit(revision).tree.traverse()
                if blob.type == "blob"
            }

        state = State()
        for name in ("config", "meta"):
            blob = blobs.get(f"{name}.yml")
            value = load_yaml(blob.data_stream.read()) if blob is not None else {}
            if revision is None and not staged:
                path = self._storage_path(name, ".yml")
                if path.is_file():
                    value = self.read_yaml(name)
            setattr(state, name, value)

        if "versions.yml" in blobs:
            schema = load_yaml(blobs["versions.yml"].data_stream.read())
            records = []
            for path, blob in sorted(blobs.items()):
                if path.startswith("versions/"):
                    record = json.loads(blob.data_stream.read())
                    record["path"] = path.removeprefix("versions/")
                    records.append(record)

            data = pd.DataFrame(records)
            columns = list(dict.fromkeys([
                *schema.get("columns", ["sha1"]), *data.columns
            ]))
            state.data = data.reindex(columns=columns).fillna("").astype(str)

        elif "data.tsv" in blobs:
            state.data = _read_tsv_blob(blobs["data.tsv"], state.data)

        try:
            names = _table_names(state.config)[1:]
        except ValueError:
            # Commands that read a malformed config.yml report it themselves.
            names = []
        for name in names:
            state.tables[name] = (_read_tsv_blob(blobs[name], pd.DataFrame())
                                  if name in blobs else pd.DataFrame())
        return state

    def stage_text(self, path: str, text: str) -> None:
        """
        Stage a text string as a file in the Git index.

        Args:
            path (str): The path of the file to stage.
            text (str): The text content to stage.
        """
        with TemporaryFile() as handle:
            handle.write(text.encode("utf-8"))
            handle.seek(0)
            sha = self.git.hash_object("-w", "--stdin", istream=handle)
        self.git.update_index("--add", "--cacheinfo", "100644", sha, path)


    @contextmanager
    def state_lock(self):
        """
        Hold an advisory lock while the state is re-read, changed and saved.

        Commands that run for minutes, such as downloads and scans, re-read
        the state under this lock before saving, so a change another command
        saved in the meantime is kept rather than overwritten. A thread that
        holds the lock may take it again, through any Dothm of the same
        ``.hm``, as save_state does.
        """
        lock_path = Path(self.git_dir) / "hallmark.lock"
        mutex, held = _process_state_lock(lock_path)
        with mutex:
            depth = getattr(held, "depth", 0)
            if depth:
                # A second flock from this process would wait for itself.
                held.depth = depth + 1
                try:
                    yield
                finally:
                    held.depth = depth
                return
            with open(lock_path, "a+b") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                held.depth = 1
                try:
                    yield
                finally:
                    held.depth = 0
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _legacy_record_paths(self) -> list[str]:
        """Return the index's per-row ``versions/`` records from older layouts."""
        return [path for path, stage in self.index.entries if stage == 0
                and (path == "versions.yml" or path.startswith("versions/"))]

    def _remove_from_index(self, paths) -> None:
        """Remove paths from the index in one Git process, however many there are."""
        with TemporaryFile() as names:
            names.write(b"".join(os.fsencode(str(path)) + b"\0" for path in paths))
            names.seek(0)
            self.git.update_index("--force-remove", "-z", "--stdin", istream=names)

    def _drop_legacy_records(self) -> None:
        """
        Remove the per-row ``versions/`` records older layouts kept, once
        save_state has unstaged them.

        data.tsv is the catalogue's one record. Older commits stay readable,
        because ``load_state`` still reads their records.
        """
        rmtree(self.path / "versions", ignore_errors=True)
        (self.path / "versions.yml").unlink(missing_ok=True)

    def _reset_to_head(self, head, paths) -> None:
        """Reset index entries to HEAD, removing those HEAD does not have."""
        if head is None:
            self._remove_from_index(paths)
        else:
            self.index.reset(head, paths=sorted(paths), working_tree=False)

    def restore_index(self, files=(), rows=()) -> None:
        """
        Unstage paths, leaving every file in ``.hm`` as it is on disk.

        Args:
            files (Sequence[str]): Paths inside ``.hm`` such as ``config.yml``,
                or ``"."`` to unstage everything.
            rows (Sequence[str]): Catalogue paths whose rows return to HEAD;
                a folder selects every row beneath it.

        Raises:
            ValueError: If nothing is given, a path matches nothing staged or
                committed, or a row names a ``file:`` entry of ``config.yml``,
                which is restored through ``config.yml`` instead.
        """
        if not files and not rows:
            raise ValueError("Usage: hm restore --staged <path> [<path> ...]")
        head = self.head.commit if self.head.is_valid() else None
        original = self.index.entries.copy()
        try:
            if "." in files:
                if head is None:
                    # Each access to self.index reads a new index object.
                    index = self.index
                    index.entries.clear()
                    index.write()
                else:
                    self.index.reset(head, working_tree=False)
                return
            head_paths = ({blob.path for blob in head.tree.traverse()
                           if blob.type == "blob"} if head is not None else set())
            index_paths = {path for path, stage in self.index.entries if stage == 0}
            for path in files:
                if path not in head_paths | index_paths:
                    raise ValueError(
                        f"No staged or committed path matches '.hm/{path}'")
            if files:
                self._reset_to_head(head, files)
            if rows:
                self._restore_rows(head, head_paths, rows)
        except Exception:
            index = self.index
            index.entries = original
            index.write()
            raise

    def _restore_rows(self, head, head_paths, rows) -> None:
        """Return the selected catalogue rows of every TSV to HEAD and stage them."""
        staged = self.load_state(staged=True)
        committed = self.load_state("HEAD") if head is not None else State()

        def under(path):
            return [row for row in rows if path == row or path.startswith(row + "/")]

        def frame(state, name):
            if name == "data.tsv":
                return state.data
            return state.tables.get(name, pd.DataFrame())

        names = list(dict.fromkeys([
            "data.tsv", *staged.tables, *committed.tables]))
        plans, matched = [], set()
        for name in names:
            staged_rows, committed_rows = frame(staged, name), frame(committed, name)
            staged_paths = self._row_paths(staged, staged_rows)
            committed_paths = self._row_paths(committed, committed_rows)
            matched |= {row for path in staged_paths + committed_paths
                        for row in under(path)}
            plans.append((name, staged_rows, committed_rows,
                          [not under(path) for path in staged_paths],
                          [bool(under(path)) for path in committed_paths]))
        entries = {entry["file"] for state in (staged, committed)
                   for _, entry in file_entries(state.config)}
        for row in rows:
            if row in matched:
                continue
            if row in entries:
                raise ValueError(
                    f"{row!r} is a file: entry of config.yml; restore it with "
                    "hm restore --staged .hm/config.yml")
            raise ValueError(f"No staged or committed path matches {row!r}")
        for name, staged_rows, committed_rows, keep, take in plans:
            if all(keep) and not any(take):
                continue
            restored = pd.concat([staged_rows[keep], committed_rows[take]],
                                 ignore_index=True, sort=False)
            if (name in head_paths
                    and set(restored.columns) == set(committed_rows.columns)
                    and normalize_rows(restored[committed_rows.columns]).equals(
                        normalize_rows(committed_rows))):
                # Unstaging every change restores HEAD's own bytes.
                legacy = ([path for path in head_paths
                           if path == "versions.yml" or path.startswith("versions/")]
                          + self._legacy_record_paths() if name == "data.tsv" else [])
                self._reset_to_head(head, [name, *legacy])
                continue
            self.stage_text(name, data_tsv_text(restored))
            legacy = self._legacy_record_paths() if name == "data.tsv" else []
            if legacy:
                self._remove_from_index(legacy)

    @staticmethod
    def _row_paths(state, frame) -> list:
        """Return each row's catalogue path, built from its fields if needed."""
        if frame.empty:
            return []
        fmt = single_data_format(state.config)
        return [row_to_path(record, fmt).as_posix()
                for record in frame.to_dict(orient="records")]

    def save_state(self, state: State) -> None:
        """
        Write and stage ``config.yml``, ``meta.yml``, ``data.tsv`` and the
        other TSVs that ``config.yml`` names, all of them or none.

        Rows are saved, and kept in memory, in the form load_state returns, so
        saving a loaded state changes nothing. Each row's path is checked first.
        A table without columns, never written, is not created. If a file or
        the index cannot be updated, every file keeps its previous content.

        Args:
            state (State): State to save; its rows are normalized in place.

        Raises:
            ValueError: If a row's path is not a safe relative path, or
                ``config.yml`` names an invalid TSV. Nothing is written then.
        """
        fmt = single_data_format(state.config)
        state.data = normalize_rows(state.data)
        for record in state.data.to_dict(orient="records"):
            row_to_path(record, fmt)
        names = _table_names(state.config)[1:]
        state.tables = {name: normalize_rows(state.tables.get(name, pd.DataFrame()))
                        for name in names}
        for frame in state.tables.values():
            for path in frame.get("path", []):
                validate_relative_path(path, label="catalogue path")
        written = [name for name, frame in state.tables.items()
                   if len(frame.columns)]
        texts = {"config.yml": _yaml_text(state.config),
                 "meta.yml": _yaml_text(state.meta),
                 "data.tsv": data_tsv_text(state.data),
                 **{name: data_tsv_text(state.tables[name]) for name in written}}
        with self.state_lock():
            legacy = self._legacy_record_paths()

            def stage():
                self._add_to_index(list(texts))
                # load_state reads data.tsv only once these are gone.
                if legacy:
                    self._remove_from_index(legacy)

            self._replace_together(texts, then=stage, also=legacy)
            self._drop_legacy_records()

    def _replace_together(self, texts: dict, then, also=()) -> None:
        """
        Replace files in ``.hm`` so that a failure leaves each one as it was.

        Every text is written to a temporary file before any file is replaced,
        so a full disk fails first. If a replacement or ``then`` fails, or the
        process is interrupted, the files get their previous content back, the
        index its previous entries for them, and the original error is raised.
        If the index cannot be put back, the files keep the content it stages.

        Args:
            texts (dict): Text by file name in ``.hm``, in replacement order.
            then (Callable[[], None]): Run once every file is replaced, to
                stage them.
            also: Other paths whose index entries ``then`` changes; they are
                put back with the rest.
        """
        for name in texts:
            # Left by a save that was killed.
            for stale in self.path.glob(f".{glob.escape(name)}.*.tmp"):
                stale.unlink(missing_ok=True)
        targets = {name: self.path / name for name in texts}
        previous, temps, replaced, restore_index = {}, {}, [], False
        indexed = {*texts, *also}
        try:
            for name, path in targets.items():
                previous[name] = self._keep_previous(path)
            for name, text in texts.items():
                descriptor, temp = tempfile.mkstemp(
                    prefix=f".{name}.", suffix=".tmp", dir=self.path)
                temps[name] = Path(temp)
                newline = "" if name.lower().endswith(".tsv") else None
                with os.fdopen(descriptor, "w", encoding="utf-8",
                               newline=newline) as handle:
                    handle.write(text)
            entries = self._entries_of(self.index, indexed)
            for name, temp in temps.items():
                # Listed first: an interrupt can arrive just after the rename.
                replaced.append(name)
                temp.replace(targets[name])
            restore_index = True
            then()
        except BaseException as exc:
            unrestored = []
            if restore_index:
                try:
                    self._restore_entries(indexed, entries)
                except BaseException:
                    # The index keeps this save's entries, so the files do too.
                    unrestored = ["the index", *map(str, targets.values())]
                    replaced = []
            for name in reversed(replaced):
                # Not stopped by a second interrupt: the files must match again.
                try:
                    self._restore_file(targets[name], previous[name])
                except BaseException:
                    unrestored.append(str(targets[name]))
            if unrestored and hasattr(exc, "add_note"):
                exc.add_note("Could not restore after the failed save: "
                             + ", ".join(unrestored))
            raise
        finally:
            for path in [*temps.values(), *previous.values()]:
                if isinstance(path, Path):
                    path.unlink(missing_ok=True)

    @staticmethod
    def _keep_previous(path: Path):
        """
        Used by _replace_together.
        Return what restores ``path``: a hard link to it, which a rollback
        renames back without writing, its bytes if no link can be made, or
        None if it does not exist.
        """
        if not path.is_file():
            return None
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.close(descriptor)
        backup = Path(name)
        try:
            backup.unlink()
            os.link(path, backup)
            return backup
        except OSError:
            backup.unlink(missing_ok=True)
            return path.read_bytes()

    @staticmethod
    def _restore_file(path: Path, previous) -> None:
        """Used by _replace_together. Put back what _keep_previous returned."""
        if previous is None:
            path.unlink(missing_ok=True)
            return
        temp = None
        try:
            if isinstance(previous, bytes):
                descriptor, temp = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(previous)
                previous = temp
            # Not Path.replace, which the chaos tests count as forward writes.
            os.replace(previous, path)
        finally:
            if temp is not None:
                Path(temp).unlink(missing_ok=True)

    @staticmethod
    def _entries_of(index, paths) -> dict:
        """Return the entries of ``paths`` in ``index``, keyed by (path, stage)."""
        return {key: entry for key, entry in index.entries.items()
                if key[0] in paths}

    def _restore_entries(self, paths, entries) -> None:
        """Used by _replace_together. Give ``paths`` their earlier index entries."""
        # Each access to self.index reads a new index object.
        index = self.index
        current = self._entries_of(index, paths)
        if current == entries:
            return
        for key in current:
            del index.entries[key]
        index.entries.update(entries)
        index.write()

    def _add_to_index(self, paths) -> None:
        """
        Stage worktree files in one Git process.

        GitPython hashes each added file in its own subprocess. Plumbing
        reads literal, NUL-separated paths, so names need no pathspec quoting
        and unchanged files are only checked, not hashed again.

        Args:
            paths: Paths relative to the ``.hm`` worktree.
        """
        with TemporaryFile() as names:
            names.write(b"".join(os.fsencode(str(path)) + b"\0" for path in paths))
            names.seek(0)
            self.git.update_index("--add", "-z", "--stdin", istream=names)

    def read_yaml(self, stem: Union[Path, str]) -> dict:
        """
        Load a YAML file and return its contents as a dictionary.

        Args:
            stem (Union[Path, str]): The stem of the file name (without extension)

        Returns:
            dict: The contents of the YAML file as a dictionary.
        """
        # return the contents of the YAML file as a dictionary,
        # using the helper function to load the YAML file and handle empty files
        return load_yaml_file(self._storage_path(stem, ".yml"))

    def write_yaml(self, data: dict, stem: Union[Path, str]) -> None:
        """
        Dump a dictionary to a YAML file, preserving key order and using
        literal block style for multi-line strings.

        Args:
            data (dict): The dictionary to be dumped to YAML.
            stem (Union[Path, str]): The stem of the file name (without extension)
        """
        path = self._storage_path(stem, ".yml")
        # Use a temporary file to ensure atomic write operations, preventing
        # data corruption in case of interruptions during the write process.
        with replace_file_on_success(path) as temp_path:
            with temp_path.open("w", encoding="utf-8") as handle:
                # Use a custom YAML dumper to preserve key order
                # and handle multi-line strings
                write_yaml(data, handle)

    def read_tsv(self, stem: Union[Path, str]) -> pd.DataFrame:
        """
        Load a TSV file into a pandas DataFrame.

        Args:
            stem (Union[Path, str]): The stem of the file name (without extension)

        Returns:
            pd.DataFrame: The contents of the TSV file as a pandas DataFrame.
        """
        # return a DataFrame by reading the TSV file with tab separator
        return pd.read_csv(
            self._storage_path(stem, ".tsv"),
            sep="\t",
            dtype=str,
            encoding="utf-8",
            keep_default_na=False)

    def write_tsv(self, data: pd.DataFrame, stem: Union[Path, str]) -> None:
        """
        Write catalogue rows to a TSV file atomically, as ``data_tsv_text`` does.

        Args:
            data (pd.DataFrame): The rows to write.
            stem (Union[Path, str]): The stem of the file name (without extension).
        """
        path = self._storage_path(stem, ".tsv")
        with replace_file_on_success(path) as temp_path:
            with temp_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(data_tsv_text(data))
