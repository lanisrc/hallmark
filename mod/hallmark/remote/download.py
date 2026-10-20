"""Remote data file downloader for hallmark repositories."""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Mapping, Optional, Sequence, Union
from tqdm import tqdm

import pandas as pd

from ..transport import OperationContext, RemoteSpec
from ..transport.base import DownloadError, validate_remote_path
from .plan import DownloadItem, DownloadPlan
from .sources import effective_sources, materialize_sources
from ..repo.state import is_remote_catalog
from ..utils import (
    CHECKSUM_ALGORITHMS_BY_STRENGTH,
    SUPPORTED_CHECKSUM_ALGORITHMS,
    FILE_IO_CHUNK_SIZE,
    as_list_of_dicts,
    calculate_file_checksums,
    infer_checksum_algorithm,
    require_nonempty_string,
    resolve_path_in_root,
    is_valid_checksum)
from ..repo.config import (
    normalize_remotes,
    remote_name_of,
    validate_tsv_filename,
    row_to_path)

# Maximum number of rows to read from a file at once when computing checksums.
TSV_READ_CHUNK_SIZE = 10_000
# union type for checksum specifications, string or a tuple of (algorithm, checksum).
ChecksumSpec = Union[str, tuple[str, str]]
# The size of chunks to read from a file when downloading or computing checksums.
DOWNLOAD_CHUNK_SIZE = 8192

def _repository_config(repo) -> dict:
    """
    Used by _select_remote_config and _select_download_items.
    Return repository configuration as a dictionary.

    Args:
        repo: The hallmark repository object.

    Returns:
        dict: The repository configuration as a dictionary.

    Raises:
        DownloadError: If the configuration is not a dictionary.
    """
    config = repo.state.config
    # if there is no configuration, return an empty dictionary
    if config is None:
        return {}
    # raise a DownloadError if the configuration is not a dictionary
    if not isinstance(config, dict):
        raise DownloadError("Invalid config.yml; expected a mapping")

    return config


