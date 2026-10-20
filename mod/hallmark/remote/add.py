import os
from copy import deepcopy
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
    rows_of,
    validate_source_name,
)
from ..utils import as_list_of_dicts, resolve_path_in_root
from ..repo.config import (
    LOCAL_RESERVED_FIELDS, check_pattern_fields, literal_fields, normalize_remotes,
    remote_name_of,
    validate_tsv_filename)
from ..repo.dothm import data_tsv_text
from ..repo.state import cell_text, is_remote_catalog
from ..transport import OperationContext, RemoteSpec
from ..transport.base import (
    DownloadError, RemoteConfigurationError, copy_backend_options,
    reject_url_secrets)

# Columns Hallmark writes for every remote catalogue row; pattern fields with
# these names are not stored.
CATALOG_COLUMNS = ["path", "checksum_algorithm", "checksum", "size_bytes", "mtime",
                   "checksum_source", "source"]
_RESERVED_FIELDS = {*CATALOG_COLUMNS, *LOCAL_RESERVED_FIELDS}
# A kept sha1 must describe the same server file as the new row.
_SAME_FILE = ("size_bytes", "mtime", "checksum_algorithm", "checksum")


# Shorthand schemes for WebDAV servers, which speak HTTP(S).
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
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http", "ssh", "sftp", "s3"}:
        raise ValueError("Remote add requires an HTTP(S), WebDAV, S3, SSH or SFTP URL")
    reject_url_secrets(value)
    path = parts.path
    if "{" in path:
        if fmt is not None:
            raise ValueError("Use a pattern in the URL or --fmt, not both")
        boundary = path.rfind("/", 0, path.index("{")) + 1
        fmt = unquote(path[boundary:])
        path = path[:boundary]
    if fmt is not None:
        path_matches("validation", fmt=fmt)
    return urlunsplit(parts._replace(path=path)), fmt


def _describe_selection(fmt, filter):
    """Describe a remote selection for messages about the files it matched."""
    parts = []
    if fmt:
        parts.append(f"pattern {fmt!r}")
    if filter:
        patterns = [filter] if isinstance(filter, str) else list(filter)
        parts.append("filter " + ", ".join(repr(pattern) for pattern in patterns))
    return " and ".join(parts) or "any file"


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


def _choose_source(sources, remote, root, fmt, filter, name):
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
    if fmt is None and not filter:
        if len(here) > 1:
            raise ValueError(
                "Several sources catalog this folder ("
                + ", ".join(source.name for source in here)
                + "); choose one with --source")
        return here[0] if here else None
    return next((source for source in here if source.selects_like(fmt, filter)),
                None)


def _check_selection_keeps_rows(source, rows, fmt, filter):
    """Refuse a changed pattern or filter that would drop catalogued files."""
    dropped = [path for path in rows["path"]
               if not path_matches(source.remote_path(path)[len(source.root):],
                                   filter=filter, fmt=fmt)]
    if dropped:
        raise ValueError(
            f"{len(dropped)} catalogued file(s) of source {source.name!r} do not "
            f"fit {_describe_selection(fmt, filter)}:\n{_listed(dropped, 3)}\n"
            "Use a pattern that still matches them, or a new branch.")


def _check_collisions(paths, others, selected, sources):
    """Refuse paths that already belong to another source, as files or folders."""
    other_paths = set(others["path"]) if "path" in others.columns else set()
    folders = {parent for path in other_paths for parent in _parents(path)}
    clashes = {path for path in paths
               if path in other_paths or path in folders
               or any(parent in other_paths for parent in _parents(path))}
    if not clashes:
        return
    owners = (set(others.loc[others["path"].isin(clashes), "source"])
              if "source" in others.columns else set())
    place = (selected.remote, selected.root, selected.prefix)
    same = [source.name for source in sources if source.name in owners
            and (source.remote, source.root, source.prefix) == place]
    if same:
        hint = (f"To change the pattern of source {same[0]!r}, add again with "
                f"--source {same[0]}")
    elif selected.prefix:
        hint = "Choose a different --prefix, or narrow it with --filter"
    else:
        hint = "Add this source with --prefix DIR, or narrow it with --filter"
    raise ValueError(
        f"{len(clashes)} path(s) already belong to another source:\n"
        f"{_listed(clashes)}\n{hint}.")


def _parents(path):
    parts = path.split("/")[:-1]
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def _saved_catalogue(repo):
    """Return the saved catalogue as comparable text: config, meta and rows."""
    saved = repo.dothm.load_state()
    return (repr(saved.config), repr(saved.meta), data_tsv_text(saved.data))


def _same_file(before, record):
    """Return whether the server reports a catalogued file unchanged."""
    return all(cell_text(before.get(key)) == cell_text(record.get(key))
               for key in _SAME_FILE)


def _keep_sha1(frame, previous):
    """Carry each file's known sha1 forward when the server reports it unchanged."""
    if "sha1" not in previous.columns or previous.empty:
        return frame
    old = previous.set_index("path")
    kept = []
    for record in frame.to_dict(orient="records"):
        sha1 = ""
        if record["path"] in old.index:
            before = old.loc[record["path"]]
            if _same_file(before, record):
                sha1 = before.get("sha1", "")
        kept.append(sha1)
    frame = frame.copy()
    frame["sha1"] = kept
    return frame


def _changed_upstream(frame, previous, worktree):
    """
    Return downloaded paths whose file the server now reports as changed.

    A path counts as downloaded when its row had a sha1 or a file exists at
    it in the worktree.
    """
    if previous.empty or "path" not in previous.columns:
        return []
    old = previous.set_index("path")
    changed = []
    for record in frame.to_dict(orient="records"):
        path = record["path"]
        if path not in old.index or _same_file(old.loc[path], record):
            continue
        if cell_text(old.loc[path].get("sha1")) or _exists_in(worktree, path):
            changed.append(path)
    return sorted(changed)


