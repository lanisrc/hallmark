"""
Utilities for storing and restoring content-addressed objects.

This module defines the ``Objects`` class, which manages the Hallmark
object store. Files are stored using their SHA-1 checksum and can later
be restored to a specified location.
"""

import hashlib
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Optional, Union

from .helper_functions import (
    FILE_IO_CHUNK_SIZE,
    atomic_output_path,
    file_checksum,
    valid_checksum)


class Objects:
    """
    Manage the Hallmark object store.

    Files are stored in a content-addressed directory structure based on
    their SHA-1 checksum, allowing them to be efficiently retrieved and
    restored.
    """
    def __init__(self, path: Union[Path, str]):
        """
        Initialize the object store.

        Args:
            path (Path or str): Path to the Hallmark repository. The object
                store is located in the ``objects`` subdirectory.
        """
        self.root = Path(path) / "objects"

    @staticmethod
    def _copy_atomically(
        src: Path,
        dest: Path,
        *,
        expected_sha1: str,
        chunk_size: int = FILE_IO_CHUNK_SIZE,
        ) -> Path:
        """
        Used by store and restore.
        Copy a file from src to dest atomically, verifying its SHA-1 checksum.

        Args:
            src (Path): Source file path.
            dest (Path): Destination file path.
            expected_sha1 (str): Expected SHA-1 checksum of the source file.
            chunk_size (int): Size of chunks to read at a time.

        Returns:
            Path: Path to the copied file.

        Raises:
            ValueError: If the actual SHA-1 checksum of the source file does not match
            the expected SHA-1 checksum.
        """
        src = Path(src)
        dest = Path(dest)
        # Normalize the expected SHA-1 checksum to ensure it is valid and in lowercase
        normalized_expected = Objects._normalize_sha1(expected_sha1)
        # Ensure the destination directory exists
        dest.parent.mkdir(parents=True, exist_ok=True)
        # If an expected SHA-1 checksum is provided, initialize a SHA-1 hash object
        digest = hashlib.sha1()

        # Use atomic_output_path to create a temporary file for the copy operation
        with atomic_output_path(dest) as temp_path:
            # read the source file in binary mode and write to the temporary file
            with src.open("rb") as source, temp_path.open("wb") as target:
                for chunk in iter(lambda: source.read(chunk_size), b""):
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
        # Use the helper function file_checksum to compute the SHA-1 checksum
        return file_checksum(path, algorithm="sha1", chunk_size=chunk_size)


    @staticmethod
    def _normalize_sha1(sha1: str) -> str:
        """
        Used by _copy_atomically, _split_checksum, and store.
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
            or not valid_checksum("sha1", sha1)
            ):
            raise ValueError("SHA-1 checksum must be exactly 40 hexadecimal characters")

        # if the checksum is valid, return it in lowercase
        return sha1.lower()


    def _split_checksum(self, sha1: str) -> Path:
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
        normalized = self._normalize_sha1(sha1)
        # split the checksum into two parts
        return self.root / normalized[:2] / normalized[2:]

    def contains(self, sha1: str) -> bool:
        """
        Return whether a checksum exists in the object store.
        Args:
            sha1 (str): SHA-1 checksum to locate.
        Returns:
            bool: True when the corresponding object exists.
        """
        return self._split_checksum(sha1).is_file()

    def object_path(self, sha1: str) -> Path:
        """
        Return the path where a checksum's object is (or would be) stored.
        Args:
            sha1 (str): SHA-1 checksum to locate.
        Returns:
            Path: Path to the object in this store.
        """
        return self._split_checksum(sha1)

    def missing(self, checksums: Iterable[str]) -> list[str]:
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

    def store(self, src: Path, sha1: str, *, actual_sha1: Optional[str] = None) -> Path:
        """
        Store a file in the object store using its SHA-1 checksum.

        Args:
            src (Path): Path to the source file to store.
            sha1 (str): SHA-1 checksum of the source file.
            actual_sha1 (Optional[str]): Optional actual SHA-1 of the source file.
                If provided, it will be used to verify the integrity of the source file.

        Returns:
            Path: Path to the stored file.

        Raises:
            ValueError: If the actual SHA-1 checksum of the source file does not match
                the expected SHA-1.
        """
        src = Path(src)
        # split the SHA-1 checksum into its storage path
        stored = self._split_checksum(sha1)
        expected_sha1 = sha1.lower()
        # if the actual SHA-1 checksum is not provided, calculate it from source file
        if actual_sha1 is None:
            actual_sha1 = self._calculate_sha1(src)
        # if the actual SHA-1 checksum is provided, normalize it to lowercase
        else:
            actual_sha1 = self._normalize_sha1(actual_sha1)

        # if the actual checksum does not match the expected checksum, raise an error
        if actual_sha1 != expected_sha1:
            raise ValueError(
                f'File "{src}" changed after it was added: '
                f"expected SHA-1 {expected_sha1}, got {actual_sha1}")
        # if the file already exists in the object store, return its path
        if stored.is_file():
            return stored

        # return the path to the stored file after copying it atomically
        return self._copy_atomically(src, stored, expected_sha1=expected_sha1)

    def restore(self, sha1: str, dest: Path) -> Path:
        """
        Restore a file from the object store to a specified destination.
        Args:
            sha1 (str): SHA-1 checksum of the object to restore.
            dest (Path): Destination path for the restored file.

        Returns:
            Path: Path to the restored file.

        Raises:
            FileNotFoundError: If the object does not exist in the store.
        """
        # split the SHA-1 checksum into its storage path
        stored = self._split_checksum(sha1)
        # if the file does not exist in the object store, raise an error
        if not stored.is_file():
            raise FileNotFoundError(
                f"object {sha1} not found in objects store")
        # otherwise, copy the stored file to the destination atomically
        return self._copy_atomically(stored, dest, expected_sha1=sha1)