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
    providing immutable history (using ``git``); and

3.  ``Worktree``:
    an on-disk working tree/directory, where data files are discovered
    and consumed; and

4.  ``Objects``:
    file contents in ``.hm/objects``, stored by SHA-1 as git stores blobs.
    ``commit`` copies local files in and ``checkout`` copies them out as
    writable files; downloads are published there and hard-linked into the
    ``Worktree``, read-only.

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
2.  hard-link the stored object into the ``Worktree``;
3.  record its SHA-1 in the ``State``, read again under a lock; and
4.  save the ``State`` to ``Dothm``, staged for the user to commit.

A crash between any two steps leaves the repository consistent: at worst a
temporary file, a stored object not yet linked, or a linked file whose SHA-1
the next download records without transferring it again.

The catalogue itself is ``config.yml``, ``meta.yml`` and ``data.tsv``, with
one line per file. See ``doc/data_backend.md`` for every command's effect on
these files and on ``.hm/objects``.


API
---

Currently, |hallmark|_ has two built-in APIs:

1.  Python API:
    the native API that all |hallmark|_ features are implemented in.
    ``State`` is the active object during the process lifetime.
    ``Repo`` opens an on-disk repository. For an in-memory file index,
    ``ParaFrame`` can discover and select files without ``Dothm`` or
    ``Worktree``.

2.  CLI:
    python features wrapped by ``click``.
    Repository commands such as ``hm add`` load ``State`` from a discovered
    ``Dothm`` repository, execute the requested operation, then
    write staged state updates back to ``Dothm`` before exit.
    ``State`` is short-lived. ``hm init`` and ``hm clone`` create the repository;
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


..  |hallmark| replace:: ``hallmark``

..  _hallmark: https://github.com/l6a/hallmark
