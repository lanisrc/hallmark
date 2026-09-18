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

Clone a remote dataset without downloading dataset files::

   hallmark clone https://data.desi.lbl.gov/public/ ./desi --filter '**/*.fits'
   hallmark clone ssh://lab-data/srv/data/ ./lab --fmt 'run{run:d}.h5'

An existing Hallmark Git repository is cloned with its history. A published
HTTP/SFTP catalog snapshot starts new local history. Use ``--source-type git``
for a Git endpoint whose URL cannot be distinguished from a directory URL.
The older ``build`` command remains a deprecated compatibility interface.

Downloading remote data
-----------------------

Inspect the offline transfer plan and then confirm a download::

   hallmark download --all --dry-run
   hallmark download --filter 'runs/**'

Explicit paths and ``--tsv data.tsv`` also select files. Every nonempty transfer
requires interactive confirmation, including transfers requested through
``clone --download``. The old ``--yes`` option no longer bypasses approval.
A filter or filename format never authorizes a transfer.

Python uses the same plan and requires explicit approval::

   from hallmark import Repo

   repo = Repo.clone('ssh://lab-data/srv/data/', 'lab', progress=True)
   plan = repo.plan_download(filter='runs/**')
   print(plan.summary())
   result = repo.download(plan, approved=True, progress=True)

Plans preserve their selected remote, destination and checksums even when
repository configuration later changes. Size and time estimates remain unknown
when metadata or a credible throughput estimate is unavailable.

.. automodule:: hallmark.download_plan
   :members:

Dataset builders and data remotes
---------------------------------

.. autofunction:: hallmark.repo_builder.build_repo

.. autofunction:: hallmark.repo_builder.list_remote_files

Associate a data remote with a local SSH authentication profile::

   repo.set_config(remote_name="campus", remote_auth="campus")

Remove the profile reference::

   repo.set_config(remote_name="campus", remote_auth="")

The repository stores the profile name. The SSH settings remain in the
local authentication file. Passing ``remote_auth=None`` leaves the
reference unchanged.

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
