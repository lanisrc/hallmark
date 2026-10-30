"""Run the shipped demo notebooks with a disposable HTTP dataset instead of SSH."""

from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import site
import subprocess
import sys
from threading import Thread

import pytest


DEMO_DIR = Path(__file__).resolve().parents[1] / "demo"
DEMO_URL = "ssh://hallmark-demo/srv/hallmark_demo_data"
_CHECKS_BEFORE_CLEANUP = {
    "demo_python.ipynb": """
assert plan.file_count == 2
for downloaded_repo in (eht, full_copy):
    for source in ("M87", "SgrA"):
        for day in (1, 2):
            name = f"{source}_{day:03d}.txt"
            assert (downloaded_repo.worktree / name).read_text() == (
                f"demo data for {name}\\n")
assert full_copy.download_result["failed"] == 0
assert later_copy.download_result is None
for metadata_only_repo in (catalog_only, later_copy):
    assert not list(metadata_only_repo.worktree.glob("*.txt"))
""",
    # S3 adds two files when boto3 and moto are installed.
    "demo_private_data.ipynb": """
assert first_download["failed"] == 0
assert first_download["succeeded"] == (11 if s3_mock else 9)
assert (checked["succeeded"], checked["failed"]) == (1, 1)
assert restored["from_store"] == ["SgrA_001.txt"]
assert modified == ["SgrA_001.txt"]
assert refetched["succeeded"] == 1 and "from_store" not in refetched
assert from_mirror["succeeded"] == len(sent["mirror"]) == 3
assert clone_result["failed"] == 0
assert (copy.worktree / "copy/M87_001.txt").read_text() == "EHT data M87_001.txt\\n"
assert "M87_004.txt" not in set(eht.state.data.path)
assert (eht.worktree / "M87_004.txt").exists()
""",
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def demo_dataset(tmp_path):
    root = tmp_path / "server"
    root.mkdir()
    checksums = []
    for source in ("M87", "SgrA"):
        for day in (1, 2):
            name = f"{source}_{day:03d}.txt"
            path = root / name
            path.write_text(f"demo data for {name}\n", encoding="utf-8")
            checksums.append(f"{sha256(path.read_bytes()).hexdigest()}  {name}")
    (root / "sha256sums").write_text("\n".join(checksums) + "\n")
    handler = partial(_QuietHandler, directory=str(root))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("notebook", [
    "demo_cli.ipynb",
    "demo_python.ipynb",
    "demo_add.ipynb",
    "demo_private_data.ipynb",
])
def test_demo_notebook_workflow(tmp_path, demo_dataset, notebook):
    """Execute every code cell, retaining state and failing on the first error.

    JSON loading avoids adding Jupyter dependencies to the test suite. Bash cells
    run in one shell and Python cells in one interpreter. Only the documented SSH
    URL is replaced; discovery, downloads, clones, and local commands are real.
    The notebooks run as on a fresh machine, without a git identity or netrc.
    """
    document = json.loads((DEMO_DIR / notebook).read_text(encoding="utf-8"))
    cells = [(index, "".join(cell["source"]).replace(DEMO_URL, demo_dataset))
             for index, cell in enumerate(document["cells"], start=1)
             if cell["cell_type"] == "code"]
    home = tmp_path / "home"
    home.mkdir()
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               NETRC=str(tmp_path / "no-netrc"), TMPDIR=str(tmp_path))
    # Python finds packages installed with pip --user through HOME.
    env.setdefault("PYTHONUSERBASE", site.getuserbase())
    # Include the checkout for subprocesses even when the package is not installed.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(DEMO_DIR.parent / "mod"), env.get("PYTHONPATH")]))
    if document["metadata"]["kernelspec"]["language"] == "bash":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("The CLI demo requires Bash")
        # Invoke the public entrypoint with this interpreter, without requiring an
        # installed hm console script or a Bash Jupyter kernel.
        preamble = (
            "set -euo pipefail\n"
            f"hm() {{ {shlex.quote(sys.executable)} -c "
            "'from hallmark.cli import hallmark; hallmark()' \"$@\"; }\n"
        )
        script = preamble + "\n".join(
            f"printf 'Running cell {index}\\n'\n{source}\n"
            for index, source in cells)
        command = [bash, "-c", script]
    else:
        # API downloads return result dictionaries; verify payloads too so an
        # unsuccessful transfer cannot pass merely because no exception arose.
        validation = _CHECKS_BEFORE_CLEANUP.get(notebook)
        if validation is not None:
            cells = [
                (index, validation + source if "shutil.rmtree(workspace)" in source
                 else source) for index, source in cells]
        script = "\n".join(
            f"print('Running cell {index}', flush=True)\n{source}\n"
            for index, source in cells)
        command = [sys.executable, "-c", script]
    result = subprocess.run(command, cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Error:" not in result.stderr, result.stdout + result.stderr
    assert f"Running cell {cells[-1][0]}\n" in result.stdout
