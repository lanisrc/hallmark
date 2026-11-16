"""The research cycle of ``test_e2e_cli.py`` through the public Python API.

The same group builds its catalogues from a script: local images and
simulations, the DESI, EHT, JWST, collaborator and observatory remotes,
plan-and-approve downloads, a reprocessed data release, clones for
collaborators, and a lab tape archive behind a custom ``DataBackend``.

The phases share one world and run in order. The last test fails if any
public method, property or constructor of ``hallmark.__all__`` and the
documented backends was never called; a profiler records calls in every
thread, so work done in download and checksum workers counts.
"""

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest
import yaml

import hallmark
from hallmark import (
    CyVerseBackend, DataBackend, DownloadItem, DownloadPlan, HttpBackend, ParaFrame,
    RemoteEntry, Repo, SshBackend, register_backend,
)
from hallmark.error import CheckoutError, CloneError
from hallmark.remote import backends
from hallmark.remote.backends import (
    CapabilityError, DownloadError, OperationContext, RemoteConfigurationError,
    RemoteSpec, S3Backend, TransferCancelled, WebDavBackend,
)
from hallmark.transport.base import AccessRefused
from chaos import check_invariants
from e2e_world import (
    JWST_PREFIX, MIRROR_EXTRA, MOD, CallRecorder, add_lab_night, api_targets,
    build_world, make_lab_products, make_sims, retire_desi_file,
)
from survey import DESI_DARK, apply_dr_update, apply_silent_corruption, \
    make_telescope_tree

pytestmark = pytest.mark.e2e

SPECTRA_FMT = "{group:d}/{pixel:d}/{product}-main-dark-{hpx:d}.fits"
EXPOSURES_FMT = "{group:d}/{pixel:d}/hpixexp-main-dark-{hpx:d}.csv"
UVFITS_FMT = ("uvfits/{release}_{src}_{year:d}_{doy:03d}_{band}"
              "_hops_netcal_StokesI.uvfits")
JWST_FMT = "{target}/jw{program}-{obs}_{visit}_{instrument}_{optics}_x1dints.fits"
LAB_FMT = "{source}/{year}/{doy:03d}_{band}.fits"
REDROCK_23040 = "230/23040/redrock-main-dark-23040.fits"
RETIRED = "230/23041/qso_mgii-main-dark-23041.fits"
TAPE_URL = "https://tape.example.invalid/vault/"
_failed = []
_repos = {}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    Path(hallmark.__file__).resolve().relative_to(MOD)
    with build_world(tmp_path_factory.mktemp("e2e-api")) as world, \
            pytest.MonkeyPatch.context() as monkeypatch:
        world.apply_env(monkeypatch)
        monkeypatch.chdir(world.base / "work")
        # A private registry, so the tape backend registered here stays here.
        monkeypatch.setattr(backends, "_registered", dict(backends._registered))
        monkeypatch.setattr(backends, "_loaded", {})
        monkeypatch.setattr(backends.metadata, "entry_points", lambda: {})
        yield world


@pytest.fixture(scope="module")
def recorder(world):
    recorder = CallRecorder(api_targets()).start()
    yield recorder
    recorder.stop()


@pytest.fixture(autouse=True)
def in_order(request, recorder):
    """Skip the remaining phases once one fails."""
    if _failed:
        pytest.skip(f"an earlier phase failed: {_failed[0]}")
    yield
    report = getattr(request.node, "report_call", None)
    if report is not None and report.failed:
        _failed.append(request.node.name)


def work(world):
    return world.base / "work"


def identify(repo):
    repo.set_identity("Ada Lovelace", "ada@example.org")
    return repo


def catalogued(repo):
    """Every catalogued path: TSV rows and ``file:`` entries."""
    paths = set(repo.state.data.get("path", []))
    for frame in repo.state.tables.values():
        paths.update(frame.path)
    for section in ("data", "meta"):
        paths.update(entry["file"] for entry in repo.state.config.get(section) or []
                     if isinstance(entry, dict) and "file" in entry)
    return paths


def assert_payloads(world, repo, paths):
    expected = world.payloads()
    for path in paths:
        assert (repo.worktree / path).read_bytes() == expected[path], path


def assert_sound(path, **options):
    found = check_invariants(path, **options)
    assert not found, str(found)


def approved(repo, **selection):
    """Plan a download for ``selection`` and run it with approval."""
    return repo.download(repo.plan_download(**selection), approved=True)


