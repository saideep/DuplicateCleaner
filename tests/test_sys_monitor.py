"""System-monitoring guards — disk pre-check, CPU throttle, resource sample."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from duplicate_cleaner.sys import monitor as mon


def test_disk_check_aborts_when_below_threshold(tmp_path: Path) -> None:
    class _Fake:
        total = 100 * (1024**3)
        used = 99 * (1024**3)
        free = 1 * (1024**3)  # 1 GB free

    with patch.object(mon.psutil, "disk_usage", return_value=_Fake()):
        with pytest.raises(mon.DiskSpaceError) as ex:
            mon.check_free_disk(tmp_path, min_free_gb=5.0)
        assert "1." in str(ex.value)  # includes the actual free-GB figure


def test_disk_check_passes_when_above_threshold(tmp_path: Path) -> None:
    class _Fake:
        total = 100 * (1024**3)
        used = 10 * (1024**3)
        free = 90 * (1024**3)

    with patch.object(mon.psutil, "disk_usage", return_value=_Fake()):
        snap = mon.check_free_disk(tmp_path, min_free_gb=5.0)
        assert snap.free_gb == pytest.approx(90.0, abs=0.01)


def test_maybe_throttle_sleeps_when_cpu_above_threshold() -> None:
    slept: list[float] = []
    with patch.object(mon.psutil, "cpu_percent", return_value=95.0):
        result = mon.maybe_throttle(
            85.0, sleep_seconds=0.2, sleep_fn=slept.append
        )
    assert result is True
    assert slept == [0.2]


def test_maybe_throttle_no_sleep_below_threshold() -> None:
    slept: list[float] = []
    with patch.object(mon.psutil, "cpu_percent", return_value=10.0):
        result = mon.maybe_throttle(
            85.0, sleep_seconds=0.2, sleep_fn=slept.append
        )
    assert result is False
    assert slept == []


def test_maybe_throttle_disabled_when_pct_is_100() -> None:
    slept: list[float] = []
    with patch.object(mon.psutil, "cpu_percent", return_value=99.0):
        assert mon.maybe_throttle(100.0, sleep_fn=slept.append) is False
    assert slept == []


def test_be_polite_never_raises() -> None:
    """os.nice may fail (already at max) and taskpolicy may be missing.
    ``be_polite`` swallows every error — the scan must not abort on politeness.
    """
    mon.be_polite()  # smoke: any failure would raise


def test_throttle_uses_passed_cpu_pct_not_second_sample() -> None:
    """H6: maybe_throttle must respect a caller-supplied cpu_pct so the
    scan loop does exactly one ``cpu_percent`` sample per iteration.
    A stub that would return 0 from ``psutil.cpu_percent`` must NOT be
    consulted when ``cpu_pct=95`` is passed explicitly.
    """
    slept: list[float] = []
    # Force psutil to return a low value; the passed cpu_pct is what should
    # matter, not this sample.
    with patch.object(mon.psutil, "cpu_percent", return_value=0.0) as cpu_mock:
        result = mon.maybe_throttle(
            85.0, cpu_pct=95.0, sleep_seconds=0.1, sleep_fn=slept.append
        )
    assert result is True
    assert slept == [0.1]
    # And the underlying psutil.cpu_percent was NEVER called — the loop
    # already sampled once via ``sample_resources``.
    assert cpu_mock.call_count == 0


def test_throttle_no_sleep_when_passed_cpu_pct_below_threshold() -> None:
    """H6: when the caller passes a below-threshold cpu_pct, no sleep and
    no fallback psutil sampling.
    """
    slept: list[float] = []
    with patch.object(mon.psutil, "cpu_percent", return_value=99.0) as cpu_mock:
        result = mon.maybe_throttle(
            85.0, cpu_pct=50.0, sleep_seconds=0.1, sleep_fn=slept.append
        )
    assert result is False
    assert slept == []
    assert cpu_mock.call_count == 0


def test_be_polite_uses_absolute_taskpolicy_path() -> None:
    """H9: ``be_polite`` must invoke ``/usr/bin/taskpolicy`` — never a
    PATH-resolved lookup that an attacker could shim.
    """
    import os as _os
    import subprocess as _sp
    from unittest.mock import patch as _patch

    with _patch.object(mon.os, "nice", return_value=0), \
         _patch.object(_os.path, "exists", return_value=True), \
         _patch.object(mon.subprocess, "run") as run_mock:
        run_mock.return_value = _sp.CompletedProcess(args=[], returncode=0)
        mon.be_polite()

    assert run_mock.call_count == 1
    args, _kwargs = run_mock.call_args
    argv = args[0]
    assert argv[0] == "/usr/bin/taskpolicy", (
        f"taskpolicy must be the absolute system binary, got {argv[0]!r}"
    )


def test_be_polite_skips_when_taskpolicy_missing() -> None:
    """H9: when ``/usr/bin/taskpolicy`` is absent, ``be_polite`` must NOT
    fall back to ``shutil.which`` — it silently skips the I/O tier bump.
    """
    import os as _os
    from unittest.mock import patch as _patch

    with _patch.object(mon.os, "nice", return_value=0), \
         _patch.object(_os.path, "exists", return_value=False), \
         _patch.object(mon.subprocess, "run") as run_mock:
        mon.be_polite()

    assert run_mock.call_count == 0
