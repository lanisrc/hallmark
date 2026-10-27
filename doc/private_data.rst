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
system OpenSSH ``ssh`` and ``sftp`` version 8.0 or newer, with connection
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

This creates ``./lab/.hm`` and catalogs the run files in ``data.tsv``, with
their sizes, modification times and any checksums. The ``checksum_source`` column
says where each checksum came from: ``published`` for a checksum file such as
``SHA256SUMS`` beside the data. ``README.md`` matches no format, so it is listed
as a ``file:`` entry in ``config.yml``, with its published checksum if there is
one. The URL is the exact discovery root. A filename format extracts parameters
and sorts the files; it does not approve a download. A glob filter selects which
paths to catalog:

.. code-block:: bash

   hm init ./lab-h5
   cd ./lab-h5
   hm add 'sftp://lab-data/srv/exports/lab/' \
       --filter '**/*.h5'
   hm commit -m "Record remote catalog"

Without ``--filter``, discovery recursively covers every directory beneath the
URL and catalogs every file it finds. A filter still traverses
directories needed to find matching files. Discovery shows an indeterminate
progress bar with counts of completed directories and discovered files.
The percentage and completion time remain unknown until a total is available.

A scan stages its catalog only after it completes. If the connection or access
fails, or no file matches the URL and filter, ``add`` explains
the problem, exits with an error and keeps the previously staged catalog
unchanged; it never stages a partial result.

Several filename formats can be cataloged at once by repeating ``--fmt``, each
as ``FMT`` or ``FMT=DB``. Every format's files go to their own TSV, named by
``DB`` or after the format itself:

.. code-block:: bash

   hm add 'ssh://lab-data/srv/exports/lab/' \
       --fmt 'runs/run_{run:03d}.h5=runs' --fmt 'logs/run_{run:03d}.log=logs'

A format added to an existing catalog is kept next to the earlier ones. When the
catalog had a single format in ``data.tsv``, that TSV is renamed after its format.
If files are already sorted into TSVs, ``hm add`` asks whether to sort them
again with the new format (``resort``), moving each file to the format that fits
it best, or to sort only files that no format matched yet (``unsorted``). When
other sources have sorted files too, it also offers ``all``, which sorts every
source's files again by their recorded paths, without listing their servers.
Python callers pass ``resort=True``, ``resort="all"`` or ``resort=False`` to
``Repo.add``.

``hm detect REMOTE`` suggests formats for a configured remote, named like
``origin`` or given as a URL inside it. A URL can point at a subfolder; only
that folder is scanned and the remote itself is unchanged. The command prints
the suggested formats and the ``hm add`` command that uses them; ``--add``
catalogs with every detected format right away:

.. code-block:: bash

   hm detect origin
   hm detect 'ssh://lab-data/srv/exports/lab/logs/' --add

Both URL schemes use structured SFTP directory enumeration and file transfers.
SFTP-only accounts work for discovery as well as downloading. Discovery
reads listings and published checksum manifests without reading dataset files
to compute checksums. Missing checksums remain unknown until a download records
the file's SHA-1. For reproducible catalogs, publish an immutable export with a
SHA-256 manifest, for example ``<digest>  runs/run_001.h5`` records in
``SHA256SUMS``.

Where no manifest exists and the account has a login shell, the server can
compute checksums instead:

.. code-block:: bash

   hm add 'ssh://lab-data/srv/exports/lab/' --server-checksum sha256

The server runs ``xargs -0 sha256sum`` (or ``md5sum``, ``sha1sum``,
``sha512sum``) on every file without an equal or stronger published checksum;
file names reach it only as input, never as part of a command. Hashing reads
each such file on the server, so prefer a data-transfer node to a shared login
node for large exports. The choice is saved with the source and repeated by
later ``add`` runs. SFTP-only accounts report that they cannot hash, and names
containing a backslash or newline cannot be hashed this way.

The generated data remote is named ``origin`` and points to the source URL.
Each ``add`` records a *source*: the remote, the folder beneath it, and the
filters that selected its files. Its rows carry the source name in the
``catalog_source`` column, leaving ``source`` free for a format field such as
an observed object. Filename formats belong to the whole catalog: they sort the
files of every source.
The destination may already contain files, provided it has no ``.hm``;
initialization preserves those files. An existing ``.hm`` is rejected before
network access. ``hm init PATH`` always creates an empty local repository.
Use ``add URL`` inside it to discover remote data.

