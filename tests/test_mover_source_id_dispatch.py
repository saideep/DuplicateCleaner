"""v0.2 sub-phase 5b — mover dispatches by ``source_id`` (Alt-C).

Every proposed-discard member routes through one of two branches BEFORE any
local filesystem check runs:

* ``source_id == "local"`` — original v0.1.1 rails (resolve → exclusions →
  containment against report roots).
* Anything else — cloud rails: ``source_id`` must be in
  ``AccountsRegistry``, ``cloud_file_id`` must match the per-provider shape
  regex, ``etag`` must be present, ``is_shared`` must be False.

The cloud ``Path`` is NEVER ``.resolve()``d — see AUDIT_LOG "Cloud discards
go to cloud trash" invariant and design doc §4.2.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from duplicate_cleaner.apply.mover import ApplyError, apply_report
from duplicate_cleaner.auth.accounts import AccountEntry
from duplicate_cleaner.report.schema import Report, ReportGroup, ReportMember

# Realistic Google Drive id — Base64url, ≥20 chars — matches the shape regex
# in ``paths.validate_cloud_entry``.
_GDRIVE_REAL_ID = "1AbCdEfGhIjKlMnOpQrSt"


class _FakeRegistry:
    """Minimal registry stub — mirrors ``AccountsRegistry.load()`` for tests.

    The mover only touches ``.load()`` at validate time; this keeps the test
    self-contained (no config-dir writes, no on-disk accounts.toml).
    """

    def __init__(self, ids: list[str]) -> None:
        self._entries = [
            AccountEntry(id=i, type="gdrive", label=i, user="test", added_ts="")
            for i in ids
        ]

    def load(self) -> list[AccountEntry]:
        return self._entries


def _mkreport(tmp_path: Path, cloud_member_kwargs: dict[str, Any]) -> Path:
    """Build a report with one local keeper + one cloud discard.

    ``cloud_member_kwargs`` is spread onto the ``ReportMember(...)`` call for
    the discard so each test can vary a single field (missing cloud_file_id,
    is_shared=True, ...).
    """
    keeper = tmp_path / "keep.bin"
    keeper.write_bytes(b"x" * 8)

    disc_member = ReportMember(
        path=Path("gdrive:personal://My Drive/foo.bin"),
        size=8,
        mtime=1000.0,
        hash="H" * 32,
        score=-1.0,
        signals=[],
        is_proposed_keeper=False,
        **cloud_member_kwargs,
    )

    report = Report(
        roots=[tmp_path],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=8,
        groups=[
            ReportGroup(
                id="g1",
                hash="H" * 32,
                size=8,
                reclaim_bytes=8,
                members=[
                    ReportMember(
                        path=keeper,
                        size=keeper.stat().st_size,
                        mtime=keeper.stat().st_mtime,
                        hash="H" * 32,
                        score=1.0,
                        signals=[],
                        is_proposed_keeper=True,
                    ),
                    disc_member,
                ],
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())
    return p


def test_local_entry_uses_local_rails(tmp_path: Path) -> None:
    """A pure-local report continues to flow through the v0.1.1 rails."""
    keep = tmp_path / "keep.bin"
    disc = tmp_path / "disc.bin"
    keep.write_bytes(b"x" * 8)
    disc.write_bytes(b"x" * 8)
    report = Report(
        roots=[tmp_path],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=8,
        groups=[
            ReportGroup(
                id="g1",
                hash="H" * 32,
                size=8,
                reclaim_bytes=8,
                members=[
                    ReportMember(
                        path=keep,
                        size=keep.stat().st_size,
                        mtime=keep.stat().st_mtime,
                        hash="H" * 32,
                        score=1.0,
                        signals=[],
                        is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=disc,
                        size=disc.stat().st_size,
                        mtime=disc.stat().st_mtime,
                        hash="H" * 32,
                        score=0.0,
                        signals=[],
                        is_proposed_keeper=False,
                    ),
                ],
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())

    result = apply_report(p, commit=False, runs_dir=tmp_path / "runs")
    assert result["planned"] == 1
    assert result["verified"] == 1
    assert result["cloud_deferred"] == 0


def test_cloud_entry_refused_unknown_source_id(tmp_path: Path) -> None:
    """A ``source_id`` absent from ``AccountsRegistry`` refuses the discard."""
    report_path = _mkreport(
        tmp_path,
        cloud_member_kwargs={
            "source_id": "gdrive:evil",
            "cloud_file_id": _GDRIVE_REAL_ID,
            "etag": f"{_GDRIVE_REAL_ID}:1",
            "is_shared": False,
        },
    )
    reg = _FakeRegistry(ids=["gdrive:known"])
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
        )
    msg = str(exc.value)
    assert "gdrive:evil" in msg
    assert "AccountsRegistry" in msg or "not registered" in msg


def test_cloud_entry_refused_missing_cloud_file_id(tmp_path: Path) -> None:
    """A cloud discard with no ``cloud_file_id`` cannot be dispatched."""
    report_path = _mkreport(
        tmp_path,
        cloud_member_kwargs={
            "source_id": "gdrive:personal",
            "cloud_file_id": None,
            "etag": "some:etag",
            "is_shared": False,
        },
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
        )
    assert "cloud_file_id" in str(exc.value)


def test_cloud_entry_refused_bad_cloud_file_id_shape(tmp_path: Path) -> None:
    """A poisoned cloud_file_id (path traversal) refuses the discard."""
    report_path = _mkreport(
        tmp_path,
        cloud_member_kwargs={
            "source_id": "gdrive:personal",
            "cloud_file_id": "a/b/c",
            "etag": "a/b/c:1",
            "is_shared": False,
        },
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
        )
    assert "shape" in str(exc.value) or "gdrive" in str(exc.value)


def test_cloud_entry_refused_missing_etag(tmp_path: Path) -> None:
    """A cloud discard with no ``etag`` refuses — required for 5c drift check."""
    report_path = _mkreport(
        tmp_path,
        cloud_member_kwargs={
            "source_id": "gdrive:personal",
            "cloud_file_id": _GDRIVE_REAL_ID,
            "etag": None,
            "is_shared": False,
        },
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
        )
    assert "etag" in str(exc.value)


def test_cloud_entry_refused_when_is_shared_true(tmp_path: Path) -> None:
    """The 'shared cloud files informational-only' invariant blocks discards."""
    report_path = _mkreport(
        tmp_path,
        cloud_member_kwargs={
            "source_id": "gdrive:personal",
            "cloud_file_id": _GDRIVE_REAL_ID,
            "etag": f"{_GDRIVE_REAL_ID}:1",
            "is_shared": True,
        },
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])
    with pytest.raises(ApplyError) as exc:
        apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
        )
    assert "shared" in str(exc.value).lower()


def test_gphotos_cloud_file_id_shape_validated_by_mover(tmp_path: Path) -> None:
    """v0.6-patch M3: mover-level cloud_file_id shape gate covers gphotos.

    Before v0.6-patch ``_PROVIDER_ID_PATTERNS`` had entries only for gdrive
    and onedrive; a poisoned report with
    ``source_id='gphotos:personal', cloud_file_id='../evil'`` silently
    passed the mover's validation.  The source-side gate would still
    catch it before a URL was interpolated, but the mover-level rail is
    load-bearing once v0.6.1 wires trash on the source side.
    """
    keeper = tmp_path / "keep.bin"
    keeper.write_bytes(b"x" * 8)

    disc_member = ReportMember(
        path=Path("gphotos:personal://foo.HEIC"),
        size=8,
        mtime=1000.0,
        hash="H" * 32,
        score=-1.0,
        signals=[],
        is_proposed_keeper=False,
        source_id="gphotos:personal",
        cloud_file_id="../evil/foo",
        etag="e:1",
        is_shared=False,
    )
    report = Report(
        roots=[tmp_path],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=8,
        groups=[
            ReportGroup(
                id="g1",
                hash="H" * 32,
                size=8,
                reclaim_bytes=8,
                members=[
                    ReportMember(
                        path=keeper,
                        size=keeper.stat().st_size,
                        mtime=keeper.stat().st_mtime,
                        hash="H" * 32,
                        score=1.0,
                        signals=[],
                        is_proposed_keeper=True,
                    ),
                    disc_member,
                ],
            )
        ],
    )
    p = tmp_path / "report.json"
    p.write_text(report.model_dump_json())

    from unittest.mock import MagicMock

    stub_src = MagicMock()
    stub_src.is_read_only_scan = False
    reg = _FakeRegistry(ids=["gphotos:personal"])
    with pytest.raises(ApplyError) as exc:
        apply_report(
            p,
            commit=False,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
            sources={"gphotos:personal": stub_src},
        )
    # The gphotos shape regex fires at the mover level (paths.py).
    assert "shape" in str(exc.value) or "gphotos" in str(exc.value)


def test_cloud_path_never_resolved(tmp_path: Path) -> None:
    """Alt-C invariant: cloud ``Path`` is never ``.resolve()``d in the mover.

    Patches ``paths.resolve_for_check`` to record every call — no cloud
    ``Path`` may reach it.  The local keeper's real path still routes through
    the local rails (which DO resolve), so the patch record must contain
    only local filesystem paths.

    v0.2 sub-phase 5c: the mover now requires a ``sources`` map to dispatch
    cloud discards.  We supply a MagicMock source with
    ``is_read_only_scan=False`` — the dry-run (commit=False) path does NOT
    call ``check_drift`` or ``move_to_trash`` on it, but the pre-flight
    tripwire is exercised.
    """
    from unittest.mock import MagicMock

    report_path = _mkreport(
        tmp_path,
        cloud_member_kwargs={
            "source_id": "gdrive:personal",
            "cloud_file_id": _GDRIVE_REAL_ID,
            "etag": f"{_GDRIVE_REAL_ID}:1",
            "is_shared": False,
        },
    )
    reg = _FakeRegistry(ids=["gdrive:personal"])

    from duplicate_cleaner.apply import mover as mover_mod

    resolve_calls: list[Path] = []
    original = mover_mod.resolve_for_check

    def _spy(p: Path) -> Path:
        resolve_calls.append(p)
        return original(p)

    # Stub source — dry-run does not call check_drift / move_to_trash.
    stub_src = MagicMock()
    stub_src.is_read_only_scan = False

    with patch.object(mover_mod, "resolve_for_check", side_effect=_spy):
        result = apply_report(
            report_path,
            commit=False,
            runs_dir=tmp_path / "runs",
            registry=reg,  # type: ignore[arg-type]
            sources={"gdrive:personal": stub_src},
        )

    # No call recorded a cloud path (identified by the "gdrive:" prefix).
    for p in resolve_calls:
        assert "gdrive:" not in str(p), (
            f"resolve_for_check called on cloud path {p!r} — Alt-C violated"
        )
    # Sub-phase 5c: cloud dispatch is wired; the cloud discard is planned.
    assert result["cloud_deferred"] == 0
    assert result["planned_cloud"] == 1
    # The local keeper was NOT a discard, so plan_moves emits nothing local.
    assert result["planned_local"] == 0
    # ``planned`` is now the sum of local + cloud.
    assert result["planned"] == 1
    # Dry-run: nothing actually moved.
    assert stub_src.check_drift.call_count == 0
    assert stub_src.move_to_trash.call_count == 0
