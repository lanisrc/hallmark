from hallmark.repo.config import (
    KNOWN_FIELD_VALUES,
    _match_file_against_fmts,
    match_file)


### _match_file_against_fmts tests ###

def test_match_simple_clean_match():
    """
    Test that a simple clean match works correctly.
    """
    fmt_entries = [{"fmt": "a{a}_i{i}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("a0.75_i30.h5", fmt_entries)

    assert i == 0, f"unexpected index: {i}, expected 0"
    assert result == {"a": "0.75", "i": "30"}, \
        f"unexpected result: {result}, expected {{'a': '0.75', 'i': '30'}}"


def test_match_missing_field_is_dropped():
    """
    A field that is missing from the filename should be dropped from the
    result, rather than causing a failed match.
    """
    fmt_entries = [{"fmt": "a{a}_i{i}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("a0.75.h5", fmt_entries)

    assert i == 0, f"unexpected index: {i}, expected 0"
    assert result == {"a": "0.75"}, \
        f"unexpected result: {result}, expected {{'a': '0.75'}}"
    assert "i" not in result, f"unexpected result: {result}, expected 'i' to drop out"


def test_match_missing_field_behind_long_literal_is_dropped():
    """
    A field that is missing from the filename, but is behind a long distinctive literal,
    should be dropped from the result, rather than causing a failed match.
    """
    fmt_entries = [
        {"fmt": "prefix_long_literal_{opt}_suffix.txt", "db": "x.tsv"}]
    i, result = _match_file_against_fmts(
        "prefix_long_literal_suffix.txt", fmt_entries)
    assert i == 0, f"unexpected index: {i}, expected 0"
    assert "opt" not in result, \
        f"unexpected result: {result}, expected 'opt' to drop out"

    i, result = _match_file_against_fmts(
        "prefix_long_literal_extra_suffix.txt", fmt_entries)
    assert i == 0, f"unexpected index: {i}, expected 0"
    assert result == {"opt": "extra"}, \
        f"unexpected result: {result}, expected {{'opt': 'extra'}}"

    i, result = _match_file_against_fmts("unrelated_suffix.txt", fmt_entries)
    assert i is None and result is None, \
        f"unexpected match: {(i, result)}, expected no match"


def test_match_no_matching_fmt_returns_none():
    """
    A filename that doesn't match any of the provided formats should
    return None for both the index and the result.
    """
    fmt_entries = [{"fmt": "a{a}_i{i}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("completely_unrelated_file.xyz", fmt_entries)

    assert i is None, f"unexpected index: {i}, expected None"
    assert result is None, f"unexpected result: {result}, expected None"


def test_match_prefers_more_specific_fmt_over_looser_one():
    """
    When multiple formats could match a filename, the one with more
    specific literals should be preferred over a looser one.
    """
    fmt_entries = [
        {"fmt": "{a}-longtoken1-{b}-longtoken2-{c}-longtoken3-{d}",
         "db": "looks_specific.tsv"},
        {"fmt": "{x}.{y}", "db": "actually_correct.tsv"},
    ]
    i, result = _match_file_against_fmts("hello.world", fmt_entries)

    assert fmt_entries[i]["db"] == "actually_correct.tsv", \
        f"unexpected index: {i}, expected the second format to match"


def test_match_repeated_name_resolves_when_values_agree():
    """
    When a repeated name's occurrences genuinely agree, that name
    should be resolved to that value, rather than being dropped.
    """
    fmt = "{project_code}/group.uid{guid}.{author}.{track}-{source}-{project_code}" \
    "-{kind}.{format}"
    rel_path = "2016.V/group.uid_ABC.author.e17-3C279-2016.V-4fit.tgz"
    fmt_entries = [{"fmt": fmt, "db": "data.tsv"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)

    assert result is not None, \
        f"unexpected None result for {rel_path} with fmt {fmt_entries}"
    assert result["project_code"] == "2016.V", \
        f"unexpected project_code: {result.get('project_code')}, expected '2016.V'"


def test_match_repeated_name_drops_when_values_disagree():
    """
    When a repeated name's occurrences genuinely disagree, that name
    must be dropped, not silently resolved to the wrong value.
    """
    fmt = "{project_code}/group.uid{guid}.{author}.{track}-{source}-{project_code}" \
    "-{kind}.{format}"
    rel_path = "2016.V/group.uid_ABC.author.e17-3C279-othervalue-4fit.tgz"
    fmt_entries = [{"fmt": fmt, "db": "data.tsv"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)

    if result is not None:
        assert "project_code" not in result, f"unexpected project_code: \
            {result.get('project_code')}, expected it to be dropped due to disagreement"


def test_match_known_value_resolves_field_shift_ambiguity():
    """
    A field with a known-value constraint should be resolved to that
    value, rather than being dropped, even if that means the next field
    is shifted over to fill the gap.
    """
    assert "kind" in KNOWN_FIELD_VALUES, \
        f"unexpected KNOWN_FIELD_VALUES: {KNOWN_FIELD_VALUES}, expected 'kind' to be \
            a known-value field"
    fmt = "{track}-{obs_num}-{band}-{pointing}-{source}-{kind}.{format}"
    rel_path = "e18a24-1-b1-na-CYG-A-4fit.tar"
    fmt_entries = [{"fmt": fmt, "db": "data.tsv"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)
    assert result["track"] == "e18a24", \
        f"unexpected track: {result.get('track')}, expected 'e18a24'"
    assert result["obs_num"] == "1", \
        f"unexpected obs_num: {result.get('obs_num')}, expected '1'"
    assert result["band"] == "b1", \
        f"unexpected band: {result.get('band')}, expected 'b1'"
    assert result["pointing"] == "na", \
        f"unexpected pointing: {result.get('pointing')}, expected 'na'"
    assert result["source"] == "CYG-A", \
        f"unexpected source: {result.get('source')}, expected 'CYG-A'"
    assert result["kind"] == "4fit", \
        f"unexpected kind: {result.get('kind')}, expected '4fit'"


def test_match_known_value_field_is_never_dropped():
    """
    A field with a known-value constraint should never be dropped, even
    if the next field is missing and would otherwise cause a shift.
    """
    fmt = "{scope}-processing-logs.{format}"
    fmt_entries = [{"fmt": fmt, "db": "log.tsv"}]
    i, result = _match_file_against_fmts("README.txt", fmt_entries)

    assert result is None, f"unexpected result: {result}, expected None because \
        'README.txt' does not match the known-value field 'scope'"


def test_match_long_literal_not_silently_cleared():
    """
    Regression test: a long distinctive literal should not be silently cleared
    """
    fmt_entries = [
        {"fmt": "{scope}-processing-logs.{format}", "db": "log.tsv"},]
    i, result = _match_file_against_fmts("EHTmetadata_2018April_v1.2.tar.gz",
                                         fmt_entries)

    assert result is None, f"unexpected result: {result}, expected None because the \
        long distinctive literal '-processing-logs.' should not be clearable"


def test_match_genuine_log_file_still_matches():
    """
    A genuine log file that matches the format should still match,
    even though the format has a long distinctive literal that is not
    clearable.
    """
    fmt_entries = [
        {"fmt": "{scope}-processing-logs.{format}", "db": "log.tsv"},]
    i, result = _match_file_against_fmts("na-processing-logs.tgz", fmt_entries)

    assert result == {"scope": "na", "format": "tgz"}, \
        f"unexpected result: {result}, expected {{'scope': 'na', 'format': 'tgz'}}"


def test_match_short_delimiter_still_clearable():
    """
    A short, generic delimiter (like "-") should still be clearable
    when its field is dropped -- only long, distinctive literals are
    restricted.
    """
    fmt_entries = [{"fmt": "a{a}-{b}.h5", "db": "x.tsv"}]
    i, result = _match_file_against_fmts("a0.75.h5", fmt_entries)

    assert result == {"a": "0.75"}, \
        f"unexpected result: {result}, expected {{'a': '0.75'}}"


def test_match_empty_fmt_entries_returns_none():
    """
    No format candidates should yield no match.
    """
    i, result = _match_file_against_fmts("any_file.txt", [])

    assert i is None and result is None, f"expected no match, got {(i, result)}"


def test_match_prefers_exacter_match_when_one_format_is_looser():
    """
    A more exact format should win over a looser competing format.
    """
    fmt_entries = [
        {"fmt": "{a}.{b}", "db": "loose.tsv"},
        {"fmt": "hello.{b}", "db": "exact.tsv"},]
    i, result = _match_file_against_fmts("hello.world", fmt_entries)

    assert i == 1, f"expected second format to win, got index {i}"
    assert result == {"b": "world"}, \
        f"unexpected result: {result}, expected {{'b': 'world'}}"


def test_match_typed_field_can_use_greedy_fallback():
    """
    A typed field (like {a:d}) should be able to use the greedy fallback search to
    match a number that is followed by a long literal.
    """
    fmt_entries = [{"fmt": "{a:d}-{label}.dat", "db": "data.tsv"}]
    index, result = _match_file_against_fmts("12-foo-bar.dat", fmt_entries)

    assert index == 0, f"unexpected index: {index}, expected 0"
    assert result == {"a": "12", "label": "foo-bar"}, \
        f"unexpected result: {result}, expected {{'a': '12', 'label': 'foo-bar'}}"


def test_matcher_runs_greedy_fallback_once_per_format(monkeypatch):
    """
    Test that the greedy fallback search is only run once per format, even if
    the first attempt fails to match.
    Args:
        monkeypatch: A pytest fixture for safely patching builtins and other objects.
    """
    calls = 0
    def fake_greedy_search(segments, rel_path):
        """A fake greedy search that always returns a match
        and increments a call counter."""
        nonlocal calls
        calls += 1
        return {"value": "a"}, 1
    monkeypatch.setattr("hallmark.repo.config._leak_score", lambda *args: 1)
    monkeypatch.setattr(
        "hallmark.repo.config._drop_and_greedy_search", fake_greedy_search)
    matched_index, values = _match_file_against_fmts(
        "x_a.dat", [{"fmt": "x_{value}.dat", "db": "data.tsv"}])

    assert matched_index == 0, f"unexpected matched_index: {matched_index}, expected 0"
    assert values == {"value": "a"}, \
        f"unexpected values: {values}, expected {{'value': 'a'}}"
    assert calls == 1, \
        f"expected _drop_and_greedy_search to be called once, got {calls} calls"


def test_match_is_case_sensitive():
    """
    Literal text must match with the same case, so files differing only in
    case do not match the format.
    """
    fmt_entries = [{"fmt": "a{a}_i{i}.h5"}]
    i, result = _match_file_against_fmts("A0_i0.h5", fmt_entries)

    assert i is None and result is None, \
        f"unexpected match: {(i, result)}, expected no match for different case"


def test_match_stage_field_is_constrained_like_kind():
    """
    A detected {stage} field should be restricted to known processing stages,
    so it resolves the same field-shift ambiguity as {kind}, while an
    unconstrained field name lets the shift happen.
    """
    assert "stage" in KNOWN_FIELD_VALUES, \
        f"unexpected KNOWN_FIELD_VALUES: {KNOWN_FIELD_VALUES}, expected 'stage'"
    rel_path = "e18a24-1-b1-na-CYG-A-4fit.tar"
    fmt_entries = [
        {"fmt": "{track}-{obs_num}-{band}-{pointing}-{source}-{stage}.{format}"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)
    assert result["source"] == "CYG-A", \
        f"unexpected source: {result.get('source')}, expected 'CYG-A'"
    assert result["stage"] == "4fit", \
        f"unexpected stage: {result.get('stage')}, expected '4fit'"

    fmt_entries = [
        {"fmt": "{track}-{obs_num}-{band}-{pointing}-{source}-{p5}.{format}"}]
    i, result = _match_file_against_fmts(rel_path, fmt_entries)
    assert result["source"] == "CYG", \
        f"unexpected source: {result.get('source')}, expected the unconstrained \
            field to shift to 'CYG'"


### match_file tests ###

def test_match_file_skips_known_static_stems():
    """
    A file whose whole stem is a known static name never matches a format.
    """
    i, result = match_file("README.txt", [{"fmt": "{name}.txt"}])

    assert i is None and result is None, \
        f"unexpected match: {(i, result)}, expected README.txt to be skipped"


def test_match_file_uses_plain_parse_for_static_last_piece():
    """
    A file whose last dotted stem piece is a static name only matches with a
    plain parse, so it cannot drop fields like the full matcher can.
    """
    i, result = match_file("x.readme.txt", [{"fmt": "{name}.readme.txt"}])
    assert (i, result) == (0, {"name": "x"}), \
        f"unexpected match: {(i, result)}, expected (0, {{'name': 'x'}})"

    fmt_entries = [{"fmt": "{a}_{b}.readme.txt"}]
    assert _match_file_against_fmts("x.readme.txt", fmt_entries) == (0, {"a": "x"}), \
        "expected the full matcher to drop the missing field"
    i, result = match_file("x.readme.txt", fmt_entries)
    assert i is None and result is None, \
        f"unexpected match: {(i, result)}, expected the plain parse to reject it"


def test_match_file_delegates_to_full_matcher():
    """
    Ordinary files go through the full matcher, including dropped fields.
    """
    i, result = match_file("a0.75.h5", [{"fmt": "a{a}_i{i}.h5"}])

    assert (i, result) == (0, {"a": "0.75"}), \
        f"unexpected match: {(i, result)}, expected (0, {{'a': '0.75'}})"


### matcher on real EHT file names ###

def test_match_eht_resolves_a_repeated_pipeline_field():
    """
    A field named twice, like {pipeline} in the folder and the file name, is
    resolved to the value both occurrences share. (EHTC_M87pol2017_Nov2023.)
    """
    path = ("casa_data/April05/SR2_M87_2017_095_hi_casa_EVPA_rotation+dcal+ampscale"
            "+netcal+10s-avg+RLgain-amp.uvfits")
    fmt_entries = [{"fmt": "{pipeline}_data/{observation_date}/SR2_M87_2017_{scan}"
                           "_{band}_{pipeline}_{calibration}.uvfits"}]

    assert match_file(path, fmt_entries) == (0, {
        "observation_date": "April05", "scan": "095", "band": "hi",
        "calibration": "EVPA_rotation+dcal+ampscale+netcal+10s-avg+RLgain-amp",
        "pipeline": "casa"})


def test_match_eht_resolves_a_repeated_format_field():
    """
    A {format} field used as both the folder and the extension resolves to one
    value, and the matching fmt wins over a similar one without -LMTcal.
    (EHTC_FirstSgrAResults_May2022.)
    """
    fmt_entries = [
        {"fmt": "{format}/ER6_SGRA_2017_{scan}_{band}_{pipeline}"
                "_netcal-LMTcal_StokesI.{format}"},
        {"fmt": "{format}/ER6_SGRA_2017_{scan}_{band}_{pipeline}"
                "_netcal_StokesI.{format}"},
        {"fmt": "{format}_{method}/ER6_SGRA_2017_{scan}_{band}_{pipeline}"
                "_netcal-LMTcal-{method}_StokesI.{format}"}]

    assert match_file("csv/ER6_SGRA_2017_096_hi_casa_netcal-LMTcal_StokesI.csv",
                      fmt_entries) == (0, {
        "format": "csv", "scan": "096", "band": "hi", "pipeline": "casa"})


def test_match_eht_drops_missing_fields_to_find_the_best_fmt():
    """
    A deliverables file missing some fields of its fmt still matches it, with
    those fields dropped, ahead of the dataset's other fmts.
    (EHTC_2017L1_May2022.)
    """
    fmt_entries = [
        {"fmt": "{project_code}/group.uid{guid}.{author}.{kind}.{format}"},
        {"fmt": "{eht_project_code}/group.uid{guid}.{author}.{scope}"
                "-processing-logs.{format}"},
        {"fmt": "{project_date}/{eht_project_code}/group.uid{guid}.{author}."
                "{prefix}track{track}-{date}.app_deliverables.{format}"},
        {"fmt": "{eht_project_code}/group.uid{guid}.{author}.{track}-{alma_band}-"
                "{eht_band}-{source}-{scan_code}-{sequence}-{kind}.{format}"},
        {"fmt": "{eht_project_code}/group.uid{guid}.{author}.metadata-"
                "{eht_product_code}.{format}"},
        {"fmt": "{eht_project_code}.{algorithm}sums"}]
    path = ("2016.1.01114.V/group.uid___A001_X87c_X245.ec_jlgomez.track_a."
            "app_deliverables.tgz")

    assert match_file(path, fmt_entries) == (2, {
        "project_date": "2016.1.01114.V", "guid": "___A001_X87c_X245",
        "author": "ec_jlgomez", "prefix": "", "track": "_a", "format": "tgz"})
