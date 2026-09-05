"""AccountsRegistry — TOML roundtrip, id uniqueness, id/label conventions."""
from __future__ import annotations

from pathlib import Path

import pytest

from duplicate_cleaner.auth.accounts import (
    AccountEntry,
    AccountsRegistry,
    DuplicateAccountError,
)


def _registry(tmp_path: Path) -> AccountsRegistry:
    return AccountsRegistry(path=tmp_path / "accounts.toml")


def test_roundtrip_add_and_load(tmp_path: Path) -> None:
    r = _registry(tmp_path)
    e = AccountEntry(
        id="gdrive:personal",
        type="gdrive",
        label="personal",
        user="me@example.com",
        added_ts="2026-09-05T00:00:00+00:00",
    )
    r.add(e)
    got = r.load()
    assert got == [e]


def test_multiple_entries_preserve_order(tmp_path: Path) -> None:
    r = _registry(tmp_path)
    e1 = AccountEntry("gdrive:a", "gdrive", "a", "a@x", "2026-01-01T00:00:00+00:00")
    e2 = AccountEntry("gdrive:b", "gdrive", "b", "b@x", "2026-02-01T00:00:00+00:00")
    r.add(e1)
    r.add(e2)
    assert r.load() == [e1, e2]


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    r = _registry(tmp_path)
    r.add(AccountEntry("gdrive:x", "gdrive", "x", "", ""))
    with pytest.raises(DuplicateAccountError):
        r.add(AccountEntry("gdrive:x", "gdrive", "x-bis", "", ""))


def test_remove_returns_true_only_when_present(tmp_path: Path) -> None:
    r = _registry(tmp_path)
    r.add(AccountEntry("gdrive:x", "gdrive", "x", "", ""))
    assert r.remove("gdrive:x") is True
    assert r.remove("gdrive:x") is False
    assert r.load() == []


def test_get_returns_matching_entry(tmp_path: Path) -> None:
    r = _registry(tmp_path)
    r.add(AccountEntry("gdrive:z", "gdrive", "z", "z@x", "ts"))
    got = r.get("gdrive:z")
    assert got is not None and got.user == "z@x"
    assert r.get("missing") is None


def test_file_is_written_with_0o600(tmp_path: Path) -> None:
    import stat as st

    r = _registry(tmp_path)
    r.add(AccountEntry("gdrive:x", "gdrive", "x", "", ""))
    mode = st.S_IMODE((tmp_path / "accounts.toml").stat().st_mode)
    assert mode == 0o600


def test_load_returns_empty_when_absent(tmp_path: Path) -> None:
    assert _registry(tmp_path).load() == []
