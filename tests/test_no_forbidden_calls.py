"""Enforces the safety guarantee: no destructive filesystem calls in library code."""
from __future__ import annotations

import re
from pathlib import Path

# Patterns that would delete without going through the Trash.
_FORBIDDEN: list[re.Pattern[str]] = [
    re.compile(r"\bos\.remove\("),
    re.compile(r"\bos\.unlink\("),
    re.compile(r"\bos\.rmdir\("),
    re.compile(r"\.unlink\("),
    re.compile(r"\.rmdir\("),
    re.compile(r"\bshutil\.rmtree\("),
    re.compile(r"\bos\.system\("),
    re.compile(r"\bos\.execvp\("),
    re.compile(r"\bos\.execve\("),
    re.compile(r"\bos\.execv\("),
    # Explicit re-import forms that would bypass a naive grep for `os.remove`.
    re.compile(r"\bfrom\s+os\s+import\s+remove\b"),
    re.compile(r"\bfrom\s+os\s+import\s+unlink\b"),
    re.compile(r"\bfrom\s+os\s+import\s+rmdir\b"),
    re.compile(r"\bfrom\s+shutil\s+import\s+rmtree\b"),
    re.compile(r"\bimport\s+os\s+as\s+"),
    re.compile(r"\bimport\s+shutil\s+as\s+"),
]

# ``shutil.move`` is the legitimate restore primitive in ``apply/undo.py``.
# Anywhere else, treat it as forbidden — a general ``.move()`` call is easy
# to reach for and a copy-then-delete is not what we want.  Match on the
# path relative to ``src/duplicate_cleaner/`` so a future ``undo.py`` under
# a different module (``src/duplicate_cleaner/other/undo.py``) would still
# trip the guard.
_SHUTIL_MOVE_PAT = re.compile(r"\bshutil\.move\(")
_SHUTIL_MOVE_ALLOWED_RELPATHS: frozenset[str] = frozenset(
    {"apply/undo.py", "organize/undo.py"}
)

# subprocess.run + a bare 'rm' string literal in the same file is a red
# flag — shelling out to /bin/rm would bypass Trash entirely. Applied per
# file (not per line) so the check is hard to weaken by splitting across
# variables.
_SUBPROCESS_PAT = re.compile(r"\bsubprocess\.\w+\(")
_RM_LITERAL_PAT = re.compile(r"""(?:["']rm["']|["']rm\s|/rm["'])""")


def test_source_has_no_forbidden_destructive_calls() -> None:
    src = Path(__file__).parent.parent / "src" / "duplicate_cleaner"
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        text = py.read_text()
        rel = py.relative_to(src).as_posix()
        for pat in _FORBIDDEN:
            for m in pat.finditer(text):
                offenders.append(f"{rel}: {m.group(0)}")

        # shutil.move: forbidden everywhere except the whitelisted undo path.
        if rel not in _SHUTIL_MOVE_ALLOWED_RELPATHS:
            for m in _SHUTIL_MOVE_PAT.finditer(text):
                offenders.append(f"{rel}: {m.group(0)}")

        # subprocess + 'rm' string literal in the same file.
        if _SUBPROCESS_PAT.search(text) and _RM_LITERAL_PAT.search(text):
            offenders.append(
                f"{rel}: subprocess call with a bare 'rm' string literal"
            )

    assert not offenders, (
        "Forbidden destructive calls in library code — use send2trash instead: "
        + ", ".join(offenders)
    )


def test_shutil_move_allowlist_uses_path_relative_match() -> None:
    """B10: a hypothetical ``other/undo.py`` must not satisfy the allowlist."""
    # The allowlist is populated with full relpaths (``apply/undo.py``), never
    # basenames (``undo.py``).  If someone regresses to a bare basename, a
    # future undo.py placed elsewhere would silently gain shutil.move rights.
    for rel in _SHUTIL_MOVE_ALLOWED_RELPATHS:
        assert "/" in rel, (
            f"allowlist entry {rel!r} is a bare basename; use "
            "'module/undo.py' form so the check is path-relative."
        )
