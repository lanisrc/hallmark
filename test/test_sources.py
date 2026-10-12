import pandas as pd
import pytest

from hallmark.remote.clone import _catalog_filenames
from hallmark.remote.sources import (
    SourceSpec,
    derive_remote_name,
    derive_source_name,
    effective_sources,
    match_remote,
    materialize_sources,
    normalize_sources,
    rows_of,
)
from hallmark.repo.state import State, is_remote_catalog
from hallmark.transport.base import RemoteSpec


def test_source_folders_are_normalized_and_round_trip():
    source = SourceSpec("osn", "osn", root="m87/sims", prefix="osn",
                        fmt="{sim}.h5", filter="*.h5")
    assert (source.root, source.prefix, source.filter) == (
        "m87/sims/", "osn/", ("*.h5",))
    assert source.to_config() == {"name": "osn", "remote": "osn",
                                  "root": "m87/sims/", "prefix": "osn/",
                                  "fmt": "{sim}.h5", "filter": ["*.h5"]}
    assert SourceSpec.from_config(source.to_config()) == source
    assert SourceSpec("origin", "origin").to_config() == {
        "name": "origin", "remote": "origin"}
    assert source.local_path("a.h5") == "osn/m87/sims/a.h5"
    assert source.remote_path("osn/m87/sims/a.h5") == "m87/sims/a.h5"
    with pytest.raises(ValueError, match="outside source"):
        source.remote_path("other/a.h5")


@pytest.mark.parametrize("kwargs", [
    {"name": "bad name"},
    {"name": "-dash"},
    {"name": "x" * 65},
    {"remote": ""},
    {"root": "../up"},
    {"root": "/absolute"},
    {"prefix": ".hm/data"},
    {"prefix": "data/.git/"},
    {"fmt": " "},
    {"filter": ["*.h5", ""]},
])
def test_invalid_sources_are_rejected(kwargs):
    values = {"name": "ok", "remote": "origin", **kwargs}
    with pytest.raises(ValueError):
        SourceSpec(**values)


def test_source_lists_check_names_and_remotes():
    entries = [{"name": "a", "remote": "origin"}, {"name": "b", "remote": "osn"}]
    assert [s.name for s in normalize_sources(entries, ["origin", "osn"])] == ["a", "b"]
    assert normalize_sources(None) == []
    with pytest.raises(ValueError, match="unique"):
        normalize_sources([entries[0], entries[0]])
    with pytest.raises(ValueError, match="unknown remote"):
        normalize_sources(entries, ["origin"])
    with pytest.raises(ValueError, match="only"):
        normalize_sources([{"name": "a", "remote": "origin", "url": "x"}])
    with pytest.raises(ValueError, match="list"):
        normalize_sources({"name": "a", "remote": "origin"})


def test_selection_identity_ignores_filter_order():
    source = SourceSpec("a", "origin", fmt="{x}.h5", filter=["*.h5", "run*"])
    assert source.selects_like("{x}.h5", ("run*", "*.h5"))
    assert not source.selects_like("{x}.fits", ("run*", "*.h5"))
    assert not source.selects_like("{x}.h5", None)


def remotes(*urls, names=None):
    names = names or [f"r{index}" for index in range(len(urls))]
    return [{"name": name, "url": url} for name, url in zip(names, urls)]


@pytest.mark.parametrize("url, expected", [
    ("https://data.test/eht/m87/", ("deep", "")),
    ("https://data.test/eht/m87/2017/", ("deep", "2017/")),
    ("https://data.test/eht/sgra/", ("top", "sgra/")),
    ("https://data.test/eht%20data/x/", ("spaced", "x/")),
    ("https://data.test/ehtc/", None),
    ("http://data.test/eht/", None),
    ("https://data.test:8443/eht/", None),
    ("https://other.test/eht/", None),
])
def test_match_remote_picks_the_deepest_root_on_whole_folders(url, expected):
    configured = remotes("https://data.test/eht/", "https://DATA.test/eht/m87",
                         "https://data.test/eht%20data/",
                         names=["top", "deep", "spaced"])
    assert match_remote(RemoteSpec.from_url(url), configured) == expected


def test_match_remote_compares_ssh_users_and_decoded_roots():
    configured = remotes("ssh://alice@lab/srv/ex%23port/", "ssh://lab/srv/")
    assert match_remote(RemoteSpec.from_url("ssh://alice@lab/srv/ex%23port/a/"),
                        configured) == ("r0", "a/")
    assert match_remote(RemoteSpec.from_url("ssh://lab/srv/ex%23port/a/"),
                        configured) == ("r1", "ex#port/a/")
    assert match_remote(RemoteSpec.from_url("sftp://lab/srv/"), configured) is None


