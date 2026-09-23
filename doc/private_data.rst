Data sources and private data
=============================

``hallmark init`` creates a local repository or catalogs a named source or raw
dataset URL. It reads directory listings and published checksum metadata without
downloading dataset files or offering transfers. ``hallmark clone`` copies a
complete existing catalog, then reviews a download plan by default. Use
``clone --no-download`` to skip review. ``hallmark download`` selects and approves
transfers from an existing catalog.

1. Choose a source
------------------

List supported sources, then inspect DESI releases and collection roots. These
commands read source definitions without crawling a dataset:

.. code-block:: bash

   hallmark sources
   hallmark sources desi

DESI supports EDR and DR1. Its collections are ``redshifts`` (merged redshift
catalogs), ``spectra`` (HEALPixel and tile product directories), and ``vac``
(value-added products). DR1 also provides ``lss`` (large-scale-structure
catalogs). The mappings follow the `DESI data organization
<https://data.desi.lbl.gov/doc/organization/>`_.

Create a catalog for one collection in an explicit release:

.. code-block:: bash

   hallmark init ./desi --from desi --release dr1 --collection redshifts

Repeat ``--collection`` to include several collections. Without it, Hallmark
catalogs the entire release, including spectra, ancillary products and root
files. Collection selection limits the directories traversed. Paths remain
relative to the release root even when a single collection is selected.
The source name, release, collections and resolved URL are saved in metadata.
Downloads use that resolved URL and the saved catalog without rediscovery.

``hallmark init ./desi --from desi`` prompts for a release in a terminal.
Scripts and Python must provide a release explicitly. Cancelling the prompt
creates no destination. No release is selected implicitly.

Raw URLs remain available for a specific subtree or another service:

.. code-block:: bash

   hallmark init ./observations --from 'https://data.example.org/export/' \
       --filter '**/*.fits'

The URL is the exact recursive root. HTTP sources need a supported browsable
index; HTTP file access alone does not provide directory enumeration.
``--release`` and ``--collection`` apply only to named sources. Public HTTP
sources need no credentials; private HTTP retains Requests' local ``.netrc``
and environment behavior. See :doc:`backends` for source and transport plugins.

2. Configure SSH or SFTP access
-------------------------------

Use standard OpenSSH configuration. Add your server alias to ``~/.ssh/config``:

.. code-block:: text

   Host lab-data
       HostName data.example.org
       User researcher
       IdentityFile ~/.ssh/id_ed25519
       IdentitiesOnly yes
       StrictHostKeyChecking yes

Use your actual host, user and key. Provision the server's verified host key in
``known_hosts`` and load an encrypted key into your SSH agent. Check that SFTP
works without an interactive password or MFA prompt:

.. code-block:: bash

   ssh -V
   sftp -o BatchMode=yes -b /dev/null lab-data

Hallmark requires OpenSSH 9.6 or newer with connection multiplexing. Linux and
macOS are supported; Windows SSH transport is unsupported. Both ``ssh://`` and
``sftp://`` use structured SFTP enumeration and transfers. The server needs no
Hallmark, Git, Python or login shell. SSH configuration supplies identities,
users, ports and proxies, including ``ProxyJump``; explicit URL user/port values
take precedence. Hallmark enforces noninteractive access and strict host-key
checking. There is no Hallmark authentication file or profile-saving prompt.

3. Select catalog entries and extract fields
--------------------------------------------

Suppose the source contains ``runs/run_001.h5``, ``runs/run_002.h5`` and
``README.md``. Include both run files and the README, extracting the integer
``run`` field only from matching filenames:

.. code-block:: bash

   hallmark init ./lab --from 'sftp://lab-data/srv/exports/lab/' \
       --filter 'runs/*.h5' --filter 'README*' \
       --format 'runs/run_{run:03d}.h5'

``--filter`` selects catalog entries; repeated globs form a union. Matching
is case-sensitive, ``*`` stays within a path segment, and ``**`` spans zero or
more directories. Quote patterns to prevent shell expansion. Globs filter the
inventory; they do not currently prune directory traversal. Narrow the URL or
choose collections when you want to limit the crawl.

Without ``--format``, Hallmark automatically detects filename templates from the
filtered paths using its existing format detector. Multiple detected templates
contribute columns to one catalog; each path uses the first matching template
in the detector's stable order. Inferred names are heuristic, for example
``runs/run_001.h5`` and ``runs/run_002.h5`` yield ``runs/run_{scan}.h5``.
Detection uses filenames only and does not inspect dataset contents. Static
metadata and archive files can be ignored during inference but remain cataloged.
If no usable template is found, the catalog contains paths and metadata alone.

