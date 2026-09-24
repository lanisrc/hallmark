"""Public data backends and registration for collaboration-specific adapters.

Installed adapters declare the ``hallmark.backends`` entry-point group. Only
the selected adapter is imported when opening an operation; parsing remote
configuration and creating download plans never load adapter code.
"""

from importlib import metadata
from threading import RLock

from .transport import OperationContext
from .transport.base import (
    CapabilityError, DataBackend, DownloadError, RemoteConfigurationError,
    RemoteEntry, RemoteObjectMissing, RemoteSpec, TransferCancelled, backend_name,
)
from .transport.cyverse import CyVerseBackend
from .transport.http import HttpBackend
from .transport.ssh import SshBackend


_registered = {"http": HttpBackend, "ssh": SshBackend, "cyverse": CyVerseBackend}
_loaded = {}
_lock = RLock()


def _entry_points(name):
    """Find matching plugin metadata on Python 3.9 and newer versions."""
    entries = metadata.entry_points()
    if hasattr(entries, "select"):
        entries = entries.select(group="hallmark.backends")
    else:
        entries = entries.get("hallmark.backends", ())
    return [entry for entry in entries if entry.name == name]


def _validate_class(backend_class):
    if (not isinstance(backend_class, type)
            or not issubclass(backend_class, DataBackend)
            or backend_class is DataBackend):
        raise RemoteConfigurationError("Backend must be a DataBackend subclass")
    return backend_class


def register_backend(name, backend_class):
    """
    Register a backend class under a persistent configuration name.

    Args:
        name (str): Unique lowercase name using letters, digits, underscores,
            and hyphens, beginning with a letter.
        backend_class (type[DataBackend]): Class constructed with an
            OperationContext when selected by a data remote.

    Raises:
        RemoteConfigurationError: If the name is invalid, already registered
            or installed, or the class does not implement the base interface.

    Register before opening a repository operation. Distribution entry points
    provide automatic registration in fresh Python processes and the CLI.
    """
    name = backend_name(name)
    _validate_class(backend_class)
    with _lock:
        if name in _registered or name in _loaded or _entry_points(name):
            raise RemoteConfigurationError(f"Backend {name!r} is already registered")
        _registered[name] = backend_class


def get_backend(name):
    """Resolve a registered class, lazily importing a selected installed plugin."""
    name = backend_name(name)
    with _lock:
        entries = _entry_points(name)
        if len(entries) > 1 or (entries and name in _registered):
            raise RemoteConfigurationError(f"Duplicate backend name {name!r}")
        if name in _registered:
            return _registered[name]
        if name in _loaded:
            return _loaded[name]
        if not entries:
            raise RemoteConfigurationError(
                f"Backend {name!r} is not installed or registered")
        try:
            backend_class = entries[0].load()
        except Exception:
            raise RemoteConfigurationError(
                f"Unable to load backend {name!r}; check its installation") from None
        _loaded[name] = _validate_class(backend_class)
        return _loaded[name]


__all__ = [
    "DataBackend", "HttpBackend", "SshBackend", "CyVerseBackend",
    "register_backend", "RemoteEntry", "RemoteSpec", "OperationContext",
    "CapabilityError", "DownloadError", "RemoteConfigurationError",
    "RemoteObjectMissing", "TransferCancelled",
]
