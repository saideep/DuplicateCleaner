"""Tests for ``dc migrate copy`` — v0.5-b execution loop.

Every test injects fake ``Source``-shaped objects so no real network or
filesystem-outside-``tmp_path`` I/O fires.  The tests lock in the safety
envelope: dry-run touches nothing, hash mismatch trashes the destination
BEFORE the manifest advances, drift aborts the whole run, resume respects
prior ``state="done"`` entries, and the manifest is written atomically via
:func:`os.replace`.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from duplicate_cleaner.migrate.mover import (
    MigrationError,
    _throttled_stream,
    execute_migration,
)
from duplicate_cleaner.migrate.plan import (
    MigrationEntry,
    MigrationManifest,
    MigrationPlan,
)
from duplicate_cleaner.sources.base import (
    SourceDriftError,
    TrashedLocation,
    UploadResult,
)


class _FakeSource:
    """Fake Source with configurable behaviour for every method the mover uses."""

    def __init__(
        self,
        source_id: str,
        *,
        is_read_only_scan: bool = False,
    ) -> None:
        self.id = source_id
        self.is_read_only_scan = is_read_only_scan
        self.check_drift = MagicMock(return_value=None)
        self.upload = MagicMock()
        self.move_to_trash = MagicMock(
            return_value=TrashedLocation(
                source_id=source_id,
                original_path="",
                cloud_file_id="trashed",
                cloud_trash_id="trashed",
            )
        )
        self.restore_from_trash = MagicMock(return_value=None)

    def read_bytes(
        self, record: object, chunk_size: int = 1 << 20
    ) -> Iterator[bytes]:
        # Yield a couple of chunks so throttle-count tests see multiple sleeps.
        yield b"hello"
        yield b"world"


def _make_plan(*entries: MigrationEntry) -> MigrationPlan:
    return MigrationPlan(
        source_id="gdrive:src",
        dest_id="gdrive:dst",
        entries=list(entries),
    )


def _copy_entry(
    name: str = "a.pdf",
    *,
    size: int = 10,
    source_hash: str = "md5:" + "a" * 32,
) -> MigrationEntry:
    return MigrationEntry(
        source_id="gdrive:src",
        source_file_id="SRC" + name,
        source_path=f"gdrive:src://{name}",
        source_etag=f"SRC{name}:t0",
        source_size=size,
        source_hash=source_hash,
        source_mime="application/pdf",
        dest_expected_path=name,
        action="copy",
        reason="copy",
    )


def _write_plan(tmp_path: Path, plan: MigrationPlan) -> Path:
    p = tmp_path / "plan.json"
    p.write_text(plan.model_dump_json())
    return p


def test_copy_dry_run_touches_nothing(tmp_path: Path) -> None:
    """Dry-run: no upload, no drift check, no manifest write."""
    plan = _make_plan(_copy_entry())
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    result = execute_migration(
        plan_path,
        manifest_path,
        commit=False,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    assert result.planned == 1
    assert result.copied == 0
    assert not manifest_path.exists()
    src.check_drift.assert_not_called()
    dst.upload.assert_not_called()


def test_copy_commit_uploads_and_manifests(tmp_path: Path) -> None:
    """Commit path: drift-check, upload, manifest with state='done'."""
    plan = _make_plan(_copy_entry(size=10, source_hash="md5:" + "b" * 32))
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    dst.upload.return_value = UploadResult(
        cloud_file_id="DST001",
        etag="DST001:tX",
        uploaded_hash_algo="md5",
        uploaded_hash="b" * 32,
    )
    result = execute_migration(
        plan_path,
        manifest_path,
        commit=True,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    assert result.copied == 1
    assert result.errored == 0
    assert result.committed is True
    assert manifest_path.exists()
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    assert len(manifest.entries) == 1
    e = manifest.entries[0]
    assert e.state == "done"
    assert e.dest_cloud_file_id == "DST001"
    assert e.uploaded_hash == "b" * 32
    assert e.verified is True

    # The upload was handed an iterator that produced the fake bytes.
    args, _ = dst.upload.call_args
    dest_path, byte_stream, size = args
    assert dest_path == "a.pdf"
    assert size == 10
    joined = b"".join(byte_stream)
    assert joined == b"helloworld"


def test_copy_hash_mismatch_trashes_dest(tmp_path: Path) -> None:
    """Same-algo hash mismatch → dest trashed, source untouched, state='error'."""
    plan = _make_plan(_copy_entry(source_hash="md5:" + "a" * 32))
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    dst.upload.return_value = UploadResult(
        cloud_file_id="DST_WRONG",
        etag="DST_WRONG:tX",
        uploaded_hash_algo="md5",
        uploaded_hash="c" * 32,  # different from source_hash
    )
    result = execute_migration(
        plan_path,
        manifest_path,
        commit=True,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    assert result.copied == 0
    assert result.errored == 1
    dst.move_to_trash.assert_called_once()
    trash_arg = dst.move_to_trash.call_args.args[0]
    assert getattr(trash_arg, "cloud_file_id", None) == "DST_WRONG"
    # Source never trashed.
    src.move_to_trash.assert_not_called()
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    e = manifest.entries[0]
    assert e.state == "error"
    assert e.error_message and "hash" in e.error_message.lower()


def test_copy_drift_aborts_run(tmp_path: Path) -> None:
    """Any per-entry SourceDriftError aborts the whole run."""
    plan = _make_plan(
        _copy_entry("a.pdf"),
        _copy_entry("b.pdf"),
    )
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    src.check_drift.side_effect = SourceDriftError("etag drift")
    dst = _FakeSource("gdrive:dst")
    with pytest.raises(MigrationError):
        execute_migration(
            plan_path,
            manifest_path,
            commit=True,
            sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
        )
    # Upload was never attempted for either entry.
    dst.upload.assert_not_called()


def test_copy_resume_from_manifest_skips_done(tmp_path: Path) -> None:
    """Entries flagged state='done' in --resume-from are skipped."""
    entry_a = _copy_entry("a.pdf")
    entry_b = _copy_entry("b.pdf")
    plan = _make_plan(entry_a, entry_b)
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"

    # Build a prior manifest that marks entry a as done.
    from duplicate_cleaner.migrate.mover import _plan_entry_to_manifest_entry
    prior = MigrationManifest(
        plan_source_id="gdrive:src",
        plan_dest_id="gdrive:dst",
    )
    done_a = _plan_entry_to_manifest_entry(entry_a.model_dump()).model_copy(
        update={
            "state": "done",
            "verified": True,
            "dest_cloud_file_id": "PRIOR_A",
        }
    )
    prior.entries.append(done_a)
    resume_path = tmp_path / "prior.json"
    resume_path.write_text(prior.model_dump_json())

    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    dst.upload.return_value = UploadResult(
        cloud_file_id="DST_B",
        etag="DST_B:tX",
        uploaded_hash_algo="md5",
        uploaded_hash="a" * 32,
    )
    result = execute_migration(
        plan_path,
        manifest_path,
        commit=True,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
        resume_from=resume_path,
    )
    # a.pdf → resumed (skipped this run); b.pdf → copied.
    assert result.copied == 1
    dst.upload.assert_called_once()
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    states = {e.source_path: e.state for e in manifest.entries}
    assert states["gdrive:src://a.pdf"] == "skipped"
    assert states["gdrive:src://b.pdf"] == "done"


def test_copy_bandwidth_throttle_sleeps() -> None:
    """The throttle wrapper sleeps between chunks proportional to size."""
    sleeps: list[float] = []

    def _sleep(t: float) -> None:
        sleeps.append(t)

    def _chunks() -> Iterator[bytes]:
        yield b"a" * 100
        yield b"b" * 200

    out = list(
        _throttled_stream(_chunks(), max_mbps=1.0, sleep_fn=_sleep)
    )
    assert out == [b"a" * 100, b"b" * 200]
    # 100 B = 800 bits at 1 Mbps → 800 / 1e6 = 0.0008 s
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(100 * 8 / 1_000_000.0)
    assert sleeps[1] == pytest.approx(200 * 8 / 1_000_000.0)


def test_copy_deferred_entry_marked_skipped(tmp_path: Path) -> None:
    """Plan entries with action='defer' land as state='skipped' — no upload."""
    plan = MigrationPlan(
        source_id="gdrive:src",
        dest_id="gdrive:dst",
        entries=[
            MigrationEntry(
                source_id="gdrive:src",
                source_path="gdrive:src://shared.pdf",
                source_size=10,
                dest_expected_path="shared.pdf",
                action="defer",
                reason="shared with me",
            )
        ],
    )
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    result = execute_migration(
        plan_path,
        manifest_path,
        commit=True,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    dst.upload.assert_not_called()
    assert result.deferred == 1
    assert result.copied == 0
    manifest = MigrationManifest.model_validate_json(manifest_path.read_text())
    assert manifest.entries[0].state == "skipped"


def test_copy_manifest_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-run ``os.replace`` failure leaves the target manifest untouched.

    Locks in the atomic-write invariant: a failed rename does NOT half-write
    the destination file — the previous (or absent) manifest stays in place.
    """
    plan = _make_plan(_copy_entry())
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    dst.upload.return_value = UploadResult(
        cloud_file_id="DST_ATOMIC",
        etag="DST_ATOMIC:t",
        uploaded_hash_algo="md5",
        uploaded_hash="a" * 32,
    )

    calls = {"n": 0}
    real_replace = __import__("os").replace

    def _flaky_replace(a: str, b: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated replace failure")
        real_replace(a, b)

    monkeypatch.setattr("os.replace", _flaky_replace)
    with pytest.raises(OSError):
        execute_migration(
            plan_path,
            manifest_path,
            commit=True,
            sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
        )
    # Manifest was not partially materialised — the tempfile got fsynced
    # but the rename failed, so ``manifest_path`` never came into existence.
    assert not manifest_path.exists()


def test_copy_refuses_read_only_dest(tmp_path: Path) -> None:
    """A destination constructed with is_read_only_scan=True is rejected."""
    plan = _make_plan(_copy_entry())
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst", is_read_only_scan=True)
    with pytest.raises(MigrationError):
        execute_migration(
            plan_path,
            manifest_path,
            commit=True,
            sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
        )


def test_copy_missing_source_in_map(tmp_path: Path) -> None:
    """A source_id absent from sources_by_id raises MigrationError up-front."""
    plan = _make_plan(_copy_entry())
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    with pytest.raises(MigrationError):
        execute_migration(
            plan_path,
            manifest_path,
            commit=True,
            sources_by_id={},
        )


def test_copy_manifest_json_shape(tmp_path: Path) -> None:
    """Manifest JSON round-trips through MigrationManifest cleanly."""
    plan = _make_plan(_copy_entry())
    plan_path = _write_plan(tmp_path, plan)
    manifest_path = tmp_path / "manifest.json"
    src = _FakeSource("gdrive:src")
    dst = _FakeSource("gdrive:dst")
    dst.upload.return_value = UploadResult(
        cloud_file_id="X",
        etag="X:t",
        uploaded_hash_algo="md5",
        uploaded_hash="a" * 32,
    )
    execute_migration(
        plan_path,
        manifest_path,
        commit=True,
        sources_by_id={"gdrive:src": src, "gdrive:dst": dst},
    )
    raw = json.loads(manifest_path.read_text())
    assert raw["manifest_version"] == "0.5.0"
    assert raw["plan_source_id"] == "gdrive:src"
    assert raw["plan_dest_id"] == "gdrive:dst"
