"""
Resolve secrets that local authentication profiles refer to.

A profile never holds a secret value. It names where the value comes from:
an environment variable, an entry in the operating system keyring, or a
helper command (such as a password manager) that prints it. Values are read
only when a request needs them and are wrapped so that ``repr``, ``str`` and
error messages never show them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .base import CapabilityError, RemoteConfigurationError

COMMAND_TIMEOUT = 30
COMMAND_OUTPUT_LIMIT = 64 * 1024
SECRET_SOURCES = ("env", "keyring", "command")


class Secret:
    """A secret value that never appears in ``repr`` or ``str``."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        """Return the secret for use in a request."""
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class SecretRef:
    """
    Where a profile's secret comes from.

    Attributes:
        profile (str): Profile name, for messages.
        field (str): Profile field, for messages.
        source (str): ``env``, ``keyring`` or ``command``.
        target (tuple[str, ...]): Variable name, keyring service and user
            name, or command arguments.
    """

    profile: str
    field: str
    source: str
    target: tuple

    @classmethod
    def parse(cls, value, *, profile: str, field: str) -> SecretRef:
        """
        Validate a secret reference from an auth profile.

        Args:
            value (dict): ``{"env": NAME}``, ``{"keyring": {"service": S,
                "username": U}}`` or ``{"command": [PROGRAM, ARG, ...]}``.
            profile (str): Profile name, for messages.
            field (str): Profile field, for messages.

        Returns:
            SecretRef: The validated reference.

        Raises:
            RemoteConfigurationError: If the value is a literal secret or an
                unsupported reference. The message never includes the value.
        """
        where = f"Auth profile {profile!r} field {field!r}"
        if not isinstance(value, dict) or len(value) != 1:
            raise RemoteConfigurationError(
                f"{where} must name where its secret comes from, for example "
                "{env: VARIABLE}; secret values cannot be stored in auth.yml")
        (source, target), = value.items()
        if source == "env" and isinstance(target, str) and target:
            return cls(profile, field, source, (target,))
        if (source == "keyring" and isinstance(target, dict)
                and set(target) == {"service", "username"}
                and all(isinstance(item, str) and item for item in target.values())):
            return cls(profile, field, source,
                       (target["service"], target["username"]))
        if (source == "command" and isinstance(target, list) and target
                and all(isinstance(item, str) and item for item in target)):
            return cls(profile, field, source, tuple(target))
        raise RemoteConfigurationError(
            f"{where} must use one of: " + ", ".join(SECRET_SOURCES))

    def resolve(self) -> Secret:
        """
        Read the secret.

        Returns:
            Secret: The value, hidden from ``repr`` and ``str``; call ``reveal``
            to use it.

        Raises:
            RemoteConfigurationError: If the secret is unavailable.
            CapabilityError: If the keyring package is not installed.
        """
        where = f"auth profile {self.profile!r} field {self.field!r}"
        if self.source == "env":
            value = os.environ.get(self.target[0])
            if not value:
                raise RemoteConfigurationError(
                    f"Environment variable {self.target[0]} for {where} is not set")
            return Secret(value)
        if self.source == "keyring":
            try:
                import keyring
            except ImportError:
                raise CapabilityError(
                    "Keyring secrets need the keyring package; "
                    "install hallmark[keyring]") from None
            try:
                value = keyring.get_password(*self.target)
            except Exception:
                raise RemoteConfigurationError(
                    f"The system keyring could not provide {where}") from None
            if not value:
                raise RemoteConfigurationError(
                    f"The system keyring has no entry for {where}")
            return Secret(value)
        return Secret(self._run_command(where))

    def _run_command(self, where: str) -> str:
        program = shutil.which(self.target[0])
        if program is None:
            raise RemoteConfigurationError(
                f"Secret command {self.target[0]!r} for {where} was not found")
        try:
            # stderr may echo the secret, so it is discarded rather than reported.
            completed = subprocess.run(
                [program, *self.target[1:]], shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=COMMAND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            raise RemoteConfigurationError(
                f"Secret command for {where} did not finish") from None
        output = completed.stdout
        if completed.returncode != 0:
            raise RemoteConfigurationError(f"Secret command for {where} failed")
        if len(output) > COMMAND_OUTPUT_LIMIT:
            raise RemoteConfigurationError(
                f"Secret command for {where} printed too much")
        try:
            lines = output.decode("utf-8").rstrip("\r\n").splitlines()
        except UnicodeError:
            raise RemoteConfigurationError(
                f"Secret command for {where} must print UTF-8 text") from None
        if len(lines) != 1 or not lines[0]:
            raise RemoteConfigurationError(
                f"Secret command for {where} must print one line")
        return lines[0]
