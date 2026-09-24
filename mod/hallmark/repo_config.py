"""
Utilities for managing Hallmark repository configuration.

This module provides helper functions for reading, validating, and
updating repository configuration values stored in ``config.yml``.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from typing import Dict, Iterable, Optional

from .helper_functions import (
    as_list_of_dicts, coerce_fmt_value, normalize_nonempty_string,
    validate_path_component, validate_relative_path)
from .state import DEFAULT_DB

from .transport.base import (
    RemoteSpec, backend_name, reject_controls, thaw_backend_options)


def _update_remote_config(
    config: dict,
    remote_name: Optional[str],
    remote_url: Optional[str],
    remote_backend: Optional[str] = None,
    remote_backend_options=None,
    ) -> None:
    """
    Used by set_config.
    Update the repository data remote and its transport configuration.

    Args:
        config (dict): The repository configuration dictionary.
        remote_name (str, optional): New remote repository name.
        remote_url (str, optional): New remote repository URL.
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
        selected["name"] = remote_name
    if remote_url is not None:
        selected["url"] = remote_url
    if remote_backend == "":
        selected.pop("backend", None)
    elif remote_backend is not None:
        selected["backend"] = backend_name(remote_backend)
    if remote_backend_options is not None:
        selected["backend_options"] = thaw_backend_options(
            remote_backend_options)
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

        if "auth" in entry:
            raise ValueError(
                "Hallmark auth profiles have been removed. Configure the host in "
                "~/.ssh/config, use its alias in the remote URL, and remove the "
                "obsolete auth field from config.yml.")
        if set(entry) - {"name", "url", "backend", "backend_options"}:
            raise ValueError(
                "Remote entries support only name, url, backend and "
                "backend_options fields")
        if "backend" in entry:
            entry["backend"] = backend_name(entry["backend"])
        if "backend_options" in entry:
            entry["backend_options"] = thaw_backend_options(
                entry["backend_options"])
        if isinstance(entry.get("url"), str):
            reject_controls(entry["url"], "Remote URL")
            # allow names without URLs while configuring a data remote
            RemoteSpec.parse(
                entry["url"], backend=entry.get("backend"),
                backend_options=entry.get("backend_options"))
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


@dataclass(frozen=True)
class CatalogEntry:
    """
    A data entry that owns a catalog table.

    Attributes:
        index (int): Position of the entry in the config ``data`` list.
        fmt (str, optional): Filename template relative to the worktree, or to
            ``url`` for a remote entry. Builder path catalogs may omit it.
        db (str): TSV name of the entry's catalog table.
        url (str, optional): Remote directory the template is relative to.
            Entries without a URL describe files in the worktree.
        backend (str, optional): Registered transport backend for ``url``.
        backend_options (dict, optional): Backend-specific configuration.
    """

    index: int
    fmt: Optional[str]
    db: str
    url: Optional[str] = None
    backend: Optional[str] = None
    backend_options: Optional[dict] = field(default=None, hash=False, compare=False)

    @property
    def is_remote(self) -> bool:
        """True when the entry catalogs files at a remote URL."""
        return bool(self.url)


def _data_entry_list(config) -> list:
    """Return the config ``data`` section as a list, or an empty list."""
    if not isinstance(config, dict):
        return []
    return as_list_of_dicts(config.get("data")) or []


def is_placeholder(entry) -> bool:
    """
    Return True for a data entry that does not catalog files yet, such as the
    ``encoding`` placeholder written by ``hallmark init``.
    """
    return isinstance(entry, dict) and not {"fmt", "url", "file", "db"} & set(entry)


def _entry_fmt(entry: dict) -> Optional[str]:
    """Return an entry's stripped template, or None if it has none."""
    fmt = entry.get("fmt")
    return fmt.strip() if isinstance(fmt, str) and fmt.strip() else None


