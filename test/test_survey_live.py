"""Tiny end-to-end downloads from real public survey archives.

These follow the public-collection examples in doc/private_data.rst
against DESI DR1 and the EHT M87 release on CyVerse. They need network
access, so they only run with HALLMARK_RUN_LIVE_TESTS=1 (nightly in CI).
Every plan is checked before approval so a run downloads at most a few
megabytes. CyVerse lists sizes such as "554K" that Hallmark does not
record, so its downloads are limited to single, exactly named files.
"""

import hashlib
import os

import pytest
import requests

from hallmark import Repo

pytestmark = [
    pytest.mark.survey_live,
    pytest.mark.skipif(os.environ.get("HALLMARK_RUN_LIVE_TESTS") != "1",
                       reason="Set HALLMARK_RUN_LIVE_TESTS=1 to reach public archives"),
]

DESI = ("https://data.desi.lbl.gov/public/dr1/spectro/redux/iron/"
        "healpix/main/dark/230/23040/")
EHT = ("https://data.cyverse.org/dav-anon/iplant/commons/"
       "cyverse_curated/EHTC_FirstM87Results_Apr2019/uvfits/")
MAX_TARGET_BYTES = 2_000_000
MAX_RUN_BYTES = 10_000_000
_downloaded = []


@pytest.fixture(scope="module")
def archive_available():
    """Skip, rather than fail, when an archive itself is down or overloaded.

    The probe uses plain Requests with Hallmark's timeouts, so a failure
    after a successful probe points at Hallmark rather than the archive.
    """
    checked = {}

    def check(url):
        if url not in checked:
            try:
                status = requests.get(url, timeout=(10, 30)).status_code
                checked[url] = (status < 500, f"HTTP {status}")
            except requests.RequestException as exc:
                checked[url] = (False, type(exc).__name__)
        available, detail = checked[url]
        if not available:
            pytest.skip(f"{url} is unavailable ({detail})")

    return check


def _download(repo, plan):
    """Download a plan, skipping when the archive times out or errors."""
    result = repo.download(plan, approved=True)
    errors = " ".join(map(str, result["errors"]))
    if result["failed"] and all(
            "ReadTimeout" in str(error) or "ConnectTimeout" in str(error)
            or "HTTP 5" in str(error) for error in result["errors"]):
        pytest.skip(f"The archive failed during the download: {errors}")
    return result


def _approve_small(plan, expected_files, sizes_listed=True):
    """Refuse to download anything unexpectedly large.

    Without listed sizes, only a single file the test names exactly is
    allowed, and its size is checked after the download.
    """
    assert plan.file_count == expected_files, plan.summary()
    if sizes_listed:
        assert plan.unknown_size_count == 0, plan.summary()
    else:
        assert plan.file_count == 1, plan.summary()
    assert plan.known_bytes <= MAX_TARGET_BYTES, plan.summary()
    assert sum(_downloaded) + plan.known_bytes <= MAX_RUN_BYTES
    _downloaded.append(plan.known_bytes or MAX_TARGET_BYTES)


def test_desi_dr1_files_match_published_checksums(tmp_path, archive_available):
    archive_available(DESI)
    repo = Repo.init(tmp_path / "desi")
    repo.add(DESI, filter=["redrock-main-dark-23040.fits",
                           "hpixexp-main-dark-23040.csv"])
    rows = repo.state.data.set_index("path")
    assert set(rows.checksum_algorithm) == {"sha256"}
    repo.commit("Record remote catalog")

    plan = repo.plan_download(all_files=True)
    _approve_small(plan, expected_files=2)
    result = _download(repo, plan)
    assert (result["succeeded"], result["failed"]) == (2, 0), result
    for path, row in rows.iterrows():
        data = (repo.worktree / path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row.checksum
        assert len(data) == int(row.size_bytes)


def test_eht_m87_uvfits_downloads_from_cyverse(tmp_path, archive_available):
    archive_available(EHT)
    name = "SR1_M87_2017_095_lo_hops_netcal_StokesI.uvfits"
    repo = Repo.init(tmp_path / "eht")
    repo.add(EHT, filter=name)
    assert list(repo.state.data.path) == [name]
    repo.commit("Record remote catalog")

    plan = repo.plan_download(all_files=True)
    _approve_small(plan, expected_files=1, sizes_listed=False)
    result = _download(repo, plan)
    assert (result["succeeded"], result["failed"]) == (1, 0), result
    data = (repo.worktree / name).read_bytes()
    assert data.startswith(b"SIMPLE")
    assert len(data) <= MAX_TARGET_BYTES


def test_eht_url_pattern_catalogs_every_day_and_band(tmp_path, archive_available):
    archive_available(EHT)
    repo = Repo.init(tmp_path / "eht")
    repo.add(EHT + "SR1_M87_2017_{day:d}_{band}_hops_netcal_StokesI.uvfits")
    rows = repo.state.data
    assert set(rows.band) == {"lo", "hi"}
    assert {95, 96, 100, 101} <= {int(day) for day in rows.day}
    repo.commit("Record remote catalog")

    plan = repo.plan_download(all_files=True, filter="*_100_lo_*")
    _approve_small(plan, expected_files=1, sizes_listed=False)
    result = _download(repo, plan)
    assert (result["succeeded"], result["failed"]) == (1, 0), result
