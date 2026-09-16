from __future__ import annotations

import re
import shutil
import string
import time
from functools import lru_cache
from pathlib import Path
from string import Formatter
from urllib.parse import unquote, urlsplit

import pandas as pd
import parse

from .repo import Repo
from .transport import OperationContext, RemoteSpec
from .transport.base import (
    CapabilityError, DownloadError, RemoteObjectMissing, reject_controls)
from .fmt_detection import (
    detect_fmt,
    KNOWN_PROCESSING_STAGES,
    KNOWN_STATIC_FILE_STEMS)
from .dothm import dump_yaml
from .error import DothmError
from .helper_functions import (
    CHECKSUM_ALGORITHMS,
    CHECKSUM_ALGORITHMS_BY_STRENGTH,
    CHECKSUM_ALGORITHM_PATTERN,
    SUPPORTED_CHECKSUM_ALGORITHMS,
    valid_checksum,
    atomic_output_path,
    load_yaml_file,
    normalize_nonempty_string,
    prompt_choice,
    validate_path_component,
    validate_relative_path)
from .repo_config import (
    fmt_fields,
    normalize_tsv_name,
    normalize_remotes,
    fmt_entries_from_config)

# base URL for the CyVerse curated data repository
_CYVERSE_CURATED_BASE = \
    "https://data.cyverse.org/dav-anon/iplant/commons/cyverse_curated/"
# regular expression to match rows in an HTML index page for a directory listing
_INDEX_ROW_RE = re.compile(
    r'<tr class="object (collection|data-object[^"]*)">'
    r'<td class="name"><a href="([^"]+)"')
# regex to match lines in a checksum file, capturing the checksum and filename
_SUMS_LINE_RE = re.compile(r"^([0-9a-fA-F]{8,}) [ *](.+)$", re.MULTILINE)
# regex to match checksum file names, capturing the base name and algorithm
_SUMS_FILENAME_RE = \
    re.compile(rf"^(?P<name>.+)\."rf"(?P<algorithm>{CHECKSUM_ALGORITHM_PATTERN})sums$")
# keywords to identify checksum files based on their names
_CHECKSUM_NAME_KEYWORDS = ("sum", "checksum", "hash", "manifest", *CHECKSUM_ALGORITHMS)
# maximum number of worker threads for static checksum computation
_STATIC_CHECKSUM_MAX_WORKERS = 8
# maximum number of worker threads for remote crawling and checksum computation
_REMOTE_CRAWL_MAX_WORKERS = 8
# minimum ratio of matching lines in a checksum file to consider it valid
_MANIFEST_LINE_MATCH_RATIO = 0.5
# regular expression to match checksum algorithms in file names, case-insensitive
_ALGORITHM_IN_NAME_RE = re.compile(rf"({CHECKSUM_ALGORITHM_PATTERN})", re.IGNORECASE)
# default checksum algorithm to use when the algorithm cannot be determined
_UNKNOWN_CHECKSUM_ALGORITHM = "unknown"
# maximum file size (in bytes) for which we will compute a checksum
_MAX_DOWNLOAD_SIZE_FOR_CHECKSUM = 10 * 1024 * 1024  # 10 MB
# maximum length of a literal segment in a format string that can be cleared
_MAX_CLEARABLE_LITERAL_LENGTH = 2
# known field values for specific format fields, used for parsing and matching
KNOWN_FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "kind": KNOWN_PROCESSING_STAGES,
    "algorithm": CHECKSUM_ALGORITHMS}


def _remote_url(base_url: str, relative_path: str) -> str:
    """Render a transport URL from a literal path, preserving directory slashes."""
    url = RemoteSpec.parse(base_url).file_url(relative_path)
    if relative_path.endswith("/") and not url.endswith("/"):
        url += "/"
    return url


# cache for performance, since the same fmt may be used for many files in a dataset
@lru_cache(maxsize=256)
def _get_fmt_match_data(fmt: str) -> tuple[tuple, parse.Parser, int]:
    """
    Used by _get_fmt_match_plan.
    Precompute the parse segments and parser for a given format string.

    Args:
        fmt: The format string to precompute match data for.

    Returns:
        A tuple containing:
        - segments: A tuple of (literal, name, format_spec, conversion) tuples
        - parser: The compiled parse parser.
        - literal_char_count: The count of literal characters in the format string.
    """
    # get the parsed segments from the format string using string.Formatter
    segments = tuple(Formatter().parse(fmt))
    # compile the format string into a parse parser for efficient matching
    parser = parse.compile(fmt)
    # count the number of literal characters in the format string by removing
    # all field placeholders and measuring the length of the remaining string
    literal_char_count = len(re.sub(r"\{[^}]*\}", "", fmt))
    return segments, parser, literal_char_count


# cache for performance, since the same set of fmts may be used for many files
@lru_cache(maxsize=64)
def _get_fmt_match_plan(fmts: tuple[str, ...]) -> tuple[tuple, ...]:
    """
    Used by _match_static_named_file_plain_only and _match_file_against_fmts.
    Precompute the match plan for a list of format strings, sorting them by specificity.

    Args:
        fmts: A tuple of format strings to precompute match plans for.

    Returns:
        A tuple of tuples, where each inner tuple contains:
        - index: The index of the format string in the original fmts tuple.
        - segments: A tuple of (literal, name, format_spec, conversion) tuples.
        - parser: The compiled parse parser.
        - literal_char_count: The count of literal characters in the format string.
    """
    # sort the format indexes by their literal char count, from most to least specific
    sorted_indexes = sorted(
        range(len(fmts)), key=lambda index: -_get_fmt_match_data(fmts[index])[2])
    # return a tuple of (index, segments, parser, literal_char_count) for each fmt
    return tuple(
        (index, *_get_fmt_match_data(fmts[index])) for index in sorted_indexes)


def _leak_score(segments, result, greedy_names=frozenset()):
    """
    Used by _match_file_against_fmts and _match_static_named_file_plain_only.
    Compute a "leak score" for a parse result, which counts how many times
    the last character of a literal segment appears in the matched value
    of a non-greedy named field. A score of 0 means no leaks, which is ideal.

    Args:
        segments: List of (literal, name, format_spec, conversion) tuples
        result: The parse.Result object from parsing the rel_path string
        greedy_names: Set of names that are considered greedy and should be ignored

    Returns:
        An integer leak score, where 0 indicates no leaks
    """
    leak = 0
    # count how many times the last char of its literal appears in the matched value
    for lit, name, _fmt_spec, _conv in segments:
        # if the segment has no name or is greedy, skip it
        if name is None or name in greedy_names:
            continue
        value = str(result.named.get(name, ""))
        if lit and lit[-1] in string.punctuation:
            leak += value.count(lit[-1])
    return leak


