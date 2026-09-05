"""TokenStore invariants — 0o600 write, 0o700 parent, atomic replace, mode enforcement on read."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from duplicate_cleaner.auth.tokens import TokenPermissionError, TokenStore


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_save_creates_parent_directory_with_0o700(tmp_path: Path) -> None:
    tdir = tmp_path / "tokens"
    assert not tdir.exists()
    store = TokenStore(base_dir=tdir)
    store.save("gdrive:test", {"access_token": "x"})
    assert tdir.exists()
    assert _mode(tdir) == 0o700


def test_save_writes_file_with_0o600(tmp_path: Path) -> None:
    store = TokenStore(base_dir=tmp_path / "tokens")
    store.save("gdrive:test", {"access_token": "abc", "refresh_token": "def"})
    path = (tmp_path / "tokens" / "gdrive:test.json")
    assert path.exists()
    assert _mode(path) == 0o600


def test_save_is_atomic_leaves_no_temp_files(tmp_path: Path) -> None:
    store = TokenStore(base_dir=tmp_path / "tokens")
    store.save("gdrive:one", {"access_token": "one"})
    store.save("gdrive:one", {"access_token": "two"})  # overwrite
    contents = list((tmp_path / "tokens").iterdir())
    assert len(contents) == 1, [p.name for p in contents]
    data = json.loads(contents[0].read_text())
    assert data["access_token"] == "two"


def test_load_rejects_loose_mode(tmp_path: Path) -> None:
    tdir = tmp_path / "tokens"
    store = TokenStore(base_dir=tdir)
    store.save("gdrive:test", {"access_token": "x"})
    path = tdir / "gdrive:test.json"
    os.chmod(path, 0o644)  # loosen: group + world readable
    with pytest.raises(TokenPermissionError):
        store.load("gdrive:test")


def test_load_missing_returns_none(tmp_path: Path) -> None:
    store = TokenStore(base_dir=tmp_path / "tokens")
    assert store.load("nonexistent:acct") is None


def test_load_round_trip(tmp_path: Path) -> None:
    store = TokenStore(base_dir=tmp_path / "tokens")
    payload = {
        "access_token": "AT",
        "refresh_token": "RT",
        "scopes": ["a", "b"],
        "expires_at": 1234567890.0,
    }
    store.save("gdrive:x", payload)
    got = store.load("gdrive:x")
    assert got is not None
    assert got["access_token"] == "AT"
    assert got["refresh_token"] == "RT"
    assert got["scopes"] == ["a", "b"]


def test_delete_removes_file(tmp_path: Path) -> None:
    store = TokenStore(base_dir=tmp_path / "tokens")
    store.save("gdrive:kill", {"access_token": "x"})
    path = tmp_path / "tokens" / "gdrive:kill.json"
    assert path.exists()
    store.delete("gdrive:kill")
    assert not path.exists()


def test_list_accounts_returns_stems(tmp_path: Path) -> None:
    store = TokenStore(base_dir=tmp_path / "tokens")
    store.save("gdrive:a", {"access_token": "1"})
    store.save("gdrive:b", {"access_token": "2"})
    ids = store.list_accounts()
    assert set(ids) == {"gdrive:a", "gdrive:b"}


def test_invalid_account_id_rejected(tmp_path: Path) -> None:
    store = TokenStore(base_dir=tmp_path / "tokens")
    for bad in ("", "..", ".", "a/b"):
        with pytest.raises(ValueError):
            store.save(bad, {"access_token": "x"})
