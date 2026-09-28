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
        data: Tabular file index containing indexed object checksums
        (``sha1``) and associated metadata.
    """

    config:    dict         = field(default_factory=dict)
    meta:      dict         = field(default_factory=dict)
    data:      pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=COLUMNS)
    )

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
            incoming = _normalized_state_data(pf)
        # Merge the incoming rows with the existing state.
        merged = pd.concat([self.data, incoming], ignore_index=True, sort=False)

        key_columns = [column for column in merged.columns if column != "sha1"]
        if key_columns:
            # Remove duplicate entries while keeping the most recent row.
            deduped = merged.drop_duplicates(subset=key_columns, keep="last")
        else:
            # If there are no key columns, keep only the last row.
            deduped = merged.tail(1)

        # reset the index of the deduplicated DataFrame
        # retain only the "sha1" and key columns.
        self.data = (
            deduped.loc[:, ["sha1", *key_columns]].reset_index(drop=True))

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
        self.data = _normalized_state_data(pf)
