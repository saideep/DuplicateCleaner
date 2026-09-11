# Changelog

All notable changes to this project will be documented in this file. Format: Keep a Changelog. Versioning: SemVer.

## [Unreleased]

### v0.6.1 — Google Photos trash scope escalation (2026-09-11)

Per-account escalation flow so users can re-consent Google Photos accounts from `photoslibrary.readonly` (v0.6 default) to the full `photoslibrary` scope.  Adding trash for `gphotos:personal` doesn't grant it for `gphotos:family` — each token file carries its own `has_trash` flag.

Added:

- `dc auth grant-gphotos-trash <account_id>` — re-runs OAuth against the full `photoslibrary` scope, updates the account's token file with the new scopes and `has_trash=True`.  Refuses on non-gphotos accounts (exit 2) and on missing tokens (exit 1).
- `dc auth revoke-gphotos-trash <account_id>` — mirror flow that downgrades back to `photoslibrary.readonly` and flips `has_trash=False`.
- `dc auth list` grows a `scope` column showing `read-only` / `trash-enabled` / `read-only (permanent)` per account.
- `has_trash: bool = False` param on `GooglePhotosSource.__init__` (default preserves v0.6 behaviour).  Threaded from the token file via `_build_gphotos_source(for_apply=True)` at every apply-time / migrate construction site.
- `trash_enabled_source_ids: frozenset[str] | set[str] | None = None` kwarg on `score.rules.score_group` and `score_groups`.  Gphotos accounts in the set are NOT marked informational; they flow through the normal cross-source scoring so a trashable-but-currently-cloud photo can be proposed as a discard.  iCloud is unconditionally informational (permanently read-only, no escalation path).
- 10 new tests in `tests/test_gphotos_trash_escalation.py` covering the three-layer gate, the CLI grant flow, the scorer's trash-enabled bypass, and the auth-list scope column.

Changed:

