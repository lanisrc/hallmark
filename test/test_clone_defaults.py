from hashlib import sha1

import pytest
import requests
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.transport.base import DownloadError
from mock_server import MockServer


@pytest.fixture
def clone_source(tmp_path, monkeypatch):
    source = Repo.init(tmp_path / 'source')
    payload = b'science'
    (source.worktree / 'item1.txt').write_bytes(payload)
    source.add('item{item:d}.txt')
    source.set_config(remote_url='https://clone.test/data/')
    source.commit('Catalogued data')
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
    assert repo.state.data.iloc[0]['sha1'] == sha1(b'science').hexdigest()
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
    (source.worktree / 'item2.txt').write_bytes(b'science')
    source.add('item{item:d}.txt')
    source.commit('Add another file')
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
