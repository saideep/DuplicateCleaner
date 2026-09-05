"""Shared path validation helpers — used by scan, apply, and config.

Everything that decides whether a path is safe to touch flows through here
so a single hardening pass in this module hardens the whole tool.
"""
from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable
from pathlib import Path

from duplicate_cleaner.constants import (
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILE_SUFFIXES,
    EXCLUDED_PATH_SUBSTRINGS,
    EXCLUDED_ROOTS,
)

# Any /Users/<X>/Library subtree — for every user on the box, not just the current one.
_USER_LIBRARY_RE = re.compile(r"^/Users/[^/]+/Library(?:/|$)")

# Volumes-level exclusions: a bootable APFS clone at ``/Volumes/BackupBoot/``
# has the same structural sensitivity as ``/`` — its System/Library/etc trees
# must not be scanned or trashed, or the clone becomes unbootable.
_VOLUMES_EXCLUDED_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pat)
    for pat in (
        r"^/Volumes/[^/]+/System(?:/|$)",
        r"^/Volumes/[^/]+/Library(?:/|$)",
        r"^/Volumes/[^/]+/Applications(?:/|$)",
        r"^/Volumes/[^/]+/usr(?:/|$)",
        r"^/Volumes/[^/]+/opt(?:/|$)",
        r"^/Volumes/[^/]+/private/etc(?:/|$)",
        r"^/Volumes/[^/]+/private/var/(?:db|log|vm|root|audit|folders|tmp)(?:/|$)",
        r"^/Volumes/[^/]+/Users/[^/]+/Library(?:/|$)",
    )
)


def is_excluded_root_path(path: Path) -> bool:
    """True if the path is inside an EXCLUDED_ROOTS entry or a user Library."""
    s = str(path)
    for root in EXCLUDED_ROOTS:
        if s == root or s.startswith(root + os.sep):
            return True
    if _USER_LIBRARY_RE.match(s):
        return True
    for pat in _VOLUMES_EXCLUDED_RES:
        if pat.match(s):
            return True
    return any(sub in s for sub in EXCLUDED_PATH_SUBSTRINGS)


def has_excluded_segment(path: Path) -> bool:
    """True if any component of the path matches EXCLUDED_DIR_NAMES."""
    return any(part in EXCLUDED_DIR_NAMES for part in path.parts)


def has_excluded_suffix(path: Path) -> bool:
    """True if the basename ends with an EXCLUDED_FILE_SUFFIXES entry."""
    return path.name.endswith(EXCLUDED_FILE_SUFFIXES)


def matches_globs(path: Path, globs: list[str]) -> bool:
    """True if the path (as a string) matches any of the given fnmatch globs."""
    if not globs:
        return False
    s = str(path)
    return any(fnmatch.fnmatch(s, g) for g in globs)


def validate_not_excluded(path: Path) -> None:
    """Raise ValueError if the path matches any hard-coded exclusion.

    Callers should pass a resolved absolute path so symlink dodges are caught.
    """
    if is_excluded_root_path(path):
        raise ValueError(f"path is inside an excluded root: {path}")
    if has_excluded_segment(path):
        raise ValueError(f"path contains an excluded directory name: {path}")
    if has_excluded_suffix(path):
        raise ValueError(f"path has an excluded suffix: {path}")


def resolve_for_check(path: Path) -> Path:
    """Resolve for exclusion checking; tolerates non-existent paths.

    On Python 3.6+ ``resolve(strict=False)`` works for missing tails.
    """
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is inside ``parent`` — callers should pass resolved paths."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_scan_root_candidate(path: Path) -> None:
    """Reject a candidate scan root that would sweep too much of the filesystem.

    Shared between ``config.active_homes`` validation, ``dc scan`` CLI-level
    root validation, and ``apply``/``undo`` report-root validation. A single
    source of truth so a hardening pass here hardens every caller.

    Rules — raise ``ValueError`` on the first failure:

    * empty string is not a path,
    * ``/`` (or anything resolving to ``/``) is rejected,
    * two or fewer path segments after resolution is rejected — e.g. ``/``,
      ``/Users``, ``/Volumes`` on their own,
    * the path must exist on disk,
    * the resolved path must not be inside a hard-coded excluded root
      (``/System``, ``/Library``, ``~/Library``, ``/private/var/folders``,
      ``/Volumes/*/System``, …).

    Callers should pass a raw path — this helper does the ``expanduser`` +
    ``resolve`` itself so every caller applies the same normalisation.
    """
    raw_str = str(path).strip()
    if not raw_str:
        raise ValueError(
            "scan root is an empty string; must be an absolute directory path."
        )
    expanded = Path(raw_str).expanduser()
    resolved = resolve_for_check(expanded)
    if str(resolved) == "/" or len(resolved.parts) <= 2:
        raise ValueError(
            f"scan root too shallow: {raw_str!r} (resolved to {resolved}) — "
            "must be a specific directory such as /Users/<name>, not /, /Users, "
            "or /Volumes."
        )
    if not resolved.exists():
        raise ValueError(f"scan root does not exist on disk: {resolved}")
    if is_excluded_root_path(resolved):
        raise ValueError(
            f"scan root is inside an excluded system location: {resolved}"
        )


def trash_dir_for(path: Path, uid: int | None = None) -> Path:
    """Return the platform Trash directory that would hold ``path``.

    Files on the boot volume land in ``~/.Trash``. Files on external drives
    (``/Volumes/<VOL>/...``) land in ``/Volumes/<VOL>/.Trashes/<uid>/``.
    """
    resolved = resolve_for_check(path)
    parts = resolved.parts
    if len(parts) >= 3 and parts[0] == os.sep and parts[1] == "Volumes":
        volume_root = Path(os.sep) / "Volumes" / parts[2]
        current_uid = uid if uid is not None else os.getuid()
        return volume_root / ".Trashes" / str(current_uid)
    return Path.home() / ".Trash"


def known_trash_dirs(uid: int | None = None) -> list[Path]:
    """Enumerate every Trash directory a legal ``trashed_at_path`` may live in.

    H2: ``dc undo`` must refuse to ``shutil.move`` a source that is not
    inside one of these directories — otherwise a poisoned manifest could
    coerce undo into relocating arbitrary user-owned files (``~/.ssh/id_rsa``,
    etc.) under the guise of a "restore".

    Returns resolved paths so the caller can compare against
    ``candidate.resolve()`` without further normalisation. Non-existent
    volumes' Trashes are still returned — resolve() on a missing path yields
    a comparable absolute path, and the containment check simply fails.
    """
    current_uid = uid if uid is not None else os.getuid()
    dirs: list[Path] = [resolve_for_check(Path.home() / ".Trash")]
    volumes = Path("/Volumes")
    try:
        for vol in volumes.iterdir():
            if not vol.is_dir():
                continue
            dirs.append(
                resolve_for_check(vol / ".Trashes" / str(current_uid))
            )
    except OSError:
        # /Volumes may not exist (non-macOS host) or may not be readable —
        # the ~/.Trash entry alone is still a legal target.
        pass
    return dirs


def is_inside_any_trash(path: Path, uid: int | None = None) -> bool:
    """True if ``path`` (after resolve) is contained in a known Trash directory.

    Used by ``dc undo`` to gate every source path before ``shutil.move``.
    Callers should pass a resolved path or trust ``resolve_for_check`` here
    to catch symlink dodges.
    """
    resolved = resolve_for_check(path)
    return any(is_within(resolved, trash) for trash in known_trash_dirs(uid=uid))


TrashResolver = Callable[[Path], Path]
