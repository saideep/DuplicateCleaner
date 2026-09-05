"""Config + scoring weights loaded from the user's XDG config dir."""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from duplicate_cleaner.paths import (
    resolve_for_check,
    validate_scan_root_candidate,
)

CONFIG_DIR = Path.home() / ".config" / "duplicate_cleaner"
CONFIG_PATH = CONFIG_DIR / "config.toml"
WEIGHTS_PATH = CONFIG_DIR / "weights.json"

# Default macOS bundle extensions — directories whose names end in one of
# these are hashed as a single atomic unit rather than descended into.
DEFAULT_BUNDLE_EXTENSIONS: tuple[str, ...] = (
    ".app",
    ".pages",
    ".numbers",
    ".keynote",
    ".rtfd",
    ".sparsebundle",
    ".xcodeproj",
    ".playground",
    ".framework",
    ".bundle",
)


def _default_max_workers() -> int:
    """Half the reported CPU count, floor 1 — leaves the machine responsive."""
    cpu = os.cpu_count() or 2
    return max(1, cpu // 2)


class Config(BaseModel):
    """User-editable configuration."""

    active_homes: list[Path] = Field(default_factory=list)
    min_size_bytes: int = 4096
    exclude_globs: list[str] = Field(default_factory=list)
    follow_symlinks: bool = False

    # v0.1.1 — archive recursion.
    max_archive_depth: int = 2

    # v0.1.1 — macOS bundle handling.
    bundle_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BUNDLE_EXTENSIONS)
    )

    # v0.1.1 — system monitoring.
    max_workers: int = Field(default_factory=_default_max_workers)
    throttle_on_cpu_pct: float = 85.0
    min_free_disk_gb: float = 5.0

    @field_validator("max_workers")
    @classmethod
    def _validate_max_workers(cls, v: int) -> int:
        """Floor at 1 — zero workers would deadlock the pipeline."""
        return max(1, int(v))

    @field_validator("bundle_extensions")
    @classmethod
    def _validate_bundle_extensions(cls, v: list[str]) -> list[str]:
        """Normalise to lower-case, dot-prefixed extensions."""
        out: list[str] = []
        for raw in v:
            s = str(raw).strip().lower()
            if not s:
                continue
            if not s.startswith("."):
                s = "." + s
            out.append(s)
        return out

    @field_validator("active_homes")
    @classmethod
    def _validate_active_homes(cls, v: list[Path]) -> list[Path]:
        """Reject bogus, missing, or system-owned active_home entries.

        active_homes must be specific existing user directories. Empty
        strings, ``/``, and shallow paths (``/Users`` on its own) are
        rejected because they would make the scorer treat every file as
        living in the active home. Paths that resolve inside a hard-coded
        excluded root (``/Library``, ``/System``, ``/private``, …) are
        rejected because scanning there is forbidden regardless of intent.

        Delegates to :func:`validate_scan_root_candidate` so ``active_homes``
        and ``report.roots`` are enforced against identical rules.
        """
        resolved: list[Path] = []
        for raw in v:
            try:
                validate_scan_root_candidate(raw)
            except ValueError as e:
                raise ValueError(f"{e}. Edit {CONFIG_PATH}.") from e
            resolved.append(resolve_for_check(Path(str(raw).strip()).expanduser()))
        return resolved


DEFAULT_CONFIG_TOML = """# duplicate_cleaner configuration
#
# Declare one or more directories that are your "live" home(s).
# Duplicates found outside these paths score as archived/backup copies
# and are preferred for discard. Without at least one entry, `dc scan`
# refuses to run.
active_homes = []

# Files smaller than this (in bytes) are ignored.
min_size_bytes = 4096

# Extra glob patterns to exclude, in addition to the built-in list
# (~/Library, /System, /private, node_modules, __pycache__, .git/objects,
#  iCloud .icloud placeholders, etc.).
exclude_globs = []

# Follow symlinks during walk. Default false to avoid loops and duplicates.
follow_symlinks = false

# Archive recursion — how many levels of archive-in-archive to descend.
# Depth 1 = recurse only the outer archive. Depth 2 recurses one level of
# nested archives. Archives at depth > max_archive_depth are hashed as
# opaque blobs, not descended into.
max_archive_depth = 2

# macOS bundles are directories the walker treats as single files.
# bundle_extensions = [".app", ".pages", ".numbers", ".keynote", ".rtfd",
#                     ".sparsebundle", ".xcodeproj", ".playground",
#                     ".framework", ".bundle"]

# System monitoring — how polite the scanner is on your machine.
# max_workers — thread cap for hashing. Default: os.cpu_count() // 2.
# max_workers = 4
# throttle_on_cpu_pct — sleep between hash batches when system CPU exceeds
# this percentage. Set to 100 to disable throttling. Default: 85.
# throttle_on_cpu_pct = 85
# min_free_disk_gb — refuse to scan if free space on the cache volume is
# below this threshold. Default: 5 GB.
# min_free_disk_gb = 5
"""


DEFAULT_WEIGHTS: dict[str, float] = {
    "path_marker_backup": -8.0,
    "filename_copy_marker": -6.0,
    "under_active_home": 4.0,
    "under_inactive_home": -4.0,
    "downloads_transit": -2.0,
    "depth_penalty_per_level": -0.5,
    "newest_mtime": 3.0,
    "oldest_mtime_tiebreak": 1.0,
    "clean_git_repo": 2.0,
    "external_drive_penalty": -2.0,
    "larger_size": 2.0,
}


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load configuration; raises if it does not exist yet."""
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run `dc init` to create one."
        )
    with path.open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)
    return Config.model_validate(data)


def write_default_config(path: Path = CONFIG_PATH) -> None:
    """Write the default config.toml at the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TOML)


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    """Load weights; create defaults on first run."""
    if not path.exists():
        write_default_weights(path)
    with path.open() as f:
        data = json.load(f)
    merged = dict(DEFAULT_WEIGHTS)
    for k, v in data.items():
        merged[k] = float(v)
    return merged


def write_default_weights(path: Path = WEIGHTS_PATH) -> None:
    """Write the default weights.json at the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(DEFAULT_WEIGHTS, f, indent=2, sort_keys=True)
