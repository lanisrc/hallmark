"""Catalog remote files that match a URL template, without downloading them."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .discovery import (
    TemplateMatcher, discover, split_url_template, template_url)
from .fmt_detection import detect_fmt
from .paraframe import ParaFrame
from .repo_config import (
    DEFAULT_DB, add_entry, find_entry, next_db_name, row_to_path)
from .repo_manifest import catalog_map
from .transport import OperationContext, RemoteSpec
from .transport.base import thaw_backend_options

# Columns of a remote catalog table, before the template fields.
REMOTE_COLUMNS = ("sha1", "checksum_algorithm", "checksum", "size_bytes", "mtime")


def _remote(base: str, backend=None, backend_options=None) -> RemoteSpec:
    """Parse a directory URL, inheriting a registered source's backend."""
    if backend is None:
        from .sources import backend_for_url

        inherited = backend_for_url(base)
        if inherited is not None:
            backend, options = inherited
            backend_options = options if backend_options is None else backend_options
    return RemoteSpec.parse(base, backend=backend, backend_options=backend_options)


def _require_template(url: str) -> tuple[str, str]:
    """Split a URL template, pointing directory URLs to ls-remote."""
    base, template = split_url_template(url)
    if template is None:
        raise ValueError(
            f"{base} is a directory; run hallmark ls-remote {base} to see "
            "suggested templates, then add URL/TEMPLATE")
    return base, template


def match_remote_template(url: str, *, progress=False, backend=None,
                          backend_options=None):
    """
    List the remote files matching a URL template without downloading them.

    Only directories the template can match are listed. Published checksum
    manifests in those directories provide checksums.

    Args:
        url (str): URL template such as ``https://host/ER2/{src}_{day}.h5``.
        progress (bool | callable): Display discovery progress or receive updates.
        backend (str, optional): Registered backend for the URL.
        backend_options (dict, optional): Backend-specific configuration.

    Returns:
        tuple[RemoteSpec, str, pd.DataFrame, int]: The remote directory, the
        template, catalog rows sorted by path (with a ``path`` column), and
        the number of listed files that matched the pattern but not the
        template's exact formatting.

    Raises:
        ValueError: If the URL or template is invalid.
        DownloadError: If discovery fails.
    """
    base, template = _require_template(url)
    matcher = TemplateMatcher(template)
    remote = _remote(base, backend, backend_options)
    with OperationContext(remote) as context:
        entries = discover(context, include=matcher.glob, progress=progress,
                           descend=matcher.may_contain)
    rows, mismatched = [], 0
    for entry in entries:
        values = matcher.match(entry.path)
        if values is None:
            continue
        # a file must be re-creatable from its fields, as local rows are
        try:
            rendered = row_to_path(values, template).as_posix()
        except (KeyError, TypeError, ValueError):
            rendered = None
        if rendered != entry.path:
            mismatched += 1
            continue
        published_sha1 = (entry.checksum if entry.checksum_algorithm == "sha1"
                          else "")
        rows.append({
            "path": entry.path, "sha1": published_sha1 or "",
            "checksum_algorithm": entry.checksum_algorithm or "",
            "checksum": entry.checksum or "",
            "size_bytes": "" if entry.size is None else str(entry.size),
            "mtime": "" if entry.mtime is None else str(entry.mtime),
            **values})
    frame = pd.DataFrame(
        rows, columns=["path", *REMOTE_COLUMNS, *matcher.fields], dtype=object)
    return remote, template, frame, mismatched


