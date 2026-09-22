"""Initialize remote datasets and clone existing Hallmark catalogs."""

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
from .transport.base import (RemoteObjectMissing, literal_path,
                             thaw_backend_options)
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

    Return None when ``config.yml`` is absent. Once present, incomplete or
    malformed catalog metadata raises CloneError rather than triggering fallback.
    """
    try:
        config_text = context.read_text("config.yml")
    except RemoteObjectMissing:
        return None
    try:
        config = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise CloneError("Invalid published catalog config.yml") from exc
    if not isinstance(config, dict) or "data" not in config:
        raise CloneError("Published catalog config.yml must contain a data mapping")
    try:
        meta_text = context.read_text("meta.yml")
        data_text = context.read_text("data.tsv")
    except RemoteObjectMissing as exc:
        raise CloneError(
            "Published Hallmark catalog is missing required metadata") from exc
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
    if source.backend:
        remote["backend"] = source.backend
    if source.backend_options:
        remote["backend_options"] = thaw_backend_options(source.backend_options)
    repo.dothm.dump_yml({"data": [data_spec], "remote": [remote]}, "config")
    repo.dothm.dump_yml({"source": source.url}, "meta")
    repo.dothm.dump_tsv(frame, "data", na_rep="")
    _metadata_commit(repo, ["config.yml", "meta.yml", "data.tsv"],
                     "Discover remote catalog")


def initialize_remote(cls, path, url, *, backend=None, backend_options=None,
                      auth=None, filter=None, fmt=None, progress=False):
    """Discover a dataset and initialize its catalog without replacing local files."""
    destination = Path(path).expanduser().absolute()
    if destination.is_symlink():
        raise DestinationExistsError(f"Destination is a symbolic link: {path}")
    dothm_path, _ = cls.lwpaths(destination)
    if dothm_path.exists() or dothm_path.is_symlink():
        raise DestinationExistsError(
            f"Hallmark repository already exists: {dothm_path}")
    if destination.exists() and not destination.is_dir():
        raise NotADirectoryError(f"Destination is not a directory: {path}")
    path_matches("validation", filter=filter, fmt=fmt)
    source = RemoteSpec.parse(str(url), auth, backend=backend,
                              backend_options=backend_options)
    # Claim only the metadata directory. On failure, existing dataset files stay put.
    missing_parents = []
    parent = dothm_path.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    created_parents = []
    owns_metadata = False
    try:
        for parent in reversed(missing_parents):
            try:
                parent.mkdir()
                created_parents.append(parent)
            except FileExistsError:
                if not parent.is_dir():
                    raise
        try:
            dothm_path.mkdir()
            owns_metadata = True
        except FileExistsError as exc:
            raise DestinationExistsError(
                f"Hallmark repository already exists: {dothm_path}") from exc
        with OperationContext(source) as context:
            entries = discover(context, filter=filter, fmt=fmt, progress=progress)
        repo = cls.init(destination)
        _write_inventory(repo, source, entries, fmt)
        return repo
    except BaseException:
        if owns_metadata:
            rmtree(dothm_path, ignore_errors=True)
        for parent in reversed(created_parents):
            try:
                parent.rmdir()
            except OSError:
                pass
        raise


def _git_source(url, source_type):
    """Determine whether the selected source should be cloned with Git."""
    if source_type == "git":
        return True
    if source_type != "auto":
        return False
    parsed = urlsplit(url)
    scp_style = "://" not in url and re.match(r"^(?:[^/@:]+@)?[^/:]+:.+", url)
    return (not parsed.scheme or parsed.scheme in {"git", "file", "ssh"}
            or bool(scp_style)
            or (parsed.scheme != "sftp" and parsed.path.rstrip("/").endswith(".git")))


def _clone_git(cls, url, destination, display_path, auth):
    """Clone a catalog's Git history without modifying its tracked metadata."""
    if auth is not None:
        raise ValueError("Git cloning uses Git/SSH authentication, not auth profiles")
    dothm_path, worktree_path = cls.lwpaths(destination)
    local = Path(url).expanduser()
    git_url = str(local / ".hm") if (local / ".hm").is_dir() else url
    try:
        Dothm.clone(git_url, dothm_path, display_path=display_path)
    except CloneError as exc:
        raise CloneError(
            f"{exc}\nFor a raw dataset, use hallmark init PATH --from URL.") from exc
    if worktree_path:
        Worktree.init(worktree_path)
    return cls(destination)


def clone_catalog(cls, url, path, *, auth=None, source_type="auto"):
    """
    Clone an existing Git catalog or published HTTP/SFTP catalog snapshot.

    Existing destinations are rejected before source access. An incomplete
    destination created by this call is removed on failure. Dataset discovery
    belongs to ``Repo.init(from_url=...)``.

    Args:
        cls: Repository class used to initialize or open the result.
        url (str): Existing Git catalog or published snapshot location.
        path (Path | str): New repository destination.
        auth (str, optional): Local profile for snapshot metadata access.
        source_type (str): ``auto``, ``git``, or ``catalog``.

    Returns:
        Repo: Repository containing complete catalog metadata without payloads.

    Raises:
        DestinationExistsError: If the destination already exists.
        CloneError: If a requested catalog is missing or invalid.
        ValueError: If source options are invalid.
        DownloadError: If remote metadata cannot be read or validated.
    """
    if source_type not in {"auto", "git", "catalog"}:
        raise ValueError("source_type must be auto, git, or catalog")
    url = str(url)
    destination = Path(path).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise DestinationExistsError(
            f"fatal: destination path '{path}' already exists and is not empty.")
    is_git = _git_source(url, source_type)
    if is_git and auth is not None:
        raise ValueError("Git cloning uses Git/SSH authentication, not auth profiles")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise DestinationExistsError(f"Destination already exists: {path}") from exc
    try:
        if is_git:
            return _clone_git(cls, url, destination, path, auth)
        source = RemoteSpec.parse(url, auth)
        with OperationContext(source) as context:
            snapshot = _snapshot(context)
        if snapshot is None and not source.root.rstrip("/").endswith(".hm"):
            nested = RemoteSpec.parse(url.rstrip("/") + "/.hm/", auth)
            with OperationContext(nested) as context:
                snapshot = _snapshot(context)
        if snapshot is None:
            if source_type == "auto" and source.scheme in {"http", "https"}:
                return _clone_git(cls, url, destination, path, auth)
            raise CloneError("No published Hallmark catalog at this URL; "
                             "use hallmark init PATH --from URL for a raw dataset")
        repo = cls.init(destination)
        for name, contents in snapshot.items():
            (repo.dothm.path / name).write_text(contents, encoding="utf-8")
        _metadata_commit(repo, snapshot, "Import published catalog snapshot")
        return repo
    except BaseException:
        rmtree(destination, ignore_errors=True)
        raise
