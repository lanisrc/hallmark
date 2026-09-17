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

Building a repository
---------------------

Build a repository and choose filename formats interactively::

   hallmark build ./repositories EHTC_2018L1_Dec2024

Load formats and remotes from an existing configuration::

   hallmark build ./repositories EHTC_2018L1_Dec2024 \
       --config-file ./existing/config.yml

Provide filename formats directly::

   hallmark build ./repositories EHTC_EXAMPLE \
       --fmt "images/{source}_{date}.fits=data" \
       --fmt "README.{format}=readme"

Provide a custom remote or remotes::

   hallmark build ./repositories EHTC_EXAMPLE \
       --remote "origin=https://data.example.org/EHTC_EXAMPLE/" \
       --remote "backup=https://backup.example.org/EHTC_EXAMPLE/"

The generated repository is named ``DATASET_NAME.hm``. The ``--fmt`` option
may be repeated, but it cannot be combined with ``--config-file``.

Downloading remote data
-----------------------

Download specific remote-relative paths::

   hallmark download README.md data/example.fits

Download everything represented by a configured TSV::

   hallmark download --tsv data.tsv

Preview a complete repository download::

   hallmark download --all --dry-run

Large selections require confirmation unless ``--yes`` is supplied.

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

``download_remote_data`` returns a dictionary containing ``succeeded``,
``failed``, ``total_bytes``, and ``errors``. Check ``failed`` and ``errors``
for individual transfer failures. Configuration errors and failed SSH
connection checks raise ``DownloadError`` before downloads begin.
HTTP, SSH, and SFTP downloads use the same rules for destination paths
and checksum verification.

See :ref:`private-lab-data-over-ssh` for a CLI workflow, authentication
settings, and supported server configurations.