``--format`` overrides detection with one explicit template, allowing you to
choose field names and types. It adds named fields without excluding files.
In the example above, the README remains in
the catalog with an empty ``run`` value. Extraction fields cannot overwrite
reserved metadata columns. Their columns exist even if no filename matches.
Inferred templates that would overwrite metadata or cannot be parsed are skipped;
invalid explicit templates are rejected before discovery. Templates are recorded
under ``extraction`` in the data configuration, separately from the local indexing
``fmt`` setting. Literal catalog paths determine downloads, even if detection
found no template. Reloading, cloning, and downloading do not repeat detection.

Without ``--filter``, every discovered file within the selected scope is
cataloged. Discovery preserves available sizes, times and published checksums;
unknown values remain unknown. It never downloads payloads to compute hashes.
Publish SHA-256 manifests for reproducible verification where possible.

The destination may already contain files if it has no ``.hm``; those files are
preserved. An existing catalog is rejected before remote access. A destination
ending in ``.hm`` creates a bare catalog. Plain ``hallmark init PATH`` creates a
local repository without cataloging local files automatically.

4. Preview and approve downloads
--------------------------------

From the new worktree, preview one run using only local catalog metadata, then
request the same transfer:

.. code-block:: bash

   cd lab
   hallmark download --filter 'runs/run_001.h5' --dry-run
   hallmark download --filter 'runs/run_001.h5'

The download command displays the file count, known bytes, unknown-size count,
source and destination, then asks for confirmation. Declining leaves the catalog
available. ``--filter`` selects only cataloged files; explicit paths cannot add
files omitted during initialization. Downloads do not change catalog membership.
Use ``--all`` for the whole catalog or ``--tsv data.tsv`` for a configured table.
A bare catalog requires ``--output DIRECTORY`` with explicit selectors; the
interactive chooser can prompt for it. ``--max-workers`` defaults to 4
and must be positive. Internal SSH session limits may further cap concurrency.

Dataset transfers require approval even for one file. The legacy download
``--yes`` option does not bypass confirmation. To choose files interactively
instead of supplying selectors on the command line, run:

.. code-block:: bash

   hallmark download --interactive

The chooser warns about bandwidth and disk use, then offers ``patterns``,
``all``, or ``skip`` (the default). Enter one relative-path glob per line without
shell quotes; a blank line finishes the list. Spaces and commas remain part of
the pattern. Patterns are ORed, case-sensitive, and support recursive ``**``.
Finishing an empty list or matching no files returns to selection.

The preview shows the source, destination, file count, known bytes, unknown-size
count, and up to 20 paths with recorded sizes. It uses local metadata only;
unknown sizes remain unknown. Choose ``download`` to approve that exact plan,
``change`` to select again and review a new plan, or ``skip`` (the default).
``--interactive --dry-run`` previews the selection and exits without offering
execution. ``--output``, ``--remote`` and ``--max-workers`` are supported with
``--interactive``; paths, ``--filter``, ``--tsv`` and ``--all`` cannot be combined
with it. Bare ``hallmark download`` still requires selectors or ``--interactive``.

CLI cloning first copies the complete catalog, then shows a download plan and
asks for confirmation. All cataloged files are selected unless ``--filter``
narrows the planned transfers. These examples use separate destinations:

.. code-block:: bash

   hallmark clone ./lab/.hm ./lab-copy-selected --filter 'runs/run_001.h5'
   hallmark clone ./lab/.hm ./lab-copy-interactive --interactive

The initialization filter controls catalog membership. Clone filters and the
chooser select only downloads, retaining the complete catalog and its history.
``clone --interactive`` opens the chooser instead of the default plan and cannot
be combined with ``--filter``. ``clone --no-download`` skips review entirely and
cannot be combined with ``--interactive``, ``--filter``, or ``--output``.
``clone --output DIRECTORY`` changes the download destination; a bare catalog
prompts for one if omitted. Declining or EOF during clone's review exits
successfully and leaves the catalog available. Ctrl+C or a transfer failure exits
unsuccessfully while preserving it. Empty catalogs and catalogs without a data
remote skip review with an explanation.

Without an interactive terminal, clone still copies the catalog, warns that
approval is unavailable, and prints repository-specific preview and download
commands reflecting its filters and output directory. Later transfers still
require explicit approval. Standalone
``download --interactive`` requires a terminal. Existing source requirements,
such as an explicit DESI release in noninteractive use, still apply.
Download worker limits belong on ``download``; ``init`` and ``clone`` do not
accept them. Python initialization and cloning remain metadata-only.

