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

from pathlib import Path
from typing import Union


class Worktree(type(Path())):
    """Materialized data root used by indexing and consumer tools.

    ``Worktree`` is where file objects are discovered by format string
    and later consumed by downstream software.
    """

    def __new__(cls, path: Union[Path, str]) -> "Worktree":
        """Create a new Worktree instance with the given path.
        Raises NotADirectoryError if the path is not a directory.
        Raises FileNotFoundError if the path does not exist."""
        # use expanduser() to handle ~ in paths and resolve() to get the absolute path
        resolved = Path(path).expanduser().resolve()
        # check if the resolved path exists first to avoid silent failures
        if not resolved.exists():
            raise FileNotFoundError(f'Worktree "{resolved}" not found')
        # raise error instead of returning a non-existent path to avoid silent failures
        if not resolved.is_dir():
            raise NotADirectoryError(f'Worktree "{resolved}" is not a directory')

        # if the checks pass, create a new instance of Worktree with the resolved path
        return super().__new__(cls, resolved)

    @classmethod
    def init(cls, path: Union[Path, str]) -> "Worktree":
        """Initialize a new Worktree at the given path. Creates the directory if it
        does not exist, including any necessary parent directories.
        Raises NotADirectoryError if the path exists but is not a directory."""
        # use expanduser() to handle ~ in paths and resolve() to get the absolute path
        resolved = Path(path).expanduser().resolve()
        # raise error instead of returning a non-existent path to avoid silent failures
        if resolved.exists() and not resolved.is_dir():
            raise NotADirectoryError(f'Worktree "{resolved}" is not a directory')
        # make the directory if it doesn't exist, including necessary parent directories
        resolved.mkdir(parents=True, exist_ok=True)
        # create a new instance of the Worktree class with the resolved path
        return cls(resolved)

    def __truediv__(self, key):
        return Path(self) / key
