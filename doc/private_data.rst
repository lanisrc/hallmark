.. _private-data:

Private data over SSH and SFTP
==============================

|hallmark|_ can discover a remote dataset, prepare a local catalog, and download
selected files after approval. ``init --from`` creates a new ``.hm`` from a
remote dataset; ``clone`` copies an existing Hallmark Git catalog or published
catalog snapshot. Both leave dataset files on their servers by default.
A **data remote** identifies the dataset location; it is independent of the
Git remote or snapshot URL used to share the catalog.

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

2. Initialize a catalog from remote data
----------------------------------------

Suppose the export contains ``runs/run_001.h5``, ``runs/run_002.h5`` and
``README.md``. From an empty local workspace, run:

.. code-block:: bash

   hallmark init ./lab --from 'ssh://lab-data/srv/exports/lab/' \
       --fmt 'runs/run_{run:03d}.h5'

This creates ``./lab/.hm`` and catalogs only the matching run files. The URL
is the exact discovery root. A filename format selects paths and extracts
parameters; it does not approve a download. A glob filter can select paths
without defining parameters:

.. code-block:: bash

   hallmark init ./lab-h5 --from 'sftp://lab-data/srv/exports/lab/' \
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
The destination may already contain files, provided it has no ``.hm``;
initialization preserves those files. An existing ``.hm`` is rejected before
network access. Use ``hallmark init PATH`` without ``--from`` for a local
repository. The older remote ``build`` command and ``build_repo`` API remain
available but are deprecated in favor of ``init --from``.

Select a built-in backend with ``--backend http``, ``ssh``, or ``cyverse``, or
name an installed plugin. Without an explicit choice, Hallmark selects a
backend from the URL. ``--backend-options FILE`` reads a YAML mapping of
nonsecret backend settings. These settings are saved with the data remote;
credentials belong in local authentication configuration. See :doc:`backends`
for the plugin interface and a multiple-server example.

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

To request downloads during initialization, add ``--with-download``. Catalog
preparation finishes first, then the same plan and approval prompt appear.
Declining keeps the new catalog available; an empty selection needs no approval:

.. code-block:: bash

   hallmark init ../lab-one --from 'ssh://lab-data/srv/exports/lab/' \
       --filter 'runs/run_001.h5' --with-download

An existing catalog can be cloned from a local path, a Git endpoint, or an
HTTP/SFTP directory containing its metadata. For example, copy this catalog:

.. code-block:: bash

   hallmark clone ./.hm ../lab-copy

Git endpoints preserve the complete catalog and history. Published HTTP/SFTP
snapshots start new local history. A snapshot URL may name the metadata
directory itself or its parent containing ``.hm``. Use ``--source-type git``
to require Git or ``--source-type catalog`` to require a snapshot. Automatic
selection treats local paths, SCP-style addresses, Git/file/SSH URLs and URLs
ending in ``.git`` as Git sources. SFTP URLs select snapshots. Other HTTP(S)
URLs are checked for a snapshot before Git is attempted. Authentication errors,
connection failures and malformed snapshots are reported rather than treated
as dataset directories. Use ``init --from`` for raw datasets.

Clone filters and formats select files to download, leaving the full catalog
and its history unchanged. They require ``--with-download`` (the older
``--download`` spelling remains an alias):

.. code-block:: bash

   hallmark clone ./.hm ../lab-selected \
       --filter 'runs/run_001.h5' --with-download

``--no-fetch-data`` remains a compatibility alias for the default behavior
without dataset downloads. Git authentication and data authentication are
independent; cloning a catalog does not require credentials for its data.

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

Associate an existing data remote with a profile, or select one when
initializing a catalog from a remote dataset:

.. code-block:: bash

   hallmark set-config --remote-name origin --remote-auth lab
   hallmark download --remote origin --tsv data.tsv --dry-run
   hallmark init ../lab-profile --from 'ssh://lab-data/srv/exports/lab/' --auth lab

The new catalog's ``origin`` remote records ``auth: lab``. Clear a reference with
``hallmark set-config --remote-name origin --remote-auth ''`` to use SSH
configuration alone. With ``init``, ``--auth`` selects data access. With
``clone``, it selects access to an SSH/SFTP catalog snapshot; cloned data
remotes retain their own profile references. Git cloning uses Git's own
authentication configuration.

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

These examples use fresh local destinations. Like the CLI, ``Repo.init``
with ``from_url`` discovers metadata without downloading dataset files:

