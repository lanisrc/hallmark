"""
Utilities for managing Hallmark repository configuration.

This module provides helper functions for reading, validating, and
updating repository configuration values stored in ``config.yml``.
"""

from __future__ import annotations

import re
import string

import pandas as pd
import parse

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Dict, Optional

from ..fmt_detection import KNOWN_PROCESSING_STAGES, KNOWN_STATIC_FILE_STEMS
from ..utils import (
    CHECKSUM_ALGORITHMS, as_list_of_dicts, convert_format_value,
    require_nonempty_string, validate_path_name, validate_relative_path)

from ..transport.base import (
    RemoteSpec,
    validate_backend_name,
    validate_profile_name,
    reject_control_characters,
    reject_url_secrets,
    copy_backend_options,
)

# maximum length of a literal segment in a format string that can be cleared
_MAX_CLEARABLE_LITERAL_LENGTH = 2
# known field values for specific format fields, used for parsing and matching
KNOWN_FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "kind": KNOWN_PROCESSING_STAGES,
    "stage": KNOWN_PROCESSING_STAGES,
    "algorithm": CHECKSUM_ALGORITHMS}


def _update_remote_config(
    config: dict,
    remote_name: Optional[str],
    remote_url: Optional[str],
    remote_auth: Optional[str] = None,
    remote_backend: Optional[str] = None,
    remote_backend_options=None,
    ) -> None:
    """
    Used by set_config.
    Update the repository data remote and its local profile reference.

    Args:
        config (dict): The repository configuration dictionary.
        remote_name (str, optional): New remote repository name.
        remote_url (str, optional): New remote repository URL.
        remote_auth (str, optional): Local SSH profile name. An empty string
            removes the reference; None leaves it unchanged.
        remote_backend (str, optional): Registered backend name. An empty string
            restores automatic selection; None leaves it unchanged.
        remote_backend_options (mapping, optional): Replace backend options.
            An empty mapping clears options; None leaves them unchanged.

    Raises:
        ValueError: If the remote configuration is invalid or if the specified
                    remote name does not exist and no URL is provided.
    """
    # get the current remote configuration from the repository config
    configured = config.get("remote")
    # preserve_list is True if the configured remote is a list, False otherwise
    preserve_list = isinstance(configured, list)
    # if the remotes configuration is None, initialize it as an empty list
    if configured is None:
        remotes = []
    # if the remote configuration is a list, normalize it into a list of dictionaries
    elif isinstance(configured, (dict, list)):
        remotes = normalize_remotes(configured)
    # if the remote configuration is neither a list nor a dictionary, raise an error
    else:
        raise ValueError("Invalid remote configuration in config.yml")

    # if there are no remotes, create a new empty dictionary and append it to the list
    if not remotes:
        selected = {}
        remotes.append(selected)
    # if there is exactly one remote, select it for updating
    elif len(remotes) == 1:
        selected = remotes[0]
    # if there is more than one remote, select the one matching the provided name
    elif remote_name is not None:
        # find the remote with the specified name in the list of remotes
        selected = next(
            (remote for remote in remotes if remote.get("name") == remote_name), None)
        # if the no remote with the specified name is found
        if selected is None:
            # raise an error if no remote URL is provided
            if remote_url is None:
                raise ValueError(f"Remote {remote_name!r} is not configured")
            # if a remote URL is provided, create a new remote entry with the name
            selected = {"name": remote_name}
            remotes.append(selected)
    # if there are multiple remotes and no name is specified
    else:
        # see if there is a remote named "origin" in the list of remotes
        selected = next(
            (remote for remote in remotes if remote.get("name") == "origin"), None)
        # if no remote named "origin" is found, raise an error
        if selected is None:
            raise ValueError(
                "Multiple remotes are configured; specify --remote-name")

    if remote_name is not None:
        previous_name = selected.get("name") or "origin"
        selected["name"] = remote_name
        # Sources refer to their remote by name.
        for source in config.get("sources") or []:
            if isinstance(source, dict) and source.get("remote") == previous_name:
                source["remote"] = remote_name
    if remote_url is not None:
        selected["url"] = remote_url
    if remote_auth == "":
        selected.pop("auth", None)
    elif remote_auth is not None:
        selected["auth"] = validate_profile_name(remote_auth)
    if remote_backend == "":
        selected.pop("backend", None)
    elif remote_backend is not None:
        selected["backend"] = validate_backend_name(remote_backend)
    if remote_backend_options is not None:
        selected["backend_options"] = copy_backend_options(
            remote_backend_options)
    # normalize the remotes configuration to ensure it is a list of dictionaries
    normalized = normalize_remotes(remotes)
    # if preserve_list is True, store the normalized list;
    # otherwise, store only the first entry
    config["remote"] = (normalized if preserve_list else normalized[0])


