Use Cases
=========

This section describes representative workflows supported by the
current |hallmark|_ CLI and Python API.


1. CLI: Standard Repository Ingest
----------------------------------

Alice is organizing telescope data in a normal directory.
She initializes that directory as a |hallmark|_ worktree::

    hallmark init obs
    cd obs

She adds the files using a python format-string pattern::

    hallmark add "{site}/{year:d}/{day:d}.fits"

She then commits the updated hallmark index::

    hallmark commit -m "Initial observation ingest"

She can inspect current repository paths at any time::

    hallmark info

She can also inspect current changes and commit history::

    hallmark status
    hallmark log

These git-like commands report or modify hallmark tracked state files
in ``.hm``.
Dataset files are represented through staged ``sha1`` column in
``data.tsv``, with other useful parameters (e.g., site, year, day),
associated with each file in different rows.


2. CLI: Bare Repository with Worktree Ingest
--------------------------------------------

Bob prefers managing a bare hallmark repository for storage and
staging data from a linked worktree.
He initialized the bare repository and verify its mode::

    hallmark init --bare sim.hm
    cd sim.hm
    hallmark info

He then attaches a worktree, stages discovered files, and commits::

    hallmark worktree add /data/outputs
    cd /data/outputs
    hallmark add "run{run:d}/frame{frame:d}.h5"
    hallmark status
    hallmark diff
    hallmark commit -m "Simulation snapshots"

The commit updates the same bare repository that owns the linked
worktree.


3. CLI: Branch-Isolated Analysis with Multiple Worktrees
--------------------------------------------------------

Carol wants to manage data from multiple simultaneous observations
without mixing data.
She attaches a second worktree for a new branch, which is created from
the current branch when it does not already exist::

    hallmark worktree add /data/obs obs2

The branch name is optional and defaults to the final component of the
destination path, so ``hallmark worktree add /data/obs`` would attach a
branch named ``obs`` instead.

She lists linked worktrees and continues on the new branch::

    hallmark worktree list
    cd /data/obs
    hallmark status
    hallmark add "{site}/{year:d}/{day:d}.fits"
    hallmark commit -m "Observation ingest on branch obs2"

Each worktree stays isolated by branch, so staged state and commits do
not interfere.


4. Python: Programmatic Repository Updates
------------------------------------------

David uses the Python API for scripted ingest against an on-disk
repository::

    from hallmark import Repo

    repo = Repo.open("obs")
    info = repo.info()
    print(info.local_path, info.worktree_path)

    for y in range(2000,2026,5):
        repo.add(f"{{site}}/{y}/{{day:d}}.fits")  # escape {{ and }}
    repo.commit("Nightly ingest")

This workflow is suitable for finer control of data ingest.


5. Python: In-Memory State Workflows
------------------------------------

Emma can use an memory-backed facade for data transformations that do
not require git operations::

    from hallmark import Repo

    repo = Repo()
    repo.add("data/{site}/{year:d}/{day:d}.fits")
    repo.worktree("data_transformed/{year:d}/{day:d}/{site}.fits")

This is especially useful for data (re-)organization.


..  |hallmark| replace:: ``hallmark``

..  _hallmark: https://github.com/l6a/hallmark
