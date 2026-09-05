"""Manifest write is atomic: tmp → fsync → replace → dir-fsync.

Simulating a crash mid-write by making ``os.replace`` raise. The on-disk
state must be one of:

* the previous ``manifest.json`` (unchanged), or
* no ``manifest.json`` at all

— never a partially-written manifest.json. The ``manifest.json.tmp``
sibling may or may not exist; it is scratch space and is fair game.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from duplicate_cleaner.apply.mover import _write_manifest


def test_replace_failure_leaves_prior_manifest_intact(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    prior = {"created_at": "prior", "entries": [{"marker": 1}]}
    _write_manifest(manifest, prior)
    assert manifest.exists()
    # Sanity: prior write persisted.
    assert json.loads(manifest.read_text())["created_at"] == "prior"

    new_payload = {"created_at": "new", "entries": [{"marker": 2}]}
    with patch(
        "duplicate_cleaner.apply.mover.os.replace",
        side_effect=OSError("simulated crash mid-replace"),
    ):
        with pytest.raises(OSError):
            _write_manifest(manifest, new_payload)

    # The old manifest survived — no partial-write clobber.
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    assert data["created_at"] == "prior"


def test_replace_failure_on_first_write_leaves_no_partial(
    tmp_path: Path,
) -> None:
    """First write ever; crash inside os.replace. manifest.json must NOT
    exist, and if manifest.json.tmp exists it must be either empty or the
    complete unpromoted payload — never a partial write."""
    manifest = tmp_path / "manifest.json"
    payload = {"created_at": "new", "entries": []}
    with patch(
        "duplicate_cleaner.apply.mover.os.replace",
        side_effect=OSError("simulated crash"),
    ):
        with pytest.raises(OSError):
            _write_manifest(manifest, payload)

    assert not manifest.exists(), "target must not exist after failed replace"
    tmp = tmp_path / "manifest.json.tmp"
    if tmp.exists():
        # If the tmp survived, it must decode as valid JSON — never a
        # truncated fragment. That is the atomicity guarantee.
        parsed = json.loads(tmp.read_text())
        assert parsed == payload


def test_normal_write_produces_target_and_removes_tmp(tmp_path: Path) -> None:
    """Happy path: after a successful write there is a target file and no
    leftover .tmp sibling."""
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, {"created_at": "ok", "entries": [1, 2, 3]})
    assert manifest.exists()
    tmp = tmp_path / "manifest.json.tmp"
    assert not tmp.exists(), "tmp file must be consumed by the atomic replace"
