.. _private-data:


Private data over SSH and SFTP
==============================

|hallmark|_ can discover a remote dataset, prepare a local catalog, and download
selected files after approval. ``init`` creates an empty ``.hm``; ``add URL``
stages a remote catalog and ``commit`` records it. ``clone`` copies an existing
Hallmark Git catalog or published snapshot. Dataset files stay on their servers
until explicitly downloaded.
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

   hm init ./lab
   cd ./lab
   hm add 'ssh://lab-data/srv/exports/lab/' \
       --fmt 'runs/run_{run:03d}.h5'
   hm commit -m "Record remote catalog"

This creates ``./lab/.hm`` and catalogs only the matching run files. The URL
is the exact discovery root. A filename format selects paths and extracts
parameters; it does not approve a download. Each branch has one filename
format: adding the bare URL again rescans with the saved format, and a new
format is refused, before the server is contacted, if any catalogued file does
not fit it. There is no option to force it; a broader format that fits every
catalogued file replaces the old one. A glob filter can select paths without
defining parameters:

.. code-block:: bash

   hm init ./lab-h5
   cd ./lab-h5
   hm add 'sftp://lab-data/srv/exports/lab/' \
       --filter '**/*.h5'
   hm commit -m "Record remote catalog"

Without ``--filter`` or ``--fmt``, discovery recursively covers every directory
beneath the URL and catalogs all discovered files. A filter still traverses
directories needed to find matching files. Discovery shows an indeterminate
progress bar with counts of completed directories and discovered files.
The percentage and completion time remain unknown until a total is available.

A scan stages its catalog only after it completes. If the connection or access
fails, or no file matches the URL, filter and filename format, ``add`` explains
the problem, exits with an error and keeps the previously staged catalog
unchanged; it never stages a partial result.

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
network access. ``hm init PATH`` always creates an empty local repository.
Use ``add URL`` inside it to discover remote data.

Select a built-in backend with ``--backend http``, ``ssh``, or ``cyverse``, or
name an installed plugin. Without an explicit choice, Hallmark selects a
backend from the URL. ``--backend-options FILE`` reads a YAML mapping of
nonsecret backend settings. These settings are saved with the data remote;
credentials belong in local authentication configuration. See :doc:`backends`
for the plugin interface.

Keep passwords and keys outside ``.hm``. ``add URL`` and
``set-config --remote-url`` reject URLs containing a password, an HTTP(S)
username or token, a query string or a fragment, before contacting the server
and without repeating the URL. Store SSH users and keys in ``~/.ssh/config`` or
a local profile in ``~/.config/hallmark/auth.yml`` (see section 4), and
HTTP(S) logins in ``~/.netrc``. An SSH username such as
``ssh://researcher@lab-data/srv/exports/lab/`` is not a secret and is allowed.
``clone`` likewise refuses a source URL with a password or an HTTP(S) username
or token, before creating anything, because Git would save it in
``.hm/.git/config``; use a Git credential helper or ``~/.netrc`` instead.

3. Preview, approve and download
--------------------------------

From the directory containing ``lab``, enter that repository:

.. code-block:: bash

   cd lab
   hm info
   hm download --tsv data.tsv --dry-run
   hm download runs/run_001.h5

The dry run reads only the local catalog. It reports file count, known bytes,
the number of unknown file sizes, destination and source, then lists the
selected files and any paths that are not in the catalog. The CLI reports
duration as unknown. Python callers can supply a transfer rate to estimate
duration when all file sizes are known. The dry run does not test credentials,
host trust or remote-file existence.

Every nonempty CLI download displays its plan and asks
``Download these files? [y/N]``. Answer ``y`` to authorize the displayed
transfer. Refusal, a blank answer or end-of-input transfers no dataset files.

During transfer, byte progress and measured throughput provide an ETA when the
total is known; unknown totals remain indeterminate.

Choose a scope from inside ``lab``:

.. code-block:: bash

   hm download runs --dry-run
   hm download --all --filter 'runs/run_00[12].h5' --dry-run
   hm download --all --output ./payload