Several sources in one catalog
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A catalog can combine selections from several folders and servers:

.. code-block:: bash

   hm add 'ssh://lab-data/srv/exports/lab/runs/run_{run:03d}.h5'
   hm add 'ssh://lab-data/srv/exports/lab/' --filter 'notes/*.txt'
   hm add 'https://archive.example.org/eht/m87/' --prefix archive
   hm commit -m "Record three sources"

A URL inside an existing remote's folder reuses that remote, and paths stay
relative to its URL, so ``runs/run_001.h5`` above downloads to
``runs/run_001.h5``. A URL on another server adds a remote named after its
host. Downloaded files mirror each server, so two sources cannot catalog the
same path. When they would, ``add`` lists the clashing paths and says for each
whether both sources have the same content (matching checksums, or the same
file on the server), different content (a checksum or size differs), or nothing
to compare. At a terminal it then asks whether to skip them, keeping the rows
already catalogued, which suits copies of one file on two mirrors; to keep
both, with the new source's files under a folder you name; or to abort.
Elsewhere it refuses, and ``--skip-duplicates`` or ``--prefix DIR`` adds the
source anyway. The choice is not saved, so adding that source again meets the
same paths. A path that would be a file in one source and a folder in another
is always refused. ``add`` also warns about paths that differ only in letter
case, such as ``M87.fits`` and ``m87.fits``: on macOS and Windows they are one
file, so only one of them can be downloaded there. Use ``--remote NAME`` to
name a new remote or pick an existing one, and ``--source NAME`` to name the
source.

Repeating an ``add`` with the same URL and filters updates its source: new
files are added. Files gone from the server leave the catalog only when you say
so: ``add`` lists them and asks whether to remove them or keep their rows, and
``--prune`` or ``--keep-missing`` answers in advance (``prune=True`` or
``prune=False`` in Python). It also lists, as changed upstream, files you
downloaded whose size, modification time or checksum the server now reports
differently; the next ``download`` replaces them. A bare URL updates the single
source of that folder. Different filters in the same folder add another source;
to change an existing source's filters instead, run ``add`` with ``--source
NAME``. New filters must still match every catalogued file of that source, so a
narrower selection belongs on a new branch. ``set-config --fmt`` does not apply
to remote catalogs; add a format with ``hm add URL --fmt``.

``download`` fetches each file from the remote of the source that cataloged
it, opening one connection per remote; the plan lists every remote and its file
count. ``download --source NAME`` selects one source's files, and
``download --remote NAME`` fetches every selected file from that remote
instead, at the same path beneath its URL, which suits a mirror.

Catalogs created before sources existed keep working and record their single
source on the next ``add``.

S3-compatible storage
~~~~~~~~~~~~~~~~~~~~~

Objects in AWS S3 and compatible stores (Open Storage Network, MinIO, Wasabi,
Ceph) are catalogued from ``s3://BUCKET/PREFIX/`` URLs after
``pip install hallmark[s3]``. Stores other than AWS need their endpoint in a
backend options file:

.. code-block:: bash

   printf 'endpoint_url: https://sdsc.osn.xsede.org\n' > osn.yml
   hm add 's3://ehtc-sims/m87/' --backend-options osn.yml --auth osn

The options are ``endpoint_url``, ``region``, ``addressing_style`` (``auto``,
``virtual`` or ``path``), ``anonymous`` (``true`` for public buckets),
``page_size`` (objects per listing request, at most 1000) and ``etag_md5``.
Objects uploaded with a SHA-256 or SHA-1 checksum are catalogued with it as a
``server`` checksum. Set ``etag_md5: auto`` to also treat single-part ETags as
MD5 checksums; leave it unset for buckets that encrypt objects with KMS or
customer keys, whose ETags are not checksums.

Credentials come from an ``s3`` auth profile, otherwise from the usual AWS
sources (environment, ``~/.aws`` configuration, instance roles). Without any,
requests are unsigned, as public buckets expect. A profile is bound to its
buckets and endpoints, so a shared catalog cannot send your keys to another
server:

