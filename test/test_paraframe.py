import pickle
import re
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd
from hallmark import ParaFrame
import pytest


@pytest.fixture
def create_temp_data(tmp_path):
    root = tmp_path / "data"
    for a in range(10):
        directory = root / f"a_{a}"
        directory.mkdir(parents=True)
        for b in range(1, 11):
            (directory / f"b_{b}.txt").write_text("data\n", encoding="utf-8")
    return root


def test_paraframe_class_functionality(create_temp_data):
    # a user wants to create a paraframe
    fmt = "a_{a:d}/b_{b:d}.txt"
    pf = ParaFrame.parse(fmt, base_path=create_temp_data)
    assert all("\\" not in path for path in pf["path"]), \
        "All paths should use forward slashes."

    # users wants to filter files to see those with a = 0
    scalar_filter = pf(a=0)
    # checking that the correct number of files and column values are filtered
    assert len(scalar_filter) == 10
    assert scalar_filter["a"].unique().tolist() == [0]

    # users wants to filter files to see those with a = 0 or 1
    list_filter = pf(a=[0,1])
    # checking that the correct number of files and column values are filtered
    assert len(list_filter) == 20
    assert sorted(list_filter["a"].unique().tolist()) == [0, 1]

    # users want to filter files to see those with a = 0 or b = 10
    tuple_filter = pf(a=0, b=10)
    # splitting columns with a=0 value and b=10 value to check that the
    # correct number of files and column values are filtered
    a_filter = tuple_filter(a=0)
    b_filter = tuple_filter(b = 10)
    assert len(tuple_filter) == 19
    assert len(a_filter) == 10
    assert len(set(a_filter["a"])) == 1
    assert len(b_filter) == 10
    assert len(set(b_filter["b"])) == 1

    # users want to filter files to see those with a=0 and b=10
    and_filter = pf(a=0)(b=10)
    assert len(and_filter) == 1
    assert len(set(and_filter["a"])) == 1
    assert len(set(and_filter["b"])) == 1
    assert all(and_filter["a"] == [0])
    assert all(and_filter["b"] == [10])

    # users want to filter files to see those with a >= 1 and a <=4
    mask_filter = pf[(1 <= pf.a) & (pf.a <= 4)]
    assert len(mask_filter) == 40
    assert sorted(mask_filter["a"].unique().tolist()) == [1, 2, 3, 4]


def test_debug(create_temp_data, capsys):
    # users want to see a detailed summary of how ParaFrame utilizes globbing
    fmt = "a_{a:d}/b_{b:d}.txt"
    pf = ParaFrame.parse(fmt, base_path=create_temp_data, debug=True)
    captured = capsys.readouterr()

    assert len(pf) == 100
    assert f'Pattern: "{create_temp_data}/a_*/b_*.txt"' in captured.out
    assert "100 matches" in captured.out


### filter tests ###

@pytest.mark.parametrize("make_value", [
    lambda: (1, 0),
    lambda: {0, 1},
    lambda: frozenset({0, 1}),
    lambda: range(2),
    lambda: np.array([1, 0]),
    lambda: pd.Series([1, 0], index=[7, 8]),
    lambda: pd.Index([1, 0]),
    lambda: (value for value in (1, 0)),
], ids=["tuple", "set", "frozenset", "range", "ndarray", "series", "index",
        "generator"])
def test_filter_list_like_value_matches_any_of_its_elements(make_value):
    pf = ParaFrame({"path": ["p0", "p1", "p2"], "a": [0, 1, 2]})

    assert pf(a=make_value())["path"].tolist() == ["p0", "p1"]


@pytest.mark.parametrize("values,value", [
    (["a", "b", "ab"], "ab"),
    ([b"a", b"b", b"ab"], b"ab"),
], ids=["str", "bytes"])
def test_filter_text_value_is_a_scalar(values, value):
    pf = ParaFrame({"path": ["p0", "p1", "p2"], "kind": values})

    assert pf(kind=value)["path"].tolist() == ["p2"]


