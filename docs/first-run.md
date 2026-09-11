# First run on a fresh Mac mini

You just cloned the repo. This guide takes you from zero to a completed dry-run scan in about 15 minutes, plus another 10 per cloud provider if you want cloud dedup.

Nothing here modifies files under `~/` until you explicitly type `--commit`. Every deletion goes to macOS Trash. `dc undo` restores.

## Step 1 — Install system prerequisites

```shell
# Command Line Tools (once, if not already installed)
xcode-select --install

# Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12 and uv (the venv + dependency manager we use)
brew install python@3.12 uv
```

Optional (only if you want audio and/or video near-duplicate detection):

```shell
brew install chromaprint ffmpeg   # audio: v0.8 uses chromaprint via pyacoustid; video: v0.8 uses ffmpeg for keyframes
```

Skip these if you only care about exact + image near-dup + organize + migrate. Image near-dup is pure Python (Pillow + imagehash) and needs no Homebrew step.

## Step 2 — Clone and sync

```shell
git clone https://github.com/saideep/DuplicateCleaner.git ~/DuplicateCleaner
cd ~/DuplicateCleaner
uv sync                          # core deps
# or: uv sync --all-extras       # + [audio], [video], [icloud], [docs], [gps], [ocr]
```

Optional-extra install groups (add on demand):

| Extra | What it enables |
|---|---|
| `[audio]` | `pyacoustid` — Chromaprint audio fingerprint (needs `brew install chromaprint`) |
| `[video]` | `Pillow` + `imagehash` for keyframe pHash (needs `brew install ffmpeg`) |
| `[icloud]` | `osxphotos` — reads the local Photos.photoslibrary bundle for `dc scan --sources icloud:*` |
| `[docs]` | `python-docx` / `python-pptx` / `openpyxl` for organizer signal extraction on Office documents |
| `[gps]` | `piexif` + `geopy` for photo event clustering with GPS-based sub-events |
| `[ocr]` | `pytesseract` for PDF classification via OCR on scan-based PDFs |

`uv sync` reads `pyproject.toml` + `uv.lock`, creates `.venv/`, installs every runtime and dev dependency, and puts the `dc` command on the venv's PATH. Every future command in this guide uses `uv run dc <...>` (or activate the venv once with `source .venv/bin/activate` and drop the `uv run` prefix).

Verify:

```shell
uv run dc --version
uv run dc --help
```

## Step 3 — Write the default config

```shell
uv run dc init
```

Writes `~/.config/duplicate_cleaner/config.toml` and `~/.config/duplicate_cleaner/weights.json`. Both are safe defaults except one field you MUST edit before scanning:

```toml
# ~/.config/duplicate_cleaner/config.toml
active_homes = ["/Users/vaannada"]     # ← change this to your live user directory
```

`dc scan` refuses to run without a real `active_homes`. This is the safety rail that stops the scorer from proposing to delete files in your live home while keeping duplicates in an old backup.

Open the file in your editor and set `active_homes` to your actual user path.

## Step 4 — First scan (dry-run, local only)

Start with a small directory to get a feel for the output:

```shell
uv run dc scan ~/Downloads --report ~/dc-report
```

This walks `~/Downloads`, computes hashes, builds duplicate groups, and writes `~/dc-report/report.html` + `~/dc-report/report.json`. Zero files are moved.

Open the report:

```shell
open ~/dc-report/report.html
```

You'll see sections for:
- **Exact duplicates** — byte-identical file groups with a proposed keeper (highlighted) and discards (dimmed).
- **Project tree duplicates** — whole-repo backups collapsed into a single tree-diff entry (v0.4).
- **Archives** — whole-archive delete proposals when every member is duplicated elsewhere.
- **Unique files** — singletons that will never be discard candidates.
- **APFS clone informational rows** — files sharing storage; not proposed for deletion.

## Step 5 — Apply the report (still dry-run)

```shell
uv run dc apply ~/dc-report/report.json
```

Prints what WOULD be moved. No files touched yet. Read the output carefully.

## Step 6 — Commit the moves to Trash

Once you're happy with the plan:

```shell
uv run dc apply ~/dc-report/report.json --commit
```

