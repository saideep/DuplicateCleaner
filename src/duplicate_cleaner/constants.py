"""Hard-coded exclusions — never scanned regardless of config."""
from __future__ import annotations

import os
from pathlib import Path

# Absolute-path prefixes (or exact paths) that are never traversed.
#
# ``/etc``, ``/var``, and ``/tmp`` are symlinks into ``/private`` on macOS,
# so we list the sensitive subtrees under both their canonical and resolved
# names. We do NOT blanket-exclude ``/private`` because ``/private/tmp``
# (i.e. ``/tmp``) is legitimate user scratch space. But ``/var/folders``
# (``$TMPDIR`` on macOS) holds LaunchServices caches, saved app state, and
# in-flight document saves — scanning it can race with running apps, so it
# IS excluded. Tests that rely on pytest's ``tmp_path`` must point pytest at
# ``/tmp`` via ``--basetemp`` (see ``pyproject.toml``).
EXCLUDED_ROOTS: tuple[str, ...] = (
    "/System",
    "/Library",
    "/Applications",
    "/usr",
    "/opt",
    "/sbin",
    "/bin",
    "/etc",
    "/private/etc",
    "/var/db",
    "/private/var/db",
    "/var/log",
    "/private/var/log",
    "/var/vm",
    "/private/var/vm",
    "/var/root",
    "/private/var/root",
    "/var/audit",
    "/private/var/audit",
    "/var/folders",
    "/private/var/folders",
    "/var/tmp",
    "/private/var/tmp",
    "/Users/Shared",
    str(Path.home() / "Library"),
)

# Directory names excluded anywhere in the tree.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)

# Any path containing one of these substrings is excluded.
#
# ``.Trashes`` on external drives contains files being trashed by other
# tools; entering it can race with those tools. Never scan it.
EXCLUDED_PATH_SUBSTRINGS: tuple[str, ...] = (
    f"{os.sep}.git{os.sep}objects{os.sep}",
    f"{os.sep}System Volume Information{os.sep}",
    f"{os.sep}.Trashes{os.sep}",
)

# iCloud placeholder files — the real bytes are not on disk.
EXCLUDED_FILE_SUFFIXES: tuple[str, ...] = (".icloud",)