def add_remote_template(repo, url: str, *, dry_run=False, progress=False,
                        backend=None, backend_options=None) -> ParaFrame:
    """
    Catalog the remote files matching a URL template as a data entry.

    A new template becomes another data entry recording the directory URL.
    Adding a tracked URL template again syncs it: new files are added,
    changed files updated, and files no longer listed are dropped.

    Args:
        repo: Repository object.
        url (str): URL template such as ``https://host/ER2/{src}_{day}.h5``.
        dry_run (bool): List the matching files without staging them.
        progress (bool | callable): Display discovery progress or receive updates.
        backend (str, optional): Registered backend recorded for the URL.
        backend_options (dict, optional): Backend-specific configuration.

    Returns:
        ParaFrame: The matching files with their ``path`` and field values.
        ``attrs["mismatched"]`` counts listed files skipped because the
        template cannot re-create their names exactly.

    Raises:
        ValueError: If the URL is invalid, matches nothing, conflicts with a
            tracked template, or matches files tracked by another template.
        DownloadError: If discovery fails.
    """
    base, template = _require_template(url)
    TemplateMatcher(template)
    config = repo.state.config
    entry = find_entry(config, template)
    if entry is not None and (entry.url or "").rstrip("/") != base.rstrip("/"):
        where = "the worktree" if not entry.url else entry.url
        raise ValueError(
            f"template {template!r} is already tracked from {where}; run "
            f"hallmark rm --cached {template!r} first")
    if entry is not None and backend is None:
        backend, backend_options = entry.backend, entry.backend_options
    remote, template, frame, mismatched = match_remote_template(
        url, progress=progress, backend=backend, backend_options=backend_options)
    if frame.empty:
        detail = (f"; {mismatched} listed file(s) fit the pattern but not the "
                  "template's formatting" if mismatched else "")
        raise ValueError(
            f"template {template!r} did not match any files at {base}{detail}")
    result = ParaFrame(frame[["path", *frame.columns[len(REMOTE_COLUMNS) + 1:]]])
    result.attrs["mismatched"] = mismatched
    if dry_run:
        return result
    owned = catalog_map(repo.state)
    overlap = sorted(path for path in frame["path"]
                     if path in owned and owned[path].db != getattr(entry, "db", None))
    if overlap:
        shown = ", ".join(overlap[:5])
        more = f" and {len(overlap) - 5} more" if len(overlap) > 5 else ""
        raise ValueError(
            f"template {template!r} matches files tracked by another template "
            f"({shown}{more}); use a more specific template or run "
            "hallmark rm --cached TEMPLATE first")
    if entry is None:
        db = next_db_name(config, used=repo._used_table_names())
        spec = {"fmt": template, "url": base}
        # record a backend chosen explicitly or inherited from a source release
        if remote.backend != RemoteSpec.parse(base).backend:
            spec["backend"] = remote.backend
        if remote.backend_options:
            spec["backend_options"] = thaw_backend_options(remote.backend_options)
        if db != DEFAULT_DB:
            spec["db"] = db
        add_entry(config, spec)
    else:
        db = entry.db
    # a remote template is synced as a whole, like git add <pathspec>
    repo.state.replace(frame.drop(columns=["path"]), db=db)
    repo.dothm.dump(repo.state)
    return result


def suggest_templates(url: str, *, progress=False, backend=None,
                      backend_options=None) -> tuple[str, list[tuple[str, int]], int]:
    """
    List a remote directory and suggest URL templates for its files.

    Args:
        url (str): Directory URL.
        progress (bool | callable): Display discovery progress or receive updates.
        backend (str, optional): Registered backend for the URL.
        backend_options (dict, optional): Backend-specific configuration.

    Returns:
        tuple[str, list[tuple[str, int]], int]: The directory URL, suggested
        URL templates with the number of files each matches (most first), and
        the number of files listed.

    Raises:
        ValueError: If the URL has template fields or is otherwise invalid.
        DownloadError: If discovery fails.
    """
    base, template = split_url_template(url.rstrip("/") + "/")
    if template is not None:
        raise ValueError("ls-remote takes a directory URL without template fields")
    remote = _remote(base, backend, backend_options)
    with OperationContext(remote) as context:
        paths = [entry.path for entry in discover(context, progress=progress)]
    suggestions = []
    for candidate in detect_fmt(paths):
        try:
            matcher = TemplateMatcher(candidate)
        # inference can mistake literal braces for fields or pick reserved names
        except ValueError:
            continue
        count = 0
        for path in paths:
            values = matcher.match(path)
            if values is None:
                continue
            try:
                if row_to_path(values, candidate).as_posix() == path:
                    count += 1
            except (KeyError, TypeError, ValueError):
                continue
        if count:
            suggestions.append((template_url(base, candidate), count))
    suggestions.sort(key=lambda item: (-item[1], item[0]))
    return base, suggestions, len(paths)


def remote_url_template(entry) -> Optional[str]:
    """Return a remote entry's full URL template, or None for a local entry."""
    if not entry.url or not entry.fmt:
        return None
    return template_url(entry.url, entry.fmt)
