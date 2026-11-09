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
accessed through HTTP(S), WebDAV, S3, SSH/SFTP, and CyVerse.
Additional data services can be supported through backend plugins.
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

Python 3.9 or newer and Git are required.
Install a released version from PyPI::

    pip install hallmark

The examples below describe the development version.
Install this checkout to use its current API::

    git clone https://github.com/lanisrc/hallmark.git
    cd hallmark
    pip install -e .

Private data remotes
--------------------

|hallmark|_ catalogs data on remote servers without downloading it, and
transfers files only after approval::

    hm init campus
    cd campus
    hm add 'ssh://campus/srv/export/' --filter '**/*.h5'
    hm commit -m 'Add remote data'
    hm download --all --dry-run
    hm download --all

``hm`` is the CLI command; ``hallmark`` remains an alias.
For a private server, |hallmark|_ uses OpenSSH 8.0 or newer with a trusted
host key and key-based authentication that does not require a prompt.
Servers that require interactive two-factor authentication (such as Duo on
some HPC login nodes) are not supported yet.
See the `documentation <https://lanisrc.github.io/hallmark/>`_ for the
design, use cases and data backends.

Development and support
-----------------------

See the `contribution guidelines
<https://github.com/lanisrc/hallmark/blob/main/doc/CONTRIBUTING.md>`_ for
development and review practices.
Report bugs and request help through the
`issue tracker <https://github.com/lanisrc/hallmark/issues>`_.
Include your Hallmark version, operating system, and a minimal example
without credentials or private data.

From a development checkout, install the validation tools and run::

    python -m pip install -e . --group dev --group lint --group doc
    python -m pytest --cov
    ruff check --select E,F mod test
    python -m sphinx -b html -W doc doc/_build

SSH integration tests require local OpenSSH server/client tools.
They use a loopback server and temporary keys, and do not connect to a lab
server or change your SSH trust files::

    HALLMARK_RUN_SSH_TESTS=1 pytest test/test_ssh_integration.py -q

..  |hallmark| replace:: ``hallmark``
..  |yukon|    replace:: ``yukon``
..  |banyan|   replace:: ``banyan``

..  _l6a:      https://github.com/lanisrc
..  _hallmark: https://github.com/lanisrc/hallmark
..  _yukon:    https://github.com/lanisrc/yukon
..  _banyan:   https://github.com/lanisrc/banyan
..  _eht:      https://eventhorizontelescope.org
