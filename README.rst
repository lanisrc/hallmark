|hallmark|_
===========

    Reproducibility is the |hallmark|_ of the scientific method.

..  image:: https://codecov.io/gh/lanisrc/hallmark/graph/badge.svg?branch=main
    :target: https://codecov.io/gh/lanisrc/hallmark
    :alt: Coverage

Modern science has become so complex that many science projects rely
on multiple software packages to work in unison, resulting in networks of
data products along the analyses.
Versioning and managing these data products are essential in making
modern data- and computation-intensive science reproducible.

Motivated by the `Event Horizon Telescope (EHT) <eht_>`_'s
observational data calibration pipelines and theory data analyses
tools, |hallmark|_ is a lightweight package designed to version
control and manage data products in a complex workflow.
It provides a common Python API and CLI for local files and remote data
accessed through HTTP(S), WebDAV, S3, SSH/SFTP, and CyVerse. Additional data
services can be supported through backend plugins.
By using |hallmark|_ with other packages such as |yukon|_ and
|banyan|_ in `Project Laniakea <l6a_>`_, researchers can utilize
computing infrastructures in a global scale to accelerate their
science.

``ParaFrame``
-------------

``ParaFrame`` is a specialized subclass of ``pandas.DataFrame`` that 
automatically extracts parameters encoded in file paths. When performing 
large-scale parameter surveys or building simulation libraries, parameters 
are often encoded directly in file naming schemes (e.g., 
``Ma+0.94_i70/sed_Rh160.h5``).

Features: 

* **Decodes file paths** back to structured parameters using Python format strings
* **Builds DataFrames** with parsed parameters as columns
* **Supports custom encodings** via YAML configuration for complex parsing rules
* **Provides intuitive filtering** for parameter selection—easier than pure pandas

``Tutorial``
-------------

Examples of using ``ParaFrame`` with Python API or Command Line Interface (CLI)
can be found in the Jupyter Notebook tutorials in the ``demo`` folder.
``demo/demo_private_data.ipynb`` shows several sources, the object store and
private servers.

``Installation``
-----------------

Python 3.9 or newer and Git are required. Install a released version from PyPI::

    pip install hallmark

The examples below describe the development version. Install this checkout
to use its current API::

    git clone https://github.com/lanisrc/hallmark.git
    cd hallmark
    pip install -e .

..  |hallmark| replace:: ``hallmark``
..  |yukon|    replace:: ``yukon``
..  |banyan|   replace:: ``banyan``

..  _l6a:      https://github.com/lanisrc
..  _hallmark: https://github.com/lanisrc/hallmark
..  _yukon:    https://github.com/lanisrc/yukon
..  _banyan:   https://github.com/lanisrc/banyan
..  _eht:      https://eventhorizontelescope.org

Private data remotes
--------------------

|hallmark|_ can download data products from HTTP(S), SSH, and SFTP servers.
A data remote specifies where the files are stored, while a Git remote
is used to share the history of the data index.
For a private server, |hallmark|_ uses OpenSSH 8.0 or newer with a trusted
host key and key-based authentication that does not require a prompt.
Servers that require interactive two-factor authentication (such as Duo on
some HPC login nodes) are not supported yet.

Create a repository, stage a remote catalog, then commit it::

    hm init campus
    cd campus
    hm add 'ssh://campus/srv/export/' --filter '**/*.h5'
    hm commit -m 'Add remote data'
    hm download --all --dry-run
    hm download --all

``hm`` is the CLI command; ``hallmark`` remains an alias. Initialization creates the empty
local repository. Remote ``add`` discovers files without downloading dataset
contents or committing the catalog. CyVerse, ordinary browsable HTTPS
directories, SSH and SFTP use the same workflow. Omit the filter to catalog
all discoverable files beneath the supplied URL.

Repeat ``--fmt`` to catalog several filename formats at once, each as ``FMT``
or ``FMT=DB``. Each format's files go to their own TSV, and files that match no
format are listed as ``file:`` entries in ``config.yml``. ``hm detect REMOTE``
suggests formats for a configured remote, or a folder inside it, and ``--add``
catalogs with them right away::

    hm detect origin
    hm add 'ssh://campus/srv/export/' --fmt 'run{run:d}.h5=runs' --fmt 'logs/{name}.log=logs'

Every nonempty CLI transfer asks for confirmation. For separate Python
downloads, callers can inspect
``repo.plan_download()`` and then execute ``repo.download(plan, approved=True)``.
Optional authentication profiles for SSH, HTTP(S) and S3 remain local and can be
selected with ``add --auth PROFILE`` or ``set-config --remote-auth PROFILE``.
Profiles name where each secret comes from (an environment variable, the
system keyring or a password-manager command); no secret is stored.
The obsolete ``build`` and remote initialization interfaces have been removed.
Use ``init``, ``add`` and ``commit`` for new remote catalogs.

Use ``hm clone CATALOG PATH`` for an existing Git-hosted ``.hm`` or a
published HTTP/SFTP catalog snapshot. Catalogs can live on GitHub or another
server while their data remotes point elsewhere. Git clones preserve the full
catalog and its history. Clone displays a data download plan and asks for
confirmation by default; declining keeps the catalog. Use ``hm clone CATALOG
PATH --no-download`` for catalog only. Filters narrow the download and cannot
accompany ``--no-download``. Python ``Repo.clone`` downloads only with
approval, an ``approve(plan)`` callback or ``approved=True``, as
``Repo.download`` requires; use ``download=False`` to skip.
Bare destinations require ``--no-download`` / ``download=False``.

The public ``DataBackend`` interface supports generic HTTP/SSH, WebDAV,
S3-compatible storage (``pip install hallmark[s3]``), CyVerse, and registered
plugins for collaboration-specific APIs.
See `Data backends <doc/backends.rst>`_ for configuration and an extension example.

Examples and configuration details are described in
`Private data over SSH and SFTP <doc/private_data.rst>`_.

Current limits
--------------

Remote ``add`` catalogs one or more remote selections (sources), possibly on
different servers. Each filename format of the catalog has its own TSV, and a
single format uses ``data.tsv``. Keep remote catalogs separate from locally
versioned files.
Local ``add`` selects files by one filename format; ``commit`` stores their
contents in ``.hm/objects``. Clone copies catalog history and downloads from
its configured data remote; transfer of a local object store is not implemented.

Development and support
-----------------------

See the `contribution guidelines <doc/CONTRIBUTING.md>`_ for development and
review practices. Report bugs and request help through the
`issue tracker <https://github.com/lanisrc/hallmark/issues>`_. Include your Hallmark
version, operating system, and a minimal example without credentials or private data.

From a development checkout, install the validation tools and run::

    python -m pip install -e . --group dev --group lint --group doc
    python -m pytest --cov
    ruff check --select E,F mod test
    python -m sphinx -b html -W doc doc/_build

SSH integration tests require local OpenSSH server/client tools and
``HALLMARK_RUN_SSH_TESTS=1``; see `transport validation <doc/usecase.rst>`_.
