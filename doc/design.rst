Design
======

|hallmark|_ follows the
`Unix philosophy <https://en.wikipedia.org/wiki/Unix_philosophy#Origin>`_:

    Write programs that do one thing and do it well.
    Write programs to work together.
    Write programs to handle text streams, because that is a universal
    interface.

    -- Douglas McIlroy

The "one thing" for |hallmark|_ is maintaining a reproducible data
index.
With a well-designed indexing mechanism, it becomes natural to expose
a small set of core functions:

1.  **add/remove:**
    find data from any source and bring them into the index;

2.  **index:**
    compute checksums of data objects and index their relationships;

3.  **version control:**
    append immutable records; and

4.  **view:**
    emit manifests of subsets for other tools to consume.


Architecture
------------

A |hallmark|_ repository is the entry point for a version-controlled
dataset index.
It has four architecture components with different responsibilities:

1.  ``State``:
    the canonical in-memory data container, where all index mutations
    happen (add/remove/index);

2.  ``Dothm``:
    an on-disk version-controlled repository, persisting ``State`` and
    providing immutable history (using ``git``);

3.  ``Worktree``:
    an on-disk working tree/directory, where data files are discovered
    and consumed; and

4.  ``Objects``:
    file contents in ``.hm/objects``, stored by SHA-1 as git stores blobs.
    ``commit`` copies local files in and ``checkout`` copies them out as
    writable files;
    downloads are published there and hard-linked into the ``Worktree``,
    read-only.

The data flow can be summarized as::

                Repo
     ____________/\____________
    /                          \

    State ---persist-------+
      ^                    |
      |                    |
      |                    v
      +----instantiate-- Dothm (".hm" git repository)
      |                    ^
      |                    |link
      |                    v
      +-----discover---- Worktree
                           |
                           |access
                           v
                         Other tools

Downloads keep ``Worktree``, ``Objects``, ``State`` and ``Dothm`` in step
(``Worktree <-> Objects <-> State <-> Dothm``), moving between them only in
this order:

1.  download into ``Objects``, into a temporary file that gets its final
    name only once complete and verified;

2.  hard-link the stored object into the ``Worktree``, atomically replacing
    an outdated earlier download whose bytes stay in ``Objects``;

3.  record its SHA-1 in the ``State``, read again under a lock; and

4.  save the ``State`` to ``Dothm``, staged for the user to commit.

A crash between any two steps leaves the repository consistent: at worst a
temporary file, a stored object not yet linked, or a linked file whose SHA-1
is not recorded.
The next download records it, without a transfer when the catalog has a
checksum for the file and after fetching it to compare when not.

The catalog itself is ``config.yml``, ``meta.yml`` and one TSV per filename
format (``data.tsv`` for a single format, or for none), with one line per file;
files no format matches are ``file:`` entries in ``config.yml``.


API
---

Currently, |hallmark|_ has two built-in APIs:

1.  Python API:
    the native API that all |hallmark|_ features are implemented in.
    ``State`` is the active object during the process lifetime.
    ``Repo`` opens an on-disk repository.
    For an in-memory file index, ``ParaFrame`` can discover and select
    files without ``Dothm`` or ``Worktree``.

2.  CLI:
    python features wrapped by ``click``.
    Repository commands such as ``hm add`` load ``State`` from a discovered
    ``Dothm`` repository, execute the requested operation, then
    write staged state updates back to ``Dothm`` before exit.
    ``State`` is short-lived.
    ``hm init`` and ``hm clone`` create the repository;
    inspection commands do not write staged state.

``Worktree`` follows the same idea as
`git worktree <https://git-scm.com/docs/git-worktree>`_:
multiple ``repo/.hm`` directories can be attached to one underlying
repository to support parallel data transformations and
branch-isolated workflows.


Repository ``Repo``
-------------------

|hallmark|_ supports three repository forms with the same internal
``Dothm`` data model:

1.  standard repository:::

         +--- Worktree
	 v
        "repo/.hm/" <--- Dothm, a standard git repo

2.  bare repository:::

        "repo.hm/" <--- Dothm, a standard git repo without a data worktree

3.  shared repository (multiple worktrees):::

         +--- Worktree                     git repo
         v                                        ^
        "repo1/.hm/" <--- Dothm, a git worktree --+
                                                  |
         +--- Worktree                            |
         v                                        |
        "repo2/.hm/" <--- Dothm, a git worktree --+
                                                  |
         +--- Worktree                            |
         v                                        |
        "repo3/.hm/" <--- Dothm, a git worktree --+


Private Data
------------

|hallmark|_ catalogs a remote dataset where it lives: dataset files stay on
their servers until explicitly downloaded.
A data remote is independent of the Git remote or snapshot URL that shares
the catalog, so a catalog can live on GitHub while its data stays on a lab
server.
A remote catalog adds four components:

1.  data remote:
    a named URL in ``config.yml``, with an optional backend and profile name;

2.  source:
    the remote, folder, prefix and filters of one ``hm add``, named in each
    row's ``catalog_source`` column;

3.  ``DataBackend``:
    lists and transfers files for one kind of server, see :doc:`backends`; and

4.  profile:
    settings in a local ``auth.yml``, of which the catalog records only the
    name; secrets come from an environment variable, the system keyring or a
    helper command.

The data flow can be summarized as::

    Server ---list---> add ---stage---> State ---commit---> Dothm
      |                                   |
      |                                   |plan, approve
      |                                   v
      +-----------fetch------------> Objects ---link---> Worktree

Discovery reads listings and published checksum manifests, never dataset
files, and stages its catalog only after a complete scan.
A server without a usable listing needs a published catalog or a backend
plugin.
Filename formats belong to the whole catalog and sort the files of every
source.

The ``checksum_source`` column records where each checksum came from:
``published`` for a manifest such as ``SHA256SUMS``, or ``server`` for a
digest from the service.
A missing checksum stays unknown until a download records the file's SHA-1.

Downloaded files mirror each server's paths, so two sources cannot catalog
the same path; ``hm add`` lists each clash and offers to skip it, place the
new source under a prefix, or abort.
Files gone from the server leave the catalog only by choice (``--prune``).
Files that changed on the server are replaced by the next approved download,
with the old bytes kept in ``Objects``.

Planning reads only the local catalog and makes no network requests.
A ``DownloadPlan`` freezes its files, remote, backend and destination, and
every nonempty transfer needs approval: a prompt in the CLI, or
``approved=True`` in Python.
Conflicts with local files stop the selection before any transfer.
A multi-file download is not a single transaction: a failed file leaves the
others in place, and running it again transfers only the rest.

Downloaded files are read-only links into ``.hm/objects``, so copy one to a
new name before editing it.
Never delete ``.hm/objects`` by hand; it also holds committed local files
that may exist nowhere else.

SSH and SFTP URLs both transfer over SFTP, using OpenSSH 8.0 or newer.
Authentication must succeed without password, two-factor or initial
hardware-key prompts.
The server needs no |hallmark|_, Git, Python or login shell.
``hm add`` and ``hm set-config`` reject URLs containing a password, an
HTTP(S) username or token, a query string or a fragment.

Keep remote catalogs separate from locally versioned files;
cloning does not transfer a local object store.

See :doc:`usecase` for an SSH workflow and ``demo/demo_private_data.ipynb``
for worked examples with several sources, S3, logins, clones and mirrors.


..  |hallmark| replace:: ``hallmark``

..  _hallmark: https://github.com/lanisrc/hallmark
