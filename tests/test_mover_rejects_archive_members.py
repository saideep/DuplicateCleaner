"""Mover rejects any discard whose path contains the ``::`` archive separator.

Enforces the plan's "whole-archive proposal only" contract: an archive
member is never a legal Trash target, no matter what the JSON claims.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from duplicate_cleaner.apply.mover import ApplyError, apply_report


def _write_report(json_path: Path, member_path: str) -> None:
    data = {
        "version": "0.1.1",
        "generated_at": "2026-09-05T00:00:00+00:00",
        "roots": [str(json_path.parent)],
        "total_files_scanned": 2,
        "total_groups": 1,
        "total_reclaim_bytes": 1024,
        "groups": [
            {
                "id": "g1",
                "kind": "exact",
                "size": 1024,
                "hash": "H" * 64,
                "reclaim_bytes": 1024,
                "members": [
                    {
                        "path": str(json_path.parent / "keep.bin"),
                        "size": 1024,
                        "mtime": 1.0,
                        "hash": "H" * 64,
                        "score": 5.0,
                        "signals": [],
                        "is_proposed_keeper": True,
                        "is_informational": False,
                    },
                    {
                        # Virtual archive-member path — MUST be rejected.
                        "path": member_path,
                        "size": 1024,
                        "mtime": 1.0,
                        "hash": "H" * 64,
                        "score": -1.0,
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
    json_path.write_text(json.dumps(data))


def test_apply_refuses_archive_member_as_discard(tmp_path: Path) -> None:
    """A discard entry with ``::`` in the path aborts the whole apply.

    Uses a scan root that satisfies the shallow-path guard, then hand-writes
    a report that names ``root/dup.zip::inner.bin`` as a discard.
    """
    root = tmp_path / "Users" / "me" / "scan"
    root.mkdir(parents=True)
    (root / "keep.bin").write_bytes(b"x" * 1024)
    report = root / "report.json"
    _write_report(report, str(root / "dup.zip::inner.bin"))

    with pytest.raises(ApplyError) as ex:
        apply_report(report, commit=False)
    assert "::" in str(ex.value)
