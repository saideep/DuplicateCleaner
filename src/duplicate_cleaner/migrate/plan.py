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

# v0.5-b execute-side states.  ``pending`` is the freshly-materialised entry
# awaiting an upload; ``done`` follows a successful copy + post-upload hash
# match; ``skipped`` covers plan actions that never touch bytes (defer,
# skip, plan-error) plus resume-time skips; ``error`` marks a failed upload
# or a hash-mismatch that trashed the botched destination copy.
MigrationEntryState = Literal["pending", "done", "skipped", "error"]

# Bumped whenever the manifest schema changes shape.  Consumers key off
# this so a future v0.5-c can refuse to load a stale manifest cleanly.
MIGRATE_MANIFEST_VERSION = "0.5.0"


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


class MigrationManifestEntry(BaseModel):
    """One row in a ``dc migrate copy`` run manifest.

    Mirrors :class:`MigrationEntry` for the input plan half, then extends
    with the execute-side fields ``dc migrate cleanup`` and ``dc migrate
    undo`` key off:

    - ``state`` = ``pending`` at manifest-write time, flipped to ``done`` on
      a successful copy + hash match, ``skipped`` when the plan action is
      ``defer`` / ``skip`` / plan-time ``error`` (or when
      ``--resume-from`` finds the entry already done), and ``error`` when
      the copy failed mid-run (upload rejected, hash mismatch, drift, etc.).
    - ``dest_cloud_file_id`` / ``dest_etag`` / ``uploaded_hash`` /
      ``uploaded_hash_algo`` — populated on a ``done`` entry from the
      destination's post-upload re-fetch.  ``verify_migration`` diffs
      ``dest_etag`` against the current cloud etag as a fast pre-check.
    - ``verified`` starts ``False``; the copy loop flips it to ``True`` on
      a byte-level hash match at upload time.  ``verify_migration`` may
      also demote it back to ``False`` when a post-copy verify hash
      mismatch surfaces.  ``verified_ts`` stamps the last successful verify.
    - ``cleanup_done`` / ``source_cloud_trash_id`` — populated by
      ``cleanup_source_after_migration`` when the source's original is
      trashed.  ``undo_migration`` reads both to reverse the cleanup.
    - ``source_blake3`` — stamped by the copy loop from a BLAKE3 tee over
      the outgoing byte stream.  Provides a canonical hash for cross-algo
      pairs (md5 gdrive → sha256 onedrive) so ``dc migrate verify --full``
      can do a real byte-level compare against ``dest_blake3`` instead of
      trusting only the etag.
    - ``dest_blake3`` — stamped by ``dc migrate verify --full`` from a
      streaming BLAKE3 over the destination bytes.  Compared against
      ``source_blake3`` (when present); persisted regardless as an audit
      trail even when only one side of the pair carries BLAKE3.
    - ``error_message`` retains the last failure reason so a run log stays
      self-contained even after the CLI process exits.
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
    state: MigrationEntryState = "pending"
    reason: str = ""
    size_limit_hit: bool = False
    dest_cloud_file_id: str | None = None
    dest_etag: str | None = None
    uploaded_hash_algo: str | None = None
    uploaded_hash: str | None = None
    source_blake3: str | None = None
    dest_blake3: str | None = None
    verified: bool = False
    verified_ts: float | None = None
    cleanup_done: bool = False
    source_cloud_trash_id: str | None = None
    error_message: str | None = None


class MigrationManifest(BaseModel):
    """Top-level envelope for the ``dc migrate copy`` run manifest.

    Written BEFORE the first upload fires so a crash mid-run leaves a
    replayable artifact.  ``manifest_version`` stamps the schema so a
    later ``dc migrate verify`` / ``cleanup`` / ``undo`` can refuse a
    stale shape rather than silently mis-key.
    """

    manifest_version: Literal["0.5.0"] = "0.5.0"
    plan_source_id: str
    plan_dest_id: str
    created_ts: float = Field(default_factory=lambda: datetime.now(UTC).timestamp())
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    plan_path: str = ""
    entries: list[MigrationManifestEntry] = Field(default_factory=list)

    @property
    def counts_by_state(self) -> dict[str, int]:
        """Return ``{state: count}`` for CLI + summary rendering."""
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.state] = out.get(e.state, 0) + 1
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
