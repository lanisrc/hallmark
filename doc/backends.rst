Data backends
=============

A data backend discovers files and transfers them from a data source. It does
not determine where the Hallmark catalog is hosted. The same catalog can be
shared through GitHub, an HTTP server or SFTP while its backend accesses a
separate data service or several servers.

Built-in backends and configuration
-----------------------------------

``hallmark.backends`` exposes the common ``DataBackend`` base and these
implementations:

* ``HttpBackend`` transfers HTTP(S) files and discovers supported HTML indexes.
* ``SshBackend`` uses SFTP for both SSH and SFTP data URLs, including accounts
  without a login shell.
* ``CyVerseBackend`` extends HTTP support with CyVerse directory and metadata
  parsing.

Without an explicit backend, Hallmark selects one from the data URL. Explicit
selection takes precedence. For example:

.. code-block:: bash

   hallmark init ./observations --from 'https://data.example.org/export/' \
       --backend http --filter '**/*.fits'

For an installed plugin, pass its registered name and an optional YAML mapping:

.. code-block:: bash

   hallmark init ./survey --from 'https://survey.example.org/release/' \
       --backend survey --backend-options ./survey-options.yml

``Repo.init(..., from_url=url, backend="survey", backend_options=options)``
accepts the same settings in Python. The data remote stores ``backend`` and
``backend_options`` alongside its ``name``, ``url`` and optional ``auth``:

.. code-block:: yaml

   remote:
     - name: origin
       url: https://survey.example.org/release/
       backend: survey
       backend_options:
         release: dr1

Options contain ordinary configuration that can be shared with the catalog.
Keep tokens, passwords and private keys outside them. The built-in SSH backend
uses local auth profiles; HTTP retains Requests' authentication and environment
behavior. A plugin documents how it obtains any additional credentials locally.
The example ``release`` option above is interpreted by the plugin, not Hallmark.

Update an existing remote in Python with
``repo.set_config(remote_backend="survey", remote_backend_options=options)``.
Passing ``remote_backend=""`` returns to URL-based selection; passing
``remote_backend_options={}`` clears the options. ``None`` leaves the respective
setting unchanged. Configurations without backend fields remain valid.

``repo.plan_download()`` reads the local catalog without opening a connection.
The plan captures its selected backend name and deeply immutable options, in
addition to its source URL, profile reference, destination and file metadata.
Changes to repository settings or the caller's original options mapping do not
redirect an approved plan. Install or register the selected plugin before
executing a transfer that uses it.

Writing a backend
-----------------

Implement ``DataBackend`` or extend an existing backend. All classes receive
an ``OperationContext`` through ``__init__(context)``. The context exposes the
validated ``RemoteSpec`` as ``context.remote`` and its frozen options as
``context.remote.backend_options``. ``context.session()`` supplies a Requests
session per worker thread; ``context.check_cancelled()`` raises when the
operation has been cancelled.

.. list-table:: Backend contract
   :header-rows: 1
   :widths: 35 65

   * - Method
     - Responsibility
   * - ``prepare()``
     - Check access and prepare operation resources before discovery or transfer.
       Repeated calls must be safe.
   * - ``iter_entries(on_directory=None)``
     - Yield ``RemoteEntry`` records with literal logical paths relative to the
       dataset root. Report completed directories through the optional callback.
       Missing sizes, timestamps and published checksums remain ``None``.
   * - ``read_text(relative_path, limit)``
     - Read metadata within the supplied byte limit and return decoded text.
       Reject oversized responses and propagate access or connection errors.
   * - ``fetch(relative_path, destination, *, chunk_size=8192)``
     - Write a payload to the caller-managed temporary path. Check cancellation
       during transfer and call ``context.on_bytes(count)`` when set.
   * - ``cancel()``
     - Stop resources started by this backend, including active child operations.
   * - ``close()``
     - Release connections and processes when the operation ends.