def _drop_and_greedy_search(segments, rel_path):
    """
    Used by _match_file_against_fmts.
    Try all combinations of dropping fields and making fields greedy to find a
    leak-free parse result. Is more expensive than _greedy_only_search,
    so we only use it as a last resort.

    Args:
        segments: List of (literal, name, format_spec, conversion) tuples from fmt
        rel_path: The file path string to parse

    Returns:
        A tuple of (parse.Result, drop_count) if a leak-free parse is found,
        otherwise (None, None)
    """
    literal_positions = [] # list of (start_idx, end_idx) for each literal segment
    search_start_idx = 0
    # find all literal segments that are not matched in the rel_path
    # meaning that their corresponding fields must be dropped to find a match
    for lit, _name, _fs, _c in segments:
        if not lit:
            # if there are no literal characters in this segment, record its position
            # even tho no lit is present, we still need to track position for dropping
            literal_positions.append((search_start_idx, search_start_idx))
            continue
        # find the literal in the rel_path starting from the current search position
        idx = rel_path.find(lit, search_start_idx)
        # if the literal is not found, record None for its start and end positions
        if idx == -1:
            literal_positions.append((None, None))
        else:
            # if the literal is found, record its start and end positions
            literal_positions.append((idx, idx + len(lit)))
            # move the search start to the next position
            search_start_idx = idx + len(lit)

    # field names that appear more than once in the segments are considered repeated
    seen_names: set[str] = set()
    repeated_names: set[str] = set()
    for _lit, name, _fs, _c in segments:
        if name is None:
            continue
        if name in seen_names:
            # if a name is repeated it requires special handling
            repeated_names.add(name)
        else:
            seen_names.add(name)

    # for repeated names, we need to resolve their values based on the first occurrence
    resolved_values: dict[str, str] = {}
    # track names that are dropped because their literal segment was not found
    dropped_names: set[str] = set()
    # track the first occurrence of each repeated name along with its value
    first_seen: dict[str, tuple[int, str]] = {}
    # find the values of repeated names based on their literal positions in rel_path
    for idx, (_lit, name, _fs, _c) in enumerate(segments):
        # if the name is not repeated, we don't need to resolve it
        if not (name and name in repeated_names):
            continue
        _lit_start, lit_end = literal_positions[idx]
        # if the literal segment was not found, we cannot resolve this name
        if lit_end is None:
            # if the name was seen before, we can use its first occurrence's value
            if name in first_seen:
                resolved_values.setdefault(name, first_seen[name][1])
            # if name was not seen before, we drop it since we cannot resolve its value
            else:
                dropped_names.add(name)
            # skip to the next segment since we cannot resolve this one
            continue

        # next start is the start index of the next literal segment unless at the end
        next_lit_start = literal_positions[idx + 1][0] \
            if idx + 1 < len(segments) else None
        # the field is between the end of this literal and the start of the next literal
        field_end = next_lit_start if next_lit_start is not None else len(rel_path)
        # value is naive because it may not be correct
        naive_field_value = rel_path[lit_end:field_end]
        if name not in first_seen:
            # if this is the first occurrence of the repeated name, store its value
            first_seen[name] = (field_end, naive_field_value)
        # if this is not the first, check if the naive value matches the first
        elif name not in resolved_values and name not in dropped_names:
            first_field_end, first_field_value = first_seen[name]
            # if the first occurrence's value is found in the current field's value
            if first_field_value in rel_path[first_field_end:]:
                resolved_values[name] = first_field_value
            else:
                # if the naive value does not match the first, we drop this name
                dropped_names.add(name)

    # reconstruct the fmt with dropped names removed and repeated names resolved
    parts = []
    for lit, name, _fs, _c in segments:
        # always include the literal part of the segment in the reconstructed format
        parts.append(lit)
        if name in dropped_names:
            continue
        if name in resolved_values:
            # concatenate the literal and the resolved value for repeated names
            parts[-1] = lit + resolved_values[name]
            continue
        # is still an unresolved repeated name, so we include it in the format string
        if name is not None:
            parts.append("{" + name + (":" + _fs if _fs else "") + "}")

    raw_simplified_segments = list(Formatter().parse("".join(parts)))
    simplified_segments = []
    for lit, name, _fs, _c in raw_simplified_segments:
        # if the literal is too long, split it into two segments
        if name is not None and len(lit) > _MAX_CLEARABLE_LITERAL_LENGTH:
            simplified_segments.append((lit[:-1], None, None, None))
            simplified_segments.append((lit[-1:], name, _fs, _c))
        # if the literal is short enough, keep it as a single segment
        else:
            simplified_segments.append((lit, name, _fs, _c))

    # create a tuple of allowed values for each segment based on known field values
    allowed_values_by_segment = tuple(frozenset(KNOWN_FIELD_VALUES[name])
                            if name in KNOWN_FIELD_VALUES
                             and not format_spec else None
                            for _literal, name, format_spec, _conversion
                             in simplified_segments)
    # get set of global delimiters, which are the last characters of literal segments
    global_delimiters = {
        lit[-1] for lit, _name, _fs, _c in simplified_segments
        if lit and lit[-1] in string.punctuation}

    segment_count = len(simplified_segments)
    path_length = len(rel_path)

    @lru_cache(maxsize=None)
    def solve(segment_idx, path_idx):
        """
        Recursive function to explore all combinations of dropping fields and
        making fields greedy to find a leak-free parse result.
        Utilizes memoization to avoid recomputing the same subproblems.

        Args:
            segment_idx: Current index in the simplified_segments list.
                    prior to this index, all segments have been processed.
            path_idx: Current index in the rel_path string.

            Returns:
                A tuple (drop_count, leak_count, future_matches) if a valid parse,
                otherwise None.
        """
        # best case: all segments are processed
        if segment_idx == segment_count:
            # if the entire rel_path has been consumed, return a successful parse
            return (0, 0, ()) if path_idx == path_length else None

        # unpack the current segment into its components
        lit, name, _fs, _c = simplified_segments[segment_idx]
        # get the allowed values for this segment, if any
        allowed_values = allowed_values_by_segment[segment_idx]
        best = None # format is (drop_count, leak_count, future_matches)

        # if the segment has a name and no allowed values, we can consider dropping it
        if name is not None and not _fs and allowed_values is None:
            # if the literal is short enough, we can consider dropping this field
            if len(lit) <= _MAX_CLEARABLE_LITERAL_LENGTH:
                # recursively try dropping this field and moving to the next segment
                sub = solve(segment_idx + 1, path_idx)
                # if dropping the field leads to a valid parse, consider it a candidate
                if sub is not None:
                    # unpack the result of the recursive call
                    drop_count, leak_count, future_matches = sub
                    candidate = (drop_count + 1, leak_count, future_matches)
                    # if the candidate is better than the current best, update best
                    # is better if it has fewer drops, or same drops but fewer leaks
                    if best is None or candidate[:2] < best[:2]:
                        best = candidate
            # if the segment starts with a literal, try matching it against the rel_path
            if rel_path.startswith(lit, path_idx):
                # recursively try matching this field by moving to the next segment
                # and updating the path index to account for the matched literal
                sub = solve(segment_idx + 1, path_idx + len(lit))
                # same logic as above
                if sub is not None:
                    drop_count, leak_count, future_matches = sub
                    candidate = (drop_count + 1, leak_count, future_matches)
                    if best is None or candidate[:2] < best[:2]:
                        best = candidate

        # if the segment has no name, it is a literal that must be matched
        if rel_path.startswith(lit, path_idx):
            # the field starts immediately after the matched literal
            field_start = path_idx + len(lit)

            # name is None means this is a literal segment
            if name is None:
                # recursively try matching the next segment after this literal
                sub = solve(segment_idx + 1, field_start)
                if sub is not None:
                    if best is None or sub[:2] < best[:2]:
                        best = sub
            # if the segment has a name, it is a field that can be greedy
            else:
                # try all possible end positions for the field
                for end in range(field_start, path_length + 1):
                    # get the value of the field from the rel_path
                    value = rel_path[field_start:end]
                    # if allowed_values is provided, skip values that are not allowed
                    if allowed_values is not None and value not in allowed_values:
                        continue
                    # recursively try matching the next segment after this field
                    sub = solve(segment_idx + 1, end)
                    if sub is not None:
                        drop_count, leak_count, future_matches = sub
                        value = rel_path[field_start:end]
                        # the field is considered to have "leaked" if
                        # the boundary character appears in its value
                        extra_leak = sum(value.count(d) for d in global_delimiters)
                        candidate = (drop_count, leak_count + extra_leak,
                                     ((name, value),) + future_matches)
                        if best is None or candidate[:2] < best[:2]:
                            best = candidate
        return best

    # intial call for the recursive solve function
    result = solve(0, 0)
    # clear the cache to free memory after the search is complete
    solve.cache_clear()
    if result is None:
        return None, None

    drop_count, _leak_count, future_matches = result
    # build a dictionary of the matched field names and their values
    named = dict(future_matches)
    named.update(resolved_values)

    # return the matched named fields and the total number of dropped fields
    return named, drop_count + len(dropped_names)


