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

Users catalog files with ``hallmark add 'URL/TEMPLATE'``, and Hallmark selects
the transport from the URL automatically. Source authors can specify an adapter
and its nonsecret configuration in a release definition; templates added
beneath the release URL use it, so users need no backend flags or options file.

Define a source and register it for the current Python process:

.. code-block:: python

   from hallmark import DataSource, SourceRelease, Repo, register_source

   survey = DataSource("survey", "Example survey", {
       "dr1": SourceRelease(
           "https://survey.example.org/release/",
           {"redshifts": ("catalogs/redshifts",)},
       ),
   })
   register_source(survey)
   repo = Repo.init("survey")
   repo.add("https://survey.example.org/release/catalogs/redshifts/{name}.fits")

Collection roots are relative directories below the release URL. To expose
this descriptor to other Python processes and the CLI, publish the ``survey``
instance in the plugin's ``pyproject.toml``:

.. code-block:: toml

   [project.entry-points."hallmark.sources"]
   survey = "my_survey.sources:survey"

Users can then run ``hallmark sources survey`` to see the URLs of available
releases and collections, and ``hallmark ls-remote`` and ``hallmark add`` with
those URLs. Source registration and listing must not contact the service.
Source names are unique across registered and installed descriptors.

A template's data entry records its URL and, when the URL alone would select a
different transport, its ``backend`` and ``backend_options``. Python
``repo.add(url_template, backend=..., backend_options=...)`` selects them
explicitly. Data remotes configured with ``set-config`` keep their own
transport settings; the developer API ``repo.set_config(remote_backend=...,
remote_backend_options=...)`` updates them and has no CLI counterpart.
Existing catalog transport metadata remains supported. Options contain
shareable configuration, never credentials. SSH uses ``~/.ssh/config`` and its
agent; HTTP retains Requests authentication.

``repo.plan_download()`` reads the local catalog without opening a connection.
The plan captures each source's backend and immutable options, URL, the
destination and file metadata. Later configuration changes cannot redirect an
approved plan. Install the selected transport plugins before executing a
transfer.

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
       When ``context.descend`` is set, skip each subdirectory for which it
       returns False; it receives the relative path ending in ``/``. ``add``
       uses it so templates list only directories they can match. Ignoring it
       is correct but slower.
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

Register the class for the current Python process before cataloging or
transfer:

.. code-block:: python

   from hallmark import DataSource, SourceRelease, Repo, register_backend, register_source
   from my_survey.backend import SurveyBackend

   register_backend("survey", SurveyBackend)
   register_source(DataSource("survey", "Manifest survey", {
       "dr1": SourceRelease("https://survey.example.org/release/", {},
                            backend="survey"),
   }))
   repo = Repo.init("survey")
   repo.add("https://survey.example.org/release/{field}/{name}.fits")

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
combines HTTP roots beneath logical prefixes. Its source definition supplies
the following adapter options:

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

   from hallmark import DataSource, SourceRelease, Repo, register_source
   from demo.multi_server_backend import register

   register()
   register_source(DataSource("combined", "Two data servers", {
       "v1": SourceRelease("https://north.example.org/export/", {},
           backend="multi-server", backend_options={"routes": {
               "north": "https://north.example.org/export/",
               "south": "https://south.example.org/export/",
           }}),
   }))
   repo = Repo.init("combined")
   repo.add("https://north.example.org/export/{site}/run_{run}.fits")
   plan = repo.plan_download(include="north/**/*.fits")
   print(plan.summary())

The example is tested with two disposable local HTTP servers. The URLs above
are illustrative. Collaboration-specific adapters for LSST, Roman, JWST or
Euclid can implement their own manifests, directory conventions or API
routing through the same contract; those service-specific plugins are not
included with Hallmark.
