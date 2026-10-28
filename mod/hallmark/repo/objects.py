"""
Keep file contents in ``.hm/objects``, named by their SHA-1.

One store serves both kinds of catalogue, as git's object database does.
``hm commit`` copies local files in, and ``checkout`` copies them back out
as writable files. Downloads arrive through a temporary
``objects/tmp/.NAME.RANDOM`` file that only gets its final name once it is
complete and verified, as rsync does; they are then hard-linked into the
worktree, read-only, so a file appears there whole or not at all and the
same bytes are never stored twice.

Every object is read-only. Some objects are the only copy of a committed
local file, so nothing in the store may be deleted by hand.

A download stays hard-linked to its object, so writing to it in place, after
``chmod`` or as root, would change the object too. Each object therefore has
a record of its size and modification time in ``objects/stat/``, as git's
index and git-annex's inode cache keep for the files they track. A link is
only trusted while its object still matches that record.
"""

import hashlib
import os
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

from ..utils import (
    FILE_IO_CHUNK_SIZE,
    NO_HARD_LINK_ERRORS,
    replace_file_on_success,
    publish_no_overwrite,
    calculate_file_checksum,
    is_valid_checksum)

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


def _make_writable(path: Path) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    os.chmod(path, mode | stat.S_IWUSR)


def _link_over(source: Path, destination: Path) -> None:
    """Hard-link ``source`` to ``destination``, replacing it atomically."""
    while True:
        temp = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}")
        try:
            os.link(source, temp)
            break
        except FileExistsError:
            continue
    try:
        os.replace(temp, destination)
    finally:
        # rename() leaves both names when they already link the same file.
        temp.unlink(missing_ok=True)


