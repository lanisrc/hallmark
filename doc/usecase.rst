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


Private lab data over SSH
-------------------------

A **data remote** locates dataset bytes. A **Git remote** synchronizes the
catalog's history in ``.hm``. They are separate configurations, even when both
are named ``origin``. Selecting ``download --remote campus`` selects only that
data remote; Hallmark does not automatically fall back to another server.

Use OpenSSH ``ssh`` and ``sftp`` 9.6 or newer on Linux. Linux and macOS have
integration jobs; macOS support should be checked against that job's result.
Windows SSH transport is not supported. Requests remains required for HTTP,
including its existing environment and ``.netrc`` authentication behavior.

Before downloading, configure your SSH alias, trust the server's host key, and
load a usable key into your agent (or configure an identity file). Workers use
``BatchMode=yes`` and default to ``StrictHostKeyChecking=yes``. Password prompts,
interactive 2FA, and an initial interactive hardware-key challenge cannot be
answered by Hallmark. Complete those prerequisites separately. Hallmark owns a
new multiplexed connection for each invocation and closes it afterward; existing
user-owned masters are neither reused nor stopped. Multiplexing is required.
ProxyJump and other trusted SSH policy remain in your local SSH configuration.

Configure an absolute dataset root::

    hallmark set-config --remote-name campus --remote-url ssh://campus/srv/export/
    hallmark download --remote campus --all --dry-run
    hallmark download --remote campus --all

``sftp://`` is an alias for ``ssh://``. Explicit users and ports are supported,
for example ``ssh://researcher@campus:2222/srv/export/``. IPv6 addresses need
brackets: ``ssh://[2001:db8::1]/srv/export/``. SCP shorthand and implicit
home-relative roots are unsupported. URL paths must encode reserved characters,
e.g. ``has%20space%23tag``. Catalog/manifest paths are literal: ``literal%20.h5``
names a file containing a percent sign. Spaces, quotes, glob characters, and
leading dashes are supported; control characters and backslashes are rejected.

Downloads stream into temporary files, verify recorded checksums, then replace
the requested destinations atomically. A failed transfer or checksum mismatch
preserves an existing destination and removes its temporary file. Other files
that already succeeded remain available. Cancellation terminates owned SSH
process groups, waits for workers and closes the master. HTTP workers check
cancellation between chunks, but blocked Requests calls can take their normal
``(10, 30)`` connect/read timeouts to return. A slowly trickling response can
keep a blocked read alive longer; no fixed five-second HTTP
cancellation guarantee is made. Existing destination symlinks and observed
symlink swaps are rejected. This is not protection against every race with a
concurrently hostile local writer.

Local profiles
~~~~~~~~~~~~~~

Profiles live in ``$HALLMARK_AUTH_FILE``, otherwise
``$XDG_CONFIG_HOME/hallmark/auth.yml``, otherwise
``~/.config/hallmark/auth.yml``. Repository configuration stores only the profile
name. Profiles currently apply to SSH/SFTP, with this strict schema:

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

Supported profile fields are ``hosts`` and the settings shown above; profiles
may also override the four time/concurrency defaults. Global defaults cannot
set ``hosts``, ``user``, ``port`` or ``identity_file``. Passwords, tokens, arbitrary
SSH options, executables and SSH config paths are not supported fields. The
profile name must match ``[A-Za-z0-9_-]{1,64}``. A missing profile or a URL host
outside its ``hosts`` list fails before connections start. URL user/port override
profile defaults, then SSH configuration and OpenSSH defaults apply. Host bindings
refer to the URL alias or literal host, before SSH config resolves ``HostName``.

``transfer_timeout`` is a total wall-clock budget per file in seconds, not an
idle/read timeout; increase it locally for large datasets. ``max_sessions`` caps
concurrent transfers per operation to respect the server's session limit.
Only a local profile/default may select ``host_key_policy: accept-new`` for
first-contact trust; changed host keys still fail.

Associate or remove a profile reference::

    hallmark set-config --remote-name campus --remote-auth campus
    hallmark set-config --remote-name campus --remote-auth ''

The Python equivalents are ``repo.set_config(remote_auth="campus")`` and
``repo.set_config(remote_auth="")``. ``None`` leaves the reference unchanged.
No resolved identity settings are written into the repository.

Build a private catalog
~~~~~~~~~~~~~~~~~~~~~~~

The crawl source is independent of remotes recorded in the resulting catalog.
``--dataset-url`` is the exact dataset root; the dataset name is only a label::

    hallmark build catalogs lab \
      --dataset-url ssh://campus/srv/export/ \
      --dataset-auth campus --allow-remote-commands \
      --fmt 'runs/run_{i:d}.h5=data.tsv'

SSH builds require a POSIX login shell and Python 3 on the server. The explicit
``--allow-remote-commands`` flag permits fixed read-only recursive listing and,
when requested, hashing commands. Repository configuration cannot enable this
permission. SFTP-only accounts support downloads of cataloged files but cannot
build catalogs. Listings omit symlinks and special files, including symlinked
directories. Remote roots are namespaces; enforce access boundaries on the server.

Existing checksum manifests are preferred. Supported manifests use unescaped GNU
sum lines: a hexadecimal digest, a space, a space or ``*`` marker, then the literal
filename. Strong algorithm names such as ``sha256`` are preserved. GNU escaped
filename records are rejected. Missing digests remain absent/``unknown``; auth and
ambiguous SSH failures are errors, never treated as an absent optional manifest.

Add ``--remote-hash`` to compute server SHA-256 for files without a manifest hash.
Hashing is limited to 10 MiB per file, 100 MiB total and a 60-second operation
budget. Larger files or exhausted budgets retain unknown digests. Obvious file
size/mtime changes during hashing fail. Recursive listing has a 100,000-file and
16 MiB output limit with a 60-second command timeout. Text reads have a 16 MiB
per-file and 64 MiB aggregate limit; new reads stop after a five-minute crawl
budget. No dataset file is downloaded merely to compute an SSH checksum.

When no output remotes are supplied, the synthesized ``origin`` records the
source URL and its profile reference. ``--remote NAME=URL`` records a different
output location without changing the crawl source or inheriting its auth profile.
A name-only output remote defaults to the crawl URL but must be configured with
its own auth reference if needed.

Without ``--dataset-url``, legacy CyVerse dataset-name lookup is preserved.
An explicit HTTP root requires ``--index-format cyverse-html`` and must expose
that HTML index dialect. Arbitrary HTTP directory listing and WebDAV PROPFIND
are not implemented. Static HTTP checksums retain the bounded small-file MD5
fallback. Local add/commit/checkout workflows still require one format backed
by ``data.tsv``; builder and download workflows may use multiple catalogs.

Use immutable exports or server snapshots when reproducibility matters: crawling
and hashing a live directory cannot produce a guaranteed snapshot. Subsequent
downloads always verify whatever digest the catalog records.

Transport validation
~~~~~~~~~~~~~~~~~~~~

Run ordinary tests with ``pytest -q``. The SFTP parser tests use a local
``sftp-server`` process without networking. The complete OpenSSH tests use only
loopback sockets, ephemeral keys, isolated known-hosts/config files and temporary
data::

    HALLMARK_RUN_SSH_TESTS=1 pytest test/test_ssh_integration.py -q

The opt-in fails if required tools are unavailable. It never connects to a live
lab server or changes normal SSH trust files. CI installs OpenSSH server and
runs this suite separately from the Python-version matrix.