def test_lab_catalogue(world):
    lab_path = work(world) / "lab"
    images = make_lab_products(lab_path)
    assert Repo.resolve_repo_paths(lab_path) == (lab_path / ".hm", lab_path)
    assert Repo.resolve_repo_paths(work(world) / "archive.hm")[1] is None
    identify(Repo.init(lab_path))
    lab = Repo(lab_path)
    assert lab.identity() == ("Ada Lovelace", "ada@example.org")
    assert lab.effective_identity() == ("Ada Lovelace", "ada@example.org")
    assert lab.worktree == lab_path and lab.download_result is None

    frame = lab.add(LAB_FMT)
    assert isinstance(frame, ParaFrame) and set(frame.path) == set(images)
    assert list(frame(source="SgrA").path) == ["SgrA/2017/097_lo.fits"]
    assert len(frame.filter(band="lo", doy="095")) == 4
    assert len(frame.filter(band="lo").filter(source="M87")) == 2
    status = lab.status()
    assert status["branch"] == "main" and set(status["staged"]["added"]) == set(images)
    assert status["untracked"] == ["M87/2017/README.txt"]
    assert lab.commit("Reduce the 2017 campaign") is True
    first = lab.status()["commit"]
    assert lab.commit("Nothing new") is False
    assert lab.commit("Pipeline eht-imaging 1.2", allow_empty=True) is True
    assert "Pipeline eht-imaging 1.2" in lab.log()

    night = add_lab_night(lab_path)
    (lab_path / "SgrA/2017/098_lo.fits").write_bytes(b"SIMPLE  =  T" + b" " * 2868)
    staged = lab.add_paths(["M87/2018/", "SgrA/2017/098_lo.fits"])
    assert set(staged.path) >= {*night, "SgrA/2017/098_lo.fits"}
    lab.restore_staged(["M87/2018/"])
    assert not set(night) & set(lab.status()["staged"]["added"])
    lab.restore_staged(["."])
    assert lab.status()["staged"]["added"] == []
    everything = lab.add(".")
    assert everything.attrs["unmatched"] == ["M87/2017/README.txt"]
    lab.commit("Add the 2018 night")

    parsed = ParaFrame.parse(LAB_FMT, base_path=lab_path)
    assert len(parsed) == 8 and set(parsed.year) == {2017, 2018}
    files, pattern = ParaFrame.glob_search(LAB_FMT, base_path=lab_path,
                                           return_pattern=True)
    assert len(files) == 8 and pattern
    sample = ParaFrame({"path": ["a.fits", "b.fits"], "band": ["lo", "hi"]},
                       base_path=lab_path)
    assert sample.base_path == lab_path and list(sample(band="hi").path) == ["b.fits"]

    assert lab.create_branch("recal") == "recal"
    assert lab.branches()["names"] == ["main", "recal"]
    assert lab.checkout("recal") is True and lab.checkout("recal") is False
    original = (lab_path / "M87/2017/095_lo.fits").read_bytes()
    (lab_path / "M87/2017/095_lo.fits").write_bytes(original[:-1] + b"!")
    lab.add(".")
    lab.commit("Recalibrate day 95")
    lab.checkout(first)
    assert lab.branches()["detached_at"].startswith(first)
    with pytest.raises(RuntimeError, match="not on a branch"):
        lab.commit("Lost", allow_empty=True)
    lab.checkout("main")
    (lab_path / "M87/2017/095_lo.fits").write_bytes(b"scribbled")
    with pytest.raises(CheckoutError, match="uncommitted changes"):
        lab.checkout("recal")
    (lab_path / "M87/2017/095_lo.fits").write_bytes(original)

    assert lab.add_worktree("recal") is True
    review = Repo(work(world) / "recal")
    assert review.branches()["current"] == "recal"
    assert (review.worktree / "M87/2017/095_lo.fits").read_bytes() != original
    a, b = lab_path / "M87/2017/095_lo.fits", lab_path / "M87/2017/095_hi.fits"
    sums = Repo.checksum_many([a, b, a])
    assert len(sums) == 2 and sums[a] == Repo.checksum(a)
    assert sums[a] == hashlib.sha1(original).hexdigest()
    assert lab.objects.contains(sums[a])
    assert Path(lab.dothm.path) == lab_path / ".hm"
    assert_sound(lab_path)


