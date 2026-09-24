"""Clone existing Hallmark catalogs from Git or published snapshots."""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from shutil import rmtree
from urllib.parse import urlsplit

import pandas as pd
import yaml

from .dothm import Dothm
from .error import CloneError, DestinationExistsError
from .helper_functions import as_list_of_dicts
from .repo_config import fmt_fields, normalize_remotes, normalize_tsv_name
from .repo_manifest import row_relative_path
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
    try:
        return row_relative_path(row, formats).as_posix()
    except ValueError as exc:
        raise CloneError(str(exc)) from exc


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


def _clone_git(cls, url, destination, display_path):
    """Clone a catalog's Git history without modifying its tracked metadata."""
    dothm_path, worktree_path = cls.lwpaths(destination)
    local = Path(url).expanduser()
    git_url = str(local / ".hm") if (local / ".hm").is_dir() else url
    try:
        Dothm.clone(git_url, dothm_path, display_path=display_path)
    except CloneError as exc:
        raise CloneError(
            f"{exc}\nFor a raw dataset, use hallmark init PATH, then "
            "hallmark add 'URL/{field}...'.") from exc
    if worktree_path:
        Worktree.init(worktree_path)
    return cls(destination)


def clone_catalog(cls, url, path, *, source_type="auto"):
    """
    Clone an existing Git catalog or published HTTP/SFTP catalog snapshot.

    Existing destinations are rejected before source access. An incomplete
    destination created by this call is removed on failure. Raw datasets are
    cataloged with ``Repo.add(url_template)`` in a new repository.

    Args:
        cls: Repository class used to initialize or open the result.
        url (str): Existing Git catalog or published snapshot location.
        path (Path | str): New repository destination.
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise DestinationExistsError(f"Destination already exists: {path}") from exc
    try:
        if is_git:
            return _clone_git(cls, url, destination, path)
        source = RemoteSpec.parse(url)
        with OperationContext(source) as context:
            snapshot = _snapshot(context)
        if snapshot is None and not source.root.rstrip("/").endswith(".hm"):
            nested = RemoteSpec.parse(url.rstrip("/") + "/.hm/")
            with OperationContext(nested) as context:
                snapshot = _snapshot(context)
        if snapshot is None:
            if source_type == "auto" and source.scheme in {"http", "https"}:
                return _clone_git(cls, url, destination, path)
            raise CloneError("No published Hallmark catalog at this URL; for a "
                             "raw dataset, use hallmark init PATH, then "
                             "hallmark add 'URL/{field}...'")
        repo = cls.init(destination)
        for name, contents in snapshot.items():
            (repo.dothm.path / name).write_text(contents, encoding="utf-8")
        _metadata_commit(repo, snapshot, "Import published catalog snapshot")
        return repo
    except BaseException:
        rmtree(destination, ignore_errors=True)
        raise
