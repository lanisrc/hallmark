# Private data demo: copyable instructions

Run these instructions on your local machine using an existing data export.
The SSH configuration and dataset path are illustrative. Replace them with your
own export settings; this guide does not provision or contact a particular VM.

The export is assumed to contain `runs/run_001.dat`, `runs/run_002.dat`, a
`README.md`, and optionally a publisher-provided `SHA256SUMS`. The client needs
Hallmark, Git, and OpenSSH `ssh`/`sftp` 9.6+. The server needs SFTP, but does not
require a login shell, Python, Hallmark, or Git. Run the Bash blocks in order in
one terminal, starting in the Hallmark source checkout.

1. Install the checkout in your active Python environment. `set -e` stops the
shell if a command fails or a download is declined, so later commands do not
continue after an unsuccessful transfer.

```bash
set -e
python -m pip install -e .
```

2. Add this illustrative entry to `~/.ssh/config`, preserving your other entries
and replacing the host, user, and identity with your export's settings.

```text
Host lab-data
    HostName vm.example.org
    User researcher
    IdentityFile ~/.ssh/hallmark_vm
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

The verified host key must already be in `known_hosts`. Load an encrypted key
into your SSH agent first. SSH configuration alone is sufficient for this demo.
For optional named profiles, see the [private data guide](../doc/private_data.rst).
Each collaborator defines credentials locally; a catalog stores only the
profile name.

3. Set the exact export root and create a new local workspace. Choose a different
workspace name when repeating the demo; the data-server path below is an example.

```bash
export HM_DEMO_URL='ssh://lab-data/srv/exports/lab/'
export HM_DEMO_WORKSPACE="$HOME/hallmark-private-client"
mkdir "$HM_DEMO_WORKSPACE"
cd "$HM_DEMO_WORKSPACE"
```

4. Discover the two indexed runs and prepare a local `.hm`. The format selects
run files and extracts their run numbers; it does not authorize a download.

```bash
hallmark init ./client --from "$HM_DEMO_URL" --fmt 'runs/run_{run:03d}.dat'
cd client
```

The destination can contain existing files when `.hm` is absent; those files
are preserved. An existing `.hm` is rejected before contacting the data server.
Only listings and published metadata are read. Available sizes, modification
times, and published checksums are retained; missing checksums stay unknown.
Dataset files are not downloaded to calculate checksums.

Omit `--fmt` to catalog every file below the dataset root. A filter such as
`--filter 'runs/*.dat'` selects relative paths without defining parameters.
`init --backend NAME --backend-options FILE` selects an installed data backend
and reads nonsecret options from a YAML mapping. See
[data backends](../doc/backends.rst) for registration and multiple-server routing.

5. Review an offline plan for one run, then approve its transfer. The second
command displays the plan again and asks for confirmation; answer `y` only when
the source, destination, and selection are correct. The final check stops the
workflow if no selected input was downloaded.

```bash
hallmark download runs/run_001.dat --dry-run
hallmark download runs/run_001.dat --max-workers 2
test -f runs/run_001.dat
```

A dry run reports file count, known bytes, unknown-size count, source, and
destination without contacting the server. It does not check credentials or
remote-file existence. Every nonempty CLI transfer prompts, including one file.
Refusal transfers nothing and leaves the catalog available. Empty selections
need no confirmation. `--all` selects every cataloged file; `--tsv data.tsv`
selects that table. Neither flag supplies approval.

Successful files are published only after transfer and any checksum verification.
A failed transfer preserves an existing destination and removes its temporary
file. Cancellation closes connections started for the operation. Repeating a
download transfers the selection again.

6. Use the equivalent SFTP URL for this data remote. Changing the configuration
does not transfer files; the dry run shows the new source and another destination.

```bash
hallmark set-config --remote-name origin \
    --remote-url "sftp://${HM_DEMO_URL#ssh://}"
hallmark download runs/run_001.dat --output ../via-sftp --dry-run
```

Both `ssh://` and `sftp://` use SFTP for discovery and data transfer. Relative
output paths use the current directory. A bare `.hm` catalog requires an
explicit `--output`. To set a named authentication profile, use
`hallmark set-config --remote-name origin --remote-auth PROFILE`; an empty
profile name returns to SSH configuration alone.

7. Use the same review step in Python. The
[scientific workflows notebook](scientific_workflows_python.ipynb) demonstrates
`Repo.plan_download()` followed by explicit approval and
`repo.download(plan, approved=True)`. It stops on refusal or returned transfer
failures. A plan retains its source, profile reference, backend settings,
destination, paths, and checksums even if repository configuration later changes.

8. To review a download as part of initialization, add `--with-download` to an
`init --from` command. The catalog is created before the approval prompt;
declining keeps it available. Python accepts `download=True` and an
`approve(plan)` callback. The [private data guide](../doc/private_data.rst)
contains both forms.

9. Use the same discovery workflow for public datasets. The
[scientific CLI workflows](scientific_workflows_cli.md) select one EHT file from
its [UVFITS directory](https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/EHTC_FirstM87Results_Apr2019/uvfits/)
and one DESI redrock product from
[HEALPixel 23040](https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/23040/).
These narrow dataset roots avoid listing an entire archive. HTTPS requires a
usable directory index or a backend that can enumerate the dataset; arbitrary
URLs cannot reveal hidden files.

10. Reuse the existing catalog without downloading dataset files. This local
clone retains the committed catalog and its Git history, including the original
SSH data URL. The uncommitted configuration change in step 6 remains local.

```bash
cd "$HM_DEMO_WORKSPACE"
hallmark clone ./client/.hm ./catalog-copy
```

`clone` copies existing catalogs; raw dataset URLs belong to `init --from`.
A Git catalog can live on GitHub while its data remains on the SSH server. A
published HTTP/SFTP catalog snapshot can likewise use a different data server;
snapshot imports start local history. Cloning metadata does not require the
configured data profile or dataset credentials. Git catalog authentication and
data authentication are independent.

Clone preserves the complete catalog. Its `--filter` and `--fmt` options select
optional downloads and require `--with-download` (`--download` is an alias).
They do not remove catalog rows. Plain `init` creates a local repository; the
legacy remote `build` command is deprecated in favor of `init --from`.
Downloading files alone does not populate the local object store; the scientific
workflows show how to add and commit selected local inputs before branching.

This demo passed on Python 3.9 and 3.13 using a disposable local SFTP export
with two 30-byte run files and published checksums. Discovery, one approved
30-byte transfer, refusal before transfer, configuration changes, and catalog
cloning passed on both versions. These checks do not establish availability or
credentials for any external server. Keep the workspace for inspection and
remove it manually when finished.
