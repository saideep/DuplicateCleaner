"""v0.2 sub-phase 5c — mover dispatches cloud discards through Source.move_to_trash.

Tests the wire-up between ``apply_report`` and per-source
:meth:`Source.check_drift` + :meth:`Source.move_to_trash`.  Every cloud
source in these tests is a ``MagicMock`` — no real network, no real
provider SDK.  The invariants under test are:

* Cloud entries never reach ``send2trash`` or ``resolve_for_check``.
* Missing source_id in the sources map raises ``ApplyError`` up-front.
* ``is_read_only_scan=True`` trips BEFORE ``check_drift`` fires.
* Drift → ``ApplyError`` (entire run aborts, same as local drift).
* Manifest carries every cloud metadata field per row.
* Mixed local + cloud reports produce both entry shapes in one manifest.
* Shared cloud discards are refused (invariant).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.apply.mover import ApplyError, apply_report
from duplicate_cleaner.auth.accounts import AccountEntry
from duplicate_cleaner.report.schema import Report, ReportGroup, ReportMember
from duplicate_cleaner.sources.base import (
    SourceDriftError,
    TrashedLocation,
)

# Realistic Google Drive id — Base64url, ≥20 chars — matches the shape regex
# in ``paths.validate_cloud_entry``.
_GDRIVE_REAL_ID = "1AbCdEfGhIjKlMnOpQrSt"
_GDRIVE_REAL_ID_2 = "2ZyXwVuTsRqPoNmLkJiHg"


class _FakeRegistry:
    """Minimal ``AccountsRegistry.load()`` stub — no on-disk accounts.toml."""

    def __init__(self, ids: list[str]) -> None:
        self._entries = [
            AccountEntry(id=i, type="gdrive", label=i, user="test", added_ts="")
            for i in ids
        ]

    def load(self) -> list[AccountEntry]:
        return self._entries


def _make_cloud_source_mock(
    account_id: str = "gdrive:personal",
    *,
    is_read_only_scan: bool = False,
    move_side_effect: Any = None,
    drift_side_effect: Any = None,
) -> MagicMock:
    """Build a MagicMock source that mimics :class:`Source` shape.

    ``move_to_trash`` returns a plausible ``TrashedLocation`` by default;
    tests that want a specific return value override ``move_side_effect``.
    """
    src = MagicMock()
    src.id = account_id
    src.is_read_only_scan = is_read_only_scan
    if drift_side_effect is not None:
        src.check_drift.side_effect = drift_side_effect
    if move_side_effect is not None:
        src.move_to_trash.side_effect = move_side_effect
    else:
        src.move_to_trash.return_value = TrashedLocation(
            source_id=account_id,
            original_path=f"{account_id}://foo",
            cloud_file_id=_GDRIVE_REAL_ID,
            cloud_trash_id=_GDRIVE_REAL_ID,
        )
    return src


def _mkreport(
    tmp_path: Path,
    cloud_members: list[dict[str, Any]] | None = None,
    local_discard_names: list[str] | None = None,
) -> Path:
    """Build a report at ``tmp_path/report.json``.

    ``cloud_members`` — one dict per cloud discard, spread onto ReportMember.
    ``local_discard_names`` — file basenames to create & mark as discards.
    A single local keeper is always present so ``report.roots`` is non-empty.
    """
    keeper = tmp_path / "keep.bin"
    keeper.write_bytes(b"x" * 8)
    members: list[ReportMember] = [
        ReportMember(
            path=keeper,
            size=keeper.stat().st_size,
            mtime=keeper.stat().st_mtime,
            hash="H" * 32,
            score=1.0,
            signals=[],
            is_proposed_keeper=True,
        )
    ]
    for name in local_discard_names or []:
        p = tmp_path / name
        p.write_bytes(b"x" * 8)
        members.append(
            ReportMember(
                path=p,
                size=p.stat().st_size,
                mtime=p.stat().st_mtime,
                hash="H" * 32,
                score=0.0,
                signals=[],
                is_proposed_keeper=False,
            )
        )
    for cm in cloud_members or []:
        # Provide sensible defaults so each test only overrides what varies.
        defaults: dict[str, Any] = {
            "path": Path("gdrive:personal://My Drive/foo.bin"),
            "size": 8,
            "mtime": 1000.0,
            "hash": "H" * 32,
            "score": -1.0,
            "signals": [],
            "is_proposed_keeper": False,
            "source_id": "gdrive:personal",
            "cloud_file_id": _GDRIVE_REAL_ID,
            "etag": f"{_GDRIVE_REAL_ID}:2026-09-05T12:00:00Z",
            "is_shared": False,
        }
        defaults.update(cm)
        members.append(ReportMember(**defaults))

    report = Report(
        roots=[tmp_path],
        total_files_scanned=len(members),
        total_groups=1,
        total_reclaim_bytes=8,
        groups=[
            ReportGroup(
                id="g1",
                hash="H" * 32,
                size=8,
                reclaim_bytes=8,
                members=members,
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())
    return p


def test_apply_cloud_entry_calls_source_move_to_trash(tmp_path: Path) -> None:
    """A cloud discard routes through ``Source.check_drift`` + ``move_to_trash``."""
    report_path = _mkreport(
        tmp_path,
        cloud_members=[{}],  # default cloud member
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    src = _make_cloud_source_mock("gdrive:personal")
    result = apply_report(
        report_path,
        commit=True,
        runs_dir=tmp_path / "runs",
        registry=reg,  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    assert result["committed"] is True
    assert result["moved_cloud"] == 1
    assert result["moved_local"] == 0
    src.check_drift.assert_called_once()
    src.move_to_trash.assert_called_once()
    # The FileRecord passed to move_to_trash carries the report's fields.
    (record,), _kwargs = src.move_to_trash.call_args
    assert record.cloud_file_id == _GDRIVE_REAL_ID
    assert record.source_id == "gdrive:personal"
    assert record.etag == f"{_GDRIVE_REAL_ID}:2026-09-05T12:00:00Z"


def test_apply_cloud_entry_refused_when_source_not_in_map(tmp_path: Path) -> None:
    """Missing source_id in the sources map → ApplyError up-front, no I/O."""
    report_path = _mkreport(tmp_path, cloud_members=[{}])
    reg = _FakeRegistry(ids=["gdrive:personal"])
    # sources map lists a DIFFERENT account_id — the mover must refuse.
    other = _make_cloud_source_mock("gdrive:other")
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=True,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
            sources={"gdrive:other": other},
        )
    msg = str(exc.value)
    assert "gdrive:personal" in msg
    # No source method was called.
    assert other.check_drift.call_count == 0
    assert other.move_to_trash.call_count == 0


def test_apply_cloud_entry_drift_check_fires(tmp_path: Path) -> None:
    """SourceDriftError from check_drift aborts the whole apply run."""
    report_path = _mkreport(tmp_path, cloud_members=[{}])
    reg = _FakeRegistry(ids=["gdrive:personal"])
    src = _make_cloud_source_mock(
        "gdrive:personal",
        drift_side_effect=SourceDriftError("etag drift"),
    )
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=True,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
            sources={"gdrive:personal": src},
        )
    assert "drift" in str(exc.value).lower()
    # move_to_trash never fired — drift check gated the call.
    assert src.move_to_trash.call_count == 0


def test_apply_cloud_manifest_records_source_metadata(tmp_path: Path) -> None:
    """Manifest entry carries source_id, cloud_file_id, cloud_trash_id, etag."""
    scan_etag = f"{_GDRIVE_REAL_ID}:2026-09-05T12:00:00Z"
    report_path = _mkreport(
        tmp_path,
        cloud_members=[
            {
                "source_id": "gdrive:personal",
                "cloud_file_id": _GDRIVE_REAL_ID,
                "etag": scan_etag,
                "path": Path("gdrive:personal://Documents/report.pdf"),
            }
        ],
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    src = _make_cloud_source_mock("gdrive:personal")
    result = apply_report(
        report_path,
        commit=True,
        runs_dir=tmp_path / "runs",
        registry=reg,  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    manifest_path = Path(result["manifest_path"])
    data = json.loads(manifest_path.read_text())
    entries = data["entries"]
    assert len(entries) == 1
    row = entries[0]
    assert row["source_id"] == "gdrive:personal"
    assert row["cloud_file_id"] == _GDRIVE_REAL_ID
    # Default mock returns cloud_trash_id == cloud_file_id (Drive semantics).
    assert row["cloud_trash_id"] == _GDRIVE_REAL_ID
    assert row["etag"] == scan_etag
    # Original path is the cloud display string, unchanged.
    assert row["original_path"] == "gdrive:personal:/Documents/report.pdf" or \
        row["original_path"] == "gdrive:personal://Documents/report.pdf"
    # trashed_at_path stays null for cloud entries.
    assert row["trashed_at_path"] is None


def test_apply_mixed_local_and_cloud(tmp_path: Path) -> None:
    """2 local + 3 cloud entries — each dispatch goes to the correct channel."""
    report_path = _mkreport(
        tmp_path,
        local_discard_names=["discard_a.bin", "discard_b.bin"],
        cloud_members=[
            {
                "cloud_file_id": _GDRIVE_REAL_ID,
                "etag": f"{_GDRIVE_REAL_ID}:t1",
                "path": Path("gdrive:personal://c1.bin"),
            },
            {
                "cloud_file_id": _GDRIVE_REAL_ID_2,
                "etag": f"{_GDRIVE_REAL_ID_2}:t2",
                "path": Path("gdrive:personal://c2.bin"),
            },
            {
                "cloud_file_id": "3XyZw" + "A" * 16,  # 21 chars, all base64url
                "etag": "3XyZw" + "A" * 16 + ":t3",
                "path": Path("gdrive:personal://c3.bin"),
            },
        ],
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    src = _make_cloud_source_mock("gdrive:personal")

    # Fake local trash — copy to a temp dir so we don't touch ~/.Trash.
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()

    def trash_fn(p: Path) -> Path:
        import shutil

        dest = trash_dir / p.name
        shutil.move(str(p), str(dest))
        return dest

    result = apply_report(
        report_path,
        commit=True,
        runs_dir=tmp_path / "runs",
        trash_fn=trash_fn,
        registry=reg,  # type: ignore[arg-type]
        sources={"gdrive:personal": src},
    )
    assert result["moved_local"] == 2
    assert result["moved_cloud"] == 3
    assert result["moved"] == 5
    # Cloud dispatch: 3 check_drift + 3 move_to_trash calls.
    assert src.check_drift.call_count == 3
    assert src.move_to_trash.call_count == 3
    # Local dispatch: 2 files landed in the fake trash dir.
    assert (trash_dir / "discard_a.bin").exists()
    assert (trash_dir / "discard_b.bin").exists()
    # Manifest carries both shapes.
    data = json.loads(Path(result["manifest_path"]).read_text())
    local_rows = [e for e in data["entries"] if e["source_id"] == "local"]
    cloud_rows = [e for e in data["entries"] if e["source_id"] != "local"]
    assert len(local_rows) == 2
    assert len(cloud_rows) == 3
    for r in local_rows:
        assert r["cloud_file_id"] is None
        assert r["trashed_at_path"] is not None
    for r in cloud_rows:
        assert r["cloud_file_id"] is not None
        assert r["cloud_trash_id"] is not None
        assert r["etag"] is not None


def test_apply_cloud_shared_file_refused(tmp_path: Path) -> None:
    """A cloud discard with is_shared=True hits the invariant at validate time."""
    report_path = _mkreport(
        tmp_path,
        cloud_members=[
            {"is_shared": True},
        ],
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    src = _make_cloud_source_mock("gdrive:personal")
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=True,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
            sources={"gdrive:personal": src},
        )
    assert "shared" in str(exc.value).lower()
    assert src.check_drift.call_count == 0
    assert src.move_to_trash.call_count == 0


def test_apply_read_only_source_refused(tmp_path: Path) -> None:
    """is_read_only_scan=True on a source in the map trips BEFORE any HTTP call."""
    report_path = _mkreport(tmp_path, cloud_members=[{}])
    reg = _FakeRegistry(ids=["gdrive:personal"])
    ro_src = _make_cloud_source_mock(
        "gdrive:personal", is_read_only_scan=True
    )
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=True,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
            sources={"gdrive:personal": ro_src},
        )
    msg = str(exc.value).lower()
    assert "read-only" in msg or "read_only" in msg or "is_read_only" in msg
    # Tripwire fired BEFORE any drift check.
    assert ro_src.check_drift.call_count == 0
    assert ro_src.move_to_trash.call_count == 0


def test_apply_cloud_entry_local_only_mode_refuses(tmp_path: Path) -> None:
    """sources=None with any cloud entry raises ApplyError up-front."""
    report_path = _mkreport(tmp_path, cloud_members=[{}])
    reg = _FakeRegistry(ids=["gdrive:personal"])
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=True,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
            sources=None,
        )
    assert "local-only" in str(exc.value).lower() or "sources" in str(exc.value)
