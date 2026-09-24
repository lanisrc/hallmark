API Reference
=============

This section contains the automatically generated API documentation for the
``hallmark`` package.

Core Repository
---------------

.. automodule:: hallmark.repo
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo_state
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo_config
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo_manifest
   :members:
   :show-inheritance:

Repository Worktrees
--------------------

.. automodule:: hallmark.worktree
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo_worktree
   :members:
   :show-inheritance:

State Management
----------------

.. automodule:: hallmark.state
   :members:
   :show-inheritance:

Data Handling
-------------

.. automodule:: hallmark.paraframe
   :members:
   :show-inheritance:

.. automodule:: hallmark.objects
   :members:
   :show-inheritance:

Downloading
-----------

.. automodule:: hallmark.downloader
   :members:
   :show-inheritance:

Utilities
---------

.. automodule:: hallmark.helper_functions
   :members:
   :show-inheritance:

.. automodule:: hallmark.fmt_detection
   :members:
   :show-inheritance:

.. automodule:: hallmark.dothm
   :members:
   :show-inheritance:

.. automodule:: hallmark.error
   :members:
   :show-inheritance:

Command Line Interface
----------------------

The Hallmark command-line interface provides commands for creating, managing,
and interacting with Hallmark repositories.

.. automodule:: hallmark.cli
   :members:
   :show-inheritance:

Preparing a repository
----------------------

Create a local repository::

   hallmark init ./local-project

Catalog remote files with a URL template, without downloading dataset files::

   hallmark add 'https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/{pixel}/redrock-main-dark-{pixel}.fits'
   hallmark add 'ssh://lab-data/srv/data/run{run:d}.h5'
   hallmark commit -m 'Catalog remote data'

The URL up to the first path segment containing a ``{field}`` is recorded as
the template's source; the rest is matched against the listed files, one path
segment at a time, and its fields become catalog columns. Each template is
tracked beside the others; adding one again syncs it with the server, and
``hallmark rm --cached TEMPLATE`` stops tracking it. ``hallmark add -n`` previews
the matches, and ``hallmark ls-remote URL`` lists a directory and suggests
templates. Python uses ``repo.add(url_template)``.

Use ``hallmark clone SOURCE PATH`` for an existing Hallmark Git repository
or published HTTP/SFTP snapshot. Git clones retain the full catalog and its
history; snapshots start new local history. Use ``--source-type git`` or
``--source-type catalog`` to override automatic detection. The older ``build``
command remains available but is deprecated in favor of ``add`` with a URL
template.

Downloading remote data
-----------------------

Preview the selected files using the local catalog, then confirm a download::

   hallmark download --all --dry-run
   hallmark download --include 'runs/**'

Explicit cataloged paths and ``--tsv data.tsv`` also select files. Every nonempty
transfer requires interactive confirmation. ``init`` and ``add`` only create the
repository or catalog files; they never offer downloads. Each remote template's
files download from its source, so one plan can span several servers;
``--remote NAME`` selects a configured data remote for every file instead. CLI
cloning copies the complete catalog, then reviews a download plan by default.
Use ``clone --include`` to narrow that plan, ``clone --interactive`` for a
chooser, or ``clone --no-download`` to skip review. ``clone --output`` specifies
the download directory; bare catalogs prompt for one if omitted. These download
options cannot be combined with ``--no-download``; ``--interactive`` also
conflicts with ``--include``. ``download --include`` narrows the saved catalog
without changing it.
The old download ``--yes`` option does not bypass approval.

Use ``hallmark download --interactive`` to choose patterns or all cataloged files,
review recorded sizes, then download, revise, or skip. Add ``--dry-run`` to stop at
the preview. Without a terminal, clone keeps the catalog and prints commands for
later; standalone ``download --interactive`` reports an error. Python ``add``
and cloning remain metadata-only and never open this CLI chooser.

Python uses the same plan. First inspect the selected files::

   from hallmark import Repo

   repo = Repo.init('lab')
   repo.add('ssh://lab-data/srv/data/{group}/run{run:d}.h5', progress=True)
   plan = repo.plan_download(include='runs/**')
   print(plan.summary())

After reviewing the plan, approve the download and check for failures::

   result = repo.download(plan, approved=True, progress=True)
   if result['failed']:
       raise RuntimeError('\n'.join(result['errors']))

Plans preserve their selected sources, backend settings, destination and
checksums even when repository configuration later changes. Size estimates
require recorded file sizes; duration estimates also require a supplied
transfer rate.

.. automodule:: hallmark.download_plan
   :members:

Data backends
-------------

See :doc:`backends` for registration, installed plugins and the transfer
contract. Backend classes are also exported from ``hallmark``. Existing
``hallmark.transport`` imports remain compatibility aliases.

.. automodule:: hallmark.backends
   :members:
   :show-inheritance:

Dataset builders and data remotes
---------------------------------

.. autofunction:: hallmark.repo_builder.build_repo

.. autofunction:: hallmark.repo_builder.list_remote_files

SSH/SFTP uses standard ``~/.ssh/config`` and the SSH agent. Use URLs containing
the configured host alias; no Hallmark authentication profile is used.

The legacy ``download_remote_data`` function also requires ``approved=True``.
Both download APIs return a dictionary containing ``succeeded``,
``failed``, ``total_bytes``, and ``errors``. Check ``failed`` and ``errors``
for individual transfer failures. Configuration errors and failed SSH
connection checks raise ``DownloadError`` before downloads begin.
HTTP, SSH, and SFTP downloads use the same rules for destination paths
and checksum verification.

See :doc:`private_data` for CLI and Python examples, and
:ref:`private-transport-reference` for authentication settings and
supported server configurations.

Named data sources
------------------

List sources with ``hallmark sources`` and release/collection URLs with
``hallmark sources desi``, then catalog files beneath them with ``ls-remote`` and
``add``. See :doc:`private_data` for templates and migration.

.. autofunction:: hallmark.repo_remote.suggest_templates

.. automodule:: hallmark.sources
   :members: DataSource, SourceRelease, register_source, get_source, list_sources
