# DuplicateCleaner

Intelligent duplicate detector for macOS: exact, near-duplicate, semantic, and project-tree aware.

## Why it exists

Standard `fdupes`-style tools only catch byte-identical files. That leaves you drowning in duplicates from cross-machine migrations, cross-format re-exports (JPEG vs. HEIC of the same photo, MP3 vs. FLAC of the same track), and — the worst case — thousands of individual file matches inside two copies of the same project tree that should collapse to a single "these two folders are the same backup" line. DuplicateCleaner is built for the case where you have multiple `~/Documents`, `~/Desktop`, and `~/Downloads` folders scattered across old machine backups and external drives, and you need a tool that understands which copy is the *live* one and which is the archive. It runs safely (Trash, not `rm`), reviews every proposed deletion in HTML before you commit, and learns your preferences over time so high-confidence groups can eventually auto-apply.

## Features

Shipping in v0.1.1 (current release):

- Exact-duplicate detection using a size-bucket then partial then full BLAKE3 hash pipeline.
- SQLite-backed cache keyed on `(path, size, mtime)` — rescans are near-instant when nothing changed.
- Rule-based "right home" scorer with a signed weight per signal (path hints, active-home membership, mtime, git-cleanliness, external-vs-internal drive, and more).
- User-declared `active_homes` config — no silent guessing about which `~/Documents` is the real one.
- Static HTML report plus machine-readable JSON. Every score is broken down per signal so nothing is opaque.
- `dc apply` runs dry-run by default; `--commit` moves proposed discards to the macOS Trash via `send2trash`.
- JSON undo manifest per run — `dc undo` restores every moved file.
- Hard-link aware. Files sharing an inode are informational only and never proposed for deletion.
- APFS clone detection via `getattrlist` with `ATTR_CMNEXT_CLONEID`. Files sharing a clone lineage are informational and never proposed for deletion, so reclaim estimates match the bytes you actually recover.
- Archive recursion. Hashes traverse into `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, and `.tar.xz` archives up to a configurable nesting depth. Only whole archives are proposed for deletion — never a member of an archive in isolation. Encrypted or corrupt archives are skipped and reported.
- macOS bundle handling. `.app`, `.pages`, `.numbers`, `.keynote`, `.rtfd`, `.sparsebundle`, `.xcodeproj`, `.playground`, `.framework`, and `.bundle` are treated as single atomic units. The walker never descends into them.
- System monitoring during scans. A live progress bar shows CPU %, RAM MB, free disk GB, and files processed. The process runs at `os.nice(10)` and best-effort `taskpolicy -c background`, throttles hashing when CPU is above the configured threshold, and refuses to start if free disk on the cache volume is below the configured minimum.
- Singleton "Unique files" section in every report — files with no duplicates are enumerated so a scan doubles as a directory census.
- `--discover` mode. `dc scan --discover` produces an enumeration-only report and proposes zero deletions. Useful for surveying an unfamiliar drive before running a real scan.

In development for v0.2:

- Cloud sources. Google Drive and OneDrive Personal accounts are scanned alongside local trees. Duplicates that span local and cloud propose the cloud copy for deletion (local wins any cross-source tie).
- OAuth 2.0 authentication with bundled clients. `dc auth add gdrive` and `dc auth add onedrive` handle the browser-based login flow end to end with zero setup. `--client-secret path.json` lets you bring your own OAuth client.
- Multi-account support with user-chosen labels. `gdrive:personal`, `gdrive:family`, `gdrive:work`, `onedrive:main` — each account is a separate source ID you can include in a scan.
- Cloud deletions go to the provider's trash (Google Drive trash, OneDrive recycle bin), never hard-delete. OAuth scopes are trash-only — the tool literally cannot hard-delete a cloud file.
- Shared cloud files are informational-only. If the provider reports the file was authored by someone else, the scanner never proposes it for deletion.
- Undo works across sources. A single manifest can carry mixed local + cloud entries; `dc undo` restores each entry via the right API.
- New `--sources` flag on `dc scan` selects which backends to enumerate. Existing local-only invocations behave identically.

Later milestones:

- Perceptual image near-duplicate detection *(coming in v0.3)*.
- Project directory tree aggregation — collapse two copies of the same repo to one tree-diff line *(coming in v0.4)*.
- Semantic PDF and text matching via normalized-text hash plus fuzzy fallback *(coming in v0.5)*.
- Google Photos and iCloud Photos sources *(coming in v0.6)*.
- Audio and video near-duplicate detection using Chromaprint fingerprints and keyframe pHash *(coming in v0.6)*.
- Adaptive weights that learn from your overrides and gate auto-apply behind a confidence threshold *(coming in v0.7)*.

## Requirements

- macOS 15 Sequoia or later.
- Mac mini with Apple Silicon (M1, M2, or M4 all supported; M4 is the reference platform).
- Python 3.12.x (Apple Silicon native).
- Homebrew.
- Internet connectivity when scanning cloud sources (v0.2). A Google account for `dc auth add gdrive` and/or a personal Microsoft account for `dc auth add onedrive`. Local-only scans require no network.

### System behavior

`dc scan` is designed to be a polite background citizen on a machine you are also using.

- The process re-nices itself to `10` via `os.nice(10)` and issues a best-effort `taskpolicy -c background`, so foreground apps stay responsive.
- Hashing throttles when system CPU exceeds `throttle_on_cpu_pct` (default `85`). Set it to `100` in the config to disable throttling.
- The pre-scan check refuses to start if free space on the cache volume is below `min_free_disk_gb` (default `5`). Free space, or lower the threshold in the config.
- Worker count defaults to `os.cpu_count() // 2`. Override via `max_workers` in the config if you want to hand the machine to the scan or hold it back further.

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
- Hard-linked files are detected via `(st_dev, st_ino)` matching and appear as informational rows — never proposed for deletion.
- APFS clones are detected via `getattrlist` with `ATTR_CMNEXT_CLONEID`. Members of a clone family are informational only and never proposed for deletion.
- Archives are proposed as whole units only. Individual members inside a `.zip` or tarball are never proposed for deletion.
- macOS bundles are atomic. `.app`, `.pages`, `.xcodeproj`, and the rest of the bundle list are treated as single files.
- Symlinks are not followed by default.

