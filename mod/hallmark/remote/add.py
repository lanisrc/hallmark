from copy import deepcopy
from urllib.parse import unquote, urlsplit, urlunsplit

import pandas as pd

from .discovery import discover_remote_files, path_matches
from ..utils import as_list_of_dicts
from ..repo.config import (
    filename_fields, fmt_entries_from_config, match_file, normalize_remotes,
    validate_tsv_filename)
from ..transport import OperationContext, RemoteSpec
from ..transport.base import copy_backend_options


def is_remote_catalog(state):
    return "path" in state.data.columns and "sha1" not in state.data.columns


def split_remote_pattern(value, fmt=None):
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http", "ssh", "sftp"}:
        raise ValueError("Remote add requires an HTTP(S), SSH or SFTP URL")
    path = parts.path
    if "{" in path:
        if fmt:
            raise ValueError("Use a pattern in the URL or --fmt, not both")
        boundary = path.rfind("/", 0, path.index("{")) + 1
        fmt = [unquote(path[boundary:])]
        path = path[:boundary]
    # each --fmt, or the pattern in the URL, is one fmt entry, like the build's FMT=DB
    fmt_entries = [entry if isinstance(entry, dict) else {"fmt": entry}
                   for entry in ([fmt] if isinstance(fmt, str) else fmt or [])]
    for entry in fmt_entries:
        path_matches("validation", fmt=entry["fmt"])
    return urlunsplit(parts._replace(path=path)), fmt_entries


def _validate_db_names(fmt_entries):
    """
    Used by add_remote.
    Validate the database names for the given format entries. If a format entry does
    not have an explicit db name, it assigns "data.tsv" for a single entry or a name
    derived from the format string for multiple entries.
    Args:
        fmt_entries (list of dict): List of format entries to validate.

    Returns:
        list of dict: List of validated format entries with proper db names.
    """
    # Filter out entries that do not have a valid non-empty fmt string
    entries = [entry for entry in fmt_entries
               if isinstance(entry.get("fmt"), str) and entry["fmt"].strip()]
    return [{**entry, "db": validate_tsv_filename(
                entry.get("db") or ("data.tsv" if len(entries) == 1
                                    else entry["fmt"].strip().replace("/", "_")))}
            for entry in entries]


def add_remote(repo, value, *, fmt=None, filter=None, auth=None, backend=None,
               backend_options=None, progress=False):
    url, fmt_entries = split_remote_pattern(value, fmt)
    path_matches("validation", filter=filter)
    specs = as_list_of_dicts(repo.state.config.get("data", []))
    if specs is None or any(not isinstance(spec, dict) for spec in specs):
        raise ValueError("Catalog data must be a mapping or list of mappings")
    # like build_repo: the fmts given with --fmt (or in the URL), otherwise the
    # fmts already in config.yml
    if not fmt_entries:
        fmt_entries = fmt_entries_from_config(repo.state.config)
    fmt_entries = _validate_db_names(fmt_entries)
    remotes = normalize_remotes(repo.state.config.get("remote"))
    if len(remotes) == 1 and remotes[0].get("url") == url:
        previous = remotes[0]
        if auth is None:
            auth = previous.get("auth")
        if backend is None:
            backend = previous.get("backend")
        if backend_options is None:
            backend_options = previous.get("backend_options")
    source = RemoteSpec.from_url(url, auth, backend=backend,
                              backend_options=backend_options)
    existing = repo.state.data
    if not existing.empty and not is_remote_catalog(repo.state):
        raise ValueError(
            "Use a separate repository for local files and remote catalogs")
    if not existing.empty and (len(remotes) != 1
                              or remotes[0].get("url") != source.url):
        raise ValueError(
            "Remote add currently supports one dataset root per repository")
    with OperationContext(source) as context:
        # every listed file is matched against the fmts below
        entries = discover_remote_files(context, filter=filter, progress=progress)
    columns = ["path", "checksum_algorithm", "checksum", "size_bytes", "mtime"]
    # without a fmt, every file is cataloged by its path in data.tsv
    table_entries = fmt_entries or [{"db": "data.tsv"}]
    # make rows for each fmt entry to be filled with parsed data from matching files
    rows_by_fmt: list[list[dict]] = [[] for _ in table_entries]
    for entry in entries:
        row = dict(zip(columns, (entry.path, entry.checksum_algorithm,
                                entry.checksum, entry.size, entry.mtime)))
        i = 0
        if fmt_entries:
            i, result = match_file(entry.path, fmt_entries)
            # files that match no fmt are left out of the catalog
            if result is None:
                continue
            row.update({key: value for key, value in result.items()
                        if key not in {*columns, "sha1"}})
        rows_by_fmt[i].append(row)
    if not any(rows_by_fmt):
        return pd.DataFrame(columns=columns)

    # initialize dictionaries to group rows and fields by their target database
    rows_by_target_db: dict[str, list[dict]] = {}
    fields_by_target_db: dict[str, list[str]] = {}
    for fmt_entry, rows in zip(table_entries, rows_by_fmt):
        db = fmt_entry["db"]
        # group the rows for each target database, extending existing rows if present
        rows_by_target_db.setdefault(db, []).extend(rows)
        # group the fields for each target database, ensuring no duplicates
        target_fields = fields_by_target_db.setdefault(db, [])
        # for each field extracted from the format string, add it to the target fields
        for field in filename_fields(fmt_entry.get("fmt", "")):
            # if the field not already in the manifest columns or target fields, add it;
            # a sha1 column would mark the catalog as local files, so it is skipped
            if (field not in columns and field != "sha1"
                    and field not in target_fields):
                target_fields.append(field)

    state = deepcopy(repo.state)
    tables = {}
    for db, rows in rows_by_target_db.items():
        frame = pd.DataFrame(rows, columns=[*columns, *fields_by_target_db[db]])
        # rows from earlier adds stay in their TSV, refreshed by this listing
        if db == "data.tsv":
            existing = state.data
        elif (repo.dothm.path / db).is_file():
            existing = repo.dothm.read_tsv(db)
        else:
            existing = pd.DataFrame()
        if not existing.empty:
            frame = pd.concat([existing, frame], ignore_index=True, sort=False)
            frame = frame.drop_duplicates(subset=["path"], keep="last")
        tables[db] = frame.fillna("").sort_values("path").reset_index(drop=True)
    # data.tsv stays an empty catalog when every fmt uses its own TSV
    state.data = tables.pop("data.tsv", state.data if is_remote_catalog(state)
                            else pd.DataFrame(columns=columns))
    remote = {"name": "origin", "url": source.url, "backend": source.backend}
    if source.auth:
        remote["auth"] = source.auth
    if source.backend_options:
        remote["backend_options"] = copy_backend_options(source.backend_options)
    state.config["data"] = [{"db": entry["db"], "fmt": entry["fmt"]}
                            for entry in fmt_entries] or [{"db": "data.tsv"}]
    state.config["remote"] = [remote]
    repo.dothm.save_state(state)
    # TSVs other than data.tsv are written and staged next to it
    for db, frame in tables.items():
        repo.dothm.write_tsv(frame, db)
    if tables:
        repo.dothm.index.add(list(tables))
    repo.state = repo.dothm.load_state()
    return pd.DataFrame([row for rows in rows_by_fmt for row in rows])
