import copy

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
    validate_source_name,
)
from hallmark.repo.state import State, is_remote_catalog
from hallmark.transport.base import RemoteSpec


def test_source_folders_are_normalized_and_round_trip():
    source = SourceSpec("osn", "osn", root="m87/sims", prefix="osn",
                        filter="*.h5")
    assert (source.root, source.prefix, source.filter) == (
        "m87/sims/", "osn/", ("*.h5",))
    assert source.to_config() == {"name": "osn", "remote": "osn",
                                  "root": "m87/sims/", "prefix": "osn/",
                                  "filter": ["*.h5"]}
    assert SourceSpec.from_config(source.to_config()) == source
    assert SourceSpec("origin", "origin").to_config() == {
        "name": "origin", "remote": "origin"}
    assert source.local_path("a.h5") == "osn/m87/sims/a.h5"
    assert source.remote_path("osn/m87/sims/a.h5") == "m87/sims/a.h5"
    with pytest.raises(ValueError, match="outside source"):
        source.remote_path("other/a.h5")


@pytest.mark.parametrize("kwargs, message", [
    ({"name": "bad name"}, "Source names use"),
    ({"name": "-dash"}, "Source names use"),
    ({"name": "x" * 65}, "Source names use"),
    ({"remote": ""}, "Source 'ok' must name its remote"),
    ({"root": "../up"}, "safe relative path"),
    ({"root": "/absolute"}, "safe relative path"),
    ({"prefix": ".hm/data"}, "repository metadata"),
    ({"prefix": "data/.git/"}, "repository metadata"),
    ({"filter": ["*.h5", ""]}, "Source filter must be a glob or a list of globs"),
])
def test_invalid_sources_are_rejected(kwargs, message):
    values = {"name": "ok", "remote": "origin", **kwargs}
    with pytest.raises(ValueError, match=message):
        SourceSpec(**values)


@pytest.mark.parametrize("name", ["a", "9", "a_-", "x" * 64])
def test_source_names_of_1_to_64_characters_are_accepted(name):
    assert validate_source_name(name) == name


@pytest.mark.parametrize("name", ["", "x" * 65, None, 5, "_x", "a\n"])
def test_malformed_source_names_are_refused(name):
    with pytest.raises(ValueError, match="Source names use"):
        validate_source_name(name)


@pytest.mark.parametrize("remote", [None, 5])
def test_a_source_remote_must_be_a_name(remote):
    with pytest.raises(ValueError, match="Source 'a' must name its remote"):
        SourceSpec("a", remote)


def test_a_configured_source_must_name_its_remote():
    with pytest.raises(ValueError, match="Source 'a' must name its remote"):
        SourceSpec.from_config({"name": "a"})


@pytest.mark.parametrize("checksum", ["", "MD5", "crc32"])
def test_an_unknown_server_checksum_is_refused(checksum):
    with pytest.raises(ValueError, match="server_checksum must be one of"):
        SourceSpec("a", "origin", server_checksum=checksum)


def test_a_server_checksum_round_trips_through_the_config():
    source = SourceSpec("a", "origin", server_checksum="sha256")
    assert source.to_config() == {"name": "a", "remote": "origin",
                                  "server_checksum": "sha256"}
    assert SourceSpec.from_config(source.to_config()) == source


@pytest.mark.parametrize("value", [None, "", [], ()])
def test_an_empty_filter_means_no_filter(value):
    assert SourceSpec("a", "origin", filter=value).filter == ()
    assert SourceSpec("a", "origin").selects_like(value)


@pytest.mark.parametrize("value", [{"*.h5"}, [None], [3], 5])
def test_a_source_filter_must_be_globs(value):
    with pytest.raises(ValueError, match="Source filter must be a glob"):
        SourceSpec("a", "origin", filter=value)


def test_repeated_filter_patterns_are_kept():
    source = SourceSpec("a", "origin", filter=["*.h5", "*.h5"])
    assert source.filter == ("*.h5", "*.h5")


@pytest.mark.parametrize("path", ["px/r/x.h5", "p", ""])
def test_a_path_outside_the_source_prefix_is_refused(path):
    with pytest.raises(ValueError, match="outside source 'a'"):
        SourceSpec("a", "origin", prefix="p/").remote_path(path)


