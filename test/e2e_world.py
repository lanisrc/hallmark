"""A small synthetic research world for the end-to-end scenario tests.

``build_world`` serves releases laid out like the real archives a group
would combine, each scaled down to a few kilobytes:

* DESI DR1 healpix spectra over HTTP with ``*.sha256sum`` manifests, plus a
  mirror holding the same files and one extra pixel;
* the EHT M87 2017 release on a CyVerse-style listing, without a manifest;
* a collaborator's calibration share over WebDAV, behind Basic auth;
* JWST program 1366 products in an S3 bucket (moto's server, optional);
* an observatory export over loopback SFTP (optional);
* two servers for published catalogue snapshots, one public, one private.

It also provides the environment a fresh machine would have, an ``hm``
runner that starts the real entry point in a subprocess, and the two
coverage gates: every CLI option used, every public API entry point called.
"""

from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import getpass
import inspect
import logging
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading

import click
import yaml

from http_server import DatasetHTTPServer, LocalServer, dav_handler, write_file
from survey import (
    DESI_DARK, EHT_UVFITS, _fits_like, _write_manifest, make_desi_like_release,
    make_eht_like_release, make_telescope_tree,
)
from sshd import SshdError, sshd_binary, start_sshd

MOD = Path(__file__).resolve().parents[1] / "mod"
ENTRY = "from hallmark.cli import hallmark; hallmark(prog_name='hm')"

DESI_PIXELS = ((230, 23040), (230, 23041))
MIRROR_EXTRA = (230, 23043)
EHT_RELEASE = EHT_UVFITS.split("/")[0]
JWST_BUCKET = "stpubdata"
JWST_PREFIX = "jwst/public/jw01366/L3/t/"
JWST_TARGET = "WASP-39"
JWST_OBSERVATIONS = (
    ("o001", "niriss_clear-gr700xd-substrip256"),
    ("o002", "nircam_f322w2-grismr-subgrism256"),
    ("o003", "nirspec_f290lp-g395h-s1600a1-sub2048"),
    ("o004", "nirspec_clear-prism-s1600a1-sub512"),
)
COLLAB_USER = "alice"
COLLAB_PASSWORD = "calibrate-2017"
GROUP_TOKEN = "group-token-1366"


def basic(user, password):
    """An HTTP Basic ``Authorization`` header value."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# Data ---------------------------------------------------------------------

def make_eht_release(root, seed=0):
    """The EHT M87 2017 release: UVFITS and CSV per day and band, a README.

    Returns:
        dict: Release-relative path to contents.
    """
    files = make_eht_like_release(root, seed=seed)
    rng = random.Random(seed + 100)
    for path in list(files):
        name = path.rsplit("/", 1)[1].replace(".uvfits", ".csv")
        csv = f"{EHT_RELEASE}/csv/{name}"
        files[csv] = (f"time,u,v,vis\n{rng.random():.6f},1,2,3\n").encode()
        write_file(root, csv, files[csv])
    readme = f"{EHT_RELEASE}/README.md"
    files[readme] = b"# EHT M87 2017 first results\n"
    write_file(root, readme, files[readme])
    return files


def make_jwst_program(seed=0):
    """JWST program 1366 level-3 products for one target.

    Returns:
        dict: Key beneath ``JWST_PREFIX`` to contents.
    """
    rng = random.Random(seed)
    files = {}
    for observation, mode in JWST_OBSERVATIONS:
        stem = f"{JWST_TARGET}/jw01366-{observation}_t001_{mode}"
        files[f"{stem}_x1dints.fits"] = _fits_like(rng, 2880 + rng.randrange(2880))
        files[f"{stem}_whtlt.ecsv"] = (
            f"# %ECSV 1.0\nint_mid_MJD_UTC whitelight_flux\n"
            f"59791.{rng.randrange(10**6)} {rng.random():.6f}\n").encode()
    return files


def make_lab_products(root, seed=0):
    """Reduced images laid out as ``{source}/{year}/{doy:03d}_{band}.fits``.

    Returns:
        dict: Worktree-relative path to contents, without ``README.txt``.
    """
    rng = random.Random(seed)
    files = {}
    for source, year, doy, band in (
            ("M87", 2017, 95, "lo"), ("M87", 2017, 95, "hi"),
            ("M87", 2017, 96, "lo"), ("M87", 2017, 96, "hi"),
            ("SgrA", 2017, 97, "lo")):
        path = f"{source}/{year}/{doy:03d}_{band}.fits"
        files[path] = _fits_like(rng, 2880)
        write_file(root, path, files[path])
    write_file(root, "M87/2017/README.txt", "Reduced with eht-imaging 1.2\n")
    return files


def add_lab_night(root, seed=1):
    """Write the 2018 M87 night that arrives after the first commit."""
    rng = random.Random(seed)
    files = {}
    for band in ("lo", "hi"):
        path = f"M87/2018/111_{band}.fits"
        files[path] = _fits_like(rng, 2880)
        write_file(root, path, files[path])
    return files


SIM_NAMES = ("a0_i30.h5", "a0.5_i30.h5", "am0.5_i30.h5", "a0.94_i60.h5",
             "am0.94_i60.h5")


def make_sims(root, seed=0):
    """GRMHD snapshots named ``a{spin}_i{inc}.h5``; ``m`` marks negative spin."""
    rng = random.Random(seed)
    for name in SIM_NAMES:
        write_file(root, name, b"\x89HDF\r\n\x1a\n" + rng.randbytes(256))


def retire_desi_file(root, files, path):
    """Withdraw one file from a DESI-like release and rewrite its manifest."""
    (Path(root) / path).unlink()
    del files[path]
    directory = path.rsplit("/", 1)[0]
    group, pixel = directory.split("/")[-2:]
    names = sorted(other.rsplit("/", 1)[1] for other in files
                   if other.rsplit("/", 1)[0] == directory)
    _write_manifest(root, directory, names, files,
                    f"redux_iron_healpix_main_dark_{group}_{pixel}.sha256sum")


def calibration_notes():
    """The collaborator's share: gain tables and a README."""
    return {
        "gains/2017/M87_095_gains.csv": b"station,gain\nALMA,1.02\nSMA,0.97\n",
        "gains/2017/M87_096_gains.csv": b"station,gain\nALMA,1.01\nSMA,0.99\n",
        "README.txt": b"Gain tables for the 2017 campaign.\n",
    }


