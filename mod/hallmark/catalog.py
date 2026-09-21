"""Create local catalogs from Git repositories and remote data directories."""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from shutil import rmtree
from urllib.parse import urlsplit

import pandas as pd
import parse
import yaml

from .discovery import discover, path_matches
from .dothm import Dothm
from .error import CloneError, DestinationExistsError
from .helper_functions import as_list_of_dicts
from .repo_config import fmt_fields, normalize_remotes, normalize_tsv_name, row_to_path
from .transport import OperationContext, RemoteSpec
from .transport.base import RemoteObjectMissing, literal_path
from .worktree import Worktree


def _catalog_names(config):
    """Validate catalog filenames before reading any remote metadata."""
    entries = as_list_of_dicts(config.get("data", []))
    if entries is None or any(not isinstance(entry, dict) for entry in entries):
        raise CloneError("Catalog data must be a mapping or list of mappings")
    names = {"data.tsv"}
    for entry in entries:
        if entry.get("db"):
            names.add(normalize_tsv_name(entry["db"]))
        if entry.get("file"):
            literal_path(entry["file"])
    normalize_remotes(config.get("remote"))
    return sorted(names)


def _catalog_formats(config, name):
    """Return the filename formats associated with a catalog TSV."""
    entries = as_list_of_dicts(config.get("data", [])) or []
    return [entry["fmt"] for entry in entries if entry.get("fmt")
            and normalize_tsv_name(entry.get("db", "data.tsv")) == name]


def _row_path(row, formats):
    """Resolve a catalog row to one literal relative path."""
    value = row.get("path")
    if value is not None and not pd.isna(value) and str(value):
        return literal_path(str(value)).as_posix()
    paths = []
    for template in formats:
        try:
            paths.append(row_to_path(row, template).as_posix())
        except (ValueError, KeyError):
            continue
    if len(set(paths)) != 1:
        raise CloneError("Cannot resolve a catalog row to a unique file path")
    return paths[0]


def _validate_snapshot(files, config):
    """Validate catalog tables and paths before writing the snapshot."""
    for name in _catalog_names(config):
        try:
            frame = pd.read_csv(StringIO(files[name]), sep="\t", dtype=str,
                                keep_default_na=False)
        except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            raise CloneError(f"Invalid published catalog table: {name}") from exc
        formats = _catalog_formats(config, name)
        has_fields = any(fmt_fields(fmt) and set(fmt_fields(fmt)) <= set(frame.columns)
                         for fmt in formats)
        if not ({"path", "sha1"} & set(frame.columns) or has_fields):
            raise CloneError(f"Unrecognized published catalog columns: {name}")
        for _, row in frame.iterrows():
            _row_path(row, formats)


def _snapshot(context):
    """
    Read and validate a published catalog without fetching dataset files.

    Return None when required catalog files are absent or ``config.yml``
    does not describe a catalog. Invalid tables and metadata raise CloneError.
    """
    try:
        config_text = context.read_text("config.yml")
    except RemoteObjectMissing:
        return None
    try:
        config = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return None
    if not isinstance(config, dict) or "data" not in config:
        return None
    try:
        meta_text = context.read_text("meta.yml")
        data_text = context.read_text("data.tsv")
    except RemoteObjectMissing:
        return None
    try:
        meta = yaml.safe_load(meta_text)
    except yaml.YAMLError as exc:
        raise CloneError("Invalid published catalog metadata") from exc
    if not isinstance(meta, dict):
        raise CloneError("Published catalog meta.yml must be a mapping")
    files = {"config.yml": config_text, "meta.yml": meta_text,
             "data.tsv": data_text}
    for name in _catalog_names(config):
        if name not in files:
            files[name] = context.read_text(name)
    _validate_snapshot(files, config)
    return files


def _metadata_commit(repo, files, message):
    """Commit catalog metadata and reload the repository state."""
    repo.dothm.index.add(list(files))
    if repo.dothm.index.diff("HEAD"):
        repo.dothm.index.commit(message)
    repo.state = repo.dothm.load()


def _filter_catalog(repo, *, filter=None, fmt=None):
    """Filter catalog tables and file entries, then commit the selection."""
    if filter is None and fmt is None:
        return
    config = repo.state.config
    names = _catalog_names(config)
    for name in names:
        frame = repo.dothm.load_tsv(name)
        formats = _catalog_formats(config, name)
        keep = []
        for _, row in frame.iterrows():
            path = _row_path(row, formats)
            keep.append(path_matches(path, filter=filter, fmt=fmt))
        repo.dothm.dump_tsv(frame.loc[keep], name)
    for section in ("data", "meta"):
        values = as_list_of_dicts(config.get(section, []))
        if values is None:
            continue
        config[section] = [entry for entry in values
                           if not entry.get("file") or path_matches(
                               entry["file"], filter=filter, fmt=fmt)]
    repo.dothm.dump_yml(config, "config")
    _metadata_commit(repo, [*names, "config.yml"], "Filter local catalog")


