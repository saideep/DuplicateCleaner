"""v0.6.1 — Google Photos trash scope escalation.

Locks in the four load-bearing behaviours of the escalation flow:

1. ``move_to_trash`` refuses without ``has_trash=True`` and points the
   operator at ``dc auth grant-gphotos-trash``.
2. ``move_to_trash`` on a trash-enabled account still refuses because
   the Google Photos Library API v1 does not expose a library-wide
   trash endpoint — the message points at photos.google.com.
3. ``dc auth grant-gphotos-trash`` updates the token file's scopes +
   stamps ``has_trash=True`` (OAuth flow mocked).
4. The scorer treats a trash-enabled gphotos account as a normal
   cross-source member (NOT informational), while a read-only account
   remains informational.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from duplicate_cleaner.compare.exact import Group
from duplicate_cleaner.config import DEFAULT_WEIGHTS, Config
from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.scan.walk import FileRecord
from duplicate_cleaner.score.rules import score_group
from duplicate_cleaner.sources.base import SourceError
from duplicate_cleaner.sources.gphotos import GooglePhotosSource

_REAL_ID = "AAaaBBbbCCccDDddEEee"


def _hr(path: Path, *, source_id: str) -> HashedRecord:
    """Small factory mirroring test_scoring_cross_source._hr."""
    return HashedRecord(
        path=path,
        size=100,
        mtime=1000.0,
        inode=abs(hash(str(path))) & 0xFFFFFFFF,
        dev=1,
        nlink=1,
        full_hash="H" * 64,
        source_id=source_id,
    )


def test_move_to_trash_refuses_without_has_trash() -> None:
    """A gphotos source with ``has_trash=False`` refuses trash with an
    actionable error message pointing at ``dc auth grant-gphotos-trash``.
    """
    service = MagicMock()
    service.mediaItems.return_value = MagicMock()
    src = GooglePhotosSource(
        "gphotos:personal",
        credentials=None,
        # Flag off so we bypass the scan tripwire and reach the
        # has_trash check.
        is_read_only_scan=False,
        has_trash=False,
        service_factory=lambda _c: service,
    )
    assert src.has_trash is False
    rec = FileRecord(
        path=Path("gphotos:personal://photo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:personal",
        cloud_file_id=_REAL_ID,
    )
    with pytest.raises(SourceError) as exc:
        src.move_to_trash(rec)
    msg = str(exc.value)
    assert "grant-gphotos-trash" in msg
    assert "gphotos:personal" in msg


def test_move_to_trash_investigate_api_capability() -> None:
    """Google Photos Library API v1 does NOT expose a library-wide trash
    endpoint.  Even for a trash-enabled account, ``move_to_trash``
    surfaces a distinct ``SourceError`` naming the API constraint and
    pointing at photos.google.com/trash for the manual step.

    This test locks in the v0.6.1 fallback behaviour so a future
    refactor that swaps the raise for a phantom API call is caught.
    """
    service = MagicMock()
    service.mediaItems.return_value = MagicMock()
    src = GooglePhotosSource(
        "gphotos:personal",
        credentials=None,
        is_read_only_scan=False,
        has_trash=True,
        service_factory=lambda _c: service,
    )
    rec = FileRecord(
        path=Path("gphotos:personal://photo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:personal",
        cloud_file_id=_REAL_ID,
    )
    with pytest.raises(SourceError) as exc:
        src.move_to_trash(rec)
    msg = str(exc.value)
    assert "photos.google.com" in msg
    assert "Google Photos Library API" in msg or "library-wide" in msg
    # No Photos API call is expected — the raise fires before any
    # ``mediaItems`` method is invoked.
    assert not service.mediaItems.return_value.method_calls


def test_move_to_trash_read_only_scan_still_wins() -> None:
    """The scan-time tripwire fires BEFORE the has_trash check.

    Defense-in-depth: even a trash-enabled account constructed with
    ``is_read_only_scan=True`` refuses.  Locks the ordering so a
    future refactor cannot accidentally check has_trash first and
    reach a Photos API call from the scan path.
    """
    src = GooglePhotosSource(
        "gphotos:personal",
        credentials=None,
        is_read_only_scan=True,
        has_trash=True,
        service_factory=lambda _c: MagicMock(),
    )
    rec = FileRecord(
        path=Path("gphotos:personal://photo.HEIC"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gphotos:personal",
        cloud_file_id=_REAL_ID,
    )
    with pytest.raises(SourceError) as exc:
        src.move_to_trash(rec)
    msg = str(exc.value).lower()
    assert "read-only" in msg


def test_grant_gphotos_trash_updates_token_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dc auth grant-gphotos-trash`` re-runs OAuth with the full
    photoslibrary scope and stamps ``has_trash=True`` on the token
    file.  OAuth is mocked so the flow does not hit the network.
    """
    from duplicate_cleaner.auth.accounts import AccountEntry, AccountsRegistry
    from duplicate_cleaner.auth.tokens import TokenStore
    from duplicate_cleaner.cli import app

    accounts_path = tmp_path / "accounts.toml"
    tokens_dir = tmp_path / "tokens"

    def _account_reg() -> AccountsRegistry:
        return AccountsRegistry(path=accounts_path)

    def _token_store() -> TokenStore:
        return TokenStore(base_dir=tokens_dir)

    monkeypatch.setattr("duplicate_cleaner.cli.AccountsRegistry", _account_reg)
    monkeypatch.setattr("duplicate_cleaner.cli.TokenStore", _token_store)

    # Pre-register the account and its initial (read-only) token blob.
    registry = _account_reg()
    registry.add(
        AccountEntry(
            id="gphotos:personal",
            type="gphotos",
            label="personal",
            user="me@example.com",
            added_ts=AccountsRegistry.now_ts(),
        )
    )
    initial_token = {
        "account_id": "gphotos:personal",
        "type": "gphotos",
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "access_token": "old-access-token",
        "refresh_token": "old-refresh-token",
        "scopes": [
            "https://www.googleapis.com/auth/photoslibrary.readonly"
        ],
        "has_trash": False,
        "user_email": "me@example.com",
    }
    _token_store().save("gphotos:personal", initial_token)

    # Mock the OAuth flow to return a fresh token with the full scope.
    fake_new_token = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 3600,
        "scopes": [
            "https://www.googleapis.com/auth/photoslibrary"
        ],
        "user_email": "me@example.com",
    }

    def _fake_run_flow(**kwargs: Any) -> dict[str, Any]:
        # Confirm we are running with the full-scope list.
        assert kwargs["scopes"] == [
            "https://www.googleapis.com/auth/photoslibrary"
        ]
        # Confirm the client_id / client_secret came from the existing
        # token blob (not a new BYO flow).
        assert kwargs["client_id"] == "test-client-id.apps.googleusercontent.com"
        assert kwargs["client_secret"] == "test-secret"
        return dict(fake_new_token)

    monkeypatch.setattr("duplicate_cleaner.cli.run_localhost_flow", _fake_run_flow)

    runner = CliRunner()
    result = runner.invoke(
        app, ["auth", "grant-gphotos-trash", "gphotos:personal"]
    )
    assert result.exit_code == 0, result.stdout
    assert "Escalated" in result.stdout or "escalated" in result.stdout.lower()

    # Token file reflects the new state.
    saved = _token_store().load("gphotos:personal")
    assert saved is not None
    assert saved["has_trash"] is True
    assert saved["access_token"] == "new-access-token"
    assert saved["scopes"] == [
        "https://www.googleapis.com/auth/photoslibrary"
    ]
    # Client id / secret carried forward from the pre-existing blob.
    assert saved["client_id"] == "test-client-id.apps.googleusercontent.com"
    assert saved["client_secret"] == "test-secret"


