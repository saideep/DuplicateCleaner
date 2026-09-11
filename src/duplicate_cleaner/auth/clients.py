"""Bundled OAuth client IDs and provider endpoints.

Compiled-in constants keep the zero-setup default: ``dc auth add gdrive`` (or
``dc auth add onedrive``) works out-of-the-box without the user registering
their own OAuth application.  The BYO override reads a provider-downloaded
``client_secret_*.json`` via :func:`load_client_secret_json` and takes
precedence.

TODO(v0.2 pre-release): the placeholder values below must be replaced by a
real Google Cloud Console-registered Desktop app client id + secret AND a
real Microsoft Entra App Registration (public client, Personal accounts) id
before v0.2 ships publicly.  Track under docs/AUDIT_LOG.md.
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

# ---- Microsoft (OneDrive Personal) ------------------------------------------
# Public-client PKCE flow — Microsoft desktop apps registered as "Public
# client / native (mobile & desktop)" do NOT require a client secret with
# PKCE, so ``BUNDLED_ONEDRIVE_CLIENT_SECRET`` stays empty.  The
# ``/consumers`` authority scopes the app to personal Microsoft accounts and
# rejects Business/Work tenants at the token endpoint — the v0.2 design
# explicitly excludes OneDrive Business (which serves ``quickXorHash``
# instead of SHA-256).
BUNDLED_ONEDRIVE_CLIENT_ID = "BUNDLED_ONEDRIVE_CLIENT_ID_TO_REPLACE"
BUNDLED_ONEDRIVE_CLIENT_SECRET = ""

ONEDRIVE_AUTH_URL = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
)
ONEDRIVE_TOKEN_URL = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
)
# Microsoft does not have a dedicated token-revocation endpoint the way
# Google does — signing out (``/logout``) invalidates the browser session
# but is not a client-credentialled revoke.  ``dc auth remove`` therefore
# falls back to deleting the local token file (send2trash) which is what
# users actually need.
ONEDRIVE_LOGOUT_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/logout"

# ``User.Read`` is required to look up the signed-in user's email at
# auth-add time via ``GET /me``; ``Files.ReadWrite`` covers read + trash;
# ``offline_access`` yields a refresh token.
ONEDRIVE_DEFAULT_SCOPES: tuple[str, ...] = (
    "Files.ReadWrite",
    "offline_access",
    "User.Read",
)

# Microsoft Graph v1.0 root — used by ``sources/onedrive.py``.
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

# ---- Google Photos ---------------------------------------------------------
# v0.6: Google Photos uses the same OAuth 2.0 endpoints as Google Drive (both
# are Google-hosted).  Only the API scope differs — see
# ``GPHOTOS_DEFAULT_SCOPES`` (read-only in v0.6) vs ``GPHOTOS_TRASH_SCOPES``
# (full access; deferred to v0.6.1 because it requires user re-consent).
BUNDLED_GPHOTOS_CLIENT_ID = "BUNDLED_GPHOTOS_CLIENT_ID_TO_REPLACE"
BUNDLED_GPHOTOS_CLIENT_SECRET = ""

GPHOTOS_AUTH_URL = GDRIVE_AUTH_URL
GPHOTOS_TOKEN_URL = GDRIVE_TOKEN_URL
GPHOTOS_REVOKE_URL = GDRIVE_REVOKE_URL

GPHOTOS_DEFAULT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/photoslibrary.readonly",
)
# Reserved for v0.6.1 scope escalation flow — required to actually trash a
# Google Photos media item programmatically.  v0.6 refuses trash calls with
# an actionable SourceError message and defers the escalation.
GPHOTOS_TRASH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/photoslibrary",
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
