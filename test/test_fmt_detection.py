import pytest
from pathlib import Path
from hallmark.fmt_detection import (
    _delimiters,
    _tokens,
    _token_cache,
    _finalize_param_names,
    _infer_param,
    _is_drive_path,
    _paths_to_fmts,
    _join_tokens_with_delims,
    _known_param_tags,
    _matching_paths,
    _parsed_paths,
    _collapse_freeform_tails,
    _rescue_unmatched_paths,
    _align,
    _generic_param_names,
    merge_fmts_sharing_all_literals,
    scan_inventory,
    combine_alike_fmts,
    detect_fmt)


### token and delim tests ###

def test_tokenization_preserves_plus_inside_a_token():
    """ Test that the tokenization process preserves plus signs within tokens,
    rather than splitting them.
    """
    value = "M87+3C279_hops/scan-001.fits"

    assert _tokens(value) == ["M87+3C279", "hops", "scan", "001", "fits"], \
        f"expected plus to be preserved inside a token, got {_tokens(value)}"
    assert _delimiters(value) == ["_", "/", "-", "."], \
        f"expected delimiters to be preserved, got {_delimiters(value)}"


def test_token_cache_deduplicates_identical_path_keys():
    """
    Test that the token cache deduplicates identical paths,
    returning a single token list for each unique path
    """
    assert _token_cache(["a_1.dat", "a_1.dat"]) == {"a_1.dat": ["a", "1", "dat"]}, \
        f"expected identical paths to be deduplicated, got \
            {_token_cache(['a_1.dat', 'a_1.dat'])}"


def test_tokenization_cache_does_not_expose_mutable_values():
    """
    Test that the tokenization cache does not expose mutable values that can be
    modified externally, ensuring that the cached tokenization remains immutable.
    """
    value = "M87_hops/data-001.fits"
    tokens = _tokens(value)
    delimiters = _delimiters(value)
    tokens[0] = "changed"
    delimiters[0] = "+"

    assert _tokens(value) == ["M87", "hops", "data", "001", "fits"], \
        f"expected tokenization cache to be immutable, got {_tokens(value)}"
    assert _delimiters(value) == ["_", "/", "-", "."], \
        f"expected tokenization cache to be immutable, got {_delimiters(value)}"


### finalize_param_names tests ###

def test_finalize_param_names_renumbers_positional_fields():
    """
    Test that _finalize_param_names renumbers positional fields to be sequential.
    """
    paths = ["item_a_b.dat", "item_c_d.dat"]

    assert _finalize_param_names("item_{p5}_{p9}.dat", paths) == (
        "item_{p0}_{p1}.dat"), f"expected positional fields to be renumbered, got \
            {_finalize_param_names('item_{p5}_{p9}.dat', paths)}"


def test_finalize_param_names_reuses_name_for_identical_signatures():
    """
    Test that _finalize_param_names reuses the same parameter name for identical
    signatures in the observed paths.
    """
    paths = ["hops/hops/result", "casa/casa/result"]

    assert _finalize_param_names("{pipeline}/{pipeline}/result", paths) == (
        "{pipeline}/{pipeline}/result"), f"expected identical param names to be reused,\
              got {_finalize_param_names('{pipeline}/{pipeline}/result', paths)}"


def test_finalize_param_names_disambiguates_different_signatures():
    """
    Test that _finalize_param_names disambiguates identical parameter names when
    they correspond to different values in the observed paths.
    """
    paths = ["M87-SGRA.dat", "SGRA-M87.dat"]

    assert _finalize_param_names("{source}-{source}.dat", paths) == (
        "{source}-{source2}.dat"), f"expected disambiguation of identical param names, \
            got {_finalize_param_names('{source}-{source}.dat', paths)}"


def test_finalize_param_names_preserves_repeated_positional_fields():
    """
    Test that _finalize_param_names preserves repeated positional fields when they
    correspond to the same values in the observed paths.
    """
    paths = ["same_same.dat", "other_other.dat"]
    finalized = _finalize_param_names("{p5}_{p5}.dat", paths)

    assert finalized == "{p0}_{p0}.dat", \
        f"expected repeated positional fields to be preserved, got {finalized}"
    assert _parsed_paths(finalized, paths) == set(paths), f"expected repeated \
        positional fields to be preserved, got {_parsed_paths(finalized, paths)}"