# Environment and runner ---------------------------------------------------

@dataclass
class Result:
    """A finished ``hm`` run."""
    argv: tuple
    returncode: int
    stdout: str
    stderr: str

    @property
    def text(self):
        """Standard output and standard error together."""
        return self.stdout + self.stderr


class Hm:
    """Run the real ``hm`` entry point in a subprocess and record each call."""

    def __init__(self, env):
        self.env = env
        self.calls = []

    def __call__(self, *args, cwd, input="", expect=0):
        """Run ``hm ARGS`` in ``cwd`` with ``input`` on stdin.

        Args:
            cwd: Directory to run in.
            input (str): Text for prompts; stdin is always a pipe.
            expect (int | None): Required exit code, or None to skip the check.

        Returns:
            Result: The finished run.
        """
        argv = tuple(str(arg) for arg in args)
        process = subprocess.run(
            [sys.executable, "-c", ENTRY, *argv], cwd=str(cwd), env=self.env,
            input=input, capture_output=True, text=True, timeout=120)
        result = Result(argv, process.returncode, process.stdout, process.stderr)
        self.calls.append((argv, process.returncode))
        if expect is not None and process.returncode != expect:
            raise AssertionError(
                f"hm {' '.join(argv)} (in {cwd}) exited {process.returncode}, "
                f"expected {expect}:\n{result.text}")
        return result


