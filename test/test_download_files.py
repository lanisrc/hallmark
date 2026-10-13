from hashlib import sha1, sha256
from pathlib import Path

import pandas as pd
import pytest
import requests
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.remote.download import DownloadError
from mock_server import MockServer


@pytest.fixture
def download_repo(tmp_path, monkeypatch):
    repo = Repo.init(tmp_path / 'repo')
    repo.set_config(remote_url='https://download.test/data/')
    payloads = {'M87/a.txt': b'first', 'M87/nested/b.txt': b'second',
                'SgrA/c.txt': b'third', 'M870/d.txt': b'fourth'}
    rows = []
    server = MockServer('https://download.test/data/')
    for path, contents in payloads.items():
        rows.append({'path': path, 'sha256': sha256(contents).hexdigest(),
                     'size_bytes': len(contents)})
        server.add_file(path, contents)
    repo.state.data = pd.DataFrame(rows)
    repo.dothm.save_state(repo.state)
    repo = Repo(repo.worktree)
    calls = []
    original = server.get

    def get(url, **kwargs):
        calls.append(url)
        return original(url, **kwargs)

    def session():
        return server

    server.get = get
    monkeypatch.setattr(requests, 'Session', session)
    monkeypatch.chdir(repo.worktree)
    return repo, calls, payloads, server


@pytest.mark.parametrize('folder', ['M87', 'M87/'])
def test_download_folder_includes_subfolders(download_repo, folder):
    repo, calls, payloads, _ = download_repo
    result = CliRunner().invoke(hallmark, ['download', folder], input='y\n')
    assert result.exit_code == 0, result.output
    assert sorted(calls) == ['https://download.test/data/M87/a.txt',
                             'https://download.test/data/M87/nested/b.txt']
    for path in ['M87/a.txt', 'M87/nested/b.txt']:
        assert (repo.worktree / path).read_bytes() == payloads[path]
    assert not (repo.worktree / 'SgrA').exists()
    assert not (repo.worktree / 'M870').exists()


@pytest.mark.parametrize('dry_run', [False, True])
def test_unknown_paths_stop_the_whole_download(download_repo, dry_run):
    repo, calls, _, _ = download_repo
    args = ['download', 'M87/', 'missing.txt', 'empty/']
    if dry_run:
        args.append('--dry-run')
    result = CliRunner().invoke(hallmark, args, input='y\n')
    assert result.exit_code == 1, result.output
    assert 'not in the catalog' in result.output
    assert 'missing.txt' in result.output
    assert 'empty' in result.output
    assert 'Download these files?' not in result.output
    assert calls == []
    assert not (repo.worktree / 'M87').exists()


@pytest.mark.parametrize('dry_run', [False, True])
def test_matching_file_is_skipped(download_repo, dry_run):
    repo, calls, payloads, _ = download_repo
    existing = repo.worktree / 'M87/a.txt'
    existing.parent.mkdir()
    existing.write_bytes(payloads['M87/a.txt'])
    before = existing.stat().st_mtime_ns
    args = ['download', 'M87/a.txt']
    if dry_run:
        args.append('--dry-run')
    result = CliRunner().invoke(hallmark, args)
    assert result.exit_code == 0, result.output
    assert 'Already downloaded: M87/a.txt' in result.output
    assert 'Download these files?' not in result.output
    assert existing.stat().st_mtime_ns == before
    assert calls == []


@pytest.mark.parametrize('dry_run', [False, True])
def test_conflicts_stop_all_downloads(download_repo, dry_run):
    repo, calls, _, _ = download_repo
    for name in ['SgrA/c.txt', 'M870/d.txt']:
        path = repo.worktree / name
        path.parent.mkdir()
        path.write_bytes(b'keep this')
    args = ['download', '--all']
    if dry_run:
        args.append('--dry-run')
    result = CliRunner().invoke(hallmark, args, input='y\n')
    assert result.exit_code == 1, result.output
    assert 'SgrA/c.txt' in result.output
    assert 'M870/d.txt' in result.output
    assert 'Nothing downloaded' in result.output
    assert 'Download these files?' not in result.output
    assert calls == []
    assert not (repo.worktree / 'M87').exists()
    assert (repo.worktree / 'SgrA/c.txt').read_bytes() == b'keep this'
    assert (repo.worktree / 'M870/d.txt').read_bytes() == b'keep this'


