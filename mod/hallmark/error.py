# Copyright 2026 the Hallmark Authors
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

"""Error hierarchy for Hallmark."""

from pathlib import Path
from typing import Optional, Union

from git.exc import GitError, GitCommandError


class HallmarkError(RuntimeError):
    """Base exception for Hallmark-specific failures."""


class DothmError(HallmarkError, GitError):
    """Raised for `.hm` repository validation and access failures."""


class CloneError(HallmarkError, GitError):
    """Raised for hallmark clone failures."""

    @classmethod
    def from_git_command(
        cls,
        error: GitCommandError,
        fallback: Optional[str] = None,
        clone_path: Optional[Union[Path, str]] = None,
        display_path: Optional[Union[Path, str]] = None,
    ) -> "CloneError":
        """
        Build a clone error from a failed Git command.

        The message starts at Git's ``fatal:`` line when there is one, and names
        ``display_path`` wherever Git printed the clone path.

        Args:
            error (GitCommandError): The failed Git command.
            fallback (str, optional): Message to use when Git printed nothing.
            clone_path (Path | str, optional): Path that Git cloned into.
            display_path (Path | str, optional): Path to show the user instead.

        Returns:
            CloneError: The error to raise.
        """
        text = str(error.stderr or fallback or error).strip()
        if "fatal:" in text:
            text = text[text.index("fatal:"):].strip(" '")

        if clone_path is not None and display_path is not None:
            clone_path = Path(clone_path)
            display_text = str(Path(display_path))
            # all possible versions of the clone path to replace in the error message
            clone_candidates = {
                str(clone_path),
                str(clone_path.expanduser().resolve()),}
            # for each candidate, replace it in the error message with the display text
            for candidate in sorted(clone_candidates, key=len, reverse=True):
                text = text.replace(candidate, display_text)

        return cls(text)


class DestinationExistsError(CloneError):
    """Raised when clone destination already exists."""


class CheckoutError(HallmarkError):
    """Raised when a hallmark checkout cannot proceed safely."""
