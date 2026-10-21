"""
Catalog a remote folder as one source of a catalogue.

A scan lists the source's files. The catalogue's filename formats sort them:
each format keeps its files' rows in its own TSV, and files no format matches
become ``file:`` entries in ``config.yml``. Nothing is written unless the
scan completes, and the catalogue is saved only if no other command changed
it meanwhile.
"""

import os
from copy import deepcopy
from dataclasses import replace
from pathlib import PurePosixPath
from typing import NamedTuple
from urllib.parse import unquote, urlsplit, urlunsplit

import pandas as pd

from .discovery import discover_remote_files, path_matches
from .sources import (
    SourceSpec,
    derive_remote_name,
    derive_source_name,
    match_remote,
    materialize_sources,
    normalize_sources,
    validate_source_name,
)
from ..fmt_detection import KNOWN_STATIC_FILE_STEMS
from ..utils import (
    as_list_of_dicts, infer_checksum_algorithm, is_valid_checksum,
    resolve_path_in_root)
from ..repo.catalog import (
    ENTRY, SECTION, SOURCE, TABLE, catalog_rows, entry_row, row_entry,
    table_names)
from ..repo.config import (
    LOCAL_RESERVED_FIELDS, check_pattern_fields, filename_fields,
    fmt_entries_from_config, literal_fields, match_file, normalize_remotes,
    remote_name_of, validate_tsv_filename)
from ..repo.dothm import data_tsv_text
from ..repo.state import DEFAULT_DB, cell_text, is_remote_catalog
from ..transport import OperationContext, RemoteSpec
from ..transport.base import (
    DownloadError, RemoteConfigurationError, copy_backend_options,
    reject_url_secrets)

__all__ = ["add_remote", "is_remote_catalog", "split_remote_pattern"]

# Columns Hallmark writes for every remote catalogue row; pattern fields with
# these names are not stored.
CATALOG_COLUMNS = ["path", "checksum_algorithm", "checksum", "size_bytes", "mtime",
                   "checksum_source", SOURCE]
_RESERVED_FIELDS = {*CATALOG_COLUMNS, *LOCAL_RESERVED_FIELDS}
# A kept sha1 must describe the same server file as the new row.
_SAME_FILE = ("size_bytes", "mtime", "checksum_algorithm", "checksum")


_WEBDAV_SCHEMES = {"webdav": "http", "webdavs": "https"}


def _expand_webdav_scheme(value, backend):
    """Rewrite ``webdav(s)://`` to ``http(s)://`` with the webdav backend."""
    scheme, separator, rest = value.partition("://")
    mapped = _WEBDAV_SCHEMES.get(scheme.lower()) if separator else None
    if mapped is None:
        return value, backend
    if backend not in (None, "webdav"):
        raise ValueError(f"{scheme}:// URLs use the webdav backend, not {backend}")
    return f"{mapped}://{rest}", "webdav"


def split_remote_pattern(value, fmt=None):
    """
    Split a remote URL pattern into the folder URL and format entries.

    Args:
        value (str): Folder URL, optionally ending in a filename pattern.
        fmt (str | list, optional): Format given with ``--fmt``: a string, or
            a list of strings or ``{"fmt", "db"}`` entries.

    Returns:
        tuple[str, list[dict]]: The folder URL and its ``{"fmt", ...}``
        entries, relative to that folder.

    Raises:
        ValueError: For an unsupported URL, a URL holding secrets, a pattern
            given both ways, or an invalid format.
    """
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http", "ssh", "sftp", "s3"}:
        raise ValueError("Remote add requires an HTTP(S), WebDAV, S3, SSH or SFTP URL")
    reject_url_secrets(value)
    path = parts.path
    if "{" in path:
        if fmt:
            raise ValueError("Use a pattern in the URL or --fmt, not both")
        # The pattern starts after the last folder before its first field.
        boundary = path.rfind("/", 0, path.index("{")) + 1
        fmt = [unquote(path[boundary:])]
        path = path[:boundary]
    fmt_entries = [entry if isinstance(entry, dict) else {"fmt": entry}
                   for entry in ([fmt] if isinstance(fmt, str) else fmt or [])]
    for entry in fmt_entries:
        path_matches("validation", fmt=entry.get("fmt"))
    return urlunsplit(parts._replace(path=path)), fmt_entries