### _infer_param tests ###

@pytest.mark.parametrize(
    "observed, expected",[
        ({"e17a10", "e18b24"}, "experiment"),
        ({"hi", "lo"}, "band"),
        ({"1", "7"}, "pass"),
        ({"095", "123"}, "scan"),
        ({"20170406", "20170411"}, "date"),
        ({"hops", "casa"}, "pipeline"),
        ({"M87", "SGRA"}, "source"),
        ({"tar", "tgz"}, "format"),
        ({"unmapped-a", "unmapped-b"}, "p9"),
        (set(), "p9")])
def test_infer_param_uses_patterns_known_values_and_fallback(observed, expected):
    """
    Test that _infer_param correctly infers parameter names based on known patterns,
    known values, and falls back to a generic name when no match is found.
    """
    assert _infer_param(observed, "p9") == expected, f"expected \
        {_infer_param(observed,'p9')} to be {expected} for observed values {observed}"


### _is_drive_path tests ###

@pytest.mark.parametrize(
    "name, expected",
    [("bundle.TGZ", True), ("bundle.tar.gz", True), ("bundle.uvfits", False)])
def test_is_drive_path_is_case_insensitive(name, expected):
    """
    Test that _is_drive_path correctly identifies drive/archive files regardless of case
    Args:
        name: The filename to test.
        expected: The expected boolean result indicating if the file is a drive/archive.
    """
    assert _is_drive_path(Path(name)) is expected, \
        f"expected {_is_drive_path(Path(name))} for {name}, got {expected}"


def test_is_drive_path_handles_multipart_archive_suffixes():
    """
    _is_drive_path should detect multi-part archive suffixes.
    """
    assert _is_drive_path(Path("x.tar.gz")), \
        f"expected .tar.gz to be detected as a drive path, got {Path('x.tar.gz')}"
    assert _is_drive_path(Path("x.tar.bz2")), \
        f"expected .tar.bz2 to be detected as a drive path, got {Path('x.tar.bz2')}"
    assert _is_drive_path(Path("x.tar.xz")), \
        f"expected .tar.xz to be detected as a drive path, got {Path('x.tar.xz')}"
    assert not _is_drive_path(Path("x.fits")), \
        f"expected .fits not to be detected as a drive path, got {Path('x.fits')}"


### _paths_to_fmts tests ###

def test_paths_to_fmts_merges_when_one_position_is_globally_fixed():
    """
    Test that _paths_to_fmts merges paths into a single format when one position
    is globally fixed.
    """
    paths = ["sgra_20170406_nustar", "sgra_20170411_nustar",
             "sgra_20170406_chandra", "sgra_20170407_chandra"]
    fmts = _paths_to_fmts(paths)

    assert fmts == ["sgra_{date}_{p1}"], \
        f"expected one merged fmt with an inferred date field, got {fmts}"


def test_paths_to_fmts_falls_back_to_anchor_clustering_for_unrelated_paths():
    """
    Test that _paths_to_fmts falls back to anchor clustering for unrelated paths.
    """
    paths = ["A_B_C_D", "A_E_F_G", "H_I_J_K", "L_I_M_N"]
    fmts = _paths_to_fmts(paths)

    assert sorted(fmts) == sorted(["A_{p0}_{p1}_{p2}", "{p0}_I_{p1}_{p2}"]), \
        f"expected two separate fmts for unrelated schemes, got {fmts}"


def test_paths_to_fmts_single_path_returns_literal_fmt():
    """
    Test that _paths_to_fmts returns a literal format for a single path.
    """
    fmts = _paths_to_fmts(["uniquefile_v1"])

    assert fmts == ["uniquefile_v1"], \
        f"expected literal fmt for single path, got {fmts}"


