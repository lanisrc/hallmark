"""
Describe the remote selections a catalogue tracks.

A data remote names a server root. A source is one ``hm add`` selection
beneath a remote: a folder (``root``), optional glob filters, and an optional
local folder (``prefix``). Each catalogued file records its source, and its
``path`` is ``prefix + root +`` the path inside the folder, so files from
several remotes share one catalogue without colliding. Filename formats are
the catalogue's, in ``config.yml``'s ``data`` section, and sort every
source's files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence
from urllib.parse import unquote

import pandas as pd

from ..repo.catalog import SOURCE, catalog_tables
from ..repo.config import normalize_remotes, remote_name_of
from ..transport.base import RemoteSpec, validate_remote_path
from ..utils import CHECKSUM_ALGORITHMS

SOURCE_FIELDS = ("name", "remote", "root", "prefix", "filter", "server_checksum")
SERVER_CHECKSUMS = CHECKSUM_ALGORITHMS
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def validate_source_name(name) -> str:
    """Return a source name made of letters, digits, ``_`` and ``-``."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(
            "Source names use 1-64 letters, digits, '_' or '-', "
            "starting with a letter or digit")
    return name


def _validate_folder(value, label: str) -> str:
    """Return a relative folder ending in ``/``, or ``""`` for the root."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a relative folder")
    folder = validate_remote_path(value.rstrip("/")).as_posix()
    if folder in ("", "."):
        return ""
    return folder + "/"


def _normalize_filter(value) -> tuple:
    if value in (None, ""):
        return ()
    patterns = [value] if isinstance(value, str) else value
    if (not isinstance(patterns, (list, tuple))
            or any(not isinstance(item, str) or not item for item in patterns)):
        raise ValueError("Source filter must be a glob or a list of globs")
    return tuple(patterns)


@dataclass(frozen=True)
class SourceSpec:
    """
    One remote selection recorded in a catalogue.

    Attributes:
        name (str): Unique source name, recorded in each row's ``source``.
        remote (str): Name of the data remote that serves the files.
        root (str): Folder beneath the remote URL, ``""`` or ending in ``/``.
        prefix (str): Local folder for the files, ``""`` or ending in ``/``.
        filter (tuple[str, ...]): Glob filters relative to ``root``.
        server_checksum (str, optional): Algorithm the server computes for
            files without a published checksum.
    """

    name: str
    remote: str
    root: str = ""
    prefix: str = ""
    filter: tuple = ()
    server_checksum: Optional[str] = None

    def __post_init__(self):
        validate_source_name(self.name)
        if not isinstance(self.remote, str) or not self.remote:
            raise ValueError(f"Source {self.name!r} must name its remote")
        object.__setattr__(self, "root", _validate_folder(self.root, "Source root"))
        # Path validation refuses .hm and .git folders at any depth.
        prefix = _validate_folder(self.prefix, "Source prefix")
        object.__setattr__(self, "prefix", prefix)
        object.__setattr__(self, "filter", _normalize_filter(self.filter))
        if self.server_checksum not in (None, *SERVER_CHECKSUMS):
            raise ValueError(
                "server_checksum must be one of: " + ", ".join(SERVER_CHECKSUMS))

    @classmethod
    def from_config(cls, entry) -> SourceSpec:
        """Validate one ``sources`` entry from ``config.yml``."""
        if not isinstance(entry, dict):
            raise ValueError("Each source must be a mapping")
        unknown = set(entry) - set(SOURCE_FIELDS)
        if unknown:
            raise ValueError(
                "Sources support only " + ", ".join(SOURCE_FIELDS) + " fields")
        return cls(entry.get("name"), entry.get("remote"), entry.get("root", ""),
                   entry.get("prefix", ""), entry.get("filter"),
                   entry.get("server_checksum"))

    def to_config(self) -> dict:
        """Return the ``config.yml`` entry, omitting empty fields."""
        entry = {"name": self.name, "remote": self.remote}
        for key in ("root", "prefix"):
            if getattr(self, key):
                entry[key] = getattr(self, key)
        if self.filter:
            entry["filter"] = list(self.filter)
        if self.server_checksum:
            entry["server_checksum"] = self.server_checksum
        return entry

    def selects_like(self, filter) -> bool:
        """Return whether filters repeat this source's selection."""
        return set(self.filter) == set(_normalize_filter(filter))

    def local_path(self, path_in_root: str) -> str:
        """Return the catalogue path of a file found inside ``root``."""
        return self.prefix + self.root + path_in_root

    def remote_path(self, catalogue_path: str) -> str:
        """Return a catalogue path relative to the remote URL."""
        if not catalogue_path.startswith(self.prefix):
            raise ValueError(
                f"Catalogue path {catalogue_path!r} is outside source {self.name!r}")
        return catalogue_path[len(self.prefix):]


def remote_names(config) -> list:
    """Return the configured remote names, naming a single unnamed remote origin."""
    remotes = normalize_remotes(config.get("remote"))
    return [remote_name_of(remote) for remote in remotes]


