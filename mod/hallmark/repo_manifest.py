from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator

import pandas as pd

from .helper_functions import safe_str
from .repo_config import CatalogEntry, catalog_entries, fmt_fields, row_to_path
from .transport.base import literal_path


def manifest_frame_from_pf(pf, fmt: str) -> pd.DataFrame:
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
    fields = fmt_fields(fmt)
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
            # use safe_str to handle None and NaN values
            safe_str(record[field]) if field in pf_columns else None)
            for field in fields})
        rows.append(row)

    return pd.DataFrame(rows, columns=columns)


@dataclass(frozen=True)
class CatalogRow:
    """
    One cataloged file.

    Attributes:
        path (str): POSIX path relative to the worktree and the data source.
        record (dict): The catalog row.
        db (str): TSV name of the catalog table.
        entries (tuple[CatalogEntry, ...]): Data entries sharing the table.
        local (bool): True when the file's content is tracked in the worktree
            and object store; False for catalog-only rows such as remote files.
    """

    path: str
    record: dict
    db: str
    entries: tuple
    local: bool


def _cell_text(value) -> str:
    """Return a catalog cell as text, treating missing markers as empty."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "nan", "<na>"} else text


def row_relative_path(row, formats) -> Path:
    """
    Resolve a catalog row to one literal relative path.

    A ``path`` column is authoritative. Otherwise exactly one template must
    render the row.

    Args:
        row: Catalog row mapping.
        formats (list[str]): Templates of the data entries sharing the table.

    Returns:
        Path: The relative file path.

    Raises:
        ValueError: If the row cannot be resolved to a unique safe path.
    """
    value = row.get("path")
    if _cell_text(value):
        return Path(literal_path(str(value)).as_posix())
    paths = set()
    for template in formats:
        try:
            paths.add(row_to_path(row, template).as_posix())
        except (ValueError, KeyError):
            continue
    if len(paths) != 1:
        raise ValueError("Cannot resolve a catalog row to a unique file path")
    return Path(paths.pop())


def table_groups(config) -> dict[str, tuple[CatalogEntry, ...]]:
    """
    Group catalog entries by the TSV that holds their rows.

    Args:
        config (dict): The repository configuration dictionary.

    Returns:
        dict[str, tuple[CatalogEntry, ...]]: Entries keyed by TSV name.
    """
    groups: dict[str, list[CatalogEntry]] = {}
    for entry in catalog_entries(config):
        groups.setdefault(entry.db, []).append(entry)
    return {name: tuple(entries) for name, entries in groups.items()}


def is_local_table(entries, frame: pd.DataFrame) -> bool:
    """
    Return True when a table's rows describe worktree files stored as objects.

    Remote entries, path catalogs, and builder tables shared by several
    templates are catalog-only: their files are not committed as objects,
    restored by checkout, or checked in the worktree.

    Args:
        entries (tuple[CatalogEntry, ...]): Data entries sharing the table.
        frame (pd.DataFrame): The catalog table.

    Returns:
        bool: True for a local table.
    """
    return (len(entries) == 1 and entries[0].fmt is not None
            and not entries[0].is_remote
            and "sha1" in frame.columns and "path" not in frame.columns)


def iter_catalog_rows(state, *, local: bool | None = None) -> Iterator[CatalogRow]:
    """
    Iterate over the files cataloged by every data entry.

    Args:
        state: Repository state.
        local (bool, optional): True for local rows only, False for
            catalog-only rows only, or None for all rows.

    Yields:
        CatalogRow: Each cataloged file.

    Raises:
        ValueError: If a row cannot be resolved to a safe relative path.
    """
    for name, entries in table_groups(state.config).items():
        frame = state.table(name)
        if frame.empty:
            continue
        local_table = is_local_table(entries, frame)
        if local is not None and local_table != local:
            continue
        formats = [entry.fmt for entry in entries if entry.fmt]
        for record in frame.to_dict(orient="records"):
            if local_table:
                path = row_to_path(record, formats[0])
            else:
                path = row_relative_path(record, formats)
            yield CatalogRow(path.as_posix(), record, name, entries, local_table)


def row_fingerprint(record) -> str:
    """
    Summarize the recorded content of a catalog row.

    The SHA-1 is preferred, then a published checksum, then size and
    modification time.

    Args:
        record: Catalog row mapping.

    Returns:
        str: A comparable fingerprint.
    """
    sha1 = _cell_text(record.get("sha1"))
    if sha1:
        return sha1.lower()
    checksum = _cell_text(record.get("checksum"))
    if checksum:
        algorithm = _cell_text(record.get("checksum_algorithm")) or "unknown"
        return f"{algorithm.lower()}:{checksum.lower()}"
    size = _cell_text(record.get("size_bytes"))
    mtime = _cell_text(record.get("mtime"))
    return f"size:{size};mtime:{mtime}"


def catalog_map(state) -> dict[str, CatalogRow]:
    """
    Map every cataloged path to its catalog row.

    Args:
        state: Repository state.

    Returns:
        dict[str, CatalogRow]: Rows keyed by relative POSIX path.
    """
    return {row.path: row for row in iter_catalog_rows(state)}


def iter_manifest_entries(
    state,
    *,
    fmt: str | None = None,
    ) -> Iterator[tuple[Path, str]]:
    """
    Iterate over the local manifest entries in the repository state.

    Args:
        state: Repository state.
        fmt (str | None): Optional filename format applied to ``data.tsv``.
            If not provided, the local catalog entries of the repository
            configuration are used.

    Returns:
        Iterator[tuple[Path, str]]: An iterator over tuples containing the
        relative file path and its SHA-1 checksum.

    Yields:
        tuple[Path, str]: Tuples of the relative file path and its SHA-1 checksum.
    """
    if fmt is None:
        for row in iter_catalog_rows(state, local=True):
            yield Path(row.path), str(row.record["sha1"])
        return
    # If the repository state has no data, return immediately
    if state.data.empty:
        return
    # convert each record in the repository state to a dictionary
    for record in state.data.to_dict(orient="records"):
        # yield a tuple containing the relative file path and its SHA-1 checksum
        yield row_to_path(record, fmt), str(record["sha1"])


def manifest_map(state, *, fmt: str | None = None) -> dict[str, str]:
    """
    Create a mapping from local file paths to SHA-1 checksums.

    Args:
        state: Repository state.
        fmt (str | None): Optional filename format applied to ``data.tsv``.
            If not provided, the local catalog entries of the repository
            configuration are used.

    Returns:
        dict[str, str]: Dictionary mapping relative file paths to their
        corresponding SHA-1 checksums.
    """
    # call iter_manifest_entries to get an iterator of (path, checksum) tuples
    return {path.as_posix(): checksum
            for path, checksum in iter_manifest_entries(state, fmt=fmt)}
