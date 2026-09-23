"""Remote data file downloader for hallmark repositories."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Mapping, Optional, Sequence, Union
from urllib.parse import unquote, urlsplit, urlunsplit
from tqdm import tqdm

import requests
import pandas as pd

from .transport import OperationContext, RemoteSpec
from .transport.base import DownloadError, literal_path
from .download_plan import DownloadItem, DownloadPlan
from .helper_functions import (
    CHECKSUM_ALGORITHMS_BY_STRENGTH,
    SUPPORTED_CHECKSUM_ALGORITHMS,
    as_list_of_dicts,
    atomic_output_path,
    file_checksum,
    normalize_nonempty_string,
    resolve_contained_path,
    valid_checksum)
from .repo_config import (
    normalize_remotes,
    normalize_tsv_name,
    row_to_path)

# Maximum number of files to download before showing a warning message.
BULK_DOWNLOAD_WARNING_FILE_COUNT = 100
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
    Used by download_remote_data and _download_file.
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
    Used by _checksum_spec and _entry_checksum.
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


def _checksum_spec(value, algorithm=None) -> Optional[ChecksumSpec]:
    """
    Used by _row_checksum and _entry_checksum.
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
    # If the algorithm is None, return None to indicate no valid algorithm is available
    if algorithm_text is None:
        return None
    # Normalize the algorithm to lowercase and check if it is supported
    algorithm_text = algorithm_text.lower()
    if algorithm_text not in SUPPORTED_CHECKSUM_ALGORITHMS:
        # If the algorithm is not supported, raise a DownloadError
        raise DownloadError(
            f"Unsupported checksum algorithm: {algorithm_text!r}")
    # if the algorithm is supported, return a tuple of (algorithm, checksum)
    return algorithm_text, checksum


def _row_checksum(row: Union[pd.Series, Mapping[str, object]]
                  ) -> Optional[ChecksumSpec]:
    """
    Used by add_frame.
    Extract the checksum specification from a DataFrame row.

    Args:
        row (Union[pd.Series, Mapping[str, object]]): Row from a DataFrame or a mapping

    Returns:
        Optional[ChecksumSpec]: The checksum specification, or None if not available.
    """
    # Check for legacy SHA-1 checksum in the "sha1" column first
    legacy_sha1 = _checksum_spec(row.get("sha1"))
    # If a legacy SHA-1 checksum is found, return it immediately
    if legacy_sha1 is not None:
        return legacy_sha1
    # If not found, check the "checksum" and "checksum_algorithm" columns
    return (_checksum_spec(row.get("checksum"), row.get("checksum_algorithm"))
            or _entry_checksum(row))


def _entry_checksum(entry: dict) -> Optional[ChecksumSpec]:
    """
    Used by _select_download_items.
    Extract either legacy or builder checksum metadata from a config entry.

    Args:
        entry (dict): A configuration entry from the repository's data or meta section.

    Returns:
        Optional[ChecksumSpec]: A tuple of (algorithm, checksum) or just a checksum
        string, or if no valid checksum is found return the result of _checksum_spec
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
    # if no valid checksum is found, return the result of _checksum_spec
    return _checksum_spec(entry.get("checksum"), entry.get("checksum_algorithm"))


def _checksum_identity(expected_checksum: Optional[ChecksumSpec]
                       ) -> Optional[tuple[str, str]]:
    """
    Used by _validate_checksum_spec and _merge_selected_file.
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


