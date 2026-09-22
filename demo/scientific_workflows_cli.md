# Four scientific workflows from the terminal

Use selected EHT/CyVerse and DESI products, Roman–Rubin simulations on an
existing SFTP export, and a local DES Y3 product. The matching
[Python notebook](scientific_workflows_python.ipynb) includes a worktree example.
This guide uses branches; the current CLI has no worktree command.

Run the setup, then one numbered workflow in order in the same Bash terminal.
Each workflow uses a new directory. The examples version a small
`analysis-settings.txt` file alongside one scientific input. Editing that file
records an analysis choice; it does not process or modify the scientific data.

## Setup and disk budget

Install the local Hallmark checkout in your active environment and create a new
workspace outside the source checkout. `set -e` stops the shell if a command
fails or a download is declined, so later versioning commands cannot continue
with a missing input. To repeat a workflow, choose a new workspace.

```bash
set -e
python -m pip install -e .
export HM_ROOT="$HOME/hallmark-science-cli"
mkdir "$HM_ROOT"
```

Use your normal Git author configuration. No optional scientific-processing
packages are needed for these examples. Each remote workflow selects one file;
review its size before approving the transfer. Allow space for the downloaded
input and a stored baseline copy. Settings changes store only the changed
settings file; they do not duplicate an unchanged scientific input.

`init --from` discovers a raw dataset and creates a catalog using directory
listings and published metadata. It does not download dataset files. Its
filters select catalog entries. `clone` copies an existing catalog; clone
filters select optional downloads and require `--with-download`. Every nonempty
CLI transfer displays its plan and prompts for approval. `--dry-run` prepares an
offline plan without transferring files. Unknown sizes remain unknown.

Each example uses a working directory containing `.hm`. After download,
`hallmark add '{name}'` indexes the selected local input and settings file;
`commit` stores their contents so branch checkout can restore them. The catalog
URL and data URL can be hosted separately. See the
[private data guide](../doc/private_data.rst) for authentication and catalog
hosting, and [data backends](../doc/backends.rst) for backend extensions.

## 1. CyVerse: EHT M87 visibility provenance

