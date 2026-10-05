from hashlib import sha1
from pathlib import Path

import pandas as pd
import pytest
import requests
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.error import CloneError, DestinationExistsError
from hallmark.remote.clone import default_clone_destination
from hallmark.transport.base import DownloadError
from mock_server import MockServer


def _catalog_remote_files(repo, files, message="Catalogued remote data"):
    """Commit a catalog of files that stay on the mock HTTP server."""
    repo.state.config = {
        "data": [{"db": "data.tsv"}],
        "remote": {"name": "origin", "url": "https://clone.test/data/"}}
    repo.state.data = pd.DataFrame([
        {"path": name, "checksum_algorithm": "sha1",
         "checksum": sha1(content).hexdigest(), "size_bytes": len(content)}
        for name, content in files])
    repo.dothm.save_state(repo.state)
    repo.dothm.index.commit(message)


def _remote_catalog_source(path, files=(("item1.txt", b"science"),)):
    """Create a repository whose committed catalog lists remote files."""
    repo = Repo.init(path)
    _catalog_remote_files(repo, files)
    return repo


@pytest.fixture
def clone_source(tmp_path, monkeypatch):
    payload = b'science'
    source = _remote_catalog_source(tmp_path / 'source', [('item1.txt', payload)])
    server = MockServer('https://clone.test/data/')
    server.add_file('item1.txt', payload)
    calls = []
    original = server.get

    def get(url, **kwargs):
        calls.append(url)
        return original(url, **kwargs)

    def session():
        return server

    server.get = get
    monkeypatch.setattr(requests, 'Session', session)
    return source, calls, server


def test_python_clone_downloads_by_default(clone_source, tmp_path):
    source, calls, server = clone_source
    target = tmp_path / 'copy'
    repo = Repo.clone(str(source.dothm.path), target)
    assert (target / 'item1.txt').read_bytes() == b'science'
    assert repo.state.data.iloc[0]['checksum'] == sha1(b'science').hexdigest()
    assert repo.download_result['succeeded'] == 1
    assert calls == ['https://clone.test/data/item1.txt']


def test_python_clone_decline_preserves_catalog(clone_source, tmp_path):
    source, calls, server = clone_source
    target = tmp_path / 'copy'
    plans = []

    def decline(plan):
        assert (target / '.hm/data.tsv').exists()
        assert calls == []
        plans.append(plan)
        return False

    repo = Repo.clone(str(source.dothm.path), target, approve=decline)
    assert len(plans) == 1
    assert repo.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert repo.download_result is None
    assert not (target / 'item1.txt').exists()
    assert calls == []


@pytest.mark.parametrize('answer', ['y\n', 'n\n', '\n'])
def test_default_cli_clone_confirmation(clone_source, tmp_path, answer):
    source, calls, server = clone_source
    target = tmp_path / 'copy'
    result = CliRunner().invoke(hallmark, [
        'clone', str(source.dothm.path), str(target)], input=answer)
    assert result.exit_code == 0, result.output
    assert 'Download these files? [y/N]' in result.output
    assert '1 file(s)' in result.output
    assert 'Source: https://clone.test/data/' in result.output
    assert str(target.resolve()) in result.output
    assert (target / '.hm/data.tsv').exists()
    if answer == 'y\n':
        assert calls == ['https://clone.test/data/item1.txt']
        assert (target / 'item1.txt').read_bytes() == b'science'
        assert 'Successfully downloaded 1 files' in result.output
    else:
        assert calls == []
        assert not (target / 'item1.txt').exists()
        assert 'Aborted' not in result.output
        assert 'Skipped download; run hm download --all' in result.output


@pytest.mark.parametrize('bare', [False, True])
def test_clone_no_download_never_plans(clone_source, tmp_path, monkeypatch, bare):
    source, calls, server = clone_source
    target = tmp_path / ('copy.hm' if bare else 'copy')

    def reject_plan(*args, **kwargs):
        raise AssertionError('No-download must not plan a transfer')

    monkeypatch.setattr(Repo, 'plan_download', reject_plan)
    result = CliRunner().invoke(hallmark, [
        'clone', str(source.dothm.path), str(target), '--no-download'])
    assert result.exit_code == 0, result.output
    assert Repo(target).dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert 'Download these files?' not in result.output
    assert calls == []


@pytest.mark.parametrize('cli', [False, True])
def test_bare_clone_requires_explicit_skip(tmp_path, monkeypatch, cli):
    target = tmp_path / 'bare.hm'

    def reject_source(*args, **kwargs):
        raise AssertionError('Invalid bare clone must fail before source access')

    monkeypatch.setattr('hallmark.remote.clone.clone_catalog', reject_source)
    if cli:
        result = CliRunner().invoke(hallmark, ['clone', 'source', str(target)])
        assert result.exit_code != 0
        assert '--no-download' in result.output
    else:
        with pytest.raises(DownloadError, match='download=False'):
            Repo.clone('source', target)
    assert not target.exists()


