
import pytest

from hallmark.error import CheckoutError
from hallmark import Repo
from hallmark.transport import OperationContext
from hallmark.transport.base import RemoteObjectMissing


@pytest.fixture
def remote_listing(monkeypatch):
    pages = {"": '<h1>Index of data</h1><a href="M87_001.fits">M87_001.fits</a>'
                   '<a href="M87_002.fits">M87_002.fits</a>'
                   '<a href="notes.txt">notes.txt</a>'}
    reads = []

    def read_text(context, path):
        reads.append(path)
        if path not in pages:
            raise RemoteObjectMissing("No metadata")
        return pages[path]

    monkeypatch.setattr(OperationContext, "read_text", read_text)
    return pages, reads








def test_failed_remote_add_preserves_staged_state(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/", filter="*.fits")
    before = {name: (repo.dothm.path / name).read_bytes()
              for name in ("config.yml", "meta.yml", "data.tsv", ".git/index")}
    remote_listing[0].clear()
    with pytest.raises(RuntimeError):
        repo.add("https://example.test/data/")
    assert before == {name: (repo.dothm.path / name).read_bytes() for name in before}


def test_remote_add_rejects_different_root_and_local_data_before_network(
        tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "remote")
    repo.add("https://example.test/data/")
    remote_listing[1].clear()
    with pytest.raises(ValueError, match="one dataset root"):
        repo.add("https://different.test/data/")
    assert remote_listing[1] == []
    local = Repo.init(tmp_path / "local")
    (local.worktree / "run1.txt").write_text("one")
    local.add("run{run:d}.txt")
    with pytest.raises(ValueError, match="separate repository"):
        local.add("https://example.test/data/")
    assert remote_listing[1] == []








def test_remote_add_preserves_other_catalog_tables(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.state.config["data"] = [{"db": "other.tsv"}]
    repo.dothm.save_state(repo.state)
    with pytest.raises(ValueError, match="only the data.tsv"):
        repo.add("https://example.test/data/")
    assert repo.state.config["data"] == [{"db": "other.tsv"}]
    assert remote_listing[1] == []








def test_remote_add_stages_without_payload_or_commit(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    head = repo.dothm.head.commit.hexsha
    repo.state.meta["project"] = "keep"
    repo.dothm.save_state(repo.state)
    frame = repo.add("https://example.test/data/{src}_{day:03d}.fits")
    assert frame.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert repo.dothm.head.commit.hexsha == head
    assert repo.state.meta == {"project": "keep"}
    assert repo.state.data["src"].tolist() == ["M87", "M87"]
    assert repo.status()["staged"]["added"] == frame.path.tolist()
    assert repo.status()["worktree"]["deleted"] == []
    assert not (repo.worktree / "M87_001.fits").exists()
    assert not repo.objects.root.exists()
    assert not any(path.endswith(".fits") for path in remote_listing[1])
    assert repo.commit("Add remote EHT data")
    assert repo.dothm.head.commit.message.strip() == "Add remote EHT data"
    assert not repo.commit("No changes")
    assert repo.plan_download().file_count == 2



def test_remote_add_merges_paths_and_leaves_unmatched_rows(tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    root = "https://example.test/data/"
    repo.add(root, filter="*001.fits")
    repo.commit("First selection")
    repo.add(root, filter="*002.fits")
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert repo.status()["staged"]["added"] == ["M87_002.fits"]
    repo.commit("Second selection")
    repo.add(root, filter="*002.fits")
    assert repo.dothm.index.diff("HEAD") == []
    repo.add(root, filter="absent*")
    assert repo.dothm.index.diff("HEAD") == []



def test_remote_parameter_cannot_turn_catalog_into_local_objects(
        tmp_path, remote_listing):
    repo = Repo.init(tmp_path / "data")
    repo.add("https://example.test/data/{sha1}_{day}.fits")
    assert "sha1" not in repo.state.data.columns
    assert repo.commit("Record remote files")
    assert not repo.objects.root.exists()



def test_remote_catalog_clone_and_branches_keep_payloads_separate(
        tmp_path, remote_listing):
    source = Repo.init(tmp_path / "source")
    source.add("https://example.test/data/", filter="*.fits")
    source.commit("Remote catalog")
    repo = Repo.clone(str(source.dothm.path), tmp_path / "copy", download=False)
    assert repo.dothm.head.commit.hexsha == source.dothm.head.commit.hexsha
    assert repo.status()["worktree"]["deleted"] == []
    original = repo.dothm.active_branch.name
    repo.checkout("notes")
    repo.add("https://example.test/data/", filter="*.txt")
    with pytest.raises(CheckoutError, match="Commit"):
        repo.checkout(original)
    repo.commit("Add notes")
    (repo.worktree / "M87_001.fits").write_text("Keep local content")
    repo.checkout(original)
    assert repo.state.data.path.tolist() == ["M87_001.fits", "M87_002.fits"]
    assert (repo.worktree / "M87_001.fits").read_text() == "Keep local content"
    assert not repo.objects.root.exists()

