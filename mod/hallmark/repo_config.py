"""
Utilities for managing Hallmark repository configuration.

This module provides helper functions for reading, validating, and
updating repository configuration values stored in ``config.yml``.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from string import Formatter
from typing import Dict, Optional

from .helper_functions import (
    as_list_of_dicts, coerce_fmt_value, normalize_nonempty_string,
    validate_path_component, validate_relative_path)

from .transport.base import RemoteSpec, profile_name, reject_controls


def _update_remote_config(
    config: dict,
    remote_name: Optional[str],
    remote_url: Optional[str],
    remote_auth: Optional[str] = None,
    ) -> None:
    """
    Used by set_config.
    Update the repository configuration with a new remote name and/or URL.

    Args:
        config (dict): The repository configuration dictionary.
        remote_name (str, optional): New remote repository name.
        remote_url (str, optional): New remote repository URL.

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
        selected["name"] = remote_name
    if remote_url is not None:
        selected["url"] = remote_url
    if remote_auth == "":
        selected.pop("auth", None)
    elif remote_auth is not None:
        selected["auth"] = profile_name(remote_auth)
    # normalize the remotes configuration to ensure it is a list of dictionaries
    normalized = normalize_remotes(remotes)
    # if preserve_list is True, store the normalized list;
    # otherwise, store only the first entry
    config["remote"] = (normalized if preserve_list else normalized[0])


def _data_spec_or_none(config) -> Optional[dict]:
    """
    Used by single_data_fmt and require_branch_data_spec.
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
                "name": normalize_nonempty_string(remote, label=f"remote {index} name")}
        # if the remote is a dictionary, make a copy of it for normalization
        elif isinstance(remote, dict):
            entry = dict(remote)
        # otherwise, raise an error since the remote must be a string or dictionary
        else:
            raise ValueError(f"remote {index} must be a string or dictionary")

        if set(entry) - {"name", "url", "auth"}:
            raise ValueError("Remote entries support only name, url and auth fields")
        if "auth" in entry:
            profile_name(entry["auth"])
        if isinstance(entry.get("url"), str):
            reject_controls(entry["url"], "Remote URL")
            # Names without URLs are allowed while configuring/building a catalog.
            RemoteSpec.parse(entry["url"], entry.get("auth"))
        # for each required key ("name" and "url"), validate that it exists
        for key in ("name", "url"):
            # if the key is not present in the entry, skip to the next key
            if key not in entry:
                continue
            # normalize the value of the key to ensure it is a non-empty string
            entry[key] = normalize_nonempty_string(
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


def single_data_fmt(config: dict) -> Optional[str]:
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
    spec = _data_spec_or_none(config)
    # if there is no valid single data specification, return None
    if spec is None:
        return None
    fmt = spec.get("fmt")
    # if the "fmt" value is not a non-empty string, return None
    if not isinstance(fmt, str) or not fmt.strip():
        return None
    return fmt.strip()


def get_or_create_branch_data_spec(config: dict) -> dict:
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


def require_branch_data_spec(repo) -> dict:
    """
    Return the branch data specification. Raises RuntimeError if
    the configuration does not define exactly one entry under ``data``.

    Args:
        repo: Repository object.

    Returns:
        dict: The branch data specification.
    """
    # call the helper function to get the single data specification from the config
    spec = _data_spec_or_none(repo.state.config)
    # raise a RuntimeError if there is no valid single data specification
    if spec is None:
        raise RuntimeError(
            'branch config must define exactly one entry under "data" in config.yml')
    return spec


def branch_fmt(repo) -> str:
    """
    Return the configured filename format. Raises RuntimeError if
    no valid format string is defined.

    Args:
        repo: Repository object.

    Returns:
        str: The format string stored in ``data[0].fmt``.
    """
    return normalize_nonempty_string(
        require_branch_data_spec(repo).get("fmt"),
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
) -> dict:
    """
    Update the repository configuration.

    Existing configuration values are preserved unless explicitly
    replaced.

    Args:
        repo: Repository object.
        fmt (str, optional): Filename format.
        remote_name (str, optional): Remote repository name.
        remote_url (str, optional): Remote repository URL.
        remote_auth (str, optional): Local SSH profile; empty string removes it.
        encoding_updates (dict, optional): Encoding values to merge into
            the existing configuration.

    Returns:
        dict: The updated configuration.
    """
    config = deepcopy(repo.state.config)
    # raise a ValueError if the provided config is not a dictionary
    if not isinstance(config, dict):
        raise ValueError("repository config must be a mapping")

    # if a new format string is provided, validate that it is a non-empty string
    if fmt is not None:
        fmt = normalize_nonempty_string(fmt, label="fmt")

    # if encoding updates are provided
    if encoding_updates is not None:
        # raise an error if encoding_updates is not a dictionary
        if not isinstance(encoding_updates, dict):
            raise ValueError("encoding_updates must be a dictionary")

        normalized_encodings = {}
        # for each field and pattern in the encoding updates
        for field, pattern in encoding_updates.items():
            # validate that the field name is a non-empty string
            normalized_field = normalize_nonempty_string(
                field, label="encoding field names")
            # validate that the encoding pattern is a non-empty string
            normalized_pattern = normalize_nonempty_string(
                pattern, label=f"encoding for {field!r}")

            # store the normalized field name and pattern in the dictionary
            normalized_encodings[normalized_field] = normalized_pattern
        # set the encoding_updates to the normalized dictionary
        encoding_updates = normalized_encodings

    # if a new remote name is provided, validate that it is a non-empty string
    if remote_name is not None:
        remote_name = normalize_nonempty_string(remote_name, label="remote_name")
    # if a new remote URL is provided, validate that it is a non-empty string
    if remote_url is not None:
        if isinstance(remote_url, str):
            reject_controls(remote_url, "Remote URL")
        remote_url = normalize_nonempty_string(remote_url, label="remote_url")
    if remote_auth not in (None, ""):
        profile_name(remote_auth)

    # if a new format string or encoding updates are provided
    if fmt is not None or encoding_updates is not None:
        # ensure that the "data" section has exactly one entry and retrieve it
        spec = get_or_create_branch_data_spec(config)
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

    # if a new remote name or URL is provided, update the remote configuration
    if remote_name is not None or remote_url is not None or remote_auth is not None:
        _update_remote_config(config, remote_name, remote_url, remote_auth)

    repo.state.config = config
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
    spec = require_branch_data_spec(repo)
    return [spec] if isinstance(spec.get("encoding"), dict) else []


def fmt_fields(fmt: str) -> list[str]:
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


def normalize_tsv_name(value) -> str:
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
    name = validate_path_component(raw_name, label="TSV database name")
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
    values = {}
    for _, field_name, format_spec, _ in Formatter().parse(fmt):
        if field_name:
            values[field_name] = coerce_fmt_value(str(row[field_name]), format_spec)
    # render the path using the format string and coerced values
    rendered_path = fmt.format(**values)
    # validate the rendered path to ensure it is a safe relative path
    return validate_relative_path(rendered_path, label="formatted data path")