@dataclass(repr=False)
class World:
    """Servers, credentials and the environment of one scenario run."""
    base: Path
    env: dict
    hm: Hm
    desi: DatasetHTTPServer
    desi_files: dict
    mirror: DatasetHTTPServer
    mirror_files: dict
    eht: DatasetHTTPServer
    eht_files: dict
    dav: LocalServer
    snapshots: DatasetHTTPServer
    private: LocalServer
    auth_file: Path
    s3: dict | None = None
    ssh: dict | None = None
    skipped: dict = field(default_factory=dict)

    def __repr__(self):
        return f"World({str(self.base)!r}, skipped={sorted(self.skipped)})"

    @property
    def desi_url(self):
        return self.desi.url(DESI_DARK + "/")

    @property
    def mirror_url(self):
        return self.mirror.url(DESI_DARK + "/")

    @property
    def eht_url(self):
        return self.eht.url(EHT_RELEASE + "/")

    @property
    def dav_url(self):
        return self.dav.url.replace("http://", "webdav://")

    @property
    def snapshot_url(self):
        return self.snapshots.url("survey/")

    @property
    def private_url(self):
        return self.private.url + "survey/"

    @property
    def jwst_url(self):
        return f"s3://{JWST_BUCKET}/{JWST_PREFIX}"

    def payloads(self):
        """What each catalogue path should hold now, read from the servers.

        DESI files are keyed as the primary archive serves them (the
        mirror's for paths only it has), EHT files
        under ``eht/``, the collaborator's under ``collab/``, JWST under
        ``jwst/`` and the observatory's under ``obs/``.
        """
        found = {}
        for root in (self.mirror.root, self.desi.root):
            desi = root / DESI_DARK
            for path in desi.rglob("*"):
                if path.is_file() and not path.name.endswith(".sha256sum"):
                    found[path.relative_to(desi).as_posix()] = path.read_bytes()
        eht = self.eht.root / EHT_RELEASE
        for path in eht.rglob("*"):
            if path.is_file():
                found["eht/" + path.relative_to(eht).as_posix()] = path.read_bytes()
        lanes = [("collab/", self.dav.files)]
        if self.s3 is not None:
            lanes.append(("jwst/", self.s3["files"]))
        if self.ssh is not None:
            lanes.append(("obs/", self.ssh["files"]))
        for prefix, files in lanes:
            found.update({prefix + key: data for key, data in files.items()})
        return found

    def publish(self, repo_path):
        """Publish a repository's committed catalogue to both snapshot servers."""
        dothm = Path(repo_path) / ".hm"
        names = subprocess.run(
            ["git", "-C", str(dothm), "ls-tree", "--name-only", "HEAD"],
            check=True, capture_output=True, text=True).stdout.split()
        for name in names:
            if name == "config.yml" or name == "meta.yml" or name.endswith(".tsv"):
                data = subprocess.run(
                    ["git", "-C", str(dothm), "show", f"HEAD:{name}"],
                    check=True, capture_output=True).stdout
                write_file(self.snapshots.root, f"survey/{name}", data)
                self.private.files[f"survey/{name}"] = data
        return names

    def apply_env(self, monkeypatch):
        """Use this world's environment in the current process."""
        for name in list(os.environ):
            if name.startswith(("GIT_", "AWS_")) or name in {
                    "HALLMARK_AUTH_FILE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"}:
                monkeypatch.delenv(name, raising=False)
        for name, value in self.env.items():
            if name not in {"PYTHONPATH"}:
                monkeypatch.setenv(name, value)


def _shim(path, binary, config):
    path.write_text(f'#!/bin/sh\nexec {binary} -F "{config}" "$@"\n')
    path.chmod(0o755)


def _clean_environment(base, bin_dir, home):
    env = {name: value for name, value in os.environ.items()
           if name in {"SYSTEMROOT", "TERM", "COLUMNS"}}
    user = getpass.getuser()
    env.update(
        PATH=os.pathsep.join([str(bin_dir), os.environ.get("PATH", os.defpath)]),
        HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
        NETRC=str(base / "no-netrc"), PYTHONPATH=str(MOD),
        LC_ALL="C.UTF-8", LANG="C.UTF-8", USER=user, LOGNAME=user,
        NO_PROXY="*", no_proxy="*", TMPDIR=str(base / "tmp"),
        AWS_CONFIG_FILE=str(base / "no-aws-config"),
        AWS_SHARED_CREDENTIALS_FILE=str(base / "no-aws-credentials"),
        AWS_EC2_METADATA_DISABLED="true",
        COLLAB_PASSWORD=COLLAB_PASSWORD,
        JWST_ACCESS_KEY_ID="testing",
    )
    return env