def _single_data_config(config) -> Optional[dict]:
    """
    Used by single_data_format and require_data_config.
    Extract the single data specification from a repository configuration.

    Args:
        config (dict): The repository configuration dictionary.

    Returns:
        Optional[dict]: The single data specification if defined,
        or None if not defined or if the configuration is invalid.
    """
    # if the provided config is not a dictionary, return None
    if not isinstance(config, dict):
        return None
    # get the "data" section from the configuration
    data = config.get("data")
    # if the "data" section is not a list with exactly one entry that is a dictionary
    if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)):
        # indicate that the configuration does not define exactly one entry
        return None
    # return the first (and only) entry in the "data" list
    return data[0]


def remote_name_of(remote: dict) -> str:
    """
    Return a data remote's name.

    A catalogue with one remote may leave it unnamed; it is then ``origin``
    everywhere a remote is chosen or reported.
    """
    return remote.get("name") or "origin"


def normalize_remotes(remotes) -> list[dict]:
    """
    Normalize remote configuration into a list of dictionaries.

    Args:
        remotes: A string, dictionary, or sequence of remote configurations.

    Returns:
        A list of normalized remote dictionaries.

    Raises:
        ValueError: If remotes is not a string, dictionary, or sequence,
                    or if any remote configuration is invalid.
    """
    # if there are no remotes provided, return an empty list
    if remotes is None:
        return []
    # if remotes is a string or dictionary, wrap it in a list for uniform processing
    if isinstance(remotes, (str, dict)):
        remote_values = [remotes]
    else:
        # try to convert remotes to a list, raising an error if it is not iterable
        try:
            remote_values = list(remotes)
        except TypeError as exc:
            raise ValueError(
                "remotes must be a string, dictionary, or sequence") from exc

    normalized = []
    seen_names: dict[str, int] = {}
    # iterate over each remote configuration and validate/normalize it
    for index, remote in enumerate(remote_values):
        # if the remote is a string, treat it as a name and create a dictionary
        if isinstance(remote, str):
            entry = {
                "name": require_nonempty_string(remote, label=f"remote {index} name")}
        # if the remote is a dictionary, make a copy of it for normalization
        elif isinstance(remote, dict):
            entry = dict(remote)
        # otherwise, raise an error since the remote must be a string or dictionary
        else:
            raise ValueError(f"remote {index} must be a string or dictionary")

        if set(entry) - {"name", "url", "auth", "backend", "backend_options"}:
            raise ValueError(
                "Remote entries support only name, url, auth, backend and "
                "backend_options fields")
        if "auth" in entry:
            validate_profile_name(entry["auth"])
        if "backend" in entry:
            entry["backend"] = validate_backend_name(entry["backend"])
        if "backend_options" in entry:
            entry["backend_options"] = copy_backend_options(
                entry["backend_options"])
        if isinstance(entry.get("url"), str):
            reject_control_characters(entry["url"], "Remote URL")
            # allow names without URLs while configuring a data remote
            RemoteSpec.from_url(
                entry["url"], entry.get("auth"), backend=entry.get("backend"),
                backend_options=entry.get("backend_options"))
        # for each required key ("name" and "url"), validate that it exists
        for key in ("name", "url"):
            # if the key is not present in the entry, skip to the next key
            if key not in entry:
                continue
            # normalize the value of the key to ensure it is a non-empty string
            entry[key] = require_nonempty_string(
                entry[key],
                label=f"remote {index} {key}")

        # get the name of the remote from the entry to check for duplicates
        name = entry.get("name")
        # if the name is present, check if it has been seen before
        if name is not None:
            previous_index = seen_names.get(name)
            # if a previous remote with the same name exists, raise an error
            if previous_index is not None:
                raise ValueError(
                    f"remote {index} duplicates the name "
                    f"of remote {previous_index}: {name!r}")
            # record the name and its index to track duplicates
            seen_names[name] = index
        # add the normalized entry to the list of normalized remotes
        normalized.append(entry)

    # if there are multiple remotes, ensure that all of them define names
    if len(normalized) > 1:
        # get the indexes of any remotes that do not have a "name" key
        unnamed_indexes = [index for index, entry in enumerate(normalized)
                           if "name" not in entry]
        # if there are any unnamed remotes, raise a ValueError listing their indexes
        if unnamed_indexes:
            indexes = ", ".join(map(str, unnamed_indexes))
            raise ValueError(
                "multiple remotes must all define names; "
                f"unnamed remote index(es): {indexes}")

    return normalized


