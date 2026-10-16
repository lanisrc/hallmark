from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pandas as pd

from .discovery import discover_remote_files, path_matches
from ..fmt_detection import KNOWN_STATIC_FILE_STEMS
from ..utils import SUPPORTED_CHECKSUM_ALGORITHMS, as_list_of_dicts
from ..repo.config import (
    filename_fields, fmt_entries_from_config, match_file, normalize_remotes,
    validate_tsv_filename)
from ..transport import OperationContext, RemoteSpec
from ..transport.base import copy_backend_options


def is_remote_catalog(state):
    return "path" in state.data.columns and "sha1" not in state.data.columns


def split_remote_pattern(value, fmt=None):
    """
    Split a remote URL pattern into the base URL and format entries.

    Args:
        value (str): The remote URL pattern.
        fmt (str or list of dict, optional): Format entries specified via --fmt.

    Returns:
        tuple: A tuple containing the base URL and a list of format entries.
    """
    parts = urlsplit(value)
    # only support HTTP(S), SSH, and SFTP URLs
    if parts.scheme not in {"https", "http", "ssh", "sftp"}:
        raise ValueError("Remote add requires an HTTP(S), SSH or SFTP URL")
    path = parts.path
    if "{" in path:
        if fmt:
            raise ValueError("Use a pattern in the URL or --fmt, not both")
        # the boundary is the last slash before the first '{' in the path
        boundary = path.rfind("/", 0, path.index("{")) + 1
        # extract the format string from the path after the boundary
        fmt = [unquote(path[boundary:])]
        path = path[:boundary]
    # convert each format string into a dictionary with a "fmt" key
    fmt_entries = [entry if isinstance(entry, dict) else {"fmt": entry}
                   for entry in ([fmt] if isinstance(fmt, str) else fmt or [])]
    # validate each format entry by checking if it matches the expected pattern
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

    validated = []
    for entry in entries:
        db = entry.get("db")
        # a lone fmt without a db keeps its rows in data.tsv
        if not db and len(entries) == 1:
            db = "data.tsv"
        # with several fmts, a fmt without a db gets a TSV named after itself,
        # with "/" replaced since a TSV name is a single file name
        elif not db:
            db = entry["fmt"].strip().replace("/", "_")
        # check the name and add the .tsv suffix when it is missing
        validated.append({**entry, "db": validate_tsv_filename(db)})
    return validated


