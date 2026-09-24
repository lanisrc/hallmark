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


2. CLI: Bare Repository with Explicit Downloads
-----------------------------------------------

Bob prefers keeping the data index separate from downloaded files.
He initializes a bare |hallmark|_ catalog, adds a simulation export from a
campus server, and verifies its location::

    hallmark init sim.hm
    cd sim.hm
    hallmark add 'ssh://campus/srv/export/run{run:d}/frame{frame:d}.h5'
    hallmark commit -m "Catalog simulation export"
    hallmark info

The ``.hm`` suffix selects a bare catalog without a data worktree.
``add`` records ``ssh://campus/srv/export/`` as the source of the template
``run{run:d}/frame{frame:d}.h5`` and catalogs the matching files without
downloading them.
Bob reviews the selection and approves downloading to an explicit directory::

    hallmark download --all --output ../outputs --dry-run
    hallmark download --all --output ../outputs

The catalog stays in ``sim.hm``; the downloaded files go to ``outputs``.
For local ingest and versioning, Bob can initialize ``outputs`` as a
standard repository and follow Alice's workflow.


3. Python: Branch-Isolated Analysis with Multiple Worktrees
-----------------------------------------------------------

Carol wants to manage data from multiple simultaneous observations
without mixing data.
Starting from a committed standard repository, she creates a new branch
and attaches a second worktree through the Python API::

    from hallmark import Repo

    repo = Repo("obs")
    repo.add_worktree("obs2")
    second = Repo(repo.worktree.parent / "obs2")

After adding observation files beneath ``obs2``, she stages and commits
them in that worktree::

    second.add("{site}/{year:d}/{day:d}.fits")
    second.commit("Observation ingest on branch obs2")

Each worktree stays isolated by branch, so staged state and commits do
not interfere.


4. Python: Programmatic Repository Updates
------------------------------------------

David uses the Python API for scripted ingest against an on-disk
repository::

    from hallmark import Repo

    repo = Repo("obs")
    print(repo.dothm.path, repo.worktree)

    repo.add("{site}/{year:d}/{day:d}.fits")
    repo.commit("Nightly ingest")

This workflow is suitable for finer control of data ingest.


5. Python: In-Memory State Workflows
------------------------------------

Emma can use ``ParaFrame`` to discover and select files without
creating a Git repository::

    from hallmark import ParaFrame

    data = ParaFrame.parse("{site}/{year:d}/{day:d}.fits", base_path="data")
    selected = data.filter(year=2025)
    print(selected)

This keeps the file index in memory. Emma can pass the selected paths to
her analysis tools; filtering the index does not modify the data files.


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

SSH configuration
~~~~~~~~~~~~~~~~~

Frank keeps connection settings in ``~/.ssh/config``. Hallmark passes the URL's
host alias to OpenSSH, which resolves the user, port, identity and any proxy.
Explicit URL user and port values take precedence. Hallmark does not read a
separate authentication file or prompt to create one.

Catalog files on the server
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Frank can catalog the run files directly on the server::

    hallmark init lab
    cd lab
    hallmark ls-remote ssh://campus/srv/export/runs/
    hallmark add 'ssh://campus/srv/export/runs/run_{i:d}.h5'
    hallmark commit -m "Catalog lab runs"
    hallmark download --all --dry-run
    hallmark download --all

``ls-remote`` lists the directory and suggests templates without changing the
repository. ``add`` records ``ssh://campus/srv/export/runs/``, the URL up to
the first path segment with a field, as the template's source, and catalogs
only the files matching ``run_{i:d}.h5``. They download to ``lab/run_1.h5``
and so on; to keep the ``runs`` directory in the local layout, make it part of
the template, as in ``ssh://campus/srv/export/{group}/run_{i:d}.h5``.
Fields never span a ``/``, and only directories the template can match are
listed. A template that matches nothing is an error, and a file is cataloged
only if the template re-creates its name exactly; ``hallmark add -n`` previews
the matches. No dataset contents are downloaded.
The final command displays the transfer plan and asks Frank for confirmation.

Both SSH and SFTP sources use structured SFTP directory enumeration. The server
needs no login shell, Python or Hallmark, and SFTP-only accounts work for
both listing and downloads. Symlinks and special files are skipped.
Authentication and permission failures stop listing rather than producing
an apparently complete catalog.

When the server changes, Frank runs the same ``add`` command again. It syncs
the template's catalog: new files are added, changed sizes or checksums are
updated, and files no longer on the server are dropped. ``hallmark status``
summarizes these catalog changes per template before he commits them.

Frank keeps his reduced products in the worktree beside the server data. A
branch can track several templates, local and remote::

    hallmark add 'reduced/run_{i:d}.dat'
    hallmark commit -m "Add reduced runs"

Local templates are committed as objects, so ``checkout`` restores their
files. Remote templates are committed as catalog entries only: checkout never
removes or restores downloaded copies, and ``status`` does not report missing
or downloaded copies as changes. After switching branches, downloading again
replaces a stale copy. ``hallmark rm --cached TEMPLATE`` stops tracking a
template and leaves its files in place.

The data entry records the template's source URL. Data remotes configured
with ``set-config`` act as mirrors: ``download --remote NAME`` downloads every
selected file from that remote instead. Local templates download from the
default data remote, when one is configured.
Existing Git-hosted catalogs keep their complete catalog, history and
recorded sources when cloned. Select and approve transfers afterward with
``download --include``; downloads leave the catalog and history unchanged.
The Git catalog can be hosted separately from its data servers.

Published manifest checksums are recorded in the catalog. Missing checksums
remain unknown; |hallmark|_ does not read dataset files locally or on the
server to compute them during discovery. Downloads verify any available
publisher checksum. Discovery covers the requested tree without an overall
time or entry limit. Individual requests have limits, and discovery can
be cancelled.

CyVerse and common HTTPS directory indexes are detected automatically::

    hallmark add \
        'https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/{pixel}/redrock-main-dark-{pixel}.fits'

A server with no usable listing needs a published Hallmark catalog or a
backend plugin that understands its API. See :doc:`backends` for the shared
interface and registration. Discovery cannot infer hidden file URLs.
HTTP/SFTP catalog snapshots start local history; Git endpoints supply catalog
history. Use ``--source-type git`` for ambiguous Git URLs.

Frank can also inspect and approve a transfer in Python::

    from hallmark import Repo

    repo = Repo('lab')
    plan = repo.plan_download(include='run_1.h5')
    print(plan.summary())

After reviewing the plan, he approves the download and checks for failures::

    result = repo.download(plan, approved=True, progress=True)
    if result['failed']:
        raise RuntimeError('\n'.join(result['errors']))

Plans are built from local metadata without contacting the server. Missing
sizes and duration estimates are reported as unknown. During transfer,
progress reports bytes and estimates remaining time when possible.
``build`` and ``build_repo`` remain available but are deprecated in favor of
``add`` with a URL template.
Cataloging remote files does not populate the local object store. To version
downloaded files locally, stop tracking the remote template with
``hallmark rm --cached`` and add the same template as a local one.

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
