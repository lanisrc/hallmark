"""
Keep downloaded file contents beneath ``.hm/cache``, named by SHA-1.

A download is written to a temporary ``cache/tmp/.NAME.RANDOM`` file, which
only gets its final name once complete and verified, as rsync does. The
object is then made read-only and hard-linked into the worktree, so a file
appears there whole or not at all, and the same bytes are never stored
twice. Everything in the cache can be downloaded again, so removing it is
always safe; ``.hm/objects`` keeps the committed local files that cannot.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from ..utils import NO_HARD_LINK_ERRORS, publish_no_overwrite
from .objects import Objects

# Leftovers older than this come from downloads that stopped; an active
# transfer keeps updating its temporary file's modification time.
STALE_TEMP_SECONDS = 24 * 3600
# Windows cannot delete a read-only file or detect in-place edits through a
# hard link, so downloads there are copied instead.
LINK_INTO_WORKTREE = os.name != "nt"
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _make_read_only(path: Path) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    os.chmod(path, mode & ~_WRITE_BITS)


class Cache(Objects):
    """
    Re-downloadable file contents, stored by SHA-1.

    Args:
        path (Path | str): Main ``.hm`` directory; linked worktrees pass the
            same directory so they share one cache.
    """

    def __init__(self, path: Union[Path, str]):
        super().__init__(path, name="cache")
        self.tmp = self.root / "tmp"

    @contextmanager
    def temp_file(self, name: str):
        """
        Yield an empty temporary file named ``.NAME.RANDOM`` in the cache.

        The file is removed on exit unless it was published.

        Args:
            name (str): Base name of the file being downloaded.
        """
        self.tmp.mkdir(parents=True, exist_ok=True)
        descriptor, temp = tempfile.mkstemp(prefix=f".{name}.", dir=self.tmp)
        os.close(descriptor)
        try:
            yield Path(temp)
        finally:
            Path(temp).unlink(missing_ok=True)

    def publish(self, temp: Path, sha1: str) -> Path:
        """
        Store a verified temporary file as the object for its SHA-1.

        An existing object with the same content is kept, so files already
        linked to it stay linked. An object whose content no longer matches,
        because a linked file was edited in place, is replaced.

        Args:
            temp (Path): Complete temporary file whose SHA-1 is ``sha1``.
            sha1 (str): SHA-1 of the file's content.

        Returns:
            Path: The object's path.
        """
        target = self.object_path(sha1)
        target.parent.mkdir(parents=True, exist_ok=True)
        _make_read_only(temp)
        try:
            publish_no_overwrite(temp, target)
        except FileExistsError:
            if self._calculate_sha1(target) != self._validate_sha1(sha1):
                os.replace(temp, target)
        return target

    def is_linked(self, sha1: str, path: Path) -> bool:
        """
        Return whether ``path`` is a hard link to the object for ``sha1``.

        Args:
            sha1 (str): Object to compare with.
            path (Path): File in the worktree; symbolic links never match.
        """
        try:
            target = self.object_path(sha1).stat()
            found = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return (stat.S_ISREG(found.st_mode)
                and (found.st_dev, found.st_ino) == (target.st_dev, target.st_ino))

    def materialize(self, sha1: str, destination: Path) -> str:
        """
        Place the object for ``sha1`` at a new destination.

        The object is hard-linked where possible. Where the destination is on
        another filesystem, or links are unavailable, a verified writable copy
        is made instead. An existing destination is never replaced.

        Args:
            sha1 (str): Object to place.
            destination (Path): New file path; its folder must exist.

        Returns:
            str: ``"linked"`` or ``"copied"``.

        Raises:
            FileExistsError: If ``destination`` already exists.
            FileNotFoundError: If the object is not cached.
        """
        source = self.object_path(sha1)
        if LINK_INTO_WORKTREE:
            try:
                os.link(source, destination)
                return "linked"
            except FileExistsError:
                raise
            except OSError as exc:
                if exc.errno not in NO_HARD_LINK_ERRORS:
                    raise
        if not source.is_file():
            raise FileNotFoundError(f"object {sha1} not found in the cache")
        self._copy_and_verify_file(source, destination, expected_sha1=sha1,
                                   overwrite=False)
        mode = stat.S_IMODE(os.stat(destination).st_mode)
        os.chmod(destination, mode | stat.S_IWUSR)
        return "copied"

    def cleanup_stale_temps(self, max_age: float = STALE_TEMP_SECONDS) -> int:
        """
        Remove temporary files left by downloads that stopped long ago.

        Args:
            max_age (float): Minimum age in seconds since the last write.

        Returns:
            int: Number of files removed.
        """
        if not self.tmp.is_dir():
            return 0
        cutoff = time.time() - max_age
        removed = 0
        for entry in os.scandir(self.tmp):
            try:
                if (entry.is_file(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                    os.unlink(entry.path)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed
