from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from functools import lru_cache
from itertools import takewhile
from pathlib import Path

import parse

from .utils import CHECKSUM_ALGORITHMS, iter_repository_files

# common meta extensions to look for when building the fmts
META_EXTENSIONS = [".py", ".sh", ".md", ".rst", ".cfg", ".ini", ".yml", ".yaml",
                    ".sl", ".par", ".xcm", ".codes", ".swp"]
# common meta file stems to look for when building the fmts
KNOWN_STATIC_FILE_STEMS = frozenset({
    "readme", "license", "licence", "inventory", "run"})
# processing stages that are known to be used in the EHT pipeline
KNOWN_PROCESSING_STAGES = (
    "fits", "hops", "4fit", "dxin", "haxp", "pcin", "pcqk", "swin")
# common archive formats that are recognized in the repository
ARCHIVE_FORMATS = ("tgz", "tar", "zip", "bz2", "xz", "zst", "7z", "rar")
# drive/archive file extensions derived from the known archive formats
DRIVE_EXTENSIONS = [f".{archive_format}" for archive_format in ARCHIVE_FORMATS]
# delimiter characters fmt detection splits on
_DELIM_PATTERN = r"[_\-./]"
# treat common multi-part archive suffixes explicitly
_MULTI_PART_DRIVE_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz")
# combined drive suffixes for easier checking of drive/archive files
_DRIVE_SUFFIXES = (*_MULTI_PART_DRIVE_EXTS, *DRIVE_EXTENSIONS)
# non-discriminative tags that should not be used to combine fmts
_NON_DISCRIMINATIVE_TAGS = {"format", "algorithm"}
# regex pattern to match any parameter token in a fmt
_PARAM_TOKEN_PATTERN = r"\{.*?\}"
# delimiter regex to split paths and fmts into tokens
_DELIM_RE = re.compile(_DELIM_PATTERN)
# regex to match parameter tokens in a fmt
_PARAM_TOKEN_RE = re.compile(_PARAM_TOKEN_PATTERN)
# regex to match generic parameter tokens of the form {pN}
_GENERIC_PARAM_RE = re.compile(r"\{p\d+\}")
# regex to capture the number in a generic parameter token {pN}
_GENERIC_PARAM_CAPTURE_RE = re.compile(r"\{p(\d+)\}")
# regex to match positional parameter names of the form pN with optional suffix
_POSITIONAL_NAME_RE = re.compile(r"p\d+(.*)")
# regex to match positional parameter tokens of the form {pN}
_POSITIONAL_TOKEN_RE = re.compile(r"\{p(\d+)")

# convert the extensions to lowercase and remove the leading dot for easier comparison
META_EXTS_LOWER = {ext.lstrip(".").lower() for ext in META_EXTENSIONS}

# regex patterns for known parameter types to help infer parameter names
_PARAM_PATTERNS: dict[str, re.Pattern[str]] = {
    "experiment": re.compile(r"e\d{2}[a-z]\d{2}"),
    "band": re.compile(r"^(hi|lo|b\d)$"),
    "pass": re.compile(r"^\d$"),
    "scan": re.compile(r"^\d{3}$"),
    "date": re.compile(r"^\d{8}$"),}
# known parameter values for common parameters to help infer parameter names
KNOWN_PARAM_VALUES: dict[str, set[str]] = {
    "band": {"hi", "lo"},
    "pipeline": {"hops", "casa", "smili", "difmap"},
    "stage": set(KNOWN_PROCESSING_STAGES),
    "source": {"3C273", "3C279", "3C454", "3C454.3", "3C84", "BLLAC", "CENA",
               "CTA102", "M87", "NGC1052", "OJ287", "SGRA", "3c273", "3c279",
               "3c454", "3c84", "bllac", "cena", "cta102", "m87", "ngc1052",
               "oj287", "sgra", "MRK501", "NRAO530", "mrk501", "nrao530"},
    "stokes": {"I", "Q", "U", "V", "StokesI"},
    "method": {"besttime", "norm", "scan"},
    "format": {"csv", "uvfits", "txt", *ARCHIVE_FORMATS},
    "pointing": {"3c273", "3c279", "cena", "ehtc", "m87", "na", "ngc1052",
                 "oj287", "sgra"},
    "algorithm": (set(CHECKSUM_ALGORITHMS) | {f"{algorithm}sums"
                    for algorithm in CHECKSUM_ALGORITHMS}),
    "purpose": {"description", "metadata", "deliverables", "checksum", "checksums"},
    "author": {"eht", "jlgomez", "savolainen", "sdoeleman"},}


