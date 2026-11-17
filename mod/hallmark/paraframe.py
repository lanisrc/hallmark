# Copyright 2019 Chi-kwan Chan
# Copyright 2019 Steward Observatory
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Utilities for discovering and parsing parameterized file collections.

This module defines :class:`ParaFrame`, a subclass of
:class:`pandas.DataFrame` that provides convenient methods for locating,
parsing, and filtering files whose names follow parameterized naming
conventions.
"""

import re
import parse
from collections.abc import Iterator
from glob import escape as glob_escape, glob
from pathlib import Path
from string import Formatter

import pandas as pd

from .utils import (
    find_encoding_for_format,
    apply_regex_replacement,
    literal_format,
    try_numeric_conversion,
    validate_format_fields)


def _reusable(encodings):
    """Return encodings as a list if it is a one-shot iterator, else unchanged."""
    return list(encodings) if isinstance(encodings, Iterator) else encodings


def _named_fields(fmt):
    """Return the named fields of fmt in order, once each; {} and {0} are skipped."""
    names = []
    for _, name, _, _ in Formatter().parse(fmt):
        if name and not name.isdigit() and name not in names:
            names.append(name)
    return names


class ParaFrame(pd.DataFrame):
    """
    A subclass of :class:`pandas.DataFrame` with additional methods for
    parameterized file discovery and filtering.

    ``ParaFrame`` behaves like a standard ``pandas.DataFrame`` while
    providing the following additional functionality:

    * ``__init__``: Initializes the class and stores ``encodings`` and
      ``base_path`` as metadata.
    * ``_constructor``: Returns a new ``ParaFrame`` while preserving
      ``encodings`` and ``base_path``.
    * ``glob_search``: Finds files matching a parameterized format string.
    * ``parse``: Builds a ``ParaFrame`` by parsing file paths that match
      a format pattern.
    * ``__call__`` and ``filter``: Convenience methods for filtering rows
      by column values.
    """

    _metadata = ["encodings", "base_path"]

    def __init__(self, data=None, encodings=None, base_path=None, **kwargs):
        """
        Initialize a ``ParaFrame``.

        Args:
            data: Data used to initialize the ``ParaFrame``.
            encodings (dict or list of dict, optional): Filename encoding
                specifications from ``config.yml``, as a mapping or a sequence
                of mappings. An iterator is read once and stored as a list.
                Defaults to ``{}``.
            base_path (Path or str, optional): Root directory associated with
                the ``ParaFrame``. Defaults to the current working directory.
            **kwargs: Additional arguments passed to
                :class:`pandas.DataFrame`.
        """
        super().__init__(data, **kwargs)
        self.encodings = _reusable(encodings) or {}
        self.base_path = Path(base_path) if base_path is not None else Path.cwd()

    @property
    def _constructor(self):
        """
        Return the constructor used by pandas operations.

        Returns:
            callable: A constructor that preserves ``encodings`` and
            ``base_path`` metadata.
        """
        def create_paraframe(*args, **kwargs):
            kwargs.setdefault("encodings", self.encodings)
            kwargs.setdefault("base_path", self.base_path)
            return ParaFrame(*args, **kwargs)
        return create_paraframe

    def __call__(self, **kwargs):
        """
        Filter the ``ParaFrame``.

        Equivalent to calling :meth:`filter`.

        Args:
            **kwargs: Column names and values used for filtering.

        Returns:
            ParaFrame: The filtered ``ParaFrame``.
        """
        return self.filter(**kwargs)

    def filter(self, **kwargs):
        """
        Filter a ``ParaFrame`` by matching column values.

        Each keyword is one condition on a column, and the conditions are
        OR-ed: a row is kept when it satisfies any of them, so a call with no
        keywords keeps no rows. Chain calls, as in ``pf(a=0)(b=10)``, to
        require all of them.

        This replaces :meth:`pandas.DataFrame.filter`: it selects rows by
        value, not columns by label.

        Args:
            **kwargs: Column names and the values to keep. A list or tuple
                keeps the rows equal to any of its elements; any other value
                keeps the rows equal to it. Rows whose value is missing (NaN,
                None or ``<NA>``) never match a single value.

        Returns:
            ParaFrame: The rows that satisfy any of the conditions.

        Raises:
            KeyError: If a keyword is not a column.
        """
        # Create a boolean mask for filtering rows based on the specified conditions
        mask = pd.Series(False, index=self.index)
        # Iterate over the provided keyword arguments to build the mask
        for column, value in kwargs.items():
            # if the value is a sequence, use isin to check for membership
            if isinstance(value, (tuple, list)):
                condition = self[column].isin(value)
            # otherwise, use eq to check for equality
            else:
                condition = self[column].eq(value)
            # Update the mask to include rows that satisfy the current condition
            mask |= condition.fillna(False)
        # Return a new DataFrame containing only the rows that satisfy the mask
        return self[mask]

    @staticmethod
    def _find_encoding_settings(fmt, encodings, *, encoding):
        """
        Used by glob_search and Repo.add_paths.
        Resolve the YAML encoding spec that applies to fmt, when encoding=True.

        Args:
            fmt: The format string being searched with.
            encodings: Encoding specifications from config.yml (a mapping or
                sequence of mappings), or None.
            encoding: If False, no encoding lookup is performed.

        Returns:
            (yaml_encodings, fmt_enc): the matched encoding spec dict (``{}`` when
            encoding is False), and the specific fmt template used to look it up
            (fmt itself, unless a narrower entry_fmt matched within fmt).

        Raises:
            ValueError: If encodings is malformed, or no matching/valid encoding
                spec can be found for fmt.
        """
        if not encoding:
            return {}, fmt

        # if no encodings are provided, use an empty list
        if encodings is None:
            encoding_specs = []
        # if encodings is a dictionary, wrap it in a list
        elif isinstance(encodings, dict):
            encoding_specs = [encodings]
        # try to convert encodings to a list; if it fails, raise a ValueError
        else:
            try:
                encoding_specs = list(encodings)
            except TypeError as exc:
                raise ValueError(
                    "encodings must be a mapping or sequence of mappings") from exc
        # if any entry in encoding_specs is not a dictionary, raise a ValueError
        if not all(isinstance(entry, dict) for entry in encoding_specs):
            raise ValueError("encodings must contain only dictionaries")

        fmt_enc = fmt
        for entry in encoding_specs:
            entry_fmt = entry.get("fmt")
            # if entry_fmt is a non-empty string and in the provided format string
            if (isinstance(entry_fmt, str) and entry_fmt and entry_fmt in fmt):
                # use this entry's format for encoding
                fmt_enc = entry_fmt
                # break after finding the first matching entry
                break

        # find the encoding specification corresponding to fmt_enc
        yaml_encodings = find_encoding_for_format(fmt_enc, encoding_specs)
        # raise a ValueError if no encoding specification is found for fmt_enc
        if yaml_encodings is None:
            raise ValueError(
                f"Error: The format '{fmt_enc}' is missing "
                "from config.yml.")
        # get the encoding dictionary from the found specification
        enc_dict = yaml_encodings.get("encoding", {})
        # if enc_dict is not a dictionary, raise a ValueError
        if not isinstance(enc_dict, dict):
            raise ValueError(
                f"Encoding for '{fmt_enc}' must be a dictionary.")
        # check if all values in enc_dict are strings; if not, raise a ValueError
        if not all(isinstance(pattern, str) for pattern in enc_dict.values()):
            raise ValueError(f"Encoding patterns for '{fmt_enc}' must be strings.")
        # if enc_dict is empty or all values are empty strings, raise a ValueError
        if not any(pattern for pattern in enc_dict.values()):
            raise ValueError(
                f"'{fmt_enc}' has no regex spec; use encoding=False.")

        return yaml_encodings, fmt_enc

    @classmethod
    def glob_search(
        cls,
        fmt,
        *args,
        encodings=None,
        base_path=None,
        debug=False,
        return_pattern=False,
        encoding=False,
        **kwargs,
    ):
        """
        Find files matching a parameterized format string.

        This method searches for files using the supplied format string.
        When ``encoding=True``, regular-expression encodings defined in the
        YAML configuration are also applied.

        Args:
            fmt (str):
                Format string describing the expected filename pattern,
                relative to ``base_path``. A leading ``/`` is ignored.

            ``*args``:
                Positional arguments used to fill the format string.

            encodings (dict or list of dict):
                Encoding specifications from ``config.yml``, as a mapping or
                a sequence of mappings. Read only when ``encoding`` is
                ``True``.

            base_path (Path):
                Root directory to search, taken literally.
                Defaults to ``Path.cwd()``.

            debug (bool):
                If ``True``, prints debugging information.

            return_pattern (bool):
                If ``True``, returns both the glob pattern and the matched
                files.

            encoding (bool):
                If ``True``, applies regex encodings defined in the YAML
                configuration.

            ``**kwargs``:
                Keyword arguments used to fill the format string; each must
                name a field of ``fmt``. Missing values are replaced with the
                wildcard ``*``.

        Returns:
            tuple:
                If ``return_pattern`` is ``True``, returns
                ``(globbed_files, pattern)``.

                Otherwise returns
                ``(yaml_encodings, fmt_g, globbed_files)``, where ``fmt_g``
                is the format the encoding lookup settled on (``fmt`` itself
                when ``encoding`` is False) without its leading ``/`` and
                format specs, used to read the field values as the text the
                names hold.

        Raises:
            ValueError: If a field of ``fmt`` has no name or is not a plain
                name with an optional spec, if a keyword names no field of
                ``fmt``, if ``fmt`` has more unnamed fields
                than positional values, if a keyword value does not fit its
                field's format spec, or, with ``encoding=True``, if
                ``encodings`` is malformed or has no usable entry for ``fmt``.
        """
        # Determine the base path for searching files.
        # If not provided, use the current working directory.
        base_path = (Path(base_path) if base_path is not None else Path.cwd())
        validate_format_fields(fmt, positional=bool(args))

        pmax = len(fmt) // 3  # to specify a parameter, we need at least
        # three characters '{p}'; the maximum number
        # of possible parameters is `len(fmt) // 3`.

        yaml_encodings, fmt_enc = cls._find_encoding_settings(
            fmt, encodings, encoding=encoding)
        # a keyword that names no field is a mistake, not a filter
        unknown = sorted(set(kwargs) - set(_named_fields(fmt)))
        if unknown:
            raise ValueError(
                f"{fmt!r} has no field named " + ", ".join(map(repr, unknown)))

        # Only the format is filled in; the base path is literal, even when
        # its folder names contain braces or glob characters.
        pattern = fmt.lstrip("/")
        fmt_g = literal_format(fmt_enc.lstrip("/"))

        # plus one to ensure at least one iteration of the loop
        for i in range(pmax + 1):
            if debug:
                print(i, pattern, args, kwargs)
            try:
                pattern = pattern.format(*args, **kwargs)
                break
            except IndexError as exc:
                raise ValueError(
                    f"{fmt!r} has more unnamed fields than the {len(args)} "
                    "positional values given") from exc
            # if a KeyError occurs, it means a required keyword argument is missing
            except KeyError as exc:
                # get the missing key from the exception
                key = str(exc.args[0])
                # match exactly this field, with any spec, so {a} leaves {ab} alone
                field_pattern = (r"\{" + re.escape(key) + r"(?:[.[!:][^{}]*)?\}")
                replacement = f"{{{key}}}"

                # pattern is the glob pattern with missing field replaced by a wildcard
                pattern = re.sub(field_pattern, replacement, pattern)
                # set the missing keyword argument to a wildcard for globbing
                kwargs[key] = "*"

        pattern = str(Path(glob_escape(str(base_path))) / pattern)
        # glob the files using the constructed pattern and filter out directories
        globbed_files = sorted(match for match in glob(pattern)
                               if Path(match).is_file())

        # Print the glob pattern and a summary of matches
        if debug:
            print(f'Pattern: "{pattern}"')
            n = len(globbed_files)
            if n > 1:
                print(f'{n} matches, e.g., "{globbed_files[0]}"')
            elif n > 0:
                print(f'{n} match, i.e., "{globbed_files[0]}"')
            else:
                print("No match; please check format string")

        # if return_pattern is True, return the matched files and the glob pattern
        if return_pattern:
            return globbed_files, pattern
        # otherwise, return the encoding specs, the glob format, and the matched files
        else:
            return yaml_encodings, fmt_g, globbed_files

    @classmethod
    def parse(
        cls,
        fmt,
        *args,
        encodings=None,
        base_path=None,
        debug=False,
        encoding=False,
        **kwargs,
    ):
        """
        Build a ``ParaFrame`` by parsing file paths that match a format
        string.

        Args:
            fmt (str):
                Format string containing ``{parameter}`` fields, relative to
                ``base_path``. A leading ``/`` is ignored. File names must fit
                ``fmt``, including its type specs and letter case.

            ``*args``:
                Values for the unnamed fields (``{}`` or ``{0}``), in order.
                Unnamed fields do not become columns.

            encodings (dict or list of dict):
                Encoding specifications from ``config.yml``, as a mapping or
                a sequence of mappings. An iterator is read once and stored
                as a list. Defaults to ``{}``.

            base_path (Path):
                Root directory to search, taken literally.
                Defaults to ``Path.cwd()``.

            debug (bool):
                If ``True``, prints debugging information.
                Defaults to ``False``.

            encoding (bool):
                If ``True``, applies regex encoding.
                Defaults to ``False``.

            ``**kwargs``:
                Values for named fields; only files whose names hold these
                values are searched. Each keyword must name a field of
                ``fmt``.

        Returns:
            ParaFrame: One row per matched file, with a ``path`` column (the
            path relative to ``base_path``, with ``/`` separators) followed by
            one column per named field. A field's values are read as the text
            the names hold, and become numbers only when that keeps the text,
            so ``005`` stays ``005``. When no file matches, the frame is empty
            and has no columns. ``attrs["unmatched"]`` lists the relative
            paths that the glob found but that do not fit ``fmt``, including
            its type specs.

        Raises:
            ValueError: For the problems :meth:`glob_search` raises on, or if
                a format spec in ``fmt`` is not recognised.
        """
        base_path = Path(base_path) if base_path is not None else Path.cwd()
        encodings = _reusable(encodings)

        yaml_encodings, fmt_g, globbed_files = cls.glob_search(
            fmt, *args,
            encodings=encodings,
            base_path=base_path,
            debug=debug,
            encoding=encoding,
            **kwargs,
        )

        # As in hm add ., a name must fit the typed format, letter case included.
        # The spec names that format, so encodings is read only once.
        fmt_enc = yaml_encodings["fmt"] if encoding else fmt
        checker = parse.compile(fmt_enc.lstrip("/"), case_sensitive=True)
        parser = parse.compile(fmt_g, case_sensitive=True)
        frame = []
        unmatched = []

        # Writing the ParaFrame
        for f in globbed_files:
            f_short = Path(f).relative_to(base_path).as_posix()
            if encoding:
                f_new = apply_regex_replacement(f_short, yaml_encodings)
            else:
                f_new = f_short

            r = parser.parse(f_new) if checker.parse(f_new) is not None else None

            if r is None:
                unmatched.append(f_short)
                if debug:
                    print(f'Failed to parse "{f}"')
            else:
                frame.append({"path": f_short, **r.named})
        result = cls(frame, encodings=encodings, base_path=base_path)
        # attempt to convert each parameter column to numeric
        # if conversion fails the column stays as string
        for column in result.columns:
            # if the column is not "path", attempt to convert it to numeric
            if column != "path":
                result[column] = try_numeric_conversion(result[column])
        result.attrs["unmatched"] = unmatched
        return result
