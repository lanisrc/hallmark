"""Named data sources with explicit releases and collection directory roots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import metadata
from threading import RLock
from types import MappingProxyType

from .transport.base import RemoteSpec, backend_name, literal_path


@dataclass(frozen=True)
class SourceRelease:
    """A release URL and collection names mapped to relative directory roots.

    ``backend`` and ``backend_options`` are optional adapter configuration for
    source authors. Collection paths and catalog paths use the release root.
    """

    url: str
    collections: Mapping
    description: str = ""
    backend: str | None = None
    backend_options: Mapping = field(default_factory=dict)
    collection_descriptions: Mapping = field(default_factory=dict)

    def __post_init__(self):
        remote = RemoteSpec.parse(self.url, backend=self.backend,
                                  backend_options=self.backend_options)
        collections = {}
        for name, paths in self.collections.items():
            backend_name(name)
            if isinstance(paths, str):
                paths = (paths,)
            paths = tuple(literal_path(path).as_posix() for path in paths)
            if not paths:
                raise ValueError("Collections must have at least one directory root")
            collections[name] = paths
        object.__setattr__(self, "collections", MappingProxyType(collections))
        object.__setattr__(self, "backend_options", remote.backend_options)
        object.__setattr__(self, "collection_descriptions",
                           MappingProxyType(dict(self.collection_descriptions)))

    def remote(self):
        """Return the validated transport specification without connecting."""
        return RemoteSpec.parse(self.url, backend=self.backend,
                                backend_options=self.backend_options)


@dataclass(frozen=True)
class DataSource:
    """A named service with a description and explicit release definitions."""

    name: str
    description: str
    releases: Mapping

    def __post_init__(self):
        backend_name(self.name)
        releases = dict(self.releases)
        if not releases or any(not isinstance(value, SourceRelease)
                               for value in releases.values()):
            raise ValueError("Sources require named SourceRelease definitions")
        for name in releases:
            backend_name(name)
        object.__setattr__(self, "releases", MappingProxyType(releases))


def _desi_release(release, production):
    collections = {
        "redshifts": (f"spectro/redux/{production}/zcatalog",),
        "spectra": (f"spectro/redux/{production}/healpix",
                    f"spectro/redux/{production}/tiles"),
        "vac": ("vac",),
    }
    if release == "dr1":
        collections["lss"] = ("survey/catalogs/dr1/LSS",)
    return SourceRelease(
        f"https://data.desi.lbl.gov/public/{release}/", collections,
        description="Early Data Release" if release == "edr" else "Data Release 1",
        collection_descriptions={
            "redshifts": "Merged redshift catalogs (all published versions)",
            "spectra": "HEALPixel and tile product directories",
            "vac": "Value-added products",
            **({"lss": "Large-scale-structure catalogs"} if release == "dr1" else {}),
        })


_registered = {"desi": DataSource(
    "desi", "Dark Energy Spectroscopic Instrument public data", {
        "edr": _desi_release("edr", "fuji"),
        "dr1": _desi_release("dr1", "iron"),
    })}
_loaded = {}
_lock = RLock()


def _entry_points():
    entries = metadata.entry_points()
    if hasattr(entries, "select"):
        return list(entries.select(group="hallmark.sources"))
    return list(entries.get("hallmark.sources", ()))


def register_source(source: DataSource):
    """Register a source for this process; installed plugins use hallmark.sources.

    Entry points expose a ``DataSource`` instance. Duplicate names are rejected.
    Registration and source descriptions never contact a data service.
    """
    if not isinstance(source, DataSource):
        raise TypeError("Expected a DataSource")
    with _lock:
        if (source.name in _registered or source.name in _loaded
                or any(item.name == source.name for item in _entry_points())):
            raise ValueError(f"Duplicate data source {source.name!r}")
        _registered[source.name] = source


def get_source(name: str) -> DataSource:
    """Resolve a registered source or load its installed descriptor."""
    backend_name(name)
    with _lock:
        entries = [item for item in _entry_points() if item.name == name]
        if len(entries) > 1 or (entries and name in _registered):
            raise ValueError(f"Duplicate data source {name!r}")
        if name in _registered:
            return _registered[name]
        if name in _loaded:
            return _loaded[name]
        if not entries:
            raise ValueError(f"Unknown data source {name!r}; use hallmark sources")
        try:
            source = entries[0].load()
        except Exception:
            raise ValueError(f"Unable to load data source {name!r}") from None
        if not isinstance(source, DataSource) or source.name != name:
            raise ValueError(
                f"Source plugin {name!r} must expose a matching DataSource")
        _loaded[name] = source
        return source


def list_sources():
    """Return source descriptors in name order without discovering files."""
    names = set(_registered) | {item.name for item in _entry_points()}
    return [get_source(name) for name in sorted(names)]


def backend_for_url(url: str):
    """
    Return the backend a registered source release declares for a URL.

    Args:
        url (str): Remote directory URL.

    Returns:
        tuple[str, Mapping] | None: The backend name and options of the release
        whose URL contains ``url``, or None if no such release declares one.
    """
    for source in list_sources():
        for release in source.releases.values():
            root = release.url.rstrip("/") + "/"
            if release.backend and (url.rstrip("/") + "/").startswith(root):
                return release.backend, release.backend_options
    return None
