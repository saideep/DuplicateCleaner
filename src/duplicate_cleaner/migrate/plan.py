"""Pydantic schemas for the migrate plan file (v0.5-a planning output).

v0.5-a covers the read half of ``dc migrate``: enumerate every eligible
file on the source, decide whether it should be copied, skipped, deferred
(shared / Google-native / etc.), or refused (over destination per-file
limit), and write a plan that v0.5-b's ``dc migrate copy`` will execute.

The plan schema deliberately mirrors the organize plan shape so the
review UX + JSON round-trip pattern stays consistent across features.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

# Bumped whenever the migrate schema changes shape or the action semantics
# shift.  Consumers key off this for forward-compat.
MIGRATE_PLAN_VERSION = "0.5.0"

# Destination per-file limits Graph and Drive currently document.  Kept
# module-level so tests can override them and the CLI can quote them in
# error messages.  Google Drive: 5 TB.  OneDrive Personal: 250 GB.
GDRIVE_MAX_FILE_SIZE_BYTES: int = 5 * 1024 * 1024 * 1024 * 1024  # 5 TB
ONEDRIVE_MAX_FILE_SIZE_BYTES: int = 250 * 1024 * 1024 * 1024  # 250 GB


MigrationAction = Literal["copy", "skip", "defer", "error"]


class MigrationFilter(BaseModel):
    """Input to :func:`plan_migration` — filters + safety knobs.

    ``include_globs`` are POSIX-shell globs against the source path
    (``**/*.pdf`` matches every PDF); non-matching files are silently
    dropped from the plan.  ``exclude_globs`` is the negative form.  When
    ``include_globs`` is empty every file passes the include gate.

    ``min_size`` / ``max_size`` bound the eligible file size (bytes).
    Files below ``min_size`` are silently dropped; files above ``max_size``
    are silently dropped (per-file destination limits are enforced
    separately as ``action="error"`` so the plan surfaces them loudly).

    ``exclude_shared`` and ``exclude_google_native`` mirror the v0.2
    invariants: shared cloud files are informational-only, Google-native
    docs (application/vnd.google-apps.*) have no downloadable bytes.
    Setting either False is not currently supported by the planner —
    they're here for future ``--include-shared`` opt-in surface.
    """

    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    min_size: int = 0
    max_size: int | None = None
    exclude_shared: bool = True
    exclude_google_native: bool = True


class MigrationEntry(BaseModel):
    """One source file's proposed disposition in the destination.

    ``source_hash`` is the algo-tagged foreign hash carried on the source
    :class:`FileRecord` (``md5:<hex>`` for Google Drive, ``sha256:<hex>``
    for OneDrive).  When the source is local it is the BLAKE3 hex digest
    (no algo prefix — matches ``HashedRecord.full_hash``).

    ``dest_expected_path`` is the POSIX-style path v0.5-b will hand to
    ``Source.upload``.  Never absolute, never containing ``..``, always
    inside the destination root.
    """

    source_id: str
    source_file_id: str | None = None
    source_path: str
    source_etag: str | None = None
    source_size: int
    source_hash: str | None = None
    source_mime: str | None = None
    dest_expected_path: str
    action: MigrationAction
    reason: str
    size_limit_hit: bool = False


class MigrationPlan(BaseModel):
    """Top-level plan artifact written by ``dc migrate plan``.

    v0.5-a: consumed by v0.5-b's ``dc migrate copy`` + ``verify`` + ``cleanup``
    sub-commands.  Every field is re-validated at load time so hand-edits
    (or a stale plan generated against an older schema) surface loudly.
    """

    plan_version: Literal["0.5.0"] = "0.5.0"
    source_id: str
    dest_id: str
    generated_ts: float = Field(default_factory=lambda: datetime.now(UTC).timestamp())
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    filter_summary: str = ""
    entries: list[MigrationEntry] = Field(default_factory=list)

    @property
    def counts_by_action(self) -> dict[str, int]:
        """Return ``{action: count}`` for CLI + template rendering."""
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.action] = out.get(e.action, 0) + 1
        return out


def dest_size_limit_for(dest_id: str) -> int | None:
    """Return the per-file byte cap for the named destination source.

    v0.5-a covers Google Drive + OneDrive Personal only.  A destination
    whose ``source_id`` does not match either provider returns ``None``
    — the planner then applies no size-limit gate (``action="error"``
    stays off).
    """
    if dest_id.startswith("gdrive:"):
        return GDRIVE_MAX_FILE_SIZE_BYTES
    if dest_id.startswith("onedrive:"):
        return ONEDRIVE_MAX_FILE_SIZE_BYTES
    return None
