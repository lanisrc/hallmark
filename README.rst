|hallmark|_
===========

    Reproducibility is the |hallmark|_ of the scientific method.

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
accessed through HTTP(S), SSH/SFTP, and CyVerse. Additional data services
can be supported through backend plugins.
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

``Installation``
-----------------

Python 3.9 or newer and Git are required. Install a released version from PyPI::

    pip install hallmark

The examples below describe the development version. Install this checkout
to use its current API::

    git clone https://github.com/l6a/hallmark.git
    cd hallmark
    pip install -e .

..  |hallmark| replace:: ``hallmark``
..  |yukon|    replace:: ``yukon``
..  |banyan|   replace:: ``banyan``

..  _l6a:      https://github.com/l6a
..  _hallmark: https://github.com/l6a/hallmark
..  _yukon:    https://github.com/l6a/yukon
..  _banyan:   https://github.com/l6a/banyan
..  _eht:      https://eventhorizontelescope.org

Private data remotes
--------------------

|hallmark|_ can download data products from HTTP(S), SSH, and SFTP servers.
A data remote specifies where the files are stored, while a Git remote
is used to share the history of the data index.
For a private server, |hallmark|_ uses OpenSSH 9.6 or newer with a trusted
host key and key-based authentication that does not require a prompt.

Create a repository, stage a remote catalog, then commit it::

    hm init campus
    cd campus
    hm add 'ssh://campus/srv/export/' --filter '**/*.h5'
    hm commit -m 'Add remote data'
    hm download --all --dry-run
    hm download --all

``hm`` is the CLI command; ``hallmark`` remains an alias. Commands work from any
folder inside a repository; ``hm`` finds ``.hm`` in parent folders.
Initialization creates the empty local repository. Remote ``add`` discovers
files without downloading dataset contents or committing the catalog. CyVerse,
ordinary browsable HTTPS directories, SSH and SFTP use the same workflow. Omit
the filter to catalog all discoverable files beneath the supplied URL.

Every nonempty CLI transfer asks for confirmation. For separate Python
downloads, callers can inspect
``repo.plan_download()`` and then execute ``repo.download(plan, approved=True)``.
Optional authentication profiles remain local and can be selected with
``add --auth PROFILE`` or ``set-config --remote-auth PROFILE``.
The obsolete ``build`` and remote initialization interfaces have been removed.
Use ``init``, ``add`` and ``commit`` for new remote catalogs.

Use ``hm clone CATALOG PATH`` for an existing Git-hosted ``.hm`` or a
published HTTP/SFTP catalog snapshot. Catalogs can live on GitHub or another
server while their data remotes point elsewhere. Git clones preserve the full
catalog and its history. Clone displays a data download plan and asks for
confirmation by default; declining keeps the catalog. Use ``hm clone CATALOG
PATH --no-download`` for catalog only. Filters narrow the download and cannot
accompany ``--no-download``. Python ``Repo.clone`` downloads without prompting
unless an ``approve(plan)`` callback is supplied; use ``download=False`` to skip.
Bare destinations require ``--no-download`` / ``download=False``.

The public ``DataBackend`` interface supports generic HTTP/SSH, CyVerse, and
registered plugins for collaboration-specific APIs.
See `Data backends <doc/backends.rst>`_ for configuration and an extension example.

Examples and configuration details are described in
`Private data over SSH and SFTP <doc/private_data.rst>`_.

Current limits
--------------

Remote ``add`` supports one dataset root and one ``data.tsv`` table per
repository. Keep remote catalogs separate from locally versioned files.
Local ``add`` selects files by filename format; ``commit`` stores their
contents in ``.hm/objects``. Clone copies catalog history and downloads from
its configured data remote; transfer of a local object store is not implemented.

Development and support
-----------------------

See the `contribution guidelines <doc/CONTRIBUTING.md>`_ for development and
review practices. Report bugs and request help through the
`issue tracker <https://github.com/l6a/hallmark/issues>`_. Include your Hallmark
version, operating system, and a minimal example without credentials or private data.

From a development checkout, install the validation tools and run::

    python -m pip install -e . pytest ruff sphinx
    python -m pytest
    ruff check --select E,F mod test
    python -m sphinx -b html -W doc doc/_build

SSH integration tests require local OpenSSH server/client tools and
``HALLMARK_RUN_SSH_TESTS=1``; see `transport validation <doc/usecase.rst>`_.
