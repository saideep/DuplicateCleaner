"""Migration planner — enumerate source, classify, produce a MigrationPlan.

v0.5-a coverage: planner logic + JSON/HTML round-trip.  Copy/verify/cleanup
land in v0.5-b.  No network is touched; every test uses fake Source-like
objects that yield a pre-canned FileRecord stream.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from duplicate_cleaner.migrate.plan import (
    MIGRATE_PLAN_VERSION,
    ONEDRIVE_MAX_FILE_SIZE_BYTES,
    MigrationFilter,
    MigrationPlan,
)
from duplicate_cleaner.migrate.planner import plan_migration
from duplicate_cleaner.migrate.render import render_migration_plan
from duplicate_cleaner.scan.walk import FileRecord


class _FakeSource:
    """Minimal Source-shape carrier for the planner's read-only needs."""

    def __init__(self, source_id: str, records: list[FileRecord]) -> None:
        self.id = source_id
        self.is_read_only_scan = True
        self._records = list(records)

    def list_files(self) -> Iterator[FileRecord]:
        yield from self._records


def _rec(
    source_id: str,
    name: str,
    *,
    size: int = 100,
    foreign_hash: str = "md5:" + "a" * 32,
    is_shared: bool = False,
    cloud_file_id: str = "cf001",
    mime: str = "application/pdf",
) -> FileRecord:
    """Build a cloud-shaped FileRecord for the fake source."""
    virtual = f"{source_id}://{name}"
    rec = FileRecord(
        path=Path(virtual),
        size=size,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id=source_id,
        foreign_hash=foreign_hash,
        etag=f"{cloud_file_id}:t",
        cloud_file_id=cloud_file_id,
        owner="me@example.com",
        is_shared=is_shared,
    )
    # ``FileRecord`` is frozen; attach ``mime_type`` via a wrapper subclass
    # if the planner ever gains a mime accessor.  For now the planner reads
    # via ``getattr(rec, "mime_type", None)`` which returns None.
    _ = mime
    return rec


def test_plan_enumerates_source_files() -> None:
    """Every eligible source file becomes exactly one plan entry."""
    src = _FakeSource(
        "gdrive:personal",
        [
            _rec("gdrive:personal", "a.pdf", size=100, cloud_file_id="A" * 22),
            _rec("gdrive:personal", "b.pdf", size=200, cloud_file_id="B" * 22),
            _rec("gdrive:personal", "c.pdf", size=300, cloud_file_id="C" * 22),
        ],
    )
    dst = _FakeSource("onedrive:main", [])
    plan = plan_migration(src, dst)
    assert len(plan.entries) == 3
    assert plan.source_id == "gdrive:personal"
    assert plan.dest_id == "onedrive:main"
    assert plan.plan_version == MIGRATE_PLAN_VERSION
    # Every one should be a copy (nothing on dest, no shared, no google-native).
    assert all(e.action == "copy" for e in plan.entries)


def test_plan_defers_shared_files() -> None:
    """Shared cloud files carry the informational-only invariant."""
    src = _FakeSource(
        "gdrive:personal",
        [
            _rec(
                "gdrive:personal",
                "own.pdf",
                cloud_file_id="OWN" + "X" * 19,
                is_shared=False,
            ),
            _rec(
                "gdrive:personal",
                "shared.pdf",
                cloud_file_id="SHARED" + "X" * 16,
                is_shared=True,
            ),
        ],
    )
    dst = _FakeSource("onedrive:main", [])
    plan = plan_migration(src, dst)
    actions = {e.source_path: e.action for e in plan.entries}
    assert actions["gdrive:personal:/own.pdf"] == "copy"
    assert actions["gdrive:personal:/shared.pdf"] == "defer"
    shared = next(e for e in plan.entries if e.action == "defer")
    assert "shared" in shared.reason.lower()


def test_plan_defers_google_native() -> None:
    """A Google-native mime prefix → defer, no downloadable bytes."""
    # google-native records already have foreign_hash=None; construct one
    # via a FileRecord subclass wrapper by directly instantiating with a
    # mime_type attribute injected onto the class.
    class _NativeRecord(FileRecord):
        """FileRecord with an attached mime_type for planner consumption."""

    rec = FileRecord(
        path=Path("gdrive:personal://Doc"),
        size=0,
        mtime=0.0,
        inode=0,
        dev=0,
        nlink=1,
        source_id="gdrive:personal",
        foreign_hash=None,
        etag="native:t",
        cloud_file_id="NATIVE" + "X" * 16,
        owner="me@example.com",
        is_shared=False,
    )
    # Attach mime_type dynamically via object.__setattr__ (FileRecord is
    # frozen).  In real code the future FileRecord.mime_type field would
    # be populated by ``sources.gdrive._item_to_record``.
    object.__setattr__(
        rec, "mime_type", "application/vnd.google-apps.document"
    )
    src = _FakeSource("gdrive:personal", [rec])
    dst = _FakeSource("onedrive:main", [])
    plan = plan_migration(src, dst)
    assert len(plan.entries) == 1
    entry = plan.entries[0]
    assert entry.action == "defer"
    assert "google-native" in entry.reason.lower()