Choose files with catalogued paths or folders, ``--tsv`` or ``--all``. Paths
are relative to the current folder, which can be any folder inside the
repository. A folder selects every catalogued file below it, including
subfolders; ``.`` selects everything below the current folder. ``--filter``
and ``--fmt`` only narrow that selection: on their own they show usage and
download nothing. A path that matches no catalogued file, such as a typo or a
folder without catalogued files, stops the download before the server is
contacted; ``--dry-run`` lists such paths. Selected files use their recorded
catalog checksums. Files without a usable publisher checksum can be
downloaded, but their contents cannot be checked against the catalog.

``--tsv`` can be repeated and combined with explicit paths; overlapping entries
are deduplicated. ``--all`` cannot be combined with either selection mode.
Relative output paths are relative to the current directory. Without
``--output``, downloads go to the repository worktree; bare ``.hm`` repositories
require an explicit output directory.

Existing files are never replaced. Before contacting the server, a download
checks each selected file that already exists at its destination, first by
its recorded size and then by its catalog checksum. A file with its catalog
checksum is reported as already downloaded and skipped, so repeating a
download, for example after a failed transfer, fetches only what is missing.
When the catalog has no checksum for a file, as for SSH exports without a
checksum manifest, a file with the recorded size is reported as present (size
matches, not verified) and also skipped. A file with a different size or
contents, a file the catalog records neither a checksum nor a size for, or a
folder in its place is a conflict: the download stops and transfers nothing.
Delete conflicting files first to replace them; there is no overwrite option.
``--dry-run`` lists files to download, skipped and unverified files, conflicts
and unknown paths. Checksums from manifests that do not name their algorithm,
such as ``checksums.txt``, are used when the digest length identifies MD5,
SHA-1, SHA-256 or SHA-512.

After committing the catalog, use ``download`` to review and approve a transfer.
Declining keeps the catalog available; an empty selection needs no approval:

.. code-block:: bash

   hm init ../lab-one
   cd ../lab-one
   hm add 'ssh://lab-data/srv/exports/lab/' \
       --filter 'runs/run_001.h5'
   hm commit -m "Record remote catalog"
   hm download --all

An existing catalog can be cloned from a local path, a Git endpoint, or an
HTTP/SFTP directory containing its metadata. For example, copy this catalog:

.. code-block:: bash

   hm clone ./.hm ../lab-copy

Git endpoints preserve the complete catalog and history. Published HTTP/SFTP
snapshots start new local history. A snapshot URL may name the metadata
directory itself or its parent containing ``.hm``. Use ``--source-type git``
to require Git or ``--source-type catalog`` to require a snapshot. Automatic
selection treats local paths, SCP-style addresses, Git/file/SSH URLs and URLs
ending in ``.git`` as Git sources. SFTP URLs select snapshots. Other HTTP(S)
URLs are checked for a snapshot before Git is attempted. Authentication errors,
connection failures and malformed snapshots are reported rather than treated
as dataset directories. Use ``add URL`` for raw datasets.

Clone filters and formats select files to download, leaving the full catalog
and its history unchanged. Clone offers downloads by default and asks for
confirmation; Enter or ``n`` keeps the catalog and exits successfully:

.. code-block:: bash

   hm clone ./.hm ../lab-selected \
       --filter 'runs/run_001.h5'

Use ``--no-download`` for catalog only, without a prompt. It cannot be combined
with ``--filter`` or ``--fmt`` and is required for bare destinations. An empty
selection prints ``No files selected for download.`` without prompting.
Git authentication and data authentication are independent; catalog-only cloning
does not require credentials for its data.

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

   hm set-config --remote-name origin --remote-auth lab
   hm download --remote origin --tsv data.tsv --dry-run
   hm init ../lab-profile
   cd ../lab-profile
   hm add 'ssh://lab-data/srv/exports/lab/' --auth lab
   hm commit -m "Record remote catalog"

The new catalog's ``origin`` remote records ``auth: lab``. Clear a reference with
``hm set-config --remote-name origin --remote-auth ''`` to use SSH
configuration alone. With ``add``, ``--auth`` selects data access. With
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

These examples use fresh local destinations. ``Repo.init`` creates the empty
repository; ``repo.add`` discovers metadata without downloading dataset files:

