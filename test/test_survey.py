"""End-to-end survey workflows against loopback HTTP and SSH servers.

Scenarios follow doc/usecase.rst and the public-data examples in
doc/private_data.rst, using scaled-down synthetic releases. No request
leaves the machine.
"""

import hashlib
import shutil

import pandas as pd
import pytest

from hallmark import ParaFrame, Repo
from survey import (DESI_DARK, EHT_UVFITS, Hm, apply_dr_update,
                    apply_silent_corruption, make_desi_like_release,
                    make_eht_like_release, make_lab_export, make_telescope_tree)

pytestmark = [pytest.mark.survey, pytest.mark.usefixtures("loopback_only")]

TELESCOPE_FMT = "{site}/{year:d}/{day:d}.fits"
DESI_PATTERN = "{group:d}/{pixel:d}/redrock-main-dark-{p:d}.fits"


@pytest.fixture
def hm(tmp_path, monkeypatch):
    """Run hm with no local authentication profiles."""
    monkeypatch.delenv("HALLMARK_AUTH_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return Hm()


@pytest.fixture
def desi(tmp_path, http_server):
    """A DESI-like release served with Apache-style listings."""
    www = tmp_path / "www"
    files = make_desi_like_release(www)
    return {"www": www, "files": files, "server": http_server(www)}


def _catalog(repo_path):
    return pd.read_csv(repo_path / ".hm" / "data.tsv", sep="\t", dtype=str,
                       keep_default_na=False)


def _assert_downloaded(worktree, files, prefix=""):
    for path, data in files.items():
        relative = path[len(prefix):] if prefix else path
        assert (worktree / relative).read_bytes() == data, relative


# 1. CLI: standard repository ingest
def test_telescope_ingest_commit_and_update(tmp_path, hm):
    obs = tmp_path / "obs"
    hm("init", obs, cwd=tmp_path)
    files = make_telescope_tree(obs)
    added = hm("add", TELESCOPE_FMT, cwd=obs)
    assert all(path in added.output for path in files)
    hm("commit", "-m", "Initial observation ingest", cwd=obs)
    assert "SMA/2025/96.fits" not in hm("status", cwd=obs).output

    (obs / "SMA/2025/96.fits").write_bytes(b"recalibrated")
    assert "SMA/2025/96.fits" in hm("status", cwd=obs).output
    hm("add", ".", cwd=obs)
    hm("commit", "-m", "Recalibrate SMA day 96", cwd=obs)

    log = hm("log", cwd=obs).output
    assert log.index("Recalibrate SMA day 96") < log.index("Initial observation ingest")
    info = hm("info", cwd=obs).output
    assert str(obs.resolve()) in info or str(obs) in info
    # Local catalogs record the checksum and the pattern's fields per file.
    rows = _catalog(obs).set_index(["site", "year", "day"])
    assert rows.loc[("SMA", "2025", "96"), "sha1"] == hashlib.sha1(
        b"recalibrated").hexdigest()
    assert len(rows) == len(files)


# 2. CLI: bare repository with explicit downloads
@pytest.mark.ssh_integration
def test_bare_simulation_catalog_downloads_to_explicit_output(
        tmp_path, hm, ssh_server):
    files = make_lab_export(ssh_server["root"])
    hm("init", "sim.hm", cwd=tmp_path)
    sim = tmp_path / "sim.hm"
    hm("add", ssh_server["url"], "--fmt", "run{run:d}/frame{frame:d}.h5", cwd=sim)
    hm("commit", "-m", "Record remote catalog", cwd=sim)
    assert "sim.hm" in hm("info", cwd=sim).output

    preview = hm("download", "--all", "--output", "../outputs", "--dry-run", cwd=sim)
    assert f"{len(files)} file(s)" in preview.output
    assert not (tmp_path / "outputs").exists()
    hm("download", "--all", "--output", "../outputs", cwd=sim, input="y\n")
    _assert_downloaded(tmp_path / "outputs", files)

    # The downloaded files can become a standard repository of their own.
    outputs = tmp_path / "outputs"
    hm("init", outputs, cwd=tmp_path)
    hm("add", "run{run:d}/frame{frame:d}.h5", cwd=outputs)
    hm("commit", "-m", "Version the downloaded frames", cwd=outputs)
    assert len(_catalog(outputs)) == len(files)


# 3. Python: branch-isolated analysis with multiple worktrees
def test_second_worktree_keeps_observations_isolated(tmp_path):
    repo = Repo.init(tmp_path / "obs")
    make_telescope_tree(repo.worktree)
    repo.add(TELESCOPE_FMT)
    repo.commit("Initial observation ingest")

    assert repo.add_worktree("obs2")
    second = Repo(tmp_path / "obs2")
    make_telescope_tree(second.worktree, sites=("APEX",), seed=7)
    second.add(TELESCOPE_FMT)
    second.commit("Observation ingest on branch obs2")

    assert second.dothm.active_branch.name == "obs2"
    assert repo.dothm.active_branch.name != "obs2"
    assert not (repo.worktree / "APEX").exists()
    assert "APEX" not in set(repo.state.data.site)
    assert "APEX" in set(second.state.data.site)


# 4. Python: programmatic repository updates
def test_nightly_programmatic_ingest_keeps_every_version(tmp_path):
    repo = Repo.init(tmp_path / "obs")
    versions = set()
    for night, day in enumerate((95, 96, 97)):
        make_telescope_tree(repo.worktree, sites=("ALMA",), years=(2025,),
                            days=(day,), seed=night)
        # Each night also reprocesses the first observation.
        (repo.worktree / "ALMA/2025/95.fits").write_bytes(f"pass {night}".encode())
        repo.add(TELESCOPE_FMT)
        repo.commit(f"Nightly ingest {night}")
        versions.update(repo.state.data.sha1)
    assert len(repo.state.data) == 3
    assert all(repo.objects.contains(sha1) for sha1 in versions)
    assert all(f"Nightly ingest {night}" in repo.log() for night in range(3))


# 5. Python: in-memory state workflows
def test_paraframe_selects_files_without_a_repository(tmp_path):
    data = tmp_path / "data"
    make_telescope_tree(data)
    selected = ParaFrame.parse(TELESCOPE_FMT, base_path=data).filter(year=2025)
    assert sorted(selected.path) == sorted(
        path for path in (str(p.relative_to(data)) for p in data.rglob("*.fits"))
        if "/2025/" in path)
    assert not (data / ".hm").exists()


# 6. CLI: private lab data over SSH
@pytest.mark.ssh_integration
def test_private_lab_data_from_named_ssh_remote(tmp_path, hm, ssh_server):
    files = make_lab_export(ssh_server["root"], runs=1)
    lab = tmp_path / "lab"
    hm("init", lab, cwd=tmp_path)
    hm("add", ssh_server["url"], "--fmt", "run{run:d}/frame{frame:d}.h5", cwd=lab)
    hm("commit", "-m", "Catalog lab export", cwd=lab)
    hm("set-config", "--remote-name", "campus", "--remote-url", ssh_server["url"],
       cwd=lab)
    preview = hm("download", "--remote", "campus", "--all", "--dry-run", cwd=lab)
    assert "Source: ssh://hm-test" in preview.output
    assert not (lab / "run1").exists()
    hm("download", "--remote", "campus", "--all", cwd=lab, input="y\n")
    _assert_downloaded(lab, files)


def test_desi_like_pixel_records_published_checksums(tmp_path, hm, desi):
    pixel_dir = f"{DESI_DARK}/230/23040/"
    repo = tmp_path / "desi"
    hm("init", repo, cwd=tmp_path)
    hm("add", desi["server"].url(pixel_dir), "--filter", "redrock-*.fits", cwd=repo)
    rows = _catalog(repo)
    assert list(rows.path) == ["redrock-main-dark-23040.fits"]
    data = desi["files"][pixel_dir + "redrock-main-dark-23040.fits"]
    assert rows.loc[0, "checksum_algorithm"] == "sha256"
    assert rows.loc[0, "checksum"] == hashlib.sha256(data).hexdigest()
    assert rows.loc[0, "size_bytes"] == str(len(data))
    hm("commit", "-m", "Catalog redrock file", cwd=repo)
    hm("download", "--all", cwd=repo, input="y\n")
    assert (repo / "redrock-main-dark-23040.fits").read_bytes() == data


def test_url_pattern_add_catalogs_without_payload_requests(tmp_path, hm, desi):
    server = desi["server"]
    repo = tmp_path / "desi"
    hm("init", repo, cwd=tmp_path)
    hm("add", server.url(DESI_DARK + "/") + DESI_PATTERN, cwd=repo)
    rows = _catalog(repo)
    assert list(rows.pixel) == ["23040", "23041"]
    assert set(rows.group) == {"230"}
    payloads = [path for path in server.payload_gets()
                if not path.endswith(".sha256sum")]
    assert payloads == []

    hm("commit", "-m", "Catalog redrock files", cwd=repo)
    hm("download", "--all", "--filter", "230/23041/*", cwd=repo, input="y\n")
    assert server.payload_gets()[-1].endswith("redrock-main-dark-23041.fits")
    assert not (repo / "230/23040").exists()
    hm("download", "--all", "--fmt", "{group:d}/23040/{name}", "--output", "../out",
       cwd=repo, input="y\n")
    _assert_downloaded(tmp_path / "out", {
        path: data for path, data in desi["files"].items()
        if path.endswith("redrock-main-dark-23040.fits")}, prefix=DESI_DARK + "/")


def test_eht_like_cyverse_listing_records_sizes(tmp_path, hm, http_server):
    www = tmp_path / "www"
    files = make_eht_like_release(www)
    server = http_server(www, listing_style="cyverse")
    repo = tmp_path / "eht"
    hm("init", repo, cwd=tmp_path)
    hm("add", server.url(EHT_UVFITS + "/"), "--filter", "SR1_M87_2017_095_lo_*",
       cwd=repo)
    rows = _catalog(repo)
    name = "SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits"
    assert list(rows.path) == [name]
    assert rows.loc[0, "size_bytes"] == str(len(files[f"{EHT_UVFITS}/{name}"]))
    hm("commit", "-m", "Catalog M87 day 95", cwd=repo)
    hm("download", "--all", cwd=repo, input="y\n")
    assert (repo / name).read_bytes().startswith(b"SIMPLE")


def test_data_release_update_rescans_and_refreshes(tmp_path, hm, desi):
    server, www, files = desi["server"], desi["www"], desi["files"]
    url = server.url(DESI_DARK + "/") + DESI_PATTERN
    repo = tmp_path / "desi"
    hm("init", repo, cwd=tmp_path)
    hm("add", url, cwd=repo)
    hm("commit", "-m", "DR1 catalog", cwd=repo)
    hm("download", "--all", cwd=repo, input="y\n")

    changed, added = apply_dr_update(www, files)
    hm("add", url, cwd=repo)
    rows = _catalog(repo).set_index("path")
    relative = changed[len(DESI_DARK) + 1:]
    assert rows.loc[relative, "checksum"] == hashlib.sha256(files[changed]).hexdigest()
    assert "230/23042/redrock-main-dark-23042.fits" in rows.index
    hm("commit", "-m", "DR1 reprocessing", cwd=repo)

    # Replacing a file with different contents means deleting it first.
    (repo / relative).unlink()
    hm("download", "--all", cwd=repo, input="y\n")
    _assert_downloaded(repo, {path: data for path, data in files.items()
                              if path.endswith(".fits") and "redrock" in path},
                       prefix=DESI_DARK + "/")


def test_silently_corrupted_release_file_is_rejected(tmp_path, hm, desi):
    server, www, files = desi["server"], desi["www"], desi["files"]
    repo = tmp_path / "desi"
    hm("init", repo, cwd=tmp_path)
    hm("add", server.url(DESI_DARK + "/") + DESI_PATTERN, cwd=repo)
    hm("commit", "-m", "DR1 catalog", cwd=repo)
    corrupted = f"{DESI_DARK}/230/23041/redrock-main-dark-23041.fits"
    apply_silent_corruption(www, corrupted)

    result = hm("download", "--all", cwd=repo, input="y\n", expect=1)
    assert "1 file" in result.output
    assert not (repo / "230/23041/redrock-main-dark-23041.fits").exists()
    assert not list(repo.rglob("*.part"))
    intact = f"{DESI_DARK}/230/23040/redrock-main-dark-23040.fits"
    assert (repo / "230/23040/redrock-main-dark-23040.fits").read_bytes() == \
        files[intact]


def test_clone_git_catalog_prompts_then_downloads(tmp_path, hm, desi):
    source = tmp_path / "desi"
    hm("init", source, cwd=tmp_path)
    hm("add", desi["server"].url(DESI_DARK + "/") + DESI_PATTERN, cwd=source)
    hm("commit", "-m", "DR1 catalog", cwd=source)

    declined = hm("clone", source, "later", cwd=tmp_path, input="n\n")
    assert "2 file(s)" in declined.output
    assert "Download these files? [y/N]" in declined.output
    assert not (tmp_path / "later/230").exists()
    assert len(_catalog(tmp_path / "later")) == 2

    hm("clone", source, "copy", cwd=tmp_path, input="y\n")
    _assert_downloaded(tmp_path / "copy", {
        path: data for path, data in desi["files"].items()
        if "redrock" in path}, prefix=DESI_DARK + "/")
    assert "DR1 catalog" in hm("log", cwd=tmp_path / "copy").output

    hm("clone", source, "catalog-only.hm", "--no-download", cwd=tmp_path)
    assert (tmp_path / "catalog-only.hm/data.tsv").is_file()
    hm("clone", source, "refused.hm", cwd=tmp_path, expect=1)
    assert not (tmp_path / "refused.hm").exists()


def test_clone_published_catalog_snapshot_over_http(tmp_path, hm, desi, http_server):
    source = tmp_path / "desi"
    hm("init", source, cwd=tmp_path)
    hm("add", desi["server"].url(DESI_DARK + "/") + DESI_PATTERN, cwd=source)
    hm("commit", "-m", "DR1 catalog", cwd=source)
    published = tmp_path / "published" / "catalogs" / "desi"
    published.mkdir(parents=True)
    for name in ("config.yml", "meta.yml", "data.tsv"):
        shutil.copy(source / ".hm" / name, published / name)
    catalogs = http_server(tmp_path / "published")

    hm("clone", catalogs.url("catalogs/desi/"), "copy", cwd=tmp_path, input="y\n")
    _assert_downloaded(tmp_path / "copy", {
        path: data for path, data in desi["files"].items()
        if "redrock" in path}, prefix=DESI_DARK + "/")
