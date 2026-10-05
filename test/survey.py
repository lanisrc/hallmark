"""Synthetic survey data releases and a CLI helper for workflow tests.

The layouts follow real releases, scaled down to a few kilobytes:
DESI DR1 healpix directories with sha256 manifests, EHT UVFITS files on
CyVerse, a simulation export and a telescope tree. Contents are
deterministic for a seed so failures reproduce.
"""

from __future__ import annotations

import hashlib
import random

from click.testing import CliRunner

from hallmark.cli import hallmark
from hallmark.utils import use_working_directory
from http_server import write_file

DESI_DARK = "public/dr1/spectro/redux/iron/healpix/main/dark"
EHT_UVFITS = "EHTC_FirstM87Results_Apr2019/uvfits"


def _fits_like(rng, size):
    """Bytes that start like a FITS header, padded to ``size``."""
    header = b"SIMPLE  =                    T".ljust(80) + b"END".ljust(80)
    return header + rng.randbytes(max(0, size - len(header)))


def _write_manifest(root, directory, names, files, name):
    """Write a sha256sum-style manifest for ``names`` in ``directory``."""
    lines = "".join(
        f"{hashlib.sha256(files[f'{directory}/{n}']).hexdigest()}  {n}\n"
        for n in names)
    write_file(root, f"{directory}/{name}", lines)


def make_desi_like_release(root, pixels=((230, 23040), (230, 23041)), seed=0):
    """Write a DESI-like healpix release under ``root``.

    Each pixel directory has redrock and qso_mgii FITS-like files, an
    exposure CSV and a sha256 manifest named like the real release.

    Returns:
        dict: Release-relative path to contents, excluding manifests.
    """
    rng = random.Random(seed)
    files = {}
    for group, pixel in pixels:
        directory = f"{DESI_DARK}/{group}/{pixel}"
        names = [f"redrock-main-dark-{pixel}.fits",
                 f"qso_mgii-main-dark-{pixel}.fits",
                 f"hpixexp-main-dark-{pixel}.csv"]
        for name in names:
            if name.endswith(".csv"):
                data = f"EXPID,NIGHT\n{rng.randrange(10**5)},20210514\n".encode()
            else:
                data = _fits_like(rng, 2880 + rng.randrange(2880))
            write_file(root, f"{directory}/{name}", data)
            files[f"{directory}/{name}"] = data
        _write_manifest(root, directory, names, files,
                        f"redux_iron_healpix_main_dark_{group}_{pixel}.sha256sum")
    return files


def make_eht_like_release(root, seed=0):
    """Write EHT-like UVFITS files without a checksum manifest.

    Returns:
        dict: Release-relative path to contents.
    """
    rng = random.Random(seed)
    files = {}
    for day in (95, 96, 100, 101):
        for band in ("lo", "hi"):
            path = (f"{EHT_UVFITS}/SR1_M87_2017_{day:03d}_{band}"
                    "_hops_netcal_StokesI.uvfits")
            files[path] = _fits_like(rng, 2880 + rng.randrange(1440))
            write_file(root, path, files[path])
    return files


def make_lab_export(root, runs=2, frames=3, seed=0):
    """Write a simulation export laid out as ``run{run}/frame{frame}.h5``.

    Returns:
        dict: Export-relative path to contents.
    """
    rng = random.Random(seed)
    files = {}
    for run in range(1, runs + 1):
        for frame in range(frames):
            path = f"run{run}/frame{frame}.h5"
            files[path] = b"\x89HDF\r\n\x1a\n" + rng.randbytes(512)
            write_file(root, path, files[path])
    return files


def make_telescope_tree(root, sites=("ALMA", "SMA"), years=(2024, 2025),
                        days=(95, 96), seed=0):
    """Write observations laid out as ``{site}/{year:d}/{day:d}.fits``.

    Returns:
        dict: Worktree-relative path to contents.
    """
    rng = random.Random(seed)
    files = {}
    for site in sites:
        for year in years:
            for day in days:
                path = f"{site}/{year}/{day}.fits"
                files[path] = _fits_like(rng, 2880)
                write_file(root, path, files[path])
    return files


def apply_dr_update(root, files, pixel=(230, 23042), seed=1):
    """Reprocess one redrock file and add a pixel, updating manifests.

    Returns:
        tuple: The changed path and the paths of the new pixel's files.
    """
    rng = random.Random(seed)
    changed = sorted(path for path in files if "/redrock-" in path)[0]
    files[changed] = _fits_like(rng, len(files[changed]))
    write_file(root, changed, files[changed])
    directory = changed.rsplit("/", 1)[0]
    group, number = directory.split("/")[-2:]
    names = sorted(path.rsplit("/", 1)[1] for path in files
                   if path.rsplit("/", 1)[0] == directory)
    _write_manifest(root, directory, names, files,
                    f"redux_iron_healpix_main_dark_{group}_{number}.sha256sum")
    added = make_desi_like_release(root, pixels=(pixel,), seed=seed)
    files.update(added)
    return changed, sorted(added)


def apply_silent_corruption(root, path):
    """Change a published file without updating its manifest."""
    target = root / path
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))


class Hm:
    """Run ``hm`` commands in-process through Click's test runner."""

    def __init__(self):
        self.runner = CliRunner()

    def __call__(self, *args, cwd, input=None, expect=0):
        """Run ``hm ARGS`` in ``cwd`` and check its exit code.

        Args:
            cwd: Directory to run in.
            input (str, optional): Text for prompts, such as ``"y\\n"``.
            expect (int | None): Required exit code, or None to skip the check.

        Returns:
            click.testing.Result: The completed invocation.
        """
        with use_working_directory(cwd):
            result = self.runner.invoke(hallmark, [str(arg) for arg in args],
                                        input=input)
        if expect is not None and result.exit_code != expect:
            raise AssertionError(
                f"hm {' '.join(map(str, args))} exited {result.exit_code}, "
                f"expected {expect}:\n{result.output}") from result.exception
        return result
