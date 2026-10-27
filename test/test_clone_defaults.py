from hashlib import sha1

import pytest
import requests
from click.testing import CliRunner

from hallmark import Repo
from hallmark.cli import hallmark
from hallmark.error import CloneError, DestinationExistsError
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


def test_python_clone_downloads_nothing_without_approval(clone_source, tmp_path):
    source, calls, server = clone_source
    target = tmp_path / 'copy'
    repo = Repo.clone(str(source.dothm.path), target)
    # As with Repo.download, a transfer needs approval; the catalogue is kept.
    assert repo.download_result is None
    assert calls == [] and not (target / 'item1.txt').exists()
    assert repo.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha


def test_python_clone_downloads_when_approved(clone_source, tmp_path):
    source, calls, server = clone_source
    target = tmp_path / 'copy'
    repo = Repo.clone(str(source.dothm.path), target, approved=True)
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
            Repo.clone(str(source.dothm.path), target, approved=True)
        message = str(error.value)
    assert 'Failed to download 1 file(s)' in message
    assert 'Checksum mismatch' in message
    assert 'item1.txt' in message
    assert f'Catalog kept at "{target / ".hm"}"' in message
    if cli:
        assert f'run hm download --all from "{target}" to retry.' in message
        assert 'Download completed with errors: 1 succeeded, 1 failed' in message
        assert '  - item1.txt: Checksum mismatch (sha1)' in message
    else:
        assert 'Successfully downloaded files were kept.' in message
        assert error.value.result['failed'] == 1
    assert Repo(target).dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert not (target / 'item1.txt').exists()
    assert (target / 'item2.txt').read_bytes() == b'science'
    assert sorted(calls) == [
        'https://clone.test/data/item1.txt', 'https://clone.test/data/item2.txt']


@pytest.mark.parametrize('cli', [False, True])
@pytest.mark.parametrize('download', [False, True])
def test_clone_rejects_local_files(tmp_path, cli, download):
    source = Repo.init(tmp_path / 'source')
    (source.worktree / 'item1.txt').write_bytes(b'science')
    source.add('item{item:d}.txt')
    source.commit('Local measurements')
    target = tmp_path / 'new-parent' / 'copy'
    expected = (
        "This repository tracks local files. "
        "Their contents aren't included in a clone.\n"
        "To share data, put it on a server and catalog it with hm add URL, "
        "or copy the data folder yourself.")
    if cli:
        args = ['clone', str(source.dothm.path), str(target)]
        if not download:
            args.append('--no-download')
        result = CliRunner().invoke(hallmark, args)
        assert result.exit_code == 1, result.output
        assert expected in result.output
        assert 'Download these files?' not in result.output
    else:
        with pytest.raises(CloneError) as error:
            Repo.clone(str(source.dothm.path), target, download=download)
        assert str(error.value) == expected
    assert not target.parent.exists()
    assert (source.worktree / 'item1.txt').read_bytes() == b'science'


@pytest.mark.parametrize('cli', [False, True])
@pytest.mark.parametrize('download', [False, True])
@pytest.mark.parametrize('committed', [False, True])
def test_clone_empty_repository(tmp_path, cli, download, committed):
    source = Repo.init(tmp_path / 'source')
    if committed:
        source.commit('Empty catalog')
    target = tmp_path / 'copy'
    if cli:
        args = ['clone', str(source.dothm.path), str(target)]
        if not download:
            args.append('--no-download')
        result = CliRunner().invoke(hallmark, args)
        assert result.exit_code == 0, result.output
        assert 'Download these files?' not in result.output
        repo = Repo(target)
    else:
        repo = Repo.clone(str(source.dothm.path), target, download=download)
    assert repo.state.data.empty
    assert repo.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert sorted(path.name for path in target.iterdir()) == ['.hm']


def test_clone_copy_failure_removes_destination(clone_source, tmp_path, monkeypatch):
    source, calls, _ = clone_source
    target = tmp_path / 'copy'

    def fail_copy(staged, destination, **kwargs):
        (destination / 'partial').write_text('incomplete')
        raise OSError('Copy failed')

    monkeypatch.setattr('hallmark.remote.clone.copytree', fail_copy)
    with pytest.raises(OSError, match='Copy failed'):
        Repo.clone(str(source.dothm.path), target, download=False)
    assert not target.exists()
    assert calls == []


def test_clone_preserves_destination_created_during_source_read(
        clone_source, tmp_path, monkeypatch):
    source, calls, _ = clone_source
    target = tmp_path / 'copy'
    from hallmark.repo.dothm import Dothm
    original_clone = Dothm.clone

    def clone(*args, **kwargs):
        catalog = original_clone(*args, **kwargs)
        target.mkdir()
        (target / 'keep').write_text('keep')
        return catalog

    monkeypatch.setattr(Dothm, 'clone', clone)
    with pytest.raises(DestinationExistsError):
        Repo.clone(str(source.dothm.path), target, download=False)
    assert (target / 'keep').read_text() == 'keep'
    assert calls == []
