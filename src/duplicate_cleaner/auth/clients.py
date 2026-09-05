"""Bundled OAuth client IDs and provider endpoints.

Compiled-in constants keep the zero-setup default: ``dc auth add gdrive`` works
out-of-the-box without the user registering their own OAuth application.  The
BYO override reads a Google Cloud Console-downloaded ``client_secret_*.json``
via :func:`load_client_secret_json` and takes precedence.

TODO(v0.2 pre-release): the placeholder value below must be replaced by a
real Google Cloud Console-registered Desktop app client id + secret before
v0.2 ships publicly.  Track under docs/AUDIT_LOG.md.
"""
from __future__ import annotations

import json
from pathlib import Path

BUNDLED_GDRIVE_CLIENT_ID = "BUNDLED_GDRIVE_CLIENT_ID_TO_REPLACE"
BUNDLED_GDRIVE_CLIENT_SECRET = ""

GDRIVE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GDRIVE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

GDRIVE_DEFAULT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.file",
)
GDRIVE_FULL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive",
)


def load_client_secret_json(path: Path) -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` from a Cloud Console download.

    Google exports the download in one of two envelopes — an installed-app
    ``{"installed": {...}}`` or a web app ``{"web": {...}}``.  Both are
    accepted so users can point at either.  Raises ``ValueError`` on any
    envelope shape the tool cannot interpret.
    """
    data = json.loads(path.read_text())
    for key in ("installed", "web"):
        block = data.get(key)
        if isinstance(block, dict) and "client_id" in block:
            return str(block["client_id"]), str(block.get("client_secret", ""))
    if "client_id" in data:
        return str(data["client_id"]), str(data.get("client_secret", ""))
    raise ValueError(
        f"Unrecognised client_secret envelope at {path}: expected 'installed' "
        "or 'web' object with client_id/client_secret."
    )