def test_subpath_keeps_settings_and_encodes_literal_folders():
    http = RemoteSpec.from_url("https://data.test/eht/", backend="cyverse")
    child = http.subpath("a b#1/")
    assert child.url == "https://data.test/eht/a%20b%231/"
    assert child.backend == "cyverse"
    assert http.subpath("") is http
    ssh = RemoteSpec.from_url("ssh://alice@lab:2200/srv/", "lab",
                              backend_options={"x": 1})
    child = ssh.subpath("ex#port")
    assert (child.root, child.user, child.port, child.auth) == (
        "/srv/ex#port/", "alice", 2200, "lab")
    assert dict(child.backend_options) == {"x": 1}


def test_generated_names_are_unique_and_readable():
    folder = RemoteSpec.from_url("https://Data.CyVerse.org/dav/eht/")
    assert derive_remote_name(folder, []) == "origin"
    assert derive_remote_name(folder, ["origin"]) == "data-cyverse-org"
    assert derive_remote_name(folder, ["origin", "data-cyverse-org"]) == (
        "data-cyverse-org-2")
    assert derive_source_name("origin", "", []) == "origin"
    assert derive_source_name("origin", "", ["origin"]) == "origin-2"
    assert derive_source_name("osn", "m87/sims v2/", []) == "osn-sims-v2"


def legacy_state(rows=("a.h5", "b.h5")):
    return State(
        config={"data": [{"db": "data.tsv", "fmt": "{x}.h5"}],
                "remote": [{"url": "https://data.test/eht/"}]},
        data=pd.DataFrame({"path": list(rows), "checksum": ["", ""][:len(rows)]}))


def test_legacy_catalogues_read_as_one_source_without_changes():
    state = legacy_state()
    before = (state.config.copy(), state.data.copy())
    assert effective_sources(state) == [SourceSpec("origin", "origin", fmt="{x}.h5")]
    assert state.config == before[0]
    pd.testing.assert_frame_equal(state.data, before[1])
    assert rows_of(state.data, effective_sources(state)[0],
                   effective_sources(state)).all()


def test_materialize_records_the_legacy_source():
    state = legacy_state()
    sources = materialize_sources(state)
    assert state.config["sources"] == [
        {"name": "origin", "remote": "origin", "fmt": "{x}.h5"}]
    assert state.config["remote"] == [
        {"name": "origin", "url": "https://data.test/eht/"}]
    assert state.config["data"] == [{"db": "data.tsv"}]
    assert state.data["source"].tolist() == ["origin", "origin"]
    assert effective_sources(state) == sources
    assert materialize_sources(state) == sources


def test_local_manifests_and_unconfigured_catalogues_have_no_sources():
    local = State(config={"data": [{"fmt": "{x}.h5"}]},
                  data=pd.DataFrame({"sha1": ["a" * 40], "x": ["1"]}))
    assert effective_sources(local) == []
    assert effective_sources(State()) == []


def test_rows_of_selects_rows_by_source():
    sources = [SourceSpec("a", "origin"), SourceSpec("b", "osn")]
    frame = pd.DataFrame({"path": ["1", "2", "3"], "source": ["a", "b", "a"]})
    assert rows_of(frame, sources[0], sources).tolist() == [True, False, True]


@pytest.mark.parametrize("config, columns, remote", [
    ({}, ["path", "checksum"], True),
    ({}, ["sha1", "path", "x"], False),
    ({}, ["sha1", "x"], False),
    ({}, ["path", "sha1", "source"], True),
    ({"sources": [{"name": "a", "remote": "origin"}]}, ["sha1"], True),
])
def test_remote_catalogue_predicate(config, columns, remote):
    state = State(config=config, data=pd.DataFrame(columns=columns))
    assert is_remote_catalog(state) is remote


def test_clone_validates_published_sources():
    config = {"data": [{"db": "data.tsv"}],
              "remote": [{"name": "origin", "url": "https://data.test/eht/"}],
              "sources": [{"name": "a", "remote": "origin"}]}
    assert _catalog_filenames(config) == ["data.tsv"]
    config["sources"][0]["remote"] = "missing"
    with pytest.raises(ValueError, match="unknown remote"):
        _catalog_filenames(config)