def _config_section_entries(config: dict, section_name: str) -> list[dict]:
    """
    Used by _select_download_items.
    Return the dict entries of a config section (e.g. "data" or "meta"), silently
    dropping any entries that aren't mappings.

    Args:
        config: The repository configuration dictionary.
        section_name: The top-level config key to read.

    Returns:
        A list of dict entries for the section, or [] if the section is missing or
        not a mapping/list.
    """
    entries = as_list_of_dicts(config.get(section_name))
    if entries is None:
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _require_positive_integer(value, *, label: str) -> int:
    """
    Used by execute_download_plan.
    Validate that a value is a positive, non-boolean integer.

    Args:
        value: The value to validate.
        label (str): The label to use in the error message if validation fails.

    Returns:
        int: The validated positive integer.

    Raises:
        DownloadError: If the value is not a positive integer.
    """
    # if the value is not an integer, is a boolean, or less than or equal to zero
    if (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        # raise a DownloadError with a message indicating positive integer requirement
        raise DownloadError(f"{label} must be a positive integer")
    # otherwise, return the value as a valid positive integer
    return value


def _clean_checksum(value) -> Optional[str]:
    """
    Used by _parse_checksum and _checksum_from_config.
    Clean and validate a checksum value.

    Args:
        value: The checksum value to clean.

    Returns:
        Optional[str]: The cleaned checksum string, or None if not valid.
    """
    # if the value is None, return None to indicate no valid checksum is available
    if value is None:
        return None
    # Convert the value to a string and strip whitespace
    text = str(value).strip()
    # if the text is empty or matches any of the invalid values, return None
    if (not text or text.lower() in {"none", "nan", "unknown", "<na>"}):
        return None
    # otherwise the text is a valid checksum string, return it
    return text


def _parse_checksum(value, algorithm=None) -> Optional[ChecksumSpec]:
    """
    Used by _checksum_from_row and _checksum_from_config.
    Normalize a checksum and its optional algorithm.

    Args:
        value: The checksum value to normalize.
        algorithm: The optional algorithm associated with the checksum.

    Returns:
        Optional[ChecksumSpec]: The normalized checksum specification,
        or None if not available.
    """
    # Clean the checksum value to ensure it is usable
    checksum = _clean_checksum(value)
    # If the checksum is None, return None to indicate no valid checksum is available
    if checksum is None:
        return None
    # If no algorithm is provided, return just the checksum string
    if algorithm is None:
        return checksum

    # Clean the algorithm value to ensure it is usable
    algorithm_text = _clean_checksum(algorithm)
    # A digest catalogued without its algorithm, or as "unknown", is still
    # verified: its length names the algorithm.
    if algorithm_text is None:
        inferred = infer_checksum_algorithm(checksum)
        return None if inferred is None else (inferred, checksum.lower())
    # Normalize the algorithm to lowercase and check if it is supported
    algorithm_text = algorithm_text.lower()
    if algorithm_text not in SUPPORTED_CHECKSUM_ALGORITHMS:
        # If the algorithm is not supported, raise a DownloadError
        raise DownloadError(
            f"Unsupported checksum algorithm: {algorithm_text!r}")
    # if the algorithm is supported, return a tuple of (algorithm, checksum)
    return algorithm_text, checksum


def _checksum_from_row(row: Union[pd.Series, Mapping[str, object]]
                  ) -> Optional[ChecksumSpec]:
    """
    Used by add_frame.
    Extract the checksum specification from a DataFrame row.

    Args:
        row (Union[pd.Series, Mapping[str, object]]): Row from a DataFrame or a mapping

    Returns:
        Optional[ChecksumSpec]: The checksum specification, or None if not available.
    """
    # A catalogued checksum comes first: on a remote row, "sha1" only records
    # what an earlier download computed, while local manifests have no pair.
    catalogued = _parse_checksum(row.get("checksum"), row.get("checksum_algorithm"))
    if catalogued is not None:
        return catalogued
    # Otherwise use the "sha1" column, as local manifests do
    legacy_sha1 = _parse_checksum(row.get("sha1"))
    if legacy_sha1 is not None:
        return legacy_sha1
    return _checksum_from_config(row)


def _sha1_from_row(row) -> Optional[str]:
    """Return a row's valid SHA-1 in lowercase, or None."""
    value = _clean_checksum(row.get("sha1"))
    if value is None or not is_valid_checksum("sha1", value):
        return None
    return value.lower()


def _checksum_from_config(entry: dict) -> Optional[ChecksumSpec]:
    """
    Used by _select_download_items.
    Extract either legacy or builder checksum metadata from a config entry.

    Args:
        entry (dict): A configuration entry from the repository's data or meta section.

    Returns:
        Optional[ChecksumSpec]: A tuple of (algorithm, checksum) or just a checksum
        string, or if no valid checksum is found return the result of _parse_checksum
    """
    for algorithm in CHECKSUM_ALGORITHMS_BY_STRENGTH:
        # get the checksum value for the algorithm from the entry
        value = _clean_checksum(entry.get(algorithm))
        # if the value is None, continue to the next algorithm
        if value is None:
            continue
        # if the algorithm is "sha1", return just the value (legacy behavior)
        if algorithm == "sha1":
            return value
        # otherwise, return a tuple of (algorithm, value)
        return algorithm, value
    # if no valid checksum is found, return the result of _parse_checksum
    return _parse_checksum(entry.get("checksum"), entry.get("checksum_algorithm"))


def _checksum_pair(expected_checksum: Optional[ChecksumSpec]
                       ) -> Optional[tuple[str, str]]:
    """
    Used by _validate_expected_checksum and _merge_selected_file.
    Normalize a checksum specification into a tuple of (algorithm, checksum).

    Args:
        expected_checksum (Optional[ChecksumSpec]): The expected checksum specification.

    Returns:
        Optional[tuple[str, str]]: A tuple of (algorithm, checksum) if available,
        otherwise None.
    """
    # if expected_checksum is None, return None to indicate no valid checksum available
    if expected_checksum is None:
        return None

    # if expected_checksum is a tuple, validate its length and unpack it
    if isinstance(expected_checksum, tuple):
        if len(expected_checksum) != 2:
            # if the tuple does not have exactly two elements, raise a DownloadError
            raise DownloadError("Invalid checksum specification")
        algorithm, checksum = expected_checksum
    # otherwise, treat it as a legacy SHA-1 checksum
    else:
        algorithm = "sha1"
        checksum = expected_checksum

    # return a tuple of (algorithm, checksum) with both values normalized to lowercase
    return (str(algorithm).strip().lower(), str(checksum).strip().lower())


def _validate_expected_checksum(expected_checksum: Optional[ChecksumSpec]
                            ) -> Optional[tuple[str, str]]:
    """
    Used by _existing_file_status and _download_into_store.
    Validate and normalize a checksum specification.

    Args:
        expected_checksum (Optional[ChecksumSpec]): The expected checksum specification.

    Returns:
        Optional[tuple[str, str]]: A tuple of (algorithm, checksum) if valid,
        otherwise None.

    Raises:
        DownloadError: If the checksum specification is invalid.
    """
    # get the normalized checksum identity from the expected_checksum
    identity = _checksum_pair(expected_checksum)
    # if identity is None, return None to indicate no valid checksum available
    if identity is None:
        return None

    algorithm, checksum = identity
    # raise a DownloadError if the algorithm is not supported
    if algorithm not in SUPPORTED_CHECKSUM_ALGORITHMS:
        raise DownloadError(f"Unsupported checksum algorithm: {algorithm!r}")
    # raise a DownloadError if the checksum length is invalid or contains non-hex chars
    if not is_valid_checksum(algorithm, checksum):
        raise DownloadError(f"Invalid {algorithm} checksum: {checksum!r}")

    return algorithm, checksum


def _merge_selected_file(
    selected: dict[str, tuple[Path, Optional[ChecksumSpec]]],
    relative_path: Path,
    expected_checksum: Optional[ChecksumSpec]
    ) -> None:
    """
    Used by add_file and _unique_items.
    Merge a selected file into the dictionary of selected files, ensuring no
    conflicting checksums exist.

    Args:
        selected (dict[str, tuple[Path, Optional[ChecksumSpec]]]): The dictionary of
        selected files.
        relative_path (Path): The relative path of the file to merge.
        expected_checksum (Optional[ChecksumSpec]): The expected checksum of the file.

    Raises:
        DownloadError: If there is a conflicting checksum for the same file path.
    """
    # Use the POSIX representation of the relative path as the key
    key = relative_path.as_posix()
    # Compute the normalized checksum identity for the expected checksum
    new_identity = _checksum_pair(expected_checksum)
    # Check if the file is already in the selected dictionary
    existing = selected.get(key)

    # If the file is not already selected, or if it is selected without a checksum
    if existing is None:
        selected[key] = (relative_path, expected_checksum)
        # return early since there is no existing checksum to compare against
        return

    existing_checksum = existing[1]
    existing_identity = _checksum_pair(existing_checksum)
    # If there is no existing checksum, but an expected checksum is provided
    if existing_checksum is None:
        if expected_checksum is not None:
            # Update the selected entry with the new checksum
            selected[key] = (relative_path, expected_checksum)
        # return early since there is no existing checksum to compare against
        return

    # If there is an existing checksum, but no expected checksum is provided
    if expected_checksum is None:
        # return early since there is no new checksum to compare against
        return
    # If both existing and new checksums are present, compare their identities
    if existing_identity != new_identity:
        # If the checksums do not match, raise a DownloadError indicating a conflict
        raise DownloadError(f"Conflicting checksums for {key!r}")


def _validate_download_table_name(value) -> str:
    """
    Used by _select_download_items.
    Normalize a TSV name and expose validation failures as DownloadError.

    Args:
        value: The input TSV name to normalize.

    Returns:
        The normalized TSV name as a string.

    Raises:
        DownloadError: If the TSV name is invalid.
    """
    try:
        return validate_tsv_filename(value)
    except ValueError as exc:
        raise DownloadError(str(exc)) from exc


def _select_remote_config(repo, remote_name: Optional[str] = None) -> Optional[dict]:
    """
    Used by plan_download.
    Select a remote configuration from the repository.

    Args:
        repo: The hallmark repository object.
        remote_name: The name of the remote to select.

    Returns:
        The selected remote config as a dictionary, or None if no remote is configured.

    Raises:
        DownloadError: If the specified remote is not configured or invalid config
    """
    # Get the remote configuration from the repository state.
    configured = _repository_config(repo).get("remote")
    # If no remote configuration is found, return None.
    if not configured:
        return None
    # if the remote config is a dictionary, wrap it in a list for uniform processing
    if isinstance(configured, dict):
        remote_values = [configured]
    # if the remote config is a list, filter out any entries that are not dictionaries
    elif isinstance(configured, list):
        if any(not isinstance(remote, dict) for remote in configured):
            # raise a DownloadError if any entry in the list is not a dictionary
            raise DownloadError(
                "Invalid remote entry in config.yml; each remote must be a mapping")
        # retain all entries since they are all dictionaries
        remote_values = configured
    # otherwise, raise an error indicating invalid configuration
    else:
        raise DownloadError("Invalid remote configuration in config.yml")

    # try to normalize the remote configurations into a consistent list of dictionaries
    try:
        remotes = normalize_remotes(remote_values)
    # Handle exceptions raised by normalize_remotes and raise a DownloadError
    except ValueError as exc:
        raise DownloadError(str(exc)) from exc

    # Count the number of unnamed remotes in the configuration
    named_remotes = {remote_name_of(remote): remote for remote in remotes}
    # if a specific remote name is provided, attempt to select it from the named remotes
    if remote_name is not None:
        # Normalize the requested remote name to ensure it is a non-empty string
        requested_name = require_nonempty_string(
            remote_name, label="Remote name", exception_type=DownloadError)
        # try to retrieve the requested remote from the named remotes dictionary
        try:
            return named_remotes[requested_name]
        # raise a DownloadError if the requested remote is not found in the config
        except KeyError:
            raise DownloadError(
                f"Remote {requested_name!r} is not configured") from None

    # if there is only one remote configured, return it directly
    if len(remotes) == 1:
        return remotes[0]
    # if there is a remote named "origin", return it as the default choice
    if "origin" in named_remotes:
        return named_remotes["origin"]
    # if multiple remotes are configured and none is named "origin", raise an error
    names = [
        remote.get("name", "<unnamed>")
        for remote in remotes]
    raise DownloadError(
        "Multiple remotes are configured "
        f"({', '.join(names)}); select one with --remote")


def _resolve_remote_path(
        row: Union[pd.Series, Mapping[str, object]],
        data_config: list[dict]
        ) -> Path:
    """
    Used by add_frame.
    Resolve the remote path for a given row in a DataFrame based on the repository's
    data configuration.

    Args:
        row: A pandas Series or a mapping representing a row in the DataFrame.
        data_config: A list of dicts representing the repository's data configuration.

    Returns:
        A Path object representing the resolved remote path.

    Raises:
        KeyError: If the required columns for the format are missing in the row.
        TypeError: If the data format is invalid or cannot be processed.
        ValueError: If the data format is invalid or cannot be processed.
        DownloadError: If the remote path cannot be resolved from the repo metadata.
    """
    # if the row has a "path" column and it is not null, return it as a Path object
    if "path" in row and pd.notna(row["path"]):
        return Path(str(row["path"]))
    # if there is no "path" column, iterate through the data configuration
    for entry in data_config:
        fmt = entry.get("fmt")
        # if the format is not specified, skip this entry and continue to the next one
        if not fmt:
            continue
        # try to resolve the path using the row and format, handling potential errors
        try:
            return row_to_path(row, fmt)
        # case where the required columns for the format are missing in the row
        except KeyError:
            continue
        # case where the data format is invalid or cannot be processed
        except (TypeError, ValueError) as exc:
            raise DownloadError(
                f"Invalid data format {fmt!r} for remote download: {exc}") from exc

    # available is a string listing the available columns in the row for error reporting
    available = ", ".join(map(str, row.keys()))
    # raise a DownloadError if the path cannot be resolved from the repository metadata
    raise DownloadError(
        "Unable to resolve download path from repository metadata. "
        f"Available columns: {available}")


def _fetch_verified(context, relative_path, temp_path, *, checksum, size, sha1,
                    chunk_size, name):
    """
    Fetch a file and check it against everything the catalogue records.

    The size is compared first, so a truncated transfer fails even without a
    checksum. The catalogued digest and the file's SHA-1 are computed in one
    pass over the bytes.

    Args:
        context (OperationContext): Transfer resources.
        relative_path (Path): Path beneath the remote URL.
        temp_path (Path): Temporary file to write.
        checksum (tuple[str, str], optional): Validated catalogued digest.
        size (int, optional): Catalogued size in bytes.
        sha1 (str, optional): Known SHA-1 of the content.
        chunk_size (int): Transfer read size.
        name (str): File name for messages.

    Returns:
        tuple[int, str]: Bytes received and the content's SHA-1.

    Raises:
        DownloadError: If the size or a digest does not match.
    """
    context.transport.fetch(relative_path.as_posix(), temp_path, chunk_size=chunk_size)
    received = temp_path.stat().st_size
    if size is not None and received != size:
        raise DownloadError(
            f"Size mismatch for {name}: expected {size} bytes, received {received}")
    algorithms = {"sha1"} | ({checksum[0]} if checksum else set())
    digests = calculate_file_checksums(temp_path, algorithms, FILE_IO_CHUNK_SIZE)
    if checksum is not None and digests[checksum[0]] != checksum[1]:
        raise DownloadError(f"Checksum mismatch for {name} ({checksum[0]})")
    if sha1 is not None and digests["sha1"] != sha1:
        raise DownloadError(f"Checksum mismatch for {name} (sha1)")
    return received, digests["sha1"]


def _select_download_items(
    repo,
    file_paths: Sequence[str] = (),
    tsv_names: Sequence[str] = (),
    all_files: bool = False,
    ) -> list[DownloadItem]:
    """
    Select files to download from a hallmark repository based on the provided
    file paths, TSV names, and the repository's configuration.

    Args:
        repo: The hallmark repository object.
        file_paths: A sequence of specific file paths to download.
        tsv_names: A sequence of TSV names to download files from.
        all_files: If True, include all files from the repository's configuration.

    Returns:
        list[DownloadItem]: Relative paths, checksums, and available file metadata.
    """
    # Get the repository configuration, defaulting to an empty dictionary if not found.
    config = _repository_config(repo)

    # get the data configuration entries from the "data" section of the config
    data_config = _config_section_entries(config, "data")
    # dictionary to store selected files with relative paths and optional checksums.
    selected: dict[str, tuple[Path, Optional[ChecksumSpec]]] = {}
    metadata = {}
    known_sha1 = {}
    explicit_paths = {validate_remote_path(path).as_posix() for path in file_paths}
    folder_paths = {validate_remote_path(path).as_posix() for path in file_paths
                    if str(path).endswith("/")}
    matched_paths = set()

    def add_file(
            value: Union[str, Path],
            expected_checksum: Optional[ChecksumSpec] = None,
            row=None,
            *, explicit_only=False,
            ) -> None:
        """Add a file and its catalog metadata, checking for conflicting records."""
        # Resolve the input value to a safe relative path
        relative_path = validate_remote_path(value)
        matched = False
        name = relative_path.as_posix()
        for requested in explicit_paths:
            if ((name == requested and requested not in folder_paths)
                    or name.startswith(requested + "/")):
                matched_paths.add(requested)
                matched = True
        if explicit_only and not matched:
            return
        # Merge the selected file into the dictionary of selected files, ensuring no
        # conflicting checksums exist.
        _merge_selected_file(selected, relative_path, expected_checksum)
        if row is not None:
            size = _parse_file_size(row.get("size_bytes"))
            modified = row.get("mtime")
            modified = (None if modified is None or pd.isna(modified)
                        or str(modified).strip() == "" else str(modified))
            previous_size, previous_modified = metadata.get(
                relative_path.as_posix(), (None, None))
            if size is not None and previous_size not in (None, size):
                raise DownloadError(
                    f"Conflicting sizes for {relative_path.as_posix()!r}")
            metadata[relative_path.as_posix()] = (
                size if size is not None else previous_size,
                modified if modified is not None else previous_modified)
            sha1 = _sha1_from_row(row)
            if sha1 is not None:
                if known_sha1.get(name, sha1) != sha1:
                    raise DownloadError(f"Conflicting SHA-1 for {name!r}")
                known_sha1[name] = sha1

    def add_frame(frame: pd.DataFrame, fmt_entries: list[dict],
                  *, explicit_only=False) -> None:
        """Add matching catalog rows, retaining their checksums and file metadata."""
        # if the DataFrame is None or empty, return early without adding any files
        if frame is None or frame.empty:
            return
        # get the column names from the DataFrame to use for creating dictionaries
        columns = tuple(frame.columns)
        # for each value in the DataFrame
        for values in frame.itertuples(index=False, name=None):
            # create a dictionary mapping column names to their corresponding values
            row = dict(zip(columns, values))
            # add the resolved remote path and its checksum to the selected files
            relative_path = validate_remote_path(_resolve_remote_path(row, fmt_entries))
            add_file(relative_path, _checksum_from_row(row), row,
                     explicit_only=explicit_only)

    # Organize data configuration entries by their TSV names for easier access.
    entries_by_tsv: dict[str, list[dict]] = {}
    for entry in data_config:
        # Path catalogs need no parameterized filename format.
        if not entry.get("db"):
            continue
        # download_tsv_name will normalize and validate the TSV name
        tsv_name = _validate_download_table_name(entry["db"])
        # Add the entry to the list of entries for the corresponding TSV name
        entries_by_tsv.setdefault(tsv_name, []).append(entry)


    requested_tsvs = []
    seen_tsvs = set()
    for name in tsv_names:
        # download_tsv_name will normalize and validate the TSV name
        tsv_name = _validate_download_table_name(name)
        # if the TSV name has not been seen before, add it to requested list and seen
        if tsv_name not in seen_tsvs:
            requested_tsvs.append(tsv_name)
            seen_tsvs.add(tsv_name)

    # if the all_files flag is set, include all TSVs in the requested list and seen set
    if all_files:
        for tsv_name in entries_by_tsv:
            if tsv_name not in seen_tsvs:
                requested_tsvs.append(tsv_name)
                seen_tsvs.add(tsv_name)

    lookup_tsvs = list(requested_tsvs)
    if explicit_paths:
        lookup_tsvs.extend(name for name in entries_by_tsv
                           if name not in requested_tsvs)
    for tsv_name in lookup_tsvs:
        fmt_entries = entries_by_tsv.get(tsv_name)
        # if there are no format entries for the requested TSV
        if fmt_entries is None:
            configured = ", ".join(entries_by_tsv) or "<none>"
            # raise an error indicating that the TSV is not configured in the repository
            raise DownloadError(
                f"TSV {tsv_name!r} is not configured. "
                f"Configured TSVs: {configured}")

        if tsv_name == "data.tsv":
            # The catalogue is the state, loaded from the index; data.tsv on
            # disk can hold unstaged rows.
            add_frame(repo.state.data, fmt_entries,
                      explicit_only=tsv_name not in requested_tsvs)
            continue
        # Construct the path to the TSV file in the repository's dothm directory.
        tsv_path = repo.dothm.path / tsv_name
        # if the TSV file does not exist at the constructed path, raise an error
        if not tsv_path.is_file():
            raise DownloadError(f"Configured TSV does not exist: {tsv_path}")
        # try to read the TSV file in chunks and add its entries to the selected files
        try:
            frames = pd.read_csv(
                tsv_path,
                sep="\t",
                dtype=str,
                keep_default_na=False,
                chunksize=TSV_READ_CHUNK_SIZE)
            for frame in frames:
                add_frame(frame, fmt_entries,
                          explicit_only=tsv_name not in requested_tsvs)
        # skip empty TSV files without raising an error
        except pd.errors.EmptyDataError:
            continue
        # raise a DownloadError if there is an issue reading the TSV file
        except (OSError, UnicodeError, pd.errors.ParserError) as exc:
            raise DownloadError(f"Unable to read TSV {tsv_path}: {exc}") from exc

    if all_files or explicit_paths:
        # for each section ("data" and "meta") in the repository configuration
        for section_name in ("data", "meta"):
            # iterate through each entry in the section
            for entry in _config_section_entries(config, section_name):
                # get the file path from the entry, if it exists
                file_path = entry.get("file")
                # if a file path is specified in the entry, add it to the selected files
                if file_path:
                    add_file(file_path, _checksum_from_config(entry), entry,
                             explicit_only=not all_files)

    # Determine whether to use legacy data formats based on the presence of file paths,
    # TSV names, and all_files flag.
    use_legacy_data = (not file_paths and not tsv_names and not all_files
                        ) or (all_files and not entries_by_tsv)
    if use_legacy_data:
        # legacy_formats are entries in the data configuration that have a "fmt" key
        legacy_formats = [entry for entry in data_config if entry.get("fmt")]
        # add files from legacy formats to the selected files
        add_frame(repo.state.data, legacy_formats)
    elif explicit_paths and not entries_by_tsv:
        add_frame(repo.state.data, data_config, explicit_only=True)

    unknown_paths = sorted(explicit_paths - matched_paths)
    if unknown_paths:
        raise DownloadError(
            "Paths not in the catalog, or folders with no catalogued files:\n"
            + "\n".join(unknown_paths))
    return [DownloadItem(path, checksum, *metadata.get(path.as_posix(), (None, None)),
                         sha1=known_sha1.get(path.as_posix()))
            for path, checksum in selected.values()]


def _parse_file_size(value) -> Optional[int]:
    """Read a recorded size without treating missing or malformed sizes as zero."""
    if value is None or isinstance(value, bool) or pd.isna(value):
        return None
    try:
        size = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not size.is_finite() or size < 0 or size != size.to_integral_value():
        return None
    return int(size)


def _existing_file_status(item, destination, store):
    """
    Classify an existing destination as ours (skip) or a conflict.

    Returns:
        DownloadItem | None: The item to skip, carrying the file's SHA-1 when
        it was computed, or None for a conflict.
    """
    if not destination.is_file() or destination.is_symlink():
        return None
    if store is not None and item.sha1 is not None:
        # A link is the object itself, trusted while the object is unchanged.
        if store.is_linked(item.sha1, destination) and store.verify(item.sha1):
            if (item.size_bytes is not None
                    and destination.stat().st_size != item.size_bytes):
                return None
            return item
    checksum = _validate_expected_checksum(item.checksum)
    if (checksum is None and item.sha1 is None
            and (store is None or destination.stat().st_nlink < 2)):
        # Without a digest only a link into the object store identifies the file.
        return None
    algorithms = {"sha1"} | ({checksum[0]} if checksum else set())
    digests = calculate_file_checksums(destination, algorithms)
    if checksum is not None:
        verified = digests[checksum[0]] == checksum[1]
    elif item.sha1 is not None:
        verified = digests["sha1"] == item.sha1
    else:
        # Downloaded earlier, but its SHA-1 was not staged before it stopped.
        verified = store.is_linked(digests["sha1"], destination)
    if not verified or (item.sha1 is not None and digests["sha1"] != item.sha1):
        return None
    return replace(item, sha1=digests["sha1"])


def _check_existing_files(items, output_root, store=None):
    """
    Sort plan items by what their destinations hold.

    Args:
        items: Plan items to check.
        output_root (Path): Destination root.
        store (Objects, optional): Object store. Without one, nothing is
            placed from the store and only a verified copy is skipped.

    Returns:
        tuple[list, list, list]: Items to transfer, items whose content is
        already stored, and items already at their destination.

    Raises:
        DownloadError: If any destination holds something else, so that
            nothing is downloaded until the user removes it.
    """
    pending = []
    stored = []
    skipped = []
    conflicts = []
    for item in items:
        try:
            destination = resolve_path_in_root(
                output_root, item.relative_path, label="download destination")
            blocked_parent = False
            for parent in destination.parents:
                if parent == output_root:
                    break
                if parent.exists() and not parent.is_dir():
                    blocked_parent = True
                    break
            if blocked_parent:
                conflicts.append(item.relative_path.as_posix())
            elif not os.path.lexists(destination):
                if (store is not None and item.sha1 is not None
                        and store.verify(item.sha1)):
                    stored.append(item)
                else:
                    pending.append(item)
            else:
                edited = (store is not None and item.sha1 is not None
                          and store.is_linked(item.sha1, destination)
                          and store.changed(item.sha1))
                status = _existing_file_status(item, destination, store)
                if status is None:
                    conflicts.append(item.relative_path.as_posix() + (
                        " (edited in place; delete it to download a fresh copy)"
                        if edited else ""))
                else:
                    skipped.append(status)
        except (OSError, ValueError) as exc:
            raise DownloadError(str(exc)) from exc
    if conflicts:
        raise DownloadError(
            "Existing paths conflict with the catalog or cannot be verified:\n"
            + "\n".join(conflicts)
            + "\nNothing downloaded. Delete these paths first to replace them.")
    return pending, stored, skipped


def _remote_summary(remote: dict) -> dict:
    """Pin the parts of a remote configuration a transfer depends on."""
    spec = RemoteSpec.from_url(
        remote["url"], remote.get("auth"), backend=remote.get("backend"),
        backend_options=remote.get("backend_options"))
    return {"url": remote["url"], "auth": remote.get("auth"),
            "backend": spec.backend,
            "backend_options": remote.get("backend_options") or {}}


def _route_items(repo, items, remote_name, source_names=None):
    """
    Attach each item's remote and its path beneath that remote's URL.

    Rows follow their source's remote. An explicit remote name serves every
    item instead, keeping each path beneath its source's remote, as for a
    mirror. Files outside any source use the default remote. With
    ``source_names``, only rows of those sources are kept.

    Returns:
        tuple[list[DownloadItem], dict[str, dict]]: Routed items and the
        configuration of each remote they use, by name.
    """
    try:
        sources = effective_sources(repo.state)
        configured = {remote_name_of(remote): remote for remote in
                      normalize_remotes(_repository_config(repo).get("remote"))}
    except ValueError as exc:
        raise DownloadError(str(exc)) from exc
    by_name = {source.name: source for source in sources}
    if source_names is not None:
        unknown = sorted(set(source_names) - set(by_name))
        if unknown:
            raise DownloadError(
                f"Unknown source(s): {', '.join(unknown)}. Catalog sources: "
                + (", ".join(by_name) or "<none>"))
    data = repo.state.data
    owners = (dict(zip(data["path"].astype(str), data["source"].astype(str)))
              if {"path", "source"} <= set(data.columns) else {})
    override = (_select_remote_config(repo, remote_name)
                if remote_name is not None else None)
    default = None
    routed, used = [], {}
    for item in items:
        path = item.relative_path.as_posix()
        source = by_name.get(owners.get(path)) or (
            sources[0] if len(sources) == 1 else None)
        if source_names is not None and (source is None
                                         or source.name not in source_names):
            continue
        if override is not None:
            remote = override
        elif source is not None:
            remote = configured.get(source.remote)
            if remote is None:
                raise DownloadError(
                    f"Source {source.name!r} uses unconfigured remote "
                    f"{source.remote!r}")
        else:
            default = default or _select_remote_config(repo)
            remote = default
        if remote is None or not remote.get("url"):
            raise DownloadError("No remote URL is configured in config.yml")
        name = remote_name_of(remote)
        used[name] = remote
        remote_path = source.remote_path(path) if source is not None else path
        routed.append(replace(
            item, remote=name,
            remote_path=Path(remote_path) if remote_path != path else None))
    return routed, used


def plan_download(
    repo,
    output_path: Optional[Union[Path, str]] = None,
    *,
    file_paths: Optional[Sequence[str]] = None,
    tsv_names: Optional[Sequence[str]] = None,
    all_files: bool = False,
    filter: Optional[Union[str, Sequence[str]]] = None,
    fmt: Optional[str] = None,
    remote_name: Optional[str] = None,
    estimated_bytes_per_second: Optional[float] = None,
    sources: Optional[Sequence[str]] = None,
) -> DownloadPlan:
    """
    Plan a download using local catalog metadata.

    No network requests are made. With no explicit paths or TSVs, select
    the complete catalog before applying filters. Missing sizes remain unknown.
    Folder paths select catalogued descendants. Existing checksum matches are
    skipped, and unknown paths or conflicting existing files raise an error.

    Args:
        repo: The hallmark repository object.
        output_path (Path | str, optional): Destination directory. Defaults
            to the worktree; required for a bare repository.
        file_paths (sequence[str], optional): Remote-relative file paths.
        tsv_names (sequence[str], optional): Catalog TSVs to select.
        all_files (bool): Select all configured files. Cannot be combined
            with explicit paths or TSVs. Defaults to False.
        filter (str | list[str], optional): Relative path glob or globs.
        fmt (str, optional): Filename format that selected paths must match.
        remote_name (str, optional): Configured data remote to use.
        estimated_bytes_per_second (float, optional): Positive rate used
            to estimate duration when every file size is known.
        sources (sequence[str], optional): Keep only files of these
            catalogue sources.

    Returns:
        DownloadPlan: Immutable files, source, destination, and metadata
        for review before approval.

    Raises:
        DownloadError: If the catalog, remote, selection, or destination
            is invalid.
        ValueError: If a filter, format, or supplied rate is invalid.
    """
    if output_path is None:
        output_path = getattr(repo, "worktree", None)
    if output_path is None:
        raise DownloadError("output_path is required for a bare repository")
    output_root = Path(output_path).expanduser().resolve()
    if output_root.exists() and not output_root.is_dir():
        raise DownloadError(f"Download output is not a directory: {output_root}")
    if isinstance(file_paths, (str, Path)):
        file_paths = (str(file_paths),)
    if isinstance(tsv_names, str):
        tsv_names = (tsv_names,)
    file_paths, tsv_names = tuple(file_paths or ()), tuple(tsv_names or ())
    if all_files and (file_paths or tsv_names):
        raise DownloadError("all_files cannot be combined with paths or TSVs")
    items = _select_download_items(
        repo, file_paths=file_paths, tsv_names=tsv_names,
        all_files=all_files or (not file_paths and not tsv_names))
    if filter is not None or fmt is not None:
        from .discovery import path_matches
        items = [item for item in items if path_matches(
            item.relative_path.as_posix(), filter=filter, fmt=fmt)]
    if isinstance(sources, str):
        sources = (sources,)
    items, used = _route_items(
        repo, items, remote_name,
        source_names=set(sources) if sources is not None else None)
    if len(used) == 1:
        remote = next(iter(used.values()))
    elif not used:
        remote = _select_remote_config(repo, remote_name) or {}
    else:
        remote = {}
    remotes = {name: _remote_summary(config) for name, config in used.items()}
    pinned = _remote_summary(remote) if remote.get("url") else {}
    items, stored, skipped = _check_existing_files(
        items, output_root, getattr(repo, "objects", None))
    return DownloadPlan(
        tuple(items), remote.get("url"), output_root,
        remote_auth=remote.get("auth"),
        remote_name=remote_name_of(remote) if remote.get("url") else None,
        estimated_bytes_per_second=estimated_bytes_per_second,
        remote_backend=pinned.get("backend"),
        backend_options=remote.get("backend_options"), skipped_items=tuple(skipped),
        remotes=remotes, stored_items=tuple(stored))


def execute_download_plan(
    repo,
    plan: DownloadPlan,
    *,
    approved: bool = False,
    max_workers: int = 4,
    show_progress: bool = False,
) -> dict:
    """
    Execute a download plan using its recorded source and destination.

    Args:
        repo: Repository associated with the plan. Current configuration
            is not used to change the planned transfer.
        plan (DownloadPlan): Files and destination presented for approval.
        approved (bool): Must be True for a nonempty plan. Defaults to False.
        max_workers (int): Maximum concurrent download workers. Defaults to 4.
        show_progress (bool): Show byte progress. Defaults to False.

    Returns:
        dict: Results with ``succeeded``, ``failed``, ``total_bytes``, and
        ``errors`` keys. Individual transfer failures are recorded here.

    Raises:
        TypeError: If ``plan`` is not a DownloadPlan.
        DownloadError: If approval is missing, setup fails, or the
            destination is invalid.
    """
    if not isinstance(plan, DownloadPlan):
        raise TypeError("plan must be a DownloadPlan")
    if plan.has_changes and approved is not True:
        raise DownloadError("Dataset downloads require explicit approval")
    max_workers = _require_positive_integer(max_workers, label="max_workers")
    results = {"succeeded": 0, "failed": 0, "total_bytes": 0, "errors": []}
    store = getattr(repo, "objects", None)
    if not plan.has_changes:
        if store is not None:
            # Files found complete while planning may still need their SHA-1.
            _stage_download_sha1(repo, plan, _recovered_sha1(plan.skipped_items),
                                 results)
        return results
    if store is None:
        raise DownloadError("Downloads need a repository's object store")
    if plan.output_path.resolve() != plan.output_path:
        raise DownloadError("Download destination changed since planning")
    store.cleanup_stale_temps()
    items, stored, skipped = _check_existing_files(
        _unique_items(plan.items + plan.stored_items), plan.output_path, store)
    if skipped:
        results["skipped"] = [item.relative_path.as_posix() for item in skipped]

    results.update(sha1={}, copied=[])
    try:
        # A file planned from the store is never transferred without approval.
        planned_from_store = {item.relative_path for item in plan.stored_items}
        for item in items:
            if item.relative_path in planned_from_store:
                results["failed"] += 1
                results["errors"].append(
                    f"The stored copy of {item.relative_path.as_posix()} is "
                    "missing; run hm download again")
        items = [item for item in items
                 if item.relative_path not in planned_from_store]
        for item in stored:
            _record_placed(results, _place_from_store(
                plan.output_path, store, [item], item.sha1))
        placed_from_store = [item.relative_path.as_posix() for item in stored
                             if item.relative_path.as_posix() in results["sha1"]]
        if placed_from_store:
            results["from_store"] = placed_from_store
        _transfer_by_remote(plan, items, results, store,
                            max_workers=max_workers, show_progress=show_progress)
    finally:
        # Steps 3 and 4: record what is now in place, even after an interrupt.
        found = {**_recovered_sha1(plan.skipped_items + tuple(skipped)),
                 **results.pop("sha1")}
        if not results["copied"]:
            results.pop("copied")
        _stage_download_sha1(repo, plan, found, results)
    return results


def _unique_items(items):
    """
    Merge plan items that name the same path.

    Catalogue selection already merges rows, but a plan can also be built by
    hand. A checksum fills in a missing one; two different checksums for one
    path raise DownloadError, as in selection.
    """
    selection, first = {}, {}
    for item in items:
        _merge_selected_file(selection, item.relative_path, item.checksum)
        first.setdefault(item.relative_path.as_posix(), item)
    return [replace(first[key], checksum=checksum)
            for key, (_, checksum) in selection.items()]


def _recovered_sha1(items) -> dict:
    """Return the SHA-1 of skipped items whose content was hashed."""
    return {item.relative_path.as_posix(): item.sha1 for item in items
            if item.sha1 is not None}


def _stage_download_sha1(repo, plan, sha1_by_path, results) -> None:
    """
    Record the SHA-1 of files now in place and stage it in ``.hm``.

    Only remote catalogue rows without a SHA-1 are updated, and only while
    their catalogued digest still matches the plan; the change is staged,
    not committed. Any failure here, after the files are in place, is
    reported in ``staging_error`` rather than raised.

    A catalogue written before sources existed is given its source first,
    as ``hm add URL`` does: with a ``sha1`` column and no sources it would
    look like a catalogue of local files. One naming several remotes cannot
    say which remote each row came from, so its SHA-1s are not recorded and
    ``staging_note`` says why.
    """
    if not sha1_by_path:
        return
    if not is_remote_catalog(repo.state) or "path" not in repo.state.data.columns:
        return
    # A download can take hours, so the catalogue is read again under the
    # lock: only SHA-1 cells are filled in, and other changes are kept.
    try:
        with repo.dothm.state_lock():
            _stage_into_current_state(repo, plan, sha1_by_path, results)
    except Exception as exc:
        results["staging_error"] = (
            f"Downloaded files are in place, but their SHA-1 could not be "
            f"recorded: {exc}")


def _stage_into_current_state(repo, plan, sha1_by_path, results) -> None:
    """Used by _stage_download_sha1, with the state lock held."""
    state = repo.dothm.load_state()
    if not is_remote_catalog(state) or "path" not in state.data.columns:
        return
    upgraded = False
    if "sha1" not in state.data.columns and "sources" not in state.config:
        if not materialize_sources(state):
            results["staging_note"] = (
                "SHA-1s not recorded: this catalog predates sources and names "
                "several remotes; update it with hm add URL first")
            return
        upgraded = True
    planned = {item.relative_path.as_posix(): item for item in
               plan.items + plan.stored_items + plan.skipped_items}
    data = state.data.copy()
    if "sha1" not in data.columns:
        data["sha1"] = ""
    known = data["sha1"].fillna("").astype(str)
    updates = 0
    for index, path in data["path"].astype(str).items():
        sha1 = sha1_by_path.get(path)
        if sha1 is None or known[index] or path not in planned:
            continue
        row = data.loc[index].to_dict()
        catalogued = _parse_checksum(row.get("checksum"), row.get("checksum_algorithm"))
        # The catalogue changed since planning; this SHA-1 may describe old bytes.
        if _checksum_pair(catalogued) != _checksum_pair(planned[path].checksum):
            continue
        data.at[index, "sha1"] = sha1
        updates += 1
    if not updates:
        return
    state.data = data
    repo.dothm.save_state(state)
    repo.state = state
    results["staged"] = updates
    if upgraded:
        results["catalog_upgraded"] = True


def _transfer_by_remote(plan, items, results, store, *, max_workers,
                        show_progress):
    """Transfer items over one connection per remote recorded in the plan."""
    groups: dict[Optional[str], list[DownloadItem]] = {}
    for item in items:
        groups.setdefault(item.remote if item.remote in plan.remotes else None,
                          []).append(item)
    for name, group in groups.items():
        if name is None:
            remote = RemoteSpec.from_url(
                plan.remote_url, plan.remote_auth, backend=plan.remote_backend,
                backend_options=plan.backend_options)
        else:
            pinned = plan.remotes[name]
            remote = RemoteSpec.from_url(
                pinned["url"], pinned.get("auth"), backend=pinned.get("backend"),
                backend_options=pinned.get("backend_options"))
        total_bytes = None
        if all(item.size_bytes is not None for item in group):
            total_bytes = sum(item.size_bytes for item in group)
        _download_selected_into_store(
            remote, plan.output_path, group, store, results,
            max_workers=max_workers, show_progress=show_progress,
            total_bytes=total_bytes)


def _run_transfers(remote_spec, output_root, jobs, run_job, record, *, max_workers,
                   show_progress, total_bytes=None):
    """
    Run transfer jobs on one remote with a bounded window of workers.

    Args:
        remote_spec (RemoteSpec): Remote serving every job.
        output_root (Path): Resolved destination root.
        jobs (list): Work items passed to ``run_job``.
        run_job (callable): ``run_job(context, job)``, run in a worker thread.
        record (callable): ``record(job, result, error)`` in the calling thread
            as each job finishes; ``error`` is the DownloadError message.
        max_workers (int): Maximum concurrent workers.
        show_progress (bool): Show byte progress.
        total_bytes (int, optional): Expected bytes for the progress bar.
    """
    # track transferred bytes
    progress = tqdm(
        total=total_bytes, unit="B", unit_scale=True, disable=not show_progress)
    progress_lock = Lock()
    finished = 0
    failed = 0

    def advance_bytes(count):
        # Transport callbacks run concurrently, one for each active worker.
        with progress_lock:
            progress.update(count)

    try:
        with OperationContext(remote_spec, output_root) as context:
            context.on_bytes = advance_bytes
            context.transport.prepare()
            executor = ThreadPoolExecutor(
                max_workers=min(max_workers, context.settings.max_sessions)
                if remote_spec.scheme in {"ssh", "sftp"} else max_workers)
            pending = {}
            try:
                job_iterator = iter(jobs)
                # Bound queued work as well as running workers.
                window_size = max_workers * 2

                def submit_next() -> bool:
                    """Submit the next job; return False when none remain."""
                    try:
                        job = next(job_iterator)
                    except StopIteration:
                        return False
                    pending[executor.submit(run_job, context, job)] = job
                    return True

                for _ in range(min(window_size, len(jobs))):
                    submit_next()
                while pending:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    # Aggregate per-file failures and keep successful downloads.
                    # An interrupt waits until the rest of its batch is recorded.
                    stop = None
                    for future in completed:
                        job = pending.pop(future)
                        try:
                            outcome = future.result()
                        except DownloadError as exc:
                            failed += 1
                            record(job, None, str(exc))
                        except BaseException as exc:
                            stop = stop or exc
                        else:
                            record(job, outcome, None)
                        finished += 1
                        with progress_lock:
                            progress.set_postfix(
                                files=f"{finished}/{len(jobs)}", failed=failed)
                    if stop is not None:
                        raise stop
                    for _ in completed:
                        submit_next()
            except BaseException:
                context.cancel()
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                # Jobs that finished while stopping still reached the store.
                for future, job in pending.items():
                    if (future.done() and not future.cancelled()
                            and future.exception() is None):
                        record(job, future.result(), None)
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
    # close after all downloads are complete, regardless of success or failure
    finally:
        progress.close()


def _output_root(worktree_path) -> Path:
    output_root = Path(worktree_path).expanduser().resolve()
    if output_root.exists() and not output_root.is_dir():
        raise DownloadError(f"Download output is not a directory: {output_root}")
    return output_root


def _destination(output_root, relative_path) -> Path:
    try:
        return resolve_path_in_root(output_root, relative_path,
                                    label="download destination")
    except ValueError as exc:
        raise DownloadError(str(exc)) from exc


def _download_into_store(context, store, item, chunk_size):
    """
    Fetch one file into the object store and place it at its destination.

    The transfer goes to a temporary file in the store, which becomes the SHA-1
    object only once its size and digests are verified. The destination is
    then hard-linked to that object, or receives a verified copy. Files with
    identical content share one object.

    Returns:
        tuple[int, list]: Bytes received, and ``(item, sha1, how, error)``.
    """
    name = item.relative_path.name
    try:
        with store.temp_file(name) as temp:
            received, sha1 = _fetch_verified(
                context, validate_remote_path(item.source_path), temp,
                checksum=_validate_expected_checksum(item.checksum),
                size=item.size_bytes, sha1=item.sha1, chunk_size=chunk_size,
                name=name)
            context.check_cancelled()
            store.publish(temp, sha1)
    except (OSError, ValueError) as exc:
        raise DownloadError(f"Failed to download {name}: {exc}") from None
    return received, _place_from_store(context.output_root, store, [item], sha1)


def _place_from_store(output_root, store, items, sha1):
    """Link or copy a stored object to each item's destination."""
    placed = []
    for item in items:
        name = item.relative_path.as_posix()
        try:
            destination = resolve_path_in_root(
                output_root, item.relative_path, label="download destination")
            destination.parent.mkdir(parents=True, exist_ok=True)
            placed.append((item, sha1, store.materialize(sha1, destination), None))
        except FileExistsError:
            placed.append((item, sha1, None,
                           f"{name} appeared at its destination during the "
                           "download; the existing file was kept"))
        except FileNotFoundError:
            placed.append((item, sha1, None,
                           f"The stored copy of {name} is missing; "
                           "run hm download again"))
        except (OSError, ValueError) as exc:
            placed.append((item, sha1, None, f"Failed to place {name}: {exc}"))
    return placed


def _download_selected_into_store(remote_spec, worktree_path, items, store, results,
                                  *, max_workers, show_progress, total_bytes=None):
    """
    Download plan items from one remote through the object store.

    Each placed file is recorded in ``results``: counts, ``copied`` paths and
    the ``sha1`` of every file now at its destination.
    """
    output_root = _output_root(worktree_path)

    def run_job(context, item):
        return _download_into_store(context, store, item, DOWNLOAD_CHUNK_SIZE)

    def record(item, outcome, error):
        if error is not None:
            results["failed"] += 1
            results["errors"].append(error)
            return
        received, placed = outcome
        results["total_bytes"] += received
        _record_placed(results, placed)

    _run_transfers(remote_spec, output_root, list(items), run_job, record,
                   max_workers=max_workers, show_progress=show_progress,
                   total_bytes=total_bytes)


def _record_placed(results, placed):
    for item, sha1, how, error in placed:
        if error is not None:
            results["failed"] += 1
            results["errors"].append(error)
            continue
        results["succeeded"] += 1
        results["sha1"][item.relative_path.as_posix()] = sha1
        if how == "copied":
            results["copied"].append(item.relative_path.as_posix())
