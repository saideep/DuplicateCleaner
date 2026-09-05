# Changelog

All notable changes to this project will be documented in this file. Format: Keep a Changelog. Versioning: SemVer.

## [Unreleased]

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
