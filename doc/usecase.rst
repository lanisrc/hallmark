Use Cases
=========

This section describes representative workflows supported by the
current |hallmark|_ CLI and Python API.


1. CLI: Standard Repository Ingest
----------------------------------

Alice is organizing telescope data in a normal directory.
She initializes that directory as a |hallmark|_ worktree::

    hm init obs
    cd obs

She adds the files using a python format-string pattern::

    hm add "{site}/{year:d}/{day:d}.fits"

She then commits the updated hallmark index::

    hm commit -m "Initial observation ingest"

She can inspect current repository paths at any time::

    hm info

She can also inspect current changes and commit history::

    hm status
    hm log

These git-like commands report or modify hallmark tracked state files
in ``.hm``.
Dataset files are represented through staged ``sha1`` column in
``data.tsv``, with other useful parameters (e.g., site, year, day),
associated with each file in different rows.


2. CLI: Bare Repository with Explicit Downloads
-----------------------------------------------

Bob prefers keeping the data index separate from downloaded files.
He initializes a bare |hallmark|_ catalog from a simulation export and
verifies its location::

    hm init sim.hm
    cd sim.hm
    hm add ssh://campus/srv/export/ \
        --fmt 'run{run:d}/frame{frame:d}.h5'
    hm commit -m "Record remote catalog"
    hm info

The ``.hm`` suffix selects a bare catalog without a data worktree.
Bob reviews the selection and approves downloading to an explicit directory::

    hm download --all --output ../outputs --dry-run
    hm download --all --output ../outputs

The catalog stays in ``sim.hm``; the downloaded files go to ``outputs``.
For local ingest and versioning, Bob can initialize ``outputs`` as a
standard repository and follow Alice's workflow.


3. Python: Branch-Isolated Analysis with Multiple Worktrees
-----------------------------------------------------------

Carol wants to manage data from multiple simultaneous observations
without mixing data.
Starting from a committed standard repository, she creates a new branch
and attaches a second worktree through the Python API::

    from hallmark import Repo

    repo = Repo("obs")
    repo.add_worktree("obs2")
    second = Repo(repo.worktree.parent / "obs2")

After adding observation files beneath ``obs2``, she stages and commits
them in that worktree::

    second.add("{site}/{year:d}/{day:d}.fits")
    second.commit("Observation ingest on branch obs2")

Each worktree stays isolated by branch, so staged state and commits do
not interfere.

For a catalog of files on a data server, ``obs2`` starts with read-only
links to the downloaded files whose SHA-1 was committed, from the shared
``.hm/objects``; ``hm download`` run in ``obs2`` fetches the rest.


4. Python: Programmatic Repository Updates
------------------------------------------

David uses the Python API for scripted ingest against an on-disk
repository::

    from hallmark import Repo

    repo = Repo("obs")
    print(repo.dothm.path, repo.worktree)

    repo.add("{site}/{year:d}/{day:d}.fits")
    repo.commit("Nightly ingest")

This workflow is suitable for finer control of data ingest.


5. Python: In-Memory State Workflows
------------------------------------

Emma can use ``ParaFrame`` to discover and select files without
creating a Git repository::

    from hallmark import ParaFrame

    data = ParaFrame.parse("{site}/{year:d}/{day:d}.fits", base_path="data")
    selected = data.filter(year=2025)
    print(selected)

This keeps the file index in memory.
Emma can pass the selected paths to her analysis tools;
filtering the index does not modify the data files.


6. CLI: Private Lab Data over SSH
---------------------------------

Frank works with simulation data stored on a private lab server.
He configures the SSH alias ``campus``, verifies the server's host key,
and loads his key into an SSH agent.
He then catalogs the files on the server without downloading them::

    hm init lab
    cd lab
    hm add ssh://campus/srv/export/ --fmt 'runs/run_{i:d}.h5'
    hm commit -m "Record remote catalog"

He reviews the selection and approves the transfer::

    hm download --all --dry-run
    hm download --all

To keep his SSH settings out of the repository, he moves them to a local
profile in ``~/.config/hallmark/auth.yml``::

    version: 1
    profiles:
      campus:
        hosts: [campus]
        user: researcher
        identity_file: ~/.ssh/id_ed25519

He then associates the profile with the data remote::

    hm set-config --remote-name origin --remote-auth campus

The repository stores only the profile name.
The data remote ``origin`` specifies where the dataset files are stored;
it is configured separately from the Git remote used to share the
history in ``.hm``, even when both remotes have the same name.


..  |hallmark| replace:: ``hallmark``

..  _hallmark: https://github.com/lanisrc/hallmark
