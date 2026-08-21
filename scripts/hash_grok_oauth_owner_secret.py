#!/usr/bin/env python3
"""Hash a Grok OAuth owner secret with Argon2id.

The plaintext secret is never accepted as a command-line argument and is never
written to disk. Store the original secret in a password manager. Configure
only the printed hash as GROK_MCP_OAUTH_OWNER_SECRET_HASH.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

# `uv run python scripts/hash_grok_oauth_owner_secret.py` puts `scripts/` on
# sys.path[0], not the repo root. Always load the local `app` package.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from argon2 import PasswordHasher  # noqa: E402

from app.grok_oauth.constants import OWNER_SECRET_MIN_LENGTH  # noqa: E402
from app.grok_oauth.crypto import hash_owner_secret, is_argon2id_hash  # noqa: E402


def main() -> int:
    print("Enter the owner authorization secret twice.")
    print("Keep the original secret in a password manager. This script prints only the hash.")
    first = getpass.getpass("Owner secret: ")
    second = getpass.getpass("Confirm owner secret: ")
    if first != second:
        print("error: secrets did not match", file=sys.stderr)
        return 1
    if len(first) < OWNER_SECRET_MIN_LENGTH:
        print("error: input does not meet the minimum length", file=sys.stderr)
        return 1
    try:
        hashed = hash_owner_secret(first)
    except ValueError:
        print("error: input could not be hashed", file=sys.stderr)
        return 1
    if not is_argon2id_hash(hashed):
        print("error: hasher did not produce an Argon2id hash", file=sys.stderr)
        return 1
    # Prove the hash verifies without printing the secret.
    PasswordHasher().verify(hashed, first)
    print(hashed)
    print()
    print("Set GROK_MCP_OAUTH_OWNER_SECRET_HASH to the hash above.")
    print("Do not store the plaintext secret on the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