def add_remote(repo, value, *, fmt=None, filter=None, auth=None, backend=None,
               backend_options=None, progress=False, resort=None):
    """
    Add a remote dataset to the repository.
    This function handles the addition of remote datasets to the repository by:
    1. Parsing the remote URL and extracting format entries.
    2. Validating the format entries and database names.
    3. Normalizing existing remote configurations.
    4. Ensuring that local and remote catalogs are kept separate.
    5. Returning a DataFrame of discovered remote files.

    Args:
        repo (Repo): The repository instance.
        value (str): The remote URL pattern.
        fmt (str or list of dict, optional): Format entries specified via --fmt.
        filter (str, optional): Filter pattern for remote files.
        auth (str, optional): Authentication method for the remote.
        backend (str, optional): Backend type for the remote.
        backend_options (dict, optional): Backend-specific options.
        progress (bool, optional): Whether to show progress during discovery.

    Returns:
        pandas.DataFrame: A DataFrame containing the discovered remote files.
    """
    # Split the remote URL and extract format entries from the URL and --fmt option.
    url, fmt_entries = split_remote_pattern(value, fmt)
    # Validate the extracted format entries and ensure proper database names.
    path_matches("validation", filter=filter)
    # get the existing format specifications from the repository configuration.
    specs = as_list_of_dicts(repo.state.config.get("data", []))
    # Ensure that the format specifications are valid and properly structured.
    if specs is None or any(not isinstance(spec, dict) for spec in specs):
        raise ValueError("Catalog data must be a mapping or list of mappings")
    # the fmts already in config.yml, plus any new fmt given with --fmt or the URL
    configured = fmt_entries_from_config(repo.state.config)
    added = [entry for entry in fmt_entries
             if entry["fmt"] not in {spec.get("fmt") for spec in configured}]
    # a lone fmt kept in data.tsv gets a TSV named after itself once a second
    # fmt is added, and its rows move there
    renamed = (len(configured) == 1 and bool(added) and validate_tsv_filename(
        configured[0].get("db") or "data.tsv") == "data.tsv")
    if renamed:
        configured = [{key: value for key, value in configured[0].items()
                       if key != "db"}]
    # validate the database names in the format entries
    fmt_entries = _validate_db_names([*configured, *added])
    # normalize the remote configurations from the repository state
    remotes = normalize_remotes(repo.state.config.get("remote"))
    # if there is exactly one remote and it matches the URL, use its previous settings
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
        # discover remote files based on the provided context, filter, and progress
        entries = discover_remote_files(context, filter=filter, progress=progress)
    columns = ["path", "checksum_algorithm", "checksum", "size_bytes", "mtime"]
    # TSVs written by earlier adds hold the files that are already sorted
    existing_tables = {}
    if is_remote_catalog(repo.state):
        existing_tables["data.tsv"] = repo.state.data
        for spec in specs:
            if spec.get("fmt") and spec.get("db"):
                db = validate_tsv_filename(spec["db"])
                if db != "data.tsv" and (repo.dothm.path / db).is_file():
                    existing_tables[db] = repo.dothm.read_tsv(db)
    # the renamed fmt's rows move from data.tsv to its own TSV
    if renamed:
        existing_tables[fmt_entries[0]["db"]] = existing_tables.pop(
            "data.tsv", pd.DataFrame(columns=columns))
    sorted_paths = {path for frame in existing_tables.values()
                    if "path" in frame for path in frame["path"]}
    # with a new fmt and files already sorted, ask whether to sort those again
    if added and sorted_paths:
        if callable(resort):
            resort = resort(len(sorted_paths))
        if resort is None:
            raise ValueError(
                f"{len(sorted_paths)} files are already sorted; pass resort=True to "
                "sort them again with the new fmt, or resort=False to only sort "
                "unsorted files")
    # tables are determined by the format entries, defaulting to data.tsv
    table_entries = fmt_entries or [{"db": "data.tsv"}]
    # make rows for each fmt entry to be filled with parsed data from matching files
    rows_by_fmt: list[list[dict]] = [[] for _ in table_entries]
    # list of rows that could not be matched to any format, to be processed later
    unmatched_rows = []
    # iterate over each discovered remote file and match it against the format entries
    for entry in entries:
        # unless they are sorted again, files already sorted keep their place
        if added and not resort and entry.path in sorted_paths:
            continue
        row = dict(zip(columns, (entry.path, entry.checksum_algorithm,
                                entry.checksum, entry.size, entry.mtime)))
        i = 0
        if fmt_entries:
            # match the file path against the provided format entries
            i, result = match_file(entry.path, fmt_entries)
            if result is None:
                # if no format matched, add to the unmatched rows for later processing
                unmatched_rows.append(row)
                continue
            # update the row with additional fields extracted from the format entry
            row.update({key: value for key, value in result.items()
                        if key not in {*columns, "sha1"}})
        rows_by_fmt[i].append(row)
    # if no files were listed at all, return an empty DataFrame
    if not any(rows_by_fmt) and not unmatched_rows:
        return pd.DataFrame(columns=columns)

    # process unmatched paths to identify static files and the meta file
    static_file_entries = []
    meta_file_entries: list[dict] = []
    for row in unmatched_rows:
        rel_path = row["path"]
        path = Path(rel_path)
        stem_name = path.stem.split(".")[-1]

        # get the stem name of the file and check if it is a meta file
        is_meta_file = stem_name.lower() == "meta"
        # for each unmatched path, look up its checksum from the listing
        algo, checksum = row["checksum_algorithm"], row["checksum"]

        # create an entry for the static file with its relative path
        entry = {"file": rel_path}
        # if the file is not a meta file and its stem name a known static file
        if (not is_meta_file and stem_name.lower() in KNOWN_STATIC_FILE_STEMS):
            # add the stem name as the "name" in the entry for known static files
            entry = {"name": stem_name.lower(), **entry}

        # if a valid checksum algorithm and checksum are available, include them
        if algo is not None and checksum is not None:
            if algo in SUPPORTED_CHECKSUM_ALGORITHMS:
                entry[algo] = checksum

        # if the file is a meta file, store it separately
        if is_meta_file:
            meta_file_entries.append(entry)
        # otherwise, add it to the list of static file entries for the config.yml
        else:
            static_file_entries.append(entry)

    # file: and meta: entries from earlier adds stay, unless this listing found
    # their file again, in which case its new entry or fmt row replaces them
    listed = {entry.path for entry in entries}
    static_file_entries = [spec for spec in specs if spec.get("file")
                           and spec["file"] not in listed] + static_file_entries
    meta_file_entries = [spec for spec in as_list_of_dicts(
        repo.state.config.get("meta")) or [] if isinstance(spec, dict)
        and spec.get("file") and spec["file"] not in listed] + meta_file_entries

    # initialize dictionaries to group rows and fields by their target database
    rows_by_target_db: dict[str, list[dict]] = {}
    fields_by_target_db: dict[str, list[str]] = {}
    # iterate over each format entry and its corresponding rows
    for fmt_entry, rows in zip(table_entries, rows_by_fmt):
        db = fmt_entry["db"]
        # group the rows for each target database, extending existing rows if present
        rows_by_target_db.setdefault(db, []).extend(rows)
        # group the fields for each target database, ensuring no duplicates
        target_fields = fields_by_target_db.setdefault(db, [])
        # for each field extracted from the format string, add it to the target fields
        for field in filename_fields(fmt_entry.get("fmt", "")):
            # if the field is not already in the manifest columns, not a sha1,
            # and not in the target fields, add it
            if (field not in columns and field != "sha1"
                    and field not in target_fields):
                target_fields.append(field)

    state = deepcopy(repo.state)
    tables = {}
    for db, rows in rows_by_target_db.items():
        frame = pd.DataFrame(rows, columns=[*columns, *fields_by_target_db[db]])
        # rows from earlier adds stay in their TSV, refreshed by this listing
        existing = existing_tables.get(db, pd.DataFrame())
        # a file sorted again leaves the TSV it was in before
        if added and resort and not existing.empty:
            existing = existing[~existing["path"].isin(listed)]
        if not existing.empty:
            frame = pd.concat([existing, frame], ignore_index=True, sort=False)
            frame = frame.drop_duplicates(subset=["path"], keep="last")
        tables[db] = frame.fillna("").sort_values("path").reset_index(drop=True)
    # data.tsv stays an empty catalog when every fmt uses its own TSV
    state.data = tables.pop("data.tsv", existing_tables.get(
        "data.tsv", pd.DataFrame(columns=columns)))
    remote = {"name": "origin", "url": source.url, "backend": source.backend}
    if source.auth:
        remote["auth"] = source.auth
    if source.backend_options:
        remote["backend_options"] = copy_backend_options(source.backend_options)
    # config.yml: static files, then fmt entries, then remote/meta
    state.config["data"] = [*static_file_entries,
                            *([{"db": entry["db"], "fmt": entry["fmt"]}
                               for entry in fmt_entries] or [{"db": "data.tsv"}])]
    state.config["remote"] = [remote]
    # if a meta file entry exists, add it to the config
    if meta_file_entries:
        state.config["meta"] = meta_file_entries
    repo.dothm.save_state(state)
    # TSVs other than data.tsv are written and staged next to it
    for db, frame in tables.items():
        repo.dothm.write_tsv(frame, db)
    if tables:
        repo.dothm.index.add(list(tables))
    repo.state = repo.dothm.load_state()
    return pd.DataFrame([row for rows in rows_by_fmt for row in rows])
