# Private data demo: copyable instructions

Run these instructions on your local machine using an existing data export.
The SSH alias uses the validation VM settings supplied for this demo. Replace
the example dataset root with your export path; the commands below have not
been run against that path on the VM.

For a small walkthrough, the examples assume `runs/run_001.dat`,
`runs/run_002.dat`, a `README.md`, and optionally a publisher-provided `SHA256SUMS`.
The client needs this Hallmark checkout, Git, and OpenSSH `ssh`/`sftp` 9.6+.
The server needs SFTP, but does not require a login shell, Python, Hallmark, or Git.
Run the Bash blocks in order in one local terminal.

1. Install the checkout in your active Python environment.

```bash
# Start in the Hallmark source checkout.
python -m pip install -e .
ssh -V
hallmark clone --help
```

2. Add or update this entry in `~/.ssh/config`, preserving your other entries.
Replace the host, user, or identity when using a different server.

```text
Host lab-data
    HostName 34.29.34.222
    User denniswu501
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

The verified host key must already be in `known_hosts`. Load an encrypted key
into your SSH agent first. Check SFTP access without an interactive login:

```bash
sftp -o BatchMode=yes -o StrictHostKeyChecking=yes -b /dev/null lab-data
```

3. Set the exact export root and create an isolated local workspace. The example
path is illustrative and may not exist on the validation VM.

```bash
export HM_DEMO_URL='ssh://lab-data/srv/exports/lab/'
HM_DEMO_WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/hallmark-private-client.XXXXXX")"
export HM_DEMO_WORKSPACE
cd "$HM_DEMO_WORKSPACE"
```

SSH configuration alone is enough. To demonstrate an optional local profile,
create this file outside the catalog and clone:

```bash
export HALLMARK_AUTH_FILE="$HM_DEMO_WORKSPACE/auth.yml"
cat > "$HALLMARK_AUTH_FILE" <<'YAML'
version: 1
profiles:
  demo:
    hosts: [lab-data]
    host_key_policy: strict
    connect_timeout: 10
    transfer_timeout: 60
    max_sessions: 2
YAML
chmod 600 "$HALLMARK_AUTH_FILE"
```

The profile uses the identity from SSH configuration. `hosts` matches the URL
alias `lab-data`, before SSH resolves its `HostName`. Each collaborator defines
their own local profile; catalog configuration stores only its name.

4. Discover the two indexed runs and prepare a local `.hm`.

```bash
hallmark clone "$HM_DEMO_URL" ./client \
    --auth demo --fmt 'runs/run_{run:03d}.dat'
cat ./client/.hm/config.yml
cat ./client/.hm/data.tsv
```

Expected for the illustrative export: two run rows, with sizes and modification
times where available. Published manifest checksums are attached when present;
missing checksums stay unknown. The data remote is `origin` with `auth: demo`.
Only listings and catalog metadata are fetched. Dataset files are not read to
compute checksums, and no remote commands or server Python are required.

Omit `--fmt` to recursively catalog every file below the URL, including supporting
files. Alternatively, `--filter '**/*.dat'` selects paths without defining
parameters. Filters and formats never authorize downloads. Discovery reports
completed directories and discovered files while its total remains unknown.

5. Inspect the transfer plan before approving a download.

```bash
cd "$HM_DEMO_WORKSPACE/client"
hallmark info
hallmark download --tsv data.tsv --dry-run
hallmark download runs/run_001.dat --dry-run
```

A dry run contacts no server. It reports count, known bytes, unknown-size count,
source, and destination. The CLI reports duration as unknown; Python callers
can estimate it from a supplied transfer rate when all file sizes are known.
It does not validate credentials or remote-file existence. To download the
selected run, inspect the displayed plan and answer `y` at the prompt:

```bash
hallmark download runs/run_001.dat --max-workers 2
cat runs/run_001.dat
```

Every nonempty CLI transfer prompts, including one file. Refusal or end-of-input
transfers nothing. `--yes` is deprecated and no longer bypasses approval.
Explicit file paths retain their catalog checksums. `--tsv data.tsv` selects
all rows in that table; `--all` selects all cataloged files. Overlapping paths
are deduplicated, and `--all` cannot be combined with explicit paths or TSVs.

6. Use the SFTP URL alias and change a profile reference.

```bash
hallmark set-config --remote-name origin \
    --remote-url "sftp://${HM_DEMO_URL#ssh://}" --remote-auth demo
hallmark download --filter 'runs/run_00[12].dat' --output ../via-sftp --dry-run

# Return to SSH configuration alone, without a named profile.
hallmark set-config --remote-name origin --remote-auth ''
hallmark download runs/run_001.dat --output ../via-ssh-config --dry-run

# Restore the profile for the Python examples.
hallmark set-config --remote-name origin --remote-auth demo
```

Both schemes use SFTP for discovery and transfer. SFTP-only accounts can complete
the whole workflow. Omit `--dry-run` when ready to inspect and approve a transfer.
Relative output paths use the current directory. A bare `.hm` catalog requires
an explicit `--output`.

7. Inspect and approve a Python download. Stay in the `client` directory.
The script is saved locally so its approval prompt can read terminal input.

```bash
cat > ../download_subset.py <<'PYTHON'
from hallmark import Repo
from hallmark.downloader import DownloadError