def normalize_sources(value, remotes: Sequence[str] = ()) -> list:
    """
    Validate the ``sources`` section of ``config.yml``.

    Args:
        value: The configured list of sources, or None.
        remotes (Sequence[str]): Remote names the sources may refer to. When
            empty, remote references are not checked.

    Returns:
        list[SourceSpec]: Validated sources in configuration order.

    Raises:
        ValueError: If an entry is malformed, a name repeats, or a source
            refers to an unknown remote.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("sources must be a list of mappings")
    sources = [SourceSpec.from_config(entry) for entry in value]
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise ValueError("Source names must be unique")
    if remotes:
        for source in sources:
            if source.remote not in remotes:
                raise ValueError(
                    f"Source {source.name!r} refers to unknown remote "
                    f"{source.remote!r}")
    return sources


def effective_sources(state) -> list:
    """
    Return the sources a state's catalogue rows belong to.

    Catalogues written before sources existed have one data remote and rows
    relative to its URL; they are read as a single source named after that
    remote, without changing anything on disk.

    Args:
        state (State): Repository state.

    Returns:
        list[SourceSpec]: Configured or synthesized sources.
    """
    config = state.config if isinstance(state.config, dict) else {}
    names = remote_names(config)
    if "sources" in config:
        return normalize_sources(config["sources"], names)
    legacy = any("path" in frame.columns and "sha1" not in frame.columns
                 for frame in catalog_tables(state).values())
    if not legacy or len(names) != 1:
        return []
    return [SourceSpec(names[0], names[0])]


def materialize_sources(state) -> list:
    """
    Write a legacy catalogue's synthesized source into the state.

    The single remote is named, ``sources`` is recorded and each row of
    every TSV gains its ``source``; ``file:`` entries name no source while
    there is only one. A state that already records sources is returned
    unchanged.

    Args:
        state (State): Repository state to update in place.

    Returns:
        list[SourceSpec]: The state's sources.
    """
    config = state.config
    if "sources" in config:
        return effective_sources(state)
    sources = effective_sources(state)
    remotes = normalize_remotes(config.get("remote"))
    if len(remotes) == 1 and not remotes[0].get("name"):
        remotes[0]["name"] = "origin"
    if remotes:
        config["remote"] = remotes
    config["sources"] = [source.to_config() for source in sources]
    if sources:
        if not state.data.empty:
            state.data = state.data.copy()
            state.data[SOURCE] = sources[0].name
        for name, frame in state.tables.items():
            if not frame.empty:
                state.tables[name] = frame.assign(**{SOURCE: sources[0].name})
    return sources


def _decoded_root(spec: RemoteSpec) -> str:
    """Return a remote's literal root folder ending in ``/``."""
    root = spec.root if spec.scheme in {"ssh", "sftp", "s3"} else unquote(spec.root)
    return root.rstrip("/") + "/"


def _same_server(left: RemoteSpec, right: RemoteSpec) -> bool:
    return ((left.scheme, left.host.lower(), left.port, left.user)
            == (right.scheme, right.host.lower(), right.port, right.user))


def match_remote(folder: RemoteSpec, remotes: Sequence[dict]):
    """
    Find the configured remote whose URL contains a folder.

    Args:
        folder (RemoteSpec): Folder URL being added.
        remotes (Sequence[dict]): Normalized remote entries.

    Returns:
        tuple[str, str] | None: The remote name and the folder's root beneath
        it, choosing the deepest matching remote, or None.
    """
    target = _decoded_root(folder)
    best = None
    for index, remote in enumerate(remotes):
        if not remote.get("url"):
            continue
        spec = RemoteSpec.from_url(remote["url"], remote.get("auth"),
                                   backend=remote.get("backend"))
        base = _decoded_root(spec)
        if not _same_server(spec, folder) or not target.startswith(base):
            continue
        if best is None or len(base) > len(best[2]):
            name = remote_name_of(remote)
            best = (name, target[len(base):], base)
    return None if best is None else best[:2]


def _unique(name: str, taken) -> str:
    candidate, number = name, 2
    while candidate in taken:
        candidate = f"{name}-{number}"
        number += 1
    return candidate


def derive_remote_name(folder: RemoteSpec, taken: Sequence[str]) -> str:
    """Name a new remote ``origin``, or after its host when others exist."""
    if not taken:
        return "origin"
    base = re.sub(r"[^a-z0-9]+", "-", folder.host.lower()).strip("-")[:40]
    return _unique(base or "remote", set(taken))


def derive_source_name(remote: str, root: str, taken: Sequence[str]) -> str:
    """Name a new source after its remote and the last folder of its root."""
    segment = root.rstrip("/").rsplit("/", 1)[-1] if root else ""
    segment = re.sub(r"[^A-Za-z0-9_-]+", "-", segment).strip("-")
    base = f"{remote}-{segment}" if segment else remote
    return _unique(base[:64].rstrip("-_") or "source", set(taken))


def rows_of(frame: pd.DataFrame, source: SourceSpec, sources: Sequence) -> pd.Series:
    """
    Return a mask of the rows that belong to a source.

    Rows without a ``source`` value belong to the only source of a legacy
    catalogue.
    """
    if SOURCE in frame.columns:
        values = frame[SOURCE].fillna("").astype(str)
        if len(sources) == 1:
            return values.isin({source.name, ""})
        return values == source.name
    return pd.Series(len(sources) == 1, index=frame.index)
