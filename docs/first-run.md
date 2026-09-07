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

Optional — only needed once v0.5+ ships (audio/video near-dup):

```shell
brew install chromaprint ffmpeg
```

## Step 2 — Clone and sync

```shell
git clone https://github.com/saideep/DuplicateCleaner.git ~/DuplicateCleaner
cd ~/DuplicateCleaner
uv sync
```

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

Cloud scan requires you to register your own OAuth clients (BYO). Zero-setup bundled clients are not yet available — they'll ship in a future release once the project owner registers apps under their Google/Microsoft accounts.

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
| `dc scan <dir> --report DIR` | Scan directories for duplicates. |
| `dc scan --sources local,gdrive:X` | Scan across local + cloud sources. |
| `dc apply <report.json>` | Dry-run — print proposed moves. |
| `dc apply <report.json> --commit` | Move discards to Trash. |
| `dc undo <manifest.json>` | Restore a previous apply run. |
| `dc auth add <type>` | Register a cloud account (BYO OAuth today). |
| `dc auth list` | Show configured cloud accounts. |
| `dc sources list` | Show all sources incl. local. |
| `dc organize discover <dir> --report DIR` | Propose an organized destination taxonomy. |
| `dc organize apply <plan.json>` | Dry-run organizer. |
| `dc organize apply <plan.json> --commit` | Move files to organized destinations. |
| `dc organize undo <manifest.json>` | Reverse an organize run. |
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

## What's still coming

Not usable yet in the current release:
- `dc migrate` (cloud-to-cloud consolidation) — v0.5.
- Google Photos + iCloud Photos integrations — v0.6.
- Image near-duplicate detection (resized JPEGs, screenshots of photos) — v0.7.
- Audio/video near-duplicate detection — v0.8.
- Interactive HTML review with click-to-override (currently: edit the JSON directly).

Track progress in [`README.md`](../README.md) roadmap section or `CHANGELOG.md`.

## When things go wrong

- `dc scan` refuses to start because `active_homes` isn't set → edit `~/.config/duplicate_cleaner/config.toml`.
- `dc apply --commit` refuses "insufficient free disk" → free some space or lower `min_free_disk_gb` in config.
- `dc auth add gdrive` fails "bundled Google OAuth client is not registered" → use `--client-secret path.json` with your own OAuth app.
- Something got trashed you wanted to keep → `dc undo <manifest.json>` OR right-click Trash → "Put Back".
- Config file corrupted → `rm ~/.config/duplicate_cleaner/config.toml && dc init`.
- Cache slow / disk full → `dc cache clear`.

Ping me (open a GitHub issue) if anything else surprises you.