def _validate_db_names(fmt_entries):
    """
    Give each format entry its TSV.

    A lone format without a ``db`` keeps its rows in ``data.tsv``; with
    several, a format without one gets a TSV named after itself, with ``/``
    replaced, since a TSV name is a single file name.

    Args:
        fmt_entries (list[dict]): Format entries, configured ones first.

    Returns:
        list[dict]: The entries with a non-empty format, each with its ``db``.
    """
    entries = [entry for entry in fmt_entries
               if isinstance(entry.get("fmt"), str) and entry["fmt"].strip()]
    validated = []
    for entry in entries:
        db = entry.get("db")
        if not db and len(entries) == 1:
            db = DEFAULT_DB
        elif not db:
            db = entry["fmt"].strip().replace("/", "_")
        validated.append({**entry, "db": validate_tsv_filename(db)})
    return validated


def _describe_selection(filter):
    """Describe a remote selection for messages about the files it matched."""
    if not filter:
        return "any file"
    patterns = [filter] if isinstance(filter, str) else list(filter)
    return "filter " + ", ".join(repr(pattern) for pattern in patterns)


def _listed(paths, limit=10):
    paths = sorted(paths)
    shown = "\n".join(f"  {path}" for path in paths[:limit])
    more = len(paths) - limit
    return shown + (f"\n  ... and {more} more" if more > 0 else "")


def _choose_remote(remotes, folder, name):
    """
    Return the remote entry and folder root for a URL, adding a new remote.

    An explicit name selects or creates that remote; otherwise the deepest
    configured remote containing the URL is used.
    """
    names = [remote_name_of(remote) for remote in remotes]
    if name is not None:
        if name in names:
            entry = remotes[names.index(name)]
            matched = match_remote(folder, [{**entry, "name": name}])
            if matched is None:
                raise ValueError(
                    f"The URL is not inside remote {name!r}; choose another "
                    "--remote name to add a new remote")
            return entry, matched[1]
        validate_source_name(name)
    else:
        matched = match_remote(folder, remotes)
        if matched is not None:
            return remotes[names.index(matched[0])], matched[1]
        name = derive_remote_name(folder, names)
    entry = {"name": name, "url": folder.url}
    remotes.append(entry)
    return entry, ""


def _choose_source(sources, remote, root, filter, name):
    """Return the existing source a selection updates, or None for a new one."""
    if name is not None:
        validate_source_name(name)
        current = next((source for source in sources if source.name == name), None)
        if current is not None and (current.remote, current.root) != (remote, root):
            raise ValueError(
                f"Source {name!r} catalogs another folder; choose another "
                "--source name for this URL")
        return current
    here = [source for source in sources
            if (source.remote, source.root) == (remote, root)]
    if not filter:
        if len(here) > 1:
            raise ValueError(
                "Several sources catalog this folder ("
                + ", ".join(source.name for source in here)
                + "); choose one with --source")
        return here[0] if here else None
    return next((source for source in here if source.selects_like(filter)), None)


def _check_selection_keeps_rows(source, rows, filter):
    """Refuse changed filters that would drop catalogued files."""
    dropped = [row["path"] for row in rows
               if not path_matches(source.remote_path(row["path"])[len(source.root):],
                                   filter=filter)]
    if dropped:
        raise ValueError(
            f"{len(dropped)} catalogued file(s) of source {source.name!r} do not "
            f"fit {_describe_selection(filter)}:\n{_listed(dropped, 3)}\n"
            "Use filters that still match them, or a new branch.")


class PathClash(NamedTuple):
    """
    A path a new selection shares with a file another source catalogs.

    Attributes:
        path (str): Catalogue path.
        source (str): Source that catalogs it.
        content (str): ``"same"`` when checksums, or the server path, show
            the same file; ``"different"`` when a checksum or the size
            differs; ``"unknown"`` when nothing comparable is recorded.
    """

    path: str
    source: str
    content: str

    def label(self) -> str:
        """Describe the clash in one line."""
        content = {"same": "same content", "different": "different content",
                   "unknown": "cannot compare"}[self.content]
        return f"{self.path} ({content}; source {self.source!r})"


