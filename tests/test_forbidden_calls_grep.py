"""Meta-test: if we inject a destructive call into a copy of the src tree,
does ``test_no_forbidden_calls.py`` catch it?

Rationale — the guarantee "no destructive calls" is only as good as the
grep enforcing it. This test copies ``src/duplicate_cleaner/`` to a
temp path, injects a violating file, and runs the same offender scan
against the copy. The scan MUST report an offender for each injection.

Uses only the ``_FORBIDDEN`` / ``_SHUTIL_MOVE_PAT`` patterns already
exposed by ``test_no_forbidden_calls`` — we don't duplicate them here.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tests.test_no_forbidden_calls import (
    _FORBIDDEN,
    _RM_LITERAL_PAT,
    _SHUTIL_MOVE_ALLOWED_RELPATHS,
    _SHUTIL_MOVE_PAT,
    _SUBPROCESS_PAT,
)


def _scan_for_offenders(src: Path) -> list[str]:
    """Reproduce the offender-scan loop from test_no_forbidden_calls.py."""
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        text = py.read_text()
        rel = py.relative_to(src).as_posix()
        for pat in _FORBIDDEN:
            for m in pat.finditer(text):
                offenders.append(f"{rel}: {m.group(0)}")
        if rel not in _SHUTIL_MOVE_ALLOWED_RELPATHS:
            for m in _SHUTIL_MOVE_PAT.finditer(text):
                offenders.append(f"{rel}: {m.group(0)}")
        if _SUBPROCESS_PAT.search(text) and _RM_LITERAL_PAT.search(text):
            offenders.append(f"{rel}: subprocess+rm literal")
    return offenders


@pytest.fixture()
def src_copy(tmp_path: Path) -> Path:
    """Copy the real src tree to a scratch location where we can mutate it."""
    src = Path(__file__).parent.parent / "src" / "duplicate_cleaner"
    dest = tmp_path / "duplicate_cleaner"
    shutil.copytree(src, dest)
    # Confirm the clean copy has no offenders — the baseline must be green
    # or downstream assertions have no meaning.
    baseline = _scan_for_offenders(dest)
    assert baseline == [], (
        "baseline src tree is not clean; meta-test premise fails: "
        f"{baseline}"
    )
    return dest


@pytest.mark.parametrize(
    "injection",
    [
        # Every pattern the offender scan claims to catch — one line per
        # regex family so a hole in the scan is a hole in this test.
        'os.remove("x")',
        'os.unlink("x")',
        'os.rmdir("x")',
        'Path("x").unlink()',
        'Path("x").rmdir()',
        'shutil.rmtree("x")',
        'os.system("rm -rf /")',
        'os.execvp("rm", ["rm", "-rf", "/"])',
        'from os import remove',
        'from shutil import rmtree',
        'import os as _o',
        'shutil.move("a", "b")',
    ],
)
def test_grep_catches_injected_violation(
    src_copy: Path, injection: str
) -> None:
    """Injecting a violating line MUST cause the offender scan to fire."""
    violator = src_copy / "injected_violation.py"
    violator.write_text(
        "# injected by test_forbidden_calls_grep\n"
        f"{injection}\n"
    )
    try:
        offenders = _scan_for_offenders(src_copy)
        assert offenders, (
            f"Injected {injection!r} but the offender scan reported nothing"
        )
        # And the offender file must be the one we injected.
        assert any("injected_violation.py" in o for o in offenders), (
            f"Injection not attributed to injected file: {offenders}"
        )
    finally:
        violator.unlink()


def test_grep_catches_subprocess_shellout_rm(src_copy: Path) -> None:
    """subprocess.run(['rm', '-rf', ...]) is the classic shell-out escape.

    Must be caught by the combined subprocess-and-rm-literal check.
    """
    violator = src_copy / "shellout.py"
    violator.write_text(
        "import subprocess\n"
        "subprocess.run(['rm', '-rf', '/tmp/x'])\n"
    )
    try:
        offenders = _scan_for_offenders(src_copy)
        # Should trigger either the subprocess-rm heuristic or an existing pattern.
        assert any("shellout.py" in o for o in offenders)
    finally:
        violator.unlink()


def test_baseline_source_tree_is_clean() -> None:
    """Redundant with test_no_forbidden_calls, but self-documenting: a
    baseline scan of the real src tree must report zero offenders.
    """
    src = Path(__file__).parent.parent / "src" / "duplicate_cleaner"
    offenders = _scan_for_offenders(src)
    assert offenders == [], (
        f"Baseline offender scan is not clean: {offenders}"
    )


def test_regex_matches_intended_pattern() -> None:
    """Belt-and-braces: assert each _FORBIDDEN regex matches at least one
    documented example. If a regex breaks silently (e.g. an escape drift),
    this fires."""
    corpus = "\n".join(
        [
            'os.remove("x")',
            'os.unlink("x")',
            'os.rmdir("x")',
            'p.unlink()',
            'p.rmdir()',
            'shutil.rmtree("x")',
            'os.system("rm")',
            'os.execvp("x", [])',
            'os.execve("x", [], {})',
            'os.execv("x", [])',
            'from os import remove',
            'from os import unlink',
            'from os import rmdir',
            'from shutil import rmtree',
            'import os as _o',
            'import shutil as _s',
        ]
    )
    for pat in _FORBIDDEN:
        assert pat.search(corpus), (
            f"regex {pat.pattern} does not match any documented example — "
            "it may have drifted"
        )
    # And the sub-detectors used together.
    assert re.compile(r"\bos\.\w+").search(corpus)