def _validate_checksum_spec(expected_checksum: Optional[ChecksumSpec]
                            ) -> Optional[tuple[str, str]]:
    """
    Used by _download_file and _fetch_file.
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
    identity = _checksum_identity(expected_checksum)
    # if identity is None, return None to indicate no valid checksum available
    if identity is None:
        return None

    algorithm, checksum = identity
    # raise a DownloadError if the algorithm is not supported
    if algorithm not in SUPPORTED_CHECKSUM_ALGORITHMS:
        raise DownloadError(f"Unsupported checksum algorithm: {algorithm!r}")
    # raise a DownloadError if the checksum length is invalid or contains non-hex chars
    if not valid_checksum(algorithm, checksum):
        raise DownloadError(f"Invalid {algorithm} checksum: {checksum!r}")

    return algorithm, checksum


def _merge_selected_file(
    selected: dict[str, tuple[Path, Optional[ChecksumSpec]]],
    relative_path: Path,
    expected_checksum: Optional[ChecksumSpec]
    ) -> None:
    """
    Used by add_file and _download_selected.
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
    new_identity = _checksum_identity(expected_checksum)
    # Check if the file is already in the selected dictionary
    existing = selected.get(key)

    # If the file is not already selected, or if it is selected without a checksum
    if existing is None:
        selected[key] = (relative_path, expected_checksum)
        # return early since there is no existing checksum to compare against
        return

    existing_checksum = existing[1]
    existing_identity = _checksum_identity(existing_checksum)
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


def _safe_remote_path(value: Union[str, Path]) -> Path:
    """
    Used by _select_download_items and _download_selected.
    Validate and return a safe relative Path object for a remote file path.
    Args:
        value: The input path to validate.

    Returns:
        A safe relative Path object.

    Raises:
        DownloadError: If the path is unsafe.
    """
    return literal_path(value)


def _remote_file_url(remote_url: str, relative_path: Path) -> str:
    """
    Construct a file URL from a data remote and a literal catalog path.

    Args:
        remote_url (str): URL of the dataset root.
        relative_path (Path): File path relative to the dataset root.

    Returns:
        str: URL with the relative path encoded for the transport.
    """
    return RemoteSpec.parse(remote_url).file_url(relative_path.as_posix())


def _download_tsv_name(value) -> str:
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
        return normalize_tsv_name(value)
    except ValueError as exc:
        raise DownloadError(str(exc)) from exc