``list_entries()`` is a convenience wrapper over ``iter_entries()``. Raise
``CapabilityError`` when a backend cannot list or read metadata, and
``RemoteObjectMissing`` for an absent metadata object. These exceptions are
available from ``hallmark.backends``. Do not disguise authentication failures
as missing objects.

Discovery reads listings and published metadata only. It must not download
payloads to infer checksums. The shared downloader owns approval, destination
path validation, checksum verification, atomic replacement and worker
concurrency. Backends map logical paths to server paths or API requests and
write only to the temporary destination they are given.

An instance can serve concurrent ``fetch`` calls. Protect shared mutable
state, use thread-local sessions, and check cancellation in listing, metadata
and transfer loops. Document any server concurrency limit. ``cancel`` may run
while workers are active; make it and ``close`` safe to call during cleanup.
Keep resource acquisition in ``prepare`` or managed contexts so partial setup
can be cleaned up after failures.

For example, an HTTP service can publish a small ``manifest.json`` array with
``path``, optional ``size`` and optional ``sha256`` fields. Save this adapter as
``my_survey/backend.py`` in an installable Python package:

.. code-block:: python

   import json

   from hallmark.backends import HttpBackend, RemoteEntry


   class SurveyBackend(HttpBackend):
       """Read a published manifest; inherit bounded HTTP reads and transfers."""

       def iter_entries(self, on_directory=None):
           records = json.loads(self.read_text("manifest.json", self.context.text_limit))
           for record in records:
               self.context.check_cancelled()
               digest = record.get("sha256")
               yield RemoteEntry(
                   path=record["path"],
                   size=record.get("size"),
                   checksum_algorithm="sha256" if digest else None,
                   checksum=digest,
               )
           if on_directory is not None:
               on_directory("")

Register the class for the current Python process before initialization or
transfer:

.. code-block:: python

   from hallmark import Repo, register_backend
   from my_survey.backend import SurveyBackend

   register_backend("survey", SurveyBackend)
   repo = Repo.init(
       "survey", from_url="https://survey.example.org/release/",
       backend="survey",
   )

For CLI use and other collaborators, publish the class as an entry point in
the plugin package's ``pyproject.toml``:

.. code-block:: toml

   [project.entry-points."hallmark.backends"]
   survey = "my_survey.backend:SurveyBackend"

Install that package in the same Python environment as Hallmark. Entry points
and ``register_backend`` use the same registry; duplicate names and classes
that do not subclass ``DataBackend`` are rejected. Register through one route
per process. Repository configuration stores registered names, never arbitrary
Python import paths. Old ``hallmark.transport`` imports remain supported as
compatibility aliases.

Mapping one catalog across servers
----------------------------------

The :download:`multiple-server backend example <../demo/multi_server_backend.py>`
combines HTTP roots beneath logical prefixes. For example, the options file
can contain:

.. code-block:: yaml

   routes:
     north: https://north.example.org/export/
     south: https://south.example.org/export/

The catalog records ``north/run_001.fits`` and ``south/run_002.fits``. The
backend maps those paths to their configured server roots and retains each
server's available metadata. Download plans capture the complete routing map.
These prefixes organize catalog paths; they do not require moving the data or
placing ``.hm`` on either server.

From the source checkout, register the demonstration backend explicitly:

.. code-block:: python

   from hallmark import Repo
   from demo.multi_server_backend import register

   register()
   repo = Repo.init(
       "combined",
       from_url="https://north.example.org/export/",
       backend="multi-server",
       backend_options={"routes": {
           "north": "https://north.example.org/export/",
           "south": "https://south.example.org/export/",
       }},
   )
   plan = repo.plan_download(filter="north/**/*.fits")
   print(plan.summary())

The example is tested with two disposable local HTTP servers. The URLs above
are illustrative. Collaboration-specific adapters for DESI, LSST, Roman, JWST
or Euclid can implement their own manifests, directory conventions or API
routing through the same contract; those service-specific plugins are not
included with Hallmark.
