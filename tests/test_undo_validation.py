"""v0.2 sub-phase 5b — undo dispatches by ``source_id`` (Alt-C).

Symmetric to ``test_mover_source_id_dispatch.py``: the manifest reader
dispatches each entry on ``source_id`` before ANY local-path validation.
Cloud entries flow through ``validate_cloud_manifest_entry`` (source_id
authorized, cloud_file_id shape, etag present); the cloud ``original_path``
is never ``.resolve()``d.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from duplicate_cleaner.apply.undo import restore_from_manifest
from duplicate_cleaner.auth.accounts import AccountEntry

_GDRIVE_REAL_ID = "1AbCdEfGhIjKlMnOpQrSt"


class _FakeRegistry:
    """Stand-in for ``AccountsRegistry`` — only ``.load()`` is called."""

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


def test_undo_cloud_entry_refused_unknown_source_id(tmp_path: Path) -> None:
    """An unknown ``source_id`` in a manifest cloud entry raises a per-entry error."""
    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "gdrive:evil",
                "original_path": "gdrive:evil://foo",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": _GDRIVE_REAL_ID,
                "etag": f"{_GDRIVE_REAL_ID}:1",
            }
        ],
    )
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:known"]),  # type: ignore[arg-type]
    )
    # The bad cloud entry surfaces as a per-entry error, not an abort.
    assert result["restored"] == 0
    assert len(result["errors"]) == 1
    assert "gdrive:evil" in result["errors"][0]


def test_undo_cloud_entry_refused_missing_cloud_file_id(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "gdrive:personal",
                "original_path": "gdrive:personal://foo",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": None,
                "etag": "some:etag",
            }
        ],
    )
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
    )
    assert result["restored"] == 0
    assert any("cloud_file_id" in e for e in result["errors"])


def test_undo_cloud_entry_refused_bad_cloud_file_id_shape(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "gdrive:personal",
                "original_path": "gdrive:personal://foo",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": "a/b/c",
                "etag": "a:1",
            }
        ],
    )
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
    )
    assert result["restored"] == 0
    assert any("shape" in e or "gdrive" in e for e in result["errors"])


def test_undo_cloud_entry_refused_missing_etag(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "gdrive:personal",
                "original_path": "gdrive:personal://foo",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": _GDRIVE_REAL_ID,
                "etag": None,
            }
        ],
    )
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
    )
    assert result["restored"] == 0
    assert any("etag" in e for e in result["errors"])


def test_undo_cloud_path_never_resolved(tmp_path: Path) -> None:
    """Alt-C invariant: cloud ``original_path`` is never ``.resolve()``d in undo.

    Patches ``paths.resolve_for_check`` to record every call.  A cloud
    manifest entry must NOT surface at ``resolve_for_check`` — the dispatch
    branch runs cloud rails only.

    v0.2 sub-phase 5d: the cloud dispatch now calls
    ``Source.restore_from_trash``; we supply a MagicMock source so the entry
    routes through cloud rails and never touches ``resolve_for_check``.
    """
    from unittest.mock import MagicMock

    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "gdrive:personal",
                "original_path": "gdrive:personal://My Drive/foo.bin",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": _GDRIVE_REAL_ID,
                "cloud_trash_id": _GDRIVE_REAL_ID,
                "etag": f"{_GDRIVE_REAL_ID}:1",
            }
        ],
    )

    from duplicate_cleaner.apply import undo as undo_mod

    resolve_calls: list[Path] = []
    original = undo_mod.resolve_for_check

    def _spy(p: Path) -> Path:
        resolve_calls.append(p)
        return original(p)

    stub_src = MagicMock()
    stub_src.id = "gdrive:personal"
    stub_src.restore_from_trash.return_value = None
    with patch.object(undo_mod, "resolve_for_check", side_effect=_spy):
        result = restore_from_manifest(
            manifest,
            allowed_trash_dirs=[tmp_path],
            registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
            sources={"gdrive:personal": stub_src},
        )
    # v0.2 sub-phase 5d: cloud entry restored (was cloud_deferred in 5b).
    assert result["restored_cloud"] == 1
    assert result["cloud_deferred"] == 0
    # No call recorded a cloud path.
    for p in resolve_calls:
        assert "gdrive:" not in str(p), (
            f"resolve_for_check called on cloud path {p!r} — Alt-C violated"
        )


def test_undo_local_flow_still_runs_local_rails(tmp_path: Path) -> None:
    """Regression: a local manifest entry continues to route through the
    v0.1.1 rails (F12 / H2 / H5 rejection paths) untouched by 5b.
    """
    # A poisoned local original_path pointing into ~/Library — must be
    # rejected as before.
    poisoned = str(Path.home() / "Library" / "Application Support" / "poisoned.txt")
    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "local",
                "original_path": poisoned,
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": str(tmp_path / "some.trash"),
            }
        ],
    )
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry([]),  # type: ignore[arg-type]
    )
    assert result["restored"] == 0
    # The local F12 rail fires on the ~/Library path.
    assert any("excluded" in e or "Library" in e for e in result["errors"])


def test_undo_mixed_manifest_dispatches_correctly(tmp_path: Path) -> None:
    """Cloud entries validate + dispatch cleanly and local entries continue to restore.

    The local entry uses the trashed-basename fallback (no trashed_at_path)
    against a fake Trash directory injected via ``allowed_trash_dirs``.

    v0.2 sub-phase 5d: the cloud entry now dispatches through the mock
    source's ``restore_from_trash`` — both count in ``restored``.
    """
    from unittest.mock import MagicMock

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()
    original_local = tmp_path / "restored.txt"
    trashed_local = fake_trash / "restored.txt"
    trashed_local.write_bytes(b"content")
    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "local",
                "original_path": str(original_local),
                "size": len(b"content"),
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": str(trashed_local),
            },
            {
                "source_id": "gdrive:personal",
                "original_path": "gdrive:personal://Docs/foo.bin",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": _GDRIVE_REAL_ID,
                "cloud_trash_id": _GDRIVE_REAL_ID,
                "etag": f"{_GDRIVE_REAL_ID}:1",
            },
        ],
    )
    stub_src = MagicMock()
    stub_src.id = "gdrive:personal"
    stub_src.restore_from_trash.return_value = None
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[fake_trash],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": stub_src},
    )
    # Both entries restored — one local, one cloud.
    assert result["restored_local"] == 1
    assert result["restored_cloud"] == 1
    assert result["restored"] == 2
    assert result["cloud_deferred"] == 0
    assert original_local.exists()
    stub_src.restore_from_trash.assert_called_once()


def test_undo_archive_member_check_scoped_to_local(tmp_path: Path) -> None:
    """H5 archive-member rejection must NOT fire on a cloud original_path.

    A cloud path like ``gdrive:foo://bar`` contains no ``::`` today, but the
    scope is intentionally locked to LOCAL entries so a future cloud path
    scheme with ``::`` (unlikely but permissible in the opaque display slot)
    cannot brick undo.

    v0.2 sub-phase 5d: cloud entries now dispatch to ``Source.restore_from_trash``.
    """
    from unittest.mock import MagicMock

    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "gdrive:personal",
                # Deliberately embed the archive separator — this must be
                # ACCEPTED because cloud original_paths are opaque display
                # data, not filesystem targets.
                "original_path": "gdrive:personal://foo::inner",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": _GDRIVE_REAL_ID,
                "cloud_trash_id": _GDRIVE_REAL_ID,
                "etag": f"{_GDRIVE_REAL_ID}:1",
            }
        ],
    )
    stub_src = MagicMock()
    stub_src.id = "gdrive:personal"
    stub_src.restore_from_trash.return_value = None
    # Would previously raise UndoError up-front; the cloud dispatch runs the
    # cloud rails and the entry restores.
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
        sources={"gdrive:personal": stub_src},
    )
    assert result["restored_cloud"] == 1
    assert result["errors"] == []


@pytest.mark.parametrize(
    "poisoned",
    [
        "root:/../etc/passwd",
        "../foo",
        "!\x00nope",
    ],
)
def test_undo_cloud_id_shape_regression(tmp_path: Path, poisoned: str) -> None:
    """Every poisoned cloud_file_id shape variant is rejected symmetrically."""
    manifest = _write_manifest(
        tmp_path,
        entries=[
            {
                "source_id": "gdrive:personal",
                "original_path": "gdrive:personal://foo",
                "size": 8,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
                "cloud_file_id": poisoned,
                "etag": "e:1",
            }
        ],
    )
    result = restore_from_manifest(
        manifest,
        allowed_trash_dirs=[tmp_path],
        registry=_FakeRegistry(["gdrive:personal"]),  # type: ignore[arg-type]
    )
    assert result["restored"] == 0
    assert len(result["errors"]) == 1
