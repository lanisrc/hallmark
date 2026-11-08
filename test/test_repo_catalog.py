"""Catalogue helpers: TSV names, file: entries and the rows they describe."""

from types import SimpleNamespace

import pandas as pd
import pytest

from hallmark.repo.catalog import (
    ENTRY, SECTION, TABLE, catalog_rows, catalog_tables, entry_row, file_entries,
    row_entry, table_names)
from hallmark.repo.state import State

MD5, SHA1, SHA256, SHA512 = "a" * 32, "b" * 40, "c" * 64, "d" * 128


def row(path="a.txt", **values):
    """Return the row entry_row gives, empty except for the values given."""
    return {"path": path, "checksum_algorithm": "", "checksum": "", "size_bytes": "",
            "mtime": "", "catalog_source": "", "sha1": "", **values}


@pytest.mark.parametrize("config, expected", [
    ([], ["data.tsv"]),
    (None, ["data.tsv"]),
    ({}, ["data.tsv"]),
    ({"data": {"db": "north"}}, ["data.tsv", "north.tsv"]),
    ({"data": "north.tsv"}, ["data.tsv"]),
    ({"data": None}, ["data.tsv"]),
    ({"data": ["north", {"db": "south"}]}, ["data.tsv", "south.tsv"]),
    ({"data": [{"file": "a.txt", "db": "north"}]}, ["data.tsv"]),
    ({"data": [{"file": "", "db": "north"}]}, ["data.tsv", "north.tsv"]),
    ({"data": [{"fmt": "{x}.h5"}]}, ["data.tsv"]),
    ({"data": [{"db": "north"}]}, ["data.tsv", "north.tsv"]),
    ({"data": [{"db": "north"}, {"db": "north.tsv", "fmt": "{x}.h5"},
               {"db": " north "}]}, ["data.tsv", "north.tsv"]),
    ({"data": [{"db": "north", "fmt": "{x}.h5"}, {"fmt": "{y}.fits"},
               {"db": "data.tsv"}]}, ["data.tsv", "north.tsv"]),
])
def test_table_names_lists_each_data_tsv_once_with_data_tsv_first(config, expected):
    assert table_names(config) == expected


@pytest.mark.parametrize("data, clash", [
    ([{"db": "North", "fmt": "{x}.h5"}, {"db": "north", "fmt": "{y}.h5"}],
     "'North.tsv' and 'north.tsv'"),
    ([{"db": "DATA.TSV", "fmt": "{x}.h5"}], "'data.tsv' and 'DATA.TSV'"),
])
def test_table_names_that_differ_only_in_case_are_refused(data, clash):
    with pytest.raises(ValueError,
                       match=f"TSV names {clash} differ only in letter case"):
        table_names({"data": data})


@pytest.mark.parametrize("db", [5, float("nan"), ["north"]])
def test_a_table_name_must_be_text(db):
    with pytest.raises(ValueError, match="TSV database name must be a string"):
        table_names({"data": [{"db": db, "fmt": "{x}.h5"}]})


@pytest.mark.parametrize("tables, expected", [
    (None, ["data.tsv"]),
    ({}, ["data.tsv"]),
    ({"south.tsv": pd.DataFrame(), "north.tsv": pd.DataFrame()},
     ["data.tsv", "south.tsv", "north.tsv"]),
])
def test_catalog_tables_gives_the_state_frames_with_data_tsv_first(tables, expected):
    data = pd.DataFrame({"path": ["a.txt"]})
    state = (SimpleNamespace(data=data) if tables is None
             else SimpleNamespace(data=data, tables=tables))
    found = catalog_tables(state)
    assert list(found) == expected
    assert found["data.tsv"] is data
    assert all(found[name] is tables[name] for name in expected[1:])


@pytest.mark.parametrize("config, expected", [
    ([], []),
    (None, []),
    ({"meta": {"file": "README.md"}}, [("meta", {"file": "README.md"})]),
    ({"data": [{"file": ""}, {"file": None}, {"file": 0}, {"file": "a.txt"}]},
     [("data", {"file": "a.txt"})]),
    ({"data": ["a.txt", {"db": "north"}, {"file": "b.txt", "db": "north"}]},
     [("data", {"file": "b.txt", "db": "north"})]),
    ({"meta": [{"file": "README.md"}], "data": [{"file": "a.txt"}]},
     [("data", {"file": "a.txt"}), ("meta", {"file": "README.md"})]),
])
def test_file_entries_lists_data_entries_then_meta_entries(config, expected):
    assert file_entries(config) == expected