.. code-block:: python

   from hallmark import Repo

   repo = Repo.init(
       "lab-python", from_url="ssh://lab-data/srv/exports/lab/",
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
reference, backend, backend options and destination. Later catalog or remote
configuration changes do not redirect it. ``plan_download(filter="**/*.h5")``
selects matching files from the catalog;
``plan_download(output_path="subset", tsv_names=["data.tsv"])`` selects a TSV
and destination. Planning makes no network requests. Known size totals and
unknown-size counts are available as ``known_bytes`` and
``unknown_size_count``. ``estimated_seconds`` stays ``None`` unless all sizes
are known and ``estimated_bytes_per_second`` was supplied when planning.

For downloads during initialization or cloning, supply a callback that reviews
the completed plan and returns ``True`` to approve:

.. code-block:: python

   def approve(plan):
       print(plan.summary())
       return input("Download these files? [y/N] ").strip().lower() == "y"

   repo = Repo.init(
       "lab-with-download", from_url="ssh://lab-data/srv/exports/lab/",
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

   hallmark init ./cyverse --from \
       'https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/EHTC_FirstM87Results_Apr2019/uvfits/' \
       --filter 'SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits'
   hallmark init ./desi --from \
       'https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/23040/' \
       --filter 'redrock-main-dark-23040.fits'

Each URL is the full recursive discovery root. A broad root can require many
listing requests even with a file filter; use a specific subtree when that is
your intended scope. Public collections need no auth profile. Private HTTP
access retains Requests' normal ``.netrc`` and environment behavior; Hallmark's
named auth profiles apply to SSH/SFTP.

From either initialized catalog, inspect and approve a selected subset using
``hallmark download --filter PATTERN --dry-run`` and then the same command
without ``--dry-run``. Python follows the same steps:

.. code-block:: python

   cyverse = Repo.init(
       "cyverse-python",
       from_url="https://data.cyverse.org/dav-anon/iplant/commons/"
                "cyverse_curated/EHTC_FirstM87Results_Apr2019/uvfits/",
       filter="SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits", progress=True,
   )
   desi = Repo.init(
       "desi-python",
       from_url="https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/"
                "healpix/main/dark/230/23040/",
       filter="redrock-main-dark-23040.fits", progress=True,
   )
   plan = desi.plan_download(all_files=True)
   print(plan.summary())

Inspect this plan and approve it as in the Python download example above.

HTTP provides file transfer but does not define directory enumeration. Hallmark
recognizes supported HTML indexes and directory landing pages automatically.
If a server supplies no usable listing, provide a published Hallmark catalog,
a more specific browsable URL, or a :doc:`backend plugin <backends>` for its
API. Hallmark cannot infer hidden file URLs.

7. Host catalogs separately from their data
-------------------------------------------

A catalog's location does not set its data location. For example, a team
can push the Git repository in ``.hm`` to GitHub while its data remote still
points to ``ssh://lab-data/srv/exports/lab/`` with ``auth: lab``. Collaborators
clone the existing metadata using their Git credentials:

.. code-block:: bash

   hallmark clone 'https://github.com/example/lab-catalog.git' ./shared-lab
   cd shared-lab
   hallmark download --all --dry-run

This clone needs no ``lab`` profile. Define the local profile before approving
a data transfer. The Git origin points to GitHub; the data remote named
``origin`` retains the SSH URL and profile reference recorded in the catalog.
Git fetch/pull operates on the metadata repository in ``.hm``.

The same configuration can be published as a snapshot on a separate HTTPS
server. For example, publish ``config.yml``, ``meta.yml`` and referenced TSVs
under ``https://catalogs.example.org/lab/``. Its configuration can contain:

.. code-block:: yaml

   remote:
     - name: origin
       url: ssh://lab-data/srv/exports/lab/
       auth: lab

Then clone the snapshot:

.. code-block:: bash

   hallmark clone 'https://catalogs.example.org/lab/' ./snapshot-lab

Snapshot authentication uses the metadata server's settings. The SSH data
URL and ``lab`` reference are preserved without resolving that profile during
cloning. For an SFTP snapshot, ``clone --auth catalog-reader`` selects its
metadata profile; it does not replace a data remote's profile. Snapshot
imports start local Git history and have no upstream Git history to pull.
These examples retain the usual local ``PATH/.hm`` layout while keeping
catalog hosting independent of data hosting.

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
