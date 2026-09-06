"""Walker refuses to descend into the hard-coded excluded roots.

Two flavors:

1. Direct: passing an excluded root (or a subtree of one) as the ``roots``
   argument yields zero records.
2. Via symlink: a symlink INSIDE a scannable dir pointing at an excluded
   root must not resurrect the excluded tree, even with
   ``follow_symlinks=True``.

Uses ``pytest.mark.skipif`` for host-dependent paths so a lean CI runner
without ``/opt`` or ``/sbin`` still passes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from duplicate_cleaner.constants import EXCLUDED_ROOTS
from duplicate_cleaner.scan.walk import iter_files

EXPANDED_EXCLUDED_ROOTS: list[str] = [
    "/Library",
    "/Applications",
    "/usr",
    "/opt",
    "/sbin",
    "/bin",
    "/etc",
    "/private/etc",
    "/private/var/db",
    "/private/var/log",
    "/System",
    str(Path.home() / "Library"),
]


@pytest.mark.parametrize("target_str", EXPANDED_EXCLUDED_ROOTS)
def test_walking_excluded_root_yields_nothing(target_str: str) -> None:
    target = Path(target_str)
    if not target.exists():
        pytest.skip(f"{target} does not exist on this host")
    records = list(iter_files([target]))
    assert records == [], (
        f"walker returned records from excluded root {target}: "
        f"{[str(r.path) for r in records[:3]]}"
    )


def test_symlink_into_library_is_not_followed_even_with_flag(
    tmp_path: Path,
) -> None:
    """A symlink inside a normal dir that points at ~/Library must be
    silently excluded — the resolved-path check catches it.
    """
    library = Path.home() / "Library"
    if not library.exists():
        pytest.skip("~/Library does not exist on this host")

    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    # Innocent file so the walker has something to yield when it works.
    (scan_root / "regular.txt").write_bytes(b"data")
    # Symlink pointing at the excluded root.
    os.symlink(library, scan_root / "trap")

    records = list(
        iter_files([scan_root], follow_symlinks=True)
    )
    paths = {str(r.path) for r in records}
    # The regular file is scanned.
    assert any(p.endswith("regular.txt") for p in paths)
    # Nothing under ~/Library sneaks in.
    lib_str = str(library)
    for p in paths:
        assert lib_str not in p, (
            f"symlinked ~/Library leaked into results: {p}"
        )


def test_symlink_into_system_is_not_followed(tmp_path: Path) -> None:
    system = Path("/System")
    if not system.exists():
        pytest.skip("/System does not exist on this host (non-macOS?)")

    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    os.symlink(system, scan_root / "trap")

    records = list(iter_files([scan_root], follow_symlinks=True))
    for r in records:
        assert "/System/" not in str(r.path)


def test_expanded_excluded_roots_constant_covers_docs() -> None:
    """Sanity: the roots this file exercises are the ones enumerated in
    ``EXCLUDED_ROOTS`` and documented in docs/safety.md. If a new entry
    lands there, this list should grow — the test catches the drift.

    We accept extra entries in EXCLUDED_ROOTS (e.g. ``/var/root``), but
    every entry we DO parametrize must actually appear there.
    """
    excluded_set = set(EXCLUDED_ROOTS)
    for path in EXPANDED_EXCLUDED_ROOTS:
        # /Library and ~/Library are both intended to be blocked; the
        # constants module encodes ~/Library explicitly.
        if path == str(Path.home() / "Library"):
            assert path in excluded_set
            continue
        assert path in excluded_set, (
            f"{path} is exercised by tests but missing from EXCLUDED_ROOTS "
            "— either add it to the constant or drop it from the test"
        )