5. Use the Python API
---------------------

Create a catalog and inspect a plan before transferring selected data:

.. code-block:: python

   from hallmark import Repo

   repo = Repo.init(
       "lab-python", source="sftp://lab-data/srv/exports/lab/",
       filter=["runs/*.h5", "README*"], format="runs/run_{run:03d}.h5",
       progress=True,
   )
   plan = repo.plan_download(filter="runs/run_001.h5")
   print(plan.summary())

After reviewing the plan, approve it explicitly and check the results:

.. code-block:: python

   result = repo.download(plan, approved=True, max_workers=4, progress=True)
   if result["failed"]:
       raise RuntimeError("; ".join(result["errors"]))

``Repo.init("desi", source="desi", release="dr1", collections=["redshifts"])``
uses the same named source. Python never prompts for releases. Plans freeze
paths, checksums, transport options, source and destination; subsequent catalog
configuration changes do not redirect them. ``estimated_bytes_per_second`` can
provide a duration estimate when every selected file has a known size.

6. Share a catalog
------------------

Clone an existing catalog without download review, then select downloads separately:

.. code-block:: bash

   hallmark clone ./lab/.hm ./lab-copy --no-download
   cd lab-copy
   hallmark download --filter 'runs/run_001.h5' --dry-run
   hallmark download --filter 'runs/run_001.h5'

Git clones preserve the full catalog and Git history. Published HTTP/SFTP
snapshots start new local history. Use ``--source-type git`` or
``--source-type catalog`` to override source detection. The catalog can be
hosted independently from the dataset, for example GitHub metadata with an SFTP
data remote. Metadata cloning does not connect to that data remote. Each client
configures its own SSH alias and credentials for later transfers.

Downloads alone do not populate the local object store. Use a local repository
and the normal ``add`` and ``commit`` workflow to version downloaded inputs.
Local filename-format indexing retains its existing behavior.

7. Migrate older commands
-------------------------

This interface change intentionally provides no argument aliases. Existing
catalogs retain their local formats and transport metadata. Stored authentication
profile references produce an actionable migration error.

.. list-table:: Interface migration
   :header-rows: 1
   :widths: 35 65

   * - Previous interface
     - Replacement
   * - ``init --backend`` / ``--backend-options``
     - ``--from NAME --release NAME --collection NAME``; source authors configure adapters.
   * - ``init --include``
     - ``init --filter`` selects catalog entries.
   * - ``init --extract`` / ``--fmt``
     - ``--format`` overrides automatic detection; use ``--filter`` for selection.
   * - ``download --include`` / ``--fmt``
     - ``download --filter`` with path globs.
   * - ``init --with-download``
     - Initialize metadata, then run ``download`` separately.
   * - ``clone --with-download`` / ``--download``
     - Plain ``clone`` reviews downloads by default; ``--interactive`` opens the chooser.
   * - ``clone --no-fetch-data`` or relying on catalog-only CLI defaults
     - ``clone --no-download`` copies the complete catalog without download review.
   * - Clone filtering
     - ``clone --filter`` selects payloads only; the catalog remains complete.
   * - ``--auth``, ``--remote-auth``, ``--dataset-auth``
     - Configure ``~/.ssh/config`` and use its host alias in the URL.
   * - ``Repo.init(from_url=...)``
     - ``Repo.init(source=...)``.
   * - Python ``include=`` / ``extract=`` / remote ``fmt=``
     - ``filter=`` and initialization-only ``format=``.
   * - Python clone/init download arguments and approval callbacks
     - ``repo.plan_download()`` followed by ``repo.download(plan, approved=True)``.

Remove obsolete ``auth`` fields from catalog ``config.yml`` after configuring
the host in SSH configuration. Hallmark neither reads nor deletes
``~/.config/hallmark/auth.yml``; ``HALLMARK_AUTH_FILE`` is ignored. Python profile
arguments and CLI backend-setting options have been removed. Existing
``backend`` and ``backend_options`` catalog metadata remain supported for
transport plugins. The legacy remote builder remains deprecated.

8. Transfer failures and limits
-------------------------------

Files are downloaded to temporary paths and published atomically after checksum
verification. Successful files remain if another transfer fails. Cancelling
stops owned SSH processes and waits for workers; HTTP checks cancellation
between chunks and remains subject to request timeouts. Missing checksums cannot
verify content. Setup and authentication failures stop the operation.

If SSH fails, check the alias with the SFTP command above, trusted host keys,
agent access and server permissions. If HTTP discovery fails, check that the
URL publishes a supported directory index. Large roots can take many listing
requests even when the final catalog contains few matching files.