def test_paths_to_fmts_dot_delimited_eht_style():
    """
    Test that _paths_to_fmts correctly handles dot-delimited EHT-style paths.
    """
    paths = ["AA.B.1", "AA.B.2", "AP.B.3", "AX.B.7", "XX.B.17"]
    fmts = _paths_to_fmts(paths)

    assert fmts == ["{p0}.B.{p1}"], f"expected dot-preserving merged fmt, got {fmts}"


def test_paths_to_fmts_empty_input_returns_empty_list():
    """
    Test that _paths_to_fmts returns [] for empty input.
    """
    fmts = _paths_to_fmts([])

    assert fmts == [], f"expected empty output for empty input, got {fmts}"


def test_paths_to_fmts_keeps_mixed_token_counts_separate():
    """
    Test that _paths_to_fmts keeps formats with different token counts separate.
    """
    fmts = _paths_to_fmts(["a_b", "a"])

    assert set(fmts) == {"a_b", "a"}, \
        f"expected different token counts to remain separate, got {fmts}"


### _join_tokens_with_delims tests ###

@pytest.mark.parametrize(
    "tokens, delimiters, expected",
    [
        ([], [], ""),
        (["a", "{p0}", "fits"], ["_", "."], "a_{p0}.fits")])
def test_join_tokens_with_delimiters(tokens, delimiters, expected):
    """
    Test that _join_tokens_with_delims correctly joins tokens with the right delimiters.
    Args:
        tokens: A list of tokens to join.
        delimiters: A list of delimiters to use between tokens.
        expected: The expected joined string."""
    assert _join_tokens_with_delims(tokens, delimiters) == expected, \
        f"expected {_join_tokens_with_delims(tokens, delimiters)} to be {expected}"


### _known_param_tags tests ###

def test_known_param_tags_uses_observed_values():
    """
    Test that _known_param_tags infers known parameter tags from observed values.
    """
    paths = ["hops_M87.dat", "casa_SGRA.dat"]

    assert _known_param_tags("{p0}_{p1}.dat", paths) == {"pipeline", "source"}, \
        f"expected known param tags to be inferred from observed values, got \
            {_known_param_tags('{p0}_{p1}.dat', paths)}"


### _matching_paths tests ###

def test_matching_paths_requires_same_token_layout_and_literals():
    """
    Test _matching_paths only returns paths that match the same token layout and lits.
    """
    paths = ["x_1.dat", "x-1.dat", "y_1.dat", "x_1.csv", "x_1_extra.dat"]

    assert _matching_paths("x_{p0}.dat", paths) == ["x_1.dat"], \
        f"expected only paths matching the token layout and literals, got \
            {_matching_paths('x_{p0}.dat', paths)}"


### _parsed_paths tests ###

def test_parsed_paths_enforces_repeated_parameter_equality():
    """
    Test that _parsed_paths only returns paths where repeated params have equal values.
    """
    paths = ["hops/hops/result", "hops/casa/result"]

    assert _parsed_paths("{pipeline}/{pipeline}/result", paths) == {
        "hops/hops/result"}, f"expected only paths with equal repeated parameters, got \
            {_parsed_paths('{pipeline}/{pipeline}/result', paths)}"


def test_parsed_paths_returns_empty_set_for_invalid_format():
    assert _parsed_paths("broken_{field", ["broken_value"]) == set(), f"expected empty \
        set for invalid format, got {_parsed_paths('broken_{field', ['broken_value'])}"


### _collapse_freeform_tails tests ###

def test_collapse_freeform_tails_keeps_delimiter_families_separate():
    """
    Test that _collapse_freeform_tails keeps distinct delimiter families separate.
    """
    fmts = ["x_{p0}.dat", "x-{p0}.dat"]

    assert _collapse_freeform_tails(fmts) == fmts, f"expected distinct delimiter \
        families to remain separate, got {_collapse_freeform_tails(fmts)}"


def test_collapse_freeform_tails_preserves_noncollapsible_candidates():
    """
    Test that _collapse_freeform_tails preserves non-collapsible candidates.
    """
    collapsed = _collapse_freeform_tails([
        "x_{p0}_{p1}.dat", "x_{p0}.dat", "x_literal.dat"])

    assert set(collapsed) == {"x_{p0}.dat", "x_literal.dat"}, \
        f"expected non-collapsible candidates to be preserved, got {collapsed}"


