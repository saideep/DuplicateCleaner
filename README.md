# DuplicateCleaner

Intelligent duplicate detector for macOS: exact, near-duplicate, semantic, and project-tree aware.

## Why it exists

Standard `fdupes`-style tools only catch byte-identical files. That leaves you drowning in duplicates from cross-machine migrations, cross-format re-exports (JPEG vs. HEIC of the same photo, MP3 vs. FLAC of the same track), and — the worst case — thousands of individual file matches inside two copies of the same project tree that should collapse to a single "these two folders are the same backup" line. DuplicateCleaner is built for the case where you have multiple `~/Documents`, `~/Desktop`, and `~/Downloads` folders scattered across old machine backups and external drives, and you need a tool that understands which copy is the *live* one and which is the archive. It runs safely (Trash, not `rm`), reviews every proposed deletion in HTML before you commit, and learns your preferences over time so high-confidence groups can eventually auto-apply.

## Features

Shipping in v0.1:

- Exact-duplicate detection using a size-bucket then partial then full BLAKE3 hash pipeline.
- SQLite-backed cache keyed on `(path, size, mtime)` — rescans are near-instant when nothing changed.
- Rule-based "right home" scorer with a signed weight per signal (path hints, active-home membership, mtime, git-cleanliness, external-vs-internal drive, and more).
- User-declared `active_homes` config — no silent guessing about which `~/Documents` is the real one.
- Static HTML report plus machine-readable JSON. Every score is broken down per signal so nothing is opaque.
- `dc apply` runs dry-run by default; `--commit` moves proposed discards to the macOS Trash via `send2trash`.
- JSON undo manifest per run — `dc undo` restores every moved file.
- Hard-link aware. Files sharing an inode are informational only and never proposed for deletion.
- APFS clone detection is scheduled for v0.1.1. Until then, `dc scan` prints a warning after every scan and reclaim estimates on cloned trees may be too high. See [docs/safety.md](docs/safety.md#hard-link-and-apfs-clone-handling) for the workaround.

Later milestones:

- Perceptual image near-duplicate detection *(coming in v0.2)*.
- Project directory tree aggregation — collapse two copies of the same repo to one tree-diff line *(coming in v0.3)*.
- Semantic PDF and text matching via normalized-text hash plus fuzzy fallback *(coming in v0.4)*.
- Audio and video near-duplicate detection using Chromaprint fingerprints and keyframe pHash *(coming in v0.5)*.
- Adaptive weights that learn from your overrides and gate auto-apply behind a confidence threshold *(coming in v0.6)*.

## Requirements

- macOS 15 Sequoia or later.
- Mac mini with Apple Silicon (M1, M2, or M4 all supported; M4 is the reference platform).
- Python 3.12.x (Apple Silicon native).
- Homebrew.

## Quick install

```shell
brew install python@3.12 uv
git clone <your-repo-url> ~/DuplicateCleaner && cd ~/DuplicateCleaner
uv sync
```

For a full walk-through on a fresh Mac mini, see [docs/macmini-setup.md](docs/macmini-setup.md).

## Quick start

```shell
uv run dc init
# edit ~/.config/duplicate_cleaner/config.toml — set active_homes to your real user directory
uv run dc scan ~/Documents ~/Desktop --report ~/dc-report
open ~/dc-report/report.html
uv run dc apply ~/dc-report/report.json          # dry-run
uv run dc apply ~/dc-report/report.json --commit # move discards to Trash
```

## Safety guarantees

- Never calls `os.remove` or `rm`. All deletions go to the macOS Trash via `send2trash`.
- `dc apply` is dry-run by default. You must pass `--commit` to move anything.
- Every commit writes an undo manifest to `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json` before the first file moves. `dc undo <manifest.json>` restores the full run.
- Hard-coded exclusions: `~/Library`, `/System`, `/private`, `/Volumes/*/System Volume Information`, any path under `.git/objects`, iCloud `.icloud` placeholders.
- Hard-linked files are detected via `(st_dev, st_ino)` matching and appear as informational rows — never proposed for deletion. APFS clone detection lands in v0.1.1.
- Symlinks are not followed by default.

Full detail in [docs/safety.md](docs/safety.md).

## CLI cheat sheet

| Command | Description | Example |
|---|---|---|
| `dc init` | Write the default config to `~/.config/duplicate_cleaner/config.toml`. | `dc init` |
| `dc scan <dir>...` | Scan one or more directories. Writes `report.html` + `report.json`. | `dc scan ~/Documents --report ~/dc-report` |
| `dc apply <report.json>` | Move proposed discards to Trash. Dry-run by default. | `dc apply ~/dc-report/report.json --commit` |
| `dc undo <manifest.json>` | Restore every file moved in a prior run. | `dc undo ~/.local/share/duplicate_cleaner/runs/2026-09-05T10-00/manifest.json` |
| `dc weights show` | Print current scoring weights. | `dc weights show` |
| `dc weights reset` | Restore weights to the shipped defaults. | `dc weights reset` |
| `dc cache stats` | Print cache size and hit rate. | `dc cache stats` |
| `dc cache clear` | Delete the SQLite cache. | `dc cache clear` |

## Config file

Location: `~/.config/duplicate_cleaner/config.toml`. Created by `dc init`.

Minimal example:

```toml
active_homes = ["/Users/vaannada"]
min_size_bytes = 4096

exclude_globs = [
    "**/node_modules/**",
]
```

Full reference: [docs/config.md](docs/config.md).

## License

MIT.

## Roadmap

- [x] **v0.1 — Exact-only.** Walk, size bucket, BLAKE3 pipeline, SQLite cache, rule-based scorer, HTML report, `apply` to Trash, `undo`. Current release.
- [ ] **v0.2 — Image near-duplicate.** Perceptual hash comparator plus thumbnails in the report.
- [ ] **v0.3 — Project-tree aggregation.** Directory rollup for backup-folder collapse.
- [ ] **v0.4 — PDF and text semantic.** Normalized-text hashing plus fuzzy fallback.
- [ ] **v0.5 — Audio and video near-duplicate.** Chromaprint fingerprints and keyframe pHash.
- [ ] **v0.6 — Adaptive weights.** Decisions log wired into weight updates plus `--auto-high-confidence`.
