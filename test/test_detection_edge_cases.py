"""Edge cases of filename-format detection and its merging heuristics."""

from hallmark.fmt_detection import (
    _finalize_param_names,
    _parsed_paths,
    _rescue_unmatched_paths,
    combine_alike_fmts,
    detect_fmt,
    merge_fmts_sharing_all_literals,
)


def _covered(fmts, paths):
    """Return the paths that at least one of *fmts* parses."""
    return set().union(*(_parsed_paths(fmt, paths) for fmt in fmts))


### detect_fmt ###

def test_a_lone_one_token_file_does_not_disturb_detection():
    paths = ["notes", "M87_001.fits", "M87_002.fits", "M87_003.fits"]
    assert detect_fmt(paths) == ["M87_{scan}.fits"]


### _parsed_paths ###

def test_parsed_paths_of_a_format_parse_cannot_compile_is_empty():
    # Unknown type and a name reused with another type both fail to compile.
    assert _parsed_paths("{scan:q}.fits", ["001.fits"]) == set()
    assert _parsed_paths("{scan}_{scan:d}.fits", ["1_1.fits"]) == set()


### _finalize_param_names ###

def test_finalize_param_names_skips_a_numbered_name_already_taken():
    paths = ["001_002_003.fits", "004_005_006.fits"]
    # {scan} at the end holds other values than the first {scan}, and
    # {scan2} is already used, so it becomes {scan3}.
    finalized = _finalize_param_names("{scan}_{scan2}_{scan}.fits", paths)
    assert finalized == "{scan}_{scan2}_{scan3}.fits"
    assert _parsed_paths(finalized, paths) == set(paths)


### _rescue_unmatched_paths ###

def test_rescue_uses_the_fmt_sharing_the_longest_prefix_and_suffix():
    # "a" and "z" appear in only half of the fmts, so they still distinguish.
    others = ["m_n_o", "u_v_w"]
    unmatched = ["a_b_q_r_s_y_z"]
    for fmts in (["a_b_c_y_z", "a_k_z"], ["a_k_z", "a_b_c_y_z"]):
        rescued = _rescue_unmatched_paths(fmts + others, unmatched)
        assert "a_b_{p0}_y_z" in rescued
        assert "a_k_z" in rescued
        assert rescued[2:] == others
        assert _covered(rescued, unmatched) == set(unmatched)


def test_rescue_ignores_a_fmt_whose_prefix_and_suffix_would_overlap():
    # x_k_y matches the start and end of x_k_y_y in overlapping tokens, so
    # there is no middle to turn into a parameter.
    fmts = ["x_k_y", "m_n", "u_v"]
    assert _rescue_unmatched_paths(fmts, ["x_k_y_y"]) == fmts


### combine_alike_fmts ###

def test_combine_keeps_fmts_whose_shared_literal_is_in_another_place():
    fmts = ["a_{p0}_{p1}", "{p0}_a_{p1}", "x_y_z", "u_v_w"]
    assert combine_alike_fmts(fmts) == fmts


def test_combine_keeps_tagged_fmts_apart_without_a_literal_in_common():
    fmts = ["a_{p0}", "{p1}_b"]
    tags = dict.fromkeys(fmts, {"source"})
    assert combine_alike_fmts(fmts, known_param_tags=tags) == fmts


def test_combine_turns_a_differing_literal_of_tagged_fmts_into_a_parameter():
    fmts = ["run_a_{p0}", "other_{p0}", "run_b_{p0}"]
    tags = {"run_a_{p0}": {"band"}, "run_b_{p0}": {"band"}}
    combined = combine_alike_fmts(fmts, known_param_tags=tags)
    assert combined == ["run_{p1}_{p0}", "other_{p0}"]
    paths = ["run_a_1", "run_b_2", "other_3"]
    assert _covered(combined, paths) == set(paths)


### merge_fmts_sharing_all_literals ###

def test_merge_keeps_a_fmt_whose_paths_the_merged_fmt_would_lose():
    paths = ["x_y_1.dat", "x_y_2.dat", "x_a_b_y_1.dat", "x_c_d_y_2.dat"]
    merged = merge_fmts_sharing_all_literals(
        ["x_y_{p0}.dat", "x_{p1}_{p2}_y_{p0}.dat"], paths)
    # x_{...}_y_{...}.dat cannot parse x_y_1.dat, so both fmts stay.
    assert merged == ["x_y_{p0}.dat", "x_{p0}_{p1}_y_{p2}.dat"]
    assert _covered(merged, paths) == set(paths)


def test_merge_fills_a_gap_between_literals_with_one_parameter():
    paths = ["x_a_b_tail.dat", "x_c_d_tail.dat"]
    merged = merge_fmts_sharing_all_literals(
        ["x_tail.dat", "x_{p0}_{p1}_tail.dat"], paths)
    assert merged == ["x_{p0}_tail.dat"]
    assert _covered(merged, paths) == set(paths)


def test_merge_keeps_a_shared_trailing_parameter():
    paths = ["x_1_2_3_t.dat", "x_4_5_6_t.dat",
             "x_1_2_3_4_t.dat", "x_5_6_7_8_t.dat"]
    merged = merge_fmts_sharing_all_literals(
        ["x_{a}_{b}_{c}_t.dat", "x_{d}_{e}_{f}_{c}_t.dat"], paths)
    assert merged == ["x_{p0}_{c}_t.dat"]
    assert _covered(merged, paths) == set(paths)
