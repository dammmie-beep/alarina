from __future__ import annotations

import os

import keyring

_KEYRING_SERVICE = "alarina"
_KEYRING_USER = "github_token"


class TokenNotFoundError(RuntimeError):
    pass


def get_github_token() -> str:
    token: str | None = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    if token:
        return token
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    raise TokenNotFoundError(
        "GitHub token not found. "
        "Set GITHUB_TOKEN or run: keyring set alarina github_token"
    )