def fmt_entries_from_config(config: dict) -> list[dict]:
    """
    Extract and validate the list of format entries from the repository configuration.

    Args:
        config (dict): The repository configuration dictionary.

    Returns:
        list[dict]: A list of format entries containing the "fmt" key.

    Raises:
        ValueError: If the configuration is invalid or if any entry is not a mapping.
    """
    # if the provided config is not a dictionary, raise a ValueError
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    data = config.get("data")
    # if the "data" section is not defined, return an empty list
    if data is None:
        return []

    # coerce "data" into a list (a dict becomes a single-item list, as-is if a list)
    entries = as_list_of_dicts(data)
    if entries is None:
        raise ValueError('config "data" must be a mapping or list of mappings')

    # validate that each entry in the "data" list is a dictionary
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"config data entry {index} must be a mapping")
    # return a list of entries that contain the "fmt" key
    return [entry for entry in entries if "fmt" in entry]


def single_data_format(config: dict) -> Optional[str]:
    """
    Extract the format string from a repository configuration that defines
    exactly one entry under the "data" section.

    Args:
        config (dict): The repository configuration dictionary.

    Returns:
        Optional[str]: The format string if defined,
        or None if not defined or if the configuration is invalid.
    """
    # call the helper function to get the single data specification from the config
    spec = _single_data_config(config)
    # if there is no valid single data specification, return None
    if spec is None:
        return None
    fmt = spec.get("fmt")
    # if the "fmt" value is not a non-empty string, return None
    if not isinstance(fmt, str) or not fmt.strip():
        return None
    return fmt.strip()


def get_or_create_data_config(config: dict) -> dict:
    """
    Ensure the configuration contains a valid data specification.

    If the ``data`` entry is missing or malformed, it is initialized with
    a single empty dictionary.

    Args:
        config (dict): Repository configuration.

    Returns:
        dict: The branch data specification.
    """
    # get the "data" section from the configuration
    data = config.get("data")
    # if "data" already defines exactly one dictionary entry, use it as-is
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    # otherwise (missing, empty, or malformed), initialize it with a single empty dict
    config["data"] = [{}]
    return config["data"][0]


def require_data_config(repo) -> dict:
    """
    Return the branch data specification. Raises RuntimeError if
    the configuration does not define exactly one entry under ``data``.

    Args:
        repo: Repository object.

    Returns:
        dict: The branch data specification.
    """
    # call the helper function to get the single data specification from the config
    spec = _single_data_config(repo.state.config)
    # raise a RuntimeError if there is no valid single data specification
    if spec is None:
        raise RuntimeError(
            'branch config must define exactly one entry under "data" in config.yml')
    return spec


def branch_filename_format(repo) -> str:
    """
    Return the configured filename format. Raises RuntimeError if
    no valid format string is defined.

    Args:
        repo: Repository object.

    Returns:
        str: The format string stored in ``data[0].fmt``.
    """
    return require_nonempty_string(
        require_data_config(repo).get("fmt"),
        label="branch data[0].fmt",
        exception_type=RuntimeError)