# cache so that repeated calls do not recompute the tokens and delimiters
@lru_cache(maxsize=8192)
def _split_detector_value(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """
    Used by _tokens and _delimiters.
    Split a path or format string into detector tokens and delimiters.

    Args:
        value: The path or format string to split.

    Returns:
        A tuple containing two tuples:
            - The first tuple contains the tokens extracted from the input string.
            - The second tuple contains the delimiters extracted from the input string.
    """
    return (tuple(_DELIM_RE.split(value)), tuple(_DELIM_RE.findall(value)))


def _tokens(value: str) -> list[str]:
    """
    Used by _token_cache, _matching_paths, _known_param_tags, _finalize_param_names,
    rescue_unmatched_paths, merge_fmts_sharing_all_literals, and combine_alike_fmts.
    Split a path or format string into detector tokens.
    Args:
        value: The path or format string to tokenize.
    Returns:
        List of tokens extracted from the input string.
    """
    # use the cached function to split the value into tokens and delimiters
    tokens, _ = _split_detector_value(value)
    # only return the tokens, ignoring the delimiters
    return list(tokens)


def _delimiters(value: str) -> list[str]:
    """
    Used by _paths_to_fmts, _matching_paths, _finalize_param_names,
    _collapse_freeform_tails, _rescue_unmatched_paths, merge_fmts_sharing_all_literals,
    combine_alike_fmts, and detect_fmt.
    Return the delimiters separating the detector tokens in *value*.
    Args:
        value: The path or format string to extract delimiters from.
    Returns:
        List of delimiters extracted from the input string.
    """
    # use the cached function to split the value into tokens and delimiters
    _, delimiters = _split_detector_value(value)
    # only return the delimiters, ignoring the tokens
    return list(delimiters)


def _token_cache(paths: list[str]) -> dict[str, list[str]]:
    """
    Used by _paths_to_fmts, _matching_paths, _known_param_tags,
    _finalize_param_names, _rescue_unmatched_paths, preserves_source_matches,
    observed_slot_values, and detect_fmt.
    Tokenize a collection of paths once.
    Args:
        paths: List of path strings to tokenize.
    Returns:
        Dictionary mapping each path to its list of tokens.
    """
    return {path: _tokens(path) for path in paths}


def _parsed_paths(fmt: str, paths: list[str]) -> set[str]:
    """
    Used by parsed_matches and detect_fmt.
    Return the supplied paths accepted by *fmt*.
    Args:
        fmt: The format string to match against.
        paths: List of path strings to check.
    Returns:
        Set of paths that match the given format string.
    """
    try:
        # try to compile the fmt into a parser using the parse library
        parser = parse.compile(fmt)
    except Exception:
        # if the fmt is invalid, return an empty set
        return set()
    # return the set of paths that match the fmt by checking if parsing is successful
    return {path for path in paths if parser.parse(path) is not None}


def _infer_param(observed: set[str], fallback: str) -> str:
    """
    Used by _paths_to_fmts and reconcile_region.
    Infer the parameter name for a set of observed values.

    Args:
        observed: Set of observed values for a potential parameter.
        fallback: Fallback parameter name to use if no known pattern matches.

    Returns:
        Inferred parameter name as a string.
    """
    # if the observed set is empty, return the fallback parameter name
    if not observed:
        return fallback

    # for every known parameter pattern, check if all observed values match the pattern
    for name, pattern in _PARAM_PATTERNS.items():
        if all(pattern.fullmatch(v) for v in observed):
            return name
    for name, known in KNOWN_PARAM_VALUES.items():
        # if all observed values are in the known set, use the known parameter name
        if observed <= known:
            return name
    # if no known pattern matches, return the fallback parameter name (pN)
    return fallback


def _paths_to_fmts(paths: list[str]) -> list[str]:
    """
    Used by self and detect_fmt.
    Given a list of paths, return a list of format strings (fmts) that describe
    the variable parts of the paths.

    Args:
        paths: List of path strings to analyze.

    Returns:
        List of format strings that describe the variable parts of the paths.
    """
    # tokenize the paths and cache the results to avoid redundant tokenization
    tokenized = _token_cache(paths)
    # check if all paths have the same number of tokens, allowing more advanced merging
    token_counts = {len(tokens) for tokens in tokenized.values()}
    # length will be 1 if all paths have the same number of tokens
    if len(token_counts) == 1:
        (token_count,) = token_counts
        # find positions where the tokens differ across the paths
        varying_positions = [
            i for i in range(token_count)
            if len({tokenized[p][i] for p in paths}) > 1]

        # attempt to link adjacent varying positions if they have a one-to-one mapping
        for pos_a, pos_b in zip(varying_positions, varying_positions[1:]):
            # if the positions are not adjacent, they cannot be linked, so skip
            if pos_b != pos_a + 1:
                continue
            values_a = [tokenized[p][pos_a] for p in paths]
            values_b = [tokenized[p][pos_b] for p in paths]
            distinct_a = set(values_a)
            distinct_b = set(values_b)
            min_repeats = 3
            # counters to count how many times each token appears at the two positions
            counts_a = Counter(values_a)
            counts_b = Counter(values_b)
            # if either position has a token that appears too few, skip
            if min(counts_a.values()) < min_repeats \
                or min(counts_b.values()) < min_repeats:
                continue

            # observed pairs are the unique combinations of tokens at the two positions
            observed_pairs = set(zip(values_a, values_b))
            # full bijection means each unique token in A maps to a unique token in B
            is_full_biject = len(observed_pairs) == len(distinct_a) == len(distinct_b)
            # trusted 1D function means one position maps to other with a small domain
            is_trusted_one_directional_function = False
            # if the second position is the last token, we can trust a 1D function
            if pos_b == token_count - 1:
                # maximum number of unique tokens allowed to avoid overfitting
                max_function_domain_size = 5
                # check if either pos is a function of the other with a small domain
                a_to_b_is_function = (
                    len(observed_pairs) == len(distinct_a)
                    and len(distinct_a) <= max_function_domain_size)
                b_to_a_is_function = (
                    len(observed_pairs) == len(distinct_b)
                    and len(distinct_b) <= max_function_domain_size)
                # if either direction is a trusted function, we can link the positions
                is_trusted_one_directional_function = a_to_b_is_function \
                                                    or b_to_a_is_function

            # if the two are linked by a full bijection or a trusted 1D function
            if is_full_biject or is_trusted_one_directional_function:
                # split by the linked (pos_a, pos_b) value pair
                groups: dict[tuple[str, str], list[str]] = {}
                for path in paths:
                    # recursively call _paths_to_fmts on each group to find the fmts
                    key = (tokenized[path][pos_a], tokenized[path][pos_b])
                    groups.setdefault(key, []).append(path)
                # if there is more than one group of paths
                if len(groups) > 1:
                    result = []
                    # recursively call _paths_to_fmts to find the fmts
                    for group_paths in groups.values():
                        result.extend(_paths_to_fmts(group_paths))
                    # return the combined fmts from all groups
                    return result
    # keep track of delimiters for fmt reconstruction
    delimiters = {path: _delimiters(path) for path in paths}

    # dictionary to index paths by (token_count, token_position, token_value)
    position_index: dict[tuple[int, int, str], set[str]] = {}
    # order the paths to ensure consistent processing order for reproducibility
    path_order = {path: index for index, path in enumerate(paths)}
    # for each path and its tokens
    for path, tokens in tokenized.items():
        # for each token position
        for index, token in enumerate(tokens):
            # index the path (token_count, token_position, token_value) for clustering
            position_index.setdefault((len(tokens), index, token), set()).add(path)

    # paths to process in the loop, initially all paths are remaining
    remaining_paths = list(paths)
    # unique set of remaining paths for quick membership checks
    remaining_set = set(remaining_paths)
    fmts = []
    while remaining_paths:
        # check that every path shares at least one token
        reference_tokens = tokenized[remaining_paths[0]]
        token_count = len(reference_tokens)
        # filter the remaining paths to only those with the same number of tokens
        compatible_paths = [
            path for path in remaining_paths
            if len(tokenized[path]) == token_count]
        has_any_fixed_position = any(
            # set will be length 1 if all paths have the same token at this index
            len({tokenized[s][index] for s in compatible_paths}) == 1
            for index in range(token_count))

        # cluster found
        if has_any_fixed_position:
            cluster = list(compatible_paths)
        else:
            # base the anchor around the first non-assigned token
            anchor = remaining_paths[0]
            anchor_tokens = tokenized[anchor]
            # candidate_paths are those that share at least one token with the anchor
            candidate_paths: set[str] = set()

            for index, token in enumerate(anchor_tokens):
                # update with paths that have the same token at this position
                candidate_paths.update(
                    position_index.get((token_count, index, token),set()))
            # remove any candidate paths that are not in the remaining set
            candidate_paths.intersection_update(remaining_set)
            # also remove the anchor itself to avoid self-matching
            candidate_paths.discard(anchor)

            # group the candidate paths by positions where they share tokens with anchor
            paths_by_shared_positions: dict[tuple[int, ...], list[str]] = {}
            # sort the candidate paths by their original order for reproducibility
            for path in sorted(candidate_paths, key=path_order.__getitem__,):
                tokens = tokenized[path]
                # shared_positions are the where the candidate path matches the anchor
                shared_positions = tuple(i for i in range(len(anchor_tokens))
                                         if tokens[i] == anchor_tokens[i])
                # if there are any tokens that match the anchor
                if shared_positions:
                    # group the path by its shared positions with the anchor
                    paths_by_shared_positions.setdefault(shared_positions, []
                                                         ).append(path)

            # if no candidate paths share any positions with the anchor
            if not paths_by_shared_positions:
                # remove the anchor from remaining paths and continue to next iteration
                remaining_paths.remove(anchor)
                remaining_set.remove(anchor)
                continue

            # find the cluster with most members that share positions with the anchor
            _, best_members = max(paths_by_shared_positions.items(), \
                                  key=lambda kv: len(kv[1]))
            cluster = [anchor] + best_members

        # build the fmt directly from the cluster
        cluster_tokenized = [tokenized[path] for path in cluster]
        fmt_tokens = []
        field_count = 0
        has_fixed = False
        # for each token position, check if all paths have the same token or not
        for index, token in enumerate(cluster_tokenized[0]):
            values = [tokens[index] for tokens in cluster_tokenized]
            # if every path has the same token at this position
            if len(set(values)) == 1:
                fmt_tokens.append(token)
                has_fixed = True
            # if the token is different for any, its a parameter
            else:
                observed = set(values)
                # try to infer a parameter name from the observed values
                inferred_name = _infer_param(observed, fallback=f"p{field_count}")
                fmt_tokens.append(f"{{{inferred_name}}}")
                field_count += 1

        # reconstruct the fmt from tokens and delimiters
        cluster_delims = delimiters[cluster[0]]
        fmt_candidate = _join_tokens_with_delims(fmt_tokens, cluster_delims)

        # only add the fmt if it has at least one fixed token or more than one path
        if has_fixed or len(cluster) > 1:
            fmts.append(fmt_candidate)

        # remove all the paths that fit this fmt
        cluster_set = set(cluster)
        # update the remaining set to remove paths that are now part of the cluster
        remaining_set.difference_update(cluster_set)
        remaining_paths = [p for p in remaining_paths if p not in cluster_set]

    return fmts


def _align(first_tokens: list[str], second_tokens: list[str]) \
                    -> tuple[set[int], bool] | None:
    """
    Used by combine_alike_fmts.
    Determine the positions where two token lists differ and whether they have
    any genuine matches (i.e., tokens that are the same and not parameters).

    Args:
        first_tokens: List of tokens from the first path or fmt.
        second_tokens: List of tokens from the second path or fmt.

    Returns:
        A tuple containing a set of differing positions and a boolean indicating
        if there are any genuine matches, or None if the token lists cannot be aligned.
    """
    # if the two token lists are not the same length, they cannot be aligned
    if len(first_tokens) != len(second_tokens):
        return None

    differing_positions: set[int] = set()
    has_genuine_match = False
    # for each token position, check if the tokens differ or are parameters
    for index, (first_token, second_token) in enumerate(
        zip(first_tokens, second_tokens)):
        # if either token is a parameter or they are different
        if (_is_param(first_token) or _is_param(second_token)
            or first_token != second_token):
            differing_positions.add(index)
        # if both tokens are the same and not parameters, we have a genuine match
        else:
            has_genuine_match = True

    return differing_positions, has_genuine_match


def _is_param(token: str) -> bool:
    """
    Used by _align, _literal_tokens, _known_param_tags, _matching_paths,
    _finalize_param_names, _rescue_unmatched_paths, merge_fmts_sharing_all_literals,
    and combine_alike_fmts.
    Check if a token is a parameter (i.e., enclosed in curly braces).

    Args:
        token: The token string to check.

    Returns:
        True if the token is a parameter, False otherwise.
    """
    return bool(_PARAM_TOKEN_RE.fullmatch(token))


def _is_generic_param(token: str) -> bool:
    """
    Used by is_generic_tail, reconcile_region,
    and merge_fmts_sharing_all_literals.
    Check if a token is a generic parameter of the form {pN}.

    Args:
        token: The token string to check.

    Returns:
        True if the token is a generic parameter, False otherwise.
    """
    return _GENERIC_PARAM_RE.fullmatch(token) is not None


def _generic_param_names(
        *token_lists: list[str], used: set[int] | None = None) -> Iterator[str]:
    """
    Used by _rescue_unmatched_paths, merge_fmts_sharing_all_literals,
    and combine_alike_fmts.
    Yield successive unused "pN" names, e.g. for pulling one via next(...), or
    repeatedly from a single generator instance to keep avoiding each other.

    By default, "used" numbers are those already taken by a "{pN}" token in any of
    token_lists. Pass `used` explicitly to override this with a caller-computed set
    (e.g. one that also counts suffixed positional names like "{p0date}").

    Args:
        *token_lists: Token lists to scan for existing "{pN}" tokens, to avoid
            allocating a name that collides with one already in use.
        used: Pre-computed set of N values to avoid, overriding the default scan.

    Yields:
        The next unused "pN" name, each time this generator is advanced.
    """
    # if no used set is provided, compute it from the token lists
    if used is None:
        # for each token in each token list, if it matches the generic parameter pattern
        # extract the number N and add it to the used set
        used = {
            int(m.group(1))
            for tokens in token_lists
            for tok in tokens
            if (m := _GENERIC_PARAM_CAPTURE_RE.fullmatch(tok))}
    # start from 0 and yield the next unused "pN" name
    n = 0
    # while n is not in the used set, yield "pN" and increment n
    while True:
        if n not in used:
            used.add(n)
            yield f"p{n}"
        n += 1


def _literal_tokens(
    tokens: list[str], *, alphanumeric_only: bool = False) -> list[str]:
    """
    Used by _rescue_unmatched_paths, observed_slot_values,
    and combine_alike_fmts.
    Return the literal (non-parameter) tokens from a token sequence.

    Args:
        tokens: List of tokens to filter.
        alphanumeric_only: If True, only return tokens that contain alphanumeric chars.

    Returns:
        List of literal tokens.
    """
    # if alphanumeric_only is True,
    # filter out tokens that do not contain any alphanumeric characters
    return [token for token in tokens
            if (not _is_param(token)
                and (not alphanumeric_only
                     or any(character.isalnum() for character in token)))]


def _majority_tokens(token_groups: list[list[str]]) -> set[str]:
    """
    Used by _rescue_unmatched_paths and combine_alike_fmts.
    Return the set of tokens that appear in more than half of the token groups.

    Args:
        token_groups: A list of lists of tokens, where each inner list represents
            the tokens from a single path or fmt.

    Returns:
        A set of tokens that are present in more than half of the token groups.
    """
    # if token_groups is empty, return an empty set
    if not token_groups:
        return set()

    # count the presence of each token across all token groups
    presence_counts = Counter(
        token for tokens in token_groups for token in set(tokens))
    # determine the threshold for majority presence (more than half of the groups)
    threshold = len(token_groups) / 2
    # return the set of tokens that appear in more than half of the token groups
    return {
        token for token, count in presence_counts.items() if count > threshold}


def _join_tokens_with_delims(tokens: list[str], delims: list[str]) -> str:
    """
    Used by _paths_to_fmts, _finalize_param_names, _collapse_freeform_tails,
    _rescue_unmatched_paths, merge_fmts_sharing_all_literals, combine_alike_fmts,
    and detect_fmt.
    Reconstruct a fmt (or path) string from its tokens and the delimiters
    that originally separated them.

    Args:
        tokens: The tokens (literal text or "{name}" parameters) to join.
        delims: The delimiter characters between them, one fewer than
            len(tokens).

    Returns:
        The reconstructed string or nothing if tokens is empty.
    """
    if not tokens:
        return ""
    parts = [tokens[0]]
    # reconstruct the fmt string with the delimiters in between the tokens
    for tok, delim in zip(tokens[1:], delims):
        parts.append(delim)
        parts.append(tok)
    return "".join(parts)


def _is_drive_path(path: Path) -> bool:
    """
    Used by detect_fmt.
    Return True if path looks like a drive/archive file.

    Args:
        path: Path object representing the file or directory to check.

    Returns:
        True if the path looks like a drive/archive file, False otherwise.
    """
    lower_name = path.name.lower()
    # check for multi-part archive suffixes first
    if any(lower_name.endswith(ext) for ext in _MULTI_PART_DRIVE_EXTS):
        return True
    # check for single-part archive suffixes
    return path.suffix.lower() in DRIVE_EXTENSIONS


def _matching_paths(
        fmt: str,
        data_paths: list[str],
        path_tokens_cache: dict[str, list[str]] | None = None,
        ) -> list[str]:
    """
    Used by _finalize_param_names, compatible_paths, matches_by_fmt,
    and _known_param_tags.
    Return paths whose token sequence is compatible with *fmt*.

    Args:
        fmt: The format string to match against.
        data_paths: List of data path strings to check.
        path_tokens_cache: Optional cache of tokenized paths.

    Returns:
        List of paths that match the given format string.
    """
    fmt_tokens = _tokens(fmt)
    fmt_delimiters = _delimiters(fmt)
    # cache the tokenized paths to avoid redundant tokenization if not provided
    cache = path_tokens_cache if path_tokens_cache is not None else _token_cache(
        data_paths)

    # list of paths that match the fmt based on token sequence compatibility
    matches = []
    for path in data_paths:
        path_tokens = cache[path]
        # if the number of tokens matches, the delimiters match, and each token either
        # matches the fmt token or is a parameter
        if (len(path_tokens) == len(fmt_tokens) and _delimiters(path) == fmt_delimiters
            and all(_is_param(fmt_token) or fmt_token == path_token
                for fmt_token, path_token in zip(fmt_tokens, path_tokens))):
            matches.append(path)
    return matches


def _known_param_tags(
        fmt: str,
        data_paths: list[str],
        path_tokens_cache: dict[str, list[str]] | None = None,
        matches: list[str] | None = None,
        ) -> set[str]:
    """
    Used by detect_fmt.
    Return known parameter names supported by *fmt*'s observed values.

    Args:
        fmt: The format string to match against.
        data_paths: List of data path strings to check.
        path_tokens_cache: Optional cache of tokenized paths.
        matches: Optional list of paths that match the fmt.

    Returns:
        Set of known parameter names supported by the fmt's observed values.
    """
    fmt_tokens = _tokens(fmt)
    # find the positions of any parameters in the fmt tokens
    param_positions = [
        index for index, token in enumerate(fmt_tokens) if _is_param(token)]
    # cache the tokenized paths to avoid redundant tokenization if not provided
    cache = path_tokens_cache if path_tokens_cache is not None else _token_cache(
        data_paths)
    # if matches is not provided, find all paths that match the fmt
    matched_paths = matches if matches is not None else _matching_paths(
        fmt, data_paths, path_tokens_cache=cache)

    # for each parameter position, collect the observed values from the matching paths
    values_by_position = {position: set() for position in param_positions}
    for path in matched_paths:
        path_tokens = cache[path]
        for position in param_positions:
            values_by_position[position].add(path_tokens[position])

    # tags are the known parameter names that match the observed values at each position
    tags = set()
    for values in values_by_position.values():
        for name, known_values in KNOWN_PARAM_VALUES.items():
            if values and values <= known_values:
                tags.add(name)
                break
    return tags


def _finalize_param_names(
        fmt: str,
        data_paths: list[str],
        path_tokens_cache: dict[str, list[str]] | None = None,
        matches: list[str] | None = None,
        ) -> str:
    """
    Used by finalize_supported.
    Renumber "pN" fallback parameter names sequentially and disambiguate any known
    parameter name collisions across all paths that match the fmt.

    Args:
        fmt: The fmt string to finalize parameter names for.
        data_paths: List of data paths to check against the fmt.
        path_tokens_cache: Optional dictionary mapping paths to their tokenized forms.
        matches: Optional list of paths that match the fmt. If not provided, will be
            computed from data_paths and fmt.

    Returns:
        The fmt string with finalized parameter names.
    """
    tokens = _tokens(fmt)
    delims = _delimiters(fmt)
    # create a cache of tokenized paths if not provided to avoid redundant tokenization
    cache = (path_tokens_cache if path_tokens_cache is not None
             else _token_cache(data_paths))
    # if matches is not provided, find all paths that match the fmt
    matches = matches if matches is not None else _matching_paths(fmt, data_paths,
                                                                path_tokens_cache=cache)

    # find the positions of any parameters in the fmt tokens
    param_positions = [i for i, t in enumerate(tokens) if _is_param(t)]
    # signatures are the observed values for each parameter position across all matches
    signatures_by_pos: dict[int, tuple[str, ...]] = {}
    # for each parameter position, collect the observed values from the matching paths
    for pos in param_positions:
        # create a list of observed values for this param position across all matches
        sig_vals = []
        for path in matches:
            path_tokens = cache[path]
            sig_vals.append(path_tokens[pos])
        # if there are no matches, the signature is an empty tuple
        signatures_by_pos[pos] = tuple(sig_vals)

    # new tokens are built by renumbering any positional parameters and disambiguating
    new_tokens: list[str] = []
    # the next number is used for renumbering positional parameters (pN) sequentially
    next_number = 0
    # track what names have been seen so far to track duplicates
    used_names: set[str] = set()
    # assigned by_base is used to track which parameter names have been assigned to what
    assigned_by_base: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    # used to track which positional parameter names have been assigned
    assigned_positional_names: dict[str, str] = {}

    # for each token in the fmt, check if it's a parameter and handle accordingly
    for idx, token in enumerate(tokens):
        # if the token is not a parameter, keep it as-is
        if not _is_param(token):
            new_tokens.append(token)
            continue
        # 1 and -1 are the curly braces, so the name is everything in between
        name = token[1:-1]
        # positional parameters are of the form pN, where N is a number
        positional = _POSITIONAL_NAME_RE.match(name)
        # if there is a positional parameter, renumber it sequentially
        if positional is not None:
            # get the candidate name for this positional parameter
            candidate = assigned_positional_names.get(name)
            # if this positional parameter has not been assigned a name yet, assign it
            if candidate is None:
                # candidate generated by taking the next num and appending any suffix
                candidate = f"p{next_number}{positional.group(1)}"
                next_number += 1
                assigned_positional_names[name] = candidate
                used_names.add(candidate)
            # this is a new parameter name, so add it to the new tokens
            new_tokens.append(f"{{{candidate}}}")
            continue

        # signature is the observed values for this param position across all matches
        sig = signatures_by_pos.get(idx, tuple())
        # a bucket is a list of (signature, assigned_name) pairs for a given base name
        bucket = assigned_by_base.setdefault(name, [])
        # if this signature has already been assigned a name, reuse that name
        reused = next((assigned for existing_sig, assigned in bucket
                       if existing_sig == sig), None)
        if reused is not None:
            # this is a new parameter name, so add it to the new tokens
            new_tokens.append(f"{{{reused}}}")
            continue

        # if this signature has not been assigned a name, assign a new name
        if not bucket and name not in used_names:
            assigned = name
        else:
            # disambiguate by appending a number to the base until it's unique
            n = 2
            # assigned is the candidate name that will be used for this signature
            assigned = f"{name}{n}"
            while assigned in used_names:
                n += 1
                assigned = f"{name}{n}"

        # mark the assigned name as used and add it to the bucket for this base name
        used_names.add(assigned)
        bucket.append((sig, assigned))
        new_tokens.append(f"{{{assigned}}}")

    # reconstruct the fmt string with the delimiters in between the tokens
    return _join_tokens_with_delims(new_tokens, delims)


def _collapse_freeform_tails(fmts: list[str]) -> list[str]:
    """
    Used by finalize_supported.
    Collapse fmts that share a common prefix and suffix but have freeform tails
    (i.e., parameters in the middle) into a single fmt with a generic parameter.

    Args:
        fmts: List of fmt strings to collapse.

    Returns:
        List of collapsed fmt strings, where fmts with freeform tails have been
        combined into a single fmt with a generic parameter.
    """
    # tokenize every fmt once to avoid redundant tokenization
    tokenized = _token_cache(fmts)
    delimiters = {f: _delimiters(f) for f in fmts}

    # list of fmts that have not yet been processed for collapsing
    remaining = list(fmts)
    # result is the list of fmts after collapsing freeform tails
    result = []

    while remaining:
        # use the first remaining fmt as the anchor for this iteration
        anchor = remaining[0]
        anchor_tokens = tokenized[anchor]

        # find all other fmts that share the same first and last token as the anchor
        # and also share the same first and last delimiter as the anchor
        candidates = [anchor] + [
            other for other in remaining[1:]
            if tokenized[other][0] == anchor_tokens[0]
            and tokenized[other][-1] == anchor_tokens[-1]
            and delimiters[other][:1] == delimiters[anchor][:1]
            and delimiters[other][-1:] == delimiters[anchor][-1:]]

        # if there are fewer than 2 candidates, there is nothing to collapse
        if len(candidates) < 2:
            # add the anchor to the result and remove it from remaining
            result.append(anchor)
            remaining.remove(anchor)
            continue

        min_len = min(len(tokenized[c]) for c in candidates)
        # if the minimum length is less than 3, there is not enough room to collapse
        max_prefix_len = min_len - 2
        prefix_len = 0
        # for each index in the range of the maximum prefix length
        for idx in range(max(max_prefix_len, 0)):
            # check if all candidates share the same token at this index
            values_at_idx = {tokenized[c][idx] for c in candidates}
            if len(values_at_idx) != 1:
                # if they don't, break out of the loop as the common prefix ends here
                break
            # if they do, update the prefix length
            prefix_len = idx + 1

        def is_generic_tail(c: str) -> bool:
            """
            Check if the fmt c has a generic tail, meaning that all tokens after the
            shared prefix and before the last token are parameters of the form {pN}.
            Args:
                c: The fmt string to check.
            Returns:
                True if the fmt has a generic tail, False otherwise.
            """
            # middle tokens are those after the shared prefix and before the last token
            middle = tokenized[c][prefix_len:-1]
            # check if there is at least one middle token
            # and all of them are parameters of the form {pN}
            return len(middle) >= 1 and all(
                _is_generic_param(tok) for tok in middle)

        # are collapsible if they have a generic tail after the shared prefix
        collapsible = [c for c in candidates if is_generic_tail(c)]
        # if there are fewer than 2 collapsible fmts or the shared prefix length is 0
        if len(collapsible) < 2 or prefix_len == 0:
            # nothing to collapse, add anchor to result and remove from remaining
            result.append(anchor)
            remaining.pop(0)
            # go to the next iteration of the while loop to process the next anchor
            continue

        # sample used to construct the new collapsed fmt
        sample = collapsible[0]
        # the prefix tokens are those that are shared across all collapsible fmts
        prefix_tokens = tokenized[sample][:prefix_len]
        # final token is the last token of the sample shared across all collapsible fmts
        final_token = tokenized[sample][-1]
        # track the numbers used in the positional parameters to avoid collisions
        used_numbers = {
            int(m.group(1)) for tok in prefix_tokens
            # if the token matches the pattern {pN}, add N to used_numbers
            if (m := _GENERIC_PARAM_CAPTURE_RE.fullmatch(tok))}
        next_number = 0
        # find the next available number for the new positional parameter
        while next_number in used_numbers:
            next_number += 1

        # construct the new collapsed fmt
        new_tokens = prefix_tokens + [f"{{p{next_number}}}"] + [final_token]
        new_delims = delimiters[sample][:prefix_len] + [delimiters[sample][-1]]
        collapsed = _join_tokens_with_delims(new_tokens, new_delims)
        # add the new collapsed fmt to the result and remove fmts from remaining
        result.append(collapsed)
        collapsible_set = set(collapsible)
        # remove all collapsible fmts from the remaining list to avoid reprocessing
        remaining = [fmt for fmt in remaining if fmt not in collapsible_set]

    # return the list of fmts after collapsing freeform tails
    return result


def _rescue_unmatched_paths(fmts: list[str], unmatched_paths: list[str]) -> list[str]:
    """
    Used by detect_fmt.
    Attempt to rescue unmatched paths by aligning them with existing fmts and
    creating new fmts that include a new parameter for the unmatched portion.

    Args:
        fmts: List of existing fmt strings.
        unmatched_paths: List of paths that did not match any existing fmt.

    Returns:
        List of fmt strings with rescued unmatched paths included.
    """
    # result_fmts is a copy of the input that will be modified to include rescued paths
    result_fmts = list(fmts)
    # cache the tokenized forms of the fmts to avoid redundant tokenization
    fmt_tokens_cache = _token_cache(fmts)
    # create a set of common tokens that appear in more than half of the fmts
    common_tokens = _majority_tokens([
        _literal_tokens(tokens) for tokens in fmt_tokens_cache.values()])

    for path in unmatched_paths:
        path_tokens = _tokens(path)

        # best is the fmt that has the longest shared prefix and suffix with the path
        best_idx = None
        best_prefix_len = 0
        best_suffix_len = 0
        # iterate over each fmt and check how well it aligns with the unmatched path
        for idx, fmt in enumerate(result_fmts):
            fmt_tokens = _tokens(fmt)
            # min length is the length of the shorter of the two token lists
            min_len = min(len(fmt_tokens), len(path_tokens))

            # counts if the tokens match and neither is a param or common token
            def is_distinctive_match(pair: tuple[str, str]) -> bool:
                """Check if the pair of tokens are a distinctive match
                (same literal, not a param, not common)."""
                # unpack the pair of tokens from fmt and path
                fmt_tok, path_tok = pair
                return (fmt_tok == path_tok and not _is_param(fmt_tok)
                        and fmt_tok not in common_tokens)

            # prefix/suffix length is how many matching tokens run from that end
            prefix_len = sum(1 for _ in takewhile(is_distinctive_match,
                                                  zip(fmt_tokens, path_tokens)))
            suffix_len = sum(1 for _ in takewhile(is_distinctive_match,
                                    zip(reversed(fmt_tokens), reversed(path_tokens))))
            # if the combined length is greater than or equal to the minimum length
            if prefix_len + suffix_len >= min_len:
                # skip this fmt because it is not a genuine match (it would overlap)
                continue
            # if both lengths are at least 1, consider this fmt for alignment
            if prefix_len >= 1 and suffix_len >= 1:
                # if this fmt has a longer combined prefix and suffix than the best
                if prefix_len + suffix_len > best_prefix_len + best_suffix_len:
                    # update the best values to this fmt's index and lengths
                    best_idx = idx
                    best_prefix_len = prefix_len
                    best_suffix_len = suffix_len
        # if no fmt was found that aligns with the unmatched path, skip to the next path
        if best_idx is None:
            continue

        # reconstruct the fmt with a new parameter in the middle
        fmt = result_fmts[best_idx]
        fmt_tokens = _tokens(fmt)
        fmt_delims = _delimiters(fmt)
        prefix_tokens = fmt_tokens[:best_prefix_len]
        suffix_tokens = fmt_tokens[len(fmt_tokens) - best_suffix_len:]
        # construct the new fmt with the shared prefix, new parameter, and shared suffix
        new_param = next(_generic_param_names(prefix_tokens, suffix_tokens))
        new_tokens = prefix_tokens + [f"{{{new_param}}}"] + suffix_tokens
        # reconstruct the delimiters to match the new token structure
        new_delims = (
            fmt_delims[:best_prefix_len - 1]
            + [fmt_delims[best_prefix_len - 1]]
            + [fmt_delims[len(fmt_tokens) - best_suffix_len - 1]]
            + fmt_delims[len(fmt_tokens) - best_suffix_len:])
        result_fmts[best_idx] = _join_tokens_with_delims(new_tokens, new_delims)

    # return the list of fmts after rescuing unmatched paths
    return result_fmts


def merge_fmts_sharing_all_literals(
        fmts: list[str],
        data_paths: list[str],
        path_tokens_cache: dict[str, list[str]] | None = None,
        ) -> list[str]:
    """
    Merge fmts that share the same literal tokens (non-parameter tokens) into a single
    fmt with new merged parameters. This is useful for collapsing fmts that differ
    only in their parameter names or positions but share the same literal structure.

    Args:
        fmts: List of fmt strings to merge.
        data_paths: List of data paths to check against the fmts.
        path_tokens_cache: Optional dictionary mapping paths to their tokenized forms.
    Returns:
        List of merged fmt strings, where fmts that share the same literal tokens have
        been combined into a single fmt with new merged parameters.
    """
    # cache the parsed matches for each fmt to avoid redundant computation
    @lru_cache(maxsize=None)
    def parsed_matches(fmt: str) -> set[str]:
        """Return and cache the paths accepted by *fmt*."""
        return _parsed_paths(fmt, data_paths)

    def preserves_source_matches(candidate: str, *sources: str) -> bool:
        """Return whether candidate retains every source-matched path."""
        candidate_matches = parsed_matches(candidate)
        # check if all source fmts have their matched paths in the candidate's matches
        return all(parsed_matches(source) <= candidate_matches for source in sources)

    # cache used to avoid redundant tokenization when finding observed slot values
    cache = (path_tokens_cache if path_tokens_cache is not None
            else _token_cache(data_paths))

    # cache the compatible paths for each fmt to avoid redundant computation
    @lru_cache(maxsize=None)
    def compatible_paths(fmt: str) -> list[str]:
        """Return the paths that are compatible with *fmt* based on token sequence."""
        return _matching_paths(fmt, data_paths, path_tokens_cache=cache)

    def observed_slot_values(fmt: str, slot_len: int, slot_start_idx: int) -> set[str]:
        """finds the observed values for a slot in the fmt across all matching paths"""
        # if the slot length is 0, there are no values to observe, return an empty set
        if slot_len == 0:
            return set()
        values: set[str] = set()
        # for each path that is compatible with the fmt
        for path in compatible_paths(fmt):
            # extract the tokens corresponding to the slot
            path_tokens = cache[path]
            path_delims = _delimiters(path)
            # extract the span of tokens corresponding to the slot in the path
            span = path_tokens[slot_start_idx:slot_start_idx + slot_len]
            # reconstructed string of the slot tokens with their original delimiters
            joined = span[0]
            for tok, delim in zip(span[1:], path_delims[slot_start_idx:]):
                joined += delim + tok
            values.add(joined)
        # return the set of observed values for this slot across all matching paths
        return values

    # tokenize every fmt once to avoid redundant tokenization
    tokenized = _token_cache(fmts)
    # create a mapping of each fmt to its tuple of literal tokens for comparison
    literal_sequences = {fmt: tuple(_literal_tokens(tokens))
                         for fmt, tokens in tokenized.items()}
    # remaining fmts that have not yet been processed for merging
    remaining = list(fmts)
    # result is the list of fmts after merging those that share all literal tokens
    result = []

    while remaining:
        # anchor is the first remaining fmt to use as a reference for merging
        anchor = remaining[0]
        # get all the tokens of the anchor fmt to compare with others
        anchor_literals = literal_sequences[anchor]
        # find all fmts that share the same literal sequence as the anchor
        group = [anchor] + [
            other for other in remaining[1:]
            if literal_sequences[other] == anchor_literals]

        # if there is only the anchor in the group, it cannot be merged with others
        if len(group) == 1:
            # add the anchor to the result and remove it from remaining
            result.append(anchor)
            remaining.remove(anchor)
            # skip to the next iteration of the while loop to process the next anchor
            continue

        # merge all fmts in the group into a single fmt
        merged = group[0]
        # list of other fmts in the group to merge with the anchor fmt
        unmerged_others: list[str] = []
        # iterate over the other fmts in the group to their parameters
        for other in group[1:]:

            tokens_merged = _tokens(merged)
            tokens_other = _tokens(other)
            delims_merged = _delimiters(merged)
            delims_other = _delimiters(other)


            lit_pos_merged = [i for i, t in enumerate(tokens_merged)
                              if not _is_param(t)]
            lit_pos_other = [i for i, t in enumerate(tokens_other) if not _is_param(t)]


            # avoid colliding with any "pN" numbers already used in tokens_merged
            next_generic = _generic_param_names(tokens_merged)

            def reconcile_region(region_merged: list[str], region_other: list[str],
                                 start_idx_merged: int, start_idx_other: int
                                 ) -> list[tuple[str, int, int]]:
                """
                merge two regions of tokens from the merged and other fmt,
                returning a list of tuples containing the merged token and
                its indices in both regions.
                """
                region_merged, region_other = list(region_merged), list(region_other)
                # prefix entries are the tokens at the start of both regions
                prefix_entries: list[tuple[str, int, int]] = []
                # while both regions have tokens and the first tokens are the same,
                # and either both regions have one token or the delimiters match
                while region_merged and region_other \
                      and region_merged[0] == region_other[0] \
                      and ((len(region_merged) == 1) == (len(region_other) == 1) \
                      and (len(region_merged) == 1 or \
                     delims_merged[start_idx_merged] == delims_other[start_idx_other])):
                    # append the token and its index to the prefix entries
                    prefix_entries.append((region_merged[0], start_idx_merged,
                                           start_idx_other))
                    # remove the first token from both regions and increment the start
                    region_merged, region_other = region_merged[1:], region_other[1:]
                    start_idx_merged += 1
                    start_idx_other += 1

                # suffix entries are the tokens at the end of both regions
                suffix_entries: list[tuple[str, int, int]] = []
                end_idx_merged = start_idx_merged + len(region_merged) - 1
                end_idx_other = start_idx_other + len(region_other) - 1
                # while both regions have tokens and the last tokens are the same,
                # and either both regions have one token or the delimiters match
                while region_merged and region_other \
                    and region_merged[-1] == region_other[-1] \
                    and ((len(region_merged) == 1) == (len(region_other) == 1)
                    and (len(region_merged) == 1 or delims_merged[end_idx_merged - 1] \
                                                   == delims_other[end_idx_other - 1])):
                    # append the token and its index to the suffix entries at the front
                    suffix_entries.insert(0, (region_merged[-1], end_idx_merged,
                                              end_idx_other))
                    # remove the last token from both regions and decrement the end idx
                    region_merged, region_other = region_merged[:-1], region_other[:-1]
                    end_idx_merged -= 1
                    end_idx_other -= 1

                # middle is the remaining tokens after removing the prefix and suffix
                middle_entries: list[tuple[str, int, int]] = []
                # if the remaining regions have the same length
                if len(region_merged) == len(region_other):
                    # reconcile them position by position
                    for k, (ta, tb) in enumerate(zip(region_merged, region_other)):
                        # use the generic token if one side is non-generic
                        token = ta if ta == tb else (
                            tb if _is_generic_param(ta) else ta)
                        middle_entries.append((token, start_idx_merged + k,
                                               start_idx_other + k))
                # if either region still has tokens
                elif region_merged or region_other:
                    # if a region has exactly one token and it is not generic, use it
                    if len(region_merged) == 1 \
                        and not _is_generic_param(region_merged[0]):
                        token = region_merged[0]
                    elif len(region_other) == 1 \
                        and not _is_generic_param(region_other[0]):
                        token = region_other[0]
                    # otherwise, infer a new parameter name based on the observed values
                    else:
                        # get the values inside the slot for both regions and combine
                        observed = observed_slot_values(merged, len(region_merged),
                                                        start_idx_merged)
                        observed |= observed_slot_values(other, len(region_other),
                                                         start_idx_other)
                        # create a fallback name to use if inference fails
                        fallback = "__unresolved__"
                        # try to infer a parameter name based on the observed value
                        inferred = _infer_param(observed, fallback=fallback)
                        # use fallback if inference failed
                        token = f"{{{inferred}}}" if inferred != fallback \
                            else f"{{{next(next_generic)}}}"
                    middle_entries.append((token, start_idx_merged, start_idx_other))

                return prefix_entries + middle_entries + suffix_entries

            # entries is the list of merged tokens and their indices in both fmts
            entries: list[tuple[str, int, int]] = []
            # previous indices to track the last processed literal
            prev_merged, prev_other = -1, -1
            # for each pair of literal positions in the merged and other fmt
            for pos_merged, pos_other in zip(lit_pos_merged, lit_pos_other):
                # merge the regions between the previous literal and the current literal
                entries.extend(reconcile_region(
                    tokens_merged[prev_merged + 1:pos_merged],
                    tokens_other[prev_other + 1:pos_other],
                    prev_merged + 1, prev_other + 1))
                # add the current literal token and its indices to the entries
                entries.append((tokens_merged[pos_merged], pos_merged, pos_other))
                # update the previous indices to the current literal positions
                prev_merged, prev_other = pos_merged, pos_other

            # merge any remaining tokens after the last literal
            entries.extend(reconcile_region(
                tokens_merged[prev_merged + 1:], tokens_other[prev_other + 1:],
                prev_merged + 1, prev_other + 1))
            # if there are no entries after reconciliation
            if not entries:
                # cannot merge these fmts, so add to unmerged_others and continue
                unmerged_others.append(other)
                continue

            # reconstruct the merged fmt from the reconciled entries and delimiters
            parts = [entries[0][0]]
            for token, src_idx_merged, src_idx_other in entries[1:]:
                # determine the appropriate delimiter to use based on the source indices
                delim = (delims_merged[src_idx_merged - 1] if 0 <= src_idx_merged - 1
                         < len(delims_merged) else delims_other[src_idx_other - 1])
                parts.append(delim)
                parts.append(token)
            # join the parts to form the candidate merged fmt
            candidate = "".join(parts)
            # make sure the candidate preserves the source matches for both fmts
            if preserves_source_matches(candidate, merged, other):
                merged = candidate
            else:
                # if the candidate does not preserve matches, cannot merge these fmts
                unmerged_others.append(other)

        # after merging all fmts in the group, add the merged fmt to the result
        result.append(merged)
        result.extend(unmerged_others)
        group_set = set(group)
        # get rid of the fmts that were part of this group from the remaining list
        remaining = [fmt for fmt in remaining if fmt not in group_set]
    # renumber the generic parameters in the final result to ensure they are sequential
    for i, fmt in enumerate(result):
        tokens = _tokens(fmt)
        delims = _delimiters(fmt)
        next_number = 0
        new_tokens = []
        for token in tokens:
            # if the token is a generic param, replace it with the next number
            if _is_generic_param(token):
                new_tokens.append(f"{{p{next_number}}}")
                next_number += 1
            else:
                # otherwise, keep the token as-is
                new_tokens.append(token)
        # reconstruct the fmt string with the delimiters in between the tokens
        result[i] = _join_tokens_with_delims(new_tokens, delims)

    return result


def combine_alike_fmts(
        fmts: list[str],
        known_param_tags: dict[str, set[str]] | None = None,
        ) -> list[str]:
    """
    Combine fmts when ordered lit substrings are compatible and alignment is genuine;
    optionally allow near-structure matches via known parameter tags.

    Args:
        fmts: List of fmt strings to combine.
        known_param_tags: Dictionary mapping fmt strings to sets of known parameter tags

    Returns:
        List of combined fmt strings, one per distinct pattern found.
    """
    fmts = list(fmts)
    result = []
    used = [False] * len(fmts)
    known_param_tags = known_param_tags or {}

    # tokenize every fmt once
    tokens_cache = [_tokens(fmt) for fmt in fmts]

    # find each fmt's individual literal (non-parameter) tokens
    raw_literal_tokens = {index: _literal_tokens(tokens, alphanumeric_only=True)
                          for index, tokens in enumerate(tokens_cache)}
    # if a token appears in more than half of the fmts, it is considered common
    common_tokens = _majority_tokens(list(raw_literal_tokens.values()))
    # create a cache of literal token for each fmt, excluding the common tokens
    distinctive_tokens_cache = {
        idx: [tok for tok in toks if tok not in common_tokens]
        for idx, toks in raw_literal_tokens.items()}

    # for each fmt, find all other fmts that share at least one literal token
    for i in range(len(fmts)):
        # skip if this fmt has already been merged into a previous group
        if used[i]:
            continue
        # group every fmt sharing at least one literal token with fmts[i]
        group = [i]
        # known parameter tags for the current fmt, if any
        i_tags = known_param_tags.get(fmts[i], set()) - _NON_DISCRIMINATIVE_TAGS
        # tokens for the current fmt, excluding any parameter tokens
        # avoid double counting by only comparing fmts that haven't been merged yet
        for j in range(i + 1, len(fmts)):
            # don't compare the fmt to itself
            if used[j]:
                continue
            # known parameter tags for the other fmt, if any
            j_tags = known_param_tags.get(fmts[j], set()) - _NON_DISCRIMINATIVE_TAGS
            should_group = False

            # check which fmt is longer to use as the frame for alignment
            if len(fmts[i]) >= len(fmts[j]):
                longer_idx, shorter_idx = i, j
            else:
                longer_idx, shorter_idx = j, i
            longer_tokens = tokens_cache[longer_idx]
            shorter_distinctive = distinctive_tokens_cache[shorter_idx]
            ordered_literals_match = False
            # if the shorter fmt has no distinctive literal tokens, it can't be aligned
            if shorter_distinctive:
                pos = 0
                ordered_literals_match = True
                for tok in shorter_distinctive:
                    # find the next occurrence of the literal segment in the longer fmt
                    found_at = -1
                    # for each token in the longer fmt from the last found position
                    for idx in range(pos, len(longer_tokens)):
                        # if the token matches the literal segment
                        if longer_tokens[idx] == tok:
                            found_at = idx
                            # stop searching once the literal segment is found
                            break
                    # if the literal segment is not found, the fmts can't be aligned
                    if found_at < 0:
                        ordered_literals_match = False
                        break
                    # record position after the found literal segment for next search
                    pos = found_at + 1

            # the difference in token counts between the two fmts
            token_count_gap = abs(len(tokens_cache[i]) - len(tokens_cache[j]))
            if ordered_literals_match and token_count_gap == 0:
                ti = tokens_cache[i]
                tj = tokens_cache[j]
                # try to align the two fmts
                aligned = _align(ti, tj)
                # if they are aligned
                if aligned is not None:
                    # check if the alignment is genuine
                    _diff_positions, genuine = aligned
                    if genuine:
                        should_group = True

            # check if the two fmts share any known parameter tags
            # and have the same token count
            tag_match = bool(i_tags & j_tags) and token_count_gap == 0
            # if should_group or (tag_match and ordered_literals_match):
            if should_group or tag_match:
                # add the other fmt to the group for either case being true
                group.append(j)

        # if the group only contains one fmt, it won't be merged with anything else
        if len(group) == 1:
            result.append(fmts[i])
            used[i] = True
            continue

        # use the longest fmt in the group as the frame to align everything else
        frame_idx = max(group, key=lambda idx: len(tokens_cache[idx]))
        frame_tokens = tokens_cache[frame_idx]

        # positions that differ to turn into parameters
        all_diff_positions = set()
        any_genuine = False
        for idx in group:
            # don't compare the frame to itself
            if idx == frame_idx:
                continue
            # align the frame fmt to the other fmt, marking any differing positions
            diff_positions, genuine = _align(frame_tokens, tokens_cache[idx])
            # the union of previously differing positions and the new positions
            all_diff_positions |= diff_positions
            any_genuine = any_genuine or genuine

        # no genuine matches or every position differs
        if not any_genuine or len(all_diff_positions) == len(frame_tokens):
            # append the frame fmt as-is, since it can't be merged with anything else
            result.append(fmts[i])
            used[i] = True
            continue

        new_tokens = []
        # collect the numbers of any parameters to avoid duplicates
        used_numbers = {
            # group 1 is the number in a positional parameter like {pN}
            int(match.group(1))
            for token in frame_tokens
            if _is_param(token)
            # for each token, check if it matches the pattern for a positional parameter
            for match in [_POSITIONAL_TOKEN_RE.match(token)]
            if match}
        # get a generator for new parameter names that avoids any used numbers
        allocate_param = _generic_param_names(used=used_numbers)

        for idx, token in enumerate(frame_tokens):
            # if this position differs across the group, make it a parameter
            if idx in all_diff_positions:
                # if already a parameter, keep it as-is
                if _is_param(token):
                    new_tokens.append(token)
                else:
                    new_tokens.append(f"{{{next(allocate_param)}}}")
            else:
                # if this position is the same across the group, keep the token as-is
                new_tokens.append(token)
        delims = _delimiters(fmts[frame_idx])
        # append the merged fmt to the result list
        result.append(_join_tokens_with_delims(new_tokens, delims))

        # mark all fmts in the group as used so they won't be merged again
        for idx in group:
            used[idx] = True
    return result

def scan_inventory(root: Path) -> list[str]:
    """
    Scan a directory and return all file paths found, recursively.

    Args:
        root: Path to the dataset root directory to scan.

    Returns:
        List of relative file path strings found under root.

    Raises:
        FileNotFoundError: If the provided root path does not exist.
        NotADirectoryError: If the provided root path is not a directory.
    """
    root = Path(root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"{root} does not exist")
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    # recursively iterate through the directory and collect all file paths
    return sorted(
        file.relative_to(root).as_posix() for file in iter_repository_files(root))


def detect_fmt(rel_paths: list[str], include_drives: bool = False) -> list[str]:
    """
    Auto-detect reusable formats from a collection of relative paths.

    Args:
        rel_paths: List of relative file path strings to analyze.
        include_drives: Whether to include drive paths in the detection.

    Returns:
        List of detected format strings.
    """
    # data paths are the relative paths that are not meta files or duplicates
    data_paths = []
    # normalize the relative paths to use forward slashes and remove duplicates
    normalized_paths = sorted({str(rel_path).replace("\\", "/")
                               for rel_path in rel_paths})
    for rel_path in normalized_paths:
        path = Path(rel_path)
        # skip if the path is a drive path and include_drives is False
        if not include_drives and _is_drive_path(path):
            continue
        # skip if the file extension is a known meta file extension
        if path.suffix.lstrip(".").lower() in META_EXTS_LOWER:
            continue
        # skip if the file stem (name without extension) is a known static file name
        if path.stem.lower() in KNOWN_STATIC_FILE_STEMS:
            continue
        # add the relative path to the data paths list
        data_paths.append(rel_path)

    # create a cache of tokenized forms of the paths to avoid redundant tokenization
    path_tokens_cache = _token_cache(data_paths)
    def matches_by_fmt(candidates: list[str], paths: list[str]) -> dict[str, list[str]]:
        """Match several formats using the shared path-token cache."""
        return {
            fmt: _matching_paths(fmt, paths, path_tokens_cache=path_tokens_cache)
                 for fmt in candidates}

    # save the original data paths for later use in anchor format detection
    original_data_paths = data_paths

    # A one-token path can anchor wider paths that contain it literally.
    anchor_fmts = []
    # set of anchor paths that have already been consumed to avoid duplicate processing
    consumed_anchor_paths = set()
    # if there are paths that consist of a single token, attempt to find wider matches
    for anchor in (path for path in data_paths
                   if len(path_tokens_cache[path]) == 1):
        # skip this anchor if it has already been consumed in a previous iteration
        if anchor in consumed_anchor_paths:
            continue
        # matches are paths that contain the anchor token and have not been consumed yet
        matches = [
            path for path in data_paths
            if path != anchor
            and path not in consumed_anchor_paths
            and anchor in path_tokens_cache[path]]
        # if there are no matches for this anchor, skip to the next anchor
        if not matches:
            continue

        # group the matches by their structural signature, which includes token count,
        # delimiters, and the positions of the anchor token within the path
        structural_groups: dict[
            tuple[int, tuple[str, ...], tuple[int, ...]], list[str]] = {}
        for path in matches:
            tokens = path_tokens_cache[path]
            # create a signature that uniquely identifies the structure of the path
            signature = (
                len(tokens),
                tuple(_delimiters(path)),
                # record the positions of the anchor token in the path tokens
                tuple(
                    index for index, token in enumerate(tokens) if token == anchor))
            # group the paths by their structural signature to find reusable formats
            structural_groups.setdefault(signature, []).append(path)

        anchor_was_used = False
        for structural_matches in structural_groups.values():
            # skip if there are fewer than 2 matches for this structural group,
            # as it cannot form a reusable format
            if len(structural_matches) < 2:
                continue

            # use the first path as the representative for generating the format
            representative = structural_matches[0]
            field_count = 0
            fmt_tokens = []
            # iterate over the tokens of the representative path to construct the format
            for token in path_tokens_cache[representative]:
                if token == anchor:
                    fmt_tokens.append(token)
                # if the token is a parameter (already in braces), keep it as-is
                else:
                    fmt_tokens.append(f"{{p{field_count}}}")
                    field_count += 1
            # add the constructed format to the list of anchor formats
            anchor_fmts.append(
                _join_tokens_with_delims(fmt_tokens, _delimiters(representative)))
            # update the set of consumed anchor paths to include all paths in this group
            consumed_anchor_paths.update(structural_matches)
            anchor_was_used = True
        # if the anchor token was used to create any formats, mark it as consumed
        if anchor_was_used:
            consumed_anchor_paths.add(anchor)

    # remove consumed anchor paths from the data paths to avoid reprocessing
    data_paths = [
        path for path in data_paths if path not in consumed_anchor_paths]

    # Paths with identical delimiter layouts can be compared positionally.
    paths_by_delimiters: dict[tuple[str, ...], list[str]] = {}
    for path in data_paths:
        paths_by_delimiters.setdefault(tuple(_delimiters(path)), []).append(path)

    # candidates is the list of fmts generated from the grouped paths by delimiters
    candidates = []
    for _, grouped_paths in sorted(
            paths_by_delimiters.items(), key=lambda item: len(item[0])):
        candidates.extend(_paths_to_fmts(grouped_paths))
    # candidate_matches is a mapping of each candidate fmt to its matching paths
    candidate_matches = matches_by_fmt(candidates, data_paths)
    # known_tags is a mapping of each candidate fmt to its known parameter tags
    known_tags = {
        fmt: _known_param_tags(
            fmt,
            data_paths,
            path_tokens_cache=path_tokens_cache,
            matches=candidate_matches[fmt])
        for fmt in candidates}
    # filter the candidates to only include those that have parameter tokens
    fmts = [
        fmt for fmt in combine_alike_fmts(
            candidates, known_param_tags=known_tags)
        if _PARAM_TOKEN_RE.search(fmt)]

    def finalize_supported(candidates: list[str], paths: list[str]) -> list[str]:
        """
        Keep formats with two matches and finalize their parameter names.
        Args:
            candidates: List of candidate format strings to evaluate.
            paths: List of data paths to match against the candidates.
        Returns:
            List of finalized fmts that have at least two matches in the paths.
        """
        # create a mapping of each candidate fmt to its matching paths
        matches = matches_by_fmt(candidates, paths)
        # only retain fmts that have at least two matches and finalize their param names
        return [
            _finalize_param_names(
                fmt,
                paths,
                path_tokens_cache=path_tokens_cache,
                matches=matches[fmt])
            for fmt in candidates if len(matches[fmt]) >= 2]

    # finalize supported fmts and anchor fmts by ensuring they have at least two matches
    fmts = finalize_supported(fmts, data_paths)
    anchor_fmts = finalize_supported(anchor_fmts, original_data_paths)
    # merge fmts that share all literal tokens and collapse freeform tails
    fmts = merge_fmts_sharing_all_literals(
        _collapse_freeform_tails(fmts),
        data_paths,
        path_tokens_cache=path_tokens_cache)
    # matched_paths is the set of all paths that match any of the fmts
    matched_paths = set().union(*(
        _parsed_paths(fmt, data_paths) for fmt in fmts)) if fmts else set()
    # unmatched_paths is the list of paths that did not match any of the fmts
    unmatched_paths = [
        path for path in data_paths if path not in matched_paths]
    # if there are any unmatched paths, attempt to rescue them by creating new fmts
    if unmatched_paths:
        fmts = _rescue_unmatched_paths(fmts, unmatched_paths)

    # sorted combined list of anchor fmts and other fmts, removing duplicates
    return sorted({*anchor_fmts, *fmts})