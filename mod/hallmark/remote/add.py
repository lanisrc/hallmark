from copy import deepcopy
from urllib.parse import unquote, urlsplit, urlunsplit

import pandas as pd
import parse

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
from ..utils import as_list_of_dicts
from ..repo.config import normalize_remotes, validate_tsv_filename
from ..repo.state import is_remote_catalog
from ..transport import OperationContext, RemoteSpec
from ..transport.base import (
    DownloadError, RemoteConfigurationError, copy_backend_options,
    reject_url_secrets)

# Columns Hallmark writes for every remote catalogue row; pattern fields with
# these names are not stored.
CATALOG_COLUMNS = ["path", "checksum_algorithm", "checksum", "size_bytes", "mtime",
                   "checksum_source", "source"]
_RESERVED_FIELDS = {*CATALOG_COLUMNS, "sha1"}
# A kept sha1 must describe the same server file as the new row.
_SAME_FILE = ("size_bytes", "mtime", "checksum_algorithm", "checksum")


def split_remote_pattern(value, fmt=None):
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http", "ssh", "sftp"}:
        raise ValueError("Remote add requires an HTTP(S), SSH or SFTP URL")
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
    names = [remote.get("name") or "origin" for remote in remotes]
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
            if all(str(before.get(key, "")) == str(record.get(key) if record.get(key)
                                                  is not None else "")
                   for key in _SAME_FILE):
                sha1 = before.get("sha1", "")
        kept.append(sha1)
    frame = frame.copy()
    frame["sha1"] = kept
    return frame


def add_remote(repo, value, *, fmt=None, filter=None, auth=None, backend=None,
               backend_options=None, remote=None, source=None, prefix=None,
               progress=False):
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
        progress: Discovery progress callback or flag.

    Returns:
        pandas.DataFrame: Rows found by the scan. ``attrs["removed"]`` lists
        catalogued paths that the server no longer has.
    """
    url, fmt = split_remote_pattern(value, fmt)
    path_matches("validation", filter=filter, fmt=fmt)
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
        name, prefix = current.name, current.prefix
    else:
        name = source or derive_source_name(
            remote_name, root, [item.name for item in sources])
    selected = SourceSpec(name, remote_name, root, prefix or "", fmt, filter)
    data = state.data
    mine = (rows_of(data, current, sources) if current is not None
            else pd.Series(False, index=data.index))
    previous = data[mine]
    if current is not None and not current.selects_like(fmt, filter):
        _check_selection_keeps_rows(selected, previous, fmt, filter)

    try:
        with OperationContext(server.subpath(root)) as context:
            entries = discover_remote_files(
                context, filter=filter, fmt=fmt, progress=progress
            )
    except RemoteConfigurationError:
        raise
    except DownloadError as exc:
        # Nothing is saved until the scan completes, so the catalogue is intact.
        raise DownloadError(
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
            matched = parse.parse(fmt, item.path, case_sensitive=True)
            row.update({key: value for key, value in matched.named.items()
                        if key not in _RESERVED_FIELDS})
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
    repo.dothm.save_state(state)
    repo.state = repo.dothm.load_state()
    result = pd.DataFrame(rows)
    result.attrs["removed"] = sorted(set(previous.get("path", [])) - set(frame["path"]))
    return result