def test_without_a_prefix_catalogue_paths_are_remote_paths():
    source = SourceSpec("a", "origin", root="r/")
    assert source.remote_path("r/x.h5") == "r/x.h5"


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
    # Formats belong to the catalogue, not to one source.
    with pytest.raises(ValueError, match="only"):
        normalize_sources([{"name": "a", "remote": "origin", "fmt": "{x}.h5"}])
    with pytest.raises(ValueError, match="list"):
        normalize_sources({"name": "a", "remote": "origin"})


@pytest.mark.parametrize("value", [({"name": "a", "remote": "origin"},), "a"])
def test_a_sources_section_must_be_a_list(value):
    with pytest.raises(ValueError, match="sources must be a list of mappings"):
        normalize_sources(value)


def test_an_empty_sources_section_lists_no_sources():
    assert normalize_sources([], ["origin"]) == []


def test_remote_references_are_not_checked_without_remotes():
    entries = [{"name": "a", "remote": "missing"}]
    assert [source.remote for source in normalize_sources(entries)] == ["missing"]


def test_selection_identity_ignores_filter_order():
    source = SourceSpec("a", "origin", filter=["*.h5", "run*"])
    assert source.selects_like(("run*", "*.h5"))
    assert not source.selects_like(("run*",))
    assert not source.selects_like(None)


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
    assert match_remote(RemoteSpec.from_url("ssh://lab/srv/a%2541/"),
                        configured) == ("r1", "a%41/")
    assert match_remote(RemoteSpec.from_url("sftp://lab/srv/"), configured) is None


@pytest.mark.parametrize("server", [
    "https://data.test/eht/", "s3://bucket/eht/", "ssh://lab/srv/eht/",
    "sftp://lab/srv/eht/"])
def test_match_remote_decodes_folder_names_once(server):
    folder = RemoteSpec.from_url(server + "a%2541/")
    assert match_remote(folder, remotes(server)) == ("r0", "a%41/")


def test_match_remote_prefers_the_first_of_equally_deep_remotes():
    configured = remotes("https://data.test/ab/", "https://data.test/a%62/",
                         names=["first", "second"])
    folder = RemoteSpec.from_url("https://data.test/ab/x/")
    assert match_remote(folder, configured) == ("first", "x/")
    assert match_remote(folder, configured[::-1]) == ("second", "x/")


@pytest.mark.parametrize("url, expected", [
    ("https://data.test/eht/x/", ("r0", "eht/x/")),
    ("https://data.test/", ("r0", "")),
    ("https://data.test", ("r0", "")),
])
def test_a_remote_without_a_path_holds_every_folder_on_its_server(url, expected):
    configured = remotes("https://data.test")
    assert match_remote(RemoteSpec.from_url(url), configured) == expected


@pytest.mark.parametrize("url, expected", [
    ("ssh://alice@LAB/srv/x/", ("lab", "x/")),
    ("ssh://alice@lab:2200/srv/x/", None),
    ("ssh://bob@lab/srv/x/", None),
    ("ssh://lab/srv/x/", None),
])
def test_match_remote_needs_the_same_ssh_port_and_user(url, expected):
    configured = remotes("ssh://alice@lab/srv/", names=["lab"])
    assert match_remote(RemoteSpec.from_url(url), configured) == expected


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


@pytest.mark.parametrize("url, expected", [
    ("https://" + "a" * 60 + ".test/", "a" * 40),
    ("ssh://_/srv/", "remote"),
])
def test_remote_names_derived_from_hosts_are_short_and_readable(url, expected):
    assert derive_remote_name(RemoteSpec.from_url(url), ["origin"]) == expected


@pytest.mark.parametrize("remote, root, taken, expected", [
    ("origin", "%%%/", [], "origin"),
    ("eht.data", "", [], "eht-data"),
    ("eht.data", "raw/", [], "eht-data-raw"),
    ("_lab", "", [], "lab"),
    ("-", "", [], "source"),
    ("-", "raw/", [], "raw"),
    ("origin", "", ["origin", "origin-2"], "origin-3"),
    ("x" * 70, "", [], "x" * 64),
    ("x" * 70, "", ["x" * 64], "x" * 62 + "-2"),
    ("origin", "y" * 70 + "/", [], "origin-" + "y" * 57),
    ("origin", "y" * 70 + "/", ["origin-" + "y" * 57], "origin-" + "y" * 55 + "-2"),
])
def test_derived_source_names_are_cleaned_and_cut_to_fit(remote, root, taken,
                                                          expected):
    assert derive_source_name(remote, root, taken) == expected


