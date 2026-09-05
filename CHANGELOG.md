# Changelog

All notable changes to this project will be documented in this file. Format: Keep a Changelog. Versioning: SemVer.

## [Unreleased]

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
