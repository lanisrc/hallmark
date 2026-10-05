"""Clone existing Hallmark catalogs."""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from shutil import rmtree
from urllib.parse import unquote, urlsplit

import pandas as pd
import yaml
from git import Repo as GitRepo
from git.exc import GitCommandError, GitError

from ..repo.dothm import Dothm
from ..error import CloneError, DestinationExistsError
from ..utils import as_list_of_dicts, load_yaml
from .download import _checksum_from_row, _validate_expected_checksum
from ..repo.config import (
    filename_fields,
    normalize_remotes,
    validate_tsv_filename,
    row_to_path,
)
from ..transport import OperationContext, RemoteSpec
from ..transport.base import (
    DownloadError, RemoteObjectMissing, reject_url_credentials,
    validate_remote_path)
from ..repo.worktree import Worktree
from ..repo.branches import validate_branch_name


def _catalog_filenames(config):
    """Validate catalog filenames before reading any remote metadata."""
    entries = as_list_of_dicts(config.get("data", []))
    if entries is None or any(not isinstance(entry, dict) for entry in entries):
        raise CloneError("Catalog data must be a mapping or list of mappings")
    names = {"data.tsv"}
    for entry in entries:
        if entry.get("db"):
            names.add(validate_tsv_filename(entry["db"]))
        if entry.get("file"):
            validate_remote_path(entry["file"])
    normalize_remotes(config.get("remote"))
    return sorted(names)


def _catalog_formats(config, name):
    """Return the filename formats associated with a catalog TSV."""
    entries = as_list_of_dicts(config.get("data", [])) or []
    return [entry["fmt"] for entry in entries if entry.get("fmt")
            and validate_tsv_filename(entry.get("db", "data.tsv")) == name]


def _resolve_catalog_path(row, formats):
    """Resolve a catalog row to one literal relative path."""
    value = row.get("path")
    if value is not None and not pd.isna(value) and str(value):
        return validate_remote_path(str(value)).as_posix()
    paths = []
    for template in formats:
        try:
            paths.append(row_to_path(row, template).as_posix())
        except (ValueError, KeyError):
            continue
    if len(set(paths)) != 1:
        raise CloneError("Cannot resolve a catalog row to a unique file path")
    return paths[0]


def _validate_catalog(files, config):
    """
    Validate a catalog's tables before it is written or kept.

    Every row must resolve to one safe relative path, and its checksum, if
    any, must use a supported algorithm and a well-formed digest.

    Args:
        files (dict[str, str]): Text of ``data.tsv`` and the other TSVs
            named by ``config``.
        config (dict): Parsed ``config.yml``.

    Raises:
        CloneError: If a table is missing, unreadable or invalid.
        ValueError: If a path or table name is unsafe.
    """
    for name in _catalog_filenames(config):
        if name not in files:
            raise CloneError(f"Catalog table is missing: {name}")
        try:
            frame = pd.read_csv(StringIO(files[name]), sep="\t", dtype=str,
                                keep_default_na=False)
        except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
            raise CloneError(f"Invalid catalog table: {name}") from exc
        formats = _catalog_formats(config, name)
        has_fields = False
        for fmt in formats:
            fields = filename_fields(fmt)
            if fields and set(fields) <= set(frame.columns):
                has_fields = True
                break
        if not ({"path", "sha1"} & set(frame.columns) or has_fields):
            raise CloneError(f"Unrecognized catalog columns: {name}")
        for _, row in frame.iterrows():
            path = _resolve_catalog_path(row, formats)
            try:
                _validate_expected_checksum(_checksum_from_row(row))
            except DownloadError as exc:
                raise CloneError(f"{name}: {path}: {exc}") from exc