class Duplicates(NamedTuple):
    """
    Paths a new selection shares with other sources, for ``on_duplicate``.

    Attributes:
        source (str): Name of the source being added or updated.
        clashes (tuple[PathClash, ...]): The shared paths.
        can_prefix (bool): Whether the source can still take a prefix; an
            existing source keeps its own.
    """

    source: str
    clashes: tuple
    can_prefix: bool

    def describe(self) -> str:
        """List the shared paths and how their content compares."""
        return _clash_listing(set(), list(self.clashes))


def _digests(row):
    """Return the digests a catalogue row records, by algorithm."""
    found = {}
    checksum = cell_text(row.get("checksum")).lower()
    algorithm = cell_text(row.get("checksum_algorithm")).lower()
    if checksum and algorithm in ("", "unknown"):
        algorithm = infer_checksum_algorithm(checksum) or ""
    if checksum and algorithm and is_valid_checksum(algorithm, checksum):
        found[algorithm] = checksum
    sha1 = cell_text(row.get("sha1")).lower()
    if sha1 and is_valid_checksum("sha1", sha1):
        found.setdefault("sha1", sha1)
    return found


def _compare_content(new, old, same_file):
    """Return ``"same"``, ``"different"`` or ``"unknown"`` for two rows."""
    if same_file:
        return "same"
    new_digests, old_digests = _digests(new), _digests(old)
    shared = set(new_digests) & set(old_digests)
    if any(new_digests[name] != old_digests[name] for name in shared):
        return "different"
    sizes = cell_text(new.get("size_bytes")), cell_text(old.get("size_bytes"))
    if all(sizes) and sizes[0] != sizes[1]:
        return "different"
    return "same" if shared else "unknown"


def _find_clashes(frame, others, selected, sources):
    """
    Return the new rows' paths that another source already catalogs.

    Returns:
        tuple[set, list[PathClash]]: Paths that would be a file where another
        source has a folder, or the reverse, and paths both catalog as files.
    """
    if others.empty or "path" not in others.columns:
        return set(), []
    owned = others.drop_duplicates("path").set_index("path")
    folders = {parent for path in owned.index for parent in _parents(path)}
    by_name = {source.name: source for source in sources}
    structural, duplicates = set(), []
    for record in frame.to_dict(orient="records"):
        path = record["path"]
        if path in owned.index:
            old = owned.loc[path]
            owner = by_name.get(cell_text(old.get(SOURCE)))
            # Overlapping patterns in one folder name the very same server file.
            same_file = (owner is not None and owner.remote == selected.remote
                         and owner.remote_path(path) == selected.remote_path(path))
            duplicates.append(PathClash(path, cell_text(old.get(SOURCE)),
                                        _compare_content(record, old, same_file)))
        elif path in folders or any(parent in owned.index
                                    for parent in _parents(path)):
            structural.add(path)
    return structural, sorted(duplicates)


def _clash_message(structural, duplicates, selected, sources):
    """Explain which paths clash and how to add the source anyway."""
    owners = {clash.source for clash in duplicates}
    place = (selected.remote, selected.root, selected.prefix)
    same = [source.name for source in sources if source.name in owners
            and (source.remote, source.root, source.prefix) == place]
    if same:
        hint = (f"To change the filters of source {same[0]!r}, add again with "
                f"--source {same[0]}")
    elif selected.prefix:
        hint = "Choose a different --prefix, or narrow it with --filter"
    else:
        hint = "Add this source with --prefix DIR, or narrow it with --filter"
    if duplicates and not structural:
        hint += ", or leave them out with --skip-duplicates"
    return f"{_clash_listing(structural, duplicates)}\n{hint}; previous catalogue kept."


def _clash_listing(structural, duplicates):
    """Count and list clashing paths, at most ten of them."""
    counts = {}
    for clash in duplicates:
        counts[clash.content] = counts.get(clash.content, 0) + 1
    kinds = ", ".join(
        f"{counts[content]} {label}" for content, label in (
            ("same", "with the same content"),
            ("different", "with different content"),
            ("unknown", "that cannot be compared")) if content in counts)
    lines = [clash.label() for clash in duplicates] + [
        f"{path} (a file where another source has a folder, or the reverse)"
        for path in sorted(structural)]
    shown = "\n".join(f"  {line}" for line in lines[:10])
    if len(lines) > 10:
        shown += f"\n  ... and {len(lines) - 10} more"
    return (f"{len(lines)} path(s) already belong to another source"
            + (f" ({kinds})" if kinds else "") + f":\n{shown}")


