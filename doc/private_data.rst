.. _private-data:

Private data over SSH and SFTP
==============================

|hallmark|_ can discover a remote dataset, prepare a local catalog, and download
selected files after approval. ``clone`` accepts both an existing Hallmark
catalog and an ordinary data directory. Its default result is a local ``.hm``
with no dataset files. A **data remote** identifies the dataset location;
it is separate from the Git remote used to share catalog history.

The SSH examples use the alias ``lab-data`` and the absolute dataset root
``/srv/exports/lab/``. Replace the hostname, username, identity path and root
with your own values. Run Hallmark locally. The server needs SFTP access, but
does not require Hallmark, Git, Python or a login shell.

1. Configure and check SSH access
---------------------------------

Install the checkout with ``python -m pip install -e .``. The client requires
system OpenSSH ``ssh`` and ``sftp`` version 9.6 or newer, with connection
multiplexing available. Linux and macOS have integration-test jobs; Windows
SSH transport is unsupported.

Add an entry to your local ``~/.ssh/config``:

.. code-block:: text

   Host lab-data
       HostName data.example.org
       User researcher
       IdentityFile ~/.ssh/id_ed25519
       IdentitiesOnly yes
       StrictHostKeyChecking yes

Have your administrator provision the server's verified host key in
``known_hosts``, or verify its fingerprint through a trusted channel before
accepting it. Load an encrypted key into your SSH agent before running Hallmark.
Check that SFTP works without an interactive prompt:

.. code-block:: bash

   ssh -V
   sftp -o BatchMode=yes -b /dev/null lab-data

Hallmark uses noninteractive authentication and strict host-key checking. It
cannot answer password, MFA or initial hardware-key prompts. Ordinary SSH
configuration, including ``ProxyJump``, remains available.

2. Discover a remote catalog
----------------------------

Suppose the export contains ``runs/run_001.h5``, ``runs/run_002.h5`` and
``README.md``. From an empty local workspace, run:

.. code-block:: bash

   hallmark clone 'ssh://lab-data/srv/exports/lab/' ./lab \
       --fmt 'runs/run_{run:03d}.h5'

This creates ``./lab/.hm`` and catalogs only the matching run files. The URL
is the exact discovery root. A filename format selects paths and extracts
parameters; it does not approve a download. A glob filter can select paths
without defining parameters:

.. code-block:: bash

   hallmark clone 'sftp://lab-data/srv/exports/lab/' ./lab-h5 \
       --filter '**/*.h5'

Without ``--filter`` or ``--fmt``, discovery recursively covers every directory
beneath the URL and catalogs all discovered files. A filter still traverses
directories needed to find matching files. Discovery shows an indeterminate
progress bar with counts of completed directories and discovered files.
The percentage and completion time remain unknown until a total is available.

Both URL schemes use structured SFTP directory enumeration and file transfers.
SFTP-only accounts work for discovery as well as downloading. No
``--allow-remote-commands`` or ``--remote-hash`` option is needed. Discovery
reads listings and published checksum manifests without reading dataset files
to compute checksums. Missing checksums remain unknown. For reproducible
catalogs, publish an immutable export with a SHA-256 manifest, for example
``<digest>  runs/run_001.h5`` records in ``SHA256SUMS``.

The generated data remote is named ``origin`` and points to the source URL.
Use ``hallmark init PATH`` to initialize a local repository. The older remote
``build`` command and ``build_repo`` API are deprecated in favor of ``clone``;
server-side hashing is no longer part of discovery.

3. Preview, approve and download
--------------------------------

Continue from the same workspace:

.. code-block:: bash

   cd lab
   hallmark info
   hallmark download --tsv data.tsv --dry-run
   hallmark download runs/run_001.h5

The dry run reads only the local catalog. It reports file count, known bytes,
the number of unknown file sizes, destination and source. The CLI reports
duration as unknown. Python callers can supply a transfer rate to estimate
duration when all file sizes are known. The dry run does not test credentials,
host trust or remote-file existence.

Every nonempty CLI download displays its plan and asks
``Download these files? [y/N]``. Answer ``y`` to authorize the displayed
transfer. Refusal, a blank answer or end-of-input transfers no dataset files.
The deprecated ``--yes`` option is accepted but does not bypass confirmation.
During transfer, byte progress and measured throughput provide an ETA when the
total is known; unknown totals remain indeterminate.

Choose a scope from inside ``lab``:

.. code-block:: bash

   hallmark download --filter 'runs/run_00[12].h5' --dry-run
   hallmark download --all --output ./payload

Explicit paths use their recorded catalog checksums when available, just like
``--tsv`` and ``--all``. A path absent from the catalog can be requested, but
its size and checksum are unknown. Files without a usable publisher checksum
can be downloaded, but their contents cannot be checked against the catalog.