def _exists_in(worktree, path):
    if worktree is None:
        return False
    try:
        return os.path.lexists(resolve_path_in_root(worktree, path))
    except (OSError, ValueError):
        return False


def add_remote(repo, value, *, fmt=None, filter=None, auth=None, backend=None,
               backend_options=None, remote=None, source=None, prefix=None,
               server_checksum=None, progress=False):
    """
    Catalog files from a remote folder as one source of the catalogue.

    The URL is matched to a configured remote, or becomes a new one. The
    selection updates the source with the same folder and selection (or the
    one named by ``source``), or adds a new source. Re-adding a source
    replaces its rows: files gone from the server are dropped, and a known
    ``sha1`` is kept while the server reports the file unchanged. Nothing is
    written unless the scan completes and matches at least one file.

    Args:
        repo (Repo): Repository to update.
        value (str): Folder URL, optionally ending in a filename pattern.
        fmt (str, optional): Filename pattern, if not part of the URL.
        filter (str | list[str], optional): Glob filters.
        auth, backend, backend_options: Settings for the remote; given values
            replace its saved ones.
        remote (str, optional): Remote name to use or create.
        source (str, optional): Source name to update or create.
        prefix (str, optional): Local folder for this source's files.
        server_checksum (str, optional): Have the server compute this
            algorithm for files without a published checksum; saved with the
            source and reused when it is added again.
        progress: Discovery progress callback or flag.

    Returns:
        pandas.DataFrame: Rows found by the scan. ``attrs["removed"]`` lists
        catalogued paths that the server no longer has, and
        ``attrs["changed"]`` downloaded paths whose file it reports changed.
    """
    value, backend = _expand_webdav_scheme(value, backend)
    url, fmt = split_remote_pattern(value, fmt)
    path_matches("validation", filter=filter, fmt=fmt)
    if fmt:
        check_pattern_fields(fmt, _RESERVED_FIELDS)
    specs = as_list_of_dicts(repo.state.config.get("data", []))
    if specs is None or any(not isinstance(spec, dict) for spec in specs):
        raise ValueError("Catalog data must be a mapping or list of mappings")
    if any(spec.get("file") or
           validate_tsv_filename(spec.get("db", "data.tsv")) != "data.tsv"
           for spec in specs):
        raise ValueError("Remote add currently supports only the data.tsv catalog")
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
    current = _choose_source(sources, remote_name, root, fmt, filter, source)
    if current is not None:
        if prefix is not None and SourceSpec(
                current.name, remote_name, prefix=prefix).prefix != current.prefix:
            raise ValueError(
                f"Source {current.name!r} uses prefix {current.prefix!r}; "
                "a source keeps its prefix")
        if fmt is None and not filter:
            fmt, filter = current.fmt, list(current.filter) or None
        if server_checksum is None:
            server_checksum = current.server_checksum
        name, prefix = current.name, current.prefix
    else:
        name = source or derive_source_name(
            remote_name, root, [item.name for item in sources])
    selected = SourceSpec(name, remote_name, root, prefix or "", fmt, filter,
                          server_checksum or None)
    data = state.data
    mine = (rows_of(data, current, sources) if current is not None
            else pd.Series(False, index=data.index))
    previous = data[mine]
    if current is not None and not current.selects_like(fmt, filter):
        _check_selection_keeps_rows(selected, previous, fmt, filter)

    try:
        with OperationContext(server.subpath(root)) as context:
            entries = discover_remote_files(
                context, filter=filter, fmt=fmt, progress=progress,
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
            f"No remote files matched {_describe_selection(fmt, filter)} under "
            f"{url}; previous catalogue kept")

    rows = []
    for item in entries:
        row = {"path": selected.local_path(item.path),
               "checksum_algorithm": item.checksum_algorithm,
               "checksum": item.checksum, "size_bytes": item.size,
               "mtime": item.mtime, "checksum_source": item.checksum_source,
               "source": selected.name}
        if fmt:
            # Field values keep the text of the name, as for local files.
            row.update(literal_fields(fmt, item.path) or {})
        rows.append(row)
    frame = pd.DataFrame(rows)
    others = data[~mine]
    _check_collisions(set(frame["path"]), others, selected, sources)
    stored = _keep_sha1(frame, previous.fillna(""))
    # An empty catalogue still has the local manifest's default columns.
    combined = (pd.concat([others, stored], ignore_index=True, sort=False)
                if not others.empty else stored)
    state.data = combined.fillna("").sort_values("path").reset_index(drop=True)

    if current is None:
        sources.append(selected)
    else:
        sources[sources.index(current)] = selected
    state.config["remote"] = remotes
    state.config["sources"] = [item.to_config() for item in sources]
    state.config["data"] = [{"db": "data.tsv"}]
    with repo.dothm.state_lock():
        # A scan can take minutes; never save over a change made meanwhile.
        if _saved_catalogue(repo) != scanned_from:
            raise ValueError(
                "The catalogue changed while the server was scanned "
                "(previous catalogue kept); run hm add again")
        repo.dothm.save_state(state)
    repo.state = state
    result = pd.DataFrame(rows)
    result.attrs["removed"] = sorted(set(previous.get("path", [])) - set(frame["path"]))
    result.attrs["changed"] = _changed_upstream(
        frame, previous.fillna(""), getattr(repo, "worktree", None))
    return result
