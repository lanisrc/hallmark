Use Cases
=========

This section describes representative workflows supported by the
current |hallmark|_ CLI and Python API.


1. CLI: Standard Repository Ingest
----------------------------------

Alice is organizing telescope data in a normal directory.
She initializes that directory as a |hallmark|_ worktree::

    hallmark init obs
    cd obs

She adds the files using a python format-string pattern::

    hallmark add "{site}/{year:d}/{day:d}.fits"

She then commits the updated hallmark index::

    hallmark commit -m "Initial observation ingest"

She can inspect current repository paths at any time::

    hallmark info

She can also inspect current changes and commit history::

    hallmark status
    hallmark log

These git-like commands report or modify hallmark tracked state files
in ``.hm``.
Dataset files are represented through staged ``sha1`` column in
``data.tsv``, with other useful parameters (e.g., site, year, day),
associated with each file in different rows.


2. CLI: Bare Repository with Worktree Ingest
--------------------------------------------

Bob prefers managing a bare hallmark repository for storage and
staging data from a linked worktree.
He initialized the bare repository and verify its mode::

    hallmark init --bare sim.hm
    cd sim.hm
    hallmark info

He then attaches a worktree, stages discovered files, and commits::

    hallmark worktree add /data/outputs
    cd /data/outputs
    hallmark add "run{run:d}/frame{frame:d}.h5"
    hallmark status
    hallmark diff
    hallmark commit -m "Simulation snapshots"

The commit updates the same bare repository that owns the linked
worktree.


3. CLI: Branch-Isolated Analysis with Multiple Worktrees
--------------------------------------------------------

Carol wants to manage data from multiple simultaneous observations
without mixing data.
She creates a new branch and attach a second worktree to it::

    hallmark branch obs2
    hallmark worktree add remote:/data/obs obs2

She lists linked worktrees and continue on the new branch::

    hallmark worktree list
    hallmark status
    hallmark add "{site}/{year:d}/{day:d}.fits"
    hallmark commit -m "Observation ingest on branch obs2"

Each worktree stays isolated by branch, so staged state and commits do
not interfere.


4. Python: Programmatic Repository Updates
------------------------------------------

David uses the Python API for scripted ingest against an on-disk
repository::

    from hallmark import Repo

    repo = Repo.open("obs")
    info = repo.info()
    print(info.local_path, info.worktree_path)

    for y in range(2000,2026,5):
        repo.add(f"{{site}}/{y}/{{day:d}}.fits")  # escape {{ and }}
    repo.commit("Nightly ingest")

This workflow is suitable for finer control of data ingest.


5. Python: In-Memory State Workflows
------------------------------------

Emma can use an memory-backed facade for data transformations that do
not require git operations::

    from hallmark import Repo

    repo = Repo()
    repo.add("data/{site}/{year:d}/{day:d}.fits")
    repo.worktree("data_transformed/{year:d}/{day:d}/{site}.fits")

This is especially useful for data (re-)organization.


..  |hallmark| replace:: ``hallmark``

..  _hallmark: https://github.com/l6a/hallmark


.. _private-transport-reference:

.. _private-lab-data-over-ssh:

6. CLI: Private Lab Data over SSH
----------------------------------

For a step-by-step CLI workflow and Python examples, see :doc:`private_data`.

Frank works with simulation data stored on a private lab server.
He has an existing |hallmark|_ repository that indexes the files and
wants to download them to his local worktree.
He first configures the SSH alias ``campus``, verifies the server's host
key, and loads his key into an SSH agent or configures an identity file.

From the local repository, he sets the data location, previews the
selection, and downloads the files::

    hallmark set-config --remote-name campus --remote-url ssh://campus/srv/export/
    hallmark download --remote campus --all --dry-run
    hallmark download --remote campus --all

The data remote ``campus`` specifies where the dataset files are stored.
It is configured separately from the Git remote used to share the
history in ``.hm``, even when both remotes have the same name.
The ``--remote`` option selects one data server; |hallmark|_ does not
automatically try another server if the download fails.

Downloads are written to temporary files and checked against any
checksums supplied with the selection before atomically replacing
their destinations.
The ``--tsv`` and ``--all`` options include catalog checksums. Explicit
paths also use their recorded checksums when available.
If a transfer or checksum check fails, the existing destination is
preserved and the temporary file is removed.
Files that have already downloaded successfully remain available.

SSH access and file paths
~~~~~~~~~~~~~~~~~~~~~~~~~~

The client requires OpenSSH ``ssh`` and ``sftp`` version 9.6 or newer
with connection multiplexing enabled.
Linux and macOS have integration-test jobs. Windows SSH transport is
not supported.
HTTP downloads continue to use Requests, including its environment
settings and ``.netrc`` authentication.

|hallmark|_ uses ``BatchMode=yes`` and defaults to
``StrictHostKeyChecking=yes``. Authentication must succeed without
password prompts, interactive two-factor authentication, or an initial
hardware-key prompt. Complete any interactive setup before downloading.
Each invocation opens and closes its own shared SSH connection, leaving
existing SSH connections alone. Settings such as ``ProxyJump`` belong
in the local SSH configuration.

The URL must specify an absolute dataset directory.
Both ``ssh://`` and ``sftp://`` use SFTP for file transfers.
Usernames and ports can be included, as in
``ssh://researcher@campus:2222/srv/export/``.
IPv6 addresses require brackets, as in
``ssh://[2001:db8::1]/srv/export/``.
SCP shorthand and paths relative to a home directory are not supported.

Reserved characters in URL paths must be percent-encoded, e.g.,
``has%20space%23tag``. Paths in catalogs and checksum manifests are
literal: ``literal%20.h5`` names a file containing a percent sign.
Spaces, quotes, glob characters, and leading dashes are supported;
control characters and backslashes are rejected.
Existing destination symlinks and detected changes to symlinks are
rejected, although these checks cannot prevent every race with another
process modifying the destination.

