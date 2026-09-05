"""Singleton "unique files" reporting + ``--discover`` mode behavior.

The scan step now also indexes files that appear exactly once across all
scanned roots; those entries land in ``report.singletons``. ``--discover``
mode strips every proposed keeper so ``dc apply`` refuses to run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from duplicate_cleaner.apply.mover import ApplyError, apply_report
from duplicate_cleaner.compare.exact import group_by_hash
from duplicate_cleaner.config import DEFAULT_WEIGHTS, Config
from duplicate_cleaner.hash.pipeline import hash_records
from duplicate_cleaner.report.render import render_report
from duplicate_cleaner.report.schema import (
    Report,
    ReportGroup,
    ReportMember,
    ReportSignal,
    SingletonEntry,
)
from duplicate_cleaner.scan.walk import iter_files
from duplicate_cleaner.score.rules import score_groups
from duplicate_cleaner.store import Store


def _run_full_scan(root: Path, tmp: Path) -> tuple[Path, Report]:
    """Emulate what dc scan does — return (json_path, in-memory Report)."""
    store = Store(tmp / "cache.db")
    cfg = Config(active_homes=[root], min_size_bytes=0)
    hashed = list(
        hash_records(
            iter_files([root], min_size_bytes=0),
            store,
            include_singletons=True,
        )
    )
    groups = list(group_by_hash(iter(hashed), min_size_bytes=0))
    scored = score_groups(groups, cfg, DEFAULT_WEIGHTS)

    report_groups: list[ReportGroup] = []
    total_reclaim = 0
    for sg in scored:
        members = [
            ReportMember(
                path=m.path,
                size=m.size,
                mtime=m.mtime,
                hash=m.hash,
                score=m.score,
                signals=[
                    ReportSignal(name=n, contribution=c) for n, c in m.signals
                ],
                is_proposed_keeper=m.is_proposed_keeper,
                is_informational=m.is_informational,
            )
            for m in sg.members
        ]
        report_groups.append(
            ReportGroup(
                id=sg.id,
                hash=sg.hash,
                size=sg.size,
                reclaim_bytes=sg.reclaim_bytes,
                members=members,
            )
        )
        total_reclaim += sg.reclaim_bytes

    hash_counts: dict[str, int] = {}
    for h in hashed:
        hash_counts[h.full_hash] = hash_counts.get(h.full_hash, 0) + 1
    singletons: list[SingletonEntry] = []
    seen: set[str] = set()
    for h in hashed:
        if hash_counts.get(h.full_hash, 0) != 1 or h.full_hash in seen:
            continue
        seen.add(h.full_hash)
        singletons.append(
            SingletonEntry(
                path=h.path, size=h.size, mtime=h.mtime, hash=h.full_hash
            )
        )

    report = Report(
        roots=[root.resolve()],
        total_files_scanned=len(hashed),
        total_groups=len(report_groups),
        total_reclaim_bytes=total_reclaim,
        groups=report_groups,
        singletons=singletons,
    )
    _, json_path = render_report(report, tmp / "report")
    store.close()
    return json_path, report


def test_singleton_section_lists_unique_files(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    # Three duplicates of one content + two unique files.
    for name in ("dup1.txt", "dup2.txt", "dup3.txt"):
        (home / name).write_bytes(b"SAME")
    (home / "unique_a.txt").write_bytes(b"AAAA-unique-first-content-here-abc")
    (home / "unique_b.txt").write_bytes(b"BBBB-unique-second-content-here-def")

    _json_path, report = _run_full_scan(home, tmp_path)
    assert report.singletons, "expected at least one singleton"
    names = {Path(s.path).name for s in report.singletons}
    assert "unique_a.txt" in names
    assert "unique_b.txt" in names
    # Duplicates NEVER appear in the singleton list.
    assert not (names & {"dup1.txt", "dup2.txt", "dup3.txt"})


def test_singleton_content_unique_but_size_collided_is_reported(
    tmp_path: Path,
) -> None:
    """H7: three same-size files where two are duplicates and one is unique.
    Previously the unique file's SIZE bucket had >1 members so it dodged
    ``iter_singleton_stage_records``, and its partial-hash sub-bucket had
    <2 members so ``_hash_size_bucket`` dropped it. It disappeared from
    every downstream report entirely. Post-fix it MUST appear in the
    singleton list.
    """
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    # Two duplicated files + one unique file, all same size — head-tail
    # boundary is far enough to force divergent partial hashes.
    body_dup = b"D" * 4096
    body_uniq = b"U" * 4096
    (home / "dup1.txt").write_bytes(body_dup)
    (home / "dup2.txt").write_bytes(body_dup)
    (home / "unique.txt").write_bytes(body_uniq)

    _json_path, report = _run_full_scan(home, tmp_path)

    # The unique file must be listed in ``singletons``.
    singleton_names = {Path(s.path).name for s in report.singletons}
    assert "unique.txt" in singleton_names, (
        f"unique.txt should surface as a singleton; got {singleton_names}"
    )
    # And the two duplicates must NOT be there.
    assert "dup1.txt" not in singleton_names
    assert "dup2.txt" not in singleton_names


def test_discover_report_refuses_apply(tmp_path: Path) -> None:
    """A discover-mode report has no keepers proposed. ``apply_report`` must
    refuse with a clear message before touching disk."""
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    (home / "dup1.txt").write_bytes(b"SAME")
    (home / "dup2.txt").write_bytes(b"SAME")

    json_path, _report = _run_full_scan(home, tmp_path)
    # Tamper the JSON on disk to simulate --discover output.
    data = json.loads(Path(json_path).read_text())
    data["discover"] = True
    for g in data["groups"]:
        for m in g["members"]:
            m["is_proposed_keeper"] = False
    Path(json_path).write_text(json.dumps(data))

    with pytest.raises(ApplyError) as ex:
        apply_report(json_path, commit=False)
    assert "discover" in str(ex.value).lower()