def set_config(
    repo,
    *,
    fmt: Optional[str] = None,
    remote_name: Optional[str] = None,
    remote_url: Optional[str] = None,
    encoding_updates: Optional[Dict[str, str]] = None,
    remote_auth: Optional[str] = None,
    remote_backend: Optional[str] = None,
    remote_backend_options=None,
) -> dict:
    """
    Update the repository configuration.

    Existing configuration values are preserved unless explicitly
    replaced. Nothing is saved; ``Repo.set_config`` saves and stages.

    Args:
        repo: Repository object.
        fmt (str, optional): Filename format.
        remote_name (str, optional): Data remote name. With one remote it
            renames it, and sources that use the old name follow; with several
            it selects one, or adds it when ``remote_url`` is given.
        remote_url (str, optional): Data remote URL.
        remote_auth (str, optional): Local authentication profile name. An
            empty string removes the reference; None leaves it unchanged.
        remote_backend (str, optional): Registered backend name. An empty string
            restores automatic selection; None leaves it unchanged.
        remote_backend_options (mapping, optional): Replace backend options.
            An empty mapping clears options; None leaves them unchanged.
        encoding_updates (dict, optional): Encoding values to merge into
            the existing configuration.

    Returns:
        dict: The updated configuration.

    Raises:
        ValueError: If the new filename format is incompatible with existing
            data, uses a reserved column name, or is set on a remote catalogue
            with sources; or if a remote value is invalid.
            ``RemoteConfigurationError``, a ``ValueError``, reports a remote URL
            with credentials, query or fragment, and an invalid profile name or
            backend.
    """
    config = deepcopy(repo.state.config)
    # raise a ValueError if the provided config is not a dictionary
    if not isinstance(config, dict):
        raise ValueError("repository config must be a mapping")

    # migrate existing data if the filename format has changed
    migrated_data = None
    if fmt is not None:
        # ensure the new format string is a nonempty string
        fmt = require_nonempty_string(fmt, label="fmt")
        check_pattern_fields(fmt)
        if config.get("sources"):
            raise ValueError(
                "A remote catalog's formats sort the files of its sources; "
                "add one with hm add URL --fmt PATTERN")
        # retrieve the old filename format from the repository's configuration
        old_fmt = single_data_format(repo.state.config)

        # if the new format differs from the old one or the "path" column is missing
        if "sha1" in repo.state.data.columns and (
            fmt != old_fmt or "path" not in repo.state.data.columns):
            # column for each field in the new filename format, plus "sha1" and "path"
            columns = ["sha1", "path", *filename_fields(fmt)]
            rows = []
            incompatible = 0
            # if there is existing data but no old format, we cannot verify the catalog
            if not repo.state.data.empty and old_fmt is None:
                raise ValueError("Cannot verify catalog without its saved pattern")

            # iterate over each record in the existing data as a dictionary
            for record in repo.state.data.to_dict(orient="records"):
                # convert the record to a path using the old filename format
                old_path = row_to_path(record, old_fmt)
                # if the new format contains a directory component, use the full path;
                # otherwise, use just the filename
                target = old_path.as_posix() if "/" in fmt else old_path.name
                # match the target string against the new format
                named = literal_fields(fmt, target)

                # if it does not match the new format, it is considered incompatible
                if named is None:
                    incompatible += 1
                    # skip this record as it is incompatible with the new format
                    continue
                # construct a new row for the migrated data based on the named groups
                rows.append({
                    "sha1": record["sha1"],
                    **named,
                    "path": old_path.as_posix()})

            # if there are any incompatible records, raise an error
            if incompatible:
                raise ValueError(
                    f"{incompatible} files in the catalog don't fit {fmt}.\n"
                    "Remove them first, or use a new branch for the new pattern.")

            # create a new DataFrame with the migrated rows and specified columns
            migrated_data = pd.DataFrame(rows, columns=columns)

    # if encoding updates are provided
    if encoding_updates is not None:
        # raise an error if encoding_updates is not a dictionary
        if not isinstance(encoding_updates, dict):
            raise ValueError("encoding_updates must be a dictionary")

        normalized_encodings = {}
        # for each field and pattern in the encoding updates
        for field, pattern in encoding_updates.items():
            # validate that the field name is a non-empty string
            normalized_field = require_nonempty_string(
                field, label="encoding field names")
            # validate that the encoding pattern is a non-empty string
            normalized_pattern = require_nonempty_string(
                pattern, label=f"encoding for {field!r}")

            # store the normalized field name and pattern in the dictionary
            normalized_encodings[normalized_field] = normalized_pattern
        # set the encoding_updates to the normalized dictionary
        encoding_updates = normalized_encodings

    # if a new remote name is provided, validate that it is a non-empty string
    if remote_name is not None:
        remote_name = require_nonempty_string(remote_name, label="remote_name")
    # if a new remote URL is provided, validate that it is a non-empty string
    if remote_url is not None:
        if isinstance(remote_url, str):
            reject_control_characters(remote_url, "Remote URL")
        remote_url = require_nonempty_string(remote_url, label="remote_url")
        reject_url_secrets(remote_url)
    if remote_auth not in (None, ""):
        validate_profile_name(remote_auth)

    # if a new format string or encoding updates are provided
    if fmt is not None or encoding_updates is not None:
        # ensure that the "data" section has exactly one entry and retrieve it
        spec = get_or_create_data_config(config)
        updated_spec = {}
        # if a new format string is provided, update the "fmt" key in the spec
        if fmt is not None:
            updated_spec["fmt"] = fmt
        # if the existing spec has a "fmt" key, preserve it in the updated spec
        elif "fmt" in spec:
            updated_spec["fmt"] = spec["fmt"]

        # get the existing encoding value from the spec, if any
        encoding_value = spec.get("encoding")
        # if the existing spec has an "encoding" key, preserve it in the updated spec
        if encoding_updates:
            # if its not a dictionary, initialize it as an empty dictionary
            if not isinstance(encoding_value, dict):
                encoding_value = {}
            # merge the existing encoding value with the provided updates
            encoding_value = {**encoding_value, **encoding_updates}
        # if a new encoding value is provided or the existing spec has an "encoding" key
        if "encoding" in spec or encoding_updates is not None:
            # set the "encoding" key in the updated spec to the merged encoding value
            updated_spec["encoding"] = encoding_value

        # copy over any other keys from the existing spec to the updated spec
        for key, value in spec.items():
            # exclude "fmt" and "encoding" keys since they are already handled
            if key not in {"fmt", "encoding"}:
                updated_spec[key] = value
        # update the first entry in the "data" list of the configuration
        config["data"][0] = updated_spec

    # update the remote when its name, URL, or profile reference changes
    if any(value is not None for value in (
            remote_name, remote_url, remote_auth,
            remote_backend, remote_backend_options)):
        _update_remote_config(config, remote_name, remote_url, remote_auth,
                              remote_backend, remote_backend_options)

    repo.state.config = config
    # if there is migrated data, replace the current state with it
    if migrated_data is not None:
        repo.state.replace(migrated_data)
    return config


