---
title: 'Hallmark: Git-style version control for scientific data catalogues'
tags:
  - Python
  - astronomy
  - reproducibility
  - data management
  - version control
  - Event Horizon Telescope
# TODO: add each author's orcid, confirm order and affiliations, and mark
# one author with `corresponding: true`. Thea McKenna also has commits;
# decide whether to add her here or thank her in the Acknowledgements.
authors:
  - name: Chi-kwan Chan
    affiliation: 1
  - name: Nayera Abdessalam
    affiliation: 1
  - name: Hayden Marchinek
    affiliation: 1
  - name: Rohin Sant
    affiliation: 1
  - name: Ram Adithya
    affiliation: 1
  - name: Hayden Foote
    affiliation: 1
  - name: Samuel Jacobs
    affiliation: 1
  - name: Yaqing Su
    affiliation: 1
  - name: Tianrui Wu
    affiliation: 1
  - name: Arshia Akhtarkavan
    affiliation: 1
affiliations:
  - name: Steward Observatory and Department of Astronomy, University of Arizona, United States
    index: 1
    ror: 03m2x1q45
date: 16 October 2026
bibliography: paper.bib
---

<!--
Outline, not prose. Each section lists what it must cover and the facts
the authors can draw on. Target 1,450 words (JOSS allows 750-1750).
Keep the six required headings word for word: reviewers check each by title.
No API documentation; that belongs in the Sphinx docs.
Describe the release under review: the backends and object store below
are on codex/function-atomic-replay-20260928 and PR #62, not in v0.2.0.
-->

# Summary

<!-- 150-200 words, for a non-specialist reader. No jargon. -->

- Scientific results rest on large collections of data files whose meaning
  lives in their directory and file names: telescope site, observing year,
  frequency band, processing stage, simulation parameter.
- Hallmark is a Python package and command-line tool (`hm`) that keeps a
  versioned, reproducible index, a *catalogue*, of such files, whether they
  sit on the researcher's disk or on a remote archive.
- A user describes a naming scheme once, e.g. `{site}/{year:d}/{day:d}.fits`.
  Hallmark turns each path's fields into the columns of a table that can be
  filtered like a spreadsheet [@McKinney_2010; @pandas].
- Every change to the catalogue is a commit, with the add, commit, branch
  and checkout workflow familiar from Git, so a result can name the exact
  set of files it used.
- For remote archives, Hallmark lists the server, shows a download plan, and
  fetches only the files the user approves, checking each against a
  checksum.
- Built for the Event Horizon Telescope (EHT) [@EHT_M87_I]; useful wherever
  path-structured data lives on shared servers.

# Statement of need

<!-- 250-300 words: the problem, who it is for, and how it relates to other
work (detail in State of the field). -->

- **Problem.** EHT calibration pipelines and theory libraries produce data
  products at successive processing stages and across large parameter grids.
  Collaboration members fetch them from shared servers such as CyVerse
  [@Merchant_2016], where the first M87 calibrated data are published
  [@EHT_M87_data]. TODO: one concrete example (number of files, stages,
  parameters) from an EHT workflow.
- Researchers repeatedly need to:
  (a) record which data products an analysis used, and which version of each;
  (b) select subsets by physical parameter rather than by hand-written globs;
  (c) fetch the same files again later, or on another machine, and know they
  are byte-identical.
- Ad hoc practice (directory listings, spreadsheets, `wget` scripts) loses
  this provenance and works against making data findable and reusable
  [@Wilkinson_2016].
- **Audience.** Astronomers and pipeline developers with path-structured
  file collections on HTTP(S), WebDAV, SSH/HPC or S3 storage; the need is
  not specific to astronomy. The documentation catalogues public DESI DR1
  spectra [@DESI_DR1] as a second, non-EHT example.
- **Position.** Hallmark sits between general data-versioning tools, which
  version bytes, and ad hoc scripts, which version nothing (next section).

# State of the field

