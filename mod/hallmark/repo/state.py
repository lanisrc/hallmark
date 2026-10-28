# Copyright 2025 the Hallmark Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Hold a repository's catalogue in memory.

``State`` keeps ``config.yml``, ``meta.yml``, ``data.tsv`` and the other
TSVs that ``config.yml`` names. The helpers here give catalogue values the
text form they are saved in, and tell remote catalogues from local ones.
"""

from dataclasses import dataclass, field

import pandas as pd

from ..utils import string_or_none

# Define the default columns for the state DataFrame
COLUMNS = ["sha1"]
# TSV that holds the rows of a lone data entry without a "db" name
DEFAULT_DB = "data.tsv"


def cell_text(value) -> str:
    """
    Return a catalogue value as the text it is saved and loaded as.

    Missing values become ``""`` and whole-number floats lose their ``.0``,
    which pandas adds when a numeric column has gaps.
    """
    text = string_or_none(value)
    return "" if text is None else text


def normalize_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Return catalogue rows in their one saved form: text, sorted by path.

    Every writer goes through this, so saving rows that were just loaded
    changes nothing.

    Args:
        frame (pd.DataFrame): Catalogue rows.

    Returns:
        pd.DataFrame: A copy with text values, sorted by ``path`` if present.
    """
    rows = frame.copy()
    for column in rows.columns:
        rows[column] = rows[column].map(cell_text).astype(object)
    if "path" in rows.columns:
        rows = rows.sort_values("path", kind="stable")
    return rows.reset_index(drop=True)


def is_remote_catalog(state) -> bool:
    """
    Return whether a state catalogs files on a data server.

    A remote catalogue records its ``sources`` in ``config.yml``; its rows'
    ``sha1``, when known, describes downloaded content rather than a
    committed object. Catalogues written before sources existed are
    recognized by a ``path`` column without ``sha1``, and are given their
    sources before a ``sha1`` column is ever added. Column names alone never
    decide: a local filename pattern may have a ``{source}`` field.

    Args:
        state (State): Repository state to classify.
    """
    config = state.config if isinstance(state.config, dict) else {}
    if isinstance(config.get("sources"), list):
        return True
    return any("path" in frame.columns and "sha1" not in frame.columns
               for frame in [state.data, *getattr(state, "tables", {}).values()])


def _prepare_file_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Used by update and replace.
    Normalize the state data by retaining only the relevant columns and ensuring
    that all non-checksum columns are of string type.

    Args:
        frame (pd.DataFrame): The input DataFrame to normalize.

    Returns:
        pd.DataFrame: The normalized DataFrame.

    Raises:
        ValueError: If the "sha1" column is missing from the provided DataFrame.
    """
    # Identify the parameter columns by excluding "sha1"
    parameter_columns = [
        column for column in frame.columns if column != "sha1"]
    # collect the relevant columns for normalization
    columns = ["sha1", *parameter_columns]
    # if provided DataFrame is empty, return an empty DataFrame with the columns
    if frame.empty:
        return pd.DataFrame(columns=columns)
    # raise an error if the "sha1" column is missing from the provided DataFrame
    if "sha1" not in frame.columns:
        raise ValueError('state data must contain a "sha1" column')

    # normalize the data by retaining only the relevant columns
    normalized = frame.loc[:, columns].copy()
    for column in parameter_columns:
        normalized[column] = normalized[column].map(cell_text)

    return normalized


@dataclass
class State:
    """
    In-memory Hallmark state database.

    Attributes:
        config (dict): Repository configuration values, from ``config.yml``.
        meta (dict): Repository metadata, from ``meta.yml``.
        data (pd.DataFrame): Rows of ``data.tsv``: indexed object checksums
            (``sha1``) and associated metadata, or a remote catalogue's files.
        tables (dict): The other TSVs that ``config.yml``'s ``data`` entries
            name, by file name, such as one per filename format of a remote
            catalogue.
    """

    config:    dict         = field(default_factory=dict)
    meta:      dict         = field(default_factory=dict)
    data:      pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=COLUMNS)
    )
    tables:    dict         = field(default_factory=dict)

    def update(self, pf) -> None:
        """
        Merge ``ParaFrame`` rows into the state database.

        Existing rows with matching keys are updated, while new rows are
        appended.

        Args:
            pf (ParaFrame): ``ParaFrame`` containing rows to add or update.

        Returns:
            None.
        """
        if pf.empty:
             # create empty DataFrame with the same columns as the existing state data.
             columns = (self.data.columns if len(self.data.columns) else COLUMNS)
             incoming = pd.DataFrame(columns=columns)
        # if the provided ParaFrame is not empty, normalize its data
        else:
            incoming = _prepare_file_rows(pf)
        # Merge the incoming rows with the existing state.
        merged = pd.concat([self.data, incoming], ignore_index=True, sort=False)

        # data columns are all columns except "sha1"
        data_columns = [column for column in merged.columns if column != "sha1"]
        # key columns are "path" if it exists, otherwise all data columns
        key_columns = ["path"] if "path" in merged.columns else data_columns
        # deduplicate the merged DataFrame based on the key columns,
        # keeping the last occurrence of each unique key combination
        deduped = (merged.drop_duplicates(subset=key_columns, keep="last")
                   if key_columns else merged.tail(1))
        # update the state data with the deduplicated and reindexed DataFrame
        self.data = deduped.loc[:, ["sha1", *data_columns]].reset_index(drop=True)

    def replace(self, pf) -> None:
        """
        Replace the contents of the state database.

        Existing rows are discarded and replaced with the rows from the
        provided ``ParaFrame``.

        Args:
            pf (ParaFrame): ``ParaFrame`` containing the replacement rows.

        Returns:
            None.
        """
        # call the normalization function to ensure consistent data types and structure
        self.data = _prepare_file_rows(pf)
