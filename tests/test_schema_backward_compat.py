"""v0.2 sub-phase 5a — schema backward-compat tests.

The v0.1.1 report.json / manifest.json shapes have no ``source_id``,
``cloud_file_id``, ``etag``, ``owner``, ``is_shared`` on their members and
no ``manifest_version`` at the manifest envelope.  A v0.2 build MUST load
those files silently and treat every member as ``source_id="local"`` — no
forced re-scan, no user-visible behavior change on local reports.

See ``docs/design/v0.2-subphase5-cloud-path-validation.md`` §3 and §9.
"""
from __future__ import annotations

import json
from pathlib import Path

from duplicate_cleaner.apply.mover import apply_report, load_report
from duplicate_cleaner.report.schema import (
    Manifest,
    ManifestEntry,
    Report,
    ReportGroup,
    ReportMember,
)

# ---------------------------------------------------------------------------
# Report backward-compat
# ---------------------------------------------------------------------------


def test_v0_1_1_report_loads_with_source_id_local_default(tmp_path: Path) -> None:
    """A hand-crafted v0.1.1 JSON (no source_id / cloud fields on members,
    no ``version`` on the envelope) must load cleanly and every member must
    default to ``source_id="local"`` with cloud fields cleared to None/False.
    """
    keep = tmp_path / "keep.bin"
    disc = tmp_path / "disc.bin"
    keep.write_bytes(b"x" * 8)
    disc.write_bytes(b"x" * 8)

    old_shape = {
        # No ``version`` key at all — v0.1.0-era JSON.  Also NO ``source_id``
        # / cloud fields anywhere.  Pydantic's field defaults must fill them in.
        "generated_at": "2026-09-05T00:00:00+00:00",
        "roots": [str(tmp_path)],
        "total_files_scanned": 2,
        "total_groups": 1,
        "total_reclaim_bytes": 8,
        "groups": [
            {
                "id": "g1",
                "kind": "exact",
                "size": 8,
                "hash": "H" * 32,
                "reclaim_bytes": 8,
                "members": [
                    {
                        "path": str(keep),
                        "size": 8,
                        "mtime": keep.stat().st_mtime,
                        "hash": "H" * 32,
                        "score": 1.0,
                        "signals": [],
                        "is_proposed_keeper": True,
                        "is_informational": False,
                    },
                    {
                        "path": str(disc),
                        "size": 8,
                        "mtime": disc.stat().st_mtime,
                        "hash": "H" * 32,
                        "score": 0.0,
                        "signals": [],
                        "is_proposed_keeper": False,
                        "is_informational": False,
                    },
                ],
            }
        ],
        "singletons": [],
        "archive_skips": [],
        "discover": False,
    }

    report = Report.model_validate(old_shape)

    # Fields defaulted from the v0.1.1 shape.
    for grp in report.groups:
        for m in grp.members:
            assert m.source_id == "local"
            assert m.cloud_file_id is None
            assert m.etag is None
            assert m.owner is None
            assert m.is_shared is False


def test_v0_1_1_report_apply_dry_run_still_works(tmp_path: Path) -> None:
    """End-to-end backward-compat: a v0.1.1 report.json on disk (with no
    ``version`` marker, no cloud fields on any member) must still flow
    through ``apply_report --dry-run`` byte-identically.
    """
    keep = tmp_path / "keep.bin"
    disc = tmp_path / "disc.bin"
    keep.write_bytes(b"x" * 8)
    disc.write_bytes(b"x" * 8)

    old_json_path = tmp_path / "report.json"
    old_json_path.write_text(
        json.dumps(
            {
                # No ``version`` marker at all.
                "generated_at": "2026-09-05T00:00:00+00:00",
                "roots": [str(tmp_path)],
                "total_files_scanned": 2,
                "total_groups": 1,
                "total_reclaim_bytes": 8,
                "groups": [
                    {
                        "id": "g1",
                        "kind": "exact",
                        "size": 8,
                        "hash": "H" * 32,
                        "reclaim_bytes": 8,
                        "members": [
                            {
                                "path": str(keep),
                                "size": keep.stat().st_size,
                                "mtime": keep.stat().st_mtime,
                                "hash": "H" * 32,
                                "score": 1.0,
                                "signals": [],
                                "is_proposed_keeper": True,
                                "is_informational": False,
                            },
                            {
                                "path": str(disc),
                                "size": disc.stat().st_size,
                                "mtime": disc.stat().st_mtime,
                                "hash": "H" * 32,
                                "score": 0.0,
                                "signals": [],
                                "is_proposed_keeper": False,
                                "is_informational": False,
                            },
                        ],
                    }
                ],
                "singletons": [],
                "archive_skips": [],
                "discover": False,
            }
        )
    )

    # Reader accepts a missing ``version`` and defaults every member to local.
    report = load_report(old_json_path)
    assert all(
        m.source_id == "local" for g in report.groups for m in g.members
    )

    # Dry-run apply: no side effects, no run dir created, both files remain.
    result = apply_report(
        old_json_path, commit=False, runs_dir=tmp_path / "runs"
    )
    assert result["committed"] is False
    assert result["planned"] == 1
    assert result["verified"] == 1
    assert not (tmp_path / "runs").exists()
    assert keep.exists()
    assert disc.exists()