def _write_inventory(repo, source, entries, fmt):
    """Write discovered files and their source to a new catalog."""
    columns = ["path", "checksum_algorithm", "checksum", "size_bytes", "mtime"]
    rows = []
    for entry in entries:
        row = dict(zip(columns, (entry.path, entry.checksum_algorithm,
                                entry.checksum, entry.size, entry.mtime)))
        if fmt:
            matched = parse.parse(fmt, entry.path)
            if matched is not None:
                row.update({key: value for key, value in matched.named.items()
                            if key not in columns})
        rows.append(row)
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(columns=columns)
    data_spec = {"db": "data.tsv"}
    if fmt:
        data_spec["fmt"] = fmt
    remote = {"name": "origin", "url": source.url}
    if source.auth:
        remote["auth"] = source.auth
    repo.dothm.dump_yml({"data": [data_spec], "remote": [remote]}, "config")
    repo.dothm.dump_yml({"source": source.url}, "meta")
    repo.dothm.dump_tsv(frame, "data", na_rep="")
    _metadata_commit(repo, ["config.yml", "meta.yml", "data.tsv"],
                     "Discover remote catalog")


def _git_source(url, source_type):
    """Determine whether the selected source should be cloned with Git."""
    if source_type == "git":
        return True
    if source_type != "auto":
        return False
    parsed = urlsplit(url)
    return (not parsed.scheme or parsed.scheme in {"git", "file"}
            or bool(re.match(r"^[^/]+@[^/]+:", url))
            or (parsed.scheme != "sftp" and parsed.path.rstrip("/").endswith(".git")))


def clone_catalog(cls, url, path, *, auth=None, filter=None, fmt=None,
                  source_type="auto", progress=False):
    """
    Create a local catalog from Git, a published snapshot, or a data directory.

    An incomplete destination created by this call is removed on failure.
    Existing destinations are rejected before any source access.

    Args:
        cls: Repository class used to initialize or open the result.
        url (str): Catalog location or URL of the dataset directory.
        path (Path | str): New repository destination.
        auth (str, optional): Local SSH profile for data access.
        filter (str | list[str], optional): Relative path glob or globs.
        fmt (str, optional): Filename format used to select paths.
        source_type (str): ``auto``, ``git``, ``directory``, or ``catalog``.
            Defaults to automatic detection.
        progress (bool | callable): Discovery progress display or callback.
            Defaults to False.

    Returns:
        Repo: Repository containing catalog metadata without dataset files.

    Raises:
        DestinationExistsError: If the destination already exists.
        CloneError: If a requested catalog is missing or invalid.
        ValueError: If source options are invalid.
        DownloadError: If remote metadata cannot be read or validated.
    """
    if source_type not in {"auto", "git", "directory", "catalog"}:
        raise ValueError("source_type must be auto, git, directory, or catalog")
    url = str(url)
    path_matches("validation", filter=filter, fmt=fmt)
    destination = Path(path).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise DestinationExistsError(
            f"fatal: destination path '{path}' already exists and is not empty.")
    is_git = _git_source(url, source_type)
    if is_git and auth is not None:
        raise ValueError("Git cloning uses Git/SSH authentication, not auth profiles")
    # Create the destination exclusively so cleanup only removes our own directory.
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise DestinationExistsError(f"Destination already exists: {path}") from exc
    try:
        if is_git:
            dothm_path, worktree_path = cls.lwpaths(destination)
            local = Path(url).expanduser()
            git_url = str(local / ".hm") if (local / ".hm").is_dir() else url
            Dothm.clone(git_url, dothm_path, display_path=path)
            if worktree_path:
                Worktree.init(worktree_path)
            repo = cls(destination)
            _filter_catalog(repo, filter=filter, fmt=fmt)
            return repo

        source = RemoteSpec.parse(url, auth)
        snapshot = None
        with OperationContext(source) as context:
            if source_type != "directory":
                snapshot = _snapshot(context)
                if snapshot is None and not source.root.rstrip("/").endswith(".hm"):
                    nested = RemoteSpec.parse(url.rstrip("/") + "/.hm/", auth)
                    with OperationContext(nested) as nested_context:
                        snapshot = _snapshot(nested_context)
            if snapshot is None:
                if source_type == "catalog":
                    raise CloneError("No published Hallmark catalog at this URL")
                entries = discover(context, filter=filter, fmt=fmt,
                                   progress=progress)
        repo = cls.init(destination)
        if snapshot is not None:
            for name, contents in snapshot.items():
                (repo.dothm.path / name).write_text(contents, encoding="utf-8")
            _metadata_commit(repo, snapshot, "Import published catalog snapshot")
            _filter_catalog(repo, filter=filter, fmt=fmt)
        else:
            _write_inventory(repo, source, entries, fmt)
        return repo
    except BaseException:
        rmtree(destination, ignore_errors=True)
        raise
