from __future__ import annotations

import json

from pathlib import Path
from collections.abc import Iterator

import pandas as pd

from ..utils import string_or_none
from .config import filename_fields, row_to_path, single_data_format


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
    # The manifest table will have a "sha1" column followed by the extracted fields.
    columns = ["sha1", *fields]
    # If the ParaFrame is empty, return an empty DataFrame with the appropriate columns.
    if pf.empty:
        return pd.DataFrame(columns=columns)

    pf_columns = set(pf.columns)
    rows = []
    # convert each record in the ParaFrame to a dictionary and build the manifest rows
    for record in pf.to_dict(orient="records"):
        # Create a row dictionary with the "sha1" value and the extracted fields.
        row = {"sha1": record["sha1"]}
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
        # if the format is still None, return immediately
        if fmt is None:
            return
    # convert each record in the repository state to a dictionary
    for record in state.data.to_dict(orient="records"):
        # yield a tuple containing the relative file path and its SHA-1 checksum
        yield row_to_path(record, fmt), str(record["sha1"])


def file_versions_by_path(state, *, fmt: str | None = None) -> dict[str, str]:
    """
    Create a mapping from file paths to SHA-1 checksums.

    Args:
        state: Repository state.
        fmt (str | None): Optional filename format to use for parsing paths.
            If not provided, the format will be determined from the repository
            configuration.

    Returns:
        dict[str, str]: Dictionary mapping relative file paths to their
        corresponding SHA-1 checksums.
    """
    if "path" in state.data.columns and "sha1" not in state.data.columns:
        from ..transport.base import validate_remote_path

        return {validate_remote_path(record["path"]).as_posix():
                json.dumps({key: str(value) for key, value in record.items()
                            if str(value)}, sort_keys=True)
                for record in state.data.to_dict(orient="records")}
    # call iter_manifest_entries to get an iterator of (path, checksum) tuples
    return {path.as_posix(): checksum
            for path, checksum in iter_manifest_entries(state, fmt=fmt)}
