"""Secrets come from the macOS Keychain, and nowhere else.

Every secret is a generic-password item with service "talos" and an account name
that says what it is, e.g. "gmail:owner@gmail.com". The owner stores
passwords themselves with

    security add-generic-password -s talos -a <key> -w

so a password never passes through a shell history, a file or an assistant. Talos
writes only one kind of item itself: the Microsoft sign-in token cache.
"""

from __future__ import annotations

import keyring

SERVICE = "talos"


class MissingSecret(RuntimeError):
    pass


def get(key: str) -> str:
    value = keyring.get_password(SERVICE, key)
    if not value:
        raise MissingSecret(
            f"No Keychain item for {key!r}. Store it with:\n"
            f"    security add-generic-password -s {SERVICE} -a {key} -w")
    return value


def get_optional(key: str) -> str | None:
    return keyring.get_password(SERVICE, key)


def exists(key: str) -> bool:
    """Whether the item is there, without reading the secret, so macOS shows no access dialog."""
    import subprocess
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", key],
                       capture_output=True, text=True)
    return r.returncode == 0


def put(key: str, value: str) -> None:
    keyring.set_password(SERVICE, key, value)