@pytest.mark.parametrize("entry, expected", [
    ({"file": "a.txt", "md5": MD5, "sha256": SHA256, "sha512": SHA512},
     row(checksum_algorithm="sha512", checksum=SHA512)),
    ({"file": "a.txt", "md5": MD5, "sha256": SHA256},
     row(checksum_algorithm="sha256", checksum=SHA256)),
    ({"file": "a.txt", "sha512": "", "sha256": SHA256, "md5": MD5},
     row(checksum_algorithm="sha256", checksum=SHA256)),
    ({"file": "a.txt", "sha512": float("nan"), "sha256": None, "md5": MD5},
     row(checksum_algorithm="md5", checksum=MD5)),
    ({"file": "a.txt", "sha1": SHA1}, row(sha1=SHA1)),
    ({"file": "a.txt", "sha1": SHA1, "md5": MD5},
     row(checksum_algorithm="md5", checksum=MD5, sha1=SHA1)),
    ({"file": "a.txt", "size_bytes": 12.0, "mtime": 1700000000,
      "catalog_source": "007"},
     row(size_bytes="12", mtime="1700000000", catalog_source="007")),
])
def test_an_entry_row_catalogues_the_strongest_checksum_other_than_sha1(
        entry, expected):
    assert entry_row(entry) == expected


@pytest.mark.parametrize("found, name, expected", [
    ({"path": "a.txt"}, "", {"file": "a.txt"}),
    ({"path": "a.txt"}, "readme", {"name": "readme", "file": "a.txt"}),
    ({"path": "a.txt", "checksum_algorithm": "SHA256", "checksum": SHA256}, None,
     {"file": "a.txt", "sha256": SHA256}),
    ({"path": "a.txt", "checksum_algorithm": "md5", "checksum": ""}, None,
     {"file": "a.txt"}),
    ({"path": "a.txt", "size_bytes": "12", "mtime": 1700000000.0}, None,
     {"file": "a.txt", "size_bytes": 12, "mtime": 1700000000}),
    ({"path": "a.txt", "size_bytes": "1.5", "mtime": "2026-01-01T00:00:00Z"}, None,
     {"file": "a.txt", "size_bytes": "1.5", "mtime": "2026-01-01T00:00:00Z"}),
    ({"path": "a.txt", "catalog_source": "007"}, None,
     {"file": "a.txt", "catalog_source": "007"}),
    ({"path": "a.txt", "size_bytes": "١٢", "mtime": "²"}, None,
     {"file": "a.txt", "size_bytes": "١٢", "mtime": "²"}),
    ({"path": "a.txt", "size_bytes": "", "mtime": float("nan"),
      "catalog_source": None, "sha1": ""}, None, {"file": "a.txt"}),
])
def test_a_row_entry_keeps_only_set_values_and_integer_sizes_and_mtimes(
        found, name, expected):
    assert row_entry(found, name=name) == expected


@pytest.mark.parametrize("algorithm, checksum, expected", [
    ("", MD5, {"md5": MD5}),
    ("unknown", SHA256, {"sha256": SHA256}),
    ("", "abc123", {}),
])
def test_a_row_entry_names_an_unlabelled_checksum_by_its_length(
        algorithm, checksum, expected):
    row = {"path": "a.txt", "checksum_algorithm": algorithm, "checksum": checksum}
    assert row_entry(row) == {"file": "a.txt", **expected}


def test_a_sha256_row_survives_the_round_trip_through_an_entry():
    found = row(checksum_algorithm="sha256", checksum=SHA256, size_bytes="12",
                mtime="1700000000", catalog_source="007", sha1=SHA1)
    assert entry_row(row_entry(found)) == found


@pytest.mark.parametrize("state, expected", [
    (State(), []),
    (State(tables={"north.tsv": pd.DataFrame({"sha1": [SHA1]})}), []),
    (SimpleNamespace(config=None, data=pd.DataFrame({"sha1": [SHA1]})), []),
    (State(data=pd.DataFrame({"sha1": [SHA1]}),
           tables={"north.tsv": pd.DataFrame({"path": ["n.h5"]})}),
     [{"path": "n.h5", TABLE: "north.tsv"}]),
], ids=["fresh state", "table without paths", "no tables attribute",
        "data.tsv without paths, then a table with paths"])
def test_catalog_rows_skips_tables_without_a_path_column(state, expected):
    assert catalog_rows(state) == expected


def test_catalog_rows_lists_tsv_rows_then_data_entries_then_meta_entries():
    readme, notes = {"file": "README.md"}, {"file": "notes.txt", "md5": MD5}
    state = State(
        config={"meta": [readme], "data": [{"db": "north", "fmt": "{x}.h5"}, notes]},
        data=pd.DataFrame({"path": ["a.h5", "b.h5"], "size_bytes": [None, 1.0]}),
        tables={"north.tsv": pd.DataFrame({"path": ["n.h5"], "sha1": [SHA1]})})
    rows = catalog_rows(state)
    assert rows == [
        {"path": "a.h5", "size_bytes": "", TABLE: "data.tsv"},
        {"path": "b.h5", "size_bytes": "1", TABLE: "data.tsv"},
        {"path": "n.h5", "sha1": SHA1, TABLE: "north.tsv"},
        {**row("notes.txt", checksum_algorithm="md5", checksum=MD5),
         SECTION: "data", ENTRY: notes},
        {**row("README.md"), SECTION: "meta", ENTRY: readme},
    ]
    assert rows[3][ENTRY] is notes
    assert rows[4][ENTRY] is readme