<!-- 250-300 words. Must compare commonly used packages AND justify building
rather than contributing ("build vs. contribute"). Check every claim and
table cell against each tool's current documentation before submission. -->

- **Git LFS** [@git_lfs]: replaces large files in Git with pointers and keeps
  their contents on an LFS server. Versions files the user owns; has no
  notion of third-party archives or of parameters in paths.
- **git-annex** [@git_annex] and **DataLad** [@Halchenko_2021], built on it:
  mature tools that version file contents by hash, record download URLs
  (`addurl`), and track which remote holds which file. DataLad adds nested
  datasets and provenance capture for commands. TODO: state the specific
  gaps for our users, e.g. the extra git-annex binary, worktree files as
  symlinks into the annex, and no parameter table derived from paths.
- **DVC** [@DVC]: versions data and models for machine-learning pipelines.
  `import-url` records a source URL in a committed `.dvc` file; contents go
  to a hash-addressed cache and remotes. Tracks files and directories, not
  parameter tables.
- **Pooch** [@Uieda_2020]: downloads and caches files listed in a registry
  with known hashes. A download manager; no versioned catalogue or branches.
- **Intake** [@intake]: data catalogues that describe how to load datasets.
  It can parse fields from path patterns into columns when loading; it does
  not version the set of files or verify downloads against checksums.
- Optional table, adapted from `doc/data_backend.md` section 12. Columns:
  what is versioned / records the origin of external files / parameter
  columns from paths / planned, verified fetch.
- **Build vs. contribute.** Hallmark's contribution is the combination:
  (1) a parameter table derived from path formats,
  (2) a Git-versioned catalogue of it, and
  (3) planned, verified fetching from archives that Hallmark only reads.
  Git LFS and DVC assume they own the storage. git-annex/DataLad could host
  such catalogues only as a layer above their per-file model. Third-party
  archives are addressed by path, and a file's hash is often unknown until
  its first download, so each row must record its origin. TODO: say whether
  an extension of an existing tool (e.g. a DataLad extension) was
  considered, and why it was not pursued.

# Software design

<!-- 350-400 words. Explain the reasoning and the trade-offs, not the module
layout. Four subsections, one per function; in each, every bullet is one
decision with its why and its cost. Interfaces may become one closing
sentence if words run short. -->

## Describing data

- **Naming conventions as the schema.** Python format strings, parsed with
  `parse`, turn path fields into catalogue columns. `ParaFrame`, a pandas
  `DataFrame` subclass, filters rows by parameter. *Why:* scientists already
  encode parameters in paths, so no sidecar metadata is needed. *Cost:*
  consistent naming is required. Files that match no format become plain
  `file:` entries, and `hm detect` proposes formats from a server listing.

## Versioning

- **Version the catalogue, not the bytes.** `.hm/` is a Git repository,
  driven through GitPython [@GitPython], that tracks only `config.yml`,
  `meta.yml` and one TSV per format. File contents go to a content-addressed
  store (`.hm/objects`, keyed by SHA-1). *Why:* catalogues of large archives
  stay small, diffable and reviewable, and identical content is stored once
  across branches and sources. *Cost:* the store cannot yet be pushed or
  shared, storage is whole-file, and there is no `gc`.
- **Git's vocabulary.** `add`, `commit`, `status`, `restore --staged`,
  `log`, `branch`, `checkout` and linked worktrees; checkouts complete fully
  or not at all. Local files are copied into the store on commit and
  restored as writable copies on checkout. *Why:* most users already know
  Git. *Cost:* Hallmark implements these semantics itself rather than
  storing data in Git directly.

## Fetching remote data

- **Catalogues of archives Hallmark only reads.** `hm add URL` records each
  row's source and server. *Why:* third-party archives are addressed by
  path, and a file's hash is often unknown until its first download (see
  Build vs. contribute). *Cost:* a server move needs a catalogue update
  (local URL overrides are planned).