def test_plan_marks_dest_hit_as_skip() -> None:
    """When the destination already has a file at the expected path with
    matching size + hash, the entry becomes ``action="skip"``.
    """
    same_hash = "md5:" + "b" * 32
    src = _FakeSource(
        "gdrive:personal",
        [
            _rec(
                "gdrive:personal",
                "Photos/2024/img.jpg",
                size=1024,
                foreign_hash=same_hash,
                cloud_file_id="SRC" + "X" * 19,
            ),
        ],
    )
    # Destination has the SAME relative path + size + hash.
    dst = _FakeSource(
        "gdrive:family",
        [
            _rec(
                "gdrive:family",
                "Photos/2024/img.jpg",
                size=1024,
                foreign_hash=same_hash,
                cloud_file_id="DST" + "X" * 19,
            ),
        ],
    )
    plan = plan_migration(src, dst)
    assert len(plan.entries) == 1
    assert plan.entries[0].action == "skip"
    assert "already" in plan.entries[0].reason.lower()


def test_plan_marks_over_size_limit_as_error() -> None:
    """A source file larger than the OneDrive per-file cap → error."""
    src = _FakeSource(
        "gdrive:personal",
        [
            _rec(
                "gdrive:personal",
                "huge.iso",
                size=ONEDRIVE_MAX_FILE_SIZE_BYTES + 1,
                cloud_file_id="HUGE" + "X" * 18,
            ),
        ],
    )
    dst = _FakeSource("onedrive:main", [])
    plan = plan_migration(src, dst)
    assert len(plan.entries) == 1
    entry = plan.entries[0]
    assert entry.action == "error"
    assert entry.size_limit_hit is True
    assert "exceeds" in entry.reason.lower()


def test_plan_respects_include_globs() -> None:
    """Only PDFs in the plan when include_globs = ['**/*.pdf']."""
    src = _FakeSource(
        "gdrive:personal",
        [
            _rec(
                "gdrive:personal",
                "a.pdf",
                cloud_file_id="A" * 22,
            ),
            _rec(
                "gdrive:personal",
                "b.txt",
                cloud_file_id="B" * 22,
            ),
            _rec(
                "gdrive:personal",
                "c.pdf",
                cloud_file_id="C" * 22,
            ),
        ],
    )
    dst = _FakeSource("onedrive:main", [])
    filt = MigrationFilter(include_globs=["*.pdf", "**/*.pdf"])
    plan = plan_migration(src, dst, filter_=filt)
    paths = {e.source_path for e in plan.entries}
    assert paths == {
        "gdrive:personal:/a.pdf",
        "gdrive:personal:/c.pdf",
    }


def test_plan_refuses_self_copy() -> None:
    """Audit pass 15 finding #5: plan_migration refuses when source.id == dest.id.

    Same-account migration cannot cross an ownership boundary and would
    burn quota on a self-copy.  ``plan_migration`` raises ``ValueError``
    up-front — the fake sources' ``list_files`` never fires.
    """
    src = _FakeSource(
        "gdrive:personal",
        [_rec("gdrive:personal", "a.pdf", cloud_file_id="A" * 22)],
    )
    dst = _FakeSource("gdrive:personal", [])
    with pytest.raises(ValueError, match="same-account"):
        plan_migration(src, dst)


def test_plan_produces_valid_json_and_html(tmp_path: Path) -> None:
    """render_migration_plan writes both files and JSON round-trips."""
    src = _FakeSource(
        "gdrive:personal",
        [
            _rec(
                "gdrive:personal",
                "one.pdf",
                cloud_file_id="X" * 22,
            ),
        ],
    )
    dst = _FakeSource("onedrive:main", [])
    plan = plan_migration(src, dst)
    html_path, json_path = render_migration_plan(plan, tmp_path / "reports")
    assert html_path.exists()
    assert json_path.exists()
    round_trip = MigrationPlan.model_validate_json(json_path.read_text())
    assert round_trip.source_id == plan.source_id
    assert round_trip.dest_id == plan.dest_id
    assert len(round_trip.entries) == 1
    html_text = html_path.read_text()
    assert "Migration Plan" in html_text
    assert "gdrive:personal" in html_text
