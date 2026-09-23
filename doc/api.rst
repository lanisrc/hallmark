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

Initialize a catalog from a remote dataset without downloading dataset files::

   hallmark init ./desi --from \
       https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/23040/ \
       --filter 'redrock-main-dark-23040.fits'
   hallmark init ./lab --from ssh://lab-data/srv/data/ --filter 'run*.h5' --format 'run{run:d}.h5'

``--filter`` selects catalog entries. Without ``--format``, filename templates
are detected automatically from the selected paths. An explicit ``--format``
overrides detection. Both retain unmatched files, with empty parameter values;
if no template can be inferred, paths and available metadata are still cataloged.
Python uses the same ``filter=`` and ``format=`` arguments to ``Repo.init``.

Use ``hallmark clone SOURCE PATH`` for an existing Hallmark Git repository
or published HTTP/SFTP snapshot. Git clones retain the full catalog and its
history; snapshots start new local history. Use ``--source-type git`` or
``--source-type catalog`` to override automatic detection. The older ``build``
command remains available but is deprecated in favor of ``init --from``.

Downloading remote data
-----------------------

Preview the selected files using the local catalog, then confirm a download::

   hallmark download --all --dry-run
   hallmark download --filter 'runs/**'

Explicit cataloged paths and ``--tsv data.tsv`` also select files. Every nonempty
transfer requires interactive confirmation. Initialization only creates the
repository or discovers its catalog; it never offers downloads. CLI cloning
copies the complete catalog, then reviews a download plan by default. Use
``clone --filter`` to narrow that plan, ``clone --interactive`` for a chooser,
or ``clone --no-download`` to skip review. ``clone --output`` specifies the download
directory; bare catalogs prompt for one if omitted. These download options cannot
be combined with ``--no-download``; ``--interactive`` also conflicts with ``--filter``.
``download --filter`` narrows the saved catalog without changing it.
The old download ``--yes`` option does not bypass approval.

Use ``hallmark download --interactive`` to choose patterns or all cataloged files,
review recorded sizes, then download, revise, or skip. Add ``--dry-run`` to stop at
the preview. Without a terminal, clone keeps the catalog and prints commands for
later; standalone ``download --interactive`` reports an error. Python
initialization and cloning remain metadata-only and never open this CLI chooser.

Python uses the same plan. First inspect the selected files::

   from hallmark import Repo

   repo = Repo.init('lab', source='ssh://lab-data/srv/data/', progress=True)
   plan = repo.plan_download(filter='runs/**')
   print(plan.summary())

After reviewing the plan, approve the download and check for failures::

   result = repo.download(plan, approved=True, progress=True)
   if result['failed']:
       raise RuntimeError('\n'.join(result['errors']))

Plans preserve their selected remote, backend settings, destination and
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

SSH/SFTP uses standard ``~/.ssh/config`` and the SSH agent. Set a remote URL
containing the configured host alias; no Hallmark authentication profile is used.

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

List sources with ``hallmark sources`` and release/collection roots with
``hallmark sources desi``. See :doc:`private_data` for selection and migration.

.. automodule:: hallmark.sources
   :members: DataSource, SourceRelease, register_source, get_source, list_sources