def _select_remote_config(repo, remote_name: Optional[str] = None) -> Optional[dict]:
    """
    Used by download_remote_data and plan_download.
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
    named_remotes = {remote["name"]: remote for remote in remotes if "name" in remote}
    # if a specific remote name is provided, attempt to select it from the named remotes
    if remote_name is not None:
        # Normalize the requested remote name to ensure it is a non-empty string
        requested_name = normalize_nonempty_string(
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


def _verify_validated_checksum(
    path: Path,
    expected_checksum: Optional[tuple[str, str]],
    chunk_size: int,
    ) -> None:
    """
    Used by _download_file and _fetch_file.
    Verify a checksum that has already been normalized.

    Args:
        path: The path to the file to verify.
        expected_checksum: Tuple of (algorithm, checksum) representing expected checksum
        chunk_size: The size of chunks to read the file in.

    Raises:
        DownloadError: If the computed checksum does not match the expected value.
    """
    # return early if there is no expected checksum to verify against
    if expected_checksum is None:
        return

    algorithm, expected = expected_checksum
    # Compute the actual checksum of the file at the given path
    actual = file_checksum(path, algorithm=algorithm, chunk_size=chunk_size)
    # raise a DownloadError if the computed checksum does not match the expected value
    if actual.lower() != expected:
        raise DownloadError(f"Checksum mismatch for {path.name} " f"({algorithm})")


def _fetch_file(context, relative_path, destination, expected_checksum, chunk_size):
    """
    Download and verify a file before replacing its destination.

    Args:
        context (OperationContext): Transport and destination for this download.
        relative_path (Path): Literal path relative to the dataset root.
        destination (Path): Destination used for error reporting before
            resolving the path against the context's output root.
        expected_checksum: Optional digest or (algorithm, digest) pair.
        chunk_size (int): Bytes per read when downloading or verifying.

    Returns:
        int: Number of downloaded bytes.

    Raises:
        DownloadError: If the transfer, checksum, or destination is invalid.
    """
    validated_checksum = _validate_checksum_spec(expected_checksum)
    try:
        context.check_cancelled()
        destination = resolve_contained_path(
            context.output_root, relative_path, label="download destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with atomic_output_path(destination, suffix=".part") as temp_path:
            context.transport.fetch(relative_path.as_posix(), temp_path,
                                    chunk_size=chunk_size)
            _verify_validated_checksum(temp_path, validated_checksum, chunk_size)
            size = temp_path.stat().st_size
            # Recheck for symlink changes before replacing the destination.
            # Another process can still change the path after this check.
            resolve_contained_path(context.output_root, relative_path,
                                   label="download destination")
            context.check_cancelled()
        return size
    except (OSError, ValueError) as exc:
        raise DownloadError(f"Failed to write {destination.name}: {exc}") from None


def _download_file(
    url: str,
    destination: Path,
    expected_checksum: Optional[ChecksumSpec] = None,
    chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    ) -> int:
    """
    Download one URL and verify its checksum before replacing the destination.

    This compatibility helper creates its own transport context.

    Args:
        url (str): Full URL of the remote file.
        destination (Path): Local path, which may use a different filename.
        expected_checksum: Optional digest or (algorithm, digest) pair.
        chunk_size (int): Bytes per read. Defaults to DOWNLOAD_CHUNK_SIZE.

    Returns:
        int: Number of downloaded bytes.

    Raises:
        DownloadError: If the URL, transfer, checksum, or destination is invalid.
    """
    chunk_size = _require_positive_integer(chunk_size, label="chunk_size")
    _validate_checksum_spec(expected_checksum)
    RemoteSpec.parse(url)
    parsed = urlsplit(url)
    base = urlunsplit(parsed._replace(path=parsed.path.rsplit("/", 1)[0] + "/"))
    remote = RemoteSpec.parse(base)
    relative = literal_path(unquote(parsed.path.rsplit("/", 1)[-1]))
    destination = Path(destination).absolute()
    # Direct callers may intentionally choose a different local filename.
    with OperationContext(remote, destination.parent) as context:
        if remote.scheme in {"http", "https"}:
            context._local.session = requests
            context.transport.direct_url = url
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            resolve_contained_path(destination.parent, destination.name)
            with atomic_output_path(destination, suffix=".part") as temporary:
                context.transport.fetch(relative.as_posix(), temporary,
                                        chunk_size=chunk_size)
                _verify_validated_checksum(
                    temporary, _validate_checksum_spec(expected_checksum), chunk_size)
                size = temporary.stat().st_size
                resolve_contained_path(destination.parent, destination.name)
                context.check_cancelled()
            return size
        except OSError as exc:
            raise DownloadError(f"Failed to write {destination}: {exc}") from None


def _select_download_items(
    repo,
    file_paths: Sequence[str] = (),
    tsv_names: Sequence[str] = (),
    all_files: bool = False,
    catalog_only: bool = False,
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
    explicit_paths = {_safe_remote_path(path).as_posix() for path in file_paths}

    def add_file(
            value: Union[str, Path],
            expected_checksum: Optional[ChecksumSpec] = None,
            row=None,
            ) -> None:
        """Add a file and its catalog metadata, checking for conflicting records."""
        # Resolve the input value to a safe relative path
        relative_path = _safe_remote_path(value)
        # Merge the selected file into the dictionary of selected files, ensuring no
        # conflicting checksums exist.
        _merge_selected_file(selected, relative_path, expected_checksum)
        if row is not None:
            size = _catalog_size(row.get("size_bytes"))
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
            relative_path = _safe_remote_path(_resolve_remote_path(row, fmt_entries))
            if not explicit_only or relative_path.as_posix() in explicit_paths:
                add_file(relative_path, _row_checksum(row), row)

    # Add explicitly requested file paths to the selected files.
    if not catalog_only:
        for file_path in file_paths:
            add_file(file_path)
    # Organize data configuration entries by their TSV names for easier access.
    entries_by_tsv: dict[str, list[dict]] = {}
    for entry in data_config:
        # Path catalogs need no parameterized filename format.
        if not entry.get("db"):
            continue
        # download_tsv_name will normalize and validate the TSV name
        tsv_name = _download_tsv_name(entry["db"])
        # Add the entry to the list of entries for the corresponding TSV name
        entries_by_tsv.setdefault(tsv_name, []).append(entry)


    requested_tsvs = []
    seen_tsvs = set()
    for name in tsv_names:
        # download_tsv_name will normalize and validate the TSV name
        tsv_name = _download_tsv_name(name)
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
                if file_path and (all_files or _safe_remote_path(file_path).as_posix()
                                  in explicit_paths):
                    add_file(file_path, _entry_checksum(entry), entry)

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

    missing = explicit_paths - set(selected)
    if missing:
        raise DownloadError(
            "Paths are not in the catalog: " + ", ".join(sorted(missing)))

    return [DownloadItem(path, checksum, *metadata.get(path.as_posix(), (None, None)))
            for path, checksum in selected.values()]


def _catalog_size(value) -> Optional[int]:
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


def select_download_files(
    repo,
    file_paths: Sequence[str] = (),
    tsv_names: Sequence[str] = (),
    all_files: bool = False,
) -> list[tuple[Path, Optional[ChecksumSpec]]]:
    """
    Select remote paths and their available catalog checksums.

    Args:
        repo: The hallmark repository object.
        file_paths (sequence[str]): Explicit remote-relative paths.
        tsv_names (sequence[str]): Catalog TSVs to select.
        all_files (bool): Include all configured files. Defaults to False.

    Returns:
        list[tuple]: Pairs of relative paths and optional checksums.
        Explicit paths use catalog checksums when available.

    Raises:
        DownloadError: If catalog paths or checksum records are invalid.
    """
    return [(item.relative_path, item.checksum) for item in _select_download_items(
        repo, file_paths=file_paths, tsv_names=tsv_names, all_files=all_files)]


def plan_download(
    repo,
    output_path: Optional[Union[Path, str]] = None,
    *,
    file_paths: Optional[Sequence[str]] = None,
    tsv_names: Optional[Sequence[str]] = None,
    all_files: bool = False,
    filter: Optional[Union[str, Sequence[str]]] = None,
    remote_name: Optional[str] = None,
    estimated_bytes_per_second: Optional[float] = None,
) -> DownloadPlan:
    """
    Plan a download using local catalog metadata.

    No network requests are made. With no explicit paths or TSVs, select
    the complete catalog before applying filters. Missing sizes remain unknown.

    Args:
        repo: The hallmark repository object.
        output_path (Path | str, optional): Destination directory. Defaults
            to the worktree; required for a bare repository.
        file_paths (sequence[str], optional): Remote-relative file paths.
        tsv_names (sequence[str], optional): Catalog TSVs to select.
        all_files (bool): Select all configured files. Cannot be combined
            with explicit paths or TSVs. Defaults to False.
        filter (str | list[str], optional): Relative path glob or globs.
        remote_name (str, optional): Configured data remote to use.
        estimated_bytes_per_second (float, optional): Positive rate used
            to estimate duration when every file size is known.

    Returns:
        DownloadPlan: Immutable files, source, destination, and metadata
        for review before approval.

    Raises:
        DownloadError: If the catalog, remote, selection, or destination
            is invalid.
        ValueError: If an inclusion pattern or supplied rate is invalid.
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
        all_files=all_files or (not file_paths and not tsv_names), catalog_only=True)
    if filter is not None:
        from .discovery import path_matches
        path_matches("validation", filter=filter)
        items = [item for item in items if path_matches(
            item.relative_path.as_posix(), filter=filter)]
    remote = _select_remote_config(repo, remote_name)
    if items and (remote is None or not remote.get("url")):
        raise DownloadError("No remote URL is configured in config.yml")
    remote = remote or {}
    source = None
    if remote.get("url"):
        source = RemoteSpec.parse(
            remote["url"], backend=remote.get("backend"),
            backend_options=remote.get("backend_options"))
    for item in items:
        try:
            resolve_contained_path(output_root, item.relative_path,
                                   label="download destination")
        except ValueError as exc:
            raise DownloadError(str(exc)) from exc
    return DownloadPlan(
        tuple(items), remote.get("url"), output_root,
        remote_name=remote.get("name"),
        estimated_bytes_per_second=estimated_bytes_per_second,
        remote_backend=source.backend if source is not None else None,
        backend_options=remote.get("backend_options"))


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
    if plan.items and approved is not True:
        raise DownloadError("Dataset downloads require explicit approval")
    max_workers = _require_positive_integer(max_workers, label="max_workers")
    if not plan.items:
        return {"succeeded": 0, "failed": 0, "total_bytes": 0, "errors": []}
    if plan.output_path.resolve() != plan.output_path:
        raise DownloadError("Download destination changed since planning")
    remote = RemoteSpec.parse(
        plan.remote_url, backend=plan.remote_backend,
        backend_options=plan.backend_options)
    return _download_selected(
        remote, plan.output_path,
        [(item.relative_path, item.checksum) for item in plan.items],
        max_workers=max_workers, show_progress=show_progress,
        byte_progress=True, total_bytes=plan.total_bytes)


