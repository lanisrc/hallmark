"""Example backend joining independent HTTP roots into one logical dataset.

Call ``register()`` and define a ``DataSource`` release with
``backend="multi-server"`` and ``backend_options={"routes": ...}``. Then
``repo.add(release_url + "{site}/file.fits")`` catalogs files under the release
URL with this backend; ``Repo.add(..., backend=..., backend_options=...)``
selects it without registering a source.
An installed plugin instead exposes ``MultiServerBackend`` through the
``hallmark.backends`` entry-point group. No servers are contacted on import.
"""

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace

from hallmark.backends import (
    DataBackend, OperationContext, RemoteConfigurationError,
    RemoteObjectMissing, RemoteSpec, register_backend,
)
from hallmark.transport.base import literal_path


class MultiServerBackend(DataBackend):
    """Map each first path segment to a separate HTTP(S) server root.

    Child contexts provide thread-local sessions. Their cancellation event is
    shared with the parent; cleanup closes every child's sessions. Credentials
    use Requests' usual local .netrc configuration for each server.
    """

    def __init__(self, context):
        super().__init__(context)
        routes = context.remote.backend_options.get("routes")
        if not isinstance(routes, Mapping) or not routes:
            raise RemoteConfigurationError("Multi-server options require routes")
        specs = {}
        for prefix, url in routes.items():
            if literal_path(prefix).as_posix() != prefix or "/" in prefix:
                raise RemoteConfigurationError("Route names must be one path segment")
            spec = RemoteSpec.parse(url, backend="http")
            if spec.scheme not in {"http", "https"}:
                raise RemoteConfigurationError("Example routes require HTTP(S) URLs")
            specs[prefix] = spec
        self._stack = ExitStack()
        self._children = {}
        try:
            for prefix, spec in specs.items():
                child = self._stack.enter_context(
                    OperationContext(spec, context.output_root))
                child.cancelled = context.cancelled
                self._children[prefix] = child
        except BaseException:
            self._stack.close()
            raise

    def _route(self, relative_path):
        path = literal_path(relative_path).as_posix()
        prefix, separator, suffix = path.partition("/")
        if not separator or prefix not in self._children:
            raise RemoteObjectMissing("No route for this logical dataset path")
        return self._children[prefix], suffix

    def prepare(self):
        for child in self._children.values():
            child.transport.prepare()

    def iter_entries(self, on_directory=None):
        for prefix, child in self._children.items():
            self.context.check_cancelled()

            def report(directory, prefix=prefix):
                if on_directory is not None:
                    on_directory(prefix + "/" + directory)

            for entry in child.transport.iter_entries(on_directory=report):
                yield replace(entry, path=prefix + "/" + entry.path)

    def read_text(self, relative_path, limit):
        child, path = self._route(relative_path)
        return child.transport.read_text(path, limit)

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        child, path = self._route(relative_path)
        child.on_bytes = self.context.on_bytes
        child.transport.fetch(path, destination, chunk_size=chunk_size)

    def cancel(self):
        for child in self._children.values():
            child.cancel()

    def close(self):
        self._stack.close()


def register():
    """Register this example in the current process before using it."""
    register_backend("multi-server", MultiServerBackend)