def _start_s3(stack, files):
    """Serve ``files`` from moto's S3 server, or return the reason it can't."""
    try:
        import boto3
        from moto.server import ThreadedMotoServer
    except ImportError as exc:
        return None, f"S3 lane needs boto3 and moto[server]: {exc}"
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    stack.callback(server.stop)
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    client = boto3.client(
        "s3", endpoint_url=endpoint, region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing")
    client.create_bucket(Bucket=JWST_BUCKET)
    for key, data in files.items():
        extra = {"ChecksumAlgorithm": "SHA256"} if key.endswith(".fits") else {}
        client.put_object(Bucket=JWST_BUCKET, Key=JWST_PREFIX + key, Body=data,
                          **extra)
    return {"endpoint": endpoint, "client": client, "files": files}, None


@contextmanager
def build_world(base, *, ssh=None, s3=True):
    """Start every server of the scenario under ``base``.

    Args:
        base (Path): Empty folder for data, servers and repositories.
        ssh (bool, optional): Start the SFTP lane; by default only with
            ``HALLMARK_RUN_SSH_TESTS=1``.
        s3 (bool): Start the S3 lane when boto3 and moto's server exist.

    Yields:
        World: The running world. Lanes that could not start are None, with
        the reason in ``skipped``.
    """
    base = Path(base)
    if ssh is None:
        ssh = os.environ.get("HALLMARK_RUN_SSH_TESTS") == "1"
    for name in ("home", "tmp", "bin", "work"):
        (base / name).mkdir(parents=True, exist_ok=True)
    env = _clean_environment(base, base / "bin", base / "home")
    skipped = {}
    with ExitStack() as stack:
        desi_files = make_desi_like_release(base / "desi", pixels=DESI_PIXELS)
        desi = stack.enter_context(DatasetHTTPServer(base / "desi", "apache"))
        mirror_files = make_desi_like_release(
            base / "mirror", pixels=DESI_PIXELS + (MIRROR_EXTRA,))
        mirror = stack.enter_context(DatasetHTTPServer(base / "mirror", "python"))
        eht_files = make_eht_release(base / "eht")
        eht = stack.enter_context(DatasetHTTPServer(base / "eht", "cyverse"))
        dav = LocalServer()
        stack.callback(dav.close)
        dav.files.update(calibration_notes())
        dav.authorization = basic(COLLAB_USER, COLLAB_PASSWORD)
        dav.handlers["PROPFIND"] = dav_handler(dav)
        snapshots = stack.enter_context(
            DatasetHTTPServer(base / "snapshots", "python"))
        private = LocalServer()
        stack.callback(private.close)
        private.authorization = f"Bearer {GROUP_TOKEN}"

        profiles = {
            "collab": {"type": "http", "hosts": ["127.0.0.1"], "scheme": "basic",
                       "username": COLLAB_USER,
                       "password": {"env": "COLLAB_PASSWORD"}},
            "group": {"type": "http", "hosts": ["127.0.0.1"], "scheme": "bearer",
                      "token": {"command": [sys.executable, "-c",
                                            f"print({GROUP_TOKEN!r})"]}},
        }
        s3_lane = None
        if s3:
            s3_lane, reason = _start_s3(stack, make_jwst_program())
            if reason:
                skipped["s3"] = reason
        else:
            skipped["s3"] = "S3 lane disabled"
        if s3_lane is not None:
            profiles["jwst"] = {
                "type": "s3", "buckets": [JWST_BUCKET],
                "endpoints": [s3_lane["endpoint"]],
                "access_key_id": {"env": "JWST_ACCESS_KEY_ID"},
                "secret_access_key": {"command": [sys.executable, "-c",
                                                  "print('testing')"]}}
            options = base / "work" / "s3.yml"
            options.write_text(yaml.safe_dump({
                "endpoint_url": s3_lane["endpoint"], "region": "us-east-1",
                "addressing_style": "path"}))
            s3_lane["options"] = options

        ssh_lane = None
        if not ssh:
            skipped["ssh"] = "Set HALLMARK_RUN_SSH_TESTS=1 for the SFTP lane"
        elif sshd_binary() is None:
            skipped["ssh"] = "The SFTP lane needs openssh-server"
        else:
            (base / "sshd").mkdir()
            try:
                ssh_lane = stack.enter_context(start_sshd(base / "sshd"))
            except SshdError as exc:
                skipped["ssh"] = f"sshd did not start: {exc}"
        if ssh_lane is not None:
            ssh_lane["files"] = make_telescope_tree(ssh_lane["root"])
            for name in ("ssh", "sftp"):
                binary = shutil.which(name)
                _shim(base / "bin" / name, binary, ssh_lane["config"])
            profiles["obs"] = {
                "type": "ssh", "hosts": ["hm-test"],
                "identity_file": str(ssh_lane["base"] / "client"),
                "host_key_policy": "strict", "max_sessions": 2}

        auth_file = base / "home" / "auth.yml"
        auth_file.write_text(yaml.safe_dump({"version": 2, "profiles": profiles}))
        auth_file.chmod(0o600)
        env["HALLMARK_AUTH_FILE"] = str(auth_file)
        yield World(
            base=base, env=env, hm=Hm(env), desi=desi, desi_files=desi_files,
            mirror=mirror, mirror_files=mirror_files, eht=eht,
            eht_files=eht_files, dav=dav, snapshots=snapshots, private=private,
            auth_file=auth_file, s3=s3_lane, ssh=ssh_lane, skipped=skipped)


# Coverage gates -----------------------------------------------------------

def cli_option_table(command):
    """Every spelling of every option of a Click command."""
    return {name: param for param in command.params
            if isinstance(param, click.Option)
            for name in (*param.opts, *param.secondary_opts)}


def cli_requirements(group):
    """Each command, and each (command, option) spelling, the CLI offers.

    Group options are listed under the command name ``""``.
    """
    required = {("", name) for name in cli_option_table(group)}
    for name, command in group.commands.items():
        required.add((name, None))
        required.update((name, option) for option in cli_option_table(command))
    return required


def cli_hits(calls, group):
    """The (command, option) pairs used by recorded calls.

    Returns:
        tuple[set, set]: Pairs used by any call, and pairs used by calls that
        exited 0.
    """
    used, succeeded = set(), set()
    for argv, returncode in calls:
        hits = set()
        tokens = iter(argv)
        command = None
        group_table = cli_option_table(group)
        for token in tokens:
            if token in group.commands:
                command = token
                break
            if token.split("=", 1)[0] in group_table:
                hits.add(("", token.split("=", 1)[0]))
        if command is not None:
            hits.add((command, None))
            table = cli_option_table(group.commands[command])
            for token in tokens:
                if token == "--":
                    break
                if not token.startswith("-"):
                    continue
                name = token.split("=", 1)[0] if token.startswith("--") else token[:2]
                param = table.get(name)
                if param is None:
                    continue
                hits.add((command, name))
                if not param.is_flag and token == name:
                    next(tokens, None)
        used |= hits
        if returncode == 0:
            succeeded |= hits
    return used, succeeded


API_DUNDERS = {"__init__", "__call__", "__post_init__", "__enter__", "__exit__"}


def api_targets():
    """Public callables of the API, keyed by code object.

    Covers the classes in ``hallmark.__all__`` and the documented backends in
    ``hallmark.remote.backends``: their public methods, properties,
    class and static methods, and the listed dunders, including those
    inherited from other Hallmark classes. Code generated by dataclasses and
    code from pandas, GitPython or pathlib is left out.

    Returns:
        dict: Code object to a ``Class.method`` label.
    """
    import hallmark
    from hallmark.remote.backends import (
        OperationContext, RemoteSpec, S3Backend, WebDavBackend)

    package = Path(hallmark.__file__).resolve().parent
    exported = [getattr(hallmark, name) for name in hallmark.__all__]
    classes = [item for item in exported if inspect.isclass(item)]
    classes += [WebDavBackend, S3Backend, OperationContext, RemoteSpec]
    functions = [item for item in exported if inspect.isfunction(item)]
    targets = {}

    def keep(function, label):
        function = inspect.unwrap(function)
        code = getattr(function, "__code__", None)
        if code is None:
            return
        try:
            Path(code.co_filename).resolve().relative_to(package)
        except ValueError:
            return
        targets.setdefault(code, label)

    for function in functions:
        keep(function, function.__name__)
    for cls in classes:
        for owner in cls.__mro__:
            if not owner.__module__.startswith("hallmark."):
                continue
            for name, value in vars(owner).items():
                if name.startswith("_") and name not in API_DUNDERS:
                    continue
                label = f"{owner.__name__}.{name}"
                if isinstance(value, (staticmethod, classmethod)):
                    keep(value.__func__, label)
                elif isinstance(value, property):
                    for accessor in (value.fget, value.fset, value.fdel):
                        if accessor is not None:
                            keep(accessor, label)
                elif inspect.isfunction(value):
                    keep(value, label)
    return targets


class CallRecorder:
    """Record which target code objects run, in every thread.

    ``sys.setprofile`` sees calls in this thread; ``threading.setprofile``
    reaches the worker threads that downloads and checksums start.
    """

    def __init__(self, targets):
        self.targets = targets
        self.hits = set()

    def _profile(self, frame, event, arg):
        if event == "call" and frame.f_code in self.targets:
            self.hits.add(frame.f_code)

    def start(self):
        threading.setprofile(self._profile)
        sys.setprofile(self._profile)
        return self

    def stop(self):
        sys.setprofile(None)
        threading.setprofile(None)

    def missing(self, exclude=()):
        """Labels of targets never called, except those in ``exclude``."""
        return sorted(label for code, label in self.targets.items()
                      if code not in self.hits and label not in exclude)