@pytest.mark.parametrize("value", [{0: "p0"}, MappingProxyType({0: "p0"})],
                         ids=["dict", "mapping"])
def test_filter_refuses_a_mapping_value(value):
    pf = ParaFrame({"path": ["p0", "p1"], "a": [0, 1]})

    with pytest.raises(ValueError, match="a= was given a mapping"):
        pf(a=value)


def test_filter_on_a_missing_column_raises_keyerror():
    pf = ParaFrame({"path": ["p0", "p1"], "a": [0, 1]})

    with pytest.raises(KeyError, match="zz"):
        pf(zz=1)


def test_filter_skips_missing_values_in_a_nullable_column():
    pf = ParaFrame({"path": ["p0", "p1", "p2"],
                    "a": pd.array([None, 1, 2], dtype="Int64")})

    assert pf(a=1)["path"].tolist() == ["p1"]


### metadata tests ###

@pytest.mark.parametrize("operation,paths,keeps_unmatched", [
    (lambda pf: pf(a=[1, 2]), ["a_1.txt", "a_2.txt"], True),
    (lambda pf: pf.iloc[1:], ["a_1.txt", "a_2.txt"], True),
    (lambda pf: pf.copy(), ["a_0.txt", "a_1.txt", "a_2.txt"], True),
    (lambda pf: pf.sort_values("a", ascending=False),
     ["a_2.txt", "a_1.txt", "a_0.txt"], True),
    # concat needs _constructor and pickle needs _metadata; whether attrs
    # survive them is pandas' rule, not ParaFrame's, so it is not checked
    (lambda pf: pd.concat([pf.iloc[:1], pf.iloc[2:]]), ["a_0.txt", "a_2.txt"],
     False),
    (lambda pf: pickle.loads(pickle.dumps(pf)), ["a_0.txt", "a_1.txt", "a_2.txt"],
     False),
], ids=["filter", "slice", "copy", "sort_values", "concat", "pickle"])
def test_pandas_operations_keep_the_paraframe_metadata(
        tmp_path, operation, paths, keeps_unmatched):
    _write_files(tmp_path, "a_0.txt", "a_1.txt", "a_2.txt", "a_x.txt")
    specs = [{"fmt": "a_{a:d}.txt", "encoding": {"aspin": r"m([0-9]+)"}}]
    pf = ParaFrame.parse("a_{a:d}.txt", base_path=tmp_path,
                         encodings=specs, encoding=True)

    result = operation(pf)

    assert type(result) is ParaFrame
    assert result["path"].tolist() == paths
    assert result.encodings == specs
    assert result.base_path == tmp_path
    if keeps_unmatched:
        assert result.attrs["unmatched"] == ["a_x.txt"]