### _rescue_unmatched_paths tests ###

def test_rescue_unmatched_paths_expands_compatible_middle():
    """
    Test that _rescue_unmatched_paths expands unmatched paths that are compatible
    with the source formats, even if they have a different number of tokens.
    """
    source_fmts = ["alpha_fixed_tailA", "beta_fixed_tailB"]
    unmatched = ["alpha_a_b_tailA"]
    rescued = _rescue_unmatched_paths(source_fmts, unmatched)

    assert rescued == ["alpha_{p0}_tailA", "beta_fixed_tailB"], f"expected rescued \
        formats to be ['alpha_{{p0}}_tailA', 'beta_fixed_tailB'], got {rescued}"
    assert _parsed_paths(rescued[0], unmatched) == set(unmatched), f"expected rescued \
        fmt to cover unmatched paths, got {_parsed_paths(rescued[0], unmatched)}"


def test_rescue_unmatched_paths_ignores_incompatible_family():
    """
    Test that _rescue_unmatched_paths ignores unmatched paths that are incompatible
    with the source formats.
    """
    source_fmts = ["alpha_fixed_tailA", "beta_fixed_tailB"]
    rescued = _rescue_unmatched_paths(source_fmts, ["gamma_a_b_tailC"])

    assert rescued == source_fmts, \
        f"expected incompatible family to be ignored, got {rescued}"


### _align tests ###

def test_align_reports_differences_and_literal_evidence():
    """
    Test that _align correctly identifies differing token positions and whether
    there is genuine literal evidence between two token lists.
    """
    aligned = _align(["prefix", "{p0}", "tail"], ["prefix", "value", "tail"])

    assert aligned == ({1}, True), \
        f"expected differing position 1 and genuine literal evidence, got {aligned}"


def test_align_rejects_different_token_counts():
    """
    Test that _align returns None when the two token lists have different lengths.
    """
    assert _align(["prefix", "{p0}", "tail"], ["prefix", "tail"]) is None, \
        f"expected None for different token counts, \
            got {_align(['prefix', '{p0}', 'tail'], ['prefix', 'tail'])}"


def test_align_requires_genuine_literal_evidence():
    """
    Test that _align requires at least one genuine literal match to consider two
    token lists alignable.
    """
    aligned = _align(["first", "{p0}"], ["second", "{p1}"])

    assert aligned == ({0, 1}, False), \
        f"expected no genuine literal evidence, got {aligned}"


### _generic_param_names tests ###

def test_generic_param_names_fills_smallest_gap_first():
    """
    Test that _generic_param_names yields the smallest unused "pN" name first,
    filling gaps in already-used numbers before continuing past them.
    """
    names = _generic_param_names(["a", "{p0}", "{p2}"])
    result = [next(names) for _ in range(3)]

    assert result == ["p1", "p3", "p4"], \
        f"expected gap-filling order p1, p3, p4, got {result}"


def test_generic_param_names_scans_all_provided_token_lists():
    """
    Test that _generic_param_names treats multiple token_lists as a combined set
    of already-used numbers, not just the first list.
    """
    result = next(_generic_param_names(["{p0}"], ["{p1}"]))

    assert result == "p2", \
        f"expected p2 after combining used numbers from both lists, got {result}"


def test_generic_param_names_used_override_ignores_token_lists():
    """
    Test that an explicit used= set overrides scanning token_lists entirely, even
    when it is an empty set (falsy but not None).
    """
    result = next(_generic_param_names(["{p5}", "{p6}"], used=set()))

    assert result == "p0", \
        f"expected used= to override token scanning and yield p0, got {result}"


def test_generic_param_names_used_override_respects_provided_numbers():
    """
    Test that an explicit used= set is honored as the set of already-used numbers.
    """
    result = next(_generic_param_names(used={0, 1}))

    assert result == "p2", f"expected p2 given used={{0, 1}}, got {result}"