Every discarded file goes to `~/.Trash` (or `/Volumes/<VOL>/.Trashes/<uid>/` for external drives). A manifest lands at `~/.local/share/duplicate_cleaner/runs/<utc-timestamp>/manifest.json`. Print the path so you can undo later.

## Step 7 — Undo if you change your mind

```shell
uv run dc undo ~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json
```

Every trashed file goes back to its original location. Refuses to overwrite anything that reappeared in the meantime.

## Cloud dedup (Google Drive + OneDrive) — optional, BYO OAuth

Cloud scan requires you to register your own OAuth clients (BYO). This is by design: DuplicateCleaner is a public repo and bundling personal OAuth client IDs would share quota + revocation risk across every user. You register once per provider (10 minutes each), then it's transparent.

Full walkthrough with click-by-click screenshots: [`docs/cloud-oauth-setup.md`](cloud-oauth-setup.md).

Short version:

**Google Drive**:
1. Go to https://console.cloud.google.com → create a project.
2. Enable "Google Drive API".
3. OAuth consent screen → External → Testing → add your email as a test user.
4. Credentials → Create OAuth Client ID → Desktop app → download the JSON.
5. Save the file somewhere private:
   ```shell
   chmod 600 ~/gdrive-oauth.json
   uv run dc auth add gdrive --client-secret ~/gdrive-oauth.json --label personal
   ```
6. Browser opens, you sign in, consent, and get "Authorized as you@gmail.com".

**OneDrive**:
1. Go to https://portal.azure.com → Azure Active Directory → App registrations → New registration.
2. Supported account types: "Personal Microsoft accounts only".
3. Redirect URI: Public client → `http://localhost`.
4. API permissions → Microsoft Graph → Delegated → `Files.ReadWrite` + `offline_access` + `User.Read`.
5. Save as `~/onedrive-oauth.json` (client_id + optional client_secret).
   ```shell
   chmod 600 ~/onedrive-oauth.json
   uv run dc auth add onedrive --client-secret ~/onedrive-oauth.json --label main
   ```

Verify:

```shell
uv run dc auth list
uv run dc sources list
```

Now scan across everything:

```shell
uv run dc scan ~/Documents --sources local,gdrive:personal,onedrive:main --report ~/dc-report
open ~/dc-report/report.html
uv run dc apply ~/dc-report/report.json --commit
```

Cloud discards go to the provider's own trash / recycle bin (via API — no hard delete, ever). `dc undo` restores them.

## Organizer (sort into HR / Personal / Finances / Photos / Media)

Once you've deduped, sort what's left into a sane folder structure. Discovery only:

```shell
uv run dc organize discover ~/Downloads --report ~/dc-organize
open ~/dc-organize/organize-plan.html
```

You'll see proposed destinations like `HR/Payslips/2024/`, `Finances/Receipts/2024/Amazon/`, `Photos/2024/2024-06-15/`. Edit `organize-plan.json` in your editor to override any classifications, then:

```shell
uv run dc organize apply ~/dc-organize/organize-plan.json          # dry-run
uv run dc organize apply ~/dc-organize/organize-plan.json --commit # actually move
```

Undo:

```shell
uv run dc organize undo ~/.local/share/duplicate_cleaner/organize-runs/<ts>/manifest.json
```

## Cheat sheet