Full detail in [docs/safety.md](docs/safety.md).

## CLI cheat sheet

| Command | Description | Example |
|---|---|---|
| `dc init` | Write the default config to `~/.config/duplicate_cleaner/config.toml`. | `dc init` |
| `dc scan <dir>...` | Scan one or more directories. Writes `report.html` + `report.json`. Includes a "Unique files" section for singletons. Recurses into archives; treats macOS bundles as atomic. | `dc scan ~/Documents --report ~/dc-report` |
| `dc scan --sources ...` | Scan across local + cloud sources. Local + at least one cloud account. (v0.2) | `dc scan ~/Documents --sources local,gdrive:personal --report ~/dc-report` |
| `dc scan --discover <dir>...` | Enumeration-only scan. No deletions proposed — the report is a directory census. | `dc scan --discover /Volumes/OldDrive --report ~/dc-survey` |
| `dc apply <report.json>` | Move proposed discards to Trash. Dry-run by default. Cloud entries go to their provider trash / recycle bin. | `dc apply ~/dc-report/report.json --commit` |
| `dc undo <manifest.json>` | Restore every file moved in a prior run. Cross-source aware. | `dc undo ~/.local/share/duplicate_cleaner/runs/2026-09-05T10-00/manifest.json` |
| `dc auth add gdrive` | Connect a Google Drive account via OAuth. (v0.2) | `dc auth add gdrive --label family` |
| `dc auth add onedrive` | Connect a OneDrive Personal account via OAuth. (v0.2) | `dc auth add onedrive --label main` |
| `dc auth list` | List every configured account. (v0.2) | `dc auth list` |
| `dc auth test <id>` | Verify an account's token still works. (v0.2) | `dc auth test gdrive:personal` |
| `dc auth remove <id>` | Remove an account and revoke its tokens. (v0.2) | `dc auth remove gdrive:family` |
| `dc sources list` | List sources plus per-source file counts. (v0.2) | `dc sources list` |
| `dc weights show` | Print current scoring weights. | `dc weights show` |
| `dc weights reset` | Restore weights to the shipped defaults. | `dc weights reset` |
| `dc cache stats` | Print cache size and hit rate. | `dc cache stats` |
| `dc cache clear` | Delete the SQLite cache (including the cloud hash cache). | `dc cache clear` |

Full command reference in [docs/cli.md](docs/cli.md). Cloud OAuth setup in [docs/cloud-oauth-setup.md](docs/cloud-oauth-setup.md).

## Config file

Location: `~/.config/duplicate_cleaner/config.toml`. Created by `dc init`.

Minimal example:

```toml
active_homes = ["/Users/vaannada"]
min_size_bytes = 4096

exclude_globs = [
    "**/node_modules/**",
]

# Archive handling
# max_archive_depth = 2

# Bundle handling
# bundle_extensions = [".app", ".pages", ".numbers", ".keynote", ".rtfd", ".sparsebundle", ".xcodeproj", ".playground", ".framework", ".bundle"]

# System monitoring
# max_workers = 4              # default: os.cpu_count() // 2
# throttle_on_cpu_pct = 85     # set to 100 to disable throttling
# min_free_disk_gb = 5
```

Full reference: [docs/config.md](docs/config.md).

## License

MIT.

## Roadmap

- [x] **v0.1 — Exact-only.** Walk, size bucket, BLAKE3 pipeline, SQLite cache, rule-based scorer, HTML report, `apply` to Trash, `undo`. Shipped.
- [x] **v0.1.1 — Archives, bundles, monitoring, clones.** Archive recursion, macOS bundle handling, `psutil`-based system monitoring, APFS clone detection, singleton report, `--discover` mode. Shipped.
- [ ] **v0.2 — Cloud sources.** Google Drive and OneDrive Personal listings compared against local trees; OAuth 2.0 with bundled clients; multi-account support; trash-only cloud deletion with cross-source undo. **In progress.**
- [ ] **v0.3 — Organizer.** Project-tree aggregation and directory rollup for backup-folder collapse.
- [ ] **v0.4 — Image near-duplicate.** Perceptual hash comparator plus thumbnails in the report.
- [ ] **v0.5 — PDF and text semantic.** Normalized-text hashing plus fuzzy fallback.
- [ ] **v0.6 — Google Photos, iCloud Photos, audio + video near-duplicate.** Chromaprint fingerprints and keyframe pHash.
- [ ] **v0.7 — Adaptive weights.** Decisions log wired into weight updates plus `--auto-high-confidence`.
