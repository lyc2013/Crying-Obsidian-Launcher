"""Offline accounts: validate a username and derive its Minecraft UUID.

Minecraft derives offline UUIDs the same way Java's UUID.nameUUIDFromBytes does:
an MD5 hash of "OfflinePlayer:<username>" with the version/variant bits set.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

# Vanilla accounts are 3-16 characters, letters/digits/underscore only.
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")


class AuthError(Exception):
    """Raised when the offline account input is invalid."""


@dataclass(frozen=True)
class OfflineAccount:
    username: str
    uuid: str
    access_token: str = ""

    def to_launch_options(self) -> dict[str, str]:
        """Values expected by minecraft-launcher-lib's launch options."""
        return {
            "username": self.username,
            "uuid": self.uuid,
            "token": self.access_token,
        }


def validate_username(username: str) -> str:
    name = username.strip()
    if not name:
        raise AuthError("username must not be empty")
    if not USERNAME_PATTERN.match(name):
        raise AuthError(
            "username must be 3-16 characters using only letters, digits and underscore"
        )
    return name


def offline_uuid(username: str) -> str:
    """Return the UUID Minecraft uses for this name in offline mode."""
    digest = hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest, version=3))


def offline_account(username: str) -> OfflineAccount:
    name = validate_username(username)
    return OfflineAccount(username=name, uuid=offline_uuid(name))