.. code-block:: python

   from hallmark import Repo

   repo = Repo.init("lab-python")
   repo.add(
       "ssh://lab-data/srv/exports/lab/",
       auth="lab",  # Omit when SSH configuration already supplies access.
       remote_fmt="runs/run_{run:03d}.h5",
       progress=True,
   )
   repo.commit("Record remote catalog")
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
selects matching files from the catalog; ``plan_download(file_paths=["runs"])``
selects a folder; ``plan_download(output_path="subset", tsv_names=["data.tsv"])``
selects a TSV and destination. Planning makes no network requests. Requested
paths that match no catalogued file are listed in ``plan.unknown_paths``;
downloading such a plan raises ``DownloadError`` before opening a connection. Known size totals and
unknown-size counts are available as ``known_bytes`` and
``unknown_size_count``. ``estimated_seconds`` stays ``None`` unless all sizes
are known and ``estimated_bytes_per_second`` was supplied when planning.

A small review function can approve a separate download. Cloning also accepts
this function as its optional ``approve`` callback. ``Repo.clone`` downloads
by default without a callback; ``download=False`` skips data and is required
for bare destinations. Refusing through the callback keeps the catalog:

.. code-block:: python

   def approve(plan):
       print(plan.summary())
       return input("Download these files? [y/N] ").strip().lower() == "y"

   repo = Repo.init("lab-with-download")
   repo.add(
       "ssh://lab-data/srv/exports/lab/",
       filter="runs/run_001.h5", progress=True,
   )
   repo.commit("Record remote catalog")
   plan = repo.plan_download()
   if plan.file_count and approve(plan):
       result = repo.download(plan, approved=True)
       if result["failed"]:
           raise RuntimeError("\n".join(result["errors"]))

``repo.download`` returns ``succeeded``, ``failed``, ``total_bytes`` and
``errors``. Inspect ``failed`` because successful files remain when another
file fails. Setup failures raise ``hallmark.remote.download.DownloadError``.
``repo.set_config(remote_auth="")`` removes a profile reference; ``None``
leaves it unchanged.

6. Discover public HTTPS collections
------------------------------------

The same workflow supports CyVerse and ordinary browsable HTTPS collections
such as DESI. No CyVerse-specific index option is required:

.. code-block:: bash

   hm init ./cyverse
   cd ./cyverse
   hm add 'https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/EHTC_FirstM87Results_Apr2019/uvfits/' \
       --filter 'SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits'
   hm commit -m "Record remote catalog"
   cd ..
   hm init ./desi
   cd ./desi
   hm add 'https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/23040/' \
       --filter 'redrock-main-dark-23040.fits'
   hm commit -m "Record remote catalog"

Each URL is the full recursive discovery root. A broad root can require many
listing requests even with a file filter; use a specific subtree when that is
your intended scope. Public collections need no auth profile. Private HTTP
access retains Requests' normal ``.netrc`` and environment behavior; Hallmark's
named auth profiles apply to SSH/SFTP.

From either initialized catalog, inspect and approve a selected subset using
``hm download --all --filter PATTERN --dry-run`` and then the same command
without ``--dry-run``. Python follows the same steps:

.. code-block:: python

   cyverse = Repo.init("cyverse-python")
   cyverse.add(
       "https://data.cyverse.org/dav-anon/iplant/commons/"
                "cyverse_curated/EHTC_FirstM87Results_Apr2019/uvfits/",
       filter="SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits", progress=True,
   )
   cyverse.commit("Record remote catalog")
   desi = Repo.init("desi-python")
   desi.add(
       "https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/"
                "healpix/main/dark/230/23040/",
       filter="redrock-main-dark-23040.fits", progress=True,
   )
   desi.commit("Record remote catalog")
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

   hm clone 'https://github.com/example/lab-catalog.git' ./shared-lab --no-download
   cd shared-lab
   hm download --all --dry-run

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

   hm clone 'https://catalogs.example.org/lab/' ./snapshot-lab --no-download

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

Unfinished downloads are kept in temporary ``.part`` files beside their
destinations and moved into place only after the transfer and any recorded
checksum verification succeed. A failed transfer removes its temporary file.
A destination that appears while the download runs is not replaced; that file
fails instead. Successful files remain available when another file fails; a
multi-file download is not a single transaction.
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
   * - Existing files conflict with the catalog
     - Compare the listed files with the catalog. Delete or move them, then
       download again; Hallmark never overwrites existing files.
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