class Objects:
    """
    Manage the Hallmark object store.

    Files are stored read-only in a content-addressed directory structure
    based on their SHA-1 checksum, so they can be restored to a worktree or
    hard-linked there as downloads.

    Attributes:
        root (Path): The store directory, ``path / name``.
        tmp (Path): Folder of unfinished downloads, ``root / "tmp"``.
        stats (Path): Folder of each object's size and modification time
            record, ``root / "stat"``.
    """
    def __init__(self, path: Union[Path, str], name: str = "objects"):
        """
        Initialize the object store.

        Args:
            path (Path or str): Main ``.hm`` directory; linked worktrees pass
                the same directory so they share one store.
            name (str): Store directory beneath ``path``.
        """
        self.root = Path(path) / name
        self.tmp = self.root / "tmp"
        self.stats = self.root / "stat"

    @staticmethod
    def _copy_and_verify_file(
        src: Path,
        dest: Path,
        *,
        expected_sha1: str,
        chunk_size: int = FILE_IO_CHUNK_SIZE,
        overwrite: bool = True,
        ) -> Path:
        """
        Used by store and restore.
        Copy a file from src to dest atomically, verifying its SHA-1 checksum.

        Args:
            src (Path): Source file path.
            dest (Path): Destination file path.
            expected_sha1 (str): Expected SHA-1 checksum of the source file.
            chunk_size (int): Size of chunks to read at a time.
            overwrite (bool): Replace an existing destination; otherwise
                fail with FileExistsError.

        Returns:
            Path: Path to the copied file.

        Raises:
            ValueError: If the actual SHA-1 checksum of the source file does not match
            the expected SHA-1 checksum.
        """
        src = Path(src)
        dest = Path(dest)
        # Normalize the expected SHA-1 checksum to ensure it is valid and in lowercase
        normalized_expected = Objects._validate_sha1(expected_sha1)
        # Ensure the destination directory exists
        dest.parent.mkdir(parents=True, exist_ok=True)
        # If an expected SHA-1 checksum is provided, initialize a SHA-1 hash object
        digest = hashlib.sha1()

        # Use replace_file_on_success to create a temporary file for the copy operation
        with replace_file_on_success(dest, overwrite=overwrite) as temp_path:
            # read the source file in binary mode and write to the temporary file
            with src.open("rb") as source, temp_path.open("wb") as target:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    target.write(chunk)
                    # update the SHA-1 hash with the chunk read from the source file
                    digest.update(chunk)
            # calculate the actual SHA-1 checksum of the source file
            actual_sha1 = digest.hexdigest()
            # raise an error if actual checksum and normalized checksum don't match
            if actual_sha1 != normalized_expected:
                raise ValueError(
                    f'checksum mismatch while copying "{src}": '
                    f"expected SHA-1 {normalized_expected}, got {actual_sha1}")
            # copy the temporary file to the destination, preserving metadata
            shutil.copystat(src, temp_path)

        return dest

    @staticmethod
    def _calculate_sha1(path: Path, chunk_size: int = FILE_IO_CHUNK_SIZE) -> str:
        """
        Used by store and in repo by checksum.
        Calculate a file's SHA-1 checksum using streaming reads.

        Args:
            path (Path): Path to the file.
            chunk_size (int): Size of chunks to read at a time.

        Returns:
            str: SHA-1 checksum of the file.
        """
        # Use the helper function calculate_file_checksum to compute the SHA-1 checksum
        return calculate_file_checksum(path, algorithm="sha1", chunk_size=chunk_size)


    @staticmethod
    def _validate_sha1(sha1: str) -> str:
        """
        Used by _copy_and_verify_file, _path_for_checksum, and store.
        Validate and normalize a SHA-1 checksum.

        Args:
            sha1 (str): SHA-1 checksum to validate and normalize.

        Returns:
            str: Normalized SHA-1 checksum.

        Raises:
            ValueError: If the SHA-1 checksum is not exactly 40 hexadecimal characters.
        """
        # if the checksum is not a string, or has leading/trailing whitespace,
        # or is invalid, raise a ValueError indicating the checksum is invalid
        if (
            not isinstance(sha1, str)
            or sha1 != sha1.strip()
            or not is_valid_checksum("sha1", sha1)
            ):
            raise ValueError("SHA-1 checksum must be exactly 40 hexadecimal characters")

        # if the checksum is valid, return it in lowercase
        return sha1.lower()


    def _path_for_checksum(self, sha1: str) -> Path:
        """
        Used by contains, store, and restore.
        Convert a validated SHA-1 checksum into its object-store path.

        Args:
            sha1 (str): SHA-1 checksum.

        Returns:
            Path: Path where the object is stored.

        Raises:
            ValueError: If the checksum is not exactly 40 hexadecimal characters.
        """
        # normalize the SHA-1 checksum to ensure it is valid and in lowercase
        normalized = self._validate_sha1(sha1)
        # split the checksum into two parts
        return self.root / normalized[:2] / normalized[2:]

    def contains(self, sha1: str) -> bool:
        """
        Return whether a checksum exists in the object store.

        Args:
            sha1 (str): SHA-1 checksum to locate.

        Returns:
            bool: True when the corresponding object exists.

        Raises:
            ValueError: If ``sha1`` is not exactly 40 hexadecimal characters.
        """
        return self._path_for_checksum(sha1).is_file()

    def object_path(self, sha1: str) -> Path:
        """
        Return the path where a checksum's object is (or would be) stored.

        Args:
            sha1 (str): SHA-1 checksum to locate.

        Returns:
            Path: Path to the object in this store.

        Raises:
            ValueError: If ``sha1`` is not exactly 40 hexadecimal characters.
        """
        return self._path_for_checksum(sha1)

    def checksums(self) -> list[str]:
        """
        Return the SHA-1 of every stored object, sorted.

        Stat records and unfinished downloads are not objects and are left out.
        """
        if not self.root.is_dir():
            return []
        found = []
        for folder in self.root.iterdir():
            if len(folder.name) != 2 or not folder.is_dir():
                continue
            found.extend(folder.name + entry.name for entry in folder.iterdir()
                         if is_valid_checksum("sha1", folder.name + entry.name))
        return sorted(found)

    def missing_checksums(self, checksums: Iterable[str]) -> list[str]:
        """
        Return a sorted list of checksums that are not present in the object store.

        Args:
            checksums (Iterable[str]): Iterable of SHA-1 checksums to check.

        Returns:
            list[str]: Sorted list of missing SHA-1 checksums.
        """
        unique_checksums = set(checksums)
        # return a sorted list of checksums that are not present in the object store
        return sorted(
            str(checksum)
            for checksum in unique_checksums
            if not self.contains(checksum))

    def _keep_or_replace(self, target: Path, sha1: str) -> bool:
        """
        Return whether an existing object still holds the content it is named after.

        An object whose content no longer matches, because a linked file was
        edited in place, is removed so the caller can store the content again.
        """
        if self._calculate_sha1(target) == self._validate_sha1(sha1):
            return True
        target.unlink()
        return False

    def _stat_path(self, sha1: str) -> Path:
        normalized = self._validate_sha1(sha1)
        return self.stats / normalized[:2] / normalized[2:]

    def _record_stat(self, sha1: str) -> None:
        """Record the size and modification time of a verified object."""
        info = self.object_path(sha1).stat()
        record = self._stat_path(sha1)
        record.parent.mkdir(parents=True, exist_ok=True)
        with replace_file_on_success(record) as temp:
            temp.write_text(f"{info.st_size} {info.st_mtime_ns}\n", encoding="ascii")

    def _recorded_stat(self, sha1: str) -> Optional[tuple]:
        try:
            size, mtime_ns = self._stat_path(sha1).read_text(encoding="ascii").split()
            return int(size), int(mtime_ns)
        except (FileNotFoundError, ValueError):
            return None

    def changed(self, sha1: str) -> bool:
        """
        Return whether an object no longer matches its recorded stat.

        Nothing is hashed or removed, so this suits read-only reports. An
        object without a record, or a missing object, is not reported.

        Args:
            sha1 (str): Object to check.
        """
        recorded = self._recorded_stat(sha1)
        try:
            info = self.object_path(sha1).stat()
        except FileNotFoundError:
            return False
        return recorded is not None and recorded != (info.st_size, info.st_mtime_ns)

    def verify(self, sha1: str) -> bool:
        """
        Return whether an object can be trusted to hold its named content.

        An object matching its recorded stat is trusted without hashing. One
        that differs is hashed: if the bytes still match, as after ``touch``,
        the record is renewed; otherwise the object was changed in place and
        is discarded, so it is downloaded again rather than linked anywhere
        else. An object stored before records existed is trusted and recorded.

        Args:
            sha1 (str): Object to check.

        Returns:
            bool: True if the object exists and holds its content.
        """
        path = self.object_path(sha1)
        try:
            info = path.stat()
        except FileNotFoundError:
            return False
        recorded = self._recorded_stat(sha1)
        if recorded == (info.st_size, info.st_mtime_ns):
            return True
        if recorded is not None and self._calculate_sha1(path) != sha1.lower():
            self.discard(sha1)
            return False
        self._record_stat(sha1)
        return True

    def discard(self, sha1: str) -> None:
        """
        Remove an object and its record from the store.

        Worktree files hard-linked to it keep their bytes.

        Args:
            sha1 (str): Object to remove.
        """
        self.object_path(sha1).unlink(missing_ok=True)
        self._stat_path(sha1).unlink(missing_ok=True)

    def store(self, src: Path, sha1: str, *, actual_sha1: Optional[str] = None) -> Path:
        """
        Store a copy of a file in the object store using its SHA-1 checksum.

        Args:
            src (Path): Path to the source file to store.
            sha1 (str): SHA-1 checksum of the source file.
            actual_sha1 (Optional[str]): Optional actual SHA-1 of the source file.
                If provided, it will be used to verify the integrity of the source file.

        Returns:
            Path: Path to the stored, read-only object.

        Raises:
            ValueError: If the actual SHA-1 checksum of the source file does not match
                the expected SHA-1.
        """
        src = Path(src)
        # split the SHA-1 checksum into its storage path
        stored = self._path_for_checksum(sha1)
        expected_sha1 = sha1.lower()
        # if the actual SHA-1 checksum is not provided, calculate it from source file
        if actual_sha1 is None:
            actual_sha1 = self._calculate_sha1(src)
        # if the actual SHA-1 checksum is provided, normalize it to lowercase
        else:
            actual_sha1 = self._validate_sha1(actual_sha1)

        # if the actual checksum does not match the expected checksum, raise an error
        if actual_sha1 != expected_sha1:
            raise ValueError(
                f'File "{src}" changed after it was added: '
                f"expected SHA-1 {expected_sha1}, got {actual_sha1}. "
                "Run hm add again before committing.")
        if not (stored.is_file() and self._keep_or_replace(stored, expected_sha1)):
            self._copy_and_verify_file(src, stored, expected_sha1=expected_sha1)
            _make_read_only(stored)
        self._record_stat(expected_sha1)
        return stored

    def restore(self, sha1: str, dest: Path) -> Path:
        """
        Restore a writable copy of an object to a specified destination.

        Args:
            sha1 (str): SHA-1 checksum of the object to restore.
            dest (Path): Destination path for the restored file.

        Returns:
            Path: Path to the restored file.

        Raises:
            FileNotFoundError: If the object does not exist in the store.
            ValueError: If ``sha1`` is malformed, or the object's content no
                longer matches it.
        """
        # split the SHA-1 checksum into its storage path
        stored = self._path_for_checksum(sha1)
        # if the file does not exist in the object store, raise an error
        if not stored.is_file():
            raise FileNotFoundError(
                f"object {sha1} not found in objects store")
        # otherwise, copy the stored file to the destination atomically
        restored = self._copy_and_verify_file(stored, dest, expected_sha1=sha1)
        # Objects are read-only, but local files stay editable in the worktree.
        _make_writable(restored)
        return restored

    @contextmanager
    def temp_file(self, name: str):
        """
        Yield an empty temporary file named ``.NAME.RANDOM`` in the store.

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
            if not self._keep_or_replace(target, sha1):
                os.replace(temp, target)
        self._record_stat(sha1)
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

    def materialize(self, sha1: str, destination: Path, *,
                    replace: bool = False) -> str:
        """
        Place the object for ``sha1`` at a destination as a download.

        The object is hard-linked, read-only, where possible. On Windows, where
        the destination is on another filesystem, or where links are unavailable,
        a verified writable copy is made instead. An existing destination is
        only replaced when asked, and then atomically, so it is never missing.

        Args:
            sha1 (str): Object to place.
            destination (Path): File path; its folder must exist.
            replace (bool): Replace an existing destination.

        Returns:
            str: ``"linked"`` or ``"copied"``.

        Raises:
            FileExistsError: If ``destination`` exists and ``replace`` is False.
            FileNotFoundError: If the object is not stored.
            ValueError: If ``sha1`` is malformed, or a copied object's content no
                longer matches it.
        """
        source = self.object_path(sha1)
        if LINK_INTO_WORKTREE:
            try:
                if replace:
                    _link_over(source, Path(destination))
                else:
                    os.link(source, destination)
                return "linked"
            except FileExistsError:
                raise
            except OSError as exc:
                if exc.errno not in NO_HARD_LINK_ERRORS:
                    raise
        if not source.is_file():
            raise FileNotFoundError(f"object {sha1} not found in the object store")
        self._copy_and_verify_file(source, destination, expected_sha1=sha1,
                                   overwrite=replace)
        _make_writable(destination)
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