def test_output_directory_and_mixed_skips(download_repo, tmp_path):
    repo, calls, payloads, _ = download_repo
    output = tmp_path / 'output'
    (output / 'M87').mkdir(parents=True)
    (output / 'M87/a.txt').write_bytes(payloads['M87/a.txt'])
    result = CliRunner().invoke(hallmark, [
        'download', 'M87/', '--output', str(output)], input='y\n')
    assert result.exit_code == 0, result.output
    assert 'Already downloaded: M87/a.txt' in result.output
    assert '1 file(s); 6 bytes' in result.output
    assert calls == ['https://download.test/data/M87/nested/b.txt']
    assert (output / 'M87/nested/b.txt').read_bytes() == b'second'
    assert not (repo.worktree / 'M87').exists()


@pytest.mark.parametrize('contents', [b'first', b'different'])
def test_execution_rechecks_files_after_planning(download_repo, contents):
    repo, calls, _, _ = download_repo
    plan = repo.plan_download(file_paths=['M87/a.txt'])
    target = repo.worktree / 'M87/a.txt'
    target.parent.mkdir()
    target.write_bytes(contents)
    if contents == b'first':
        result = repo.download(plan, approved=True)
        assert result['succeeded'] == 0
        assert result['skipped'] == ['M87/a.txt']
    else:
        with pytest.raises(DownloadError, match='conflict'):
            repo.download(plan, approved=True)
    assert calls == []
    assert target.read_bytes() == contents


def test_file_created_during_transfer_is_not_overwritten(download_repo):
    repo, _, _, server = download_repo
    target = repo.worktree / 'M87/a.txt'
    original = server.get

    def get(url, **kwargs):
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b'created during transfer')
        return original(url, **kwargs)

    server.get = get
    plan = repo.plan_download(file_paths=['M87/a.txt'])
    result = repo.download(plan, approved=True)
    assert result['failed'] == 1
    assert 'appeared at its destination' in result['errors'][0]
    assert target.read_bytes() == b'created during transfer'
    assert sorted(path.name for path in target.parent.iterdir()) == ['a.txt']
    # The verified download stays cached for a later run.
    assert repo.objects.contains(sha1(b'first').hexdigest())


@pytest.mark.parametrize('kind', ['directory', 'parent_file', 'unknown_checksum'])
def test_existing_unverifiable_paths_are_preserved(download_repo, kind):
    repo, calls, _, _ = download_repo
    target = repo.worktree / 'M87/a.txt'
    if kind == 'parent_file':
        target.parent.write_bytes(b'parent file')
    else:
        target.parent.mkdir()
        if kind == 'directory':
            target.mkdir()
        else:
            target.write_bytes(b'first')
            repo.state.data.loc[0, 'sha256'] = ''
    with pytest.raises(DownloadError, match='conflict'):
        repo.plan_download(file_paths=['M87/a.txt'])
    assert calls == []
    if kind == 'parent_file':
        assert target.parent.read_bytes() == b'parent file'
    elif kind == 'directory':
        assert target.is_dir()
    else:
        assert target.read_bytes() == b'first'


def test_folder_filters_keep_catalog_metadata(download_repo):
    repo, _, _, _ = download_repo
    plan = repo.plan_download(file_paths=['M87/'], filter='**/a.txt',
                              fmt='M87/{name}.txt')
    assert [item.relative_path for item in plan.items] == [Path('M87/a.txt')]
    assert plan.items[0].checksum == ('sha256', sha256(b'first').hexdigest())


def test_dry_run_lists_pending_and_skipped_without_writing(download_repo):
    repo, calls, _, _ = download_repo
    target = repo.worktree / 'M87/a.txt'
    target.parent.mkdir()
    target.write_bytes(b'first')
    result = CliRunner().invoke(hallmark, ['download', 'M87/', '--dry-run'])
    assert result.exit_code == 0, result.output
    assert 'Already downloaded: M87/a.txt' in result.output
    assert 'M87/nested/b.txt' in result.output
    assert '1 file(s); 6 bytes' in result.output
    assert 'Download these files?' not in result.output
    assert calls == []
    assert not (repo.worktree / 'M87/nested').exists()
