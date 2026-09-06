"""Config + scoring weights loaded from the user's XDG config dir."""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

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

    # v0.2 sub-milestone 5e — cross-source keeper preference.
    #
    # ``retained_cloud_order`` is a list of full ``source_id`` values (e.g.
    # ``"gdrive:personal"``, ``"onedrive:main"``) consulted only when a
    # duplicate group has NO local member and multiple cloud members.  The
    # earliest-listed source in the group wins the keeper role.  Cloud
    # sources not in the list sort last (order among them is unstable but
    # deterministic within one run).  Local always wins any cross-source tie
    # — this list has no effect on groups containing a local member.  See
    # ``docs/config.md`` for worked examples.
    retained_cloud_order: list[str] = Field(default_factory=list)

    # v0.3 sub-milestone 5.3-a — organizer discovery.
    #
    # ``organize_confidence_threshold``: files below this classifier score go
    # to ``Unsorted/`` rather than a guessed domain.
    # ``organize_dir_mode``: mode applied to freshly-created destination
    # directories during ``dc organize apply`` (unused in 5.3-a; declared
    # here so users can pin it before apply lands in 5.3-b).
    # ``rename_policy``: LOCKED default ``"preserve"`` — filename bytes are
    # never mutated by ``dc organize`` unless the user has explicitly opted
    # in to a date-prefix policy via config.
    # ``event_gap_hours`` / ``min_event_photos``: photo-video event
    # clustering knobs; see design doc §4.
    # ``enforce_dedup_ordering``: when True, ``dc organize discover``
    # refuses to run if any pending dedup group exists.  Default False —
    # emits a soft warning instead.
    organize_confidence_threshold: float = 0.75
    organize_dir_mode: int = 0o755
    rename_policy: Literal["preserve", "date_prefix", "date_event_prefix"] = "preserve"
    event_gap_hours: int = 12
    min_event_photos: int = 5
    enforce_dedup_ordering: bool = False

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
    # v0.2 sub-milestone 5e — cross-source signals.  The two marker-only
    # weights (``is_shared_file``, ``is_singleton_across_sources``) do NOT
    # additively affect the score — they force ``is_informational=True``
    # at the same layer as hardlinks and APFS clones.  Kept in the weight
    # map so config-round-trip stays stable and so future revisions can
    # experiment without touching every construction site.
    "cloud_when_local_exists": -3.0,
    "is_shared_file": 0.0,
    "is_singleton_across_sources": 0.0,
}


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load configuration; raises if it does not exist yet.

    v0.2 sub-milestone 5e: cross-source preference lives in the
    ``[cross_source_preference]`` TOML section.  ``retained_cloud_order``
    is spliced into the top-level config dict here so users can keep the
    file grouped without polluting the flat model with a sub-model type.

    v0.3 sub-milestone 5.3-a: organizer knobs may live in an ``[organize]``
    TOML section for grouping; keys are flattened into the top-level
    config dict with the ``organize_`` prefix already present.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run `dc init` to create one."
        )
    with path.open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)
    pref = data.pop("cross_source_preference", None)
    if isinstance(pref, dict):
        order = pref.get("retained_cloud_order")
        if order is not None:
            data.setdefault("retained_cloud_order", order)
    organize = data.pop("organize", None)
    if isinstance(organize, dict):
        # ``[organize.domains]`` sub-table is reserved for future rule
        # customization; keys we recognize today are flattened here.
        organize.pop("domains", None)
        for key, value in organize.items():
            if key.startswith("organize_"):
                data.setdefault(key, value)
            else:
                data.setdefault(f"organize_{key}" if key in _ORGANIZE_PREFIXED else key, value)
    return Config.model_validate(data)


# Organizer TOML keys that live under ``[organize]`` without the
# ``organize_`` prefix (for readability) — the loader adds the prefix.
_ORGANIZE_PREFIXED: frozenset[str] = frozenset({"confidence_threshold", "dir_mode"})


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
