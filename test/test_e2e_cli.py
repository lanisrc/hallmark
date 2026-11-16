"""One research group's catalogue cycle through the real ``hm`` command.

The group keeps its own reduced images and simulations, catalogues public
DESI, EHT and JWST releases with a collaborator's share and an observatory
export, downloads them, absorbs a reprocessed data release, and hands the
catalogue to collaborators. Every step runs the installed entry point in a
subprocess on a machine with no Git identity, netrc or AWS settings, so
argument parsing, prompts on a piped stdin, exit codes and environment
handling are the real ones.

Each phase is one test; they share one world and run in order, and a phase
is skipped once an earlier one failed. The last test fails if any command
or option of the CLI was never used.
"""

import csv
from pathlib import Path
import re
import subprocess

import pytest
import yaml

import hallmark
from hallmark.cli import hallmark as hm_group
from chaos import check_invariants, snapshot_hm
from e2e_world import (
    DESI_PIXELS, MIRROR_EXTRA, MOD, add_lab_night, build_world, cli_hits,
    cli_requirements, make_lab_products, make_sims, retire_desi_file,
)
from survey import DESI_DARK, apply_dr_update, apply_silent_corruption

pytestmark = pytest.mark.e2e

SPECTRA_FMT = "{group:d}/{pixel:d}/{product}-main-dark-{hpx:d}.fits"
EXPOSURES_FMT = "{group:d}/{pixel:d}/hpixexp-main-dark-{hpx:d}.csv"
UVFITS_FMT = ("uvfits/{release}_{src}_{year:d}_{doy:03d}_{band}"
              "_hops_netcal_StokesI.uvfits")
JWST_FMT = "{target}/jw{program}-{obs}_{visit}_{instrument}_{optics}_x1dints.fits"
REDROCK_23040 = "230/23040/redrock-main-dark-23040.fits"
RETIRED = "230/23041/qso_mgii-main-dark-23041.fits"
_failed = []


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    Path(hallmark.__file__).resolve().relative_to(MOD)
    with build_world(tmp_path_factory.mktemp("e2e-cli")) as world:
        yield world


@pytest.fixture(autouse=True)
def in_order(request):
    """Skip the remaining phases once one fails."""
    if _failed:
        pytest.skip(f"an earlier phase failed: {_failed[0]}")
    yield
    report = getattr(request.node, "report_call", None)
    if report is not None and report.failed:
        _failed.append(request.node.name)


def work(world):
    return world.base / "work"


def run(world, *args, cwd, input="", expect=0):
    return world.hm(*args, cwd=cwd, input=input, expect=expect)


def refused(world, repo, *args, phrase, cwd=None, input="", expect=1):
    """Run a command that must fail and leave the repository unchanged."""
    before = snapshot_hm(repo)
    result = run(world, *args, cwd=cwd or repo, input=input, expect=expect)
    assert phrase in result.text, result.text
    assert snapshot_hm(repo) == before
    return result


def config(repo):
    return yaml.safe_load((Path(repo) / ".hm" / "config.yml").read_text())