def test_simulation_library(world):
    sims_path = work(world) / "sims"
    make_sims(sims_path)
    sims = identify(Repo.init(sims_path))
    config = sims.set_config(fmt="a{aspin}_i{inc}.h5",
                             encoding_updates={"aspin": "m([0-9.]+)",
                                               "inc": "([0-9]+)"})
    assert config["data"][0]["fmt"] == "a{aspin}_i{inc}.h5"
    frame = sims.add("a{aspin}_i{inc}.h5", encoding=True)
    spins = dict(zip(frame.path, frame.aspin))
    assert spins["am0.5_i30.h5"] == -0.5 and spins["a0.94_i60.h5"] == 0.94
    assert sims.commit("Spin sweep")
    assert_sound(sims_path)


def test_survey_catalogue(world):
    survey = identify(Repo.init(work(world) / "survey"))
    _repos["survey"] = survey
    config = survey.set_config(remote_name="desi", remote_url=world.desi_url)
    assert config["remote"] == {"name": "desi", "url": world.desi_url}
    events = []
    rows = survey.add(world.desi_url, source="desi",
                      remote_fmt=[{"fmt": SPECTRA_FMT, "db": "spectra"}],
                      filter=["**/*.fits", "**/*.csv"], progress=events.append)
    assert len(rows) == 6 and events and events[-1]["matched"] == 6
    assert len(survey.state.tables["spectra.tsv"]) == 4

    eht = dict(backend="cyverse", remote="eht", source="m87", prefix="eht",
               remote_fmt=[{"fmt": UVFITS_FMT, "db": "uvfits"}])
    with pytest.raises(ValueError, match="already sorted"):
        survey.add(world.eht_url, **eht)
    survey.add(world.eht_url, resort=False, **eht)
    assert len(survey.state.tables["uvfits.tsv"]) == 8
    with pytest.raises(CapabilityError, match="cannot compute checksums"):
        survey.add(world.eht_url, source="m87", server_checksum="sha256")

    seen = []

    def keep_the_primary(duplicates):
        seen.append(duplicates)
        return "skip"

    mirrored = survey.add(world.mirror_url, remote="mirror", source="desi-extra",
                          filter=["**/*.fits", "**/*.csv"],
                          on_duplicate=keep_the_primary)
    assert seen and len(mirrored.attrs["skipped"]) == 6
    group, pixel = MIRROR_EXTRA
    assert f"{group}/{pixel}/redrock-main-dark-{pixel}.fits" in catalogued(survey)
    again = survey.add(world.mirror_url, source="desi-extra", on_duplicate="skip")
    assert len(again.attrs["skipped"]) == 6

    survey.add(world.dav_url, auth="collab", remote="collab", source="notes",
               prefix="collab")
    assert "collab/gains/2017/M87_095_gains.csv" in catalogued(survey)

    if world.s3 is not None:
        options = yaml.safe_load(world.s3["options"].read_text())
        survey.add(world.jwst_url, backend_options=options, auth="jwst",
                   remote="jwst", source="jwst", prefix="jwst",
                   remote_fmt=[{"fmt": JWST_FMT, "db": "jwst"}], resort=False)
        assert len(survey.state.tables["jwst.tsv"]) == 4
        survey.set_config(remote_name="jwst", remote_backend="s3",
                          remote_backend_options={**options, "page_size": 3})
        before = survey.state.tables["jwst.tsv"].copy()
        survey.add(world.jwst_url, source="jwst")
        assert survey.state.tables["jwst.tsv"].equals(before)

    if world.ssh is not None:
        survey.add(world.ssh["url"], server_checksum="sha256", auth="obs",
                   remote="obs", source="alma", prefix="obs", filter="ALMA/**")
        assert "obs/ALMA/2024/95.fits" in catalogued(survey)

    survey.set_config(remote_name="eht", remote_backend="cyverse")
    assert survey.status()["remote_catalog"] is True
    assert survey.commit("Catalogue DESI, EHT, JWST and the shares")
    assert_sound(survey.worktree)


