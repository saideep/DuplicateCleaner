from __future__ import annotations

import os
from pathlib import Path

from duplicate_cleaner.scan.walk import iter_files


def _touch(p: Path, content: bytes = b"x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def test_walk_returns_files_and_excludes_dirs(tmp_path: Path) -> None:
    _touch(tmp_path / "a.txt", b"aaa")
    _touch(tmp_path / "sub" / "b.txt", b"bbb")
    _touch(tmp_path / ".git" / "objects" / "abc", b"ccc")
    _touch(tmp_path / "node_modules" / "pkg" / "d.txt", b"ddd")
    _touch(tmp_path / "__pycache__" / "e.pyc", b"eee")
    _touch(tmp_path / "cloud" / ".foo.icloud", b"fff")

    got = {r.path for r in iter_files([tmp_path])}
    # Paths are resolved during walk, so compare against resolved versions.
    root = tmp_path.resolve()
    assert (root / "a.txt") in got
    assert (root / "sub" / "b.txt") in got
    assert not any(".git" in str(p) for p in got)
    assert not any("node_modules" in str(p) for p in got)
    assert not any("__pycache__" in str(p) for p in got)
    assert not any(str(p).endswith(".icloud") for p in got)


def test_walk_skips_symlinks_by_default(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    _touch(target, b"target")
    link = tmp_path / "link.txt"
    os.symlink(target, link)

    default_names = {r.path.name for r in iter_files([tmp_path])}
    assert "target.txt" in default_names
    assert "link.txt" not in default_names

    follow_names = {r.path.name for r in iter_files([tmp_path], follow_symlinks=True)}
    assert "target.txt" in follow_names
    assert "link.txt" in follow_names


def test_walk_applies_min_size(tmp_path: Path) -> None:
    _touch(tmp_path / "small.txt", b"x")
    _touch(tmp_path / "big.txt", b"x" * 10)

    got = {r.path for r in iter_files([tmp_path], min_size_bytes=5)}
    root = tmp_path.resolve()
    assert (root / "big.txt") in got
    assert (root / "small.txt") not in got