def _parents(path):
    parts = path.split("/")[:-1]
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def _saved_catalogue(repo):
    """Return the saved catalogue as comparable text: config, meta and every TSV."""
    saved = repo.dothm.load_state()
    return (repr(saved.config), repr(saved.meta), data_tsv_text(saved.data),
            {name: data_tsv_text(frame) for name, frame in saved.tables.items()})


def _same_file(before, record):
    """Return whether the server reports a catalogued file unchanged."""
    return all(cell_text(before.get(key)) == cell_text(record.get(key))
               for key in _SAME_FILE)


def _comparable(row, as_entry):
    """
    Return what a row records about its file, as an entry would when asked.

    A ``file:`` entry keeps a published SHA-1 where a downloaded one goes, so
    rows are compared with entries as entries.
    """
    return entry_row(row_entry(row)) if as_entry else row


def _carry_sha1(rows, previous, worktree):
    """
    Carry each file's known SHA-1 forward while the server reports it unchanged.

    Args:
        rows (list[dict]): New rows; each gets its ``sha1`` when known.
        previous (list[dict]): The source's rows before this scan.
        worktree (Path, optional): Worktree, to tell downloaded files.

    Returns:
        list[str]: Downloaded paths the server reports changed: their row had
        a SHA-1 or a file exists at them in the worktree.
    """
    old = {row["path"]: row for row in previous}
    changed = []
    for row in rows:
        before = old.get(row["path"])
        row.pop("sha1", None)
        if before is None:
            continue
        as_entry = SECTION in before or SECTION in row
        if _same_file(_comparable(before, as_entry), _comparable(row, as_entry)):
            if cell_text(before.get("sha1")):
                row["sha1"] = cell_text(before["sha1"])
        elif cell_text(before.get("sha1")) or _exists_in(worktree, row["path"]):
            changed.append(row["path"])
    return sorted(changed)


def _exists_in(worktree, path):
    if worktree is None:
        return False
    try:
        return os.path.lexists(resolve_path_in_root(worktree, path))
    except (OSError, ValueError):
        return False


def _case_clashes(new_paths, other_paths):
    """
    Return groups of paths that differ only in letter case.

    On macOS and Windows such paths name one file. Only groups including a
    new path are returned, each sorted.
    """
    groups = {}
    for path in {*new_paths, *other_paths}:
        groups.setdefault(path.casefold(), []).append(path)
    new = set(new_paths)
    return sorted(sorted(group) for group in groups.values()
                  if len(group) > 1 and new.intersection(group))


def _scanned_rows(entries, selected):
    """Return catalogue rows for the files a scan found, as ``selected`` places them."""
    return pd.DataFrame([
        {"path": selected.local_path(item.path),
         "checksum_algorithm": item.checksum_algorithm,
         "checksum": item.checksum, "size_bytes": item.size,
         "mtime": item.mtime, "checksum_source": item.checksum_source,
         SOURCE: selected.name}
        for item in entries])


def _settle_duplicates(entries, others, selected, sources, on_duplicate, *,
                       can_prefix):
    """
    Build the new rows, applying ``on_duplicate`` to paths other sources own.

    Returns:
        tuple: The rows, the source as finally placed, and skipped clashes.

    Raises:
        ValueError: If paths still clash, or nothing is left to add.
    """
    tried = {selected.prefix}
    while True:
        frame = _scanned_rows(entries, selected)
        structural, duplicates = _find_clashes(frame, others, selected, sources)
        if not (structural or duplicates):
            return frame, selected, []
        decision = on_duplicate
        if callable(decision) and not structural:
            decision = decision(Duplicates(selected.name, tuple(duplicates),
                                           can_prefix))
        if decision == "skip" and not structural:
            shared = {clash.path for clash in duplicates}
            frame = frame[~frame["path"].isin(shared)].reset_index(drop=True)
            if frame.empty:
                raise ValueError(
                    "Every selected file is already catalogued by another "
                    "source; previous catalogue kept")
            return frame, selected, duplicates
        if (isinstance(decision, tuple) and len(decision) == 2
                and decision[0] == "prefix"):
            if not can_prefix:
                raise ValueError(
                    f"Source {selected.name!r} keeps its prefix; "
                    "previous catalogue kept")
            selected = replace(selected, prefix=decision[1])
            if selected.prefix in tried:
                raise ValueError(
                    f"Prefix {selected.prefix!r} was already tried; "
                    "previous catalogue kept")
            tried.add(selected.prefix)
            continue
        raise ValueError(_clash_message(structural, duplicates, selected, sources))


