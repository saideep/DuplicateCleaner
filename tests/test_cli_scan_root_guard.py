"""G2: ``dc scan`` rejects roots that would sweep too much of the filesystem.

The check runs before any walker or hasher starts, so bad roots exit
fast with a clear error and no side effects.
"""
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
    """Stub ``load_config`` so the CLI has a valid ``active_homes`` and
    reaches the G2 root guard.
    """
    active_home = tmp_path / "home"
    active_home.mkdir()

    def _fake_load_config() -> Config:
        return Config(active_homes=[active_home])

    monkeypatch.setattr("duplicate_cleaner.cli.load_config", _fake_load_config)
    return active_home


@pytest.mark.parametrize(
    "bad_root",
    [
        "/",
        "/Users",
        "/Volumes",
        "/System",
        "/private/var/folders",
    ],
)
def test_scan_rejects_shallow_or_excluded_root(
    tmp_path: Path,
    with_active_home_config: Path,
    bad_root: str,
) -> None:
    report_dir = tmp_path / "report"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scan", bad_root, "--report", str(report_dir)],
    )
    assert result.exit_code == 2, result.stdout
    assert "Refusing to scan" in result.stdout
    assert not report_dir.exists()