def _validate_branch(git, branch):
    """
    Validate the committed catalog of one branch of a cloned repository.

    Raises:
        CloneError: If a state file is missing or the catalog is invalid.
    """
    def read(name):
        try:
            return git.show("--end-of-options", f"{branch}:{name}")
        except GitCommandError:
            raise CloneError(
                f"Branch '{branch}' is missing {name}") from None

    try:
        config = load_yaml(read("config.yml"))
        texts = {"meta.yml": read("meta.yml"), "data.tsv": read("data.tsv")}
        load_yaml(texts["meta.yml"])
    except (ValueError, yaml.YAMLError) as exc:
        detail = " ".join(str(exc).split())
        raise CloneError(
            f"Branch '{branch}' has an invalid config.yml or meta.yml: "
            f"{detail}") from exc
    try:
        for name in _catalog_filenames(config):
            texts.setdefault(name, read(name))
        _validate_catalog(texts, config)
    except (CloneError, ValueError) as exc:
        raise CloneError(
            f"Branch '{branch}' has an invalid catalog: {exc}") from exc


def _copy_branch_pointers(dothm):
    """
    Create a local branch for every branch copied from the source.

    Returns:
        list[str]: Names of all local branches.
    """
    local = {head.name for head in dothm.heads}
    for remote in dothm.remotes:
        for ref in remote.refs:
            if ref.remote_head != "HEAD" and ref.remote_head not in local:
                dothm.git.branch("--track", "--", ref.remote_head, ref.name)
                local.add(ref.remote_head)
    return sorted(local)


def _read_catalog_snapshot(context):
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
    for name in _catalog_filenames(config):
        if name not in files:
            files[name] = context.read_text(name)
    _validate_catalog(files, config)
    return files


def _commit_catalog_metadata(repo, files, message):
    """Commit catalog metadata and reload the repository state."""
    repo.dothm.index.add(list(files))
    if repo.dothm.index.diff("HEAD"):
        repo.dothm.index.commit(message)
    repo.state = repo.dothm.load_state()






def _scp_style(url):
    """Return whether a Git source uses the SCP-like ``host:path`` form."""
    return ("://" not in url and not Path(url).expanduser().exists()
            and bool(re.match(r"^(?:[^/@:]+@)?[^/:]+:.+", url)))