def _match_static_named_file_plain_only(rel_path: str, fmt_entries: list[dict]
                                        ) -> tuple[int, dict] | tuple[None, None]:
    """
    Used by build_repo.
    Match a static named file against a list of fmt entries using only the plain parse.

    Args:
        rel_path: The file path to match.
        fmt_entries: The {"fmt", "db", "name"?} entry dicts.

    Returns:
        (entry_index, parse.Result), or (None, None) if nothing matches.
    """
    # create a tuple of all format strings from the fmt entries for sorting
    fmts = tuple(entry["fmt"] for entry in fmt_entries)
    # for each format string, try to parse the rel_path using the precomputed parser
    for index, segments, parser, _literal_count in _get_fmt_match_plan(fmts):
        result = parser.parse(rel_path)
        # if the plain parse is successful and has no leaks
        if result is not None and _leak_score(segments, result) == 0:
            # return the index and named fields from the parse result
            return index, result.named
    # if no format matches, return (None, None)
    return None, None


def _match_file_against_fmts(rel_path: str, fmt_entries: list[dict]):
    """
    Used by build_repo.
    Find the best fmt entry for one file, across all of them at once.

    Args:
        rel_path: The file path to match.
        fmt_entries: The {"fmt", "db", "name"?} entry dicts.

    Returns:
        (entry_index, parse.Result), or (None, None) if nothing matches.
    """
    # create a tuple of all format strings from the fmt entries for sorting
    fmts = tuple(entry["fmt"] for entry in fmt_entries)
    # list of fmts that successfully matched the rel_path
    valid_fmts = [] # [(index, result, matched_literal_count, drop_count)]
    # track the highest literal char count among the results
    running_baseline_literal_char_count = -1

    # from the precomputed match plan, try to parse the rel_path using each fmt
    for (i, segments, parser, literal_char_count) in _get_fmt_match_plan(fmts):
        # try to parse the rel_path using the precomputed parser for this fmt
        plain_result = parser.parse(rel_path)
        result = None
        drop_count = 0
        greedy_attempt = None

        # first pass: try the plain parse result, which is the cheapest and most common
        if plain_result is not None:
            # a leak score of 0 means that no non-greedy fields have "leaked" into the
            # matched value of a greedy field, which is ideal
            if _leak_score(segments, plain_result) == 0:
                result = plain_result.named
            else:
                # attempt a greedy parse to see if we can find a leak-free result
                greedy_attempt = _drop_and_greedy_search(segments, rel_path)
                greedy_result, greedy_drop_count = greedy_attempt
                # if the greedy parse is successful and has no drops, we can use it
                if greedy_result is not None and greedy_drop_count == 0:
                    result = greedy_result

        # if the literal_char_count is greater than the running baseline,
        # this fmt is more specific, so we should try a more expensive search
        if result is None and literal_char_count > running_baseline_literal_char_count:
            # only track non-trivial literals (length >= 2) to avoid false negatives
            literals = [lit for lit, _n, _fs, _c in segments if len(lit) >= 2]
            # check if the fmt ends with a literal
            trailing_literal = segments[-1][0] if segments[-1][1] is None else ""
            # won't match if trailing lit or any non-trivial lits are not in path
            skip = (
                (len(trailing_literal) >= 2 and trailing_literal not in rel_path)
                or (bool(literals) and not any(lit in rel_path for lit in literals)))
            # if we don't skip, attempt a greedy parse to find a leak-free result
            if not skip:
                    if greedy_attempt is None:
                        greedy_attempt = _drop_and_greedy_search(segments, rel_path)
                    result, drop_count = greedy_attempt

        # if no result was found for this fmt, continue to the next one
        if result is None:
            continue

        # more specific match found if drop_count 0 and literal_char_count > baseline
        if drop_count == 0:
            running_baseline_literal_char_count = \
                max(running_baseline_literal_char_count, literal_char_count)

        # number of matched literal characters in the result with respect to rel_path
        lit_count_matched_from_fmt = len(rel_path) - sum(
            len(str(v)) for v in result.values())
        # tracks raw fmt, valid parse result, matched literal count, and drop count
        valid_fmts.append((i, result, lit_count_matched_from_fmt, drop_count))

    if not valid_fmts:
        return None, None

    # the best format has the highest matched literal count with the lowest drop count
    best_index, best_parse, _lit_count_matched_from_fmt, _drop_count = max(
        valid_fmts, key=lambda m: (m[2], -m[3]))
    return best_index, best_parse

def _record_checksum(checksums, path, algorithm, checksum):
    previous_algorithm, previous = checksums.get(path, (None, None))
    if previous is not None and previous_algorithm == algorithm:
        if previous.lower() != checksum.lower():
            raise DownloadError(f"Conflicting {algorithm} manifests for {path!r}")
        return
    strength = {name: i for i, name in enumerate(CHECKSUM_ALGORITHMS_BY_STRENGTH)}
    if previous is None or strength.get(algorithm, 99) < strength.get(
            previous_algorithm, 100):
        checksums[path] = (algorithm, checksum)