- `GooglePhotosSource.move_to_trash` refactored into a three-layer gate: (1) `is_read_only_scan=True` refuses at scan time; (2) `has_trash=False` refuses with an actionable `SourceError` naming `dc auth grant-gphotos-trash <id>`; (3) trash-enabled account raises a distinct `SourceError` naming the Google Photos Library API v1 limitation and pointing at [https://photos.google.com/trash](https://photos.google.com/trash) for the manual step.  Google Photos Library API v1 does NOT expose a library-wide trash endpoint — the escalation infrastructure is live but the final API call is blocked by Google's own capability surface.  When Google reintroduces a library-scoped delete, the API call drops in between layer 2 and layer 3 with no surrounding-rail changes needed.
- `dc auth add gphotos` stamps `has_trash: False` on the freshly-written token so v0.6 and v0.6.1 token blobs share the same shape.
- `docs/cloud-oauth-setup.md` — new "Scope escalation (v0.6.1)" section covers the grant/revoke commands and the Google API-capability wall.

Tests: 497 → 507 (10 new, 1 updated in `tests/test_sources_gphotos.py`).

### v0.6-patch — closes pass-16 audit findings (2026-09-11)

Sub-milestone patch closing all 9 audit pass-16 findings on top of v0.6 in a single release. Every finding lands with a matching test.

Fixed:

- Scorer marks `gphotos:` / `icloud:` records as informational (M1, must-fix). Without this, `dc apply --commit` refused the entire run on any mixed local + gphotos/icloud scan — the scorer proposed the read-only member as a discard, then the mover pre-flight tripwire aborted the whole run. Symmetric to the existing `is_shared` invariant.
- iCloud `read_bytes` validates the local file path against EXCLUDED_ROOTS and library-bundle containment before opening it (M2, must-fix). Defense-in-depth against a tampered Photos SQLite database returning `/etc/passwd` or a symlink into `/System`.
- `paths._PROVIDER_ID_PATTERNS` gains gphotos + icloud entries (M3). The mover-level `cloud_file_id` shape gate now covers all four cloud providers; a poisoned report with `cloud_file_id="../evil"` on gphotos/icloud is refused at `apply_report` pre-flight.
- Google Photos CDN stream is retried on 429 / 5xx / `httpx.TransportError` (M4). Symmetric to the `mediaItems.get` retry already in place; matches `sources/onedrive.py::_is_retryable_http_error`.
- Google Photos CDN stream checks `content-type` and cumulative byte count (M5). A 200 response with `content-type: text/html` (session-expired redirect) or an empty body is refused instead of silently BLAKE3-hashed.
- iCloud `_open_db` catches `OSError`, `PermissionError`, and `sqlite3.DatabaseError` (M6). Surfaces a typed `SourceError` with the actionable Full Disk Access hint instead of a raw stack trace.
- Google Photos `move_to_trash` raises `SourceError` unconditionally, matching the simpler iCloud shape (M7, simplification). The dropped `is_read_only_scan` `PermissionError` branch was unreachable in production since every apply-time construction site hard-codes `is_read_only_scan=True`.
- iCloud `list_files` uses try/finally so the "skipped N stubs" warning fires even when the caller closes the generator early (M9). `dc auth test icloud:*` reads only the first record; without this the stub-count warning was silent for the common case.

Added:

- Integration tests for the apply-time refusal of hand-crafted gphotos / icloud discards (M8). Locks in the read-only pre-flight tripwire against a future refactor that flips `is_read_only_scan` off.

Tests: 487 → 497 (10 new tests across `test_scoring_cross_source.py`, `test_sources_gphotos.py`, `test_sources_icloud.py`, `test_mover_source_id_dispatch.py`, `test_apply_cloud_dispatch.py`).

### v0.6 — Google Photos + iCloud Photos (2026-09-11)

Two new photo-native sources scan alongside Drive / OneDrive / local. Both are read-only in v0.6. Google Photos trashing lands in v0.6.1 (scope escalation from `photoslibrary.readonly` requires user re-consent); iCloud Photos deletion goes via the Photos.app permanently.

Added:

- `GooglePhotosSource` in `src/duplicate_cleaner/sources/gphotos.py`. Enumerates via `mediaItems.list` (Photos Library v1). Google Photos does not expose per-item MD5/SHA-256 in the list response, so `foreign_hash=None` on every record and the reconciliation stage downloads bytes and BLAKE3s them. `baseUrl` values expire after ~60 min, so `read_bytes` re-fetches the media item first and streams the `=d`-suffixed original-quality bytes via an unauthenticated `httpx` client so the OAuth bearer never lands on Google's signed storage host. Composite etag = `f"{id}:{creationTime}"`. Retry piggy-backs on tenacity (429 + 5xx backoff, same as gdrive). `move_to_trash` / `restore_from_trash` raise a typed `SourceError` pointing at v0.6.1.
- `iCloudPhotosSource` in `src/duplicate_cleaner/sources/iclouddrive_photos.py`. Reads the local `~/Pictures/Photos Library.photoslibrary` bundle via `osxphotos` — no OAuth, no cloud API. Photos not downloaded locally (iCloud-only stubs with `photo.path is None`) are skipped and the total is logged with an actionable hint ("Toggle 'Download Originals to This Mac' in Photos → Preferences → iCloud"). `read_bytes` streams the local file directly. Composite etag = `f"{uuid}:{date_modified_iso}"`. `move_to_trash` / `restore_from_trash` / `upload` all raise `SourceError` — the source is permanently read-only because `osxphotos` is a reader library, not a writer.
- `dc auth add gphotos --client-secret PATH.json` — new OAuth registration branch. BYO-only per the existing invariant; `_TO_REPLACE` sentinel refuses the placeholder client id with an actionable message naming the Google Photos Library API (distinct from the Drive branch).
- `dc auth add icloud [--library-path PATH]` — no OAuth. Verifies the local Photos.photoslibrary bundle exists and registers an accounts.toml row with `type="icloud"`. No token file.
- `dc scan --sources ...` now accepts `gphotos:<label>` and `icloud:<label>`.
- Optional-extra `[icloud]` in `pyproject.toml` pinning `osxphotos>=0.72.0,<0.73`.

Safety additions (two new invariants tracked in [AUDIT_LOG.md](docs/AUDIT_LOG.md#invariants-do-not-weaken)):

- Google Photos source is read-only in v0.6; `move_to_trash` refused pending v0.6.1 scope escalation.
- iCloud Photos source is read-only permanently; deletion goes via the Photos.app (osxphotos is a reader, not writer).

Preserved invariants:

- BYO OAuth only for Google Photos (bundled `_TO_REPLACE` sentinel guard identical to gdrive).
- Cloud paths never `.resolve()`d.
- `is_read_only_scan=True` default on both new sources.
- Cloud file id shape gate (Base64url, ≥ 20 chars) fires BEFORE any Photos API URL is interpolated.
- Shared cloud files informational-only — under `photoslibrary.readonly` the user only sees own items, so v0.6 emits `is_shared=False` on every Google Photos record.

Tests: 455 → 487 (17 new in `tests/test_sources_gphotos.py`, 12 new in `tests/test_sources_icloud.py`, 3 new in `tests/test_cli_auth.py`).

Explicitly deferred to v0.6.1: the full-scope escalation for Google Photos trash requires user re-consent. Until then, `dc apply` refuses to trash Google Photos entries with a clear message pointing the user at the Google Photos app / web UI.

### v0.5-b — Cloud consolidation execution (2026-09-10)

Second half of `dc migrate` — the copy / verify / cleanup / undo pipeline. v0.5-a shipped the planner + `Source.upload` protocol; v0.5-b turns the plan into actual uploads with the same safety envelope the dedup mover carries.

Added:

- `src/duplicate_cleaner/migrate/mover.py::execute_migration(plan_path, manifest_path, *, commit, sources_by_id, resume_from, max_bandwidth_mbps)` — the copy loop. Dry-run by default; `--commit` gates uploads. Per-entry: drift-check → `source.read_bytes` → optional bandwidth throttle → `dest.upload` → post-upload hash verify → atomic manifest flush. Same-algo hash mismatch trashes the destination BEFORE the manifest advances so the source stays untouched. Cross-algo pairs (md5 gdrive → sha256 onedrive) are optimistically accepted at copy time; `dc migrate verify --full` canonicalises via BLAKE3 for the strict check.
- `src/duplicate_cleaner/migrate/verify.py::verify_migration(manifest_path, *, sources_by_id, full)` — re-checks every done manifest entry. Default mode: metadata-only via `Source.check_drift`. `--full` mode: streams the destination bytes through BLAKE3.
- `src/duplicate_cleaner/migrate/cleanup.py::cleanup_source_after_migration(manifest_path, *, commit, sources_by_id)` — trashes source originals for done + verified entries. Refuses up-front (before any `move_to_trash` call fires) if any done entry has `verified=False` or `verified_ts=None`, pointing the user at `dc migrate verify`.
- `src/duplicate_cleaner/migrate/undo.py::undo_migration(manifest_path, *, sources_by_id)` — reverses cleanup + copy: source originals restored via `restore_from_trash`, destination copies trashed via `move_to_trash`. Entries with `state="pending"` / `"skipped"` / `"error"` are untouched.
- Four new CLI sub-commands: `dc migrate copy PLAN.json [--commit] [--max-bandwidth-mbps N] [--resume-from MANIFEST] [--out-manifest PATH]`, `dc migrate verify MANIFEST.json [--full]`, `dc migrate cleanup MANIFEST.json [--commit]`, `dc migrate undo MANIFEST.json`.

Safety additions (two new invariants tracked in [AUDIT_LOG.md](docs/AUDIT_LOG.md#invariants-do-not-weaken)):

- **Migration post-upload hash verify — mismatch trashes destination before source is touched.** Same-algo pairs compare directly at copy time. Cross-algo pairs are optimistically accepted; the mismatch surface moves to `dc migrate verify --full`. Either way, a same-algo mismatch at copy time trashes the botched destination copy immediately.
- **Cleanup refuses without verify.** `dc migrate cleanup` raises `CleanupError` up-front if any done entry has `verified=False` OR `verified_ts=None`. The check fires before any source `move_to_trash` call.

Preserved invariants:

- Dry-run default on `copy` / `cleanup`.
- Atomic manifest write (tempfile + fsync + `os.replace` + parent-dir fsync) BEFORE the first upload, re-flushed after every per-entry state change.
- Drift check via etag runs BEFORE every upload; mismatch aborts the whole run.
- `is_read_only_scan` tripwire fires before any HTTP call in `copy` / `cleanup` / `undo`.
- BYO OAuth only.
- Cloud paths never `.resolve()`d.
- Cloud discards go to cloud trash; undo restores via the same API.

Tests: 422 → 445 (11 new in `tests/test_migrate_copy.py`, 4 new in `tests/test_migrate_verify.py`, 4 new in `tests/test_migrate_cleanup.py`, 4 new in `tests/test_migrate_undo.py`).

### v0.5-a — Cloud consolidation planner (2026-09-10)

First half of `dc migrate`. Adds the `Source.upload` write protocol and the read-only migration planner. Copy / verify / cleanup / undo land in v0.5-b.

Added:

- `Source.upload(dest_path, byte_stream, expected_size) -> UploadResult` on the protocol. Every source that supports being a migrate destination must implement it. `LocalFileSystemSource.upload` raises `NotImplementedError` (stretch goal deferred to v0.6+). `GoogleDriveSource.upload` chunks through the `files().create` endpoint (single-shot below 5 MB, resumable session with 8 MB chunks above), resolves or creates the folder chain, and re-fetches `md5Checksum` + `modifiedTime` so `UploadResult` carries the destination-side digest and composite etag. `OneDriveSource.upload` uses `PUT /me/drive/root:/{path}:/content` for files ≤ 4 MB and `createUploadSession` + 10 MB chunked PUTs (with `Content-Range`) for larger files; re-fetches `file.hashes.sha256Hash`.
- `src/duplicate_cleaner/migrate/` package with `plan.py` (Pydantic `MigrationPlan` / `MigrationEntry` / `MigrationFilter`), `planner.py` (`plan_migration`), `render.py` (JSON + HTML), and `templates/migration-plan.html.j2`.
- `dc migrate plan --from A --to B --report DIR` CLI sub-command with `--filter` / `--exclude` / `--dest-size-limit-gb` knobs.
- New user doc `docs/migrate.md`.

Safety additions:

- Every `Source.upload` implementation validates the destination path shape (no absolute, no `..` traversal, no empty segments) BEFORE any network call.
- Every `Source.upload` implementation raises `SourcePermissionError` when the source is constructed with `is_read_only_scan=True`. `dc migrate plan` constructs both source and destination read-only; the tripwire ensures a planner bug cannot write to the destination.
- Shared cloud files stay informational-only: the planner emits `action="defer"` for them and never proposes a `copy`.
- Google-native docs (`application/vnd.google-apps.*`) are deferred: no downloadable bytes.
- Destination per-file cap enforced at plan time. Google Drive 5 TB, OneDrive Personal 250 GB. Files over the cap surface as `action="error"` with `size_limit_hit=True` so users see them loudly instead of silently.

Tests: 405 → 422 (7 new in `tests/test_migrate_plan.py`, 10 new in `tests/test_source_upload_contract.py`).

### v0.4 — Project-tree aggregation (2026-09-07)

Two copies of the same project directory — git repo, npm package, Cargo crate, Go module, etc. — now collapse to a single tree-diff entry in the report instead of surfacing as thousands of per-file matches. Discards trash the whole directory atomically; undo restores it wholesale.

Added:

- `dc scan` picks up project directories automatically. A directory qualifies when its child set contains any of `.git`, `package.json`, `Cargo.toml`, `pom.xml`, `pyproject.toml`, `Pipfile`, `go.mod`, `build.gradle`, `Gemfile`, or a `*.sln` file. Pairs of detected projects with Jaccard similarity at or above `--min-project-similarity` (default `0.90`) collapse to a single `kind="tree"` group.
- HTML report renders a distinct "Project trees" section above "Exact duplicates" with a purple badge, similarity %, identical-file count, and a collapsible per-file tree-diff.
- `dc apply --commit` sends the entire discard directory to Trash via `send2trash` in one call. `dc undo` restores the whole tree via `shutil.move` back from Trash.
- New scoring weights `is_project_tree_backup_copy` (-5) and `git_head_older` (-3) surface in `dc weights show`.

Safety additions:

- Project-tree discards refuse dirty git repos. `git status --porcelain` non-empty → validate-time refusal with an actionable message. Re-checked immediately before the move as defense-in-depth against concurrent edits.
- Project-tree discards require the target to sit inside a declared `active_home`. A whole-directory move has a much larger blast radius than a file move; the safety envelope is proportional.
- Exact-duplicate groups whose members are entirely contained inside a detected project root are removed from the report before `apply` sees them. Structural cohesion enforcement — no per-file split of a project is representable in a plan file.

Two new invariants added to [AUDIT_LOG.md](docs/AUDIT_LOG.md#invariants-do-not-weaken):

- Project-tree discards refuse dirty git repos.
- Project trees move atomically.

Tests: 379 → 397 (13 new in `tests/test_compare_tree.py`, 5 new in `tests/test_apply_tree_discard.py`).

### v0.3 — Organizer (2026-09-06)

Shipped end-to-end: `dc organize discover → review → apply → undo`. Design contract: [docs/design/v0.3-organizer.md](docs/design/v0.3-organizer.md). User-facing docs: [docs/organize.md](docs/organize.md), [docs/cli.md](docs/cli.md), [docs/config.md](docs/config.md), [docs/safety.md](docs/safety.md).

Surface delivered in the milestone:

- **`dc organize` command group with three sub-commands.** `discover` extracts signals and writes a plan JSON plus an HTML view; `review` opens a Rich-based TUI for editing the plan; `apply` creates target folders and moves files with a dry-run default. `undo` reverses a run from its manifest.
- **Nine-domain taxonomy tuned to the user's data.** `HR/Payslips/`, `HR/OfferLetters/`, `HR/Tax/`, `Personal/IDs/`, `Personal/Insurance/`, `Personal/Legal/`, `Finances/Receipts/`, `Finances/Statements/`, `Finances/Invoices/`, `Finances/Investments/`, `Photos/`, `Videos/`, `Work/`, `Projects/`, `Media/Music/`, `Media/Books/`, `Unsorted/`. Every rule surfaces the exact signals that fired so the classification is auditable.
- **Cohesion preservation.** Music albums (ID3), book series (topic or filename-prefix plus author), git projects (`.git`/`package.json`/`Cargo.toml`/`pyproject.toml`/`go.mod`/`pom.xml`/`.hg`), and photo or video event clusters move atomically. `--split-cohesive-units` required to break a cohesion group; without it, apply refuses to run before the first move.
- **PDF content classification.** Keyword lexicon per class (payslip, receipt, bank_statement, tax_form, invoice, offer_letter, insurance, investment, id_document, legal) scored against first-page text via `pdfplumber`, gated by threshold `0.6`. Optional `--ocr` for scanned PDFs with too little extractable text.
- **EXIF event clustering for photos and videos.** Time-gap splitting with an optional GPS-outlier split. Offline naming by default (`YYYY-MM-DD` or `YYYY-MM-DD_to_YYYY-MM-DD`). `--enable-geocode` opts in to Nominatim reverse-geocode for locality suffixes.
- **Confidence gating.** `organize_confidence_threshold` (default `0.75`) below which files go to `Unsorted/`. Files whose best rule scored at least `0.4` land in `Unsorted/<Domain>/` for bulk triage.
- **Rename policy user-locked to `preserve`.** `date_prefix` and `date_event_prefix` are opt-in via config. The tool never mutates filename bytes in the default mode.
- **Dedup ordering.** Soft warning by default when the most recent scan still has pending proposed discards. `enforce_dedup_ordering = true` upgrades it to a hard refusal.
- **Cross-volume move safety.** Cross-volume moves go `copy2` + fsync + `send2trash`, never `os.remove`, so hash-mismatch on the destination is recoverable via undo.
- **Path collisions.** Identical-hash collisions skip the move and are logged; different-hash collisions get a `_<hash8>` stem suffix and are logged under `collisions[]` in the manifest.
- **Directory mode `0o755` default.** `organize_dir_mode` config knob for users who want owner-only trees.

Two new invariants tracked in [AUDIT_LOG.md](docs/AUDIT_LOG.md#invariants-do-not-weaken):

- Cohesive units move atomically. Splitting requires `--split-cohesive-units`.
- Rename policy is user-locked. Default `preserve` never mutates filename bytes.

New Python dependencies planned for the milestone:

- Core: `mutagen` (ID3), `pikepdf` (PDF Info dict), `hachoir` (video metadata), `geopy` (reverse-geocode; network usage opt-in).
- `[docs]` optional-extra: `python-docx`, `python-pptx`, `openpyxl` for Office metadata.
- `[gps]` optional-extra: `piexif` for EXIF GPS fallback where Pillow does not parse the block.
- `[ocr]` optional-extra: `pytesseract` (requires system `tesseract`).
- `[mime]` optional-extra: `python-magic` (stdlib `mimetypes` is the primary path).

Sub-milestones (0.3-a → 0.3-b + 0.3-c → 0.3-d + 0.3-e → 0.3-f → 0.3-g): discovery + rules + TUI, then apply + undo, then PDF classifier, then event clustering, then cohesion enforcement, then HTML view, then cross-source. Sub-milestones 0.3-a and 0.3-b shipped 2026-09-06; the classifier and event clustering landed alongside. Interactive HTML review (0.3-f) is the last remaining sub-milestone and is tracked under the v0.3-f roadmap line in the README.

### v0.2 — cloud sources (2026-09-05)

Shipped end-to-end. Google Drive and OneDrive Personal accounts scan alongside local trees. BYO OAuth 2.0 (bundled clients rejected per the 2026-09-07 "Rejected alternatives" decision in [AUDIT_LOG.md](docs/AUDIT_LOG.md#rejected-alternatives-do-not-reopen-without-new-info) — the repo is public). Multi-account labels. Trash-only cloud deletion with cross-source undo. Cross-source scoring (`cloud_when_local_exists = -3`, local always wins). Pre-trash etag drift check.

Sub-milestone landing history (kept for audit trail):

- **Sub-phase 1 — source abstraction refactor.** Introduces `src/duplicate_cleaner/sources/` with the `Source` protocol, shared dataclasses, and exception hierarchy in `sources/base.py`. `LocalFileSystemSource` in `sources/local.py` wraps the existing walker and mover code with zero behaviour change. `FileRecord` gains six optional cloud-related fields (all defaulted so existing constructions work unchanged). Mover and undo dispatch by `source_id`; local-only reports remain byte-identical to v0.1.1. Exit gate: all existing tests pass unchanged; no new tests required. **Shipped.**
- **Sub-phase 2 — `GoogleDriveSource` (read-only).** OAuth 2.0 + PKCE localhost flow, bundled Google Drive client id (placeholder gated by `_TO_REPLACE` sentinel), token storage under `~/.config/duplicate_cleaner/tokens/`. `GoogleDriveSource` streams file metadata via `files.list`, marks shared-with-me items informational-only, and rejects trash calls during scan with a runtime tripwire. **Shipped.**
- **Sub-phase 3 — Google Drive trash + restore.** `GoogleDriveSource.move_to_trash` (`files.update(trashed=True)`) and `restore_from_trash` (`files.update(trashed=False)`), tenacity retry on 429/5xx, `SourceNotFoundError` / `SourcePermissionError` / `SourceRateLimitError` / `SourceAuthError` type hierarchy in `sources/base.py`. Audit-follow-ups B1–B13 landed alongside. **Shipped.**
- **Sub-phase 4 — `OneDriveSource` (read + trash + restore).** New `sources/onedrive.py` targeting OneDrive Personal via Microsoft Graph. Uses raw `httpx` + `msal` (no `msgraph-sdk`). Enumerates via `GET /me/drive/root/delta`, filters `deleted` items, folders, and items missing `file.hashes.sha256Hash`. `remoteItem` presence marks the record `is_shared=True`. Foreign hash format `sha256:<hex-lower>`. Trash via `DELETE /me/drive/items/{id}` (moves to Recycle Bin, keeps id). Restore via `POST /me/drive/items/{id}/restore`; when Graph returns 501 or `notSupported` (documented Personal quirk) `SourceError` fires with an actionable message pointing the user at `https://onedrive.live.com/?id=recyclebin`. Bundled Microsoft client id gated by the same `_TO_REPLACE` sentinel as the Google branch (B1 mirror). `dc auth add onedrive`, `dc auth test onedrive:<label>`, `dc auth remove onedrive:<label>` wired through the shared OAuth infrastructure. **Shipped.**
- **Sub-phase 5 — cross-source scoring, report, apply integration.** `cloud_when_local_exists = -3` fires uniformly on every cloud sibling when any non-informational local peer exists. `is_shared` + `is_singleton_across_sources` set `is_informational=True` in the same layer as archive / hardlink / APFS markers. Mover dispatches on `source_id`; cloud entries route to `Source.move_to_trash`, local entries route to `send2trash`. Undo dispatches by `source_id` to the correct `Source.restore_from_trash`. **Shipped.**
- **v0.2.1 — Cross-algo hash reconciliation.** Local BLAKE3 vs Drive MD5 vs OneDrive SHA-256 now correctly form cross-source duplicate groups end-to-end. Cache lookup → budget check (`max_cloud_download_mb`) → same-algo bucket shortcut → download-and-hash for cross-algo pairs, gated so a single scan cannot silently download all your cloud bytes. **Shipped.**

Design contract: [docs/design/v0.2-cloud-sources.md](docs/design/v0.2-cloud-sources.md). User-facing docs: [docs/cloud-oauth-setup.md](docs/cloud-oauth-setup.md), [docs/cli.md](docs/cli.md).

### Fixed (v0.1.1 ship blockers)

- **DATA-LOSS**: whole-archive delete no longer proposes archives with any encrypted, corrupt, or too-large-to-recurse members. Skips are now indexed by outer archive path and any hit vetoes the proposal (H1).
- **DATA-LOSS**: `dc undo` now verifies `trashed_at_path` (and any basename-fallback candidate) resolves inside a known Trash directory (`~/.Trash` or `/Volumes/*/.Trashes/<uid>/`). A poisoned manifest pointing at `~/.ssh/id_rsa` can no longer coerce undo into relocating arbitrary user-owned files (H2).
- APFS clone detection now uses `getattrlist`'s extended-attr `forkattr` slot for `ATTR_CMNEXT_CLONEID`, per Apple's `<sys/attr.h>`. Previous placement in `commonattr` aliased to `ATTR_CMN_SCRIPT` and silently returned "unknown" for every path — clone-family members were never marked informational (H3).
- Nested-archive expansion now streams into a memory-capped `SpooledTemporaryFile` (default 512 MiB via `max_nested_archive_bytes`). Nested archives that exceed the cap are recorded as `nested_archive_too_large` skips and never recursed into — a hostile 40 GB inner archive no longer decompresses into RAM (H4).
- `dc undo` rejects any manifest whose `original_path` contains the archive-member separator `::`, matching the mover's symmetric rule (H5).
- CPU throttle now uses exactly one `psutil.cpu_percent()` sample per loop iteration; the previous double-sampling (once in `sample_resources`, once in `maybe_throttle`) always saw ~0 on the second read and the throttle never fired (H6).
- Singleton reporting now emits files whose size collides with another but whose partial hash is unique. Previously these files disappeared from every downstream report (H7).
- Bundle hashing refuses to follow symlinks that escape the bundle root even when `--follow-symlinks` is set. A malicious `.app` can no longer stream `~/.ssh/id_rsa` bytes into its bundle hash (H8).
- `taskpolicy` politeness call is now hard-pinned to `/usr/bin/taskpolicy`; `$PATH` is never consulted, so an attacker with write access to an early PATH entry cannot inject a shim (H9).

### Known issues (tracked for v0.1.2)

- Bundle Unicode NFC/NFD normalization is not applied; identical-content bundles whose filenames differ only in normalization form may hash differently.
- Singleton `full_hash` markers of the form `"singleton-by-size:..."` and `"singleton-by-partial:..."` embed the file path and leak into JSON reports.
- `max_workers` config is exposed and validated but not yet wired to a thread pool in the hashing pipeline.

## [0.1.1] — 2026-09-05

### Added

- Archive recursion into `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, and `.tar.xz`. Whole-archive-only deletion proposals; encrypted and corrupt archives are skipped and reported. Nesting depth is capped by `max_archive_depth` (default `2`).
- macOS bundle handling. `.app`, `.pages`, `.numbers`, `.keynote`, `.rtfd`, `.sparsebundle`, `.xcodeproj`, `.playground`, `.framework`, and `.bundle` are treated as atomic units. Extensions are configurable via `bundle_extensions`.
- System monitoring via `psutil`. Live progress bar shows CPU %, RAM MB, free disk GB, and files processed. Configurable throttling on high CPU via `throttle_on_cpu_pct` (default `85`) and worker count via `max_workers` (default `os.cpu_count() // 2`).
- APFS clone detection via `getattrlist` with `ATTR_CMNEXT_CLONEID`. Clone-family members are informational only and never proposed for deletion. Reclaim estimates on cloned trees are now accurate.
- Singleton "Unique files" section in every report — files with no duplicates are enumerated so a scan doubles as a directory census.
- `dc scan --discover` mode. Enumeration-only scans produce a report with zero deletion proposals.

### Changed

- `dc scan` refuses to start if free space on the cache volume is below `min_free_disk_gb` (default `5`).
- Scan process runs at `os.nice(10)` and best-effort `taskpolicy -c background` by default to stay polite to foreground apps.
- Roadmap renumbered: cloud sources are now v0.2. Image, project-tree, PDF/text, audio/video, and adaptive weights shift to v0.3 through v0.7 respectively.

## [0.1.0] — 2026-09-05

### Added

- Exact-duplicate detection via BLAKE3 hashing (size bucket then partial hash then full hash).
- SQLite cache for near-instant rescans, keyed on `(path, size, mtime)`.
- Rule-based "right home" scorer with `active_homes` support and per-signal weight breakdown.
- HTML and JSON report generation. HTML for human review, JSON for machine editing.
- `dc apply` with dry-run default and Trash-only deletion via `send2trash`.
- `dc undo` for full restoration from an undo manifest.
- Hard-link and APFS-clone detection. Shared-inode files are informational only.
- CLI commands: `init`, `scan`, `apply`, `undo`, `weights`, `cache`.
