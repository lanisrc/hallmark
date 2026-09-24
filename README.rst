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
can be found in the Jupyter Notebook tutorials in the ``demo`` folder.

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

|hallmark|_ can catalog and download data products stored on HTTP(S), SSH, and
SFTP servers. A data source specifies where the files are stored, while a Git
remote is used to share the history of the data index.
For a private server, |hallmark|_ uses OpenSSH 9.6 or newer with a trusted
host key and key-based authentication that does not require a prompt.

Add remote files to a repository with a URL template, then select files to
download::

    hallmark init eht
    cd eht
    hallmark add 'https://archive.example.org/2026MOVIE/ER2/{src}_{day}.h5'
    hallmark commit -m 'Add remote EHT data'
    hallmark download --all --dry-run
    hallmark download --all

``add`` lists the remote directory and catalogs the matching files, with their
sizes and published checksums, without downloading dataset contents. The URL
up to the first path segment containing a ``{field}`` is recorded as the
template's source; the rest is the filename template, and its fields become
catalog columns. Adding the same URL template again syncs the catalog with the
server, including files that were removed. A branch can track several
templates, local or remote, such as ``{run}.dat`` files in the worktree and
``.h5`` files on a server; ``hallmark rm --cached TEMPLATE`` stops tracking
one. Remote files are committed as catalog entries, not as local objects.

Use ``hallmark ls-remote URL`` to list a directory and print suggested
templates, and ``hallmark add -n 'URL/TEMPLATE'`` to preview the matches.
CyVerse and ordinary browsable HTTPS directories, including DESI, use the same
workflow. SSH listing also works with SFTP-only accounts; no server shell or
Python is required.

Every dataset transfer requires approval. Python callers can inspect
``repo.plan_download()`` and then execute ``repo.download(plan, approved=True)``.
CLI users can run ``hallmark download --interactive`` to choose path globs or
all cataloged files, review recorded sizes, then download, change the selection,
or skip. ``init`` and ``add`` never offer downloads. CLI cloning copies the
complete catalog, then displays a download plan and requests confirmation by
default. Use ``clone --interactive`` for the chooser, or ``clone --no-download``
to copy only the catalog. Without a terminal, clone keeps the catalog, skips
downloading, and prints commands to use later.
SSH and SFTP use standard ``~/.ssh/config`` aliases and the SSH agent.
Use ``hallmark sources desi`` to list the release and collection URLs of a
named source for use with ``ls-remote`` and ``add``. Download selection belongs
to the separate ``download --include`` command.
The older remote ``build`` command is deprecated in favor of ``add``.

Use ``hallmark clone CATALOG PATH`` for an existing Git-hosted ``.hm`` or a
published HTTP/SFTP catalog snapshot. Catalogs can live on GitHub or another
server while their data sources point elsewhere. Git clones preserve the full
catalog and its history. ``clone --include`` narrows the planned downloads
without removing catalog entries. No payloads transfer until the user approves
the plan.

The public ``DataBackend`` interface supports generic HTTP/SSH, CyVerse, and
registered plugins for collaboration-specific APIs and multiple data servers.
See `Data backends <doc/backends.rst>`_ for configuration and an extension example.

Examples and configuration details are described in
`Private data over SSH and SFTP <doc/private_data.rst>`_.

For a sequential CLI walkthrough, see the
`private data demo <demo/private_data_demo.md>`_. It includes catalog
initialization, SSH configuration and explicit download approval.

The `scientific CLI workflows <demo/scientific_workflows_cli.md>`_ and
`scientific Python notebook <demo/scientific_workflows_python.ipynb>`_
follow four examples using EHT, DESI, Roman–Rubin and local DES data.