def test_a_new_paraframe_defaults_to_no_encodings_and_the_current_folder(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    pf = ParaFrame({"a": [1]})

    assert pf.encodings == {}
    assert pf.base_path == Path.cwd()


def test_a_paraframe_stores_iterator_encodings_as_a_list():
    specs = [{"fmt": "a_{a}.txt", "encoding": {"aspin": r"m([0-9]+)"}}]

    pf = ParaFrame({"a": [1]}, encodings=iter(specs))

    assert pf.encodings == specs


#### glob_search tests ###

def test_glob_search_replaces_a_missing_field_with_a_wildcard(tmp_path):
    """
    Test that ParaFrame.glob_search turns a field given no value into the glob
    wildcard ``*``, so every file in the folder is found.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    matches, pattern = ParaFrame.glob_search(
        "{a}",
        base_path=tmp_path,
        return_pattern=True)

    assert pattern == str(tmp_path / "*"), \
        f"Expected pattern: {tmp_path / '*'}, but got: {pattern}"
    assert matches == sorted([str(first), str(second)]), \
        f"Expected matches: {[str(first), str(second)]}, but got: {matches}"


def test_glob_search_excludes_directories(tmp_path):
    """
    Test that ParaFrame.glob_search correctly excludes directories from the
    results.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    """
    matching_file = tmp_path / "item_file"
    matching_file.write_text("contents\n", encoding="utf-8")
    matching_directory = tmp_path / "item_directory"
    matching_directory.mkdir()
    matches, _ = ParaFrame.glob_search(
        "item_{kind}",
        base_path=tmp_path,
        return_pattern=True)

    assert matches == [str(matching_file)], \
        f"Expected only the matching file, but got: {matches}"


def test_glob_search_rejects_nonmapping_encoding_entries(tmp_path):
    """
    Test that ParaFrame.glob_search raises a ValueError when the encoding
    specifications
    provided are not dictionaries.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    Raises:
        ValueError: If the encoding specifications are not dictionaries.
    """
    with pytest.raises(ValueError, match="only dictionaries"):
        ParaFrame.glob_search(
            "a_{spin}.dat", base_path=tmp_path, encodings=["invalid"], encoding=True)


def test_glob_search_encoding_true_rejects_noniterable_encodings(tmp_path):
    """
    Test that ParaFrame.glob_search raises when encodings is neither mapping nor
    iterable of mappings.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    Raises:
        ValueError: If encodings is not a mapping or sequence of mappings.
    """
    with pytest.raises(ValueError, match="mapping or sequence of mappings"):
        ParaFrame.glob_search(
            "a_{n}.dat",
            base_path=tmp_path,
            encodings=123,
            encoding=True)


def test_glob_search_takes_a_base_path_with_glob_characters_literally(tmp_path):
    _write_files(tmp_path, "run[1]/a_1.txt", "run1/a_2.txt")

    matches, _ = ParaFrame.glob_search(
        "a_{a}.txt", base_path=tmp_path / "run[1]", return_pattern=True)

    assert matches == [str(tmp_path / "run[1]" / "a_1.txt")]


def test_glob_search_strips_the_leading_slash_and_every_format_spec(tmp_path):
    _write_files(tmp_path, "x_005_q.h5", "x_012_q.h5")

    yaml_encodings, fmt_g, matches = ParaFrame.glob_search(
        "/x_{doy:03d}_{a}.h5", base_path=tmp_path, doy=5)

    assert yaml_encodings == {}
    assert fmt_g == "x_{doy}_{a}.h5"
    assert matches == [str(tmp_path / "x_005_q.h5")]


def test_parse_and_glob_search_default_to_the_current_folder(tmp_path, monkeypatch):
    _write_files(tmp_path, "a_1.txt", "sub/a_2.txt")
    monkeypatch.chdir(tmp_path)

    matches, _ = ParaFrame.glob_search("a_{a}.txt", return_pattern=True)
    frame = ParaFrame.parse("a_{a}.txt")

    assert matches == [str(Path.cwd() / "a_1.txt")]
    assert frame["path"].tolist() == ["a_1.txt"]
    assert frame.base_path == Path.cwd()


### parse tests ###

def test_parse_accepts_single_encoding_mapping(tmp_path):
    """
    Test that ParaFrame.parse correctly accepts a single encoding
    mapping and extracts
    the expected parameter values from the file names.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    """
    data_path = tmp_path / "a_m0.5.dat"
    data_path.write_text("contents\n", encoding="utf-8")
    encoding = {"fmt": "a_{aspin}.dat",
                "encoding": {"aspin": r"m([0-9]+(?:\.[0-9]+)?)"}}
    frame = ParaFrame.parse(
        "a_{aspin}.dat", base_path=tmp_path, encodings=encoding, encoding=True)

    assert frame["aspin"].tolist() == [-0.5], \
        f"Expected spin value: [-0.5], but got: {frame['aspin'].tolist()}"


def test_parse_reads_encodings_given_as_a_generator(tmp_path):
    """Encodings that can be read only once still select the typed format."""
    (tmp_path / "am0.5_i30.h5").write_text("contents\n", encoding="utf-8")
    (tmp_path / "am1_i30.h5").write_text("contents\n", encoding="utf-8")
    specs = [{"fmt": "a{aspin:f}_i{i:d}.h5", "encoding": {"aspin": "m([0-9.]+)"}}]

    frame = ParaFrame.parse("a{aspin:f}_i{i:d}.h5", base_path=tmp_path,
                            encodings=(spec for spec in specs), encoding=True)

    assert frame.to_dict(orient="records") == [
        {"path": "am0.5_i30.h5", "aspin": -0.5, "i": 30}]
    assert frame.attrs["unmatched"] == ["am1_i30.h5"]
    assert frame.encodings == specs


def test_parse_encoding_false_ignores_invalid_encoding_specs(tmp_path):
    """
    Test that ParaFrame.parse ignores encoding spec validation when
    encoding=False.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    """
    data_path = tmp_path / "a_1.dat"
    data_path.write_text("contents\n", encoding="utf-8")
    frame = ParaFrame.parse(
        "a_{n}.dat",
        base_path=tmp_path,
        encodings=["not-a-mapping"],
        encoding=False)

    assert frame["n"].tolist() == [1], \
        f"Expected parsed value [1], but got: {frame['n'].tolist()}"


def test_parse_encoding_true_requires_matching_fmt_spec(tmp_path):
    """
    Test that ParaFrame.parse raises when encoding=True and no matching
    fmt encoding
    entry exists.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    Raises:
        ValueError: If no matching fmt encoding entry exists when encoding=True.
    """
    with pytest.raises(ValueError, match="missing from config.yml"):
        ParaFrame.parse(
            "a_{n}.dat",
            base_path=tmp_path,
            encodings=[{
                "fmt": "different_{n}.dat",
                "encoding": {"n": r"[0-9]+"}}],
            encoding=True)


def test_parse_encoding_true_rejects_nonstring_patterns(tmp_path):
    """
    Test that ParaFrame.parse raises when an encoding regex value is not
    a string.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
    Raises:
        ValueError: If an encoding regex value is not a string.
    """
    with pytest.raises(ValueError, match="must be strings"):
        ParaFrame.parse(
            "a_{n}.dat",
            base_path=tmp_path,
            encodings={
                "fmt": "a_{n}.dat",
                "encoding": {"n": 123}},
            encoding=True)


def test_parse_skips_files_that_fail_to_match_after_encoding(tmp_path, capsys):
    """
    Test that ParaFrame.parse skips a globbed file whose encoded name no longer
    matches the parse template, rather than raising, lists it as unmatched and,
    with debug=True, reports it.
    Args:
        tmp_path (Path): Temporary directory provided by pytest for testing.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    data_path = tmp_path / "a_qq.dat"
    data_path.write_text("contents\n", encoding="utf-8")
    # this "aspin" regex rewrites the literal "a_" prefix itself, so the encoded
    # name no longer matches the "a_{n}.dat" parser template at all; it is keyed
    # "aspin" because that is the only key apply_regex_replacement applies
    encoding = {"fmt": "a_{n}.dat", "encoding": {"aspin": r"(a)_"}}

    frame = ParaFrame.parse(
        "a_{n}.dat", base_path=tmp_path, encodings=encoding, encoding=True,
        debug=True)

    assert frame.empty, \
        f"Expected no rows since the encoded name no longer matches, got {frame}"
    assert frame.attrs["unmatched"] == ["a_qq.dat"]
    captured = capsys.readouterr()
    assert f'Failed to parse "{data_path}"' in captured.out, \
        f"Expected a 'Failed to parse' message, got: {captured.out!r}"


def _write_files(root, *names):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data\n", encoding="utf-8")


def test_parse_field_name_prefix_of_another(tmp_path):
    """A field whose name begins another field's name leaves that field intact."""
    _write_files(tmp_path, "run1/run1_a.h5", "run2/run2_b.h5")

    frame = ParaFrame.parse("run{run}/run{run}_{run_id}.h5", base_path=tmp_path)

    assert frame["path"].tolist() == ["run1/run1_a.h5", "run2/run2_b.h5"]
    assert list(frame.columns) == ["path", "run", "run_id"]
    assert frame["run"].tolist() == [1, 2]
    assert frame["run_id"].tolist() == ["a", "b"]


def test_parse_prefix_field_kwarg_filters(tmp_path, capsys):
    """A value given for {ab} selects files even when {a} is also a field."""
    _write_files(tmp_path, "x_y.txt", "p_q.txt")

    frame = ParaFrame.parse("{ab}_{a}.txt", base_path=tmp_path, ab="x", debug=True)

    assert frame["path"].tolist() == ["x_y.txt"]
    assert list(frame.columns) == ["path", "ab", "a"]
    assert f'Pattern: "{tmp_path / "x_*.txt"}"' in capsys.readouterr().out


@pytest.mark.parametrize("fmt,field", [
    ("data/a_{}/b.txt", "an empty field"),
    ("data/a_{}/b_{b:d}.txt", "an empty field"),
    ("data/a_{0}/b_{b:d}.txt", "the field {0}"),
])
def test_parse_unnamed_field_raises_valueerror(tmp_path, fmt, field):
    _write_files(tmp_path, "data/a_1/b.txt", "data/a_1/b_10.txt")

    with pytest.raises(ValueError) as excinfo:
        ParaFrame.parse(fmt, base_path=tmp_path)

    assert str(excinfo.value) == (
        f"Format fields need names, as in {{run}}; {fmt!r} has {field}")


@pytest.mark.parametrize("fmt,message", [
    ("a_{a.x}.txt", "must be identifiers, as in {run_id}; 'a_{a.x}.txt' has {a.x}"),
    ("a_{a[0]}.txt", "must be identifiers, as in {run_id}; 'a_{a[0]}.txt' has {a[0]}"),
    ("a_{ a }.txt", "must be identifiers, as in {run_id}; 'a_{ a }.txt' has { a }"),
    ("a_{a!r}.txt", "cannot convert values; 'a_{a!r}.txt' has {a!r}"),
    ("a_{a:{w}d}.txt", "cannot contain fields; 'a_{a:{w}d}.txt' has {a:{w}d}"),
    ("a_{a:q}.txt", "format spec 'q' not recognised"),
])
def test_parse_rejects_fields_that_are_not_plain_names(tmp_path, fmt, message):
    _write_files(tmp_path, "a_1.txt")

    with pytest.raises(ValueError, match=re.escape(message)):
        ParaFrame.parse(fmt, base_path=tmp_path)


def test_parse_positional_values_fill_unnamed_fields(tmp_path):
    _write_files(tmp_path, "a_1_x.txt", "a_2_x.txt")

    frame = ParaFrame.parse("a_{}_{c}.txt", 1, base_path=tmp_path)

    assert frame["path"].tolist() == ["a_1_x.txt"]
    with pytest.raises(ValueError, match="more unnamed fields than the 1 positional"):
        ParaFrame.parse("a_{}_{}.txt", 1, base_path=tmp_path)


def test_parse_enforces_type_spec(tmp_path, capsys):
    """A name whose value does not fit the field's type is unmatched, quietly."""
    _write_files(tmp_path, "a_1.txt", "a_x.txt")

    frame = ParaFrame.parse("a_{a:d}.txt", base_path=tmp_path)

    assert frame["path"].tolist() == ["a_1.txt"]
    assert frame["a"].tolist() == [1]
    assert frame.attrs["unmatched"] == ["a_x.txt"]
    assert capsys.readouterr().out == ""


def test_parse_is_case_sensitive(tmp_path):
    _write_files(tmp_path, "q_X_x1.txt", "q_xA_X1.txt")

    frame = ParaFrame.parse("{a}_x{b:d}.txt", base_path=tmp_path)

    assert frame.to_dict(orient="records") == [
        {"path": "q_X_x1.txt", "a": "q_X", "b": 1}]
    assert frame.attrs["unmatched"] == ["q_xA_X1.txt"]


def test_parse_keyword_for_a_field_with_a_spec_keeps_the_literal_text(tmp_path):
    _write_files(tmp_path, "x_005.h5", "x_012.h5")

    everything = ParaFrame.parse("x_{doy:03d}.h5", base_path=tmp_path)
    selected = ParaFrame.parse("x_{doy:03d}.h5", base_path=tmp_path, doy=5)

    assert selected.to_dict(orient="records") == [
        {"path": "x_005.h5", "doy": "005"}]
    assert selected["doy"].dtype == everything["doy"].dtype


@pytest.mark.parametrize("fmt", ["b_{a}_{b:d}_{a}_{}.txt", "b_{a}_{b:d}_{a}_{0}.txt"])
def test_parse_with_no_match_keeps_the_path_and_field_columns(tmp_path, fmt):
    _write_files(tmp_path, "b_x_q_x_1.txt")

    frame = ParaFrame.parse(fmt, 1, base_path=tmp_path)

    assert frame.empty
    assert list(frame.columns) == ["path", "a", "b"]
    assert frame.dtypes.tolist() == [object, object, object]
    assert frame.attrs["unmatched"] == ["b_x_q_x_1.txt"]
    selected = frame(a=1)
    assert selected.empty
    assert list(selected.columns) == ["path", "a", "b"]


@pytest.mark.parametrize("encodings", [
    [{"fmt": "a_{aspin}.dat", "encoding": {"aspin": r"m([0-9]+(?:\.[0-9]+)?)"}}],
    [{"fmt": "a_{aspin}.dat", "encoding": {"aspin": r"p([0-9]+)"}},
     {"fmt": "prefix_a_{aspin}.dat",
      "encoding": {"aspin": r"m([0-9]+(?:\.[0-9]+)?)"}}],
], ids=["narrower entry only", "narrower entry before the exact one"])
def test_parse_with_a_narrower_encoding_entry_reads_the_full_format(
        tmp_path, encodings):
    _write_files(tmp_path, "prefix_a_m0.5.dat")

    frame = ParaFrame.parse("prefix_a_{aspin}.dat", base_path=tmp_path,
                            encodings=encodings, encoding=True)

    assert frame.to_dict(orient="records") == [
        {"path": "prefix_a_m0.5.dat", "aspin": -0.5}]
    assert frame.attrs["unmatched"] == []


### _find_encoding_settings tests ###

def test_resolve_encoding_spec_narrows_to_matching_entry_fmt():
    """
    Test that _find_encoding_settings uses a narrower entry "fmt" as fmt_enc when
    that entry's fmt is found within the full searched fmt.
    """
    encodings = [{"fmt": "a_{aspin}.dat", "encoding": {"aspin": r"m([0-9]+)"}}]
    yaml_encodings, fmt_enc = ParaFrame._find_encoding_settings(
        "prefix_a_{aspin}.dat", encodings, encoding=True)

    assert fmt_enc == "a_{aspin}.dat", \
        f"Expected fmt_enc to narrow to the matching entry's fmt, got {fmt_enc!r}"
    assert yaml_encodings == encodings[0], \
        f"Expected the matching encoding spec, got {yaml_encodings}"


def test_resolve_encoding_spec_rejects_nondict_encoding_value():
    """
    Test that _find_encoding_settings raises when an encoding spec's "encoding"
    value is not itself a dictionary.
    Raises:
        ValueError: If the "encoding" value in the matching encoding spec is not a
            dictionary.
    """
    encodings = {"fmt": "a_{n}.dat", "encoding": "not-a-dict"}

    with pytest.raises(ValueError, match="must be a dictionary"):
        ParaFrame._find_encoding_settings("a_{n}.dat", encodings, encoding=True)


def test_resolve_encoding_spec_rejects_empty_regex_spec():
    """
    Test that _find_encoding_settings raises when the matching encoding spec has
    no non-empty regex patterns.
    Raises:
        ValueError: If the matching encoding spec has no non-empty regex patterns.
    """
    encodings = {"fmt": "a_{n}.dat", "encoding": {"n": ""}}

    with pytest.raises(ValueError, match="has no regex spec"):
        ParaFrame._find_encoding_settings("a_{n}.dat", encodings, encoding=True)


def test_resolve_encoding_spec_requires_encodings_when_encoding_true():
    """
    Test that _find_encoding_settings raises when encoding=True but no encodings
    were provided at all.
    Raises:
        ValueError: If encoding=True but no encodings were provided.
    """
    with pytest.raises(ValueError, match="missing from config.yml"):
        ParaFrame._find_encoding_settings("a_{n}.dat", None, encoding=True)


SPIN = {"aspin": r"m([0-9]+)"}


@pytest.mark.parametrize("unusable", [
    {"fmt": "", "encoding": SPIN},
    {"fmt": 123, "encoding": SPIN},
    {"encoding": SPIN},
], ids=["empty fmt", "non-string fmt", "no fmt"])
@pytest.mark.parametrize("fmt", ["a_{aspin}.dat", "prefix_a_{aspin}.dat"],
                         ids=["exact", "narrower"])
def test_resolve_encoding_spec_skips_entries_without_a_usable_fmt(unusable, fmt):
    usable = {"fmt": "a_{aspin}.dat", "encoding": SPIN}

    yaml_encodings, fmt_enc = ParaFrame._find_encoding_settings(
        fmt, [unusable, usable], encoding=True)

    assert yaml_encodings is usable
    assert fmt_enc == "a_{aspin}.dat"


@pytest.mark.parametrize("fmt,entry_fmts", [
    ("prefix_a_{n}.dat", ["a_{n}.dat", "prefix_a_{n}.dat"]),
    ("/a_{n}.dat", ["{n}.dat", "a_{n}.dat"]),
    ("a_{n}.dat", ["{n}.dat", "/a_{n}.dat"]),
], ids=["same text", "searched with a leading slash",
        "entry with a leading slash"])
def test_resolve_encoding_spec_prefers_the_entry_for_fmt_itself(fmt, entry_fmts):
    encodings = [{"fmt": entry_fmt, "encoding": SPIN} for entry_fmt in entry_fmts]

    yaml_encodings, fmt_enc = ParaFrame._find_encoding_settings(
        fmt, encodings, encoding=True)

    assert yaml_encodings is encodings[1]
    assert fmt_enc == entry_fmts[1]


def test_resolve_encoding_spec_without_an_encoding_key_has_no_regex_spec():
    with pytest.raises(ValueError, match=re.escape("'a_{n}.dat' has no regex spec")):
        ParaFrame._find_encoding_settings(
            "a_{n}.dat", [{"fmt": "a_{n}.dat"}], encoding=True)


@pytest.mark.parametrize("encodings", [[], {}], ids=["empty list", "empty mapping"])
def test_resolve_encoding_spec_with_no_entries_names_the_missing_format(encodings):
    with pytest.raises(ValueError, match=re.escape(
            "The format 'a_{n}.dat' is missing from config.yml")):
        ParaFrame._find_encoding_settings("a_{n}.dat", encodings, encoding=True)


def test_resolve_encoding_spec_refuses_a_string():
    with pytest.raises(ValueError, match="only dictionaries"):
        ParaFrame._find_encoding_settings("a_{n}.dat", "a_{n}.dat", encoding=True)


def test_resolve_encoding_spec_with_encoding_false_does_not_read_encodings():
    class Unreadable:
        def __iter__(self):
            raise AssertionError("encodings were read")

    assert ParaFrame._find_encoding_settings(
        "/a_{n}.dat", Unreadable(), encoding=False) == ({}, "/a_{n}.dat")