def _manifest_matches(text: str, algorithm: str) -> list[tuple[str, str]]:
    """
    Used by list_remote_files.
    Parse a manifest text and return a list of (checksum, filename) tuples for each line
    in the manifest that has a valid checksum for the given algorithm.

    Args:
        text: The manifest text to parse.
        algorithm: The checksum algorithm to validate against.

    Returns:
        A list of (checksum, filename) tuples for each line in the manifest text
        that has a valid checksum for the given algorithm, allowing unknown algorithms.
    """
    if any(line.startswith("\\") for line in text.splitlines()):
        raise ValueError("GNU escaped manifest filenames are unsupported")
    # return a list of (checksum, filename) tuples for each line in the manifest text
    # if the checksum is valid for the given algorithm, allowing unknown algorithms
    return [
        (checksum, filename)
        for checksum, filename in _SUMS_LINE_RE.findall(text)
        if valid_checksum(
            algorithm, checksum, allow_unknown_algorithm=True)]

def _resolve_manifest_path(filename: str, rel_dir: str) -> str:
    """
    Used by list_remote_files.
    Resolve a manifest path relative to a given directory, ensuring it is valid.

    Args:
        filename: The manifest path to resolve.
        rel_dir: The relative directory to resolve against.

    Returns:
        A normalized and validated relative path as a POSIX-style string.

    Raises:
        ValueError: If the filename is empty after stripping whitespace,
                    or if the resolved path is not a valid relative path.
    """
    # strip whitespace from the filename to avoid issues with leading/trailing spaces
    filename = str(filename)
    reject_controls(filename, "Manifest path")

    # remove leading "./" from the filename to normalize the path
    while filename.startswith("./"):
        # start at the second character to remove the leading "./"
        filename = filename[2:]
    # if the filename is empty after stripping, raise a ValueError
    if not filename:
        raise ValueError("manifest path cannot be empty")

    # if the filename starts with the relative directory, it is already resolved
    if filename.startswith(rel_dir):
        resolved = filename
    # if the filename does not contain any slashes, prepend the relative directory
    elif "/" not in filename:
        resolved = rel_dir + filename
    # otherwise, the filename may need to be resolved relative to the directory
    else:
        # get the first segment of the filename (before the first slash)
        first_segment = filename.split("/", 1)[0]
        # remove any trailing slashes from the relative directory for comparison
        stripped_dir = rel_dir.rstrip("/")
        # create a suffix to check if the stripped directory ends with the first segment
        suffix = "/" + first_segment

        # if the stripped directory ends with the suffix, we can resolve the path
        if stripped_dir.endswith(suffix):
            # the prefix_end is the index of the last character of the suffix
            prefix_end = stripped_dir.rfind(suffix)
            # the missing prefix is the part of the relative directory before the suffix
            missing_prefix = rel_dir[:prefix_end + 1]
            # concatenate the missing prefix with the filename to get the resolved path
            resolved = missing_prefix + filename
        # otherwise, we cannot resolve the path and will use the filename as-is
        else:
            resolved = filename

    # validate the resolved path to ensure it is a valid relative path and
    # return it as a POSIX-style string
    return validate_relative_path(resolved, label="manifest path").as_posix()

def _normalize_index_href(href: str, *, is_directory: bool) -> str:
    """
    Used by list_remote_files.
    Normalize an index href to ensure it is a valid relative path.

    Args:
        href: The href to normalize.
        is_directory: A boolean indicating whether the href represents a directory.

    Returns:
        The normalized href as a POSIX-style string.

    Raises:
        ValueError: If the href is not a valid relative path.
    """
    # use urlsplit to check for scheme, netloc, query, and fragment
    reject_controls(str(href), "Index href")
    parsed = urlsplit(str(href))

    # if any components are present, raise a ValueError as the href must be relative
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"remote index path must be relative: {href!r}")

    # use unquote to decode any percent-encoded characters in the path
    decoded_path = unquote(parsed.path)
    reject_controls(decoded_path, "Index path")
    # if the path is a directory, remove any trailing slashes; otherwise, keep it as-is
    candidate = (decoded_path.rstrip("/") if is_directory else decoded_path)
    # validate the candidate path to ensure it is a valid relative path
    # return it as a POSIX-style string
    normalized = validate_relative_path(candidate, label="remote index path").as_posix()

    # if the path is a directory, append a trailing slash to indicate it is a directory
    if is_directory:
        return normalized + "/"

    return normalized

def _normalize_fmt_entries(fmt_entries: list[dict]) -> list[dict]:
    """
    Used by build_repo.
    Normalize the fmt entries to ensure they are valid and consistent.

    Args:
        fmt_entries: A list of dictionaries representing format entries.

    Returns:
        A list of normalized format entries.

    Raises:
        ValueError: If fmt_entries is not a list, or if any entry is not a dictionary,
                    or if any entry does not have a non-empty "fmt" string,
                    or if any entry does not have a "db" key.
    """
    # check that fmt_entries is a list of dictionaries with required keys
    if not isinstance(fmt_entries, list):
        raise ValueError("fmt_entries must be a list")

    normalized = []
    seen_fmts: dict[str, int] = {}
    # for each entry in fmt_entries, validate and normalize it
    for index, entry in enumerate(fmt_entries):
        # if the entry is not a dictionary, raise an error
        if not isinstance(entry, dict):
            raise ValueError(
                f"fmt entry {index} must be a dictionary")
        # normalize the "fmt" string to ensure it is non-empty and valid
        normalized_fmt = normalize_nonempty_string(
            entry.get("fmt"), label=f"fmt entry {index} format")
        # if the entry does not have a "db" key, raise an error
        if "db" not in entry:
            raise ValueError(
                f"fmt entry {index} must define a db name")

        # index of a previously seen fmt that matches the normalized_fmt
        previous_index = seen_fmts.get(normalized_fmt)
        # if a duplicate fmt is found, raise an error indicating the conflict
        if previous_index is not None:
            raise ValueError(
                f"fmt entry {index} duplicates "
                f"fmt entry {previous_index}: "
                f"{normalized_fmt!r}")
        # record the normalized fmt and its index to track duplicates
        seen_fmts[normalized_fmt] = index

        # if all the checks pass, create a normalized copy of the entry
        normalized_entry = dict(entry)
        # add the normalized fmt to the entry to ensure consistency
        normalized_entry["fmt"] = normalized_fmt
        # normalize the database name using the provided function to ensure consistency
        normalized_entry["db"] = normalize_tsv_name(entry["db"])
        # add the normalized entry to the list of normalized entries
        normalized.append(normalized_entry)

    # return the list of normalized fmt entries
    return normalized


def list_remote_files(base_url: str, *, _context=None):
    """List a CyVerse HTML index (legacy default), or an authorized SSH source."""
    if _context is None:
        with OperationContext(RemoteSpec.parse(base_url)) as context:
            return _list_source(context)
    return _list_source(_context)


def _list_source(context):
    if context.remote.scheme in {"ssh", "sftp"}:
        return _list_ssh_files(context)
    return CyverseHtmlIndex(context).list_files()