def test_v0_2_report_roundtrips(tmp_path: Path) -> None:
    """Construct a v0.2 Report with cloud members explicitly, dump to JSON,
    reload, and assert the loaded object equals the original."""
    local_path = tmp_path / "keep.bin"
    local_path.write_bytes(b"x" * 8)
    original = Report(
        version="0.2.0",
        roots=[tmp_path],
        total_files_scanned=3,
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
                        path=local_path,
                        size=8,
                        mtime=1000.0,
                        hash="H" * 32,
                        score=1.0,
                        is_proposed_keeper=True,
                    ),
                    ReportMember(
                        # A cloud member's ``path`` is opaque display data —
                        # it is intentionally non-absolute on POSIX; nothing
                        # in ``apply/`` may resolve it.
                        path=Path("gdrive:personal://My Drive/foo.bin"),
                        size=8,
                        mtime=1000.0,
                        hash="H" * 32,
                        score=-1.0,
                        source_id="gdrive:personal",
                        cloud_file_id="1AbCdEfGhIjKlMnOp",
                        etag="1AbCdEfGhIjKlMnOp:2024-06-01T12:00:00Z",
                        owner="me@example.com",
                        is_shared=False,
                    ),
                    ReportMember(
                        path=Path("onedrive:work://Docs/foo.bin"),
                        size=8,
                        mtime=1000.0,
                        hash="H" * 32,
                        score=-1.0,
                        source_id="onedrive:work",
                        cloud_file_id="AABBCCDDEEFF0011",
                        etag="AABBCCDDEEFF0011:1",
                        is_shared=True,
                    ),
                ],
            )
        ],
    )
    reloaded = Report.model_validate_json(original.model_dump_json())
    # Full-model equality (Pydantic v2 __eq__).
    assert reloaded == original
    # And every cloud field survives the round trip untouched.
    cloud_members = [
        m
        for g in reloaded.groups
        for m in g.members
        if m.source_id != "local"
    ]
    assert len(cloud_members) == 2
    assert {m.source_id for m in cloud_members} == {
        "gdrive:personal",
        "onedrive:work",
    }
    assert all(m.cloud_file_id for m in cloud_members)
    assert all(m.etag for m in cloud_members)


def test_report_serializes_source_id_explicitly() -> None:
    """v0.2 report JSON must ALWAYS carry ``source_id`` on every member — even
    for local entries — so downstream tools reading the JSON can dispatch on
    it without a ``.get("source_id", "local")`` fallback.
    """
    r = Report(
        version="0.2.0",
        roots=[Path("/tmp/pytest-dc/x")],
        total_files_scanned=1,
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
                        path=Path("/tmp/pytest-dc/x/foo.bin"),
                        size=8,
                        mtime=1000.0,
                        hash="H" * 32,
                        score=1.0,
                        is_proposed_keeper=True,
                    ),
                ],
            )
        ],
    )
    dumped = json.loads(r.model_dump_json())
    # v0.2 writer stamps every member with source_id="local" explicitly.
    m = dumped["groups"][0]["members"][0]
    assert m["source_id"] == "local"
    # Cloud fields are present with None / False so a v0.2 reader never
    # misinterprets missing-vs-null.
    assert m["cloud_file_id"] is None
    assert m["etag"] is None
    assert m["owner"] is None
    assert m["is_shared"] is False


# ---------------------------------------------------------------------------
# Manifest backward-compat
# ---------------------------------------------------------------------------


def test_manifest_backward_compat_v0_1_1_shape() -> None:
    """A raw v0.1.1 manifest row (only original_path/size/mtime/hash/
    trashed_at_path) must validate against the new ``ManifestEntry`` and
    default ``source_id`` to ``"local"``.
    """
    old_row = {
        "original_path": "/Users/x/foo.pdf",
        "size": 12345,
        "mtime": 1741234567.0,
        "hash": "H" * 32,
        "trashed_at_path": "/Users/x/.Trash/foo.pdf",
    }
    entry = ManifestEntry.model_validate(old_row)
    assert entry.source_id == "local"
    assert entry.cloud_file_id is None
    assert entry.cloud_trash_id is None
    assert entry.etag is None
    assert entry.original_path == "/Users/x/foo.pdf"
    assert entry.trashed_at_path == "/Users/x/.Trash/foo.pdf"


