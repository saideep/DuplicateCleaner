"""Tests for the v0.3-b organize apply pipeline (mover.py)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from duplicate_cleaner.config import Config
from duplicate_cleaner.organize.mover import (
    OrganizeApplyError,
    OrganizeDriftError,
    apply_plan,
)
from duplicate_cleaner.organize.plan import (
    CohesionGroup,
    PlanEntry,
    PlanFile,
    dest_for,
)


def _mk_cfg(tmp_root: Path, **overrides: object) -> Config:
    """Build a Config whose active_homes contain the given tmp root."""
    data: dict[str, object] = {
        "active_homes": [tmp_root],
        "min_size_bytes": 1,
        "exclude_globs": [],
        "follow_symlinks": False,
    }
    data.update(overrides)
    return Config.model_validate(data)


def _mk_entry(src_path: Path, *, domain: str = "HR", subfolder: str = "Payslips/2024",
              cohesion_group_id: str | None = None) -> PlanEntry:
    st = src_path.stat()
    return PlanEntry(
        source_id="local",
        source_path=src_path,
        proposed_dest=dest_for(domain, subfolder, src_path.name),
        domain=domain,
        subfolder=subfolder,
        filename=src_path.name,
        size=st.st_size,
        mtime=st.st_mtime,
        confidence=0.9,
        cohesion_group_id=cohesion_group_id,
    )


def _mk_plan(tmp_path: Path, entries: list[PlanEntry],
             cohesion_groups: list[CohesionGroup] | None = None,
             dest_root: Path | None = None) -> Path:
    plan = PlanFile(
        sources=["local"],
        roots=[tmp_path],
        dest_root=dest_root if dest_root is not None else (tmp_path / "organized"),
        total_files=len(entries),
        total_by_domain={},
        entries=entries,
        cohesion_groups=cohesion_groups or [],
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json())
    return plan_path


def _mk_source(tmp_path: Path, name: str = "payslip_march_2024.pdf",
               body: bytes = b"stub payslip contents") -> Path:
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    p = src_dir / name
    p.write_bytes(body)
    return p


# --------------------------------------------------------------------------- #
# Dry-run / commit                                                            #
# --------------------------------------------------------------------------- #


def test_organize_apply_dry_run_writes_no_files(tmp_path: Path) -> None:
    src = _mk_source(tmp_path)
    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)

    result = apply_plan(plan_path, commit=False, config=cfg, runs_dir=tmp_path / "runs")

    assert result.committed is False
    assert result.moved == 0
    assert result.manifest_path is None
    assert result.planned == 1
    assert src.exists()
    assert src.read_bytes() == b"stub payslip contents"
    assert not (tmp_path / "runs").exists()


def test_organize_apply_commit_moves_files(tmp_path: Path) -> None:
    src = _mk_source(tmp_path, name="payslip.pdf", body=b"payslip-bytes")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)

    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")

    assert result.committed is True
    assert result.moved == 1
    expected = tmp_path / "organized" / "HR" / "Payslips" / "2024" / "payslip.pdf"
    assert expected.exists()
    assert expected.read_bytes() == b"payslip-bytes"
    assert not src.exists()
    assert result.manifest_path is not None
    assert Path(result.manifest_path).exists()


# --------------------------------------------------------------------------- #
# Directory creation + collision policy                                       #
# --------------------------------------------------------------------------- #


def test_organize_apply_creates_parent_dirs(tmp_path: Path) -> None:
    src = _mk_source(tmp_path, name="doc.pdf")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src, subfolder="a/b/c/d/e")])
    cfg = _mk_cfg(tmp_path)

    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert result.moved == 1
    expected = tmp_path / "organized" / "HR" / "a" / "b" / "c" / "d" / "e" / "doc.pdf"
    assert expected.exists()


def test_organize_apply_collision_gets_suffix(tmp_path: Path) -> None:
    src = _mk_source(tmp_path, name="clash.pdf", body=b"new-content")
    # Pre-create a colliding file at the exact destination.
    dest_dir = tmp_path / "organized" / "HR" / "Payslips" / "2024"
    dest_dir.mkdir(parents=True)
    (dest_dir / "clash.pdf").write_bytes(b"already-here")

    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)

    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")

    assert result.moved == 1
    assert len(result.collisions) == 1
    entry = result.collisions[0]
    renamed = Path(entry["renamed_to"])
    assert renamed.exists()
    assert renamed.read_bytes() == b"new-content"
    # Suffix _<hash8>.pdf shape: 8 hex chars + .pdf.
    stem = renamed.stem
    assert "_" in stem
    hash_part = stem.rsplit("_", 1)[1]
    assert len(hash_part) == 8
    assert all(c in "0123456789abcdef" for c in hash_part)
    # Original colliding file untouched.
    assert (dest_dir / "clash.pdf").read_bytes() == b"already-here"


# --------------------------------------------------------------------------- #
# Cohesion enforcement                                                        #
# --------------------------------------------------------------------------- #


def test_organize_apply_refuses_split_cohesive_group(tmp_path: Path) -> None:
    src_a = _mk_source(tmp_path, name="track01.flac", body=b"one")
    src_b = _mk_source(tmp_path, name="track02.flac", body=b"two")

    a = _mk_entry(src_a, domain="Media", subfolder="Music/Miles Davis/Kind of Blue",
                  cohesion_group_id="music_album:kind-of-blue")
    # Force B to a different destination folder to trigger the split guard.
    b = _mk_entry(src_b, domain="Media", subfolder="Music/Miles Davis/Kind of Blue",
                  cohesion_group_id="music_album:kind-of-blue")
    b = b.model_copy(update={
        "subfolder": "Music/Other Artist/Other Album",
        "proposed_dest": dest_for("Media", "Music/Other Artist/Other Album",
                                  src_b.name),
    })

    plan_path = _mk_plan(tmp_path, [a, b])
    cfg = _mk_cfg(tmp_path)

    with pytest.raises(OrganizeApplyError) as excinfo:
        apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert "cohesion" in str(excinfo.value).lower()
    # Nothing moved.
    assert src_a.exists()
    assert src_b.exists()


def test_organize_apply_split_cohesive_with_flag(tmp_path: Path) -> None:
    src_a = _mk_source(tmp_path, name="track01.flac", body=b"one")
    src_b = _mk_source(tmp_path, name="track02.flac", body=b"two")

    a = _mk_entry(src_a, domain="Media", subfolder="Music/A/A1",
                  cohesion_group_id="music_album:kind-of-blue")
    b = _mk_entry(src_b, domain="Media", subfolder="Music/B/B1",
                  cohesion_group_id="music_album:kind-of-blue")

    plan_path = _mk_plan(tmp_path, [a, b])
    cfg = _mk_cfg(tmp_path)

    result = apply_plan(
        plan_path, commit=True, config=cfg,
        split_cohesive_units=True, runs_dir=tmp_path / "runs",
    )
    assert result.moved == 2
    assert any("cohesion split accepted" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Path safety rails                                                           #
# --------------------------------------------------------------------------- #


def test_organize_apply_rejects_excluded_source(tmp_path: Path) -> None:
    # Craft an entry whose source_path resolves inside ~/Library.
    fake_src = Path.home() / "Library" / "Preferences" / "fake.plist"
    entry = PlanEntry(
        source_id="local",
        source_path=fake_src,
        proposed_dest="HR/Payslips/2024/fake.plist",
        domain="HR",
        subfolder="Payslips/2024",
        filename="fake.plist",
        size=100,
        mtime=1_700_000_000.0,
        confidence=0.9,
    )
    plan_path = _mk_plan(tmp_path, [entry])
    cfg = _mk_cfg(tmp_path)

    with pytest.raises(OrganizeApplyError) as excinfo:
        apply_plan(plan_path, commit=False, config=cfg, runs_dir=tmp_path / "runs")
    assert "excluded" in str(excinfo.value).lower() or "library" in str(excinfo.value).lower()


def test_organize_apply_rejects_excluded_dest(tmp_path: Path) -> None:
    src = _mk_source(tmp_path)
    plan_path = _mk_plan(
        tmp_path,
        [_mk_entry(src)],
        dest_root=Path("/System/Library/OrganizedByAttacker"),
    )
    cfg = _mk_cfg(tmp_path)

    with pytest.raises(OrganizeApplyError) as excinfo:
        apply_plan(plan_path, commit=False, config=cfg, runs_dir=tmp_path / "runs")
    msg = str(excinfo.value).lower()
    assert "excluded" in msg or "active_home" in msg or "dest_root" in msg


def test_organize_apply_rejects_path_traversal(tmp_path: Path) -> None:
    src = _mk_source(tmp_path)
    entry = _mk_entry(src)
    # Overwrite proposed_dest with a `..` traversal segment.
    entry = entry.model_copy(update={
        "proposed_dest": "../../etc/passwd",
    })
    plan_path = _mk_plan(tmp_path, [entry])
    cfg = _mk_cfg(tmp_path)

    with pytest.raises(OrganizeApplyError) as excinfo:
        apply_plan(plan_path, commit=False, config=cfg, runs_dir=tmp_path / "runs")
    assert ".." in str(excinfo.value) or "traversal" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# Drift + atomic manifest                                                     #
# --------------------------------------------------------------------------- #


def test_organize_apply_drift_aborts(tmp_path: Path) -> None:
    src = _mk_source(tmp_path, name="drift.pdf", body=b"original-bytes")
    entry = _mk_entry(src)
    plan_path = _mk_plan(tmp_path, [entry])
    cfg = _mk_cfg(tmp_path)
    # Mutate the source file so size differs from what the plan captured.
    src.write_bytes(b"THIS IS A COMPLETELY DIFFERENT PAYLOAD SIZE ON DISK")

    with pytest.raises(OrganizeDriftError):
        apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    # File still at source; no organized dir created.
    assert src.exists()
    assert not (tmp_path / "organized").exists()


def test_organize_apply_manifest_atomic_write(tmp_path: Path) -> None:
    src = _mk_source(tmp_path)
    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)

    real_replace = __import__("os").replace

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    with (
        patch("duplicate_cleaner.organize.mover.os.replace", side_effect=_boom),
        pytest.raises(OSError),
    ):
        apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert real_replace is __import__("os").replace  # sanity: patch scope closed
    # No partial move: source still in place.
    assert src.exists()
    # No final manifest.json at the run destination.
    run_dirs = list((tmp_path / "runs").iterdir()) if (tmp_path / "runs").exists() else []
    for d in run_dirs:
        assert not (d / "manifest.json").exists()


# --------------------------------------------------------------------------- #
# Rename policy                                                               #
# --------------------------------------------------------------------------- #


def test_organize_apply_preserve_rename_policy(tmp_path: Path) -> None:
    """Default policy: filename bytes are never mutated."""
    src = _mk_source(tmp_path, name="quirky filename WITH spaces.pdf",
                     body=b"content")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src)])
    cfg = _mk_cfg(tmp_path)
    assert cfg.rename_policy == "preserve"

    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert result.moved == 1
    expected = tmp_path / "organized" / "HR" / "Payslips" / "2024" / \
        "quirky filename WITH spaces.pdf"
    assert expected.exists()


def test_organize_apply_date_prefix_rename(tmp_path: Path) -> None:
    src = _mk_source(tmp_path, name="doc.pdf", body=b"content")
    # Freeze mtime to a known value so the prefix is stable.
    import os as _os
    fixed_ts = 1_710_000_000.0  # 2024-03-09 UTC
    _os.utime(src, (fixed_ts, fixed_ts))
    entry = _mk_entry(src)
    plan_path = _mk_plan(tmp_path, [entry])
    cfg = _mk_cfg(tmp_path, rename_policy="date_prefix")

    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert result.moved == 1
    expected = tmp_path / "organized" / "HR" / "Payslips" / "2024" / "2024-03-09_doc.pdf"
    assert expected.exists()


# --------------------------------------------------------------------------- #
# Manifest contents                                                           #
# --------------------------------------------------------------------------- #


def test_organize_apply_manifest_records_entries(tmp_path: Path) -> None:
    src_a = _mk_source(tmp_path, name="a.pdf", body=b"aaa")
    src_b = _mk_source(tmp_path, name="b.pdf", body=b"bbb")
    plan_path = _mk_plan(tmp_path, [_mk_entry(src_a), _mk_entry(src_b)])
    cfg = _mk_cfg(tmp_path)

    result = apply_plan(plan_path, commit=True, config=cfg, runs_dir=tmp_path / "runs")
    assert result.moved == 2

    manifest = json.loads(Path(result.manifest_path).read_text())  # type: ignore[arg-type]
    assert manifest["version"] == "0.3.0"
    assert manifest["kind"] == "organize"
    assert len(manifest["entries"]) == 2
    for e in manifest["entries"]:
        assert "source_path" in e
        assert "dest_path" in e
        assert "size" in e
        assert "mtime" in e