def _plan_formats(config, new_entries, root):
    """
    Combine the catalogue's formats with new ones.

    New formats are relative to the added folder and are kept relative to
    the remote. When a second format joins a lone one kept in ``data.tsv``,
    the lone one gets a TSV named after itself.

    Returns:
        tuple: Every format entry with its ``db``, the new ones, and the TSV
        that ``data.tsv``'s rows move to, or None.
    """
    configured = fmt_entries_from_config(config)
    known = {entry.get("fmt") for entry in configured}
    added = []
    for entry in new_entries:
        entry = {**entry, "fmt": root + entry["fmt"]}
        if entry["fmt"] not in known:
            check_pattern_fields(entry["fmt"], _RESERVED_FIELDS)
            known.add(entry["fmt"])
            added.append(entry)
    renamed = (len(configured) == 1 and bool(added) and validate_tsv_filename(
        configured[0].get("db") or DEFAULT_DB) == DEFAULT_DB)
    if renamed:
        configured = [{key: value for key, value in configured[0].items()
                       if key != "db"}]
    entries = _validate_db_names([*configured, *added])
    return entries, added, entries[0]["db"] if renamed else None


def _sort_rows(rows, selected, fmt_entries, kept_tables):
    """
    Assign each new row to its format's TSV, or to a ``file:`` entry.

    Without formats every row goes to ``data.tsv``. Rows in ``kept_tables``
    stay in the TSV they were in. A file no format matches becomes an entry
    of ``meta`` when its name ends in ``meta``, else of ``data``.

    Args:
        rows (list[dict]): Scanned rows; each gains ``_table`` or
            ``_section`` and its format's fields.
        selected (SourceSpec): Source the rows belong to.
        fmt_entries (list[dict]): Format entries with their ``db``.
        kept_tables (dict): TSV of each path that keeps its place.
    """
    fmts = {entry["db"]: entry["fmt"] for entry in fmt_entries}
    for row in rows:
        remote_path = selected.remote_path(row["path"])
        table, fields = kept_tables.get(row["path"]), None
        if table is not None:
            fmt = fmts.get(table)
            fields = literal_fields(fmt, remote_path) if fmt else None
        elif not fmt_entries:
            table = DEFAULT_DB
        else:
            index, matched = match_file(remote_path, fmt_entries)
            if index is not None:
                table = fmt_entries[index]["db"]
                # Field values keep the text of the name, as for local files.
                fields = literal_fields(fmt_entries[index]["fmt"], remote_path) or {
                    key: str(value) for key, value in matched.items()}
        if table is None:
            stem = PurePosixPath(remote_path).stem.split(".")[-1].lower()
            row[SECTION] = "meta" if stem == "meta" else "data"
            if stem != "meta" and stem in KNOWN_STATIC_FILE_STEMS:
                row["_name"] = stem
            continue
        row[TABLE] = table
        row.update({key: value for key, value in (fields or {}).items()
                    if key not in {*CATALOG_COLUMNS, "sha1"}})


_RESORT = {True: "source", "source": "source", "all": "all",
           False: "unsorted", "unsorted": "unsorted"}


def _resort_choice(value):
    """Return ``"source"``, ``"all"`` or ``"unsorted"`` for a resort value."""
    if value is None:
        return None
    try:
        return _RESORT[value]
    except (KeyError, TypeError):
        raise ValueError(
            'resort must be True, False, "source", "all", "unsorted" or a '
            "callable") from None


def _sort_again(rows, sources, fmt_entries):
    """
    Sort other sources' files again with every format, by their paths.

    Their servers are not listed again, so each file keeps its recorded
    metadata, and a ``file:`` entry that stays one is kept as it was.
    """
    by_name = {source.name: source for source in sources}
    sorted_rows = []
    for row in rows:
        source = by_name.get(row.get(SOURCE))
        if source is None:
            sorted_rows.append(row)
            continue
        again = {key: value for key, value in row.items()
                 if key in {*CATALOG_COLUMNS, "sha1"}}
        _sort_rows([again], source, fmt_entries, {})
        sorted_rows.append(row if SECTION in again and SECTION in row else again)
    return sorted_rows