def test_python_bare_clone_without_download(clone_source, tmp_path):
    source, calls, server = clone_source
    target = tmp_path / 'bare.hm'
    repo = Repo.clone(str(source.dothm.path), target, download=False)
    assert repo.worktree is None
    assert (target / 'data.tsv').exists()
    assert repo.download_result is None
    assert calls == []


@pytest.mark.parametrize('selection', [{'filter': '*.txt'}, {'fmt': 'item{i:d}.txt'}])
def test_python_no_download_rejects_selection(tmp_path, monkeypatch, selection):
    target = tmp_path / 'copy'

    def reject_source(*args, **kwargs):
        raise AssertionError('Invalid selection must fail before source access')

    monkeypatch.setattr('hallmark.remote.clone.clone_catalog', reject_source)
    with pytest.raises(ValueError, match='download=True'):
        Repo.clone('source', target, download=False, **selection)
    assert not target.exists()


def test_clone_filtered_to_zero_files_does_not_prompt(clone_source, tmp_path):
    source, calls, server = clone_source
    target = tmp_path / 'copy'
    result = CliRunner().invoke(hallmark, [
        'clone', str(source.dothm.path), str(target), '--filter', '*.fits'])
    assert result.exit_code == 0, result.output
    assert 'No files selected for download.' in result.output
    assert 'Download these files?' not in result.output
    assert (target / '.hm/data.tsv').exists()
    assert calls == []


@pytest.mark.parametrize('cli', [False, True])
def test_clone_download_failure_keeps_catalog_and_valid_files(
        clone_source, tmp_path, cli):
    source, calls, server = clone_source
    _catalog_remote_files(source, [('item1.txt', b'science'),
                                   ('item2.txt', b'science')], 'Add another file')
    server.add_file('item1.txt', b'invalid')
    server.add_file('item2.txt', b'science')
    target = tmp_path / 'copy'
    if cli:
        result = CliRunner().invoke(hallmark, [
            'clone', str(source.dothm.path), str(target)], input='y\n')
        assert result.exit_code == 1, result.output
        message = result.output
    else:
        with pytest.raises(DownloadError) as error:
            Repo.clone(str(source.dothm.path), target)
        message = str(error.value)
    assert 'Failed to download 1 file(s)' in message
    assert 'Checksum mismatch' in message
    assert 'item1.txt' in message
    assert f'Catalog kept at "{target / ".hm"}"' in message
    assert 'Successfully downloaded files were kept.' in message
    assert Repo(target).dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert not (target / 'item1.txt').exists()
    assert (target / 'item2.txt').read_bytes() == b'science'
    assert sorted(calls) == [
        'https://clone.test/data/item1.txt', 'https://clone.test/data/item2.txt']


@pytest.mark.parametrize("source, expected", [
    ("https://github.com/team/eht-data.git", "eht-data"),
    ("git@github.com:team/eht-data.git", "eht-data"),
    ("ssh://git.example.test/srv/eht-data.git/", "eht-data"),
    ("/data/eht-data/.hm", "eht-data"),
    ("/data/eht-data/.hm/", "eht-data"),
    ("/data/eht-data", "eht-data"),
    ("../catalog.hm", "catalog"),
    ("https://catalogs.example.org/lab/", "lab"),
    ("https://catalogs.example.org/published/.hm/", "published"),
    ("file:///data/eht%20data/.hm", "eht data"),
])
def test_default_destination_is_named_after_the_source(source, expected):
    assert default_clone_destination(source) == Path(expected)


@pytest.mark.parametrize("source", ["https://example.test/", "/", ".hm", ".."])
def test_unnamed_source_requires_a_directory(source):
    with pytest.raises(CloneError, match="DIRECTORY"):
        default_clone_destination(source)


def test_cli_clone_defaults_to_a_folder_named_after_the_source(
        tmp_path, monkeypatch):
    source = _remote_catalog_source(tmp_path / "eht-data")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(hallmark, [
        "clone", str(source.dothm.path), "--no-download"])
    assert result.exit_code == 0, result.output
    assert 'Successfully cloned to "eht-data"' in result.output
    assert Repo(work / "eht-data").dothm.head.commit.hexsha == \
        source.dothm.head.commit.hexsha
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    python = Repo.clone(str(source.worktree), download=False)
    assert python.worktree == other / "eht-data"


