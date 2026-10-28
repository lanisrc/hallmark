"""
Read a catalogue as one set of files across its TSVs and ``file:`` entries.

Filename formats sort a remote catalogue's files: each ``{db, fmt}`` entry of
``config.yml``'s ``data`` section keeps its rows in its own TSV, ``data.tsv``
for a lone format, and files no format matches are listed as ``file:`` entries
in ``data``, or in ``meta`` for a meta file. Downloads, staged SHA-1s and
status cover all of them, so they read the catalogue through these helpers.
"""

from __future__ import annotations

from ..utils import CHECKSUM_ALGORITHMS_BY_STRENGTH, as_list_of_dicts
from .config import validate_tsv_filename
from .state import DEFAULT_DB, cell_text

# Sections of config.yml whose entries may name a single file.
FILE_SECTIONS = ("data", "meta")
# Keys that say where catalog_rows found a row, and the entry it came from;
# never format fields.
TABLE, SECTION, ENTRY = "_table", "_section", "_entry"
# Column, and file: entry key, naming the catalogue source a file came from.
# Not "source", which filename formats use for an observed object.
SOURCE = "catalog_source"


def table_names(config) -> list[str]:
    """
    Return the TSVs a configuration's ``data`` entries name, ``data.tsv`` first.

    An entry with a format or a ``db`` keeps rows; one without a ``db``
    uses ``data.tsv``. ``file:`` entries name no table.

    Args:
        config (dict): Repository configuration.

    Returns:
        list[str]: TSV file names, ``data.tsv`` first.

    Raises:
        ValueError: If an entry's ``db`` is not a valid TSV file name.
    """
    names = [DEFAULT_DB]
    entries = as_list_of_dicts(config.get("data")) if isinstance(config, dict) else None
    for entry in entries or []:
        if (isinstance(entry, dict) and not entry.get("file")
                and (entry.get("db") or entry.get("fmt"))):
            name = validate_tsv_filename(entry.get("db") or DEFAULT_DB)
            if name not in names:
                names.append(name)
    return names


def catalog_tables(state) -> dict:
    """Return every TSV of a state by name, ``data.tsv`` first."""
    return {DEFAULT_DB: state.data, **getattr(state, "tables", {})}


def file_entries(config) -> list[tuple[str, dict]]:
    """
    Return each ``file:`` entry of a configuration with its section.

    Args:
        config (dict): Repository configuration.

    Returns:
        list[tuple[str, dict]]: ``("data" | "meta", entry)`` pairs, in order.
    """
    found = []
    for section in FILE_SECTIONS:
        entries = as_list_of_dicts(config.get(section)) if isinstance(
            config, dict) else None
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("file"):
                found.append((section, entry))
    return found


def entry_row(entry: dict) -> dict:
    """
    Describe a ``file:`` entry as a catalogue row.

    The strongest checksum other than SHA-1 is the row's catalogued checksum;
    ``sha1`` is the SHA-1 of the content, published or recorded after a
    download, as the ``sha1`` column of a row is.
    """
    algorithm = checksum = ""
    for name in CHECKSUM_ALGORITHMS_BY_STRENGTH:
        if name != "sha1" and cell_text(entry.get(name)):
            algorithm, checksum = name, cell_text(entry[name])
            break
    return {"path": cell_text(entry.get("file")), "checksum_algorithm": algorithm,
            "checksum": checksum, "size_bytes": cell_text(entry.get("size_bytes")),
            "mtime": cell_text(entry.get("mtime")),
            SOURCE: cell_text(entry.get(SOURCE)),
            "sha1": cell_text(entry.get("sha1"))}


def row_entry(row: dict, *, name=None) -> dict:
    """
    Describe a catalogue row as a ``file:`` entry, leaving out empty values.

    Args:
        row (dict): Row with ``path`` and optional checksum, size, mtime,
            catalogue source and ``sha1``.
        name (str, optional): Name of a known file, such as ``readme``.
    """
    entry = {"name": name} if name else {}
    entry["file"] = cell_text(row.get("path"))
    algorithm = cell_text(row.get("checksum_algorithm")).lower()
    checksum = cell_text(row.get("checksum"))
    if algorithm in CHECKSUM_ALGORITHMS_BY_STRENGTH and checksum:
        entry[algorithm] = checksum
    for key in ("size_bytes", "mtime", SOURCE):
        text = cell_text(row.get(key))
        if text:
            entry[key] = int(text) if text.isdigit() else text
    if cell_text(row.get("sha1")):
        entry["sha1"] = cell_text(row["sha1"])
    return entry


def catalog_rows(state) -> list[dict]:
    """
    Return every catalogued file of a state as a row.

    Rows of each TSV come first, then ``file:`` entries. Each row records
    where it is kept: ``_table`` names its TSV, or ``_section`` its
    ``config.yml`` section, with the entry itself in ``_entry``.
    """
    rows = []
    for name, frame in catalog_tables(state).items():
        if "path" not in frame.columns:
            continue
        for record in frame.to_dict(orient="records"):
            rows.append({**{key: cell_text(value) for key, value in record.items()},
                         TABLE: name})
    for section, entry in file_entries(state.config):
        rows.append({**entry_row(entry), SECTION: section, ENTRY: entry})
    return rows