def test_downloads(world):
    survey = _repos["survey"]
    plan = survey.plan_download(all_files=True, estimated_bytes_per_second=5e6)
    assert isinstance(plan, DownloadPlan) and plan.has_changes
    assert plan.file_count == len(catalogued(survey))
    assert plan.outdated_items == () or not plan.outdated_items
    assert plan.known_bytes > 0
    assert plan.unknown_size_count >= 1 and plan.total_bytes is None
    assert plan.estimated_seconds is None
    sized = survey.plan_download(sources=["desi"], estimated_bytes_per_second=5e6)
    assert sized.unknown_size_count == 0 and sized.total_bytes == sized.known_bytes
    assert sized.estimated_seconds == pytest.approx(sized.known_bytes / 5e6)
    assert "file" in plan.summary()
    item = next(item for item in plan.items if item.relative_path.parts[0] == "eht")
    assert isinstance(item, DownloadItem)
    assert Path(item.source_path) == item.relative_path.relative_to("eht")
    with pytest.raises(DownloadError, match="approv"):
        survey.download(plan)

    mirrored = approved(survey, sources=["desi"], remote_name="mirror",
                        filter="230/23040/*")
    assert mirrored["succeeded"] == 3 and mirrored["failed"] == 0
    plan = survey.plan_download(sources=["desi", "desi-extra"])
    result = survey.download(plan, approved=True, max_workers=2, progress=True)
    assert result["failed"] == 0 and survey.download_result is result
    assert approved(survey, tsv_names=["uvfits"])["succeeded"] == 8
    csv = "eht/csv/SR1_M87_2017_{doy:03d}_{band}_hops_netcal_StokesI.csv"
    assert approved(survey, fmt=csv)["succeeded"] == 8
    approved(survey, file_paths=["eht/README.md"])

    survey.set_config(remote_name="collab", remote_auth="")
    refused = approved(survey, sources=["notes"])
    assert refused["failed"] == 3 and refused["access_refused"] == {"collab": None}
    survey.set_config(remote_name="collab", remote_auth="collab")
    shared = work(world) / "for-alice"
    result = approved(survey, output_path=shared, filter="collab/gains/**")
    assert result["succeeded"] == 2
    assert (shared / "collab/gains/2017/M87_096_gains.csv").exists()
    approved(survey, sources=["notes"])
    if world.s3 is not None:
        assert approved(survey, sources=["jwst"])["failed"] == 0
    if world.ssh is not None:
        assert approved(survey, sources=["alma"])["failed"] == 0
    assert not survey.plan_download(all_files=True).has_changes
    assert_payloads(world, survey, catalogued(survey))
    assert survey.commit("Record the downloaded SHA-1s")
    assert_sound(survey.worktree)


def test_reprocessed_release(world):
    survey = _repos["survey"]
    survey.create_branch("iron-v1")
    apply_dr_update(world.desi.root, world.desi_files)
    retire_desi_file(world.desi.root, world.desi_files, f"{DESI_DARK}/{RETIRED}")

    kept = survey.add(world.desi_url, source="desi", prune=False)
    assert kept.attrs["kept_missing"] == [RETIRED]
    assert REDROCK_23040 in kept.attrs["changed"]
    asked = []
    survey.add(world.desi_url, source="desi",
               prune=lambda paths: asked.append(list(paths)) or False)
    assert asked == [[RETIRED]] and RETIRED in catalogued(survey)
    pruned = survey.add(world.desi_url, source="desi", prune=True)
    assert pruned.attrs["removed"] == [RETIRED]
    result = approved(survey, sources=["desi"])
    assert result["replaced"] == [REDROCK_23040]
    assert survey.commit("Absorb the iron reprocessing")

    survey.checkout("iron-v1")
    assert survey.outdated_downloads() == ([REDROCK_23040], 1)
    plan = survey.plan_download(sources=["desi"])
    assert [item.relative_path.as_posix() for item in plan.outdated_items] == [
        REDROCK_23040]
    assert approved(survey, sources=["desi"])["from_store"] == [REDROCK_23040]
    survey.checkout("main")
    assert approved(survey, sources=["desi"])["from_store"] == [REDROCK_23040]
    assert_payloads(world, survey, [REDROCK_23040])

    asked = []
    survey.add(world.desi_url, source="desi",
               remote_fmt=[{"fmt": EXPOSURES_FMT, "db": "exposures"}],
               resort=lambda count, others: asked.append((count, others)) or "all")
    # Sorted rows of this source and of the others: uvfits, the mirror, JWST.
    assert asked == [(5, 10 + 4 * (world.s3 is not None))]
    exposures = survey.state.tables["exposures.tsv"]
    assert set(exposures.catalog_source) == {"desi", "desi-extra"}
    assert len(exposures) == 4
    assert survey.commit("Sort the exposure tables")
    assert_sound(survey.worktree)


