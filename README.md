# DuplicateCleaner

Intelligent duplicate detector for macOS: exact, near-duplicate, semantic, and project-tree aware.

## Why it exists

Standard `fdupes`-style tools only catch byte-identical files. That leaves you drowning in duplicates from cross-machine migrations, cross-format re-exports (JPEG vs. HEIC of the same photo, MP3 vs. FLAC of the same track), and — the worst case — thousands of individual file matches inside two copies of the same project tree that should collapse to a single "these two folders are the same backup" line. DuplicateCleaner is built for the case where you have multiple `~/Documents`, `~/Desktop`, and `~/Downloads` folders scattered across old machine backups, external drives, Google Drive accounts, and OneDrive — and you need a tool that understands which copy is the *live* one, which is the archive, and which lives in a cloud you'd rather keep smaller. It runs safely (Trash, not `rm`), reviews every proposed deletion in HTML before you commit, and remembers what it moved via a per-run manifest so any run is fully reversible.

## Features

Grouped by capability, not by version. Everything below is shipped.

### Local dedup

- Exact-duplicate detection using a size-bucket → partial hash → full hash BLAKE3 pipeline.
- SQLite-backed cache keyed on `(path, size, mtime)` — rescans are near-instant when nothing changed.
- Rule-based "right home" scorer with a signed weight per signal (path hints, active-home membership, mtime, git-cleanliness, external-vs-internal drive, cross-source local-wins, and more). Every score is broken down per signal in the report so nothing is opaque.
- User-declared `active_homes` config — no silent guessing about which `~/Documents` is the real one.
- Static HTML report plus machine-readable JSON. HTML for human review, JSON for machine editing and for feeding back into `dc apply`.
- Archive recursion. Hashes traverse into `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, and `.tar.xz` archives up to a configurable nesting depth. Only whole archives are proposed for deletion — never a member of an archive in isolation. Encrypted, corrupt, or too-large-to-recurse archives are recorded as skips and veto the outer archive's proposal.
- macOS bundle handling. `.app`, `.pages`, `.numbers`, `.keynote`, `.rtfd`, `.sparsebundle`, `.xcodeproj`, `.playground`, `.framework`, and `.bundle` are treated as single atomic units. The walker never descends into them.
- APFS clone detection via `getattrlist` with `ATTR_CMNEXT_CLONEID`. Files sharing a clone lineage are informational and never proposed for deletion, so reclaim estimates match the bytes you actually recover.
- Hardlink detection via `(st_dev, st_ino)`. Peers sharing an inode are informational only.
- Project-tree aggregation. Two copies of the same git repo, npm package, Cargo crate, Go module, Maven project, or Gradle build collapse to a single tree-diff entry in the report instead of surfacing as thousands of per-file matches. Discards trash the whole directory atomically; undo restores it wholesale. Similarity threshold configurable via `--min-project-similarity` (Jaccard, default `0.90`).
- Singleton "Unique files" section in every report — files with no duplicates are enumerated so a scan doubles as a directory census.
- `dc scan --discover` mode. Enumeration-only scan; the report proposes zero deletions. Useful for surveying an unfamiliar drive before running a real scan.

### Near-duplicate detection

- Perceptual image near-dup (v0.7). `imagehash.phash` at 256-bit resolution groups resized JPEGs, re-encodes, and screenshots-of-photos across every configured source. Hamming-distance threshold configurable via `--image-near-dup-distance` (default `8` out of 256). Skips exact-dup members already grouped by content hash. Uses union-find so three-way near-dups produce one group, not N*(N-1)/2 pairs.
- Chromaprint audio near-dup (v0.8). `pyacoustid.fingerprint_file` computes a local-only Chromaprint (no AcoustID.org lookup — privacy preserved). Fingerprints are decoded to uint32 arrays and compared bit-level; duration-first fast filter rejects pairs > 2s apart. Threshold `--audio-similarity-threshold` (default `0.95`). Requires `brew install chromaprint`.
- Video near-dup (v0.8). `ffmpeg` extracts 5 keyframes evenly across each video's duration, downscales each to 32×32 grayscale, computes pHashes (256-bit each), and averages Hamming distance across keyframes. Duration-first filter rejects pairs > 5s apart. Threshold `--video-similarity-threshold` (default `0.90`). Requires `brew install ffmpeg`. `ffmpeg` invoked via hardcoded absolute path (`/opt/homebrew/bin/ffmpeg` primary, `/usr/local/bin/ffmpeg` fallback) — never `shutil.which`, per the H9 PATH-hijack safety rail.
- All three near-dup passes are local-only. Cloud records are filtered out at intake, so cloud paths never round-trip through `Path.resolve()`.
- Cache per source in SQLite (`image_phash_cache`, `audio_fingerprint_cache`, `video_signature_cache`) with 90-day TTL sweep at scan start — rescans are near-instant.
- All three families flow through the same drift-check + `send2trash` mover rail as exact discards, and the same undo manifest restores them.

### Cloud sources

- Google Drive and OneDrive Personal accounts scanned alongside local trees. Sub-command layer is source-agnostic — a single `dc scan` invocation can mix local roots and any number of cloud accounts.
- OAuth 2.0 with **bring-your-own client credentials**. The repo is public, so bundling personal OAuth client IDs is not offered — see the note in "Safety guarantees" below and [docs/cloud-oauth-setup.md](docs/cloud-oauth-setup.md) for the 10-minute Google Cloud Console / Azure App Registration walk-through.
- Multi-account support with user-chosen labels. `gdrive:personal`, `gdrive:family`, `gdrive:work`, `onedrive:main` — each account is a separate source ID you can include in a scan.
- Cross-algo hash reconciliation. Local BLAKE3, Google Drive MD5, and OneDrive SHA-256 form cross-source duplicate groups end-to-end. Local hashes get reused via the SQLite cache; cross-algo reconciliation reads cloud bytes only when a size collision demands it, gated by `max_cloud_download_mb`.
- Cross-source scoring. When a file is present both locally and in the cloud, the local copy always wins the "keeper" role. Explicit signed weight `cloud_when_local_exists = -3` documented in `dc weights show`.
- Cloud deletions go to the provider's trash (Google Drive trash, OneDrive recycle bin), never hard-delete. OAuth scopes are trash-only (`drive.file`, `Files.ReadWrite`) — the tool literally cannot hard-delete a cloud file.
- Shared cloud files are informational-only. If Drive or OneDrive reports the file was authored by someone else (or came in via `remoteItem`), the scanner never proposes it for deletion.
- Undo works across sources. A single manifest can carry mixed local + cloud entries; `dc undo` restores each entry via the right API. OneDrive Personal restore falls back to a clear "restore manually via the web recycle bin" message when Graph returns `notSupported`.
- Pre-trash etag drift check. Immediately before `move_to_trash` fires on any cloud member, the source re-reads the file's current etag. Etag mismatch aborts the whole run (symmetric with local size+mtime drift).

### Organize

- Three-phase organizer. `dc organize discover` proposes a folder taxonomy for what remains after dedup; `dc organize review` opens a Rich-based TUI for editing the plan (or edit the plan JSON directly in `$EDITOR` — the apply step re-validates the schema); `dc organize apply` creates target folders and moves files. Every phase carries dry-run defaults and an atomic manifest.
- Nine-domain taxonomy tuned to real user data. `HR/{Payslips,OfferLetters,Tax}`, `Personal/{IDs,Insurance,Legal}`, `Finances/{Receipts,Statements,Invoices,Investments}`, `Photos/`, `Videos/`, `Work/`, `Projects/`, `Media/{Music,Books}`, `Unsorted/`. Every rule surfaces the exact signals that fired so the classification is auditable.
- Signal extractors. Filename regex (dates, seqnos, vendor lexicons), ID3 tags for music (mutagen), EXIF for photos (Pillow), video creation-date + duration (hachoir), PDF Info dict + first-page text keyword classification (pikepdf + pdfplumber), optional OCR for scanned PDFs (`--ocr`).
- Cohesion preservation. Music albums (ID3), book series (filename-prefix or topic + author), git projects (`.git`/`package.json`/`Cargo.toml`/`pyproject.toml`/`go.mod`/`pom.xml`/`.hg`), and photo or video event clusters move atomically. Splitting a cohesion group requires an explicit `--split-cohesive-units` flag; without it, apply refuses before the first move.
- Event clustering. EXIF-first capture timestamps with mtime fallback; time-gap splitting with configurable `event_gap_hours` and `min_event_photos`. Offline naming by default (`YYYY-MM-DD` or `YYYY-MM-DD_to_YYYY-MM-DD`). Optional `--enable-geocode` for Nominatim reverse-geocode suffixes.
- Confidence gating. `organize_confidence_threshold` (default `0.75`) below which files go to `Unsorted/`. Near-misses (best rule scored between `0.4` and the threshold) land in `Unsorted/<Domain>/` for bulk triage.
- Rename policy user-locked to `preserve` by default. `date_prefix` and `date_event_prefix` are opt-in via config. The tool never mutates filename bytes in the default mode.
- Directory mode `0o755` default. `organize_dir_mode` config knob for users who want owner-only trees.

### Migrate

- Cloud-to-cloud file consolidation. `dc migrate` copies files between clouds — e.g. onedrive:main → gdrive:personal — verifies the destination by hash, then optionally trashes the source originals.
- Four sub-commands plus undo. `dc migrate plan` produces a read-only plan (JSON + HTML); `dc migrate copy` executes uploads; `dc migrate verify` re-checks destination-side metadata or (with `--full`) re-hashes destination bytes via BLAKE3; `dc migrate cleanup` trashes source originals; `dc migrate undo` reverses the whole thing.
- Post-upload hash verify. Same-algo pairs (both md5, both sha256, both blake3) compare directly at copy time; a mismatch trashes the botched destination copy BEFORE the manifest advances, and the source is never touched. Cross-algo pairs (md5 gdrive ↔ sha256 onedrive) are optimistically accepted at copy time; `dc migrate verify --full` canonicalises via BLAKE3 for the strict byte-level check.
- Cleanup refuses without verify. `dc migrate cleanup` iterates every `state="done"` entry up-front and raises before any `move_to_trash` call if any entry has `verified=False` or `verified_ts=None`. Structural — a mid-batch failure on entry N cannot leave entries 1..(N-1) trashed against an unverified copy.
- Resume-from-manifest. `--resume-from PRIOR_MANIFEST` re-emits already-done entries as skipped in the current run's manifest and preserves their destination cloud IDs, so cleanup and undo still address the original copies after an interrupted transfer.
- Bandwidth throttle. `--max-bandwidth-mbps N` inserts proportional per-chunk sleeps so a long migration doesn't saturate your home connection.
- Destination per-file caps enforced at plan time. Google Drive 5 TB, OneDrive Personal 250 GB. Files over the cap surface as `action="error"` with `size_limit_hit=True` instead of failing silently mid-upload.

### Safety envelope

- Dry-run default on every apply-shaped command (`dc apply`, `dc organize apply`, `dc migrate copy`, `dc migrate cleanup`). `--commit` is the sole write gate.
- Atomic manifest before the first move. Tempfile + `flush()` + `fsync(fd)` + `os.replace()` + parent-dir fsync — the manifest is on disk before any file leaves its original location, then re-flushed after every successful move.
- Full undo via manifest. `dc undo`, `dc organize undo`, and `dc migrate undo` each restore every entry from their run's manifest, using the correct API per entry (`shutil.move` from the local Trash for local entries; `Source.restore_from_trash` for cloud entries).
- Every path from external JSON re-validated. `report.roots`, per-member `path`, `manifest.original_path`, `manifest.trashed_at_path` — each `.resolve()`ed and checked against a hard-coded exclusion set plus the manifest-declared roots.
- Hard-coded exclusions: `~/Library` (all users, regex), `/System`, `/private/{etc,var/{db,log,vm,root,audit,folders,tmp}}`, `/Volumes/*/System Volume Information`, `.git/objects`, iCloud `.icloud` placeholders. Same list applies to organize dest folders.
- Symlinks not followed by default. When followed, descendants are `.resolve()`d before the exclusion check.
- Hardlinks and APFS clones are informational-only. Files sharing an inode or a clone-family ID are never proposed for deletion, and reclaim estimates only count bytes you actually recover.
- Project-tree discards refuse dirty git repos. `git status --porcelain` non-empty → refusal at validate time and re-checked immediately before the move. Uncommitted work has no other on-disk copy; trashing the directory would destroy the working-tree diff.
- Project trees move atomically. `dc apply --commit` sends the entire discard directory to Trash via a single `send2trash` call; undo restores the whole tree. Exact-duplicate groups whose members are wholly inside a detected project root are removed from the report before `apply` sees them.
- Shared cloud files stay informational-only. The planner never proposes them for deletion or as a migrate source.
- Post-upload hash verify on migrate; cleanup refuses without verify (both invariants above).
- BYO OAuth by design. The `_TO_REPLACE` sentinel guard fires forever if you attempt to use a placeholder client id.

### System behavior

`dc scan` is designed to be a polite background citizen on a machine you are also using.

- The process re-nices itself to `10` via `os.nice(10)` and issues a best-effort `taskpolicy -c background`, so foreground apps stay responsive. `taskpolicy` is hard-pinned to `/usr/bin/taskpolicy`; `$PATH` is never consulted.
- Hashing throttles when system CPU exceeds `throttle_on_cpu_pct` (default `85`). Set it to `100` in the config to disable throttling.
- The pre-scan check refuses to start if free space on the cache volume is below `min_free_disk_gb` (default `5`).
- Worker count defaults to `os.cpu_count() // 2`. Override via `max_workers` in the config.
- Live Rich progress bar shows CPU %, RAM MB, free disk GB, and files processed.

## Requirements

- macOS 15 Sequoia or later.
- Mac mini with Apple Silicon (M1, M2, or M4 all supported; M4 is the reference platform).
- Python 3.12.x (Apple Silicon native).
- Homebrew.
- Internet connectivity only when scanning cloud sources or running `dc migrate`. A Google account for `dc auth add gdrive` and/or a personal Microsoft account for `dc auth add onedrive`, plus your own OAuth client credentials — see [docs/cloud-oauth-setup.md](docs/cloud-oauth-setup.md). Local-only scans require no network.

## Quick install

```shell
brew install python@3.12 uv
git clone https://github.com/saideep/DuplicateCleaner ~/DuplicateCleaner && cd ~/DuplicateCleaner
uv sync
```

**First-time user on a fresh Mac mini? Start here: [docs/first-run.md](docs/first-run.md).** Step-by-step from clone to first successful `--commit`.

## Deeper docs

- [docs/first-run.md](docs/first-run.md) — end-to-end setup walk-through, first scan through first commit
- [docs/macmini-setup.md](docs/macmini-setup.md) — installation reference
- [docs/cloud-oauth-setup.md](docs/cloud-oauth-setup.md) — BYO OAuth registration for Google Drive + OneDrive
- [docs/organize.md](docs/organize.md) — organizer workflow, taxonomy, cohesion, event clustering
- [docs/migrate.md](docs/migrate.md) — cloud-to-cloud migration workflow
- [docs/safety.md](docs/safety.md) — safety model + invariants (canonical)
- [docs/cli.md](docs/cli.md) — full CLI reference
- [docs/config.md](docs/config.md) — every config key

## What the report contains

Every scan writes two artifacts to your `--report` directory:

- `report.html` — a static, self-contained HTML view. Sections in order: summary tiles (files scanned, duplicate groups, reclaimable bytes), Project trees (whole-directory duplicates, purple badges, similarity %), Exact duplicates (grouped by hash, keeper vs. discards, per-signal score breakdown), Informational (hardlinks, APFS clones, shared cloud files, archive-member matches — all non-actionable), and Unique files (the singleton census).
- `report.json` — the machine-readable version. Same content, safe to hand-edit or feed back into `dc apply` after tweaking `proposed_keeper` on any group.

Every proposed discard shows why. Signals like `active_home_membership`, `path_hint_backup`, `mtime_older`, `external_drive`, `cloud_when_local_exists`, `git_head_older`, and `is_project_tree_backup_copy` each carry a signed weight that sums into the file's score. `dc weights show` prints the full weight table.

## Quick start

```shell
uv run dc init
# edit ~/.config/duplicate_cleaner/config.toml — set active_homes to your real user directory
uv run dc scan ~/Documents ~/Desktop --report ~/dc-report
open ~/dc-report/report.html
uv run dc apply ~/dc-report/report.json          # dry-run
uv run dc apply ~/dc-report/report.json --commit # move discards to Trash
```

`dc undo ~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json` reverses the run.

### Cloud quick start

Register your own OAuth clients per [docs/cloud-oauth-setup.md](docs/cloud-oauth-setup.md), then:

```shell
uv run dc auth add gdrive --client-secret ~/my-gdrive-oauth.json --label personal
uv run dc auth add onedrive --client-secret ~/my-onedrive-oauth.json --label main
uv run dc auth list
uv run dc scan ~/Documents \
    --sources local,gdrive:personal,onedrive:main \
    --report ~/dc-report
open ~/dc-report/report.html
uv run dc apply ~/dc-report/report.json --commit
```

Cloud discards go to Drive trash / OneDrive recycle bin. `dc undo` restores across sources.

### Organize quick start

```shell
uv run dc organize discover ~/Downloads ~/OldMac --dest ~/organized --plan ~/plan.json
uv run dc organize review ~/plan.json            # optional Rich TUI
uv run dc organize apply ~/plan.json             # dry-run
uv run dc organize apply ~/plan.json --commit
```

`dc organize undo <manifest.json>` reverses the run.

### Migrate quick start

```shell
uv run dc migrate plan --from onedrive:main --to gdrive:personal --report ~/dc-migrate
open ~/dc-migrate/migration-plan.html
uv run dc migrate copy ~/dc-migrate/migration-plan.json                # dry-run
uv run dc migrate copy ~/dc-migrate/migration-plan.json --commit \
    --max-bandwidth-mbps 20
uv run dc migrate verify ~/.local/share/duplicate_cleaner/migrate-runs/<ts>/manifest.json --full
uv run dc migrate cleanup ~/.local/share/duplicate_cleaner/migrate-runs/<ts>/manifest.json --commit
```

`dc migrate undo <manifest.json>` restores source originals and trashes the destination copies.

## Safety guarantees

- **Trash-only.** All local deletions go to the macOS Trash via `send2trash`. All cloud deletions go to the provider trash / recycle bin. The tool never calls `os.remove`, `rm`, or a cloud hard-delete API. A regression test (`test_no_forbidden_calls.py`) greps `src/` and fails the build if any forbidden call reappears.
- **Dry-run default on every apply command.** `dc apply`, `dc organize apply`, `dc migrate copy`, and `dc migrate cleanup` all print a summary and touch nothing unless you pass `--commit`.
- **Atomic manifest before the first move.** Every commit writes an undo manifest to `~/.local/share/duplicate_cleaner/{runs,organize-runs,migrate-runs}/<timestamp>/manifest.json` via tempfile + fsync + `os.replace` + parent-dir fsync BEFORE the first file moves.
- **Every path from external JSON re-validated.** A hand-edited plan or manifest pointing at `~/.ssh/id_rsa`, `~/Library`, `/System`, or a path outside the manifest's declared roots is refused per-entry before any move.
- **Hardlinks + APFS clones informational-only.** Members of a shared-inode or clone family are never proposed for deletion, and reclaim estimates only count bytes you actually recover.
- **Shared cloud files informational-only.** Files whose Drive/OneDrive metadata reports non-you authorship (or arrived via `remoteItem`) are never proposed for deletion or as a migrate source.
- **Dirty git repos refuse project-tree discard.** `git status --porcelain` non-empty aborts the discard at validate time and re-checks immediately before the move.
- **Post-upload hash verify on migrate.** Same-algo mismatch trashes the destination copy BEFORE the manifest advances and BEFORE the source is touched. Cross-algo verification lives in `dc migrate verify --full`.
- **Cleanup refuses without verify.** `dc migrate cleanup` raises before any `move_to_trash` call if any done entry is missing verification.
- **BYO OAuth by design.** The repo is public. Bundling personal OAuth client IDs would share the maintainer's quota, create shared-revocation risk, and obscure the exact permission grants — so BYO is the permanent design, not a "coming soon" state. The `_TO_REPLACE` sentinel guard fires on any placeholder attempt.

Full detail in [docs/safety.md](docs/safety.md).

## CLI cheat sheet

| Command | Description | Example |
|---|---|---|
| `dc init` | Write the default config to `~/.config/duplicate_cleaner/config.toml`. | `dc init` |
| `dc scan <dir>...` | Scan one or more local directories. Writes `report.html` + `report.json`, includes a "Unique files" section, recurses into archives, treats macOS bundles as atomic, aggregates project trees. | `dc scan ~/Documents --report ~/dc-report` |
| `dc scan --sources ...` | Scan across local + cloud sources in a single pass. | `dc scan ~/Documents --sources local,gdrive:personal,onedrive:main --report ~/dc-report` |
| `dc scan --discover <dir>...` | Enumeration-only scan. No deletions proposed — the report is a directory census. | `dc scan --discover /Volumes/OldDrive --report ~/dc-survey` |
| `dc apply <report.json>` | Move proposed discards to Trash. Dry-run by default. Cloud entries go to their provider trash / recycle bin. | `dc apply ~/dc-report/report.json --commit` |
| `dc undo <manifest.json>` | Restore every file moved in a prior run. Cross-source aware. | `dc undo ~/.local/share/duplicate_cleaner/runs/<ts>/manifest.json` |
| `dc auth add gdrive` | Connect a Google Drive account via OAuth (requires `--client-secret PATH.json`). | `dc auth add gdrive --client-secret ~/my-gdrive.json --label family` |
| `dc auth add onedrive` | Connect a OneDrive Personal account via OAuth (requires `--client-secret PATH.json`). | `dc auth add onedrive --client-secret ~/my-onedrive.json --label main` |
| `dc auth list` | List every configured account. | `dc auth list` |
| `dc auth test <id>` | Verify an account's token still works. | `dc auth test gdrive:personal` |
| `dc auth remove <id>` | Remove an account and revoke its tokens. | `dc auth remove gdrive:family` |
| `dc sources list` | List sources plus per-source file counts. | `dc sources list` |
| `dc organize discover <dir>...` | Propose a folder taxonomy for the roots. Writes a plan JSON and HTML view. Zero filesystem changes. | `dc organize discover ~/Downloads --plan ~/plan.json` |
| `dc organize review <plan.json>` | Interactive Rich TUI for editing the plan file. | `dc organize review ~/plan.json` |
| `dc organize apply <plan.json>` | Create target folders and move files. Dry-run by default. | `dc organize apply ~/plan.json --commit` |
| `dc organize undo <manifest.json>` | Restore every file moved in a prior organize run. | `dc organize undo ~/.local/share/duplicate_cleaner/organize-runs/<ts>/manifest.json` |
| `dc migrate plan --from A --to B --report DIR` | Cloud-to-cloud plan. Enumerates source, decides per-file copy / skip / defer / error, writes a plan. | `dc migrate plan --from onedrive:main --to gdrive:personal --report ~/dc-migrate` |
| `dc migrate copy <plan.json>` | Execute the copy actions. Dry-run by default; `--commit` uploads. Supports `--max-bandwidth-mbps N` and `--resume-from MANIFEST`. | `dc migrate copy ~/dc-migrate/migration-plan.json --commit` |
| `dc migrate verify <manifest.json>` | Re-check destination-side metadata for every done entry. `--full` re-hashes destination bytes via BLAKE3. | `dc migrate verify <manifest.json> --full` |
| `dc migrate cleanup <manifest.json>` | Trash source originals for verified done entries. Refuses if verify hasn't run. | `dc migrate cleanup <manifest.json> --commit` |
| `dc migrate undo <manifest.json>` | Reverse a migration: restore source originals, trash destination copies. | `dc migrate undo <manifest.json>` |
| `dc weights show` | Print current scoring weights. | `dc weights show` |
| `dc weights reset` | Restore weights to the shipped defaults. | `dc weights reset` |
| `dc cache stats` | Print cache size and hit rate. | `dc cache stats` |
| `dc cache clear` | Delete the SQLite cache (including the cloud hash cache). | `dc cache clear` |

Full command reference in [docs/cli.md](docs/cli.md).

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

# Cloud
# max_cloud_download_mb = 200  # cap cross-algo reconciliation reads per scan

# Organizer
# [organize]
# organize_confidence_threshold = 0.75
# organize_dir_mode = 0o755
# rename_policy = "preserve"       # or "date_prefix" | "date_event_prefix"
# event_gap_hours = 12
# min_event_photos = 5
# enforce_dedup_ordering = false
```

Full reference: [docs/config.md](docs/config.md).

## Where things live

DuplicateCleaner follows XDG-style path conventions on macOS.

| Path | Contents |
|---|---|
| `~/.config/duplicate_cleaner/config.toml` | User config. Written by `dc init`; edit at will. |
| `~/.config/duplicate_cleaner/accounts.json` | Cloud account registry (labels, source IDs, provider). |
| `~/.config/duplicate_cleaner/tokens/<source_id>.json` | Per-account OAuth tokens, mode `0600`. |
| `~/.local/share/duplicate_cleaner/cache.sqlite` | File hash cache + cloud hash cache + signal cache. |
| `~/.local/share/duplicate_cleaner/runs/<utc-timestamp>/manifest.json` | `dc apply` manifests (dedup runs). Used by `dc undo`. |
| `~/.local/share/duplicate_cleaner/organize-runs/<utc-timestamp>/manifest.json` | `dc organize apply` manifests. Used by `dc organize undo`. |
| `~/.local/share/duplicate_cleaner/migrate-runs/<utc-timestamp>/manifest.json` | `dc migrate copy` manifests. Used by `dc migrate verify` / `cleanup` / `undo`. |

The three manifest trees are deliberately distinct so you never confuse a dedup run's undo with an organize run's undo.

## Roadmap

Shipped:

- [x] **v0.1 — Exact-only.** Walk, size bucket, BLAKE3 pipeline, SQLite cache, rule-based scorer, HTML report, `apply` to Trash, `undo`.
- [x] **v0.1.1 — Archives, bundles, monitoring, clones.** Archive recursion, macOS bundle handling, `psutil`-based system monitoring, APFS clone detection, singleton report, `--discover` mode.
- [x] **v0.2 — Cloud sources.** Google Drive and OneDrive Personal via BYO OAuth, multi-account, trash-only cloud deletion, cross-source undo, cross-source scoring (local wins), etag drift check.
- [x] **v0.2.1 — Cross-algo hash reconciliation.** Local BLAKE3 vs Drive MD5 vs OneDrive SHA-256 now correctly form cross-source duplicate groups end-to-end.
- [x] **v0.3 — Organizer.** `dc organize discover → review → apply → undo`. Nine-domain taxonomy, PDF content classification, EXIF event clustering, cohesion preservation for albums / book series / git projects / photo events, rename policy user-locked to `preserve` by default.
- [x] **v0.4 — Project-tree aggregation.** Whole-directory dedup for two copies of the same git repo / npm project / Cargo crate. Jaccard similarity ≥ 0.90 default; dirty git repos refuse discard; connected-component grouping.
- [x] **v0.5 — Cloud consolidation (`dc migrate`).** `plan → copy → verify → cleanup → undo`. Post-upload hash verify, cleanup refuses without verify, bandwidth throttle, resume-from-manifest.
- [x] **v0.6 — Google Photos and iCloud Photos.** Read-only sources scan alongside Drive / OneDrive / local. Google Photos via BYO OAuth (`photoslibrary.readonly`); iCloud via the local `~/Pictures/Photos Library.photoslibrary` bundle through `osxphotos`. No provider-side hash on either → reconciliation downloads bytes and BLAKE3s them, cached per `(source_id, cloud_file_id, etag)`. Google Photos trash is deferred to v0.6.1 (scope escalation required); iCloud Photos deletion goes via the Photos.app.
- [x] **v0.6.1 — Google Photos trash scope escalation infrastructure.** `dc auth grant-gphotos-trash <account>` re-authorises with the broader `photoslibrary` scope. Token gains `has_trash=True`; scorer stops marking that account's records as informational; three-layer read-only defense still fires on non-escalated accounts. Actual library-wide trash currently constrained by Google Photos Library API v1 which doesn't expose a trash endpoint — the escalation surfaces an actionable error pointing at `photos.google.com/trash`; the code path is ready when Google adds the endpoint.
- [x] **v0.7 — Image near-duplicate.** Perceptual hash comparator via `imagehash.phash` at 256-bit resolution. Hamming-distance clustering with union-find (no combinatorial explosion for N-way near-dups). Cross-source aware but filters to `source_id == "local"` at intake so cloud paths never round-trip through `.resolve()`. SQLite cache with 90-day TTL sweep.
- [x] **v0.8 — Audio and video near-duplicate.** Chromaprint fingerprints via `pyacoustid` (local-only, no AcoustID.org lookup — privacy preserved); ffmpeg keyframe pHash for video. `ffmpeg` and `fpcalc` invoked via hardcoded absolute paths (H9 PATH-hijack safety rail). Bit-level fingerprint Hamming for audio; averaged keyframe Hamming for video. Same union-find clustering, same cache pattern with TTL sweep.

Upcoming:

- [ ] **v0.3-f — Interactive HTML review UI.** Click-to-override in the browser as an alternative to the Rich TUI. Today: edit the plan JSON directly (the apply step re-validates the schema).
- [ ] **Google Photos library-wide trash** — blocked on Google API capability (Photos Library API v1 does not expose a library-wide trash endpoint). Escalation infrastructure is in place if Google adds it.

## License

MIT.
