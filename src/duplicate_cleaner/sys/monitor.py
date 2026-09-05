"""Scan-time politeness: disk pre-check, CPU throttle, nice + taskpolicy.

Rationale: ``dc scan`` walks and hashes large trees on a machine the user is
also using for foreground work. Every helper here trades a small amount of
throughput for headroom so foreground apps stay responsive.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil  # type: ignore[import-untyped]

# H9: hard-code the system ``taskpolicy`` binary — never trust $PATH. An
# attacker with write access to an early PATH entry (``~/bin``,
# ``/opt/homebrew/bin``, …) could otherwise drop a same-named shim that
# runs with this process's UID during ``dc scan``.
_TASKPOLICY_BINARY = "/usr/bin/taskpolicy"

log = logging.getLogger(__name__)

# One byte per gigabyte constant, kept as a name so unit conversions read
# clearly at the call site.
_BYTES_PER_GB = 1024**3


class DiskSpaceError(RuntimeError):
    """Raised when a required volume has less free space than the config demands."""


@dataclass(frozen=True)
class DiskSnapshot:
    """Instantaneous free / total bytes on some volume."""

    path: Path
    total_bytes: int
    free_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / _BYTES_PER_GB


@dataclass(frozen=True)
class ResourceSnapshot:
    """One sample of process + host resource usage."""

    cpu_pct: float
    rss_bytes: int
    free_disk_gb: float
    files_processed: int


def disk_snapshot(path: Path) -> DiskSnapshot:
    """Free-space snapshot for the volume containing ``path``.

    ``psutil.disk_usage`` walks up to the first mount point, so passing a
    non-existent path raises; callers should pass an existing directory.
    """
    usage = psutil.disk_usage(str(path))
    return DiskSnapshot(
        path=path, total_bytes=int(usage.total), free_bytes=int(usage.free)
    )


def check_free_disk(path: Path, min_free_gb: float) -> DiskSnapshot:
    """Return a DiskSnapshot for ``path`` or raise if free GB is below the floor."""
    snap = disk_snapshot(path)
    if snap.free_gb < min_free_gb:
        raise DiskSpaceError(
            f"Refusing to scan: free space on {path} is "
            f"{snap.free_gb:.2f} GB, below the configured "
            f"min_free_disk_gb of {min_free_gb:.2f} GB. Free space and retry."
        )
    return snap


def be_polite(*, nice_delta: int = 10) -> None:
    """Reduce this process's scheduling priority — best-effort, never raises.

    ``os.nice(delta)`` bumps by ``delta`` (POSIX). On macOS we also try
    ``/usr/bin/taskpolicy -c background`` which flips the kernel I/O tier so
    the scan yields disk bandwidth. Missing ``taskpolicy`` or a non-zero exit
    is logged and ignored — this is a nice-to-have, not a correctness bar.

    H9: the binary path is hard-coded to ``/usr/bin/taskpolicy`` — never
    resolved through ``$PATH`` — so an attacker with write access to an
    early PATH directory cannot inject a shim.
    """
    try:
        os.nice(nice_delta)
    except OSError as exc:
        log.debug("os.nice(%d) failed: %s", nice_delta, exc)

    if not os.path.exists(_TASKPOLICY_BINARY):
        log.debug("taskpolicy not present at %s; skipping I/O throttle",
                  _TASKPOLICY_BINARY)
        return
    try:
        subprocess.run(
            [_TASKPOLICY_BINARY, "-c", "background", "-p", str(os.getpid())],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("taskpolicy invocation failed: %s", exc)


def sample_resources(
    *,
    process: psutil.Process | None,
    scan_path: Path,
    files_processed: int,
) -> ResourceSnapshot:
    """One-shot sampler for the live progress bar.

    ``psutil.cpu_percent(interval=None)`` returns the average since the last
    call, so the Rich progress-bar loop is expected to sample at a steady
    cadence — otherwise the number is stale.
    """
    proc = process or psutil.Process()
    try:
        rss = int(proc.memory_info().rss)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        rss = 0
    cpu = float(psutil.cpu_percent(interval=None))
    try:
        free_gb = disk_snapshot(scan_path).free_gb
    except OSError:
        free_gb = 0.0
    return ResourceSnapshot(
        cpu_pct=cpu,
        rss_bytes=rss,
        free_disk_gb=free_gb,
        files_processed=files_processed,
    )


def maybe_throttle(
    throttle_pct: float,
    *,
    cpu_pct: float | None = None,
    sleep_seconds: float = 0.2,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool:
    """Sleep briefly when host CPU is above ``throttle_pct``. Returns True if slept.

    ``cpu_pct`` should be supplied by the caller (typically from
    :func:`sample_resources`) so the entire loop iteration uses exactly ONE
    ``psutil.cpu_percent`` sample. ``psutil.cpu_percent(interval=None)``
    resets its accumulator on every call, so calling it twice in a row —
    once from ``sample_resources`` and once here — makes the second reading
    ~0 and the throttle never fires (H6).

    If ``cpu_pct`` is ``None``, this function falls back to sampling
    ``psutil.cpu_percent`` itself, preserving the previous public contract
    for callers that don't (yet) share a sample.

    ``sleep_fn`` is injected so tests can assert calls without waiting.
    """
    if throttle_pct >= 100.0:
        return False
    cpu = float(cpu_pct) if cpu_pct is not None else float(
        psutil.cpu_percent(interval=None)
    )
    if cpu <= throttle_pct:
        return False
    sleeper = sleep_fn or time.sleep
    sleeper(sleep_seconds)
    return True
