"""Map catalogue rows to the files they describe and to those files' SHA-1s."""

from __future__ import annotations

import json

from pathlib import Path
from collections.abc import Iterator

import pandas as pd

from ..utils import string_or_none
from .catalog import ENTRY, catalog_rows
from .state import is_remote_catalog
from .config import (
    filename_fields,
    row_to_path,
    single_data_format,
    validate_relative_path)


def build_file_table(pf, fmt: str) -> pd.DataFrame:
    """
    Build a manifest table from a ``ParaFrame``. Raises RuntimeError
    if a file path cannot be parsed using ``fmt``.

    The returned table contains a ``sha1`` column together with the
    fields extracted from the configured filename format.

    Args:
        pf: ``ParaFrame`` containing indexed file paths.
        fmt (str): Filename format used to parse the paths.

    Returns:
        pandas.DataFrame: Manifest table containing ``sha1`` values and
        parsed filename fields.
    """
    fields = filename_fields(fmt)
    # The manifest table will have a column for checksum, path, and each field
    columns = ["sha1", "path", *fields]
    # If the ParaFrame is empty, return an empty DataFrame with the appropriate columns.
    if pf.empty:
        return pd.DataFrame(columns=columns)

    pf_columns = set(pf.columns)
    rows = []
    # convert each record in the ParaFrame to a dictionary and build the manifest rows
    for record in pf.to_dict(orient="records"):
        # validate and normalize the relative file path
        row = {"sha1": record["sha1"], "path": validate_relative_path(
                                        record["path"], label="data path").as_posix()}
        # Update the row with the extracted fields
        row.update({field: (
            # use string_or_none to handle None and NaN values
            string_or_none(record[field]) if field in pf_columns else None)
            for field in fields})
        rows.append(row)

    return pd.DataFrame(rows, columns=columns)


def iter_manifest_entries(
    state,
    *,
    fmt: str | None = None,
    ) -> Iterator[tuple[Path, str]]:
    """
    Iterate over the manifest entries in the repository state.

    Args:
        state: Repository state.
        fmt (str | None): Optional filename format to use for parsing paths.
            If not provided, the format will be determined from the repository
            configuration.

    Returns:
        Iterator[tuple[Path, str]]: An iterator over tuples containing the
        relative file path and its SHA-1 checksum.

    Yields:
        tuple[Path, str]: Tuples of the relative file path and its SHA-1 checksum.
    """
    # If the repository state has no data, return immediately
    if state.data.empty:
        return
    # If no format is provided, determine it from the repository configuration.
    if fmt is None:
        # get the single data format from the repository configuration
        fmt = single_data_format(state.config)
    # convert each record in the repository state to a dictionary
    for record in state.data.to_dict(orient="records"):
        if not fmt and not string_or_none(record.get("path")):
            continue
        # yield a tuple containing the relative file path and its SHA-1 checksum
        yield row_to_path(record, fmt), str(record["sha1"])


def file_versions_by_path(state, *, fmt: str | None = None) -> dict[str, str]:
    """
    Create a mapping from file paths to what identifies their content.

    For a local catalogue each path maps to its SHA-1 checksum. For a remote
    catalogue every catalogued file, from each TSV and from ``file:`` entries,
    maps to its row as JSON text with sorted keys and no empty values, so a
    change to any field counts as a change; ``fmt`` is then not used.

    Args:
        state (State): Repository state.
        fmt (str | None): Filename format that builds a row's path from its
            fields when the row has no ``path``. If not provided, the
            repository's single data format is used.

    Returns:
        dict[str, str]: Relative file paths mapped to their SHA-1 checksums,
        or for a remote catalogue to their rows as JSON.
    """
    if is_remote_catalog(state):
        from ..transport.base import validate_remote_path

        # Every catalogued file: rows of each TSV and file: entries.
        return {validate_remote_path(record["path"]).as_posix():
                json.dumps({key: str(value) for key, value in record.items()
                            if key != ENTRY and str(value)}, sort_keys=True)
                for record in catalog_rows(state)}
    # call iter_manifest_entries to get an iterator of (path, checksum) tuples
    return {path.as_posix(): checksum
            for path, checksum in iter_manifest_entries(state, fmt=fmt)}