def test_collaborators(world):
    base = work(world)
    survey = _repos["survey"]
    source = str(survey.worktree / ".hm")
    world.publish(survey.worktree)

    copy = Repo.clone(source, base / "copy", download=False)
    assert copy.download_result is None and catalogued(copy) == catalogued(survey)
    Repo.clone(source, base / "git-copy", source_type="git", download=False)
    sizes = []
    eht_only = Repo.clone(
        source, base / "eht-only", filter="eht/**", max_workers=2, progress=False,
        approve=lambda plan: sizes.append(plan.file_count) or True)
    assert sizes == [17] and eht_only.download_result["failed"] == 0
    uvfits = Repo.clone(
        source, base / "uvfits-only", approved=True,
        fmt="eht/uvfits/SR1_M87_2017_{doy:03d}_{band}_hops_netcal_StokesI.uvfits")
    assert uvfits.download_result["succeeded"] == 8
    later = Repo.clone(source, base / "later", approve=lambda plan: False)
    assert later.download_result is None
    with pytest.raises(ValueError):
        Repo.clone(source, base / "unused", filter="eht/**", download=False)
    with pytest.raises((DownloadError, ValueError), match="download"):
        Repo.clone(source, base / "archive.hm")
    bare = Repo.clone(source, base / "archive.hm", download=False)
    assert bare.worktree is None
    with pytest.raises(DownloadError, match="output"):
        bare.plan_download(all_files=True)

    snapshot = Repo.clone(world.snapshot_url, base / "snapshot",
                          source_type="catalog", download=False)
    assert catalogued(snapshot) == catalogued(survey)
    with pytest.raises(CloneError) as refused:
        Repo.clone(world.private_url, base / "private", source_type="catalog",
                   download=False)
    assert isinstance(refused.value.__cause__, AccessRefused)
    private = Repo.clone(world.private_url, base / "private", source_type="catalog",
                         auth="group", download=False)
    assert catalogued(private) == catalogued(survey)
    with pytest.raises(ValueError, match="auth profiles"):
        Repo.clone(source, base / "git-auth", auth="group", download=False)

    apply_dr_update(world.mirror.root, world.mirror_files)
    retire_desi_file(world.mirror.root, world.mirror_files, f"{DESI_DARK}/{RETIRED}")
    corrupted = "230/23041/redrock-main-dark-23041.fits"
    apply_silent_corruption(world.mirror.root, f"{DESI_DARK}/{corrupted}")
    result = approved(later, sources=["desi"], remote_name="mirror")
    assert result["failed"] == 1 and "Checksum mismatch" in result["errors"][0]
    assert not (later.worktree / corrupted).exists()
    for repo in (copy, later, bare, snapshot, private):
        assert_sound(repo.dothm.path if repo.worktree is None else repo.worktree,
                     objects=False)
    assert_sound(eht_only.worktree)


class TapeArchive(DataBackend):
    """The lab's tape vault, read through a mounted folder named in options."""

    def _vault(self):
        return Path(self.context.remote.backend_options["vault"])

    def iter_entries(self, on_directory=None):
        vault = self._vault()
        for folder in sorted(path for path in vault.rglob("*") if path.is_dir()):
            if on_directory is not None:
                on_directory(folder.relative_to(vault).as_posix())
        for path in sorted(vault.rglob("*.fits")):
            yield RemoteEntry(path.relative_to(vault).as_posix(),
                              size=path.stat().st_size, mtime=int(path.stat().st_mtime))

    def complete_entries(self, entries):
        vault = self._vault()
        return [replace(entry, checksum_algorithm="sha256", checksum_source="server",
                        checksum=hashlib.sha256(
                            (vault / entry.path).read_bytes()).hexdigest())
                for entry in super().complete_entries(entries)]

    def fetch(self, relative_path, destination, *, chunk_size=8192):
        self.context.check_cancelled()
        data = (self._vault() / relative_path).read_bytes()
        Path(destination).write_bytes(data)
        if self.context.on_bytes is not None:
            self.context.on_bytes(len(data))