def catalog_entries(config) -> list[CatalogEntry]:
    """
    Return the data entries that own catalog tables, in config order.

    Placeholders and static ``file`` entries are skipped. An entry without an
    explicit ``db`` uses ``data.tsv``.

    Args:
        config (dict): The repository configuration dictionary.

    Returns:
        list[CatalogEntry]: The catalog entries.

    Raises:
        ValueError: If the data section is malformed or an entry names an
            invalid TSV.
    """
    data = config.get("data") if isinstance(config, dict) else None
    if data is not None and as_list_of_dicts(data) is None:
        raise ValueError('config "data" must be a mapping or list of mappings')
    entries = []
    for index, entry in enumerate(_data_entry_list(config)):
        if not isinstance(entry, dict):
            raise ValueError(f"config data entry {index} must be a mapping")
        fmt = _entry_fmt(entry)
        if fmt is None and not entry.get("db"):
            continue
        url = entry.get("url")
        entries.append(CatalogEntry(
            index=index,
            fmt=fmt,
            db=normalize_tsv_name(entry["db"]) if entry.get("db") else DEFAULT_DB,
            url=url if isinstance(url, str) and url else None,
            backend=entry.get("backend"),
            backend_options=entry.get("backend_options")))
    return entries


def catalog_table_names(config) -> list[str]:
    """
    Return the TSV names referenced by catalog entries, skipping invalid names.

    Used when loading a repository, before any operation validates the
    configuration.

    Args:
        config (dict): The repository configuration dictionary.

    Returns:
        list[str]: Unique TSV names in config order.
    """
    names = []
    for entry in _data_entry_list(config):
        if not isinstance(entry, dict):
            continue
        if entry.get("db"):
            try:
                name = normalize_tsv_name(entry["db"])
            except ValueError:
                continue
        elif _entry_fmt(entry) is not None:
            name = DEFAULT_DB
        else:
            continue
        if name not in names:
            names.append(name)
    return names


def find_entry(config, fmt: str) -> Optional[CatalogEntry]:
    """
    Return the catalog entry whose template is ``fmt``.

    Args:
        config (dict): The repository configuration dictionary.
        fmt (str): Filename template.

    Returns:
        CatalogEntry | None: The matching entry, or None.
    """
    return next((entry for entry in catalog_entries(config) if entry.fmt == fmt),
                None)


def next_db_name(config, used: Iterable[str] = ()) -> str:
    """
    Choose the TSV name for a new data entry.

    The first entry uses ``data.tsv``. Later entries use ``data-N.tsv`` with a
    number larger than any in the config or in ``used``, so a removed table's
    history is not reused by a different template.

    Args:
        config (dict): The repository configuration dictionary.
        used (Iterable[str]): TSV names used previously, such as in history.

    Returns:
        str: The TSV name for the new entry.
    """
    entries = catalog_entries(config)
    if all(entry.db != DEFAULT_DB for entry in entries):
        return DEFAULT_DB
    numbers = [1]
    for name in [*(entry.db for entry in entries), *used]:
        match = re.fullmatch(r"data-(\d+)\.tsv", str(name), flags=re.IGNORECASE)
        if match:
            numbers.append(int(match.group(1)))
    return f"data-{max(numbers) + 1}.tsv"


def add_entry(config: dict, entry: dict) -> int:
    """
    Add a data entry to the configuration.

    The first entry fills the placeholder written by ``hallmark init`` so that
    single-template repositories keep their existing layout. Later entries are
    appended.

    Args:
        config (dict): The repository configuration dictionary, updated in place.
        entry (dict): The new data entry.

    Returns:
        int: The entry's position in the config ``data`` list.

    Raises:
        ValueError: If the config ``data`` section is malformed.
    """
    data = config.get("data")
    entries = [] if data is None else as_list_of_dicts(data)
    if entries is None:
        raise ValueError('config "data" must be a mapping or list of mappings')
    entries = list(entries)
    config["data"] = entries
    if not catalog_entries(config):
        for index, existing in enumerate(entries):
            if is_placeholder(existing):
                merged = dict(entry)
                for key, value in existing.items():
                    # a remote entry has no use for an empty encoding placeholder
                    if key in merged or (entry.get("url") and value is None):
                        continue
                    merged[key] = value
                entries[index] = merged
                return index
    entries.append(entry)
    return len(entries) - 1


def remove_entry(config: dict, index: int) -> dict:
    """
    Remove a data entry from the configuration.

    Args:
        config (dict): The repository configuration dictionary, updated in place.
        index (int): The entry's position in the config ``data`` list.

    Returns:
        dict: The removed entry.
    """
    entries = list(_data_entry_list(config))
    removed = entries.pop(index)
    config["data"] = entries
    return removed