def branch_encodings(repo) -> list[dict]:
    """
    Return the configured filename encodings.

    Args:
        repo: Repository object.

    Returns:
        list[dict]: A list containing the encoding specification, or an
        empty list if no encodings are defined.
    """
    spec = require_data_config(repo)
    return [spec] if isinstance(spec.get("encoding"), dict) else []


def filename_fields(fmt: str) -> list[str]:
    """
    Extract field names from a format string.

    Args:
        fmt (str): Format string containing replacement fields.

    Returns:
        list[str]: Unique field names in the order they appear.
    """
    # initialize an empty list to store field names and a set to track seen names
    fields: list[str] = []
    seen: set[str] = set()
    # iterate through the parsed format string to extract field names
    for _, field_name, _, _ in Formatter().parse(fmt):
        # if the field name is not None and has not been seen before, add it to the list
        if field_name and field_name not in seen:
            seen.add(field_name)
            fields.append(field_name)
    return fields


# Catalogue columns that downloads read for themselves. A local filename
# field with one of these names would be taken for a size or checksum.
LOCAL_RESERVED_FIELDS = frozenset({
    "path", "sha1", "md5", "sha256", "sha512", "checksum", "checksum_algorithm",
    "checksum_source", "size_bytes", "mtime"})


def check_pattern_fields(fmt: str, reserved=LOCAL_RESERVED_FIELDS) -> None:
    """
    Refuse a filename pattern whose fields reuse catalogue column names.

    Args:
        fmt (str): Filename pattern.
        reserved (Collection[str]): Names the catalogue uses itself.

    Raises:
        ValueError: If a field name is reserved.
    """
    clashes = sorted(set(filename_fields(fmt)) & set(reserved))
    if clashes:
        raise ValueError(
            "Rename the pattern field(s) " + ", ".join(clashes)
            + ": the catalogue uses these names for its own columns")