@pytest.mark.parametrize("remote", ["origin", "eht.data", "-", "_", "é", "x" * 100])
@pytest.mark.parametrize("root", ["", "raw/", "%%%/", "_x/", "y" * 100 + "/"])
def test_derived_source_names_are_always_valid(remote, root):
    first = derive_source_name(remote, root, [])
    second = derive_source_name(remote, root, [first])
    assert validate_source_name(first) == first
    assert validate_source_name(second) == second
    assert second != first


def legacy_state(rows=("a.h5", "b.h5")):
    return State(
        config={"data": [{"db": "data.tsv", "fmt": "{x}.h5"}],
                "remote": [{"url": "https://data.test/eht/"}]},
        data=pd.DataFrame({"path": list(rows), "checksum": ["", ""][:len(rows)]}))


def test_legacy_catalogues_read_as_one_source_without_changes():
    state = legacy_state()
    before = (copy.deepcopy(state.config), state.data.copy())
    assert effective_sources(state) == [SourceSpec("origin", "origin")]
    assert state.config == before[0]
    pd.testing.assert_frame_equal(state.data, before[1])
    assert rows_of(state.data, effective_sources(state)[0],
                   effective_sources(state)).all()


def test_materialize_records_the_legacy_source():
    state = legacy_state()
    sources = materialize_sources(state)
    assert state.config["sources"] == [{"name": "origin", "remote": "origin"}]
    assert state.config["remote"] == [
        {"name": "origin", "url": "https://data.test/eht/"}]
    # The format stays the catalogue's, sorting every source's files.
    assert state.config["data"] == [{"db": "data.tsv", "fmt": "{x}.h5"}]
    assert state.data["catalog_source"].tolist() == ["origin", "origin"]
    assert effective_sources(state) == sources
    assert materialize_sources(state) == sources


ROOT = "https://data.test/eht/"


def legacy_with(**config):
    return State(config={"data": [{"db": "data.tsv"}], **config},
                 data=pd.DataFrame({"path": ["a.h5"]}))


@pytest.mark.parametrize("config", [
    {},
    {"remote": [{"name": "a", "url": ROOT}, {"name": "b", "url": "https://b.test/"}]},
])
def test_a_legacy_catalogue_needs_exactly_one_remote_for_a_source(config):
    assert effective_sources(legacy_with(**config)) == []


def test_a_config_that_is_not_a_mapping_has_no_sources():
    state = State(config=["remote"], data=pd.DataFrame({"path": ["a.h5"]}))
    assert effective_sources(state) == []


def test_a_legacy_catalogue_is_recognized_by_any_of_its_tables():
    state = State(config={"remote": {"url": ROOT}},
                  tables={"more.tsv": pd.DataFrame({"path": ["a.h5"]})})
    assert effective_sources(state) == [SourceSpec("origin", "origin")]


def test_a_table_with_sha1s_is_not_a_legacy_catalogue():
    state = State(config={"remote": {"url": ROOT}},
                  data=pd.DataFrame({"path": ["a.h5"], "sha1": ["a" * 40]}))
    assert effective_sources(state) == []


@pytest.mark.parametrize("remote, name", [
    ("eht.data", "eht-data"),
    ("my lab", "my-lab"),
    ("lab-", "lab-"),
])
def test_a_legacy_remote_name_becomes_a_valid_source_name(remote, name):
    state = legacy_with(remote={"name": remote, "url": ROOT})
    assert effective_sources(state) == [SourceSpec(name, remote)]
    assert materialize_sources(state) == [SourceSpec(name, remote)]
    assert state.config["sources"] == [{"name": name, "remote": remote}]
    assert state.data["catalog_source"].tolist() == [name]


def test_a_null_sources_section_is_read_like_a_missing_one():
    assert effective_sources(legacy_with(remote={"url": ROOT}, sources=None)) == [
        SourceSpec("origin", "origin")]
    unused = State(config={"remote": {"url": ROOT}, "sources": None})
    assert effective_sources(unused) == []