def _write_catalogue(state, rows, fmt_entries, sources):
    """
    Store catalogue rows in their TSVs and ``file:`` entries in ``config.yml``.

    Each TSV keeps its earlier columns, then the catalogue columns and its
    format's fields; ``sha1`` comes last. Entries found in ``config.yml``
    are written back as they were; ``file:`` entries name their source only
    when the catalogue has several.
    """
    config = state.config
    old_tables = {DEFAULT_DB: state.data, **state.tables}
    config["data"] = [*([{"db": entry["db"], "fmt": entry["fmt"]}
                         for entry in fmt_entries] or [{"db": DEFAULT_DB}])]
    names = table_names(config)
    fields = {entry["db"]: filename_fields(entry["fmt"]) for entry in fmt_entries}
    tables = {name: [] for name in names}
    sections = {"data": [], "meta": []}
    for row in rows:
        if TABLE in row:
            tables[row[TABLE]].append(row)
        else:
            entry = (dict(row[ENTRY]) if ENTRY in row
                     else row_entry(row, name=row.get("_name")))
            if len(sources) == 1:
                entry.pop(SOURCE, None)
            elif row.get(SOURCE):
                entry[SOURCE] = row[SOURCE]
            sections[row[SECTION]].append(entry)
    frames = {}
    for name in names:
        # A table without rows, like a fresh repository's, has no layout to keep.
        before = old_tables.get(name, pd.DataFrame())
        earlier = [] if before.empty else list(before.columns)
        columns = list(dict.fromkeys([
            *earlier, *CATALOG_COLUMNS, *fields.get(name, []),
            *(key for row in tables[name] for key in row
              if not key.startswith("_"))]))
        if "sha1" in columns:
            columns = [column for column in columns if column != "sha1"] + ["sha1"]
        frames[name] = pd.DataFrame(
            [{key: row.get(key, "") for key in columns} for row in tables[name]],
            columns=columns).fillna("")
    config["data"] = [*sections["data"], *config["data"]]
    meta = as_list_of_dicts(config.get("meta")) or []
    kept_meta = [entry for entry in meta
                 if not (isinstance(entry, dict) and entry.get("file"))]
    if sections["meta"] or len(kept_meta) != len(meta):
        config["meta"] = [*kept_meta, *sections["meta"]]
        if not config["meta"]:
            del config["meta"]
    state.data = frames.pop(DEFAULT_DB)
    state.tables = frames


def _with_owners(rows, sources):
    """Give rows without a source the only source, as a lone source's entries are."""
    if len(sources) == 1:
        for row in rows:
            if not row.get(SOURCE):
                row[SOURCE] = sources[0].name
    return rows


