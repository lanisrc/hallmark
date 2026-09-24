Data sources and private data
=============================

``hallmark add`` catalogs remote files that match a URL template. It reads
directory listings and published checksum metadata without downloading dataset
files or offering transfers. ``hallmark ls-remote`` lists a remote directory
and suggests templates. ``hallmark clone`` copies a complete existing catalog,
then reviews a download plan by default. Use ``clone --no-download`` to skip
review. ``hallmark download`` selects and approves transfers from a catalog.

1. Choose a source
------------------

List supported sources, then inspect DESI releases and collection roots. These
commands read source definitions without listing a dataset:

.. code-block:: bash

   hallmark sources
   hallmark sources desi

DESI supports EDR and DR1. Its collections are ``redshifts`` (merged redshift
catalogs), ``spectra`` (HEALPixel and tile product directories), and ``vac``
(value-added products). DR1 also provides ``lss`` (large-scale-structure
catalogs). The mappings follow the `DESI data organization
<https://data.desi.lbl.gov/doc/organization/>`_.

``hallmark sources desi`` prints the URL of each release and collection. List
a collection to see its files and suggested templates, then add one of the
suggested commands to a repository:

.. code-block:: bash

   hallmark init ./desi
   cd desi
   hallmark ls-remote https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/zcatalog/
   hallmark add 'https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/{pixel}/redrock-main-dark-{pixel}.fits'
   hallmark commit -m 'Catalog DR1 redshifts'

The source name only helps you find URLs; the catalog records the URL itself.
A release that declares a transport backend supplies it to templates beneath
its URL. Any browsable URL works the same way:

.. code-block:: bash

   hallmark add 'https://data.example.org/export/{site}/{night}.fits'

HTTP sources need a supported browsable index; HTTP file access alone does not
provide directory listings. Public HTTP sources need no credentials; private
HTTP retains Requests' local ``.netrc`` and environment behavior. URLs with
credentials, queries or fragments are rejected, because the URL is committed
with the catalog. See :doc:`backends` for source and transport plugins.

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
``sftp://`` use structured SFTP listings and transfers. The server needs no
Hallmark, Git, Python or login shell. SSH configuration supplies identities,
users, ports and proxies, including ``ProxyJump``; explicit URL user/port values
take precedence. Hallmark enforces noninteractive access and strict host-key
checking. There is no Hallmark authentication file or profile-saving prompt.

3. Write a template
-------------------

Suppose the server contains ``runs/run_001.h5``, ``runs/run_002.h5`` and
``README.md``. Preview the run files, then catalog them and commit:

.. code-block:: bash

   hallmark init ./lab
   cd lab
   hallmark add -n 'sftp://lab-data/srv/exports/lab/runs/run_{run}.h5'
   hallmark add 'sftp://lab-data/srv/exports/lab/runs/run_{run}.h5'
   hallmark commit -m 'Catalog lab runs'

The URL is split at the first path segment containing a ``{field}``:
``sftp://lab-data/srv/exports/lab/runs/`` becomes the template's source and
``run_{run}.h5`` its filename template. Files are cataloged, and later
downloaded, at paths relative to the source, here ``run_001.h5``. Put a
directory in the template to keep it in the local layout:
``sftp://lab-data/srv/exports/lab/{group}/run_{run}.h5`` catalogs
``runs/run_001.h5``.

Matching follows local ``hallmark add``:

- Each field matches text within one path segment and never spans ``/``.
- Matching is case-sensitive. Hidden files and directories match only a
  template segment that starts with ``.``.
- A file is cataloged only if the template re-creates its name exactly, so
  ``run_{run:03d}.h5`` matches ``run_001.h5`` but not ``run_1.h5``. ``add``
  reports how many listed files fit the pattern but not its formatting.
- Field names cannot be reserved catalog columns such as ``path``,
  ``checksum`` or ``size_bytes``.
- Literal parts of the URL are percent-decoded once: ``%20`` names a space and
  ``%7B`` a literal brace.