def table(repo, name):
    with (Path(repo) / ".hm" / name).open(newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def catalogued(repo):
    """Every catalogued path: TSV rows and ``file:`` entries."""
    paths = set()
    for path in (Path(repo) / ".hm").glob("*.tsv"):
        paths.update(row["path"] for row in table(repo, path.name))
    for section in ("data", "meta"):
        paths.update(entry["file"] for entry in config(repo).get(section) or []
                     if isinstance(entry, dict) and "file" in entry)
    return paths


def head(repo):
    return subprocess.run(["git", "-C", str(Path(repo) / ".hm"), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def assert_payloads(world, repo, paths):
    """The worktree holds the servers' current bytes for ``paths``."""
    expected = world.payloads()
    for path in paths:
        assert (Path(repo) / path).read_bytes() == expected[path], path


def assert_sound(repo, **options):
    found = check_invariants(repo, **options)
    assert not found, str(found)


def identify(world, repo):
    run(world, "config", "user.name", "Ada Lovelace", cwd=repo)
    run(world, "config", "user.email", "ada@example.org", cwd=repo)


def test_outside_a_repository(world):
    version = run(world, "--version", cwd=work(world))
    assert re.fullmatch(r"hm, version \S+", version.stdout.strip())
    usage = run(world, "--help", cwd=work(world))
    assert all(name in usage.stdout for name in hm_group.commands)
    assert "--skip-duplicates" in run(world, "add", "--help", cwd=work(world)).stdout
    missing = run(world, "status", cwd=work(world), expect=1)
    assert "Failed to open hallmark repository" in missing.text


def test_lab_catalogue(world):
    lab = work(world) / "lab"
    images = make_lab_products(lab)
    assert "Successfully initialized" in run(world, "init", "lab", cwd=work(world)).text
    for where in (lab, lab / "M87" / "2017"):
        info = run(world, "info", cwd=where).stdout
        assert f'"{(lab / ".hm").resolve()}"' in info
        assert f'"{lab.resolve()}"' in info
    identify(world, lab)
    assert run(world, "config", "user.name", cwd=lab).stdout.strip() == "Ada Lovelace"

    added = run(world, "add", "{source}/{year}/{doy:03d}_{band}.fits", cwd=lab)
    assert "M87/2017/README.txt" not in added.text
    staged = run(world, "status", cwd=lab).stdout
    assert all(f"new file:   {path}" in staged for path in images)
    assert "Untracked" in staged and "M87/2017/README.txt" in staged
    run(world, "commit", "-m", "Reduce the 2017 campaign", cwd=lab)
    first = head(lab)
    assert "Reduce the 2017 campaign" in run(world, "log", cwd=lab).stdout

    night = add_lab_night(lab)
    run(world, "add", "M87/2018/", cwd=lab)
    extra = add_lab_night(lab / "late", seed=2)
    for path, data in extra.items():
        target = lab / path.replace("111_", "112_")
        target.write_bytes(data)
    (lab / "SgrA/2017/098_lo.fits").write_bytes(b"SIMPLE  =  T" + b" " * 2868)
    run(world, "add", "M87/2018/112_lo.fits", "SgrA/2017/098_lo.fits", cwd=lab)
    run(world, "restore", "--staged", "SgrA/2017/098_lo.fits", cwd=lab)
    status = run(world, "status", cwd=lab).stdout
    assert "new file:   M87/2018/111_lo.fits" in status
    assert "new file:   SgrA/2017/098_lo.fits" not in status
    run(world, "restore", "--staged", "M87/2018/", cwd=lab)
    status = run(world, "status", cwd=lab).stdout
    assert "new file:   M87/2018/111_lo.fits" not in status
    run(world, "add", "M87/2018/", cwd=lab)
    run(world, "restore", "--staged", ".", cwd=lab)
    assert "new file:" not in run(world, "status", cwd=lab).stdout
    run(world, "add", ".", cwd=lab)
    status = run(world, "status", cwd=lab).stdout
    for path in [*night, "M87/2018/112_lo.fits", "SgrA/2017/098_lo.fits"]:
        assert f"new file:   {path}" in status
    run(world, "commit", "-m", "Add the 2018 night", cwd=lab)
    (lab / "M87/2018/113_hi.fits").write_bytes(b"SIMPLE  =  T" + b" " * 2868)
    run(world, "add", "113_hi.fits", cwd=lab / "M87" / "2018")
    assert "new file:   M87/2018/113_hi.fits" in run(world, "status", cwd=lab).stdout
    run(world, "commit", "-m", "Add a late scan", cwd=lab)

    assert 'Created branch "recal"' in run(world, "branch", "recal", cwd=lab).text
    branches = run(world, "branch", cwd=lab).stdout
    assert "* main" in branches and "recal" in branches
    assert "Switched to branch" in run(world, "checkout", "recal", cwd=lab).text
    original = (lab / "M87/2017/095_lo.fits").read_bytes()
    (lab / "M87/2017/095_lo.fits").write_bytes(original[:-1] + b"!")
    run(world, "add", ".", cwd=lab / "SgrA")
    assert "modified:   M87/2017/095_lo.fits" in run(world, "status", cwd=lab).stdout
    run(world, "commit", "-m", "Recalibrate day 95", cwd=lab)

    assert "not on a branch" in run(world, "checkout", first, cwd=lab).text
    assert not (lab / "M87/2018/111_lo.fits").exists()
    assert "no branch" in run(world, "branch", cwd=lab).stdout
    refused(world, lab, "commit", "-m", "lost", phrase="not on a branch")
    assert "Switched to branch" in run(world, "checkout", "main", cwd=lab).text
    assert (lab / "M87/2017/095_lo.fits").read_bytes() == original
    (lab / "M87/2017/095_lo.fits").write_bytes(b"scribbled")
    refused(world, lab, "checkout", "recal", phrase="uncommitted changes")
    (lab / "M87/2017/095_lo.fits").write_bytes(original)
    status = run(world, "status", cwd=lab).stdout
    assert "Changes" not in status and "modified" not in status
    refused(world, lab, "commit", phrase="Missing option", expect=2)
    refused(world, lab, "set-config", "--fmt",
            "{source}/{year:d}/{band}_{doy:d}.fits", phrase="don't fit")
    assert_sound(lab)


def test_simulation_library(world):
    sims = work(world) / "sims"
    make_sims(sims)
    run(world, "init", "sims", cwd=work(world))
    identify(world, sims)
    refused(world, sims, "set-config", "--encoding", "aspin",
            phrase="FIELD=REGEX")
    run(world, "set-config", "--fmt", "a{aspin}_i{inc}.h5",
        "--encoding", "aspin=m([0-9.]+)", "--encoding", "inc=([0-9]+)", cwd=sims)
    run(world, "add", "--regex", "a{aspin}_i{inc}.h5", cwd=sims)
    spins = {row["path"]: float(row["aspin"]) for row in table(sims, "data.tsv")}
    assert spins == {"a0_i30.h5": 0, "a0.5_i30.h5": 0.5, "am0.5_i30.h5": -0.5,
                     "a0.94_i60.h5": 0.94, "am0.94_i60.h5": -0.94}
    run(world, "commit", "-m", "Spin sweep", cwd=sims)
    assert_sound(sims)


def test_survey_catalogue(world):
    survey = work(world) / "survey"
    run(world, "init", "survey", cwd=work(world))
    identify(world, survey)
    run(world, "set-config", "--remote-name", "desi", "--remote-url", world.desi_url,
        cwd=survey)
    detected = run(world, "detect", "desi", cwd=survey).stdout
    assert "Detected" in detected and "To catalog with them:" in detected
    detected = run(world, "detect", world.desi_url + "230/", "--filter", "**/*.fits",
                   cwd=survey).stdout
    assert "230/{p0}/redrock-main-dark-{p1}.fits" in detected
    assert "sha256sum" not in detected

    quicklook = work(world) / "quicklook"
    run(world, "init", "quicklook", cwd=work(world))
    run(world, "set-config", "--remote-name", "desi", "--remote-url", world.desi_url,
        cwd=quicklook)
    run(world, "detect", "desi", "--filter", "**/redrock-*", "--add", cwd=quicklook)
    assert {row["path"] for name in ("data.tsv",)
            for row in table(quicklook, name)} | catalogued(quicklook) == {
        f"{group}/{pixel}/redrock-main-dark-{pixel}.fits"
        for group, pixel in DESI_PIXELS}

    run(world, "add", world.desi_url, "--source", "desi",
        "--fmt", f"{SPECTRA_FMT}=spectra",
        "--filter", "**/*.fits", "--filter", "**/*.csv", cwd=survey)
    spectra = {row["path"]: row for row in table(survey, "spectra.tsv")}
    assert len(spectra) == 4
    assert all(row["checksum_algorithm"] == "sha256" for row in spectra.values())
    assert spectra[REDROCK_23040]["product"] == "redrock"
    added = run(world, "add", world.eht_url, "--backend", "cyverse",
                "--remote", "eht", "--source", "m87", "--prefix", "eht",
                "--fmt", f"{UVFITS_FMT}=uvfits", cwd=survey, input="unsorted\n")
    assert "already sorted" in added.text
    uvfits = table(survey, "uvfits.tsv")
    assert len(uvfits) == 8 and {row["band"] for row in uvfits} == {"lo", "hi"}
    assert "eht/README.md" in catalogued(survey)

    refused(world, survey, "add", world.eht_url, "--source", "m87",
            "--server-checksum", "sha256",
            phrase="cannot compute checksums on the server")
    bad = work(world) / "bad-options.yml"
    bad.write_text("- endpoint_url\n")
    refused(world, survey, "add", world.eht_url, "--source", "m87",
            "--backend-options", bad, phrase="must contain a YAML mapping")
    refused(world, survey, "add", world.mirror_url, "--remote", "mirror",
            "--source", "desi-extra", "--filter", "**/*.fits",
            phrase="--skip-duplicates")
    mirrored = run(world, "add", world.mirror_url, "--remote", "mirror",
                   "--source", "desi-extra", "--filter", "**/*.fits",
                   "--filter", "**/*.csv", "--skip-duplicates", cwd=survey)
    assert "same content; source 'desi'" in mirrored.text
    group, pixel = MIRROR_EXTRA
    extra = {row["path"] for row in table(survey, "spectra.tsv")
             if row["catalog_source"] == "desi-extra"}
    assert extra == {f"{group}/{pixel}/{product}-main-dark-{pixel}.fits"
                     for product in ("redrock", "qso_mgii")}

    run(world, "add", world.dav_url, "--auth", "collab", "--remote", "collab",
        "--source", "notes", "--prefix", "collab", cwd=survey)
    assert {"collab/README.txt", "collab/gains/2017/M87_095_gains.csv"} <= \
        catalogued(survey)

    if world.s3 is not None:
        run(world, "add", world.jwst_url, "--backend-options", world.s3["options"],
            "--auth", "jwst", "--remote", "jwst", "--source", "jwst",
            "--prefix", "jwst", "--fmt", f"{JWST_FMT}=jwst", cwd=survey,
            input="unsorted\n")
        jwst = table(survey, "jwst.tsv")
        assert len(jwst) == 4
        assert {row["checksum_algorithm"] for row in jwst} == {"sha256"}
        assert not any(row["catalog_source"] == "jwst"
                       for row in table(survey, "spectra.tsv"))
        paged = work(world) / "s3-paged.yml"
        options = yaml.safe_load(world.s3["options"].read_text())
        paged.write_text(yaml.safe_dump({**options, "page_size": 3}))
        run(world, "set-config", "--remote-name", "jwst", "--remote-backend", "s3",
            "--remote-backend-options", paged, cwd=survey)
        assert config(survey)["remote"][-1]["backend_options"]["page_size"] == 3
        before = table(survey, "jwst.tsv")
        run(world, "add", world.jwst_url, "--source", "jwst", cwd=survey)
        assert table(survey, "jwst.tsv") == before

    if world.ssh is not None:
        run(world, "add", world.ssh["url"], "--server-checksum", "sha256",
            "--auth", "obs", "--remote", "obs", "--source", "alma",
            "--prefix", "obs", "--filter", "ALMA/**", cwd=survey)
        alma = {entry["file"]: entry for entry in config(survey)["data"]
                if entry.get("catalog_source") == "alma"}
        assert len(alma) == 4 and all("sha256" in entry for entry in alma.values())

    run(world, "set-config", "--remote-name", "eht", "--remote-backend", "cyverse",
        cwd=survey)
    refused(world, survey, "set-config", "--remote-name", "eht",
            "--remote-backend-options", bad, phrase="must contain a YAML mapping")
    status = run(world, "status", cwd=survey).stdout
    assert "new file:   eht/uvfits/" in status
    run(world, "commit", "-m", "Catalogue DESI, EHT, JWST and the shares", cwd=survey)
    assert "remote catalog unchanged" in run(world, "status", cwd=survey).stdout
    assert_sound(survey)


def test_downloads(world):
    survey = work(world) / "survey"
    planned = run(world, "download", "--all", "--dry-run", cwd=survey).stdout
    assert "Sources:" in planned
    assert not list(survey.glob("230/*/*.fits"))

    before = len(world.desi.payload_gets())
    fetched = run(world, "download", "--remote", "mirror", "--source", "desi",
                  "--filter", "230/23040/*", cwd=survey, input="y\n")
    assert "Successfully downloaded 3 files" in fetched.text
    assert len(world.desi.payload_gets()) == before
    assert len([path for path in world.mirror.payload_gets()
                if "/23040/" in path and not path.endswith(".sha256sum")]) == 3
    run(world, "download", "--source", "desi", "--max-workers", "2", cwd=survey,
        input="y\n")
    run(world, "download", "--source", "desi-extra", cwd=survey, input="y\n")
    run(world, "download", "--tsv", "uvfits", cwd=survey, input="y\n")
    run(world, "download", "--fmt",
        "eht/csv/SR1_M87_2017_{doy:03d}_{band}_hops_netcal_StokesI.csv",
        cwd=survey, input="y\n")
    run(world, "download", "eht/README.md", cwd=survey, input="y\n")

    run(world, "set-config", "--remote-name", "collab", "--remote-auth", "",
        cwd=survey)
    denied = run(world, "download", "--source", "notes", cwd=survey, input="y\n",
                 expect=1)
    assert "Set an auth profile for remote 'collab'" in denied.text
    assert "~/.netrc" in denied.text
    run(world, "set-config", "--remote-name", "collab", "--remote-auth", "collab",
        cwd=survey)
    run(world, "download", "collab/README.txt", "collab/gains/2017/M87_095_gains.csv",
        cwd=survey, input="y\n")
    shared = work(world) / "for-alice"
    run(world, "download", "--filter", "collab/gains/**", "--output", shared,
        cwd=survey, input="y\n")
    assert (shared / "collab/gains/2017/M87_096_gains.csv").read_bytes() == \
        world.dav.files["gains/2017/M87_096_gains.csv"]
    run(world, "download", "collab/gains/2017/M87_096_gains.csv", cwd=survey,
        input="y\n")
    if world.s3 is not None:
        run(world, "download", "--source", "jwst", cwd=survey, input="y\n")
    if world.ssh is not None:
        run(world, "download", "--source", "alma", "--max-workers", "2",
            cwd=survey, input="y\n")

    assert "No files selected" in run(world, "download", "--all", cwd=survey,
                                      input="y\n").text
    assert_payloads(world, survey, catalogued(survey))
    assert not (survey / REDROCK_23040).stat().st_mode & 0o222
    run(world, "commit", "-m", "Record the downloaded SHA-1s", cwd=survey)
    assert_sound(survey)


def test_reprocessed_release(world):
    survey = work(world) / "survey"
    run(world, "branch", "iron-v1", cwd=survey)
    changed, new = apply_dr_update(world.desi.root, world.desi_files)
    assert changed == f"{DESI_DARK}/{REDROCK_23040}"
    retire_desi_file(world.desi.root, world.desi_files, f"{DESI_DARK}/{RETIRED}")

    kept = run(world, "add", world.desi_url, "--source", "desi", "--keep-missing",
               cwd=survey)
    assert "Kept in the catalog, though no longer on the server: 1 file(s)" in \
        kept.text
    assert "Changed upstream (1)" in kept.text and REDROCK_23040 in kept.text
    assert RETIRED in catalogued(survey)
    pruned = run(world, "add", world.desi_url, "--source", "desi", "--prune",
                 cwd=survey)
    assert "Removed from the catalog" in pruned.text, pruned.text
    assert RETIRED not in catalogued(survey)
    assert "230/23042/redrock-main-dark-23042.fits" in catalogued(survey)
    assert "modified:   " + REDROCK_23040 in run(world, "status", cwd=survey).stdout

    replaced = run(world, "download", "--source", "desi", cwd=survey, input="y\n")
    assert "Replaced 1 outdated" in replaced.text
    assert_payloads(world, survey, [REDROCK_23040,
                                    "230/23042/redrock-main-dark-23042.fits"])
    run(world, "commit", "-m", "Absorb the iron reprocessing", cwd=survey)

    switched = run(world, "checkout", "iron-v1", cwd=survey)
    assert "Outdated downloads: 1 file(s)" in switched.text
    assert "Outdated downloads" in run(world, "status", cwd=survey).stdout
    placed = run(world, "download", "--source", "desi", cwd=survey, input="y\n")
    assert "from .hm/objects" in placed.text
    assert (survey / REDROCK_23040).read_bytes() != world.payloads()[REDROCK_23040]
    assert "Outdated downloads: 1 file(s)" in run(world, "checkout", "main",
                                                  cwd=survey).text
    run(world, "download", "--source", "desi", cwd=survey, input="y\n")
    assert_payloads(world, survey, [REDROCK_23040])

    resorted = run(world, "add", world.desi_url, "--source", "desi",
                   "--fmt", f"{EXPOSURES_FMT}=exposures", cwd=survey,
                   input="resort\n")
    assert "already sorted" in resorted.text
    exposures = {row["path"] for row in table(survey, "exposures.tsv")}
    assert exposures == {f"230/{pixel}/hpixexp-main-dark-{pixel}.csv"
                         for pixel in (23040, 23041, 23042)}
    assert not any(entry.get("file", "").endswith("23040.csv")
                   for entry in config(survey)["data"])
    run(world, "commit", "-m", "Sort the exposure tables", cwd=survey)
    assert_sound(survey)


def test_collaborators(world):
    base = work(world)
    survey = base / "survey"
    source = str(survey / ".hm")
    world.publish(survey)

    assert "Successfully cloned" in run(world, "clone", source, "copy",
                                        "--no-download", cwd=base).text
    assert catalogued(base / "copy") == catalogued(survey)
    assert not list((base / "copy").glob("230/*/*"))
    run(world, "clone", "--source-type", "git", source, "git-copy", "--no-download",
        cwd=base)
    run(world, "clone", source, "eht-only", "--filter", "eht/**",
        "--max-workers", "2", cwd=base, input="y\n")
    assert_payloads(world, base / "eht-only",
                    [path for path in catalogued(survey) if path.startswith("eht/")])
    assert not (base / "eht-only" / REDROCK_23040).exists()
    run(world, "clone", source, "uvfits-only", "--fmt",
        "eht/uvfits/SR1_M87_2017_{doy:03d}_{band}_hops_netcal_StokesI.uvfits",
        cwd=base, input="y\n")
    assert len(list((base / "uvfits-only/eht/uvfits").glob("*.uvfits"))) == 8
    assert not (base / "uvfits-only/eht/README.md").exists()
    later = run(world, "clone", source, "later", cwd=base, input="n\n")
    assert "Skipped download" in later.text

    refused_bare = run(world, "clone", source, "archive.hm", cwd=base, expect=1)
    assert "--no-download" in refused_bare.text
    assert not (base / "archive.hm").exists()
    run(world, "clone", source, "archive.hm", "--no-download", cwd=base)
    assert '"None"' in run(world, "info", cwd=base / "archive.hm").stdout
    no_output = run(world, "download", "--all", cwd=base / "archive.hm", expect=1)
    assert "--output" in no_output.text

    run(world, "clone", "--source-type", "catalog", world.snapshot_url, "snapshot",
        "--no-download", cwd=base)
    assert catalogued(base / "snapshot") == catalogued(survey)
    hint = run(world, "clone", "--source-type", "catalog", world.private_url,
               "private", "--no-download", cwd=base, expect=1)
    assert "hm clone --auth PROFILE" in hint.text
    assert not (base / "private").exists()
    run(world, "clone", "--source-type", "catalog", "--auth", "group",
        world.private_url, "private", "--no-download", cwd=base)
    assert catalogued(base / "private") == catalogued(survey)
    run(world, "clone", "--auth", "group", source, "git-auth", "--no-download",
        cwd=base, expect=1)

    apply_dr_update(world.mirror.root, world.mirror_files)
    retire_desi_file(world.mirror.root, world.mirror_files, f"{DESI_DARK}/{RETIRED}")
    corrupted = "230/23041/redrock-main-dark-23041.fits"
    apply_silent_corruption(world.mirror.root, f"{DESI_DARK}/{corrupted}")
    failed = run(world, "download", "--remote", "mirror", "--source", "desi",
                 cwd=base / "later", input="y\n", expect=1)
    assert f"{corrupted}: Checksum mismatch (sha256)" in failed.text
    assert "Failed to download 1 file(s)" in failed.text
    assert not (base / "later" / corrupted).exists()
    assert_payloads(world, base / "later", [REDROCK_23040])

    for name in ("copy", "git-copy", "later", "archive.hm", "snapshot", "private"):
        assert_sound(base / name, objects=False)
    for name in ("eht-only", "uvfits-only"):
        assert_sound(base / name)


def test_every_cli_option_is_used(world):
    used, succeeded = cli_hits(world.hm.calls, hm_group)
    required = cli_requirements(hm_group)
    missing = sorted(required - used, key=str)
    assert not missing, f"never used: {missing}"
    only_failed = sorted(required - succeeded, key=str)
    lanes_off = set(world.skipped)
    allowed = set()
    if "s3" in lanes_off:
        allowed |= {("add", "--backend-options"),
                    ("set-config", "--remote-backend-options")}
    if "ssh" in lanes_off:
        allowed |= {("add", "--server-checksum")}
    assert set(only_failed) <= allowed, f"used only in failing calls: {only_failed}"