- **Downloads that cannot half-finish.** A plan is shown first and nothing
  moves without approval. Each file is fetched to a temporary path, checked
  for size, the catalogued digest and SHA-1, published read-only,
  hard-linked into the worktree, and only then staged, under a lock.
  Checksums come from SUMS files, WebDAV properties, S3 or SSH hashing, and
  their source is recorded. *Why:* a crash never leaves a partial file, and
  every staged file has verified bytes. *Cost:* TODO, e.g. hashing every
  download; read-only links must be copied before editing.
- **Pluggable transports, no stored secrets.** HTTP(S) index crawling,
  WebDAV, SSH/SFTP through the system OpenSSH (8.0 or later), S3 through
  optional `boto3`, and CyVerse; others plug in through `register_backend`.
  Credentials come from environment variables, the OS keyring or a
  password-manager command. *Why:* existing SSH and HPC configuration works
  unchanged, and no secret is written into a repository. *Cost:* SSH needs
  key-based login (no interactive MFA yet).

## Interfaces

- **One set of operations, two front ends.** The CLI and the Python API
  expose the same operations; Python 3.9 or later; eight core dependencies.
  *Why:* TODO, e.g. scripts and notebooks share one tested code path, and
  shared HPC systems often ship older Pythons.

<!-- One figure: architecture / data flow. Uncomment once
paper/architecture.png exists (Repo -> State, .hm Git, Worktree, Objects;
remote backends -> plan -> verified objects -> read-only links).

![Hallmark's catalogue and object store. TODO caption.\label{fig:architecture}](architecture.png){ width=90% }
-->

# Research impact statement

<!-- 150-250 words. Evidence only: "compelling and specific, not
aspirational". Drop anything that cannot be backed with a link or number. -->

- TODO, the evidence that decides this section: name the EHT analyses,
  pipelines or papers that used Hallmark or `ParaFrame`, and any external
  users.
- TODO: confirm and describe how Project Laniakea's sister packages `yukon`
  and `banyan` depend on Hallmark.
- Releases on PyPI: v0.1.0 (November 2025), v0.1.1 (March 2026), v0.2.0
  (May 2026); about 1,190 downloads excluding mirrors from 10 April to
  21 October 2026 (pypistats.org; includes CI traffic). Update both before
  submission.
- Eleven people have committed code, many of them undergraduate and summer
  research students. TODO: say whether the project serves as a training
  vehicle.
- Community readiness:
  - CI on Python 3.9 to 3.14, on Linux and macOS;
  - SSH tests against a disposable OpenSSH server, including OpenSSH 8.0 on
    Rocky Linux 8 and 9;
  - S3 tests against a mocked service;
  - about 1,000 tests, with a combined branch-coverage floor of 89%;
  - documentation on GitHub Pages and demo notebooks that run in the tests.
- Leave out planned adapters (e.g. Rubin, Roman, JWST) unless they ship.

# AI usage disclosure

<!-- 60-120 words. Name tools and versions, where they were used, the kind
of help, and how humans verified it. Vague wording is "not acceptable". -->

- Tools: Claude Code with Claude Opus 5 and Opus 5.5 (Anthropic); 73
  commits carry a `Co-Authored-By: Claude` trailer. TODO: confirm with each
  contributor which other tools were used; the branch
  `codex/function-atomic-replay-20260928` suggests OpenAI Codex.
- Where: implementation, tests, documentation and demo notebooks, and the
  outline of this paper. TODO: say what share and which parts.
- Verification: every change went through pull-request review, the test
  suite and CI with a coverage floor. The core design (catalogue model,
  download and object-store model, CLI specification) was decided by the
  human authors.

# Acknowledgements

<!-- 40-80 words. Funding is required if any exists. -->

- TODO: funding sources and grant numbers, and whether sponsors had any role.
- The EHT Collaboration; CyVerse for hosting EHT data; Steward Observatory.
- TODO: contributors not on the author list (e.g. Thea McKenna).

# References