@pytest.mark.parametrize("cli", [False, True])
def test_clone_into_an_existing_empty_folder(tmp_path, cli):
    source = _remote_catalog_source(tmp_path / "source")
    target = tmp_path / "empty"
    target.mkdir()
    if cli:
        result = CliRunner().invoke(hallmark, [
            "clone", str(source.dothm.path), str(target), "--no-download"])
        assert result.exit_code == 0, result.output
    else:
        Repo.clone(str(source.dothm.path), target, download=False)
    assert Repo(target).dothm.head.commit.hexsha == source.dothm.head.commit.hexsha


def test_failed_clone_empties_an_existing_folder_but_keeps_it(tmp_path):
    source = _remote_catalog_source(tmp_path / "source")
    (source.dothm.path / "meta.yml").unlink()
    source.dothm.index.remove(["meta.yml"])
    source.dothm.index.commit("Remove required metadata")
    target = tmp_path / "empty"
    target.mkdir()
    with pytest.raises(CloneError, match="missing required file"):
        Repo.clone(str(source.dothm.path), target, download=False)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_failed_clone_keeps_files_added_to_an_existing_folder(
        tmp_path, monkeypatch):
    from hallmark.remote.clone import Dothm

    source = _remote_catalog_source(tmp_path / "source")
    target = tmp_path / "empty"
    target.mkdir()
    original = Dothm.clone

    def clone_then_fail(url, destination, **kwargs):
        original(url, destination, **kwargs)
        # Someone saves a file in the folder while the clone runs.
        (target / "notes.txt").write_text("keep me")
        raise CloneError("simulated failure")

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", clone_then_fail)
    with pytest.raises(CloneError, match="simulated failure"):
        Repo.clone(str(source.dothm.path), target, download=False)
    assert sorted(path.name for path in target.iterdir()) == ["notes.txt"]
    assert (target / "notes.txt").read_text() == "keep me"


def test_failed_clone_empties_an_existing_bare_folder(tmp_path):
    source = _remote_catalog_source(tmp_path / "source")
    (source.dothm.path / "meta.yml").unlink()
    source.dothm.index.remove(["meta.yml"])
    source.dothm.index.commit("Remove required metadata")
    target = tmp_path / "copy.hm"
    target.mkdir()
    with pytest.raises(CloneError, match="missing required file"):
        Repo.clone(str(source.dothm.path), target, download=False)
    assert target.is_dir() and list(target.iterdir()) == []


def test_failed_clone_never_follows_a_swapped_in_link(tmp_path, monkeypatch):
    from hallmark.remote.clone import Dothm

    source = _remote_catalog_source(tmp_path / "source")
    target = tmp_path / "copy.hm"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep me")
    original = Dothm.clone

    def clone_then_swap(url, destination, **kwargs):
        original(url, destination, **kwargs)
        target.rename(tmp_path / "moved.hm")
        target.symlink_to(elsewhere, target_is_directory=True)
        raise CloneError("simulated failure")

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", clone_then_swap)
    with pytest.raises(CloneError, match="simulated failure"):
        Repo.clone(str(source.dothm.path), target, download=False)
    assert target.is_symlink()
    assert (elsewhere / "keep.txt").read_text() == "keep me"


def test_clone_refuses_a_destination_inside_a_repository(tmp_path, monkeypatch):
    source = _remote_catalog_source(tmp_path / "source")
    other = Repo.init(tmp_path / "other")
    (other.worktree / "sub").mkdir()
    monkeypatch.chdir(other.worktree / "sub")
    for arguments in (["clone", str(source.dothm.path), "--no-download"],
                      ["clone", str(source.dothm.path), "nested/copy"]):
        result = CliRunner().invoke(hallmark, arguments)
        assert result.exit_code == 1
        assert "inside the Hallmark repository" in result.output
        assert str(other.worktree) in result.output
    assert sorted(path.name for path in (other.worktree / "sub").iterdir()) == []


@pytest.mark.parametrize("relative", ["source/copy", "source/.hm/copy"])
def test_clone_refuses_to_overlap_a_local_source(tmp_path, relative):
    source = _remote_catalog_source(tmp_path / "source")
    with pytest.raises(CloneError, match="overlaps the source|inside the Hallmark"):
        Repo.clone(str(source.dothm.path), tmp_path / relative, download=False)
    assert not (tmp_path / relative).exists()


def test_clone_refuses_a_nonempty_folder_or_symlink(tmp_path):
    source = _remote_catalog_source(tmp_path / "source")
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("keep")
    empty = tmp_path / "empty"
    empty.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(empty, target_is_directory=True)
    for target in (full, alias):
        with pytest.raises(DestinationExistsError):
            Repo.clone(str(source.dothm.path), target, download=False)
    assert (full / "keep.txt").read_text() == "keep"
    assert list(empty.iterdir()) == []


