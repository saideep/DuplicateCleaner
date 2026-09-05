"""APFS clone-family handling — verified via a mock ``get_clone_id``.

Real clones cannot be created in ``tmp_path`` without a supporting APFS
volume; the plan calls out mocking as the correct test strategy.
"""
from __future__ import annotations

from pathlib import Path

from duplicate_cleaner.compare.exact import Group
from duplicate_cleaner.config import DEFAULT_WEIGHTS, Config
from duplicate_cleaner.hash.pipeline import HashedRecord
from duplicate_cleaner.score.rules import score_group
from duplicate_cleaner.sys.apfs import get_clone_id


def _hr(path: Path, *, inode: int) -> HashedRecord:
    return HashedRecord(
        path=path,
        size=1,
        mtime=1000.0,
        inode=inode,
        dev=1,
        nlink=1,
        full_hash="H" * 64,
    )


def test_two_clones_marked_informational_no_keeper(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    a = home / "a.txt"
    b = home / "b.txt"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    # Distinct inodes so the hard-link rule does NOT fire — this proves the
    # informational status comes from the clone-id path alone.
    group = Group(hash="H", size=1, members=[_hr(a, inode=100), _hr(b, inode=200)])
    cfg = Config(active_homes=[home])

    def clone_lookup(_p: Path) -> int | None:
        return 12345  # same for both — they share a clone lineage

    members = score_group(
        group, cfg, DEFAULT_WEIGHTS, clone_id_lookup=clone_lookup
    )
    keepers = [m for m in members if m.is_proposed_keeper]
    info = [m for m in members if m.is_informational]
    assert len(info) == 2
    assert len(keepers) == 0


def test_mixed_clone_family_plus_separate_copy(tmp_path: Path) -> None:
    """Two clones (informational) + a separate byte-identical copy — the
    separate copy becomes the sole keeper."""
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    a = home / "a.txt"
    b = home / "b.txt"
    c = home / "c.txt"
    for p in (a, b, c):
        p.write_bytes(b"x")

    group = Group(
        hash="H",
        size=1,
        members=[_hr(a, inode=1), _hr(b, inode=2), _hr(c, inode=3)],
    )
    cfg = Config(active_homes=[home])

    def clone_lookup(p: Path) -> int | None:
        if p in (a, b):
            return 12345
        return 99  # distinct clone id — no family match

    members = score_group(
        group, cfg, DEFAULT_WEIGHTS, clone_id_lookup=clone_lookup
    )
    by_path = {m.path: m for m in members}
    assert by_path[a].is_informational
    assert by_path[b].is_informational
    assert not by_path[c].is_informational
    assert by_path[c].is_proposed_keeper


def test_clone_lookup_none_falls_back_to_hardlink_rule(tmp_path: Path) -> None:
    """When the lookup returns None, clone-detection is a no-op — the group
    behaves the same as a non-clone corpus."""
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    a = home / "a.txt"
    b = home / "b.txt"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    group = Group(hash="H", size=1, members=[_hr(a, inode=1), _hr(b, inode=2)])
    cfg = Config(active_homes=[home])

    members = score_group(
        group,
        cfg,
        DEFAULT_WEIGHTS,
        clone_id_lookup=lambda _p: None,
    )
    # No informational markers — the group proposes a keeper as usual.
    assert not any(m.is_informational for m in members)
    assert sum(m.is_proposed_keeper for m in members) == 1


def test_real_get_clone_id_returns_int_or_none_smoke(tmp_path: Path) -> None:
    """Smoke: on real Darwin, the call returns int or None — never raises."""
    p = tmp_path / "smoke.txt"
    p.write_bytes(b"x")
    result = get_clone_id(p)
    assert result is None or isinstance(result, int)


def test_real_clone_ids_match_when_created_via_clonefile(tmp_path: Path) -> None:
    """H3: two files created via ``clonefile(2)`` share a clone lineage id.

    Skipped when not running on Darwin or when the tmp filesystem is not
    APFS (``clonefile`` returns ENOTSUP). This is a smoke test — the
    important assertion is that the fixed ``getattrlist`` call returns a
    real non-zero id AND that clones share it.
    """
    import ctypes
    import ctypes.util
    import sys

    if sys.platform != "darwin":
        import pytest
        pytest.skip("clonefile is Darwin-only")

    libname = ctypes.util.find_library("System")
    if not libname:
        import pytest
        pytest.skip("libSystem not found")
    libc = ctypes.CDLL(libname, use_errno=True)
    try:
        clonefile = libc.clonefile
    except AttributeError:
        import pytest
        pytest.skip("clonefile symbol not present in libSystem")
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    clonefile.restype = ctypes.c_int

    src = tmp_path / "source.bin"
    src.write_bytes(b"clone-me-please" * 64)
    dst_a = tmp_path / "clone_a.bin"
    dst_b = tmp_path / "clone_b.bin"

    def _clone(a: Path, b: Path) -> bool:
        rc = clonefile(str(a).encode(), str(b).encode(), 0)
        if rc != 0:
            errno = ctypes.get_errno()
            # ENOTSUP=45 on macOS, EXDEV=18, EOPNOTSUPP=102 (linux), ...
            # Any failure means we're not on APFS — skip.
            import pytest
            pytest.skip(f"clonefile failed (errno={errno}); tmpdir not APFS")
            return False
        return True

    _clone(src, dst_a)
    _clone(src, dst_b)

    id_a = get_clone_id(dst_a)
    id_b = get_clone_id(dst_b)
    # If the fix landed, both should be non-None, non-zero, and equal.
    assert id_a is not None, "get_clone_id returned None on real clone"
    assert id_b is not None
    assert id_a != 0
    assert id_a == id_b
