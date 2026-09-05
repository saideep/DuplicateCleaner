"""v0.2 sub-phase 5d — undo dispatches cloud manifest rows through Source.restore_from_trash.

Every cloud source in these tests is a MagicMock; no real network, no real
provider SDK.  The invariants under test:

* A manifest cloud entry routes through the matching ``sources`` map entry.
* A missing ``source_id`` in the map surfaces as a per-entry error (not abort).
* Entries with ``cloud_trash_id=None`` are REJECTED (audit pass 10 finding #1).
* A malformed ``cloud_file_id`` is rejected.
* Mixed local + cloud manifests dispatch each family through its channel.
* ``SourceNotFoundError`` on restore is logged and skipped (recycle bin empty
  / OneDrive Personal ``notSupported``); the rest of the manifest still
  restores.
* ``SourceAuthError`` aborts the whole run.
* A v0.1.1 local-only manifest (no cloud entries) restores as before.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.apply.undo import UndoError, restore_from_manifest
from duplicate_cleaner.auth.accounts import AccountEntry
from duplicate_cleaner.sources.base import (
    SourceAuthError,
    SourceNotFoundError,
    SourcePermissionError,
    TrashedLocation,
)

_GDRIVE_REAL_ID = "1AbCdEfGhIjKlMnOpQrSt"
_GDRIVE_REAL_ID_2 = "2ZyXwVuTsRqPoNmLkJiHg"
_GDRIVE_REAL_ID_3 = "3aBcDeFgHiJkLmNoPqRsT"


class _FakeRegistry:
    """Stand-in for :class:`AccountsRegistry` — only ``.load()`` is called."""

    def __init__(self, ids: list[str]) -> None:
        self._entries = [
            AccountEntry(id=i, type="gdrive", label=i, user="test", added_ts="")
            for i in ids
        ]

    def load(self) -> list[AccountEntry]:
        return self._entries


def _write_manifest(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    """Serialise a v0.2 manifest envelope with the given entries."""
    manifest = {
        "manifest_version": "0.2.0",
        "created_at": "20260905T000000Z",
        "roots": [],
        "entries": entries,
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest))
    return p


def _mk_cloud_entry(**overrides: Any) -> dict[str, Any]:
    """Base v0.2 manifest cloud entry — every test only overrides what varies."""
    base: dict[str, Any] = {
        "source_id": "gdrive:personal",
        "original_path": "gdrive:personal://Docs/foo.bin",
        "size": 8,
        "mtime": 1.0,
        "hash": "H" * 32,
        "trashed_at_path": None,
        "cloud_file_id": _GDRIVE_REAL_ID,
        "cloud_trash_id": _GDRIVE_REAL_ID,
        "etag": f"{_GDRIVE_REAL_ID}:1",
    }
    base.update(overrides)
    return base


def _mk_local_entry(
    *,
    original_path: Path,
    trashed_at_path: Path,
    size: int,
    mtime: float = 1.0,
) -> dict[str, Any]:
    return {
        "source_id": "local",
        "original_path": str(original_path),
        "size": size,
        "mtime": mtime,
        "hash": "H" * 32,
        "trashed_at_path": str(trashed_at_path),
    }


def test_undo_cloud_entry_calls_source_restore_from_trash(tmp_path: Path) -> None:
    """A cloud manifest row dispatches to the matching Source.restore_from_trash."""
    manifest = _write_manifest(tmp_path, entries=[_mk_cloud_entry()])
    src = MagicMock()
    src.id = "gdrive:personal"
    src.restore_from_trash.return_value = None
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    assert result["restored_cloud"] == 1
    assert result["restored"] == 1
    assert result["errors"] == []
    src.restore_from_trash.assert_called_once()
    (loc,), _kwargs = src.restore_from_trash.call_args
    assert isinstance(loc, TrashedLocation)
    assert loc.source_id == "gdrive:personal"
    assert loc.cloud_file_id == _GDRIVE_REAL_ID
    assert loc.cloud_trash_id == _GDRIVE_REAL_ID
    assert loc.original_path == "gdrive:personal://Docs/foo.bin"


def test_undo_cloud_entry_refused_when_source_not_in_map(tmp_path: Path) -> None:
    """Missing source in the map surfaces as a per-entry error (not abort)."""
    manifest = _write_manifest(tmp_path, entries=[_mk_cloud_entry()])
    # The map lists a DIFFERENT account.
    other = MagicMock()
    other.id = "gdrive:other"
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:other": other},
    )
    assert result["restored_cloud"] == 0
    assert len(result["errors"]) == 1
    assert "gdrive:personal" in result["errors"][0]
    other.restore_from_trash.assert_not_called()


def test_undo_cloud_entry_with_null_cloud_trash_id_rejected(tmp_path: Path) -> None:
    """Audit pass 10 finding #1: null cloud_trash_id (aborted-apply artifact)
    MUST be rejected — restoring one would silently un-trash a file another
    client had trashed."""
    manifest = _write_manifest(
        tmp_path,
        entries=[_mk_cloud_entry(cloud_trash_id=None)],
    )
    src = MagicMock()
    src.id = "gdrive:personal"
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    assert result["restored_cloud"] == 0
    assert len(result["errors"]) == 1
    msg = result["errors"][0]
    assert "aborted apply" in msg or "nothing to restore" in msg
    src.restore_from_trash.assert_not_called()


def test_undo_cloud_entry_with_bad_cloud_file_id_shape_rejected(tmp_path: Path) -> None:
    """A poisoned cloud_file_id fails the shape gate and is refused."""
    manifest = _write_manifest(
        tmp_path,
        entries=[_mk_cloud_entry(cloud_file_id="a/b/c")],
    )
    src = MagicMock()
    src.id = "gdrive:personal"
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    assert result["restored_cloud"] == 0
    assert len(result["errors"]) == 1
    src.restore_from_trash.assert_not_called()


def test_undo_mixed_local_and_cloud(tmp_path: Path) -> None:
    """2 local + 3 cloud entries; each dispatch goes to the correct channel."""
    # Prepare fake local trash — 2 files sitting in a trash-directory
    # override so ``local_restore`` finds them.
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    local_originals: list[Path] = []
    local_trashed: list[Path] = []
    for i in range(2):
        orig = tmp_path / f"restored_{i}.txt"
        trashed = fake_trash / f"restored_{i}.txt"
        trashed.write_bytes(b"content")
        local_originals.append(orig)
        local_trashed.append(trashed)
    entries: list[dict[str, Any]] = [
        _mk_local_entry(
            original_path=o,
            trashed_at_path=t,
            size=len(b"content"),
        )
        for o, t in zip(local_originals, local_trashed, strict=True)
    ]
    for cid in (_GDRIVE_REAL_ID, _GDRIVE_REAL_ID_2, _GDRIVE_REAL_ID_3):
        entries.append(
            _mk_cloud_entry(
                cloud_file_id=cid,
                cloud_trash_id=cid,
                etag=f"{cid}:t",
                original_path=f"gdrive:personal://Docs/{cid[:4]}.bin",
            )
        )
    manifest = _write_manifest(tmp_path, entries=entries)
    src = MagicMock()
    src.id = "gdrive:personal"
    src.restore_from_trash.return_value = None
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[fake_trash],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    assert result["restored_local"] == 2
    assert result["restored_cloud"] == 3
    assert result["restored"] == 5
    assert result["errors"] == []
    # Every local file materialised at its original path via shutil.move.
    for o in local_originals:
        assert o.exists()
        assert o.read_bytes() == b"content"
    # Every cloud entry dispatched to the same source.
    assert src.restore_from_trash.call_count == 3


def test_undo_cloud_source_not_found_logs_and_continues(tmp_path: Path) -> None:
    """SourceNotFoundError (empty recycle bin / 501 notSupported) skips + continues."""
    manifest = _write_manifest(
        tmp_path,
        entries=[
            _mk_cloud_entry(
                cloud_file_id=_GDRIVE_REAL_ID,
                cloud_trash_id=_GDRIVE_REAL_ID,
                original_path="gdrive:personal://gone.bin",
            ),
            _mk_cloud_entry(
                cloud_file_id=_GDRIVE_REAL_ID_2,
                cloud_trash_id=_GDRIVE_REAL_ID_2,
                original_path="gdrive:personal://ok.bin",
            ),
        ],
    )
    call_index = {"n": 0}

    def _restore(loc: TrashedLocation) -> None:
        n = call_index["n"]
        call_index["n"] = n + 1
        if n == 0:
            raise SourceNotFoundError("recycle bin emptied")
        return None

    src = MagicMock()
    src.id = "gdrive:personal"
    src.restore_from_trash.side_effect = _restore
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    # One restored (entry 2), one skipped (entry 1).
    assert result["restored_cloud"] == 1
    assert result["skipped_cloud"] == 1
    assert len(result["errors"]) == 1
    assert "gone" in result["errors"][0] or "recycle" in result["errors"][0].lower()


def test_undo_cloud_source_permission_error_increments_skipped(
    tmp_path: Path,
) -> None:
    """Audit pass 11: SourcePermissionError is a per-entry SKIP, not abort.

    Symmetrises with SourceNotFoundError above.  A single ACL change on
    one cloud file must not torpedo the rest of the manifest.
    ``skipped_cloud`` counts BOTH kinds of "log-and-continue" outcomes.
    """
    manifest = _write_manifest(
        tmp_path,
        entries=[
            _mk_cloud_entry(
                cloud_file_id=_GDRIVE_REAL_ID,
                cloud_trash_id=_GDRIVE_REAL_ID,
                original_path="gdrive:personal://noacl.bin",
            ),
            _mk_cloud_entry(
                cloud_file_id=_GDRIVE_REAL_ID_2,
                cloud_trash_id=_GDRIVE_REAL_ID_2,
                original_path="gdrive:personal://ok.bin",
            ),
        ],
    )
    call_index = {"n": 0}

    def _restore(loc: TrashedLocation) -> None:
        n = call_index["n"]
        call_index["n"] = n + 1
        if n == 0:
            raise SourcePermissionError("caller lacks the role to restore")
        return None

    src = MagicMock()
    src.id = "gdrive:personal"
    src.restore_from_trash.side_effect = _restore
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    # One restored (entry 2), one skipped (entry 1).
    assert result["restored_cloud"] == 1
    assert result["skipped_cloud"] == 1
    assert len(result["errors"]) == 1
    err_msg = result["errors"][0].lower()
    assert "permission" in err_msg or "acl" in err_msg or "denied" in err_msg


def test_undo_cloud_source_auth_error_aborts(tmp_path: Path) -> None:
    """SourceAuthError on restore aborts the whole undo run via UndoError."""
    manifest = _write_manifest(
        tmp_path,
        entries=[
            _mk_cloud_entry(
                cloud_file_id=_GDRIVE_REAL_ID,
                cloud_trash_id=_GDRIVE_REAL_ID,
            ),
            _mk_cloud_entry(
                cloud_file_id=_GDRIVE_REAL_ID_2,
                cloud_trash_id=_GDRIVE_REAL_ID_2,
            ),
        ],
    )
    src = MagicMock()
    src.id = "gdrive:personal"
    src.restore_from_trash.side_effect = SourceAuthError("token rejected")
    with pytest.raises(UndoError) as exc:
        restore_from_manifest(
            manifest,
            allowed_trash_dirs=[tmp_path],
            registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
            sources={"gdrive:personal": src},
        )
    # The abort should NOT let entry 2 dispatch.
    assert src.restore_from_trash.call_count == 1
    assert "SourceAuthError" in str(exc.value) or "auth" in str(exc.value).lower()


def test_undo_local_only_report_still_works(tmp_path: Path) -> None:
    """v0.1.1-shaped manifest (no cloud entries) restores exactly as before.

    Regression: ``sources=None`` must be a supported call shape and the
    return dict must still expose the v0.1.1 ``restored`` key (as the sum
    of local + cloud).
    """
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    original = tmp_path / "restored.txt"
    trashed = fake_trash / "restored.txt"
    trashed.write_bytes(b"content")
    manifest_body = {
        "created_at": "20260905T000000Z",
        "entries": [
            {
                # No ``source_id`` field — v0.1.1 shape.
                "original_path": str(original),
                "size": len(b"content"),
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": str(trashed),
            }
        ],
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest_body))
    result = restore_from_manifest(
        p,
        allowed_trash_dirs=[fake_trash],
    )
    assert result["restored"] == 1
    assert result["restored_local"] == 1
    assert result["restored_cloud"] == 0
    assert result["skipped_cloud"] == 0
    assert result["errors"] == []
    assert original.exists()
    assert original.read_bytes() == b"content"


def test_undo_cloud_entry_local_only_mode_errors_per_entry(tmp_path: Path) -> None:
    """A cloud entry in a manifest handed sources=None surfaces per-entry error.

    Local rows in the same manifest still restore — the mode does not abort
    the run, only refuses each cloud row.
    """
    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    original = tmp_path / "restored.txt"
    trashed = fake_trash / "restored.txt"
    trashed.write_bytes(b"content")
    manifest = _write_manifest(
        tmp_path,
        entries=[
            _mk_local_entry(
                original_path=original,
                trashed_at_path=trashed,
                size=len(b"content"),
            ),
            _mk_cloud_entry(),
        ],
    )
    # Ensure we use the fake_trash directory, not tmp_path (which is not
    # actually a trash directory) — the local entry's trashed_at_path
    # points at fake_trash.
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[fake_trash],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources=None,
    )
    assert result["restored_local"] == 1
    assert result["restored_cloud"] == 0
    assert len(result["errors"]) == 1
    assert (
        "local-only" in result["errors"][0].lower()
        or "no sources" in result["errors"][0].lower()
    )
