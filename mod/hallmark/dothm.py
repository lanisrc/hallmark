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

from functools import cached_property
from pathlib import Path
from typing import Optional, Union
from git import Repo
from git.exc import GitCommandError

import pandas as pd
import yaml

from .error import CloneError, DothmError
from .helper_functions import (
    atomic_output_path, load_yaml_file, validate_path_component)
from .state import State


class _HallmarkYamlDumper(yaml.Dumper):
    """
    Used by dump_yaml.
    Custom YAML dumper for Hallmark that preserves the order of keys in dictionaries
    and uses literal block style for multi-line strings.
    Args:
        yaml.Dumper: The base YAML dumper class to extend. YAML dumper is
            responsible for converting Python objects into YAML format.
    """


def _str_presenter(dumper, data):
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
_HallmarkYamlDumper.add_representer(str, _str_presenter)

def dump_yaml(data, handle) -> None:
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
        Used by load_yml, dump_yml, load_tsv, and dump_tsv.
        Get the full path to a storage file in the ``.hm`` directory.
        Args:
            stem (Union[Path, str]): The stem of the file name (without extension).
            suffix (str): The file extension (e.g., ".yml", ".tsv").
        Returns:
            Path: The full path to the storage file with the correct suffix.
        """
        # validate the name of the storage file to ensure it is a valid path component
        name = validate_path_component(stem, label="storage name")
        path = self.path / name
        # ensure the path has the correct suffix and is in the correct directory
        if path.suffix.lower() != suffix.lower():
            path = path.with_suffix(suffix)
        return path

    @cached_property
    def path(self) -> Path:
        return Path(self.working_tree_dir)

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
# For simple names, you can just run: hallmark add "a{a}_i{i}.h5"
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

    def link(self, path: Union[Path, str], branch: Optional[str] = None):
        path = Path(path).resolve()  # use absolute path
        # try to add the specified path as a git worktree
        try:
            self.git.worktree("add", path, branch)
        # if adding the worktree fails, raise a DothmError with a helpful message
        except GitCommandError as exc:
            raise DothmError(f'Failed to link "{path}": {exc}')
        return Dothm(path)

    def load(self) -> State:
        return State(
            config = self.load_yml("config"),
            meta = self.load_yml("meta"),
            data = self.load_tsv("data"))

    def dump(self, state: State) -> None:
        self.dump_yml(state.config, "config")
        self.dump_yml(state.meta,   "meta")
        self.dump_tsv(state.data,   "data")
        self.index.add(["config.yml", "meta.yml", "data.tsv"])

    def load_yml(self, stem: Union[Path, str]) -> dict:
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

    def dump_yml(self, data: dict, stem: Union[Path, str]) -> None:
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
        with atomic_output_path(path) as temp_path:
            with temp_path.open("w", encoding="utf-8") as handle:
                # Use a custom YAML dumper to preserve key order
                # and handle multi-line strings
                dump_yaml(data, handle)

    def load_tsv(self, stem: Union[Path, str]) -> pd.DataFrame:
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

    def dump_tsv(
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
        with atomic_output_path(path) as temp_path:
            # Write the DataFrame to a temporary file in TSV format, ensuring that
            # the index is not included and UTF-8 encoding is used for compatibility.
            data.to_csv(
                temp_path,
                sep="\t",
                index=False,
                encoding="utf-8",
                na_rep=na_rep)