def test_generic_param_names_sequential_calls_avoid_each_other():
    """
    Test that repeated next() calls on the same generator instance each avoid all
    previously yielded names, not just the ones scanned at creation.
    """
    names = _generic_param_names([])
    result = [next(names) for _ in range(3)]

    assert result == ["p0", "p1", "p2"], \
        f"expected sequential p0, p1, p2, got {result}"


### merge_fmts_sharing_all_literals tests ###

def test_merge_fmts_sharing_all_literals_preserves_source_coverage():
    """
    Test that merge_fmts_sharing_all_literals preserves coverage of all source paths
    when merging formats that share all literals.
    """
    paths = ["x_a_tail.dat", "x_c_tail.dat", "x_a_b_tail.dat", "x_c_d_tail.dat"]
    source_fmts = ["x_{p0}_tail.dat", "x_{p0}_{p1}_tail.dat"]
    merged = merge_fmts_sharing_all_literals(source_fmts, paths)

    assert merged == ["x_{p0}_tail.dat"], \
        f"expected merged fmts to be ['x_{{p0}}_tail.dat'], got {merged}"

    covered_paths = set().union(*(_parsed_paths(fmt, paths) for fmt in merged))
    assert covered_paths == set(paths), \
        f"expected merged fmts to cover all source paths, got {covered_paths}"


def test_merge_fmts_sharing_all_literals_renumbers_multiple_params_sequentially():
    """
    Test that merge_fmts_sharing_all_literals's trailing renumbering pass compacts
    multiple generic params to sequential {p0}, {p1}, ... in left-to-right order,
    even when the merge itself produced non-sequential numbers internally (here,
    reusing the second source fmt's {p2}/{p3} names rather than the first's {p0}/{p1}).
    """
    paths = ["x_a_mid_b_tail.dat", "x_c_mid_d_tail.dat"]
    source_fmts = ["x_{p0}_mid_{p1}_tail.dat", "x_{p2}_mid_{p3}_tail.dat"]
    merged = merge_fmts_sharing_all_literals(source_fmts, paths)

    assert merged == ["x_{p0}_mid_{p1}_tail.dat"], \
        f"expected params renumbered sequentially left-to-right, got {merged}"
    covered_paths = set().union(*(_parsed_paths(fmt, paths) for fmt in merged))
    assert covered_paths == set(paths), \
        f"expected merged fmt to cover all source paths, got {covered_paths}"


### scan_inventory tests ###