def add_remote(repo, value, *, fmt=None, filter=None, auth=None, backend=None,
               backend_options=None, remote=None, source=None, prefix=None,
               server_checksum=None, on_duplicate=None, resort=None, prune=None,
               progress=False):
    """
    Catalog files from a remote folder as one source of the catalogue.

    The URL is matched to a configured remote, or becomes a new one. The
    selection updates the source with the same folder and filters (or the
    one named by ``source``), or adds a new source. Re-adding a source
    replaces its files, keeping a known ``sha1`` while the server reports the
    file unchanged; ``prune`` decides about files gone from the server.

    The catalogue's filename formats, with any new ones, sort the files:
    each format's files are rows of its TSV, and files no format matches
    become ``file:`` entries. Without formats every file is a row of
    ``data.tsv``. Nothing is written unless the scan completes and matches
    at least one file.

    Args:
        repo (Repo): Repository to update.
        value (str): Folder URL, optionally ending in a filename pattern.
        fmt (str | list, optional): Filename format, or a list of formats or
            ``{"fmt", "db"}`` entries, relative to the URL, if not part of it.
        filter (str | list[str], optional): Glob filters.
        auth, backend, backend_options: Settings for the remote; given values
            replace its saved ones.
        remote (str, optional): Remote name to use or create.
        source (str, optional): Source name to update or create.
        prefix (str, optional): Local folder for this source's files.
        server_checksum (str, optional): Have the server compute this
            algorithm for files without a published checksum; saved with the
            source and reused when it is added again.
        on_duplicate (str | callable, optional): What to do with paths that
            another source already catalogs as files. None or ``"error"``
            refuses them; ``"skip"`` leaves them out of this source and keeps
            the catalogued rows. A callable receives a :class:`Duplicates`
            and returns ``"skip"``, ``"abort"``, or ``("prefix", DIR)`` to
            place a new source's files under DIR instead. The choice is not
            saved, so adding the source again meets the same paths.
        resort (bool | str | callable, optional): When a new format joins
            and files are already sorted, True (or ``"source"``) sorts this
            source's files again, ``"all"`` sorts every source's files again,
            and False (or ``"unsorted"``) keeps each in its TSV, sorting only
            the others. Other sources' files are sorted by their recorded
            paths, without listing their servers. A callable receives the
            number of this source's sorted files, and the number of other
            sources' when there are any, and returns the choice. Without a
            choice, sorted files make the add fail.
        prune (bool | callable, optional): When catalogued files of this
            source are gone from the server, True removes them from the
            catalogue and False keeps their rows. A callable receives their
            paths and returns the choice. Without a choice the add fails, so
            no file leaves the catalogue unnoticed.
        progress: Discovery progress callback or flag.

    Returns:
        pandas.DataFrame: The files the server listed for this source.
        ``attrs["removed"]`` lists catalogued paths that the server no longer
        has and that were removed, ``attrs["kept_missing"]`` those that were
        kept, ``attrs["changed"]`` downloaded paths whose file it reports
        changed, ``attrs["skipped"]`` the :class:`PathClash` of each path
        left out, and ``attrs["case_clashes"]`` groups of catalogue paths
        that differ only in letter case.
    """
    if not (on_duplicate in (None, "error", "skip") or callable(on_duplicate)):
        raise ValueError('on_duplicate must be "error", "skip" or a callable')
    if not callable(resort):
        _resort_choice(resort)
    if not (callable(prune) or prune in (None, True, False)):
        raise ValueError("prune must be True, False or a callable")
    value, backend = _expand_webdav_scheme(value, backend)
    url, new_fmts = split_remote_pattern(value, fmt)
    path_matches("validation", filter=filter)
    specs = as_list_of_dicts(repo.state.config.get("data", []))
    if specs is None or any(not isinstance(spec, dict) for spec in specs):
        raise ValueError("Catalog data must be a mapping or list of mappings")
    if not repo.state.data.empty and not is_remote_catalog(repo.state):
        raise ValueError(
            "Use a separate repository for local files and remote catalogs")

    # What was saved when the scan began; compared again before saving.
    scanned_from = _saved_catalogue(repo)
    state = deepcopy(repo.state)
    materialize_sources(state)
    remotes = normalize_remotes(state.config.get("remote"))
    folder = RemoteSpec.from_url(url)
    entry, root = _choose_remote(remotes, folder, remote)
    remote_name = entry.setdefault("name", "origin")
    if auth is not None:
        entry["auth"] = auth
    if backend is not None:
        entry["backend"] = backend
    if backend_options is not None:
        entry["backend_options"] = copy_backend_options(backend_options)
    server = RemoteSpec.from_url(entry["url"], entry.get("auth"),
                                 backend=entry.get("backend"),
                                 backend_options=entry.get("backend_options"))
    entry["backend"] = server.backend

    sources = normalize_sources(state.config.get("sources"))
    current = _choose_source(sources, remote_name, root, filter, source)
    if current is not None:
        if prefix is not None and SourceSpec(
                current.name, remote_name, prefix=prefix).prefix != current.prefix:
            raise ValueError(
                f"Source {current.name!r} uses prefix {current.prefix!r}; "
                "a source keeps its prefix")
        if not filter:
            filter = list(current.filter) or None
        if server_checksum is None:
            server_checksum = current.server_checksum
        name, prefix = current.name, current.prefix
    else:
        name = source or derive_source_name(
            remote_name, root, [item.name for item in sources])
    selected = SourceSpec(name, remote_name, root, prefix or "", filter,
                          server_checksum or None)
    fmt_entries, added, moved_to = _plan_formats(state.config, new_fmts, root)
    everything = _with_owners(catalog_rows(state), sources)
    if moved_to is not None:
        # The lone format's rows follow it out of data.tsv.
        for row in everything:
            if row.get(TABLE) == DEFAULT_DB:
                row[TABLE] = moved_to
    mine = [row for row in everything
            if current is not None and row.get(SOURCE) == current.name]
    others = [row for row in everything
              if current is None or row.get(SOURCE) != current.name]
    if current is not None and not current.selects_like(filter):
        _check_selection_keeps_rows(selected, mine, filter)

    try:
        with OperationContext(server.subpath(root)) as context:
            entries = discover_remote_files(
                context, filter=filter, progress=progress,
                server_checksum=selected.server_checksum,
            )
    except RemoteConfigurationError:
        raise
    except DownloadError as exc:
        # Nothing is saved until the scan completes, so the catalogue is intact.
        # Keep the error's type, such as CapabilityError, for callers.
        raise type(exc)(
            f"Remote scan failed (previous catalogue kept): {exc}") from exc
    if not entries:
        raise ValueError(
            f"No remote files matched {_describe_selection(filter)} under "
            f"{url}; previous catalogue kept")

    frame, selected, skipped = _settle_duplicates(
        entries, pd.DataFrame(others, columns=list(dict.fromkeys(
            ["path", SOURCE, *(key for row in others for key in row)]))),
        selected, sources, on_duplicate, can_prefix=current is None)
    sorted_before = {row["path"]: row[TABLE] for row in mine if TABLE in row}
    owners = {source.name for source in sources}
    sorted_elsewhere = sum(TABLE in row and row.get(SOURCE) in owners
                           for row in others)
    choice = None
    if added and (sorted_before or sorted_elsewhere):
        if callable(resort):
            resort = (resort(len(sorted_before), sorted_elsewhere) if sorted_elsewhere
                      else resort(len(sorted_before)))
        choice = _resort_choice(resort)
        if choice is None:
            raise ValueError(
                f"{len(sorted_before)} files are already sorted"
                + (f", and {sorted_elsewhere} of other sources"
                   if sorted_elsewhere else "")
                + "; pass resort=True to sort them again with the new fmt, "
                + ('resort="all" to sort every source\'s files again, '
                   if sorted_elsewhere else "")
                + "or resort=False to only sort unsorted files")
    rows = frame.to_dict(orient="records")
    _sort_rows(rows, selected, fmt_entries,
               sorted_before if choice == "unsorted" else {})
    if choice == "all":
        others = _sort_again(others, sources, fmt_entries)
    missing = sorted({row["path"] for row in mine} - {row["path"] for row in rows})
    if missing:
        if callable(prune):
            prune = prune(missing)
        if prune not in (True, False):
            raise ValueError(
                f"{len(missing)} catalogued file(s) of source {selected.name!r} "
                f"are no longer on the server:\n{_listed(missing)}\nPass "
                "prune=True to remove them from the catalogue, or prune=False "
                "to keep them; previous catalogue kept")
    kept = [row for row in mine if row["path"] in missing] if not prune else []
    changed = _carry_sha1(rows, mine, getattr(repo, "worktree", None))
    if current is None:
        sources.append(selected)
    else:
        sources[sources.index(current)] = selected
    _write_catalogue(state, [*others, *rows, *kept], fmt_entries, sources)
    state.config["remote"] = remotes
    state.config["sources"] = [item.to_config() for item in sources]
    with repo.dothm.state_lock():
        # A scan can take minutes; never save over a change made meanwhile.
        if _saved_catalogue(repo) != scanned_from:
            raise ValueError(
                "The catalogue changed while the server was scanned "
                "(previous catalogue kept); run hm add again")
        repo.dothm.save_state(state)
    repo.state = state
    result = pd.DataFrame([{key: value for key, value in row.items()
                            if not key.startswith("_") and key != "sha1"}
                           for row in rows])
    result.attrs["skipped"] = skipped
    result.attrs["removed"] = missing if prune else []
    result.attrs["kept_missing"] = missing if not prune else []
    result.attrs["changed"] = changed
    result.attrs["case_clashes"] = _case_clashes(
        [row["path"] for row in rows], [row["path"] for row in others])
    return result