def download_remote_data(
    repo,
    worktree_path: Path,
    max_workers: int = 4,
    show_progress: bool = False,
    selected_files: Optional[Sequence[tuple[Path, Optional[ChecksumSpec]]]] = None,
    remote_name: Optional[str] = None,
    *,
    approved: bool = False,
    ) -> dict:
    """
    Download remote data files for a hallmark repository.

    Args:
        repo: The hallmark repository object.
        worktree_path (Path): Directory where files will be downloaded.
        max_workers (int): Maximum concurrent download workers. Defaults to 4.
        show_progress (bool): Show progress by file count. Defaults to False.
        selected_files (sequence[tuple], optional): Relative paths paired
            with optional checksums. If omitted, use ``select_download_files``.
        remote_name (str, optional): Configured data remote to use.
        approved (bool): Must be True for a nonempty transfer. Defaults to False.

    Returns:
        dict: Results with ``succeeded``, ``failed``, ``total_bytes``, and
        ``errors`` keys. Individual transfer failures are recorded here;
        successful downloads remain available.

    Raises:
        DownloadError: If approval is missing, configuration is invalid,
            or connection setup fails before transfers begin.
    """
    # Ensure that max_workers is a positive integer for concurrent downloads.
    max_workers = _require_positive_integer(max_workers, label="max_workers")
    # Initialize the results dictionary to track download statistics and errors.
    results = {"succeeded": 0, "failed": 0, "total_bytes": 0, "errors": [],}

    # If no specific files are selected for download
    if selected_files is None:
        # determine which files to download based on the repository's configuration
        selected_files = select_download_files(repo)

    # Select the appropriate remote configuration based on the provided remote name.
    remote_config = _select_remote_config(repo, remote_name=remote_name)
    # if no remote configuration and there are selected files, raise a DownloadError
    if remote_config is None:
        if selected_files:
            raise DownloadError("No remote is configured in config.yml")
        # return the results dictionary with zero downloads if no remote is configured
        return results

    # try to normalize and validate the remote URL from the selected remote config
    try:
        remote_url = normalize_nonempty_string(
            remote_config.get("url"),
            label="Remote URL",
            exception_type=DownloadError)
    # raise a DownloadError if the remote URL is not configured or invalid
    except DownloadError as exc:
        raise DownloadError("Remote URL not configured in config.yml") from exc
    # if the remote URL is not configured, raise a DownloadError to indicate the issue
    if not remote_url:
        raise DownloadError("Remote URL not configured in config.yml")
    remote_spec = RemoteSpec.parse(
        remote_url, backend=remote_config.get("backend"),
        backend_options=remote_config.get("backend_options"))
    # If there are still no files selected for download
    if not selected_files:
        # return the results without attempting any downloads
        return results

    if approved is not True:
        raise DownloadError("Dataset downloads require explicit approval")
    return _download_selected(
        remote_spec, worktree_path, selected_files,
        max_workers=max_workers, show_progress=show_progress)