``--tsv`` can be repeated and combined with explicit paths; overlapping entries
are deduplicated. ``--all`` cannot be combined with either selection mode.
Relative output paths are relative to the current directory. Without
``--output``, downloads go to the repository worktree; bare ``.hm`` repositories
require an explicit output directory. Repeating a download transfers the
selection again and replaces destinations only after successful transfer and
any recorded checksum verification.

To request downloads during cloning, add ``--download``. Catalog preparation
finishes first, then the same plan and approval prompt appear:

.. code-block:: bash

   hallmark clone 'ssh://lab-data/srv/exports/lab/' ../lab-one \
       --filter 'runs/run_001.h5' --download

An existing catalog can be cloned from a local path, a Git endpoint, or an
HTTP/SFTP directory containing its metadata:

.. code-block:: bash

   hallmark clone ./.hm ../lab-copy
   hallmark clone 'https://git.example.org/team/lab.git' ../lab-history
   hallmark clone 'https://data.example.org/catalogs/lab.hm/' ../lab-snapshot

Git endpoints preserve history. Published HTTP/SFTP snapshots start new local
history. For a Git URL without an obvious ``.git`` suffix, specify
``--source-type git``; ``--source-type catalog`` or ``directory`` explicitly
selects a published snapshot or discovery. Filtering a Git clone preserves
fetched history and commits the filtered catalog locally. Git authentication
and data-server authentication are independent. ``--no-fetch-data`` remains a
compatibility alias for the default metadata-only behavior.

4. Use a local authentication profile
-------------------------------------

Profiles are optional when SSH configuration already provides access. They let
each collaborator supply credentials locally while the catalog records only a
profile name. Add the following to ``~/.config/hallmark/auth.yml``, merging it
with any existing profiles:

.. code-block:: yaml

   version: 1
   profiles:
     lab:
       hosts: [lab-data]
       user: researcher
       identity_file: ~/.ssh/id_ed25519
       host_key_policy: strict
       connect_timeout: 10
       transfer_timeout: 3600
       shutdown_timeout: 2
       max_sessions: 4

Keep this file outside the repository and restrict it to your account, for
example with ``chmod 600 ~/.config/hallmark/auth.yml``. Hallmark reads
``$HALLMARK_AUTH_FILE`` when set, otherwise
``$XDG_CONFIG_HOME/hallmark/auth.yml``, otherwise the path above. Every client
using a catalog's profile reference must define that profile locally.

Associate an existing data remote with a profile, or select one when cloning
an ordinary remote directory:

.. code-block:: bash

   hallmark set-config --remote-name origin --remote-auth lab
   hallmark download --remote origin --tsv data.tsv --dry-run
   hallmark clone 'ssh://lab-data/srv/exports/lab/' ../lab-profile --auth lab

The new catalog's ``origin`` remote records ``auth: lab``. Clear a reference with
``hallmark set-config --remote-name origin --remote-auth ''`` to use SSH
configuration alone. ``--auth`` applies to data access; Git cloning uses Git's
own authentication configuration.

``hosts`` binds the profile to the URL's alias or literal hostname before
``HostName`` resolution. This example binds to ``lab-data``, not
``data.example.org``. Explicit URL user/port values override profile settings;
unset values use SSH configuration. Passwords, tokens and arbitrary
SSH options are not valid profile fields. ``transfer_timeout`` is the total
per-file time budget in seconds, not an idle timeout. Effective SSH download
concurrency is the smaller of ``--max-workers`` and local ``max_sessions``
(both default to 4).

5. Use the Python API
---------------------

These examples use fresh local destinations. Like the CLI, ``Repo.clone``
discovers metadata without downloading dataset files:

.. code-block:: python

   from hallmark import Repo

   repo = Repo.clone(
       "ssh://lab-data/srv/exports/lab/", "lab-python",
       auth="lab",  # Omit when SSH configuration already supplies access.
       fmt="runs/run_{run:03d}.h5",
       progress=True,
   )
   plan = repo.plan_download(file_paths=["runs/run_001.h5"])
   print(plan.summary())
   print(plan.items)  # Paths, checksums, optional size_bytes and mtime.

Inspect the plan, then approve the download:

.. code-block:: python

   result = repo.download(plan, approved=True, progress=True)
   if result["failed"]:
       raise RuntimeError("\n".join(result["errors"]))

Without ``approved=True``, a nonempty transfer raises ``DownloadError`` before
opening a connection. A plan freezes the selected files, source URL, profile
reference and destination; later catalog or remote configuration changes do not
redirect it. ``plan_download(filter="**/*.h5")`` scopes the complete catalog;
``plan_download(output_path="subset", tsv_names=["data.tsv"])`` selects a TSV
and destination. Planning makes no network requests. Known size totals and
unknown-size counts are available as ``known_bytes`` and
``unknown_size_count``. ``estimated_seconds`` stays ``None`` unless all sizes
are known and ``estimated_bytes_per_second`` was supplied when planning.

