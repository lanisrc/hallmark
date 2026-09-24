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
It has three architecture components with different responsibilities:

1.  ``State``:
    the canonical in-memory data container, where all index mutations
    happen (add/remove/index);

2.  ``Dothm``:
    an on-disk version-controlled repository, persisting ``State`` and
    providing immutable history (using ``git``); and

3.  ``Worktree``:
    an on-disk working tree/directory, where data files are discovered
    and consumed.

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
    Each ``hallmark ...`` command loads ``State`` from a discovered
    ``Dothm`` repository, executes the requested operation, then
    writes staged state updates back to ``Dothm`` before exit.
    In this mode, ``State`` is short-lived and ``Dothm`` is required.

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