def test_grant_gphotos_trash_refuses_non_gphotos_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dc auth grant-gphotos-trash`` is gphotos-only.  Registering a
    gdrive account and calling the command refuses with exit 2.
    """
    from duplicate_cleaner.auth.accounts import AccountEntry, AccountsRegistry
    from duplicate_cleaner.auth.tokens import TokenStore
    from duplicate_cleaner.cli import app

    accounts_path = tmp_path / "accounts.toml"
    tokens_dir = tmp_path / "tokens"

    monkeypatch.setattr(
        "duplicate_cleaner.cli.AccountsRegistry",
        lambda: AccountsRegistry(path=accounts_path),
    )
    monkeypatch.setattr(
        "duplicate_cleaner.cli.TokenStore",
        lambda: TokenStore(base_dir=tokens_dir),
    )

    AccountsRegistry(path=accounts_path).add(
        AccountEntry(
            id="gdrive:personal",
            type="gdrive",
            label="personal",
            user="me@example.com",
            added_ts=AccountsRegistry.now_ts(),
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["auth", "grant-gphotos-trash", "gdrive:personal"]
    )
    assert result.exit_code == 2, result.stdout
    assert "gphotos" in result.stdout.lower() or "google photos" in result.stdout.lower()


def test_scorer_treats_trash_enabled_gphotos_as_normal_source(
    tmp_path: Path,
) -> None:
    """v0.6.1: a gphotos: member in ``trash_enabled_source_ids`` is NOT
    marked informational.  The scorer's ``cloud_when_local_exists``
    penalty scores it below the local peer, so local stays keeper and
    the gphotos member is a legitimate discard candidate.
    """
    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    local_path = active / "Documents" / "photo.HEIC"
    local_path.write_bytes(b"x")
    gphotos_path = Path("gphotos:personal://photo.HEIC")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(gphotos_path, source_id="gphotos:personal"),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(
        group,
        cfg,
        DEFAULT_WEIGHTS,
        trash_enabled_source_ids=frozenset({"gphotos:personal"}),
    )
    by_source = {m.source_id: m for m in members}

    assert by_source["gphotos:personal"].is_informational is False
    assert by_source["gphotos:personal"].is_proposed_keeper is False
    assert by_source["local"].is_proposed_keeper is True


def test_scorer_treats_readonly_gphotos_as_informational(
    tmp_path: Path,
) -> None:
    """Symmetric to the trash-enabled case: ``has_trash=False`` (empty
    trash_enabled_source_ids) keeps v0.6-patch behaviour — the gphotos
    member is informational so the whole scan can commit.
    """
    active = tmp_path / "Users" / "me"
    (active / "Documents").mkdir(parents=True)
    local_path = active / "Documents" / "photo.HEIC"
    local_path.write_bytes(b"x")
    gphotos_path = Path("gphotos:personal://photo.HEIC")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(gphotos_path, source_id="gphotos:personal"),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(
        group,
        cfg,
        DEFAULT_WEIGHTS,
        # Explicit empty set — same effect as omitting the kwarg.
        trash_enabled_source_ids=frozenset(),
    )
    by_source = {m.source_id: m for m in members}

    assert by_source["gphotos:personal"].is_informational is True
    assert by_source["gphotos:personal"].is_proposed_keeper is False
    assert by_source["local"].is_proposed_keeper is True


def test_scorer_icloud_still_informational_even_with_trash_map(
    tmp_path: Path,
) -> None:
    """iCloud is permanently read-only.  Passing ``icloud:*`` in
    ``trash_enabled_source_ids`` (a nonsense caller state) is a no-op —
    icloud members stay informational unconditionally.
    """
    active = tmp_path / "Users" / "me"
    (active / "Pictures").mkdir(parents=True)
    local_path = active / "Pictures" / "photo.HEIC"
    local_path.write_bytes(b"y")
    icloud_path = Path("icloud:personal://photo.HEIC")

    group = Group(
        hash="H",
        size=1,
        members=[
            _hr(local_path, source_id="local"),
            _hr(icloud_path, source_id="icloud:personal"),
        ],
    )
    cfg = Config(active_homes=[active])
    members = score_group(
        group,
        cfg,
        DEFAULT_WEIGHTS,
        trash_enabled_source_ids=frozenset({"icloud:personal"}),
    )
    by_source = {m.source_id: m for m in members}
    assert by_source["icloud:personal"].is_informational is True


def test_auth_list_surfaces_trash_enabled_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dc auth list`` shows a ``scope`` column: ``read-only`` for a
    default gphotos account, ``trash-enabled`` once granted.
    """
    from duplicate_cleaner.auth.accounts import AccountEntry, AccountsRegistry
    from duplicate_cleaner.auth.tokens import TokenStore
    from duplicate_cleaner.cli import app

    accounts_path = tmp_path / "accounts.toml"
    tokens_dir = tmp_path / "tokens"

    monkeypatch.setattr(
        "duplicate_cleaner.cli.AccountsRegistry",
        lambda: AccountsRegistry(path=accounts_path),
    )
    monkeypatch.setattr(
        "duplicate_cleaner.cli.TokenStore",
        lambda: TokenStore(base_dir=tokens_dir),
    )

    registry = AccountsRegistry(path=accounts_path)
    registry.add(
        AccountEntry(
            id="gphotos:personal",
            type="gphotos",
            label="personal",
            user="me@example.com",
            added_ts=AccountsRegistry.now_ts(),
        )
    )
    registry.add(
        AccountEntry(
            id="gphotos:family",
            type="gphotos",
            label="family",
            user="partner@example.com",
            added_ts=AccountsRegistry.now_ts(),
        )
    )
    tokens = TokenStore(base_dir=tokens_dir)
    tokens.save(
        "gphotos:personal",
        {
            "type": "gphotos",
            "access_token": "a",
            "refresh_token": "r",
            "client_id": "c",
            "client_secret": "s",
            "has_trash": True,
        },
    )
    tokens.save(
        "gphotos:family",
        {
            "type": "gphotos",
            "access_token": "a",
            "refresh_token": "r",
            "client_id": "c",
            "client_secret": "s",
            "has_trash": False,
        },
    )

    runner = CliRunner()
    result = runner.invoke(app, ["auth", "list"])
    assert result.exit_code == 0, result.stdout
    # Rich renders the Table with box-drawing separators.  Column
    # widths are auto-computed against a narrow test terminal, so the
    # id column may truncate a long value (``gphotos:personal`` →
    # ``gphotos:person…``).  Match on ``gphotos:pers`` — a prefix
    # unique to that row — and on ``gphotos:family`` which fits
    # untruncated.  Each row is one line so a per-line substring check
    # is unambiguous.
    lines = result.stdout.splitlines()
    personal_line = next(
        (ln for ln in lines if "gphotos:pers" in ln), None
    )
    family_line = next(
        (ln for ln in lines if "gphotos:family" in ln), None
    )
    assert personal_line is not None, result.stdout
    assert family_line is not None, result.stdout
    assert "trash-enabled" in personal_line
    assert "read-only" in family_line


def test_token_saved_by_grant_has_trash_true(
    tmp_path: Path,
) -> None:
    """Round-trip: after ``grant-gphotos-trash`` writes the token file,
    a subsequent ``TokenStore.load`` returns ``has_trash=True`` (json
    round-trip is not the same as an in-memory dict update).
    """
    from duplicate_cleaner.auth.tokens import TokenStore

    tokens_dir = tmp_path / "tokens"
    tokens = TokenStore(base_dir=tokens_dir)
    tokens.save(
        "gphotos:personal",
        {
            "type": "gphotos",
            "access_token": "a",
            "refresh_token": "r",
            "client_id": "c",
            "client_secret": "s",
            "has_trash": True,
            "scopes": ["https://www.googleapis.com/auth/photoslibrary"],
        },
    )
    raw = json.loads(
        (tokens_dir / "gphotos:personal.json").read_text()
    )
    assert raw["has_trash"] is True
    loaded = tokens.load("gphotos:personal")
    assert loaded is not None
    assert loaded["has_trash"] is True
