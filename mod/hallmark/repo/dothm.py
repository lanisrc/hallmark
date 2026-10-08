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

from configparser import NoOptionError, NoSectionError
from functools import cached_property
from pathlib import Path
from typing import Optional, Union
from io import BytesIO
import json

from git import Repo
from git.exc import GitCommandError
from tempfile import TemporaryFile

import pandas as pd
import yaml

from ..error import CloneError, DothmError
from ..utils import (replace_file_on_success,
                     load_yaml_file,
                     validate_path_name,
                     resolve_path_in_root,
                     load_yaml)
from .state import State
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


class Dothm(Repo):
    """Local ``.hm`` storage backend.

    The backend version controls the hallmark ``State`` database files
    (``config.yml``, ``meta.yml``, ``data.tsv``) on-disk.
    It is itself a git worktree.
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
        return Path(self.working_tree_dir)

    def stage_file_version(self, record: dict) -> None:
        """
        Stage a version of a file in the repository.

        Args:
            record (dict): A dictionary containing the file version information.
        """
        # Get the relative path of the file within the repository
        relative = Path(record["path"])
        # create the path for storing the versioned file within the "versions" directory
        entry_path = Path("versions") / relative
        # resolve the full path of the versioned file within the repository's root
        path = resolve_path_in_root(self.path, entry_path, label="version path")
        # ensure the parent directory exists before writing the file
        path.parent.mkdir(parents=True, exist_ok=True)

        # write the file content to a temporary file and replace the original on success
        with replace_file_on_success(path) as temp_path:
            # write the JSON representation of the record to the temporary file
            temp_path.write_text(
                json.dumps(
                    {**record, "path": relative.as_posix()},
                    sort_keys=True,
                    default=str), encoding="utf-8")
        # add the versioned file to the git index
        self.index.add([entry_path.as_posix()])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.working_tree_dir is None:
            raise DothmError('The ".hm" directory must be a valid git ' \
            'worktree.')

    @classmethod
    def init(cls, *args, **kwargs) -> "Dothm":
        if kwargs.get('bare', False):
            raise DothmError('A ".hm" directory must not be a bare git ' \
            'repository')
        kwargs.setdefault("initial_branch", "main")
        dothm = super().init(*args, **kwargs)

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
        to_path = Path(to_path)

        try:
            super().clone_from(url, str(to_path))
            dothm = cls(str(to_path))

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

    def link_worktree(self, path: Union[Path, str], branch: Optional[str] = None):
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
        return self._read_identity(self.config_reader("repository"))

    def effective_identity(self) -> tuple[Optional[str], Optional[str]]:
        return self._read_identity(self.config_reader())

    def load_state(self, revision=None, *, staged=False) -> State:
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
            content = blobs["data.tsv"].data_stream.read()
            if content.strip():
                state.data = pd.read_csv(
                    BytesIO(content), sep="\t", dtype=str, keep_default_na=False
                )

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


    def restore_index(self, paths: list[str]) -> None:
        """
        Restore the Git index for the specified paths.

        Args:
            paths (list[str]): List of file paths to restore.

        Raises:
            ValueError: If no paths are provided or if a specified path does not exist
              in the index or HEAD.
        """
        if not paths:
            raise ValueError("Usage: hm restore --staged <path> [<path> ...]")

        index = self.index
        head = self.head.commit if self.head.is_valid() else None
        head_paths = {
            blob.path for blob in head.tree.traverse() if blob.type == "blob"
        } if head is not None else set()
        index_paths = {path for path, stage in index.entries if stage == 0}

        if "." in paths:
            if head is None:
                index.entries.clear()
                index.write()
            else:
                index.reset(head, working_tree=False)
            return

        old_head = {}
        old_index = {}
        for revision, known, records in (
            ("HEAD", head_paths, old_head),
            (None, index_paths, old_index),
        ):
            if "versions.yml" in known or (revision and head is None):
                continue
            state = self.load_state(revision, staged=True)
            fmt = single_data_format(state.config)
            for record in state.data.fillna("").to_dict(orient="records"):
                record["path"] = row_to_path(record, fmt).as_posix()
                records[f"versions/{record['path']}"] = record

        known = head_paths | index_paths | old_head.keys() | old_index.keys()
        selected = set()
        for path in paths:
            matches = {
                name for name in known
                if name == path or name.startswith(path + "/")
            }
            if not matches:
                label = (
                    path.removeprefix("versions/")
                    if path.startswith("versions/") else f".hm/{path}"
                )
                raise ValueError(
                    f"No staged or committed path matches {label!r}"
                )
            selected.update(matches)

        versions_selected = any(
            path.startswith("versions/") for path in selected
        )
        if versions_selected and "versions.yml" in head_paths:
            changed = {
                diff.a_path or diff.b_path for diff in index.diff(head)
            }
            versions_selected = any(
                path.startswith("versions/") for path in selected & changed
            )

        original = index.entries.copy()
        try:
            if versions_selected and "versions.yml" not in index_paths:
                columns = list(self.load_state(staged=True).data.columns)
                for path, record in old_index.items():
                    self.stage_text(
                        path, json.dumps(record, sort_keys=True, default=str)
                    )
                self.stage_text(
                    "versions.yml", yaml.safe_dump({"columns": columns})
                )

            index = self.index
            if head is None:
                for path in selected:
                    index.entries.pop((path, 0), None)
                index.write()
            else:
                index.reset(
                    head, paths=sorted(selected), working_tree=False
                )

            if versions_selected:
                for path in selected & old_head.keys():
                    self.stage_text(
                        path,
                        json.dumps(
                            old_head[path], sort_keys=True, default=str
                        ),
                    )

                state = self.load_state(staged=True)
                if "versions.yml" in head_paths:
                    same_versions = not any(
                        (diff.a_path or diff.b_path).startswith("versions/")
                        for diff in self.index.diff(head)
                    )
                else:
                    records = {
                        f"versions/{record['path']}": record
                        for record in state.data.to_dict(orient="records")
                    }
                    same_versions = records == old_head

                if same_versions and "data.tsv" in head_paths:
                    reset_paths = ["data.tsv", "versions.yml"]
                    if "versions.yml" not in head_paths:
                        reset_paths.extend(
                            path for path, stage in self.index.entries
                            if stage == 0 and path.startswith("versions/")
                        )
                    self.index.reset(
                        head, paths=reset_paths, working_tree=False
                    )
                else:
                    self.stage_text(
                        "data.tsv",
                        state.data.to_csv(
                            sep="\t", index=False, lineterminator="\n"
                        ),
                    )
        except Exception:
            index = self.index
            index.entries = original
            index.write()
            raise


    def save_state(self, state: State) -> None:
        fmt = single_data_format(state.config)
        paths = set()

        for record in state.data.fillna("").to_dict(orient="records"):
            record["path"] = row_to_path(record, fmt).as_posix()
            self.stage_file_version(record)
            paths.add(f"versions/{record['path']}")

        for_deletion = [
            path
            for path, stage in self.index.entries
            if stage == 0
            and path.startswith("versions/")
            and path not in paths
        ]
        if for_deletion:
            # These are generated metadata files. Leaving them on disk makes
            # Git treat them as untracked blockers when another branch has them.
            self.index.remove(for_deletion, working_tree=True, force=True)

        self.write_yaml(state.config, "config")
        self.write_yaml(state.meta, "meta")
        self.write_tsv(state.data, "data")
        self.write_yaml(
            {"columns": list(state.data.columns)}, "versions"
        )
        self.index.add([
            "config.yml", "meta.yml", "data.tsv", "versions.yml"
        ])

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

    def write_tsv(
            self,
            data: pd.DataFrame,
            stem: Union[Path, str],
            *,
            na_rep: str = ""
            ) -> None:
        """
        Dump a pandas DataFrame to a TSV file, ensuring atomic write operations.

        Args:
            data (pd.DataFrame): The DataFrame to be dumped to TSV.
            stem (Union[Path, str]): The stem of the file name (without extension).
            na_rep (str, optional): String representation for missing values.
                Defaults to an empty string.
        """
        path = self._storage_path(stem, ".tsv")

        # Use a temporary file to ensure atomic write operations, preventing
        # data corruption in case of interruptions during the write process.
        with replace_file_on_success(path) as temp_path:
            # Write the DataFrame to a temporary file in TSV format, ensuring that
            # the index is not included and UTF-8 encoding is used for compatibility.
            data.to_csv(
                temp_path,
                sep="\t",
                index=False,
                encoding="utf-8",
                na_rep=na_rep)
