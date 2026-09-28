from pathlib import Path

import pytest

from hallmark import ParaFrame, Repo
from hallmark.remote.download import _select_remote_config, execute_download_plan
from hallmark.remote.plan import DownloadItem, DownloadPlan


Standard_files = [
    f"a{a}_i{i}.h5"
    for a in [0, 0.75, 0.975]
    for i in [0, 30, 60, 90]
]

Encoded_files = [
    f"a{aspin}_i{i}.h5"
    for aspin in ["m0.5"]
    for i in [0, 30, 60, 90]
]

# Actually Write the Standard and Encoded files
def _write_text_files(root: Path, files: list[str]) -> None:
    for name in files:
        (root / name).write_text("test\n", encoding="utf-8")

def _encoded_data_spec() -> list[dict]:
    return [
        {
            "fmt": "encoded/a{aspin}_i{i}.h5",
            "encoding": {
                "aspin": r"m([0-9]+(\.[0-9]+)?|\.[0-9]+)"
            },
        }
    ]


@pytest.fixture(scope="session")
def hallmark_test_suite_dictionary(tmp_path_factory):

    # Make a dedicated folder that we can find for potential debugging
    tmp_path = tmp_path_factory.mktemp("hallmark_test_session")
    repo_path = tmp_path / "repo"

    # Initialize repo in dedicated folder
    repo = Repo.init(repo_path)

    # Actually write out listed files in the temporary directory
    _write_text_files(repo_path, Standard_files)
    # write encoded files to separate directory to avoid globbing issues
    encoded_path = repo_path / "encoded"
    encoded_path.mkdir()
    _write_text_files(encoded_path, Encoded_files)
    encoded_specs = _encoded_data_spec()

    # Create paraframes, glob files, glob pattern and repo behavior objects
    standard_pf = ParaFrame.parse(
        "a{a}_i{i}.h5", base_path=repo.worktree
    )

    encoded_pf = ParaFrame.parse(
        "encoded/a{aspin}_i{i}.h5",
        base_path=repo.worktree,
        encodings=encoded_specs,
        encoding=True,
    )

    standard_globbed_files, standard_glob_pattern = ParaFrame.glob_search(
        "a{a}_i{i}.h5",
        base_path=repo.worktree,
        return_pattern=True,
    )

    encoded_globbed_files, encoded_glob_pattern = ParaFrame.glob_search(
        "encoded/a{aspin}_i{i}.h5",
        base_path=repo.worktree,
        encodings=encoded_specs,
        encoding=True,
        return_pattern=True,
    )

    add_result = repo.add("a{a}_i{i}.h5")
    commit_result = repo.commit("Commit test")

    # Return any potential object needed for testing
    return {
        "standard_pf": standard_pf,
        "encoded_pf": encoded_pf,
        "standard_files": Standard_files,
        "encoded_files": Encoded_files,
        "standard_globbed_files": standard_globbed_files,
        "standard_glob_pattern": standard_glob_pattern,
        "encoded_globbed_files": encoded_globbed_files,
        "encoded_glob_pattern": encoded_glob_pattern,
        "add_result": add_result,
        "commit_result": commit_result,
        "repo_path": repo_path,
    }


def download_selection(repo, output, selected, max_workers=4, show_progress=False,
                       remote_name=None):
    remote = _select_remote_config(repo, remote_name=remote_name)
    plan = DownloadPlan(
        [DownloadItem(path, checksum) for path, checksum in selected],
        remote["url"], output, remote_auth=remote.get("auth"),
        remote_backend=remote.get("backend"),
        backend_options=remote.get("backend_options") or {})
    return execute_download_plan(repo, plan, approved=True, max_workers=max_workers,
                                 show_progress=show_progress)
