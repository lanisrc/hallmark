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
It provides a simple abstraction and a uniform Application Programming
Interface (API) on top of different backend technologies such as
`POSIX file system <https://en.wikipedia.org/wiki/Unix_filesystem>`_,
`object storage    <https://aws.amazon.com/s3/>`_,
`globus            <https://www.globus.org/>`_,
`iRODS             <https://irods.org/>`_,
`stream            <https://en.wikipedia.org/wiki/Streaming_data>`_,
etc.
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
can be found in the Jupyter Notebook tutorials in the ``demos`` folder.

``Installation``
-----------------

Install |hallmark|_ from PyPI::

    pip install hallmark

Or install from source for development purposes::

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

Hallmark can fetch cataloged files from HTTP(S), SSH and SFTP data remotes.
Data remotes locate file bytes separately from the Git remotes used for catalog
history. SSH uses system OpenSSH 9.6+, a trusted host key and noninteractive
key/agent authentication::

    hallmark set-config --remote-name campus --remote-url ssh://campus/srv/export/
    hallmark download --remote campus --all --dry-run
    hallmark download --remote campus --all

Optional local auth profiles can be selected with ``--remote-auth PROFILE``.
Private catalogs can be built with ``build --dataset-url URL --dataset-auth
PROFILE --allow-remote-commands``; SSH listing requires a POSIX shell and Python 3
on the server. SFTP-only accounts can download known files. See
`Private lab data over SSH <doc/usecase.rst#private-lab-data-over-ssh>`_ for setup,
profiles, bounded hashing, platform scope and disposable integration tests.