Only directories the template can match are listed, so a template deep in a
large tree reads few listings. A template that matches nothing is an error and
changes nothing. The catalog records each file's field values, size,
modification time and published checksum; unknown values remain unknown.
Hallmark never downloads payloads to compute hashes. Publish SHA-256 manifests
for reproducible verification where possible. Remote files are committed as
catalog entries, not as local objects.

Run the same ``add`` command again to sync the template with the server. New
files are added, changed sizes or checksums are updated, and files that are no
longer listed are removed from the catalog. ``hallmark status`` summarizes
these changes per template before you commit them.

When you do not know the layout, ``hallmark ls-remote URL`` lists the directory
and prints suggested templates, with the number of files each matches.
Suggestions come from Hallmark's filename-format detector and are heuristic:
``run_001.h5`` and ``run_002.h5`` yield ``run_{scan}.h5``. Rename fields before
adding a suggestion. ``ls-remote`` lists the whole tree below the URL and never
changes a repository.

A branch can track several templates. Each has its own source and catalog
table, and local templates, such as ``reduced/{run}.dat``, can sit beside remote
ones. A file belongs to only one template. ``hallmark rm --cached TEMPLATE``
stops tracking a template and leaves its files in place. ``hallmark init PATH``
creates an empty repository; a PATH ending in ``.hm`` creates a bare catalog.

4. Preview and approve downloads
--------------------------------

From the worktree, preview one run using only local catalog metadata, then
request the same transfer:

.. code-block:: bash

   hallmark download --include 'run_001.h5' --dry-run
   hallmark download --include 'run_001.h5'

The download command displays the file count, known bytes, unknown-size count,
sources and destination, then asks for confirmation. Declining leaves the
catalog available. ``--include`` selects only cataloged files; explicit paths
cannot add files that no template catalogs. Downloads do not change catalog
membership. Use ``--all`` for the whole catalog or ``--tsv data.tsv`` for one
template's table. A bare catalog requires ``--output DIRECTORY`` with explicit
selectors; the interactive chooser can prompt for it. ``--max-workers``
defaults to 4 and must be positive. Internal SSH session limits may further cap
concurrency.

Files of each remote template download from its source; files of local
templates download from the default data remote, if one is configured. A plan
can therefore span several servers, and its preview lists each source with its
file count. ``--remote NAME`` downloads every selected file from a data remote
configured with ``set-config`` instead. Selected files without any source are
skipped and counted.

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

The preview shows the sources, destination, file count, known bytes,
unknown-size count, and up to 20 paths with recorded sizes. It uses local
metadata only; unknown sizes remain unknown. Choose ``download`` to approve
that exact plan, ``change`` to select again and review a new plan, or ``skip``
(the default). ``--interactive --dry-run`` previews the selection and exits
without offering execution. ``--output``, ``--remote`` and ``--max-workers`` are
supported with ``--interactive``; paths, ``--include``, ``--tsv`` and ``--all``
cannot be combined with it. Bare ``hallmark download`` still requires
selectors or ``--interactive``.

CLI cloning first copies the complete catalog, then shows a download plan and
asks for confirmation. All cataloged files are selected unless ``--include``
narrows the planned transfers. These examples use separate destinations:

.. code-block:: bash

   hallmark clone ./lab/.hm ./lab-copy-selected --include 'run_001.h5'
   hallmark clone ./lab/.hm ./lab-copy-interactive --interactive

Templates control catalog membership. Clone ``--include`` globs and the chooser
select only downloads, retaining the complete catalog and its history.
``clone --interactive`` opens the chooser instead of the default plan and cannot
be combined with ``--include``. ``clone --no-download`` skips review entirely and
cannot be combined with ``--interactive``, ``--include``, or ``--output``.
``clone --output DIRECTORY`` changes the download destination; a bare catalog
prompts for one if omitted. Declining or EOF during clone's review exits
successfully and leaves the catalog available. Ctrl+C or a transfer failure exits
unsuccessfully while preserving it. Empty catalogs and catalogs without a data
source skip review with an explanation.