def test_materialize_replaces_a_null_sources_section():
    state = legacy_with(remote={"url": ROOT}, sources=None)
    assert materialize_sources(state) == [SourceSpec("origin", "origin")]
    assert state.config["sources"] == [{"name": "origin", "remote": "origin"}]
    assert state.data["catalog_source"].tolist() == ["origin"]
    state.data["sha1"] = ["a" * 40]
    assert is_remote_catalog(state)


def test_materialize_keeps_the_name_of_a_lone_named_remote():
    state = legacy_with(remote={"name": "lab", "url": ROOT})
    assert materialize_sources(state) == [SourceSpec("lab", "lab")]
    assert state.config["remote"] == [{"name": "lab", "url": ROOT}]
    assert state.config["sources"] == [{"name": "lab", "remote": "lab"}]


def test_materialize_writes_no_remote_section_when_none_is_configured():
    state = State(config={"data": [{"db": "data.tsv"}]})
    assert materialize_sources(state) == []
    assert state.config == {"data": [{"db": "data.tsv"}], "sources": []}


def test_materialize_tags_only_tables_that_have_rows():
    state = State(config={"remote": {"url": ROOT}},
                  data=pd.DataFrame(columns=["path", "checksum"]),
                  tables={"more.tsv": pd.DataFrame({"path": ["a.h5"]})})
    assert materialize_sources(state) == [SourceSpec("origin", "origin")]
    assert state.data.columns.tolist() == ["path", "checksum"]
    assert state.tables["more.tsv"]["catalog_source"].tolist() == ["origin"]


def test_materialize_twice_changes_nothing_the_second_time():
    state = legacy_with(remote={"url": ROOT})
    state.tables = {"more.tsv": pd.DataFrame({"path": ["b.h5"]})}
    sources = materialize_sources(state)
    before = copy.deepcopy(state)
    assert materialize_sources(state) == sources
    assert state.config == before.config
    pd.testing.assert_frame_equal(state.data, before.data)
    pd.testing.assert_frame_equal(state.tables["more.tsv"],
                                  before.tables["more.tsv"])


def test_materialize_leaves_recorded_sources_and_their_rows_alone():
    state = State(
        config={"remote": [{"name": "a", "url": ROOT},
                           {"name": "b", "url": "https://b.test/"}],
                "sources": [{"name": "a", "remote": "a"},
                            {"name": "b", "remote": "b"}]},
        data=pd.DataFrame({"path": ["x", "y"], "catalog_source": ["a", "b"]}))
    config = copy.deepcopy(state.config)
    assert [source.name for source in materialize_sources(state)] == ["a", "b"]
    assert state.config == config
    assert state.data["catalog_source"].tolist() == ["a", "b"]


def test_local_manifests_and_unconfigured_catalogues_have_no_sources():
    local = State(config={"data": [{"fmt": "{x}.h5"}]},
                  data=pd.DataFrame({"sha1": ["a" * 40], "x": ["1"]}))
    assert effective_sources(local) == []
    assert effective_sources(State()) == []


def test_rows_of_selects_rows_by_source():
    sources = [SourceSpec("a", "origin"), SourceSpec("b", "osn")]
    frame = pd.DataFrame({"path": ["1", "2", "3"], "catalog_source": ["a", "b", "a"]})
    assert rows_of(frame, sources[0], sources).tolist() == [True, False, True]


@pytest.mark.parametrize("count, expected", [(1, [True, True]), (2, [False, False])])
def test_rows_of_a_table_without_sources_belong_only_to_a_lone_source(count,
                                                                      expected):
    sources = [SourceSpec("a", "origin"), SourceSpec("b", "osn")][:count]
    frame = pd.DataFrame({"path": ["1", "2"]})
    assert rows_of(frame, sources[0], sources).tolist() == expected


@pytest.mark.parametrize("config, columns, remote", [
    ({}, ["path", "checksum"], True),
    ({}, ["sha1", "path", "x"], False),
    ({}, ["sha1", "x"], False),
    # A {source} field in a local pattern does not make a catalogue remote.
    ({}, ["path", "sha1", "source"], False),
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