def test_manifest_backward_compat_missing_manifest_version() -> None:
    """A v0.1.1 manifest envelope has no ``manifest_version`` key.  The
    ``Manifest`` model must still accept it via ``model_validate`` with the
    default marker in place.
    """
    old_manifest = {
        "created_at": "20260905T000000Z",
        "entries": [
            {
                "original_path": "/Users/x/foo.pdf",
                "size": 12,
                "mtime": 1.0,
                "hash": "H" * 32,
                "trashed_at_path": None,
            }
        ],
    }
    m = Manifest.model_validate(old_manifest)
    # Default marker present so downstream can key off it.
    assert m.manifest_version == "0.2.0"
    assert m.roots == []
    assert len(m.entries) == 1
    assert m.entries[0].source_id == "local"


def test_manifest_v0_2_roundtrip() -> None:
    """A mixed manifest (local + cloud entries) round-trips through
    ``Manifest.model_dump_json`` → ``model_validate_json`` byte-identically.
    """
    original = Manifest(
        created_at="20260905T120000Z",
        roots=["/Users/x/Documents"],
        entries=[
            ManifestEntry(
                original_path="/Users/x/foo.pdf",
                size=12,
                mtime=1.0,
                hash="H" * 32,
                trashed_at_path="/Users/x/.Trash/foo.pdf",
            ),
            ManifestEntry(
                original_path="gdrive:personal://My Drive/Old/foo.pdf",
                size=12,
                mtime=1.0,
                hash="H" * 32,
                trashed_at_path=None,
                source_id="gdrive:personal",
                cloud_file_id="1AbCdEfGhIjKlMnOp",
                cloud_trash_id=None,
                etag="1AbCdEfGhIjKlMnOp:2024-06-01T12:00:00Z",
            ),
        ],
    )
    reloaded = Manifest.model_validate_json(original.model_dump_json())
    assert reloaded == original
    # Sanity: cloud entry preserved every id.
    cloud_entry = reloaded.entries[1]
    assert cloud_entry.source_id == "gdrive:personal"
    assert cloud_entry.cloud_file_id == "1AbCdEfGhIjKlMnOp"
    assert cloud_entry.etag == "1AbCdEfGhIjKlMnOp:2024-06-01T12:00:00Z"


def test_manifest_local_entry_serializes_source_id_explicitly() -> None:
    """Analogous to the report test: even local manifest entries must
    stamp ``source_id="local"`` into the JSON so v0.2 tools reading the
    manifest never rely on a ``.get()`` fallback.
    """
    entry = ManifestEntry(
        original_path="/Users/x/foo.pdf",
        size=1,
        mtime=1.0,
        hash="H",
        trashed_at_path=None,
    )
    dumped = json.loads(entry.model_dump_json())
    assert dumped["source_id"] == "local"
    assert dumped["cloud_file_id"] is None
    assert dumped["cloud_trash_id"] is None
    assert dumped["etag"] is None


def test_mover_writes_v0_2_manifest_shape(tmp_path: Path) -> None:
    """Integration: the mover's on-disk manifest carries every v0.2 field
    with the local-default values.  Ensures the schema surgery lands in
    the actual write path, not just in the pydantic model definition.
    """
    keep = tmp_path / "keep.bin"
    disc = tmp_path / "disc.bin"
    keep.write_bytes(b"x" * 8)
    disc.write_bytes(b"x" * 8)

    report = Report(
        roots=[tmp_path],
        total_files_scanned=2,
        total_groups=1,
        total_reclaim_bytes=disc.stat().st_size,
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
                        is_proposed_keeper=True,
                    ),
                    ReportMember(
                        path=disc,
                        size=disc.stat().st_size,
                        mtime=disc.stat().st_mtime,
                        hash="H" * 32,
                        score=0.0,
                        is_proposed_keeper=False,
                    ),
                ],
            )
        ],
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json())

    fake_trash = tmp_path / "trash"
    fake_trash.mkdir()

    def trash_fn(p: Path) -> Path:
        import shutil

        dest = fake_trash / p.name
        shutil.move(str(p), str(dest))
        return dest

    result = apply_report(
        report_path,
        commit=True,
        runs_dir=tmp_path / "runs",
        trash_fn=trash_fn,
    )
    manifest_json = json.loads(Path(result["manifest_path"]).read_text())

    # Envelope carries the v0.2 marker + roots.
    assert manifest_json["manifest_version"] == "0.2.0"
    assert manifest_json["roots"] == [str(tmp_path)]

    # Every entry carries the v0.2 fields with local defaults.
    assert len(manifest_json["entries"]) == 1
    entry = manifest_json["entries"][0]
    assert entry["source_id"] == "local"
    assert entry["cloud_file_id"] is None
    assert entry["cloud_trash_id"] is None
    assert entry["etag"] is None
    # v0.1.1 fields survive unchanged.
    assert entry["original_path"] == str(disc)
    assert entry["trashed_at_path"] == str(fake_trash / "disc.bin")