The [EHT release](https://github.com/eventhorizontelescope/2019-D01-01) contains
2017 M87 observations. Select the April 5 low-band UVFITS product from the
release's UVFITS directory. An analysis might inspect positive-weight visibility
amplitudes before imaging; here we only record that intended quality check.

### Catalog and approve a selected download

Initialize the selected catalog, enter its working directory, and review the
offline download plan. Run the transfer only after checking the selection and
answering `y` at its prompt. Declining leaves the catalog available.
The final check stops the workflow if discovery selected no input file.

```bash
hallmark init "$HM_ROOT/eht" --from \
    'https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/EHTC_FirstM87Results_Apr2019/uvfits/' \
    --filter 'SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits'
cd "$HM_ROOT/eht"
hallmark download --all --dry-run
hallmark download --all
test -f 'SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits'
```

### Commit, annotate a branch, and recover

Store the original input with an analysis setting, then record a different
quality-review choice on `eht-provenance`. Returning to `main` restores the
original settings; the final command prints `qa=positive-weights`.

```bash
printf '%s\n' 'qa=positive-weights' > analysis-settings.txt
hallmark add '{name}'
hallmark commit -m 'Preserve EHT input and analysis settings'
hallmark checkout eht-provenance
printf '%s\n' 'qa=positive-weights-and-amplitude-review' > analysis-settings.txt
hallmark add .
hallmark commit -m 'Record EHT amplitude review setting'
hallmark checkout main
cat analysis-settings.txt
```

This changes neither calibration nor visibilities. A scientific amplitude check
would separately interpret the UVFITS weights and polarization layout.

## 2. HTTPS: DESI spectrum inspection and quality selections

Use the redrock catalog from
[DR1 Iron HEALPixel 23040](https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/23040/).
This route downloads redshift results, not the coadded spectra. A scientific
selection could combine warning flags, spectral type, and fit-quality
statistics; applying those cuts remains a separate analysis step.

### Catalog and download by product

Discover only `redrock-main-dark-23040.fits` in this HEALPixel, inspect the plan,
and approve its download. The selection excludes coadds and other products;
the final check stops the workflow if discovery selected no input file.

```bash
hallmark init "$HM_ROOT/desi" --from \
    'https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/healpix/main/dark/230/23040/' \
    --filter 'redrock-main-dark-23040.fits'
cd "$HM_ROOT/desi"
hallmark download --all --dry-run
hallmark download --all
test -f 'redrock-main-dark-23040.fits'
```

### Compare two selection branches

Commit the input with an illustrative fit-quality threshold. Record a stricter
threshold on `desi-quality`, then return to `main`; the restored setting is
`min_deltachi2=25`. No target rows are filtered by these commands.

```bash
printf '%s\n' 'min_deltachi2=25' > analysis-settings.txt
hallmark add '{name}'
hallmark commit -m 'Preserve DESI input and analysis settings'
hallmark checkout desi-quality
printf '%s\n' 'min_deltachi2=40' > analysis-settings.txt
hallmark add .
hallmark commit -m 'Record stricter DESI quality setting'
hallmark checkout main
cat analysis-settings.txt
```

## 3. SSH/SFTP: Roman–Rubin products over SFTP

Assume an existing export contains the OpenUniverse2024 stellar catalog
`stars/pointsource_10307.parquet`. Select that one product; this example does not
provision a server. A science workflow could compare magnitude selections
before matching simulated sources between surveys. Here the catalog is kept
unchanged and only the intended magnitude limit is versioned.

### Client authentication, catalog, and download

Add this illustrative alias to `~/.ssh/config`, replacing the host, user, and
identity with your existing export's settings. The verified host key must
already be in `known_hosts`; load an encrypted identity into your SSH agent.
The client needs OpenSSH 9.6+ and the server needs SFTP.

```text
Host lab-data
    HostName vm.example.org
    User researcher
    IdentityFile ~/.ssh/hallmark_vm
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Use the `stars/` directory as the dataset root so the selected product has a
simple local filename. Discover it, review the plan, and approve the transfer.
SSH configuration supplies authentication; no Hallmark profile is required.
The final check stops the workflow if discovery selected no input file.

```bash
hallmark init "$HM_ROOT/roman-rubin" --from \
    'sftp://lab-data/home/researcher/hallmark-exports/openuniverse2024/stars/' \
    --filter 'pointsource_10307.parquet'
cd "$HM_ROOT/roman-rubin"
hallmark download --all --dry-run
hallmark download --all
test -f 'pointsource_10307.parquet'
```

### Save and compare an analysis choice

Store the selected input and an illustrative magnitude limit. Change that limit
on `roman-rubin-selection`, then return to `main`. The restored value is
`magnitude_limit=24`; the Parquet catalog has not been filtered or converted.

```bash
printf '%s\n' 'magnitude_limit=24' > analysis-settings.txt
hallmark add '{name}'
hallmark commit -m 'Preserve Roman-Rubin input and analysis settings'
hallmark checkout roman-rubin-selection
printf '%s\n' 'magnitude_limit=23' > analysis-settings.txt
hallmark add .
hallmark commit -m 'Record Roman-Rubin magnitude setting'
hallmark checkout main
cat analysis-settings.txt
```

## +1. Local init: DES Y3 covariance-aware versions

Use an existing local copy of
`2pt_NG_final_2ptunblind_02_24_21_wnz_redmagic_covupdate.fits`, from the
[DES Y3 release](https://www.darkenergysurvey.org/des-year-3-cosmology-results-papers/).
This example copies that input into a new workspace and makes no network
request. A scientific angular cut must apply the same row selection to the
correlation-function tables and covariance matrix. Changing a settings file
alone does not perform that operation.

### Prepare the local analysis product

Replace the illustrative local path with your existing DES file. Initialize a
working directory and copy the input into it, leaving the source file intact.
There is no remote download to approve.

```bash
export HM_DES_LOCAL='/path/to/2pt_NG_final_2ptunblind_02_24_21_wnz_redmagic_covupdate.fits'
hallmark init "$HM_ROOT/des"
cd "$HM_ROOT/des"
cp "$HM_DES_LOCAL" .
```

### Compare angular selections with aligned covariance

Commit the input and an illustrative minimum angle. Record a different angle
on `des-angular-cut`, then recover the original `theta_min_arcmin=2.5` setting
from `main`. Both branches retain the original FITS data and covariance.

```bash
printf '%s\n' 'theta_min_arcmin=2.5' > analysis-settings.txt
hallmark add '{name}'
hallmark commit -m 'Preserve DES input and analysis settings'
hallmark checkout des-angular-cut
printf '%s\n' 'theta_min_arcmin=5' > analysis-settings.txt
hallmark add .
hallmark commit -m 'Record DES angular-cut setting'
hallmark checkout main
cat analysis-settings.txt
```

## Disk inspection, cleanup, and validation

Use `du -sh "$HM_ROOT"` to inspect the workspace. Keep it while comparing
branches; remove it manually when finished. No example changes the original
remote data or the local DES source file.

All four CLI workflows passed with tiny local HTTP/SFTP fixtures on Python 3.9
and 3.13. Each full run downloaded 66 bytes across the three remote inputs and
copied a 21-byte local input. The checks covered discovery, published checksums,
approval, settings commits, and branch restoration. Declined downloads and
empty selections stopped before versioning. Notebook copies also passed from
both the repository root and `demo/` on both Python versions.
Separate Python 3.13 runs confirmed that CLI and notebook transfer failures
stop before versioning and leave the catalog available.

The 95 CLI regression tests passed on each Python version. Bash syntax, notebook
structure and Python 3.9 syntax, local links, Ruff, and strict Sphinx checks
passed. These fixtures do not validate scientific file formats, analysis
choices, public-server availability, or public download sizes. No research
servers were contacted and no public datasets were downloaded.