Without an interactive terminal, clone still copies the catalog, warns that
approval is unavailable, and prints repository-specific preview and download
commands reflecting its ``--include`` globs and output directory. Later
transfers still require explicit approval. Standalone ``download --interactive``
requires a terminal. Download worker limits belong on ``download``; ``init``,
``add`` and ``clone`` do not accept them. Python ``add`` and cloning remain
metadata-only.

5. Use the Python API
---------------------

Catalog remote files and inspect a plan before transferring selected data:

.. code-block:: python

   from hallmark import Repo

   repo = Repo.init("lab-python")
   repo.add("sftp://lab-data/srv/exports/lab/runs/run_{run:03d}.h5",
            progress=True)
   repo.commit("Catalog lab runs")
   plan = repo.plan_download(include="run_001.h5")
   print(plan.summary())

After reviewing the plan, approve it explicitly and check the results:

.. code-block:: python

   result = repo.download(plan, approved=True, max_workers=4, progress=True)
   if result["failed"]:
       raise RuntimeError("; ".join(result["errors"]))

``repo.add(url_template, dry_run=True)`` returns the matching files without
staging them. ``repo.add(url_template, backend=..., backend_options=...)``
records a transport plugin for the template. Plans freeze paths, checksums,
sources, transport options and destination; subsequent catalog configuration
changes do not redirect them. ``plan.sources`` lists the servers a plan
contacts. ``estimated_bytes_per_second`` can provide a duration estimate when
every selected file has a known size.

6. Share a catalog
------------------

Clone an existing catalog without download review, then select downloads separately:

.. code-block:: bash

   hallmark clone ./lab/.hm ./lab-copy --no-download
   cd lab-copy
   hallmark download --include 'run_001.h5' --dry-run
   hallmark download --include 'run_001.h5'

Git clones preserve the full catalog and Git history. Published HTTP/SFTP
snapshots start new local history. Use ``--source-type git`` or
``--source-type catalog`` to override source detection. The catalog can be
hosted independently from the dataset, for example GitHub metadata with SFTP
data sources. Metadata cloning does not connect to those sources. Each client
configures its own SSH alias and credentials for later transfers.

Downloads do not populate the local object store. To version downloaded files
locally, run ``hallmark rm --cached`` on the remote template and add the same
template as a local one.

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
   * - ``init --from URL`` / ``--release`` / ``--collection``
     - ``hallmark init PATH``, then ``hallmark add 'URL/TEMPLATE'``; find URLs
       with ``hallmark sources`` and ``hallmark ls-remote``.
   * - ``init --filter`` / ``--include``
     - The template selects files; ``hallmark add -n`` previews the matches.
   * - ``init --format`` / ``--extract`` / ``--fmt``
     - The template's fields; ``hallmark ls-remote`` suggests templates.
   * - ``init --backend`` / ``--backend-options``
     - ``Repo.add(url_template, backend=..., backend_options=...)``; source
       authors can declare a backend on a release.
   * - ``download --filter`` / ``--fmt``
     - ``download --include`` with path globs.
   * - ``clone --filter``
     - ``clone --include`` selects payloads only; the catalog remains complete.
   * - ``init --with-download``
     - Add a template, then run ``download`` separately.
   * - ``clone --with-download`` / ``--download``
     - Plain ``clone`` reviews downloads by default; ``--interactive`` opens the chooser.
   * - ``clone --no-fetch-data`` or relying on catalog-only CLI defaults
     - ``clone --no-download`` copies the complete catalog without download review.
   * - Adding a different template to replace a branch's catalog
     - The template is added beside the others; ``hallmark rm --cached`` removes one.
   * - ``--auth``, ``--remote-auth``, ``--dataset-auth``
     - Configure ``~/.ssh/config`` and use its host alias in the URL.
   * - ``Repo.init(source=...)`` / ``Repo.init(from_url=...)``
     - ``Repo.init(path)`` followed by ``repo.add(url_template)``.
   * - Python ``filter=``
     - ``include=``.
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
agent access and server permissions. If listing fails, check that the URL
publishes a supported directory index. ``ls-remote`` lists the whole tree below
its URL, which can take many requests for a large root; ``add`` lists only the
directories its template can match.