class CyverseHtmlIndex:
    """Adapter for CyVerse's HTML directory-index dialect, not WebDAV PROPFIND."""

    def __init__(self, context):
        self.context = context

    def list_files(self):
        return _list_cyverse_files(self.context)


def _list_cyverse_files(context):
    def fetch_text(path):
        return context.read_text(path)

    def optional_text(path):
        try:
            return fetch_text(path)
        except RemoteObjectMissing:
            return None

    # stores all files with their checksums, keyed by relative path, for all algorithms
    file_checksums: dict[str, tuple[str | None, str | None]] = {}

    directories_to_open = [""]
    visited_directories: set[str] = set()
    # raw index text for directories that have been fetched but not yet processed
    prefetched_indexes: dict[str, str] = {}
    # Use a requests session and ThreadPoolExecutor to fetch remote indexes concurrently
    with context.executor(_REMOTE_CRAWL_MAX_WORKERS) as crawl_executor:
        # while there are directories to open or prefetched indexes to process
        while directories_to_open or prefetched_indexes:
            # if there are no prefetched indexes, fetch a batch of directories to open
            if not prefetched_indexes:
                batch = []
                # while there are directories to open and we haven't reached max workers
                while (directories_to_open and len(batch) < _REMOTE_CRAWL_MAX_WORKERS):
                    # pop a directory from the stack to process
                    candidate = directories_to_open.pop()
                    # skip if this directory has already been visited to avoid cycles
                    if candidate in visited_directories:
                        continue
                    # mark directory as visited and add it to the batch for fetching
                    visited_directories.add(candidate)
                    if len(visited_directories) > context.listing_limit:
                        raise DownloadError("Remote listing exceeds its entry limit")
                    batch.append(candidate)
                # if the batch is empty, continue to the next iteration of the loop
                if not batch:
                    continue

                # construct the full URLs for the batch of directories to fetch
                directory_urls = batch
                # fetch the index texts for the batch of directories concurrently
                index_texts = crawl_executor.map(fetch_text, directory_urls)
                # update the prefetched indexes with the fetched index texts
                prefetched_indexes.update(zip(batch, index_texts))

            # get the next directory to process from the prefetched indexes
            rel_dir = next(iter(prefetched_indexes))
            # pop the index text for this directory from the prefetched indexes
            index_text = prefetched_indexes.pop(rel_dir)
            # parse the index text to extract entries using regex
            raw_entries = _INDEX_ROW_RE.findall(index_text)
            if (len(file_checksums) + len(raw_entries) > context.listing_limit):
                raise DownloadError("Remote listing exceeds its entry limit")
            entries = []
            for entry_type, href in raw_entries:
                # try to normalize the href to ensure it is a valid relative path
                try:
                    normalized_href = _normalize_index_href(
                        href,
                        is_directory=(entry_type == "collection"))
                # if the href is invalid, skip this entry and continue to the next one
                except ValueError:
                    # Directory indexes often include a parent navigation link.
                    if href in {"../", "./", "/"}:
                        continue
                    raise
                # add the normalized entry to the list of entries for this directory
                entries.append((entry_type, normalized_href))

            # find manifest files that are not directories and match expected patterns
            manifest_hrefs = [href for entry_type, href in entries
                             if (entry_type != "collection"
                             and (_SUMS_FILENAME_RE.match(href)
                                or any(keyword in href.lower()
                                   for keyword in _CHECKSUM_NAME_KEYWORDS)))]
            # call _remote_url to construct the full URLs for the manifest files
            manifest_urls = [
                rel_dir + href for href in manifest_hrefs]
            # crawl the manifest URLs concurrently to fetch their text contents
            manifest_texts = crawl_executor.map(optional_text,
                                                manifest_urls)
            # create a dictionary mapping manifest hrefs to their fetched text contents
            manifest_text_by_href = {
                href: text for href, text in zip(manifest_hrefs, manifest_texts)
                if text is not None}
            # track only files explicitly covered by parsed manifest lines
            files_covered_by_manifest: set[str] = set()

            def record_manifest_matches(
                matches: list[tuple[str, str]], algorithm: str) -> None:
                """
                Record matches from a manifest file into the file_checksums dictionary.
                Args:
                    matches: List of (checksum, filename) extracted from the manifest.
                    algorithm: The checksum algorithm used in the manifest.
                """
                for checksum, filename in matches:
                    try:
                        full_path = _resolve_manifest_path(filename, rel_dir)
                    except ValueError:
                        raise

                    _record_checksum(file_checksums, full_path, algorithm, checksum)
                    files_covered_by_manifest.add(full_path)

            # Iterate over each entry in the current directory
            for entry_type, href in entries:
                # if the entry is a collection (directory), skip it for now
                if entry_type == "collection":
                    continue
                # get the manifest text for this href from the prefetched manifest texts
                text = manifest_text_by_href.get(href)
                # skip if manifest text is None, meaning the file could not be fetched
                if text is None:
                    continue
                # Match against expected pattern to extract covered directory and algo
                sibling_match = _SUMS_FILENAME_RE.match(href)
                if sibling_match:
                    algorithm = sibling_match.group("algorithm")
                    # match if the checksum is valid for the given algorithm
                    matches = _manifest_matches(text, algorithm)
                    # add each path and its checksum to the file_checksums dictionary
                    record_manifest_matches(matches, algorithm)
                    # go to the next entry since this one has been processed
                    continue

                # lines in manifest text that are not empty after stripping whitespace
                lines = [line for line in text.splitlines() if line.strip()]

                # search for the checksum algorithm in the filename using regex
                name_match = _ALGORITHM_IN_NAME_RE.search(href)
                # if a match is found, use the matched algorithm; otherwise, use unknown
                algorithm = (name_match.group(1).lower() if name_match
                             else _UNKNOWN_CHECKSUM_ALGORITHM)
                # only matches that are valid for the given algorithm are considered
                matches = _manifest_matches(text, algorithm)
                # if the file is empty or the number of valid matches is too low, skip
                if (not lines or len(matches) < _MANIFEST_LINE_MATCH_RATIO * len(lines)
                ):
                    continue
                # record the matches from the manifest into file_checksums dictionary
                record_manifest_matches(matches, algorithm)

            # After processing all entries, add any uncovered directories to the stack
            for entry_type, href in entries:
                rel_path = rel_dir + href
                if entry_type == "collection":
                    directories_to_open.append(rel_path)
                else:
                    # add files not explicitly covered by any manifest line with None
                    if rel_path not in files_covered_by_manifest:
                        file_checksums.setdefault(rel_path, (None, None))
    # return the complete mapping of relative paths to their checksums organized by algo
    return file_checksums


