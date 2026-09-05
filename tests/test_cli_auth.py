"""B1/B3/B4: CLI-side guards on ``dc auth add`` and ``dc scan``."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from duplicate_cleaner.cli import app
from duplicate_cleaner.config import Config


@pytest.fixture()
def with_active_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Give the CLI a valid ``active_homes`` so downstream guards can be
    reached in ``dc scan`` tests."""
    active_home = tmp_path / "home"
    active_home.mkdir()

    def _fake_load_config() -> Config:
        return Config(active_homes=[active_home])

    monkeypatch.setattr("duplicate_cleaner.cli.load_config", _fake_load_config)
    return active_home


def test_auth_add_gdrive_refuses_when_bundled_client_id_is_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: with the compiled-in placeholder client id, ``dc auth add`` exits 1."""
    # No client_secret override → resolver returns the placeholder, tripping
    # the ``.endswith('_TO_REPLACE')`` guard before any OAuth call happens.
    runner = CliRunner()
    result = runner.invoke(app, ["auth", "add", "gdrive"])
    assert result.exit_code == 1, result.stdout
    assert "bundled" in result.stdout.lower() or "client" in result.stdout.lower()


def test_scan_refuses_non_local_sources_until_sub_phase_5(
    tmp_path: Path,
    with_active_home_config: Path,
) -> None:
    """B3: ``--sources gdrive:x`` must exit 2 with an explanatory message."""
    report_dir = tmp_path / "report"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "scan",
            str(with_active_home_config),
            "--report",
            str(report_dir),
            "--sources",
            "local,gdrive:personal",
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert "sub-phase 5" in result.stdout


def test_scan_accepts_bare_local_sources_flag(
    tmp_path: Path,
    with_active_home_config: Path,
) -> None:
    """B3: the default '--sources local' path must still work."""
    report_dir = tmp_path / "report"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "scan",
            str(with_active_home_config),
            "--report",
            str(report_dir),
            "--sources",
            "local",
        ],
    )
    assert result.exit_code == 0, result.stdout


def test_auth_add_gdrive_refuses_existing_account_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B4: re-running ``dc auth add`` on an existing id refuses without --force."""
    # Isolate token / accounts paths to tmp_path so we don't touch ~/.
    accounts_path = tmp_path / "accounts.toml"
    tokens_dir = tmp_path / "tokens"

    from duplicate_cleaner.auth.accounts import AccountEntry, AccountsRegistry

    registry = AccountsRegistry(path=accounts_path)
    registry.add(
        AccountEntry(
            id="gdrive:personal",
            type="gdrive",
            label="personal",
            user="a@b",
            added_ts="2026-09-05T00:00:00+00:00",
        )
    )

    monkeypatch.setattr(
        "duplicate_cleaner.cli.AccountsRegistry",
        lambda: AccountsRegistry(path=accounts_path),
    )
    from duplicate_cleaner.auth.tokens import TokenStore

    monkeypatch.setattr(
        "duplicate_cleaner.cli.TokenStore",
        lambda: TokenStore(base_dir=tokens_dir),
    )

    # Non-interactive: CliRunner uses a non-tty stdin, so the CLI refuses.
    # Force the collision via --label so ``_default_account_id`` yields the
    # existing "gdrive:personal" instead of auto-suffixing.
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["auth", "add", "gdrive", "--label", "personal"],
        input="",  # stdin non-tty for CliRunner
    )
    assert result.exit_code == 2, result.stdout
    assert "already exists" in result.stdout


def test_scan_calls_purge_stale_cloud_hashes(
    tmp_path: Path,
    with_active_home_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B12: ``dc scan`` invokes Store.purge_stale_cloud_hashes on startup."""
    from duplicate_cleaner import cli as cli_mod
    from duplicate_cleaner.store import Store

    calls: list[float] = []
    real_purge = Store.purge_stale_cloud_hashes

    def _spy(
        self: Store, max_age_days: float = 90.0
    ) -> int:
        calls.append(max_age_days)
        return real_purge(self, max_age_days=max_age_days)

    monkeypatch.setattr(cli_mod.Store, "purge_stale_cloud_hashes", _spy)

    report_dir = tmp_path / "report"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "scan",
            str(with_active_home_config),
            "--report",
            str(report_dir),
            "--cloud-hash-ttl-days",
            "42",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert calls == [42.0]
