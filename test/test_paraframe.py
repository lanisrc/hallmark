import re

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
    assert scalar_filter["a"].unique() == 0

    # users wants to filter files to see those with a = 0 or 1
    list_filter = pf(a=[0,1])
    # checking that the correct number of files and column values are filtered
    assert len(list_filter) == 20
    assert all(list_filter["a"].unique() == [0,1])

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
    assert all(mask_filter["a"].unique() == [1,2,3,4])


def test_debug(create_temp_data, capsys):
    # users want to see a detailed summary of how ParaFrame utilizes globbing
    fmt = "a_{a:d}/b_{b:d}.txt"
    pf = ParaFrame.parse(fmt, base_path=create_temp_data, debug=True)
    captured = capsys.readouterr()

    assert len(pf) == 100
    assert f'Pattern: "{create_temp_data}/a_*/b_*.txt"' in captured.out
    assert "100 matches" in captured.out


#### glob_search tests ###

def test_glob_search_applies_final_wildcard_for_short_format(tmp_path):
    """
    Test that ParaFrame.glob_search correctly applies a final wildcard when the
    provided format string is shorter than the maximum number of path components.
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
    with pytest.raises(ValueError, match="missing from hallmark.yml"):
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
    # name no longer matches the "a_{n}.dat" parser template at all
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
    with pytest.raises(ValueError, match="missing from hallmark.yml"):
        ParaFrame._find_encoding_settings("a_{n}.dat", None, encoding=True)