def _list_ssh_files(context):
    paths = context.transport.list_entries()
    result = {path: (None, None) for path in paths}
    for path in paths:
        name = Path(path).name
        sibling = _SUMS_FILENAME_RE.match(name)
        if not sibling and not any(k in name.lower() for k in _CHECKSUM_NAME_KEYWORDS):
            continue
        text = context.read_text(path)
        name_match = _ALGORITHM_IN_NAME_RE.search(name)
        algorithm = (sibling.group("algorithm") if sibling else
                     name_match.group(1).lower() if name_match else "unknown")
        matches = _manifest_matches(text, algorithm)
        lines = [line for line in text.splitlines() if line.strip()]
        if not sibling and (not lines or len(matches) < len(lines) * 0.5):
            continue
        directory = str(Path(path).parent)
        directory = "" if directory == "." else directory + "/"
        for checksum, filename in matches:
            target = _resolve_manifest_path(filename, directory)
            if target in result:
                _record_checksum(result, target, algorithm, checksum)
    if context.remote_hash:
        for path, (_, digest) in result.items():
            if digest is None:
                result[path] = context.transport.checksum_small(path)
    return result


def build_repo(
    repo_path: Path,
    dataset_name: str,
    fmt_entries: list[dict] | None = None,
    config_file: Path | str | None = None,
    remotes: list[dict | str] | dict | str | None = None,
    overwrite: bool = False,
    *,
    dataset_url: str | None = None,
    dataset_auth: str | None = None,
    index_format: str | None = None,
    allow_remote_commands: bool = False,
    remote_hash: bool = False,
) -> "Repo":
    """Build catalogs from an explicit dataset root or the legacy CyVerse root.

    ``dataset_auth`` selects a host-bound local SSH profile independently of
    output ``remotes``. Non-default HTTP roots require ``index_format`` set to
    ``cyverse-html``. SSH crawling requires ``allow_remote_commands=True``, a
    POSIX shell and server Python 3. Server SHA-256 hashing is opt-in through
    ``remote_hash`` and bounded to 10 MiB/file, 100 MiB and 60 seconds in total.
    """
    dataset_name = validate_path_component(dataset_name, label="dataset name")
    base_url = (dataset_url if dataset_url is not None else
                _remote_url(_CYVERSE_CURATED_BASE, f"{dataset_name}/"))
    source = RemoteSpec.parse(base_url, dataset_auth)
    if source.scheme in {"http", "https"}:
        if index_format not in {None, "cyverse-html"}:
            raise CapabilityError("Supported HTTP index format: cyverse-html")
        if dataset_url is not None and index_format is None:
            raise CapabilityError(
                "Explicit HTTP roots require --index-format cyverse-html")
        if remote_hash:
            raise CapabilityError("--remote-hash is supported only for SSH sources")
    elif not allow_remote_commands:
        raise CapabilityError(
            "SSH builds require --allow-remote-commands; "
            "SFTP-only accounts can download")
    elif index_format is not None:
        raise CapabilityError("--index-format applies only to HTTP sources")
    with OperationContext(source, allow_remote_commands=allow_remote_commands,
                          remote_hash=remote_hash) as context:
        return _build_repo(repo_path, dataset_name, fmt_entries, config_file,
                           remotes, overwrite, context)


