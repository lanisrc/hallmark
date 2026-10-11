from hashlib import sha256
from pathlib import Path

import pandas as pd
import pytest
import requests
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
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
    repo.dothm.write_tsv(pd.DataFrame(rows), 'data')
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


def test_folder_filters_keep_catalog_metadata(download_repo):
    repo, _, _, _ = download_repo
    plan = repo.plan_download(file_paths=['M87/'], filter='**/a.txt',
                              fmt='M87/{name}.txt')
    assert [item.relative_path for item in plan.items] == [Path('M87/a.txt')]
    assert plan.items[0].checksum == ('sha256', sha256(b'first').hexdigest())
