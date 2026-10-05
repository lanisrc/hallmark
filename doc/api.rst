API Reference
=============

This section contains the automatically generated API documentation for the
``hallmark`` package.

Core Repository
---------------

.. automodule:: hallmark.repo
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo.history
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo.config
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo.manifest
   :members:
   :show-inheritance:

Repository Worktrees
--------------------

.. automodule:: hallmark.repo.worktree
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo.changes
   :members:
   :show-inheritance:

State Management
----------------

.. automodule:: hallmark.repo.state
   :members:
   :show-inheritance:

Data Handling
-------------

.. automodule:: hallmark.paraframe
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo.objects
   :members:
   :show-inheritance:

Downloading
-----------

.. automodule:: hallmark.remote.download
   :members:
   :show-inheritance:

Utilities
---------

.. automodule:: hallmark.utils
   :members:
   :show-inheritance:

.. automodule:: hallmark.repo.dothm
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

   hm init ./local-project

Stage and commit a remote catalog without downloading dataset files::

   hm init ./lab
   cd ./lab
   hm add 'ssh://lab-data/srv/data/run{run:d}.h5'
   hm commit -m 'Add remote data'

Use ``hm clone SOURCE PATH`` for an existing Hallmark Git repository
or published HTTP/SFTP snapshot. Git clones retain the full catalog and its
history; snapshots start new local history. Use ``--source-type git`` or
``--source-type catalog`` to override automatic detection.

Downloading remote data
-----------------------

Preview the selected files using the local catalog, then confirm a download::

   hm download --all --dry-run
   hm download runs

Choose catalogued paths or folders, relative to the current folder, or
``--all`` or ``--tsv data.tsv``; ``--filter`` and ``--fmt`` only narrow that
selection, as in ``hm download --all --filter 'runs/**'``. Paths that are not
in the catalog stop the download before the server is contacted. Every
nonempty transfer in the CLI requires interactive confirmation, including the
default clone transfer. ``clone --no-download`` skips data and the prompt. Clone filters and
formats cannot be combined with this flag and leave the complete catalog unchanged.
Declining clone's prompt keeps the catalog and exits successfully.
A filter or filename format never authorizes a transfer.

``Repo.clone(url, path)`` downloads by default without prompting. Pass
``download=False`` for catalog-only or bare clones. An optional ``approve(plan)``
callback gates a nonempty transfer: only Boolean True downloads; refusal keeps
the catalog. A bare destination with downloads enabled fails before cloning.

For separate Python downloads, first inspect the selected files::

   from hallmark import Repo

   repo = Repo.init('lab')
   repo.add('ssh://lab-data/srv/data/', progress=True)
   repo.commit('Add remote data')
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

.. automodule:: hallmark.remote.plan
   :members:

Data backends
-------------

See :doc:`backends` for registration, installed plugins and the transfer
contract. Backend classes are also exported from ``hallmark``. Existing
``hallmark.transport`` imports remain compatibility aliases.

.. automodule:: hallmark.remote.backends
   :members:
   :show-inheritance:

Data remotes
------------

Associate a data remote with a local SSH authentication profile::

   repo.set_config(remote_name="campus", remote_auth="campus")

Remove the profile reference::

   repo.set_config(remote_name="campus", remote_auth="")

The repository stores the profile name. The SSH settings remain in the
local authentication file. Passing ``remote_auth=None`` leaves the
reference unchanged.

``Repo.download`` returns a dictionary containing ``succeeded``,
``failed``, ``total_bytes``, and ``errors``. Check ``failed`` and ``errors``
for individual transfer failures. Configuration errors and failed SSH
connection checks raise ``DownloadError`` before downloads begin.
HTTP, SSH, and SFTP downloads use the same rules for destination paths
and checksum verification.

See :doc:`private_data` for CLI and Python examples, and
:ref:`private-transport-reference` for authentication settings and
supported server configurations.