def default_clone_destination(source) -> Path:
    """
    Name the folder that a clone creates when no destination is given.

    The name is the last segment of the source path or URL without a ``.git``
    suffix. A ``.hm`` folder is named after its worktree, and ``NAME.hm``
    after ``NAME``, so the default is never a bare repository.

    Args:
        source (str): Git URL, local path, or published catalog URL.

    Returns:
        Path: Relative folder name in the current folder.

    Raises:
        CloneError: If the source has no usable name.
    """
    text = str(source).strip()
    if "://" in text:
        path = unquote(urlsplit(text).path)
    elif _scp_style(text):
        path = text.split(":", 1)[1]
    else:
        path = text
    segments = [part for part in path.split("/") if part not in ("", ".")]
    while segments:
        name = segments.pop()
        if name == ".hm":
            continue
        for suffix in (".git", ".hm"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
        if name and name != "..":
            return Path(name)
        break
    raise CloneError("Cannot name a folder after this source; give a DIRECTORY")


def _local_source_root(url):
    """Return the folder of a local source repository, or None for remote URLs."""
    if url.startswith("file://"):
        local = Path(unquote(urlsplit(url).path))
    elif "://" in url:
        return None
    else:
        local = Path(url).expanduser()
    if not local.exists():
        return None
    local = local.resolve()
    return local.parent if local.name == ".hm" else local


def _check_destination(cls, url, destination, display_path):
    """
    Refuse a destination that is not empty, is nested in a repository, or
    overlaps a local source. Nothing is created or contacted.

    Returns:
        bool: True when the destination is an existing empty folder.
    """
    if destination.is_symlink() or (destination.exists() and (
            not destination.is_dir() or any(destination.iterdir()))):
        raise DestinationExistsError(
            f"fatal: destination path '{display_path}' already exists and is "
            "not empty.")
    target = destination.resolve()
    source = _local_source_root(url)
    if source is not None and (target == source or source in target.parents
                               or target in source.parents):
        raise CloneError(
            f"Destination '{display_path}' overlaps the source repository "
            f"'{source}'; choose a separate folder")
    enclosing = cls.find_root(target.parent)
    if enclosing is not None:
        raise CloneError(
            f"Destination '{display_path}' is inside the Hallmark repository at "
            f"'{enclosing}'; nested repositories are not supported, so choose "
            "a folder outside it")
    return destination.is_dir()


def _remove_partial_clone(cls, destination, *, created):
    """
    Remove what a failed clone created, never following a symbolic link.

    The folders the clone created for its destination are removed. In a
    worktree folder that existed only its new ``.hm`` is removed; a bare
    folder that existed, and held nothing before, is emptied again.

    Args:
        cls: Repository class, used to tell bare destinations apart.
        destination (Path): Clone destination.
        created (Path, optional): Highest folder created for the destination,
            or None if the destination existed.
    """
    if created is not None:
        if not created.is_symlink():
            rmtree(created, ignore_errors=True)
        return
    if destination.is_symlink():
        return
    if cls.resolve_repo_paths(destination)[1] is not None:
        dothm = destination / ".hm"
        if dothm.is_symlink():
            dothm.unlink()
        else:
            rmtree(dothm, ignore_errors=True)
        return
    destination.mkdir(exist_ok=True)
    for child in destination.iterdir():
        if child.is_dir() and not child.is_symlink():
            rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _tracks_local_files(table_text):
    """
    Return whether a ``data.tsv`` catalog versions local files.

    Local catalogs record a ``sha1`` per file and keep contents in
    ``.hm/objects``; remote catalogs list ``path`` rows on a data server.
    Unreadable tables are left to catalog validation.
    """
    try:
        frame = pd.read_csv(StringIO(table_text), sep="\t", dtype=str,
                            keep_default_na=False)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return False
    return (not frame.empty and "sha1" in frame.columns
            and "path" not in frame.columns)


def _refuse_local_file_branches(git, branches):
    """
    Refuse a clone when a branch's committed catalog versions local files.

    Args:
        git: GitPython command wrapper of the repository holding the branches.
        branches (list[tuple[str, str]]): Branch names and their revisions.

    Raises:
        CloneError: If any branch tracks local files.
    """
    local = []
    for name, revision in branches:
        try:
            table = git.show("--end-of-options", f"{revision}:data.tsv")
        except GitCommandError:
            continue
        if _tracks_local_files(table):
            local.append(name)
    if local:
        names = ", ".join(f"'{name}'" for name in local)
        raise CloneError(
            f"Branch {names} tracks local files; cloning local-file repositories "
            "is not supported yet. Nothing was created.")


def _refuse_invalid_branch_names(git, names):
    """
    Refuse source branches whose names Git would misread, such as options.

    Raises:
        CloneError: If a branch name is invalid.
    """
    for name in names:
        try:
            validate_branch_name(git, name)
        except ValueError:
            raise CloneError(
                f"Source branch {name!r} has an invalid name; cloning it is not "
                "supported. Nothing was created.") from None


def _check_local_source(url):
    """Inspect a local Git source read-only, before anything is written."""
    root = _local_source_root(url)
    if root is None:
        return
    git_dir = root / ".hm" if (root / ".hm").is_dir() else root
    try:
        source = GitRepo(git_dir)
    except GitError:
        # Not a readable repository: cloning reports the problem itself.
        return
    with source:
        names = [head.name for head in source.heads]
        _refuse_invalid_branch_names(source.git, names)
        _refuse_local_file_branches(source.git, [(name, name) for name in names])


def _is_git_source(url, source_type):
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
    dothm_path, worktree_path = cls.resolve_repo_paths(destination)
    local = Path(url).expanduser()
    git_url = str(local / ".hm") if (local / ".hm").is_dir() else url
    try:
        dothm = Dothm.clone(git_url, dothm_path, display_path=display_path)
    except CloneError as exc:
        raise CloneError(
            f"{exc}\nFor a raw dataset, use hm init PATH, "
            "then hm add URL.") from exc
    # Sources on a Git host can only be inspected once copied; the caller
    # removes the copy if a check fails.
    _refuse_invalid_branch_names(dothm.git, [
        ref.remote_head for remote in dothm.remotes
        for ref in remote.refs if ref.remote_head != "HEAD"])
    branches = _copy_branch_pointers(dothm)
    _refuse_local_file_branches(dothm.git, [(name, name) for name in branches])
    for name in branches:
        _validate_branch(dothm.git, name)
    if worktree_path:
        Worktree.init(worktree_path)
    return cls(destination)


def clone_catalog(cls, url, path, *, auth=None, source_type="auto"):
    """
    Clone an existing Git catalog or published HTTP/SFTP catalog snapshot.

    Source URLs with credentials are rejected first. Repositories with a
    branch that tracks local files cannot be cloned yet. The destination must
    be new or an empty folder, outside any Hallmark repository and the local
    source; this is checked before source access. On failure, only what this
    call created is removed: the destination it created, or the ``.hm`` it
    added to an existing worktree folder. An existing bare folder is emptied
    again. Dataset discovery belongs to ``Repo.add(URL)``.

    Args:
        cls: Repository class used to initialize or open the result.
        url (str): Existing Git catalog or published snapshot location.
        path (Path | str): New repository destination.
        auth (str, optional): Local profile for snapshot metadata access.
        source_type (str): ``auto``, ``git``, or ``catalog``.

    Returns:
        Repo: Repository containing complete catalog metadata without payloads.

    Raises:
        DestinationExistsError: If the destination exists and is not an empty
            folder.
        CloneError: If the destination is inside a repository or overlaps a
            local source, a branch tracks local files, or a requested catalog
            is missing or invalid.
        ValueError: If source options are invalid.
        DownloadError: If remote metadata cannot be read or validated.
    """
    if source_type not in {"auto", "git", "catalog"}:
        raise ValueError("source_type must be auto, git, or catalog")
    # Git records the source URL in .hm/.git/config, so it must hold no secret.
    reject_url_credentials(str(url))
    url = str(url)
    destination = Path(path).expanduser().absolute()
    existed = _check_destination(cls, url, destination, path)
    is_git = _is_git_source(url, source_type)
    if is_git and auth is not None:
        raise ValueError("Git cloning uses Git/SSH authentication, not auth profiles")
    if is_git:
        _check_local_source(url)
    created = None
    if not existed:
        # Remember the highest missing folder so a failure removes them all.
        created = destination
        while not (created.parent.exists() or created.parent.is_symlink()):
            created = created.parent
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.mkdir()
        except FileExistsError as exc:
            raise DestinationExistsError(
                f"Destination already exists: {path}") from exc
    try:
        if is_git:
            return _clone_git(cls, url, destination, path, auth)
        source = RemoteSpec.from_url(url, auth)
        with OperationContext(source) as context:
            snapshot = _read_catalog_snapshot(context)
        if snapshot is None and not source.root.rstrip("/").endswith(".hm"):
            nested = RemoteSpec.from_url(url.rstrip("/") + "/.hm/", auth)
            with OperationContext(nested) as context:
                snapshot = _read_catalog_snapshot(context)
        if snapshot is None:
            if source_type == "auto" and source.scheme in {"http", "https"}:
                return _clone_git(cls, url, destination, path, auth)
            raise CloneError("No published Hallmark catalog at this URL; "
                             "use hm init PATH, then hm add URL "
                             "for a raw dataset")
        if _tracks_local_files(snapshot["data.tsv"]):
            raise CloneError(
                "The published catalog tracks local files; cloning local-file "
                "repositories is not supported yet. Nothing was created.")
        if cls.resolve_repo_paths(destination)[1] is None:
            # Initialization creates a bare ".hm" folder itself.
            destination.rmdir()
        repo = cls.init(destination)
        for name, contents in snapshot.items():
            (repo.dothm.path / name).write_text(contents, encoding="utf-8")
        _commit_catalog_metadata(repo, snapshot, "Import published catalog snapshot")
        return repo
    except BaseException:
        _remove_partial_clone(cls, destination, created=created)
        raise