def test_scan_inventory_rejects_a_file_as_root(tmp_path):
    """
    Test that scan_inventory raises a NotADirectoryError when the root path is a file.
    Args:
        tmp_path: A temporary directory provided by pytest.
    Raises:
        NotADirectoryError: If the root path is a file.
    """
    root = tmp_path / "not-a-directory"
    root.write_text("data", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        scan_inventory(root)


def test_scan_inventory_raises_if_root_not_found(tmp_path):
    """
    Test that scan_inventory raises a FileNotFoundError when the root directory
    does not exist.
    Args:
        tmp_path: A temporary directory provided by pytest.
    Raises:
        FileNotFoundError: If the root directory does not exist.
    """
    with pytest.raises(FileNotFoundError):
        scan_inventory(tmp_path / "nonexistent")


def test_scan_inventory_finds_nested_files(tmp_path):
    """
    Test that scan_inventory correctly finds nested files in the directory structure.
    Args:
        tmp_path: A temporary directory provided by pytest.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "file1.csv").write_text("data", encoding="utf-8")
    (tmp_path / "file2.csv").write_text("data", encoding="utf-8")
    result = scan_inventory(tmp_path)

    assert set(result) == {"a/file1.csv", "file2.csv"}, \
        f"expected nested files to be found, got {result}"


def test_scan_inventory_ignores_dot_hm(tmp_path):
    """
    Test that scan_inventory ignores the .hm directory and its contents.
    Args:
        tmp_path: A temporary directory provided by pytest.
    """
    (tmp_path / ".hm").mkdir()
    (tmp_path / ".hm" / "config.yml").write_text("x", encoding="utf-8")
    (tmp_path / "visible.csv").write_text("data", encoding="utf-8")
    result = scan_inventory(tmp_path)

    assert result == ["visible.csv"], f"expected only visible.csv, got {result}"


def test_scan_inventory_returns_empty_list_for_empty_root(tmp_path):
    """
    Test that scan_inventory returns an empty list for an empty directory.
    Args:
        tmp_path: A temporary directory provided by pytest.
    """
    result = scan_inventory(tmp_path)

    assert result == [], f"expected empty inventory for empty root, got {result}"


### combine_alike_fmts tests ###

def test_combine_alike_fmts_does_not_promote_literal_without_path_evidence():
    """
    A literal must not be promoted merely because another format has a parameter there.
    """
    fmts = ["sgra_{p0}_chandra",
            "sgra_{p0}_{p1}"]
    result = combine_alike_fmts(fmts)

    assert result == fmts, \
        f"expected ambiguous formats to remain separate, got {result}"


def test_combine_alike_fmts_preserves_constant_literal_anchor():
    """
    Regression test for a bug where a constant lit anchor was being lost during merging.
    """
    fmts = [
        "ER6_SGRA_2017_{p0}_{p1}_netcal_{p2}_StokesI",
        "ER6_SGRA_2017_{p0}_{p1}_netcal_StokesI",]
    result = combine_alike_fmts(fmts)

    assert len(result) in (1, 2), f"unexpected merge behavior: {result}"
    assert any("StokesI" in fmt for fmt in result), \
        f"StokesI should remain literal, got {result}"


def test_combine_alike_fmts_rejects_zero_literal_anchor_merge():
    """
    Test that combine_alike_fmts rejects merging when there is no literal anchor.
    """
    fmts = ["sgra_{p0}_{p1}_{p2}",
            "{p0}_{p1}_{p2}"]
    result = combine_alike_fmts(fmts)

    assert len(result) == 2, \
        f"expected rejection of fully-parameterized merge, got {result}"


def test_combine_alike_fmts_keeps_different_token_counts_separate():
    """
    Formats with different token counts remain separate without supporting paths.
    """
    fmts = [
        "ER6_SGRA_2017_{p0}_{p1}_netcal_StokesI",
        "ER6_SGRA_2017_{p0}_{p1}_{p2}_netcal_StokesI",]
    result = combine_alike_fmts(fmts)

    assert result == fmts, \
        f"expected different token counts to remain separate, got {result}"


def test_combine_alike_fmts_empty_input_returns_empty_list():
    """
    Test that combine_alike_fmts returns [] for empty input.
    """
    result = combine_alike_fmts([])

    assert result == [], f"expected empty output for empty input, got {result}"


def test_combine_alike_fmts_keeps_single_format_as_is():
    """
    Test that combine_alike_fmts leaves a single format unchanged.
    """
    result = combine_alike_fmts(["sgra_{p0}_chandra"])

    assert result == ["sgra_{p0}_chandra"], \
        f"expected single fmt to remain unchanged, got {result}"


def test_combine_alike_fmts_does_not_merge_on_non_discriminative_tag_only():
    """
    Non-discriminative tags (like format) must not trigger merging by themselves.
    """
    fmts = ["{p0}_{p1}.fits", "{p0}_{p1}.tar"]
    result = combine_alike_fmts(fmts, known_param_tags={"{p0}_{p1}.fits": {"format"},
                                                        "{p0}_{p1}.tar": {"format"}})

    assert result == fmts, f"expected formats to remain separate, got {result}"


### detect_fmt tests ###

def test_detect_fmt_excludes_drive_files_by_default():
    """
    Drive/archive files should be ignored unless include_drives=True.
    """
    fmts = detect_fmt(["bundle_a.tar", "bundle_b.tar"])

    assert fmts == [], f"expected no fmts when only drives are present, got {fmts}"


def test_detect_fmt_includes_drive_files_when_flag_enabled():
    """
    Drive/archive files should be considered when include_drives=True.
    """
    fmts = detect_fmt(["bundle_a.tar", "bundle_b.tar"], include_drives=True)

    assert fmts, "expected at least one fmt when include_drives=True, got none"
    assert any(".tar" in fmt for fmt in fmts), f"expected a .tar-based fmt, got {fmts}"


def test_detect_fmt_meta_filename_filter_is_case_insensitive():
    """
    Meta filenames should be ignored regardless of case.
    """
    files = ["ReadMe.md", "readme.txt", "DATA_01.fits", "DATA_02.fits"]
    fmts = detect_fmt(files)

    assert len(fmts) == 1, f"expected one fmt from non-meta files, got {fmts}"
    assert "DATA" in fmts[0] and ".fits" in fmts[0], \
        f"expected DATA fits fmt, got {fmts}"


def test_detect_fmt_rejects_single_file_with_no_siblings():
    """
    Test that detect_fmt returns an empty list when given a single file with no siblings
    """
    fmts = detect_fmt(["uniquefile_v1.dat"])

    assert fmts == [], f"expected no fmt for single file with no siblings, got {fmts}"


def test_detect_fmt_finds_single_shared_format():
    """
    Test that detect_fmt infers one format from multiple matching filenames.
    """
    files = [
        "sgra_20170406_nustar.fits",
        "sgra_20170411_nustar.fits",
        "sgra_20170406_chandra.fits",
        "sgra_20170407_chandra.fits",]
    fmts = detect_fmt(files)

    assert fmts == ["sgra_{date}_{p0}.fits"], \
        f"expected one shared fmt with an inferred date field, got {fmts}"


def test_detect_fmt_handles_dot_delimited_names():
    """
    Test that detect_fmt preserves dot-delimited structure in inferred formats.
    """
    files = ["AA.B.1", "AA.B.2", "AP.B.3", "AX.B.7"]
    fmts = detect_fmt(files)

    assert fmts == ["{p0}.B.{pass}"], f"expected dot-delimited fmt, got {fmts}"


def test_detect_fmt_ignores_duplicate_input_paths():
    """
    Test that detect_fmt ignores duplicate paths in its input.
    """
    unique = ["data_001.fits", "data_002.fits"]

    assert detect_fmt([*unique, *unique]) == detect_fmt(unique), \
        f"expected duplicate paths to be ignored, got {detect_fmt([*unique, *unique])}"


def test_detect_fmt_keeps_plus_literal_and_infers_pipeline():
    """
    Test that detect_fmt preserves plus signs in tokens and infers the pipeline param.
    """
    paths = ["M87+3C279_hops_001.fits", "M87+3C279_casa_002.fits"]

    assert detect_fmt(paths) == ["M87+3C279_{pipeline}_{scan}.fits"], \
        f"expected plus to be preserved and pipeline inferred, got {detect_fmt(paths)}"


def test_detect_fmt_reuses_parameter_for_identical_pipeline_positions():
    """
    Test that detect_fmt uses the same param for identical positions in the path."""
    paths = ["item_hops_hops_001.dat", "item_casa_casa_002.dat",
             "item_hops_hops_003.dat", "item_casa_casa_004.dat"]

    assert detect_fmt(paths) == ["item_{pipeline}_{pipeline}_{scan}.dat"], \
        f"expected identical pipeline positions to be inferred as the same parameter, \
            got {detect_fmt(paths)}"


def test_detect_fmt_combines_sed_variations_without_losing_source():
    paths = ["SED_M87_2017.dat", "SED_M87_2018.dat", "SED_SGRA_2017.dat",
              "SED_SGRA_2018.dat"]

    assert detect_fmt(paths) == ["SED_{source}_{p0}.dat"], \
        f"expected source and p0 to be inferred, got {detect_fmt(paths)}"


def test_detect_fmt_builds_format_from_single_token_anchor():
    """
    Test that detect_fmt can build a format from a single token anchor.
    """
    paths = ["image", "image_001.fits", "image_002.fits"]

    assert detect_fmt(paths) == ["image_{p0}.{p1}"], \
        f"expected single token anchor to produce a format, got {detect_fmt(paths)}"


def test_detect_fmt_preserves_distinct_delimiter_families():
    """
    Test that detect_fmt preserves distinct delimiter families when inferring formats.
    """
    paths = ["x_a.dat", "x_b.dat", "x-c.dat", "x-d.dat"]

    assert set(detect_fmt(paths)) == {"x_{p0}.dat", "x-{p0}.dat"}, \
        f"expected distinct delimiter families to be preserved, got {detect_fmt(paths)}"


def test_detect_fmt_output_is_independent_of_input_order():
    """
    Test that detect_fmt produces the same output regardless of the order of input paths
    """
    paths = ["a_1.dat", "a_2.dat", "b-1.dat", "b-2.dat"]
    forward = detect_fmt(paths)
    reversed_result = detect_fmt(list(reversed(paths)))

    assert forward == reversed_result, f"expected output to be independent of input \
        order, got {forward} and {reversed_result}"
    assert forward == sorted(forward), f"expected output to be sorted and independent \
        of input order, got {forward} and {reversed_result}"


def test_detect_fmt_normalizes_windows_inventory_separators():
    """
    Test that detect_fmt normalizes Windows-style backslash separators to POSIX-style
    forward slashes in its output.
    """
    windows_paths = [r"folder\a_1.dat", r"folder\a_2.dat"]
    posix_paths = ["folder/a_1.dat", "folder/a_2.dat"]
    windows_result = detect_fmt(windows_paths)
    posix_result = detect_fmt(posix_paths)

    assert windows_result == posix_result, f"expected output to be the same for Windows\
          and POSIX paths, got {windows_result} and {posix_result}"
    assert all("\\" not in fmt for fmt in windows_result), \
        f"expected output to use POSIX separators, got {windows_result}"


def test_detect_fmt_anchor_preserves_multiple_structural_families():
    """
    Test that detect_fmt preserves multiple structural families when they share a
    common anchor.
    """
    paths = [
        "image", "image_001.fits", "image_002.fits", "001-image.txt", "002-image.txt"]

    assert set(detect_fmt(paths)) == {"image_{p0}.{p1}", "{p0}-image.{p1}"}, \
        f"expected multiple structural families to be preserved, \
            got {detect_fmt(paths)}"


def test_detect_fmt_excludes_drives_but_keeps_non_drive_candidates():
    """
    Drive/archive files should be filtered out while regular data files still drive
    format detection when include_drives=False.
    """
    files = ["bundle_a.tar", "bundle_b.tar", "data_001.fits", "data_002.fits"]
    fmts = detect_fmt(files)

    assert fmts == ["data_{scan}.fits"], \
        f"expected only non-drive fmt detection, got {fmts}"
    assert all(".tar" not in fmt for fmt in fmts), \
        f"expected no drive formats when include_drives=False, got {fmts}"


def test_detect_fmt_ignores_extensionless_known_static_file_stems():
    """
    Known static file stems should be ignored even when no extension is present.
    """
    files = ["README", "LICENSE", "data_001.fits", "data_002.fits"]
    fmts = detect_fmt(files)

    assert fmts == ["data_{scan}.fits"], \
        f"expected static stems without extensions to be ignored, got {fmts}"
    assert all("README" not in fmt and "LICENSE" not in fmt for fmt in fmts), \
        f"expected no static stem formats, got {fmts}"


def test_detect_fmt_anchor_requires_repeated_structure_not_singletons():
    """
    Anchor expansion should not create formats when each structural family appears once.
    """
    paths = ["image", "image_001.fits", "thumb-image.txt"]

    assert detect_fmt(paths) == [], f"expected no anchor-derived fmt from singleton \
        families, got {detect_fmt(paths)}"


### known limitation tests ###

def test_decimal_point_is_treated_as_a_delimiter():
    """
    Test that the decimal point is treated as a delimiter in tokenization,
    which can lead to unexpected tokenization of decimal numbers. Known limitation.
    """
    import re
    from hallmark.fmt_detection import _DELIM_PATTERN
    tokens = re.split(_DELIM_PATTERN, "a0.75_i30")

    assert tokens == ["a0", "75", "i30"], \
        f"expected the decimal point to fracture '0.75' into two tokens, got {tokens}"