def _download_selected(
    remote_spec,
    worktree_path,
    selected_files,
    *,
    max_workers,
    show_progress,
    byte_progress=False,
    total_bytes=None,
):
    """Download selected files with shared resources and atomic file replacement."""
    results = {"succeeded": 0, "failed": 0, "total_bytes": 0, "errors": []}

    # output_root is the resolved absolute path where files will be downloaded
    output_root = Path(worktree_path).expanduser().resolve()
    # if the output root exists and is not a directory, raise a DownloadError
    if output_root.exists() and not output_root.is_dir():
        raise DownloadError(
            f"Download output is not a directory: {output_root}")

    # Normalize selected files into a dictionary to ensure unique paths and checksums.
    normalized_selection: dict[str, tuple[Path, Optional[ChecksumSpec]]] = {}
    for selection in selected_files:
        # raise a DownloadError if the selection is not a tuple or list of length 2
        if (not isinstance(selection, (tuple, list)) or len(selection) != 2):
            raise DownloadError("selected file entries must be (path, checksum) pairs")

        relative_path, expected_checksum = selection
        # Validate and convert the relative path to a safe Path object
        relative_path = _safe_remote_path(relative_path)
        # Merge the selected file into the normalized selection,
        # ensuring no conflicting checksums exist.
        _merge_selected_file(normalized_selection, relative_path, expected_checksum)

    files_to_download = []
    # for each unique relative path and expected checksum in the normalized selection
    for relative_path, expected_checksum in (normalized_selection.values()):
        # try to resolve the destination path for the download,
        # ensuring it is contained within the output root
        try:
            destination = resolve_contained_path(
                output_root,
                relative_path,
                label="download destination")
        # Handle exceptions raised by resolve_contained_path and raise a DownloadError
        except ValueError as exc:
            raise DownloadError(str(exc)) from exc

        files_to_download.append((relative_path, destination, expected_checksum))

    # track byte totals for plans and file counts for the legacy API
    progress = tqdm(
        total=total_bytes if byte_progress else len(files_to_download),
        unit="B" if byte_progress else "file",
        unit_scale=byte_progress,
        disable=not show_progress,)
    progress_lock = Lock()

    def advance_bytes(count):
        # Transport callbacks run concurrently, one for each active worker.
        with progress_lock:
            progress.update(count)

    # try to download the files using a thread pool executor for concurrent downloads
    try:
        with OperationContext(remote_spec, output_root) as context:
            if byte_progress:
                context.on_bytes = advance_bytes
            context.transport.prepare()
            executor = ThreadPoolExecutor(
                max_workers=min(max_workers, context.settings.max_sessions)
                if remote_spec.scheme in {"ssh", "sftp"} else max_workers)
            try:
                # create an iterator over the files to download
                file_iterator = iter(files_to_download)
                # initialize a set to keep track of pending download futures
                pending = set()
                # Bound queued work as well as running workers.
                window_size = max_workers * 2

                def submit_next() -> bool:
                    """
                    Submit the next file for download if available.
                    Returns:
                        bool: True if a file was submitted,
                        False if no more files are available.
                    """
                    # try to get the next file from the iterator
                    try:
                        relative_path, destination, checksum = next(file_iterator)
                    # Handle the exception when there are no more files to download
                    except StopIteration:
                        return False
                    # add the download task to the pending set using the executor
                    pending.add(executor.submit(
                        _fetch_file, context, relative_path, destination, checksum,
                        DOWNLOAD_CHUNK_SIZE))
                    # if the download was successfully submitted, return True
                    return True

                # for each file in the initial window size, submit it for download
                for _ in range(min(window_size, len(files_to_download))):
                    submit_next()
                while pending:
                    # wait for at least one of the pending download tasks to complete
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    # Aggregate per-file failures and keep successful downloads.
                    for future in completed:
                        pending.remove(future)
                        # try to get the result of the completed download task
                        try:
                            results["total_bytes"] += future.result()
                            results["succeeded"] += 1
                        # Handle exceptions raised during the download
                        except DownloadError as exc:
                            results["failed"] += 1
                            results["errors"].append(str(exc))
                        # report completed files even when a transfer fails
                        finally:
                            if byte_progress:
                                with progress_lock:
                                    finished = results["succeeded"] + results["failed"]
                                    progress.set_postfix(
                                        files=f"{finished}/{len(files_to_download)}",
                                        failed=results["failed"])
                            else:
                                progress.update(1)
                        # submit the next file for download if available
                        submit_next()
            except BaseException:
                context.cancel()
                for future in pending:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
    # close after all downloads are complete, regardless of success or failure
    finally:
        progress.close()

    return results
