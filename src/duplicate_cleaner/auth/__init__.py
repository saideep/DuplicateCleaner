"""Shared OAuth infrastructure: token storage, accounts registry, PKCE flow."""
from __future__ import annotations

from duplicate_cleaner.auth.accounts import (
    ACCOUNTS_PATH,
    AccountEntry,
    AccountsRegistry,
    DuplicateAccountError,
)
from duplicate_cleaner.auth.clients import (
    BUNDLED_GDRIVE_CLIENT_ID,
    BUNDLED_GDRIVE_CLIENT_SECRET,
    GDRIVE_AUTH_URL,
    GDRIVE_DEFAULT_SCOPES,
    GDRIVE_FULL_SCOPES,
    GDRIVE_REVOKE_URL,
    GDRIVE_TOKEN_URL,
    load_client_secret_json,
)
from duplicate_cleaner.auth.oauth_flow import OAuthFlowError, run_localhost_flow
from duplicate_cleaner.auth.tokens import (
    TOKENS_DIR,
    TokenPermissionError,
    TokenStore,
)

__all__ = [
    "ACCOUNTS_PATH",
    "BUNDLED_GDRIVE_CLIENT_ID",
    "BUNDLED_GDRIVE_CLIENT_SECRET",
    "GDRIVE_AUTH_URL",
    "GDRIVE_DEFAULT_SCOPES",
    "GDRIVE_FULL_SCOPES",
    "GDRIVE_REVOKE_URL",
    "GDRIVE_TOKEN_URL",
    "TOKENS_DIR",
    "AccountEntry",
    "AccountsRegistry",
    "DuplicateAccountError",
    "OAuthFlowError",
    "TokenPermissionError",
    "TokenStore",
    "load_client_secret_json",
    "run_localhost_flow",
]