def test_custom_backend(world):
    vault = world.base / "vault"
    files = make_telescope_tree(vault, sites=("APEX", "JCMT"))
    register_backend("tape", TapeArchive)
    with pytest.raises(RemoteConfigurationError, match="already registered"):
        register_backend("tape", TapeArchive)
    with pytest.raises(RemoteConfigurationError, match="DataBackend subclass"):
        register_backend("tape-raw", DataBackend)

    tape = identify(Repo.init(work(world) / "tape"))
    tape.add(TAPE_URL, backend="tape", backend_options={"vault": str(vault)},
             remote_fmt=[{"fmt": "{site}/{year:d}/{day:d}.fits", "db": "nights"}])
    nights = tape.state.tables["nights.tsv"]
    assert len(nights) == len(files) and set(nights.checksum_source) == {"server"}
    result = tape.download(tape.plan_download(all_files=True), approved=True,
                           max_workers=3)
    assert result["succeeded"] == len(files)
    assert tape.commit("Catalogue the tape vault")
    copy = Repo.clone(str(tape.worktree / ".hm"), work(world) / "tape-copy",
                      approved=True)
    for path, data in files.items():
        assert (copy.worktree / path).read_bytes() == data

    remote = RemoteSpec.from_url(TAPE_URL, backend="tape",
                                 backend_options={"vault": str(vault)})
    with OperationContext(remote) as context:
        assert isinstance(context.transport, TapeArchive)
        plain = DataBackend(context)
        with pytest.raises(CapabilityError):
            list(plain.iter_entries())
        with pytest.raises(CapabilityError):
            plain.read_text("README", 10)
        with pytest.raises(CapabilityError):
            plain.server_checksums(["APEX/2024/95.fits"], "sha256")
        with pytest.raises(NotImplementedError):
            plain.fetch("APEX/2024/95.fits", world.base / "unused")
        entry = RemoteEntry("APEX/2024/95.fits", size=2880)
        assert plain.complete_entries([entry]) == [entry]
        plain.prepare()
        plain.cancel()
        plain.close()
    assert_sound(tape.worktree)


def test_transports_directly(world):
    spec = RemoteSpec.from_url(world.desi_url)
    assert spec.display == f"http://127.0.0.1:{world.desi.port}"
    assert spec.file_url("230/23040/x.fits") == world.desi_url + "230/23040/x.fits"
    assert spec.file_path("230/x.fits").endswith("/dark/230/x.fits")
    pixel = spec.subpath("230/23040/")
    with OperationContext(pixel) as context:
        assert type(context.transport) is HttpBackend
        manifest = context.read_text("redux_iron_healpix_main_dark_230_23040.sha256sum")
        assert "redrock-main-dark-23040.fits" in manifest
        target = world.base / "redrock.fits"
        context.transport.fetch("redrock-main-dark-23040.fits", target)
        assert target.read_bytes() == world.payloads()[REDROCK_23040]
        assert context.session() is context.session()
        context.check_cancelled()
        context.cancel()
        with pytest.raises(TransferCancelled):
            context.check_cancelled()

    with OperationContext(RemoteSpec.from_url(world.eht_url,
                                              backend="cyverse")) as context:
        assert isinstance(context.transport, CyVerseBackend)
        names = [entry.path for entry in context.transport.iter_entries()]
        assert "README.md" in names
    with OperationContext(RemoteSpec.from_url(world.dav.url, auth="collab",
                                              backend="webdav")) as context:
        assert isinstance(context.transport, WebDavBackend)
    with OperationContext(RemoteSpec.from_url("ssh://hm-test/srv/data/")) as context:
        assert isinstance(context.transport, SshBackend)
        context.cancel()

    if world.s3 is not None:
        options = yaml.safe_load(world.s3["options"].read_text())
        jwst = RemoteSpec.from_url(world.jwst_url, auth="jwst",
                                   backend_options=options)
        key = next(key for key in world.s3["files"] if key.endswith(".ecsv"))
        with OperationContext(jwst) as context:
            assert isinstance(context.transport, S3Backend)
            assert context.read_text(key) == world.s3["files"][key].decode()
            assert jwst.file_path(key).endswith(JWST_PREFIX + key)
    if world.ssh is not None:
        export = RemoteSpec.from_url(world.ssh["url"], auth="obs")
        night = "ALMA/2024/95.fits"
        with OperationContext(export) as context:
            context.transport.prepare()
            assert context.transport.stat(night).size == 2880
            sums = context.transport.server_checksums([night], "sha256")
            assert sums[night] == hashlib.sha256(world.ssh["files"][night]).hexdigest()
            (world.ssh["root"] / "README.txt").write_text("ALMA and SMA nights\n")
            assert context.read_text("README.txt") == "ALMA and SMA nights\n"


def test_every_public_api_is_called(world, recorder):
    exclude = set()
    if world.ssh is None:
        exclude |= {f"SshBackend.{name}" for name in (
            "prepare", "server_checksums", "fetch", "read_text", "stat",
            "iter_entries")}
    if world.s3 is None:
        exclude |= {label for label in recorder.targets.values()
                    if label.startswith("S3Backend.")}
    missing = recorder.missing(exclude)
    assert not missing, f"never called: {missing}"