Cancelling a download stops the SSH processes started by |hallmark|_,
waits for the download workers, and closes its shared connection.
HTTP workers check for cancellation between chunks. A pending Requests
call may wait for its 10-second connection timeout or 30-second read
timeout; a response that keeps sending small amounts of data can delay
cancellation further.

Local profiles
~~~~~~~~~~~~~~

Frank can keep SSH settings in a local authentication profile and
record its name in the repository.
|hallmark|_ reads profiles from ``$HALLMARK_AUTH_FILE`` when set,
otherwise from ``$XDG_CONFIG_HOME/hallmark/auth.yml``, or from
``~/.config/hallmark/auth.yml`` if neither variable is set.
Profiles apply to SSH and SFTP connections.

For example, his authentication file can contain:

.. code-block:: yaml

    version: 1
    defaults:
      connect_timeout: 10
      transfer_timeout: 3600
      shutdown_timeout: 2
      max_sessions: 4
    profiles:
      campus:
        hosts: [campus]
        user: researcher
        port: 22
        identity_file: ~/.ssh/id_ed25519
        host_key_policy: strict

He associates the profile with the data remote::

    hallmark set-config --remote-name campus --remote-auth campus

He can remove the reference later::

    hallmark set-config --remote-name campus --remote-auth ''

The Python equivalents use ``repo.set_config`` with
``remote_name="campus"`` and ``remote_auth="campus"`` or ``remote_auth=""``.
Passing ``None`` leaves the profile reference unchanged.
The repository stores only the profile name; identity settings remain local.

Profile names must match ``[A-Za-z0-9_-]{1,64}``.
The ``hosts`` list contains the aliases or hostnames used in data URLs,
before SSH resolves ``HostName``.
A missing profile or a host outside this list causes an error before
connecting. A username or port in the URL overrides the profile setting;
remaining values come from SSH configuration and OpenSSH defaults.

Profiles accept the fields shown above and may override the four
timeout and concurrency defaults. Global defaults cannot set ``hosts``,
``user``, ``port``, or ``identity_file``.
Passwords, tokens, arbitrary SSH options, executable paths, and SSH
configuration paths are not supported profile fields.

``transfer_timeout`` limits the total transfer time for each file in
seconds, so large datasets may need a higher value.
``max_sessions`` limits concurrent transfers within an operation to
respect the server's session limit.
The local profile or defaults may use ``host_key_policy: accept-new``
to accept a host key on first contact. Changed host keys still cause
an error. Repository configuration cannot enable this setting.

Clone a private catalog
~~~~~~~~~~~~~~~~~~~~~~~

Frank can prepare a catalog directly from the files on the server::

    hallmark clone ssh://campus/srv/export/ lab \
      --auth campus --fmt 'runs/run_{i:d}.h5'
    cd lab
    hallmark download --all --dry-run
    hallmark download --all

This creates ``lab/.hm`` with only the matching run files in ``data.tsv``.
Without a filter or format, the catalog contains all discovered files below
the supplied URL. No dataset contents are downloaded while cloning.
The final command displays the transfer plan and asks Frank for confirmation.

Both SSH and SFTP sources use structured SFTP directory enumeration. The server
needs no login shell, Python or Hallmark, and SFTP-only accounts work for
both discovery and downloads. Symlinks and special files are skipped.
Authentication and permission failures stop discovery rather than producing
an apparently complete catalog.

The generated data remote ``origin`` records the source URL and optional
profile name.
Existing Git-hosted catalogs keep their recorded data remotes when cloned.
A filter on an existing Git catalog creates a local metadata commit while
preserving fetched history; it does not fetch the dataset's content objects.

Published manifest checksums are recorded in the catalog. Missing checksums
remain unknown; |hallmark|_ does not read dataset files locally or on the
server to compute them during discovery. Downloads verify any available
publisher checksum. Discovery covers the requested tree without an overall
time or entry limit. Individual requests have limits, and discovery can
be cancelled.

CyVerse and common HTTPS directory indexes are detected automatically::

    hallmark clone https://data.desi.lbl.gov/public/ desi --filter '**/*.fits'

A server with no usable listing must provide a published Hallmark catalog.
Discovery cannot find files whose URLs are absent from both the listing and
catalog. HTTP/SFTP catalog snapshots start local history; Git endpoints
supply catalog history. Use ``--source-type git`` for ambiguous Git URLs.

Frank can also inspect and approve a transfer in Python::

    from hallmark import Repo

    repo = Repo('lab')
    plan = repo.plan_download(filter='runs/run_1.h5')
    print(plan.summary())
    result = repo.download(plan, approved=True, progress=True)

Plans are built from local metadata without contacting the server. Missing
sizes and duration estimates are reported as unknown. During transfer,
progress reports bytes and estimates remaining time when possible.
``build`` and ``build_repo`` remain deprecated compatibility interfaces.
Local ``add``, ``commit`` and ``checkout`` retain their existing format and
local-object requirements; preparing a remote catalog does not populate the
local object store.

Transport validation
~~~~~~~~~~~~~~~~~~~~

The regular test suite runs with ``pytest -q``.
SFTP parser tests use a local ``sftp-server`` process without networking.
To run the complete OpenSSH integration tests::

    HALLMARK_RUN_SSH_TESTS=1 pytest test/test_ssh_integration.py -q

These tests use a loopback server, temporary keys and data, and separate
SSH configuration and known-hosts files.
They do not connect to a lab server or change the user's SSH trust files.
Enabling the tests requires the OpenSSH tools to be installed; missing
tools cause a test failure.
CI installs the server and runs these tests separately from the
Python-version matrix.