def literal_format(fmt: str) -> str:
    """
    Return a filename format without its format specs.

    Field values are stored as the text the filename holds, so ``005`` in
    ``x_{doy:03d}.h5`` stays ``005``, as ``hm add PATTERN`` records it.
    Parse with the full format to validate a name and with this one to read
    its values.

    Args:
        fmt (str): Format string, possibly with specs such as ``{doy:03d}``.

    Returns:
        str: The same format with every ``{name:spec}`` reduced to ``{name}``.
    """
    parts = []
    for literal, field_name, _, _ in Formatter().parse(fmt):
        parts.append(literal.replace("{", "{{").replace("}", "}}"))
        if field_name is not None:
            parts.append("{" + field_name + "}")
    return "".join(parts)


def literal_fields(fmt: str, target: str):
    """
    Return the field text of a name that matches ``fmt``, or None.

    Args:
        fmt (str): Format string with optional specs, used to validate.
        target (str): Name or relative path to parse.

    Returns:
        dict[str, str] | None: Each field's text as it appears in ``target``,
        unconverted; None if ``target`` does not match. Matching is
        case-sensitive.
    """
    if parse.parse(fmt, target, case_sensitive=True) is None:
        return None
    match = parse.parse(literal_format(fmt), target, case_sensitive=True)
    return None if match is None else match.named


def validate_tsv_filename(value) -> str:
    """
    Validate a TSV database name and add the .tsv suffix when necessary.

    Args:
        value: The TSV database name to validate.

    Returns:
        str: The normalized TSV database name.

    Raises:
        ValueError: If the TSV database name is invalid.
    """
    # validate that the input value is a string or Path object
    if not isinstance(value, (str, Path)):
        raise ValueError("TSV database name must be a string")

    raw_name = str(value).strip()
    # check if the raw name is empty or just a dot or double dot, its invalid
    if not raw_name or raw_name in {".", ".."}:
        raise ValueError("TSV database name cannot be empty")
    # ensure the name ends with ".tsv" (case-insensitive), adding it if necessary
    if not raw_name.lower().endswith(".tsv"):
        raw_name += ".tsv"

    # validate the normalized name to ensure it is a safe single path component
    name = validate_path_name(raw_name, label="TSV database name")
    # if the name is ".tsv" (case-insensitive), raise an error since it cannot be empty
    if name.lower() == ".tsv":
        raise ValueError("TSV database name cannot be empty")
    # if all checks pass, return the validated name
    return name


def row_to_path(row, fmt: str) -> Path:
    """
    Construct a file path from a table row.

    Args:
        row: Table row containing field values.
        fmt (str): Format string used to build the path.

    Returns:
        Path: Path generated from the row values.
    """
    stored_path = row.get("path")
    # if the row already contains a valid "path" value, use it directly
    if isinstance(stored_path, str) and stored_path:
        return validate_relative_path(stored_path, label="formatted data path")

    values = {}
    for _, field_name, format_spec, _ in Formatter().parse(fmt):
        if field_name:
            values[field_name] = convert_format_value(str(row[field_name]), format_spec)
    # render the path using the format string and coerced values
    rendered_path = fmt.format(**values)
    # validate the rendered path to ensure it is a safe relative path
    return validate_relative_path(rendered_path, label="formatted data path")


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
    parser = parse.compile(fmt, case_sensitive=True)
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
    Used by match_file.
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
    Used by match_file.
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


def match_file(rel_path: str, fmt_entries: list[dict]
               ) -> tuple[int, dict] | tuple[None, None]:
    """
    Find the fmt entry that one file belongs to. Files whose whole stem is a
    known static name never match, files whose last dotted stem piece is a
    static name only match with a plain parse, and every other file goes
    through the full matcher.

    Args:
        rel_path: The file path to match.
        fmt_entries: The {"fmt", ...} entry dicts.

    Returns:
        (entry_index, named fields), or (None, None) if nothing matches.
    """
    # create a Path object for the relative path to extract its stem and last piece
    path = Path(rel_path)
    whole_stem = path.stem
    last_piece = whole_stem.split(".")[-1]

    if whole_stem.lower() in KNOWN_STATIC_FILE_STEMS:
        # if the whole stem matches, we can skip matching against formats
        return None, None
    if last_piece.lower() in KNOWN_STATIC_FILE_STEMS:
        # if the last piece of the stem matches, match using plain parse only
        return _match_static_named_file_plain_only(rel_path, fmt_entries)
    # otherwise, match against all formats using the full matching logic
    return _match_file_against_fmts(rel_path, fmt_entries)
