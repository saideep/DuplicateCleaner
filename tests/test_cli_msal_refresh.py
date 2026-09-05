"""Audit pass 11 — MSAL refresh persistence + _MSAL_APP_CACHE keying.

The OneDrive access-token refresh helper (``cli._refresh_onedrive_token``)
must:

* Persist a rotated ``refresh_token`` back into :class:`TokenStore` whenever
  Microsoft supplies a new value (rotation is the default; dropping the
  value would strand the account within days).
* NOT rewrite the token file when the refresh_token is unchanged — avoids
  disk churn on encrypted secrets and keeps ``mtime`` truthful.
* Tolerate a response that omits ``refresh_token`` entirely (rare, but
  documented) — fall back to the existing stored refresh_token and skip
  the write.

The ``_MSAL_APP_CACHE`` cache must:

* Return the SAME ``msal.PublicClientApplication`` instance for repeated
  calls with the same ``(account_id, client_id)`` — MSAL keeps an
  in-memory ``TokenCache`` and re-constructing the app throws it away.
* Return a DIFFERENT instance when the ``client_id`` changes — a BYO
  client_id override must invalidate the cache.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner import cli as cli_mod

_ACCOUNT_ID = "onedrive:test"
_CLIENT_ID = "unit-test-client-id"


def _mk_token_blob(**overrides: Any) -> dict[str, Any]:
    """A minimal-but-valid OneDrive token blob for the refresh helper."""
    base: dict[str, Any] = {
        "account_id": _ACCOUNT_ID,
        "type": "onedrive",
        "user_email": "unit@example.com",
        "client_id": _CLIENT_ID,
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "scopes": ["Files.ReadWrite", "offline_access"],
    }
    base.update(overrides)
    return base


class _FakeApp:
    """Stand-in for ``msal.PublicClientApplication``.

    Only the method the helper calls is implemented — the result dict is
    controlled by the test.
    """

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.call_count = 0

    def acquire_token_by_refresh_token(
        self, refresh_token: str, scopes: list[str]
    ) -> dict[str, Any]:
        self.call_count += 1
        self.last_refresh_token = refresh_token
        self.last_scopes = list(scopes)
        return self._result


@pytest.fixture(autouse=True)
def _reset_msal_cache() -> Any:
    """Every test gets a fresh ``_MSAL_APP_CACHE`` — otherwise a prior
    call's cached app leaks into unrelated tests."""
    cli_mod._MSAL_APP_CACHE.clear()
    yield
    cli_mod._MSAL_APP_CACHE.clear()


def test_refresh_writes_rotated_token_back_to_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Microsoft returns a NEW refresh_token, persist it to TokenStore."""
    fake_app = _FakeApp(
        {
            "access_token": "new-access",
            "refresh_token": "new-refresh",  # rotated
        }
    )
    monkeypatch.setattr(
        cli_mod, "_msal_app_for", lambda _aid, _cid: fake_app
    )
    store = MagicMock()
    access = cli_mod._refresh_onedrive_token(
        _ACCOUNT_ID, tokens=store, initial_data=_mk_token_blob()
    )
    assert access == "new-access"
    store.save.assert_called_once()
    args, _kwargs = store.save.call_args
    saved_id, saved_blob = args
    assert saved_id == _ACCOUNT_ID
    assert saved_blob["refresh_token"] == "new-refresh"
    assert saved_blob["access_token"] == "new-access"


def test_refresh_does_not_rewrite_when_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the refresh_token is unchanged, TokenStore.save is NOT called.

    Avoids disk churn on the encrypted token blob and keeps ``mtime`` a
    truthful signal for the operator / audit tools.
    """
    fake_app = _FakeApp(
        {
            "access_token": "new-access",
            # SAME refresh_token as stored — no rotation happened.
            "refresh_token": "old-refresh",
        }
    )
    monkeypatch.setattr(
        cli_mod, "_msal_app_for", lambda _aid, _cid: fake_app
    )
    store = MagicMock()
    access = cli_mod._refresh_onedrive_token(
        _ACCOUNT_ID, tokens=store, initial_data=_mk_token_blob()
    )
    assert access == "new-access"
    store.save.assert_not_called()


def test_refresh_handles_omitted_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When MSAL omits ``refresh_token`` entirely, the store isn't touched.

    Falling back to the existing refresh_token is the safest behaviour —
    a re-issued access_token is enough to complete the current run, and
    the stored refresh_token is still valid on the next call.
    """
    fake_app = _FakeApp(
        {
            "access_token": "new-access",
            # No 'refresh_token' key at all — MSAL edge case.
        }
    )
    monkeypatch.setattr(
        cli_mod, "_msal_app_for", lambda _aid, _cid: fake_app
    )
    store = MagicMock()
    access = cli_mod._refresh_onedrive_token(
        _ACCOUNT_ID, tokens=store, initial_data=_mk_token_blob()
    )
    assert access == "new-access"
    store.save.assert_not_called()


def test_msal_app_cache_returns_same_instance_for_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``_msal_app_for`` calls with same key → same object identity.

    MSAL builds an in-memory ``TokenCache`` inside every
    ``PublicClientApplication`` — re-constructing on every refresh
    throws away its bookkeeping.  Cache identity is the load-bearing
    property under test.
    """
    calls: list[tuple[str, str]] = []

    class _Recording:
        def __init__(self, client_id: str, authority: str) -> None:
            calls.append((client_id, authority))
            self.client_id = client_id

    class _MsalModule:
        PublicClientApplication = _Recording

    monkeypatch.setitem(__import__("sys").modules, "msal", _MsalModule)
    first = cli_mod._msal_app_for(_ACCOUNT_ID, _CLIENT_ID)
    second = cli_mod._msal_app_for(_ACCOUNT_ID, _CLIENT_ID)
    assert first is second
    assert len(calls) == 1  # the constructor ran exactly once


def test_msal_app_cache_different_key_returns_different_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different ``client_id`` → different cached app.

    A BYO ``--client-secret`` override changes the client_id — the cache
    key incorporates it so the stale app for the prior client_id does
    not bleed through.
    """
    class _Recording:
        def __init__(self, client_id: str, authority: str) -> None:
            self.client_id = client_id

    class _MsalModule:
        PublicClientApplication = _Recording

    monkeypatch.setitem(__import__("sys").modules, "msal", _MsalModule)
    app_a = cli_mod._msal_app_for(_ACCOUNT_ID, _CLIENT_ID)
    app_b = cli_mod._msal_app_for(_ACCOUNT_ID, "other-client-id")
    assert app_a is not app_b
    assert app_a.client_id != app_b.client_id