def sole_data_spec(config: dict, *, create: bool = False) -> Optional[dict]:
    """
    Return the data entry that single-template settings apply to.

    This is the only catalog entry, or the placeholder when there is none.

    Args:
        config (dict): The repository configuration dictionary.
        create (bool): Add an empty entry when none exists. Defaults to False.

    Returns:
        dict | None: The data entry, or None when none exists and ``create``
        is False.

    Raises:
        RuntimeError: If the branch has more than one catalog entry.
    """
    entries = catalog_entries(config)
    if len(entries) > 1:
        raise RuntimeError(
            "this branch tracks several templates; use hallmark add TEMPLATE or "
            "hallmark rm --cached TEMPLATE instead of changing config.yml")
    data = _data_entry_list(config)
    if entries:
        return data[entries[0].index]
    placeholder = next((entry for entry in data if is_placeholder(entry)), None)
    if placeholder is not None or not create:
        return placeholder
    config["data"] = [*data, {}]
    return config["data"][-1]


def get_or_create_branch_data_spec(config: dict) -> dict:
    """
    Ensure the configuration contains a data specification for single-template
    settings and return it.

    Args:
        config (dict): Repository configuration.

    Returns:
        dict: The branch data specification.
    """
    return sole_data_spec(config, create=True)


def require_branch_data_spec(repo) -> dict:
    """
    Return the branch data specification. Raises RuntimeError if the
    configuration defines no data entry or several catalog entries.

    Args:
        repo: Repository object.

    Returns:
        dict: The branch data specification.
    """
    spec = sole_data_spec(repo.state.config)
    if spec is None:
        raise RuntimeError(
            'branch config must define an entry under "data" in config.yml')
    return spec


def branch_fmt(repo) -> str:
    """
    Return the template of a single-template branch. Raises RuntimeError if
    no valid template is defined.

    Args:
        repo: Repository object.

    Returns:
        str: The format string of the branch's data entry.
    """
    return normalize_nonempty_string(
        require_branch_data_spec(repo).get("fmt"),
        label="branch data fmt",
        exception_type=RuntimeError)


def branch_encodings(repo) -> list[dict]:
    """
    Return the filename encodings of a single-template branch.

    Args:
        repo: Repository object.

    Returns:
        list[dict]: A list containing the encoding specification, or an
        empty list if no encodings are defined.
    """
    spec = require_branch_data_spec(repo)
    return [spec] if isinstance(spec.get("encoding"), dict) else []


def set_config(
    repo,
    *,
    fmt: Optional[str] = None,
    remote_name: Optional[str] = None,
    remote_url: Optional[str] = None,
    encoding_updates: Optional[Dict[str, str]] = None,
    remote_backend: Optional[str] = None,
    remote_backend_options=None,
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
        remote_backend (str, optional): Registered backend name. An empty string
            restores automatic selection; None leaves it unchanged.
        remote_backend_options (mapping, optional): Replace backend options.
            An empty mapping clears options; None leaves them unchanged.
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

    # if a new format string or encoding updates are provided
    if fmt is not None or encoding_updates is not None:
        # retrieve the only data entry; several templates cannot share settings
        spec = sole_data_spec(config, create=True)
        if fmt is not None and spec.get("url"):
            raise ValueError(
                "cannot change the template of a remote entry; run "
                "hallmark add URL/TEMPLATE instead")
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
        # replace the entry at its position in the "data" list
        index = next(position for position, entry in enumerate(config["data"])
                     if entry is spec)
        config["data"][index] = updated_spec

    # update the remote when its name, URL, or transport configuration changes
    if any(value is not None for value in (
            remote_name, remote_url,
            remote_backend, remote_backend_options)):
        _update_remote_config(config, remote_name, remote_url,
                              remote_backend, remote_backend_options)

    repo.state.config = config
    return config


def entry_encodings(spec: Optional[dict], fmt: str) -> list[dict]:
    """
    Return the filename encodings that apply to a template.

    Args:
        spec (dict, optional): The data entry or placeholder holding the
            encoding rules.
        fmt (str): The template being added.

    Returns:
        list[dict]: A list containing the encoding specification for ``fmt``,
        or an empty list if no encodings are defined.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("encoding"), dict):
        return []
    return [{**spec, "fmt": fmt}]


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
