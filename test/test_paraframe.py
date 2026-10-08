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


def test_debug(create_temp_data, capsys, tmp_path):
    # users want to see a detailed summary of how ParaFrame utilizes globbing
    fmt = "a_{a:d}/b_{b:d}.txt"
    ParaFrame.parse(fmt, base_path=create_temp_data, debug=True)
    captured = capsys.readouterr()
    print(captured.out)
    expected = (
        '0 ' + str(tmp_path) + '/data/a_{a:d}/b_{b:d}.txt () {}\n' +
        "1 " + str(tmp_path) + "/data/a_{a}/b_{b:d}.txt () {'a': '*'}\n" +
        "2 " + str(tmp_path) + "/data/a_{a}/b_{b}.txt () {'a': '*', 'b': '*'}\n" +
        'Pattern: "' + str(tmp_path) + '/data/a_*/b_*.txt"\n' +
        '100 matches, e.g., "' + str(tmp_path) + '/data/a_0/b_1.txt"\n'
    )
    assert captured.out == expected


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
    Test that ParaFrame.parse silently skips (and reports) a globbed
    file whose
    encoded name no longer matches the parse template, rather than raising.
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
        "a_{n}.dat", base_path=tmp_path, encodings=encoding, encoding=True)

    assert frame.empty, \
        f"Expected no rows since the encoded name no longer matches, got {frame}"
    captured = capsys.readouterr()
    assert "Failed to parse" in captured.out, \
        f"Expected a 'Failed to parse' message, got: {captured.out!r}"


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