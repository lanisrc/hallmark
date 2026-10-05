from copy import deepcopy
from urllib.parse import unquote, urlsplit, urlunsplit

import pandas as pd
import parse

from .discovery import discover_remote_files, path_matches
from ..utils import as_list_of_dicts
from ..repo.config import normalize_remotes, validate_tsv_filename
from ..transport import OperationContext, RemoteSpec
from ..transport.base import (
    DownloadError, RemoteConfigurationError, copy_backend_options,
    reject_url_secrets)


def is_remote_catalog(state):
    return "path" in state.data.columns and "sha1" not in state.data.columns


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


def add_remote(repo, value, *, fmt=None, filter=None, auth=None, backend=None,
               backend_options=None, progress=False):
    url, fmt = split_remote_pattern(value, fmt)
    path_matches("validation", filter=filter, fmt=fmt)
    specs = as_list_of_dicts(repo.state.config.get("data", []))
    if specs is None or any(not isinstance(spec, dict) for spec in specs):
        raise ValueError("Catalog data must be a mapping or list of mappings")
    if any(spec.get("file") or
           validate_tsv_filename(spec.get("db", "data.tsv")) != "data.tsv"
           for spec in specs):
        raise ValueError("Remote add currently supports only the data.tsv catalog")
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
    try:
        with OperationContext(source) as context:
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
    columns = ["path", "checksum_algorithm", "checksum", "size_bytes", "mtime"]
    rows = []
    for entry in entries:
        row = dict(zip(columns, (entry.path, entry.checksum_algorithm,
                                entry.checksum, entry.size, entry.mtime)))
        if fmt:
            matched = parse.parse(fmt, entry.path, case_sensitive=True)
            row.update({key: value for key, value in matched.named.items()
                        if key not in {*columns, "sha1"}})
        rows.append(row)
    frame = pd.DataFrame(rows)
    state = deepcopy(repo.state)
    if not existing.empty:
        frame = pd.concat([existing, frame], ignore_index=True, sort=False)
        frame = frame.drop_duplicates(subset=["path"], keep="last")
    state.data = frame.fillna("").sort_values("path").reset_index(drop=True)
    remote = {"name": "origin", "url": source.url, "backend": source.backend}
    if source.auth:
        remote["auth"] = source.auth
    if source.backend_options:
        remote["backend_options"] = copy_backend_options(source.backend_options)
    data_spec = {"db": "data.tsv"}
    if fmt:
        data_spec["fmt"] = fmt
    state.config["data"] = [data_spec]
    state.config["remote"] = [remote]
    repo.dothm.save_state(state)
    repo.state = repo.dothm.load_state()
    return pd.DataFrame(rows)
