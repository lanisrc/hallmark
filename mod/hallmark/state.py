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


from dataclasses import dataclass, field

import pandas as pd

# Define the default columns for the state DataFrame
COLUMNS = ["sha1"]
# The catalog table of the first data entry; other entries name their own TSV.
DEFAULT_DB = "data.tsv"
# Remote file metadata columns. They describe a row but do not identify it.
METADATA_COLUMNS = ("checksum_algorithm", "checksum", "size_bytes", "mtime")


def _normalized_state_data(frame: pd.DataFrame) -> pd.DataFrame:
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
    # Identify the parameter columns by excluding "sha1" and "path"
    parameter_columns = [
        column for column in frame.columns if column not in {"sha1", "path"}]
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
    # remote rows without a published SHA-1 store an empty value, not NaN
    normalized["sha1"] = normalized["sha1"].map(
        lambda value: "" if pd.isna(value) else value)
    for column in parameter_columns:
        # normalize non-checksum columns to string type,
        # replacing NaN values with empty strings
        normalized[column] = normalized[column].map(
            lambda value: (""if pd.isna(value) else str(value)))

    return normalized


@dataclass
class State:
    """
    In-memory Hallmark state database.

    Attributes:
        config: Repository configuration values.
        meta: Repository metadata.
        data: Tabular file index of the default catalog (``data.tsv``),
            containing indexed object checksums (``sha1``) and associated
            metadata.
        tables: Catalog tables of additional data entries, keyed by TSV name.
        changed_tables: Additional tables modified since they were last written.
        removed_tables: Additional tables to delete when the state is written.
    """

    config:    dict         = field(default_factory=dict)
    meta:      dict         = field(default_factory=dict)
    data:      pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=COLUMNS)
    )
    tables:    dict         = field(default_factory=dict)
    changed_tables: set     = field(default_factory=set, repr=False, compare=False)
    removed_tables: set     = field(default_factory=set, repr=False, compare=False)

    def table(self, db: str = DEFAULT_DB) -> pd.DataFrame:
        """
        Return a catalog table, or an empty table if it has no rows yet.

        Args:
            db (str): TSV name of the table. Defaults to ``data.tsv``.

        Returns:
            pd.DataFrame: The catalog table.
        """
        if db == DEFAULT_DB:
            return self.data
        frame = self.tables.get(db)
        return frame if frame is not None else pd.DataFrame(columns=COLUMNS)

    def set_table(self, db: str, frame: pd.DataFrame) -> None:
        """
        Replace a catalog table and mark it for writing.

        Args:
            db (str): TSV name of the table.
            frame (pd.DataFrame): The new table contents.
        """
        if db == DEFAULT_DB:
            self.data = frame
            return
        self.tables[db] = frame
        self.changed_tables.add(db)
        self.removed_tables.discard(db)

    def drop_table(self, db: str) -> None:
        """
        Remove a catalog table. ``data.tsv`` is emptied rather than deleted,
        because every Hallmark repository contains it.

        Args:
            db (str): TSV name of the table.
        """
        if db == DEFAULT_DB:
            self.data = pd.DataFrame(columns=COLUMNS)
            return
        self.tables.pop(db, None)
        self.changed_tables.discard(db)
        self.removed_tables.add(db)

    def update(self, pf, db: str = DEFAULT_DB) -> None:
        """
        Merge ``ParaFrame`` rows into a catalog table.

        Existing rows with matching keys are updated, while new rows are
        appended. Checksums and remote file metadata are not part of the key.

        Args:
            pf (ParaFrame): ``ParaFrame`` containing rows to add or update.
            db (str): TSV name of the table. Defaults to ``data.tsv``.

        Returns:
            None.
        """
        current = self.table(db)
        if pf.empty:
             # create empty DataFrame with the same columns as the existing table.
             columns = (current.columns if len(current.columns) else COLUMNS)
             incoming = pd.DataFrame(columns=columns)
        # if the provided ParaFrame is not empty, normalize its data
        else:
            incoming = _normalized_state_data(pf)
        # Merge the incoming rows with the existing state.
        merged = pd.concat([current, incoming], ignore_index=True, sort=False)

        value_columns = [column for column in merged.columns if column != "sha1"]
        key_columns = [column for column in value_columns
                       if column not in METADATA_COLUMNS]
        if key_columns:
            # Remove duplicate entries while keeping the most recent row.
            deduped = merged.drop_duplicates(subset=key_columns, keep="last")
        else:
            # If there are no key columns, keep only the last row.
            deduped = merged.tail(1)

        # reset the index of the deduplicated DataFrame
        # retain the "sha1" column followed by the other columns.
        self.set_table(
            db, deduped.loc[:, ["sha1", *value_columns]].reset_index(drop=True))

    def replace(self, pf, db: str = DEFAULT_DB) -> None:
        """
        Replace the contents of a catalog table.

        Existing rows are discarded and replaced with the rows from the
        provided ``ParaFrame``.

        Args:
            pf (ParaFrame): ``ParaFrame`` containing the replacement rows.
            db (str): TSV name of the table. Defaults to ``data.tsv``.

        Returns:
            None.
        """
        # call the normalization function to ensure consistent data types and structure
        self.set_table(db, _normalized_state_data(pf))