For downloads during cloning, supply a callback that reviews the completed
plan and returns ``True`` to approve:

.. code-block:: python

   def approve(plan):
       print(plan.summary())
       return input("Download these files? [y/N] ").strip().lower() == "y"

   repo = Repo.clone(
       "ssh://lab-data/srv/exports/lab/", "lab-with-download",
       filter="runs/run_001.h5", download=True, approve=approve, progress=True,
   )

``repo.download`` returns ``succeeded``, ``failed``, ``total_bytes`` and
``errors``. Inspect ``failed`` because successful files remain when another
file fails. Setup failures raise ``hallmark.downloader.DownloadError``.
``repo.set_config(remote_auth="")`` removes a profile reference; ``None``
leaves it unchanged.

6. Discover public HTTPS collections
------------------------------------

The same workflow supports CyVerse and ordinary browsable HTTPS collections
such as DESI. No CyVerse-specific index option is required:

.. code-block:: bash

   hallmark clone \
       'https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/EHTC_FirstM87Results_Apr2019/' \
       ./cyverse --filter '**/*.uvfits'
   hallmark clone 'https://data.desi.lbl.gov/public/' ./desi --filter '**/*.fits'

Each URL is the full recursive discovery root. A broad root can require many
listing requests even with a file filter; use a specific subtree when that is
your intended scope. Public collections need no auth profile. Private HTTP
access retains Requests' normal ``.netrc`` and environment behavior; Hallmark's
named auth profiles apply to SSH/SFTP.

From either clone, inspect and approve a selected subset using
``hallmark download --filter PATTERN --dry-run`` and then the same command
without ``--dry-run``. Python follows the same steps:

.. code-block:: python

   cyverse = Repo.clone(
       "https://data.cyverse.org/dav-anon/iplant/commons/"
       "cyverse_curated/EHTC_FirstM87Results_Apr2019/",
       "cyverse-python", filter="**/*.uvfits", progress=True,
   )
   desi = Repo.clone(
       "https://data.desi.lbl.gov/public/", "desi-python",
       filter="**/*.fits", progress=True,
   )
   plan = desi.plan_download(filter="dr1/**/selected-file.fits")
   print(plan.summary())
   # Replace the illustrative pattern with an actual catalog path, inspect,
   # and authorize only the intended selection:
   # result = desi.download(plan, approved=True, progress=True)

HTTP provides file transfer but does not define directory enumeration. Hallmark
recognizes supported HTML indexes and directory landing pages automatically.
If a server supplies no usable listing, provide a published Hallmark catalog
or a more specific browsable URL; Hallmark cannot
infer hidden file URLs.

.. _operational-behavior-and-troubleshooting:

Download behavior and troubleshooting
-------------------------------------

Files are downloaded to temporary paths and published atomically after transfer
and any recorded checksum verification. A failed transfer preserves an existing
destination and removes its temporary file. Successful files remain available
when another file fails; a multi-file download is not a single transaction.
The CLI exits nonzero on transfer failures. Cancellation stops the active
operation and closes the SSH connections and processes it started.

.. list-table:: Common failures
   :header-rows: 1
   :widths: 30 70

   * - Symptom
     - Action
   * - SSH connection check fails
     - Check the trusted host key, loaded identity, username, server availability
       and OpenSSH version. For profile-only settings, pass the same
       identity/user/port to the manual SFTP check.
   * - Profile unresolved or not bound to the host
     - Check the auth-file path, profile name and URL host against ``hosts``.
       Another collaborator's local profile is not shared with the catalog.
   * - No supported remote listing
     - Use a browsable collection root or published catalog. Confirm that an
       SFTP account can enumerate the requested directories.
   * - Checksum mismatch
     - Check whether the export changed since catalog creation. Reconcile the
       catalog and source against a trusted version before retrying.
   * - Transfer timeout or session limit
     - Adjust the local per-file time budget or lower concurrency to match
       the server's capacity.

Data URLs require absolute roots, for example
``ssh://researcher@lab-data:2222/srv/exports/lab/``. SCP shorthand is unsupported
for data directories. Percent-encode reserved characters in URL roots; catalog
paths are literal relative names. Hallmark rejects traversal and observed
destination symlinks; server permissions remain the access-control boundary.

Discovered catalogs can use path rows and published catalogs can contain
multiple TSVs. Existing local ``add``/``commit``/``checkout`` workflows still
require a compatible one-format ``data.tsv`` repository; downloading does not
populate the local object store. For local experiments, initialize a separate
repository, copy in the downloaded files, configure their filename format,
then add and commit them.

See :ref:`private-transport-reference` for profile settings, HTTP compatibility
and disposable transport tests, and :doc:`api` for function signatures.

..  |hallmark| replace:: ``hallmark``

..  _hallmark: https://github.com/l6a/hallmark