def _local_file_source(path, *, on_branch=None):
    """Create a repository whose catalog versions local files in .hm/objects."""
    source = Repo.init(path)
    if on_branch is not None:
        source.commit("Empty main", allow_empty=True)
        source.dothm.git.checkout("-b", on_branch)
    (source.worktree / "item1.txt").write_bytes(b"science")
    source.add("item{item:d}.txt")
    source.set_config(remote_url="https://clone.test/data/")
    source.commit("Catalogued local data")
    if on_branch is not None:
        source.dothm.git.checkout("main")
    return source


@pytest.mark.parametrize("arguments", [[], ["--no-download"]])
@pytest.mark.parametrize("on_branch", [None, "experiment"])
def test_cli_refuses_to_clone_local_file_repositories(
        tmp_path, monkeypatch, arguments, on_branch):
    source = _local_file_source(tmp_path / "source", on_branch=on_branch)
    monkeypatch.chdir(tmp_path)

    def reject(*args, **kwargs):
        raise AssertionError("a local-file source must be refused before copying")

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", reject)
    result = CliRunner().invoke(hallmark, [
        "clone", str(source.dothm.path), "copy", *arguments])
    assert result.exit_code == 1
    assert "tracks local files" in result.output
    assert "not supported yet" in result.output
    assert "Nothing was created." in result.output
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("relative", ["copy", "new/deep/copy"])
def test_local_file_check_runs_after_cloning_a_git_host(
        tmp_path, monkeypatch, relative):
    from hallmark.remote.clone import Dothm

    source = _local_file_source(tmp_path / "source", on_branch="experiment")
    original = Dothm.clone

    def clone(url, destination, **kwargs):
        return original(str(source.dothm.path), destination, **kwargs)

    monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", clone)
    target = tmp_path / relative
    with pytest.raises(CloneError, match="'experiment' tracks local files"):
        Repo.clone("https://git.example.test/team/data.git", target, download=False)
    assert not (tmp_path / Path(relative).parts[0]).exists()


@pytest.mark.parametrize("relative", ["copy", "new/deep/copy"])
def test_snapshot_of_local_files_is_refused(tmp_path, monkeypatch, relative):
    from hallmark.transport import OperationContext
    from hallmark.transport.base import RemoteObjectMissing

    root = "https://example.test/published/"
    pages = {root + "config.yml": "data:\n- fmt: run{run:d}.txt\n",
             root + "meta.yml": "{}\n",
             root + "data.tsv": "sha1\trun\n" + "a" * 40 + "\t1\n"}

    def read_text(context, path):
        url = context.remote.url.rstrip("/") + "/" + path
        if url not in pages:
            raise RemoteObjectMissing("No such metadata object")
        return pages[url]

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    target = tmp_path / relative
    with pytest.raises(CloneError, match="tracks local files"):
        Repo.clone(root, target, download=False)
    assert not (tmp_path / Path(relative).parts[0]).exists()


def test_empty_repository_with_remote_branch_can_be_cloned(tmp_path):
    source = Repo.init(tmp_path / "source")
    source.commit("Empty catalog", allow_empty=True)
    source.dothm.git.checkout("-b", "remote-data")
    _catalog_remote_files(source, [("item1.txt", b"science")])
    source.dothm.git.checkout("main")
    repo = Repo.clone(str(source.dothm.path), tmp_path / "copy", download=False)
    assert repo.state.data.empty


def test_local_folder_that_is_not_a_repository_reports_the_clone_failure(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.txt").write_text("not a repository")
    with pytest.raises(CloneError, match="hm init PATH, then hm add URL"):
        Repo.clone(str(plain), tmp_path / "copy", download=False)
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("name", ["-q", "--output=stolen"])
@pytest.mark.parametrize("host", [False, True])
def test_clone_refuses_option_like_branch_names(tmp_path, monkeypatch, name, host):
    from hallmark.remote.clone import Dothm

    source = _remote_catalog_source(tmp_path / "source")
    source.dothm.git.update_ref(f"refs/heads/{name}", "HEAD")
    url = str(source.dothm.path)
    if host:
        original = Dothm.clone

        def clone(git_url, destination, **kwargs):
            return original(str(source.dothm.path), destination, **kwargs)

        monkeypatch.setattr("hallmark.remote.clone.Dothm.clone", clone)
        url = "https://git.example.test/team/data.git"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(hallmark, ["clone", url, "copy", "--no-download"])
    assert result.exit_code == 1
    assert f"Source branch {name!r} has an invalid name" in result.output
    assert list(work.iterdir()) == []