.. code-block:: yaml

   version: 2
   profiles:
     osn:
       type: s3
       buckets: [ehtc-sims]
       endpoints: [https://sdsc.osn.xsede.org]
       access_key_id: {env: OSN_ACCESS_KEY_ID}
       secret_access_key: {command: [pass, show, osn/secret]}
     aws:
       type: s3
       buckets: [my-lab-bucket]
       aws_profile: research    # a profile in ~/.aws/config

Select a built-in backend with ``--backend http``, ``ssh``, ``cyverse`` or
``webdav``, or name an installed plugin. A ``webdavs://HOST/PATH/`` URL is a
shorthand for ``https://HOST/PATH/`` with ``--backend webdav``, for servers
that answer WebDAV requests but show no browsable index. Without an explicit choice, Hallmark selects a
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
the number of unknown file sizes, destination and source. The CLI reports
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

   hm download --filter 'runs/run_00[12].h5' --dry-run
   hm download --all --output ./payload

Explicit paths must be catalogued files or folders containing catalogued files.
A folder selects all catalogued files below it, including subfolders. Unknown
paths and folders without catalogued files are reported before any server request.
Explicit selections retain their recorded checksums, just like ``--tsv`` and
``--all``. Every download is checked against its recorded size. Files without a
usable publisher checksum can be downloaded to new paths, but their contents
can only be checked against the catalog once a SHA-1 is recorded (see below).

``--tsv`` can be repeated and combined with explicit paths; overlapping entries
are deduplicated. ``--all`` cannot be combined with either selection mode.
Relative output paths are relative to the current directory. Without
``--output``, downloads go to the repository worktree; bare ``.hm`` repositories
require an explicit output directory. Existing files with matching checksums
are listed as already downloaded and skipped. An earlier download that no
longer matches its catalog row, because the file changed on the server and the
source was added again or because another branch is checked out, is listed as
outdated: approving the download replaces it, and the old version stays in
``.hm/objects``. Checking out a branch or commit of a remote catalog switches
only the catalog and leaves downloaded files as they are; ``hm checkout`` and
``hm status`` report the downloads that now hold another version (a copy, made
where links are unavailable, only when its size differs), and ``hm download``
brings them in line, from ``.hm/objects`` without a transfer where that version
is stored. A downloaded file whose row has no catalog
checksum cannot prove it is current; it is fetched again to compare and kept if
unchanged. Files you created or edited, directories blocking file paths, and
existing files without a usable catalog checksum stop the whole selection
before transfer, as does a file that two catalog paths differing only in letter
case would share. Delete conflicting paths yourself before retrying.
``--dry-run`` performs the same local checks without downloading or creating
files.

Downloads pass through the object store in ``.hm/objects``, where
``hm commit`` also keeps your local files:

1. Each file is transferred to a temporary file in ``.hm/objects/tmp`` and is
   stored under its SHA-1 only once its size and checksums are verified. An
   interrupted download never leaves a partial file in the data folder, and its
   temporary file is removed a day later.
2. The stored file is hard-linked into the data folder (or copied where links
   are unavailable, for example on another filesystem). An outdated file is
   replaced in one step, so the path is never missing. Files with identical
   contents are stored once.
3. The SHA-1 of every file now in place is recorded in the catalog and staged.
   ``download`` prints ``Staged SHA-1 for N file(s); run hm commit to record
   them``. Commit them to share the hashes: later downloads, including your
   collaborators', are then verified against them, and a deleted file is
   restored from ``.hm/objects`` without a transfer. Until you commit or run
   ``hm restore --staged``, ``checkout`` refuses to switch branches.

Downloaded files are read-only links into ``.hm/objects``. A link and its
object are the same data on disk, so making a file writable and editing it in
place changes the stored copy too. Hallmark records each object's size and
modification time and notices: ``hm status`` lists the file, the next download
refuses to touch it and drops the changed object, and deleting the file lets a
fresh copy be downloaded. To change a downloaded file, copy it to a new name.
Never delete ``.hm/objects`` by hand: it also holds committed local files that
may exist nowhere else. Linked worktrees share it: ``add_worktree`` links the
files whose committed SHA-1 is already in ``.hm/objects`` into the new worktree,
read-only, and the rest wait for ``hm download`` there, which fills the same
store. A file linked into two worktrees is one copy on disk, so editing it in
place changes it in both. After an interruption, run
the same download again: files whose SHA-1 was recorded, or whose catalog
checksum matches, are recognised as complete and only the rest are transferred.
Files with neither are fetched again to compare.

After committing the catalog, use ``download`` to review and approve a transfer.
Declining keeps the catalog available; an empty selection needs no approval:

.. code-block:: bash

   hm init ../lab-one
   cd ../lab-one
   hm add 'ssh://lab-data/srv/exports/lab/' \
       --filter 'runs/run_001.h5'
   hm commit -m "Record remote catalog"
   hm download --all

An existing server-backed catalog can be cloned from a local path, a Git
endpoint, or an HTTP/SFTP directory containing its metadata. Repositories
tracking local files without a data server are rejected before the destination
is created, including with ``--no-download``. Empty repositories can be cloned.
For example, copy this catalog:

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
       server_alive_interval: 15
       server_alive_count_max: 2
       compression: false

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
(both default to 4). ``max_sessions`` is at most 10, the default session limit
of an OpenSSH server. The connection is closed after about
``server_alive_interval`` times ``server_alive_count_max`` seconds without a
reply from the server; raise them if transfers over a congested link are
dropped. Set ``compression: true`` for text-like data over slow links. Unset
``compression`` follows SSH configuration. These tuning fields may also appear
under ``defaults`` to apply to every host.

HTTP(S) profiles and secrets
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A ``version: 2`` file adds a ``type`` to each profile: ``ssh`` (the fields
above), ``http`` or ``s3``. Secrets are never written in the file. Each secret
field names where its value comes from instead: an environment variable
(``env``), the operating system keyring (``keyring``, after
``pip install hallmark[keyring]``), or a command such as a password manager
that prints it (``command``):

.. code-block:: yaml

   version: 2
   profiles:
     lab:
       type: ssh
       hosts: [lab-data]
       user: researcher
     cyverse:
       type: http
       hosts: [data.cyverse.org]
       scheme: basic
       username: alice
       password: {env: CYVERSE_PASSWORD}
     archive:
       type: http
       hosts: [archive.example.org]
       scheme: bearer
       token: {command: [pass, show, archive/token]}
     zenodo:
       type: http
       hosts: [zenodo.org]
       scheme: bearer
       token: {keyring: {service: hallmark, username: zenodo}}

Then name the profile when adding the remote, as for SSH:

.. code-block:: bash

   export CYVERSE_PASSWORD=...
   hm add 'https://data.cyverse.org/dav/iplant/home/alice/eht/' --auth cyverse

Secrets are read when a transfer starts, never during ``--dry-run``, and are
never shown in messages; a helper command's error output is discarded. An HTTP
profile sends its credentials only to its ``hosts``, and only over HTTPS (or to
this computer). A redirect to another server, such as a signed download link,
does not carry them. Without a profile, HTTP downloads use ``~/.netrc`` as
before. Because a version 2 file can run commands, Hallmark refuses one that
another account owns or can write to; ``chmod 600`` keeps it private.

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
selects matching files from the catalog;
``plan_download(output_path="subset", tsv_names=["data.tsv"])`` selects a TSV
and destination. Planning makes no network requests. Known size totals and
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
``hm download --filter PATTERN --dry-run`` and then the same command
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

Files are downloaded to temporary paths and published atomically after transfer
and any recorded checksum verification. Publication fails if the destination
appeared during transfer, preserving that file. A failed transfer removes its
temporary file. Successful files remain available
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

Discovered catalogs can use path rows, one TSV per filename format and
``file:`` entries for files no format matches; published catalogs can contain
multiple TSVs. Local ``add``/``commit``/``checkout`` workflows still use a
single format in ``data.tsv``; downloaded files are kept in the object store
in ``.hm/objects``, as committed local files are. For local experiments,
initialize a separate repository, copy in the downloaded files, configure
their filename format, then add and commit them.

See :ref:`private-transport-reference` for profile settings, HTTP compatibility
and disposable transport tests, and :doc:`api` for function signatures.

..  |hallmark| replace:: ``hallmark``

..  _hallmark: https://github.com/lanisrc/hallmark