| Command | Purpose |
|---|---|
| `dc init` | Write default config + weights. |
| `dc scan <dir> --report DIR` | Scan directories for duplicates (exact + image + audio + video + tree). |
| `dc scan --sources local,gdrive:X,onedrive:Y` | Scan across local + cloud sources. |
| `dc scan --no-include-image-near-dup` | Turn off perceptual image near-dup (default on). |
| `dc scan --no-include-audio-near-dup` | Turn off Chromaprint audio near-dup (default on). |
| `dc scan --no-include-video-near-dup` | Turn off keyframe-pHash video near-dup (default on). |
| `dc scan --min-project-similarity 0.85` | Loosen the git-project tree-aggregation threshold. |
| `dc apply <report.json>` | Dry-run — print proposed moves. |
| `dc apply <report.json> --commit` | Move discards to Trash. |
| `dc undo <manifest.json>` | Restore a previous apply run. |
| `dc auth add <gdrive\|onedrive\|gphotos> --client-secret PATH.json` | Register a cloud account (BYO OAuth). |
| `dc auth add icloud [--library-path PATH]` | Register the local iCloud Photos library (no OAuth). |
| `dc auth grant-gphotos-trash <account_id>` | Re-auth GPhotos with the broader `photoslibrary` scope. |
| `dc auth revoke-gphotos-trash <account_id>` | Downgrade a GPhotos account back to read-only. |
| `dc auth list` | Show configured accounts + their scopes. |
| `dc sources list` | Show all sources incl. local. |
| `dc organize discover <dir> --report DIR` | Propose an organized destination taxonomy. |
| `dc organize apply <plan.json> [--commit]` | Dry-run / move files to organized destinations. |
| `dc organize undo <manifest.json>` | Reverse an organize run. |
| `dc migrate plan --from A --to B --report DIR` | Plan a cloud-to-cloud consolidation. |
| `dc migrate copy <plan.json> [--commit] [--max-bandwidth-mbps N] [--resume-from MANIFEST]` | Execute the migration copies. |
| `dc migrate verify <manifest.json> [--full]` | Re-verify destination hashes. |
| `dc migrate cleanup <manifest.json> [--commit]` | Trash source originals (only after verify). |
| `dc migrate undo <manifest.json>` | Reverse a migration. |
| `dc cache stats` | Show hash-cache size and hit rate. |
| `dc cache clear` | Empty the hash cache. |

## Safety recap

- Dry-run is the default on every apply command. `--commit` is the only way to write.
- Every deletion goes to Trash (local) or cloud recycle bin (Drive / OneDrive). Never `rm`.
- Every apply writes a manifest before the first move — `dc undo` reverses.
- Hard-coded exclusions: `~/Library`, `/System`, `/Library`, `/Applications`, `/usr`, `/private/etc`, `/var/{db,log,vm,root,folders,tmp}`, `/Users/Shared`, `.Trashes/`, `.git/objects/`, iCloud placeholders.
- Symlinks: not followed by default.
- Hard-linked and APFS-cloned files: informational-only, never discarded.
- Shared cloud files (someone else's Drive shared to you): informational-only.
- Uncommitted git repos: project-tree aggregation refuses to trash a dirty repo.

Full safety design: [`docs/safety.md`](safety.md).

## What's shipped, what's still coming

Shipped (current release):
- Exact-duplicate detection (v0.1) with size-bucket → partial → full BLAKE3 hashing
- Archives, macOS bundles, APFS clones, singletons, `--discover` (v0.1.1)
- Cloud sources: Google Drive + OneDrive Personal (v0.2, BYO OAuth)
- Cross-algo hash reconciliation across sources (v0.2.1)
- Organizer: three-phase discover → apply → undo with cohesion + event clustering (v0.3-a/b/c)
- Project-tree aggregation for backup-folder collapse (v0.4)
- `dc migrate` cloud-to-cloud consolidation with post-upload hash verify (v0.5-a/b/c)
- Google Photos source, read-only (v0.6) with per-account trash-scope escalation infra (v0.6.1)
- iCloud Photos source via `osxphotos` (v0.6), permanently read-only
- Perceptual image near-duplicate detection (v0.7)
- Chromaprint audio + keyframe-pHash video near-duplicate detection (v0.8)

Still coming:
- Interactive HTML review with click-to-override (currently: edit the JSON directly).
- Google Photos library-wide trash — blocked on Google API capability (Photos Library API v1 does not expose a library-wide trash endpoint; escalation infrastructure is in place if Google adds it).

Track progress in [`README.md`](../README.md) roadmap section or `CHANGELOG.md`.

## When things go wrong

- `dc scan` refuses to start because `active_homes` isn't set → edit `~/.config/duplicate_cleaner/config.toml`.
- `dc apply --commit` refuses "insufficient free disk" → free some space or lower `min_free_disk_gb` in config.
- `dc auth add gdrive` refuses without `--client-secret` → BYO is required (by design; see docs/cloud-oauth-setup.md). Register your own Google OAuth client at console.cloud.google.com and pass the downloaded JSON.
- Something got trashed you wanted to keep → `dc undo <manifest.json>` OR right-click Trash → "Put Back".
- Config file corrupted → `rm ~/.config/duplicate_cleaner/config.toml && dc init`.
- Cache slow / disk full → `dc cache clear`.

Ping me (open a GitHub issue) if anything else surprises you.