repo = Repo(".")
plan = repo.plan_download(
    output_path="../python-subset", file_paths=["runs/run_001.dat"],
    remote_name="origin",
)
print(plan.summary())
for item in plan.items:
    print(item.relative_path, item.checksum, item.size_bytes)
if input("Download these files? [y/N] ").strip().lower() != "y":
    raise SystemExit("Download declined")
try:
    result = repo.download(plan, approved=True, max_workers=2, progress=True)
except DownloadError as exc:
    raise SystemExit(f"Download could not start: {exc}") from exc
if result["failed"]:
    raise SystemExit("\n".join(result["errors"]))
print(result)
PYTHON
python ../download_subset.py
```

The plan fixes its source, profile reference, destination, paths, and checksums.
Later catalog or configuration changes do not redirect it. Without
`approved=True`, nonempty Python transfers fail before connecting. Check the
returned `failed` count; successful files remain when another transfer fails.

Files are written to temporary paths and published atomically after transfer and
any checksum verification. Failed files preserve an existing destination and
remove their temporary file. Repeating a download transfers the selection again.
Cancellation closes the connections and processes started for the download.
Byte progress shows an ETA when a total is known; unknown totals stay indeterminate.

8. Request an approved download during cloning, using either interface.
The CLI prepares the catalog first and then displays its normal approval prompt:

```bash
hallmark clone "$HM_DEMO_URL" ../clone-and-download \
    --auth demo --filter 'runs/run_001.dat' --download
```

Python receives the completed plan through a callback:

```bash
cat > ../clone_with_approval.py <<'PYTHON'
import os
from hallmark import Repo


def approve(plan):
    print(plan.summary())
    return input("Download these files? [y/N] ").strip().lower() == "y"


repo = Repo.clone(
    os.environ["HM_DEMO_URL"], "../python-clone", auth="demo",
    filter="runs/run_001.dat", download=True, approve=approve, progress=True,
)
print(repo.dothm.path)
PYTHON
python ../clone_with_approval.py
```

9. Explore CyVerse and DESI with the same metadata-only workflow. These examples
use public endpoints, so no named auth profile is supplied. A broad URL can
require many directory-listing requests; use a specific subtree when appropriate.

```bash
cd "$HM_DEMO_WORKSPACE"
hallmark clone \
    'https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/EHTC_FirstM87Results_Apr2019/' \
    ./cyverse --filter '**/*.uvfits'
hallmark clone 'https://data.desi.lbl.gov/public/' ./desi --filter '**/*.fits'

cd "$HM_DEMO_WORKSPACE/cyverse"
hallmark download --all --dry-run
cd "$HM_DEMO_WORKSPACE/desi"
hallmark download --all --dry-run
```

Choose actual catalog paths before downloading, for example
`hallmark download PATH-FROM-THE-CATALOG`. This displays a plan and prompts for
approval. A format such as `--fmt 'runs/run_{run:03d}.dat'` is a parameterized
path template; a filter such as `--filter '**/*.fits'` is a relative path glob.
Neither supplies approval. No `--index-format cyverse-html` option is needed.

The equivalent Python preparation, from the workspace root, is:

```bash
cd "$HM_DEMO_WORKSPACE"
python - <<'PYTHON'
from hallmark import Repo

cyverse = Repo.clone(
    "https://data.cyverse.org/dav-anon/iplant/commons/"
    "cyverse_curated/EHTC_FirstM87Results_Apr2019/",
    "cyverse-python", filter="**/*.uvfits", progress=True,
)
desi = Repo.clone(
    "https://data.desi.lbl.gov/public/", "desi-python",
    filter="**/*.fits", progress=True,
)
for repo in (cyverse, desi):
    plan = repo.plan_download()
    print(plan.summary())
PYTHON
```

For a subsequent selected download, use the inspect/approve sequence in step 7
with `Repo("cyverse-python")` or `Repo("desi-python")` and an actual catalog path.
HTTPS download authentication retains Requests' `.netrc` and environment behavior.
A server with no usable index must expose a published Hallmark catalog or a
browsable directory; arbitrary HTTPS URLs cannot reveal hidden files.

10. Reuse a catalog and finish the session.

```bash
cd "$HM_DEMO_WORKSPACE"
hallmark clone ./client/.hm ./catalog-copy
hallmark init ./local-experiment
unset HALLMARK_AUTH_FILE
```

A local or Git-hosted Hallmark repository retains its history. A published
HTTP/SFTP catalog directory is imported as a snapshot with new local history.
Use `--source-type git` for a Git endpoint without an obvious `.git` suffix, or
`--source-type catalog` to require a published snapshot. Git credentials are
separate from the data remote's `--auth` profile. Filtering an existing Git
catalog preserves fetched history and adds a local filtered-catalog commit.

`clone` downloads no dataset files by default, so `--no-fetch-data` is now only
a compatibility alias. `init` creates a local repository; the older remote `build`
command is deprecated. Discovery uses no `--allow-remote-commands` or
`--remote-hash` flag, and does not read dataset files to compute checksums.
For local `add`/`commit` experiments, use a separate compatible one-format
repository; downloading a remote catalog does not populate the local object store.

See [the private data guide](../doc/private_data.rst) for profile settings,
error handling and operational details. The auth-file override above lasts only
until `unset HALLMARK_AUTH_FILE`; existing catalog profile references still require
a matching local profile for later downloads.