def _build_repo(
    repo_path: Path,
    dataset_name: str,
    fmt_entries: list[dict] | None = None,
    config_file: Path | str | None = None,
    remotes: list[dict | str] | dict | str | None = None,
    overwrite: bool = False,
    source=None,
    ) -> "Repo":
    """
    Build a hallmark repository from the given dataset and format entries.
    Creates separate TSV files for each fmt, and one config.yml file with the manifest.

    Args:
        repo_path: Path where the hallmark repo will be created.
        fmt_entries: dict entries with "fmt", "db", and optional "name" keys.
         If None, the function will attempt to detect the format entries automatically.
        config_file: Path to an existing config.yml file to load fmt entries from.
        remotes: Optional list of remote repos to add to the repo. Each remote can be a
         dict with "name" and "url" keys, or a str representing the name of the remote.

    Returns:
        The initialized Repo object, sitting on the main branch.
    """
    # validate the dataset name to ensure it is a valid path component
    dataset_name = validate_path_component(dataset_name, label="dataset name")
    # CyVerse's curated Data Commons datasets live at a predictable URL,
    # built directly from dataset_name.
    base_url = source.remote.url

    # Determine if remotes were provided by the user
    remotes_provided = remotes is not None
    # Normalize the remotes into a consistent list of dictionaries
    remotes = normalize_remotes(remotes)

    # if a config_file is provided
    if config_file is not None:
        # config file and fmt_entries are mutually exclusive
        if fmt_entries is not None:
            raise ValueError(
                "Cannot provide both fmt_entries and config_file. "
                "Choose one or the other.")
        # get the path to the config file, expanding ~ and resolving relative paths
        config_path = Path(config_file).expanduser()
        # if the config path is a directory, assume it contains a config.yml file
        if config_path.is_dir():
            config_path = config_path / "config.yml"
        # if the config file does not exist, raise a FileNotFoundError
        if not config_path.is_file():
            raise FileNotFoundError(f"Config file does not exist: {config_path}")
        # load the config.yml file and extract the fmt entries
        loaded_config = load_yaml_file(config_path)
        # extract the fmt entries from the loaded config
        fmt_entries = fmt_entries_from_config(loaded_config)
        # raise a ValueError if no fmt entries were found in the config file
        if not fmt_entries:
            raise ValueError(f"No fmt entries found in config file {config_path}.")
        # if remotes were not provided and the loaded config has a "remote" key
        if not remotes_provided and loaded_config.get("remote"):
            # normalize the remotes from the loaded config and use them
            remotes = normalize_remotes(loaded_config["remote"])

    repo_path = Path(repo_path).expanduser().resolve()
    existing_repo = None
    existing_fmt_entries = []
    # if the repo path already exists and there are no fmt entries provided
    if repo_path.exists() and fmt_entries is None:
        # try to load the existing repo and its fmt entries from config.yml
        try:
            existing_repo = Repo(repo_path)
            # extract the fmt entries from the existing repo's config.yml
            existing_fmt_entries = fmt_entries_from_config(existing_repo.state.config)
        # if the existing repo cannot be loaded, set existing_repo to None
        except DothmError:
            existing_repo = None
            existing_fmt_entries = []

    file_checksums = None
    remote_files = None
    def _ensure_remote_files_listed():
        """
        Used by build_repo.
        Ensure that the remote files have been listed and checksums collected.
        This function is called lazily to avoid unnecessary network requests if the
        user provides their own fmt entries or chooses to reuse an existing config.
        """
        # create nonlocal references to the outer variables so they can be modified
        nonlocal file_checksums, remote_files
        if file_checksums is not None:
            # already have the remote files listed, no need to do it again
            return
        print(f"Listing remote files for {dataset_name!r}...", flush=True)
        # use time.perf_counter() to measure the time taken to list remote files
        # perf_counter() is preferred for measuring elapsed time with high resolution
        list_start = time.perf_counter()
        file_checksums = list_remote_files(base_url, _context=source)
        print(f"Found {len(file_checksums)} files in "
              f"{time.perf_counter() - list_start:.1f}s")
        # sort the remote files by their relative paths for consistent ordering
        remote_files = sorted(file_checksums)

    # if the repo path already exists and has a config.yml, we will reuse it
    reused_from_existing_repo = False
    # records the interactive include_drives choice when fmts are auto-detected, so it
    # can be persisted to meta.yml and isn't lost once the build finishes
    detect_include_drives: bool | None = None

    if fmt_entries is None and existing_fmt_entries:
        print(f"Found existing config.yml with {len(existing_fmt_entries)} fmt(s):")
        for entry in existing_fmt_entries:
            print(f"  {entry['fmt']!r} -> {entry['db']}")
        # prompt the user to either use the existing fmt entries or update them
        keep_choice = prompt_choice("Use these fmts as-is, or update the list? "
        "[use/update]: ", {"use", "update"})
        # if user chooses to use existing fmts, set them as the current fmt_entries
        if keep_choice == "use":
            fmt_entries = existing_fmt_entries
            reused_from_existing_repo = True

    # if repo path exists and we are not reusing it, check if overwrite is allowed
    if repo_path.exists() and not reused_from_existing_repo and not overwrite:
        # raise an error to prevent accidental overwriting of an existing repo
        raise FileExistsError(
            f'Destination "{repo_path}" already exists. '
            "Use overwrite=True to replace it.")

    if fmt_entries is None:
        # prompt the user to choose how to supply fmt entries
        choice = prompt_choice(
            "Load fmts from an existing config file, detect them "
            "automatically, or input them yourself? "
            "[config/detect/input]: ", {"config", "detect", "input"})
        if choice == "config":
            # prompt the user for the path to an existing config.yml or repo directory
            config_path = Path(
                input(
                    "Path to the existing config.yml or repository "
                    "directory to load fmts from: ").strip()).expanduser()
            # if the provided path is a directory, assume it contains a config.yml
            if config_path.is_dir():
                config_path = config_path / "config.yml"
            # if the config file does not exist, raise an error
            if not config_path.is_file():
                raise FileNotFoundError(f"Config file does not exist: {config_path}")

            # load the config.yml file and extract the fmt entries
            loaded_config = load_yaml_file(config_path)
            fmt_entries = fmt_entries_from_config(loaded_config)
            if not fmt_entries:
                raise ValueError(
                    f"No fmt entries found in config file {config_path}.")
            # if remotes were not provided and the loaded config has a "remote" key
            if not remotes_provided and loaded_config.get("remote"):
                # normalize the remotes from the loaded config and use them
                remotes = normalize_remotes(loaded_config["remote"])

        elif choice == "detect":
            # ask the user if they want to include drive files during fmt detection
            include_drives = prompt_choice("Include drive/archive files during fmt " \
            "detection? [yes/no]: ", {"yes", "no"}, ) == "yes"
            # remember the choice so it can be recorded in meta.yml below
            detect_include_drives = include_drives

            # check if the remote files have already been listed; if not, list them now
            _ensure_remote_files_listed()
            detected_fmts = detect_fmt(remote_files, include_drives=include_drives)
            if not detected_fmts:
                raise ValueError(
                    "No fmts could be automatically detected from this dataset.")
            fmt_entries = []
            # create a fmt entry for each detected format
            if len(detected_fmts) == 1:
                # if there is only one detected format, assume it is "data.tsv"
                print(f"Detected fmt: {detected_fmts[0]!r}\n"
                      f"  Only one fmt detected; using db name 'data.tsv'.")
                fmt_entries.append({"fmt": detected_fmts[0], "db": "data.tsv"})
            else:
                for fmt in detected_fmts:
                    # prompt the user to enter a database name for each detected format
                    db_name = normalize_tsv_name(input(
                        f"Detected fmt: {fmt!r}\n"
                        f"  Enter a db name for this fmt (e.g. 'data.tsv'): "))
                    fmt_entries.append({"fmt": fmt, "db": db_name})
        elif choice == "input":
            fmt_entries = []
            print("Enter fmt entries one at a time. Leave the fmt blank to finish.")
            # allow the user to input multiple fmt entries manually
            while True:
                fmt = input("fmt (blank to finish): ").strip()
                # if the user inputs a blank fmt, exit the loop
                if not fmt:
                    break
                # prompt the user to enter a database name for the provided format
                db_name = normalize_tsv_name(input("  db name for this fmt: "))
                # append the user-provided fmt and db name to the fmt_entries list
                fmt_entries.append({"fmt": fmt, "db": db_name})

    # normalize the fmt entries to ensure they are valid and consistent
    fmt_entries = _normalize_fmt_entries(fmt_entries)

    _ensure_remote_files_listed()

    # if the repo path already exists and has a config.yml, we will reuse it
    if reused_from_existing_repo:
        repo = existing_repo
        if not remotes_provided:
            # normalize the remotes from the existing repo's config and use them
            remotes = normalize_remotes(repo.state.config.get("remote"))
    else:
        # if we are not reusing an existing repo, we need to create a new one
        if repo_path.exists():
            shutil.rmtree(repo_path)
        repo = Repo.init(repo_path)

    # add the data and remote entries to the repo's config and dump it to disk
    repo.state.config["data"] = list(fmt_entries)
    repo.state.config["remote"] = list(remotes)
    repo.dothm.dump(repo.state)
    repo.state = repo.dothm.load()
    # reread the fmts from the repo's config to ensure consistency
    fmt_entries = fmt_entries_from_config(repo.state.config)
    remotes = repo.state.config.get("remote") or []

    # if not provided, list the remote files and collect checksums for matching
    _ensure_remote_files_listed()
    # make rows for each fmt entry to be filled with parsed data from matching files
    rows_by_fmt: list[list[dict]] = [[] for _ in fmt_entries]

    # list of paths that could not be matched to any format, to be processed later
    unmatched_paths: list[str] = []
    print(f"Matching {len(remote_files)} files against {len(fmt_entries)} fmt(s)...")
    for file_index, rel_path in enumerate(remote_files):
        # print progress every 100 files matched, including the current file index
        if file_index % 100 == 0:
            print(f"Progress: {file_index}/{len(remote_files)} files matched so far",
              flush=True)
        # use time.perf_counter() to measure the time taken for matching each file
        start_time = time.perf_counter()
        # create a Path object for the relative path to extract its stem and last piece
        path = Path(rel_path)
        whole_stem = path.stem
        last_piece = whole_stem.split(".")[-1]

        if whole_stem.lower() in KNOWN_STATIC_FILE_STEMS:
            # if the whole stem matches, we can skip matching against formats
            i, result = None, None
        elif last_piece.lower() in KNOWN_STATIC_FILE_STEMS:
            # if the last piece of the stem matches, match using plain parse only
            i, result = _match_static_named_file_plain_only(rel_path, fmt_entries)
        else:
            # otherwise, match against all formats using the full matching logic
            i, result = _match_file_against_fmts(rel_path, fmt_entries)

        elapsed_time = time.perf_counter() - start_time
        # print a note if matching took longer than 1 second, including the file index
        if elapsed_time > 1.0:
            print(f"Note: matching \"{rel_path}\" took {elapsed_time:.1f}s "
                  f"(file {file_index + 1}/{len(remote_files)})")
        if result is not None:
            algorithm, checksum = file_checksums[rel_path]
            # create a row dictionary with the matched result and checksum info
            row = {"path": rel_path, **result}
            row["checksum_algorithm"] = algorithm
            row["checksum"] = checksum
            # append the row to the corresponding list for the matched format
            rows_by_fmt[i].append(row)
        else:
            # if no format matched, add to the unmatched_paths list for later processing
            unmatched_paths.append(rel_path)

    # create the manifest for each format and organize rows by database
    fmt_manifest: list[dict] = []
    # iterate over each format entry to build the manifest
    for fmt_entry in fmt_entries:
        db = fmt_entry["db"]
        entry = {"fmt": fmt_entry["fmt"], "db": db}
        # include the name in the manifest entry if it exists in the fmt_entry
        if "name" in fmt_entry:
            entry = {"name": fmt_entry["name"], **entry}
        fmt_manifest.append(entry)

    # process unmatched paths to identify static files and the meta file
    static_file_entries = []
    meta_file_entries: list[dict] = []

    # identify which unmatched paths need checksums computed (those without a checksum)
    paths_needing_checksums = [
        rel_path
        for rel_path in unmatched_paths
        if file_checksums.get(rel_path, (None, None))[1] is None]
    computed_checksums = {}
    if paths_needing_checksums:
        paths = paths_needing_checksums
        with source.executor(min(_STATIC_CHECKSUM_MAX_WORKERS, len(paths))) as executor:
            # The source owns all static checksum I/O, including SSH capability checks.
            results = executor.map(source.transport.checksum_small, paths)
            # zip the paths needing checksums with their computed results into a dict
            computed_checksums = dict(zip(paths_needing_checksums, results))

    for rel_path in unmatched_paths:
        path = Path(rel_path)
        stem_name = path.stem.split(".")[-1]

        # get the stem name of the file and check if it is a meta file
        is_meta_file = stem_name.lower() == "meta"
        # for each unmatched path, look up its checksum
        algo, checksum = file_checksums.get(rel_path, (None, None))
        # if not available, use the computed checksum from the parallel computation
        if checksum is None:
            algo, checksum = computed_checksums[rel_path]

        # create an entry for the static file with its relative path
        entry = {"file": rel_path}
        # if the file is not a meta file and its stem name a known static file
        if (not is_meta_file and stem_name.lower() in KNOWN_STATIC_FILE_STEMS):
            # add the stem name as the "name" in the entry for known static files
            entry = {"name": stem_name.lower(), **entry}

        # if a valid checksum algorithm and checksum are available, include them
        if algo is not None and checksum is not None:
            if algo == _UNKNOWN_CHECKSUM_ALGORITHM:
                entry["checksum"] = _UNKNOWN_CHECKSUM_ALGORITHM
            elif algo in SUPPORTED_CHECKSUM_ALGORITHMS:
                entry[algo] = checksum

        # if the file is a meta file, store it separately
        if is_meta_file:
            meta_file_entries.append(entry)
        # otherwise, add it to the list of static file entries for the config.yml
        else:
            static_file_entries.append(entry)

    # hallmark's bookkeeping: create and commit the meta.yml file
    meta_dict: dict = {"dataset": dataset_name}
    # record the include_drives choice so a detect-built repo's fmts stay consistent
    if detect_include_drives is not None:
        meta_dict["detect_include_drives"] = detect_include_drives
    repo.dothm.dump_yml(meta_dict, "meta")
    repo.dothm.index.add(["meta.yml"])
    repo.dothm.index.commit(f"Initialize dataset: {dataset_name}")

    # columns that will always be present in the TSV manifest files
    manifest_columns = ("path", "checksum_algorithm", "checksum")
    # initialize dictionaries to group rows and fields by their target database
    rows_by_target_db: dict[str, list[dict]] = {}
    fields_by_target_db: dict[str, list[str]] = {}
    for fmt_entry, rows in zip(fmt_entries, rows_by_fmt):
        db = fmt_entry["db"]
        # group the rows for each target database, extending existing rows if present
        rows_by_target_db.setdefault(db, []).extend(rows)
        # group the fields for each target database, ensuring no duplicates
        target_fields = fields_by_target_db.setdefault(db, [])
        # for each field extracted from the format string, add it to the target fields
        for field in fmt_fields(fmt_entry["fmt"]):
            # if the field not already in the manifest columns or target fields, add it
            if field not in manifest_columns and field not in target_fields:
                target_fields.append(field)

    # write the TSV files for each target database with the collected rows and fields
    for db, rows in rows_by_target_db.items():
        # columns are the manifest columns followed by fields specific to the target db
        columns = [*manifest_columns, *fields_by_target_db[db]]
        # create a DataFrame from the rows and columns for the target database
        df = pd.DataFrame(rows, columns=columns)
        # dump the DataFrame to a TSV file in the .dothm directory with "None" for NaN
        repo.dothm.dump_tsv(df, db, na_rep="None")
        repo.dothm.index.add([db])

    # config.yml: static files, then fmt entries, then remote/meta
    repo.state.config["data"] = [*static_file_entries, *fmt_manifest]
    if not remotes:
        # use a default remote named "origin" pointing to the base_url if none provided
        remotes = [{"name": "origin"}]
        if source.remote.auth is not None:
            remotes[0]["auth"] = source.remote.auth
    # create the final remotes list by adding the base_url to each remote entry
    final_remotes = [{"url": base_url, **remote} for remote in remotes]
    repo.state.config["remote"] = final_remotes

    # if a meta file entry exists, add it to the config
    if meta_file_entries:
        repo.state.config["meta"] = meta_file_entries

    # write the config.yml file with the static files, fmt entries, remotes, and meta
    config_path = repo.dothm.path / "config.yml"
    # use an atomic write context manager to ensure the config.yml is written safely
    with atomic_output_path(config_path) as temp_config_path:
        # write the config.yml file with the collected data
        with temp_config_path.open("w", encoding="utf-8") as f:
            f.write("data:")
            if static_file_entries or fmt_manifest:
                f.write("\n")
            # write static file entries first, then fmt entries, with newline in between
            if static_file_entries:
                dump_yaml(static_file_entries, f)
            if static_file_entries and fmt_manifest:
                f.write("\n")
            # write the fmt entries to the config.yml file
            if fmt_manifest:
                dump_yaml(fmt_manifest, f)
            # if there are no static file or fmt entries, write an empty list
            if not static_file_entries and not fmt_manifest:
                f.write(" []\n")

            # for each section (remote and meta)
            for section_name, entries in (
                ("remote", final_remotes),
                ("meta", meta_file_entries)):
                # only write the section if there are entries to include
                if entries:
                    f.write("\n")
                    # dump the section name and its entries to the config.yml file
                    dump_yaml({section_name: entries}, f)

    repo.dothm.index.add(["config.yml"])
    repo.dothm.index.commit(f"Add dataset manifest: {dataset_name}")

    # reload the repo state from the .dothm directory to reflect the latest changes
    repo.state = repo.dothm.load()
    return repo