# CLI reference

Every DuplicateCleaner subcommand is invoked as `dc <group> <verb>` (or `uv run dc <group> <verb>` outside an activated venv). This document is the full command surface — the short cheat sheet in [README.md](../README.md) is a subset.

## Global flags

| Flag | Effect |
|---|---|
| `--config PATH` | Override the config file location (default `~/.config/duplicate_cleaner/config.toml`). |
| `--verbose / -v` | Print DEBUG-level logs. Tokens and secrets are always redacted. |
| `--help` | Show help for any command. |

## `dc init`

Write the default config to `~/.config/duplicate_cleaner/config.toml`.

```shell
dc init
dc init --force        # overwrite existing config
```

## `dc scan`

Scan one or more sources and write a report.

```shell
dc scan [ROOTS...] [--sources SOURCE_LIST] [--report DIR] [--discover]
        [--max-cloud-download-mb N] [--skip-shared]
```

`ROOTS` are local directories to include for the `local` source. Cloud sources enumerate the whole account (constrained by their OAuth scope).

Options:

- `--sources` — comma-separated list of source IDs. Default `local`. Example: `--sources local,gdrive:personal,onedrive:main`.
- `--report DIR` — where to write `report.html` and `report.json`. Default `./dc-report`.
- `--discover` — enumeration only. No deletions proposed.
- `--max-cloud-download-mb N` — cap the total cloud bytes downloaded during this scan. Buckets that would exceed the cap are recorded as `not-yet-hashed` in the report. Default: unlimited.
- `--skip-shared` — skip files reported as shared by the cloud provider entirely. Without this flag they appear as informational.

Examples:

```shell
# Local-only scan (behaviour identical to v0.1.1)
dc scan ~/Documents ~/Desktop --report ~/dc-report

# Local + one Google account
dc scan ~/Documents --sources local,gdrive:personal --report ~/dc-report

# Local + two Google accounts + OneDrive
dc scan ~/Documents ~/Pictures \
    --sources local,gdrive:personal,gdrive:family,onedrive:main \
    --report ~/dc-report

# Discovery pass on an unfamiliar drive
dc scan --discover /Volumes/OldDrive --report ~/dc-survey

# Cap cloud downloads at 500 MB while dialing in a config
dc scan --sources local,gdrive:personal --max-cloud-download-mb 500 \
    --report ~/dc-report
```

## `dc apply`

Move proposed discards to their source's trash. Dry-run by default.

```shell
dc apply <report.json>          # dry-run
dc apply <report.json> --commit # actually move
```

For local entries: files go to the macOS Trash via `send2trash`. For cloud entries: entries go to Google Drive's trash or OneDrive's recycle bin via the source API. Every commit writes an undo manifest to `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json` before the first move.

## `dc undo`

Restore every file moved in a prior run. Dispatches per-entry by `source_id` — local entries move back from Trash, cloud entries restore via API.

```shell
dc undo <manifest.json>
```

## `dc auth`

Manage cloud accounts.

### `dc auth add <type> [--label LABEL] [--client-secret PATH]`

Register an account. `<type>` is one of `gdrive`, `onedrive`. `gphotos` and `icloud` are placeholders reserved for v0.6.

- `--label LABEL` — user-chosen label. Becomes the account ID: `gdrive:<LABEL>`. Default `personal`; a second unlabeled add auto-increments to `personal-2`.
- `--client-secret PATH` — use your own OAuth client JSON. Bundled client is used when omitted. See [cloud-oauth-setup.md](cloud-oauth-setup.md).

Examples:

```shell
dc auth add gdrive
dc auth add gdrive --label family
dc auth add gdrive --label work --client-secret ~/my-oauth.json
dc auth add onedrive --label main
```

### `dc auth list`

Print every configured account: ID, type, user email, when added. Never prints tokens.

```shell
dc auth list
```

### `dc auth test <account_id>`

Refresh the account's token and hit a low-cost identity endpoint. Prints OK plus the user info, or an error the user can act on.

```shell
dc auth test gdrive:family
```

### `dc auth remove <account_id>`

Delete the token file, best-effort revoke at the provider, remove the entry from `accounts.toml`.

```shell
dc auth remove gdrive:family
```

## `dc sources`

Inspect what sources DuplicateCleaner knows about.

### `dc sources list`

List all sources plus last-scan file counts.

```shell
dc sources list
```

Output:

```
local                    52,834 files    (last scan 2026-09-05)
gdrive:personal           3,201 files    (last scan 2026-09-05)
gdrive:family             1,847 files    (last scan 2026-09-04)
onedrive:main               612 files    (last scan 2026-09-04)
```

`local` is always shown. Each configured account contributes one entry. File counts come from the last scan's cache; `-` if the source has never been scanned.

## `dc organize` (v0.3)

Three-phase organizer: propose a folder taxonomy for a set of roots, review it, then apply. Full behaviour and worked examples in [organize.md](organize.md).

### `dc organize discover [ROOTS...] [--sources SRC_LIST] [--dest DIR] [--plan PATH] [--confidence-threshold FLOAT] [--event-gap-hours INT] [--enable-geocode] [--ocr] [--skip-dedup-check]`

Walk the sources, extract signals, classify each file, and write the plan JSON and its HTML view. Zero filesystem changes outside the plan artifacts and the SQLite cache.

Options:

- `ROOTS` — local directories to include (for the `local` source). Ignored for cloud sources.
- `--sources` — comma-separated source IDs. Default `local`. Non-`local` values are rejected until v0.3-g wires cross-source organize.
- `--dest DIR` — root of the target tree. Default `~/organized`.
- `--plan PATH` — where to write the plan JSON. Default `~/organize-plan.json`. A `<plan>.html` view is written alongside.
- `--confidence-threshold FLOAT` — override `organize_confidence_threshold` for this run. Default from config (`0.75`).
- `--event-gap-hours INT` — override `event_gap_hours` for this run. Default from config (`12`).
- `--enable-geocode` — reverse-geocode event centroids via Nominatim to add a locality suffix to event folder names. Off by default. Rate-limits at one request per second per user-agent per Nominatim policy.
- `--ocr` — OCR PDFs whose text extraction returns less than 100 characters. Requires the `[ocr]` extra and a system `tesseract` binary.
- `--skip-dedup-check` — suppress the soft warning when pending duplicate proposals exist. Use in scripts.

Examples:

```shell
uv run dc organize discover ~/Downloads --plan ~/plan.json
uv run dc organize discover ~/Downloads ~/OldMac --dest ~/organized \
    --plan ~/plan.json --enable-geocode
uv run dc organize discover ~/Downloads --ocr --confidence-threshold 0.85 \
    --plan ~/plan.json
```

### `dc organize review <plan.json>`

Interactive Rich TUI for editing the plan. Keybindings and layout are documented in [organize.md](organize.md). Requires a TTY; if `stdin` or `stdout` is not a terminal, the command exits with an error pointing you at `$EDITOR`.

```shell
uv run dc organize review ~/plan.json
```

### `dc organize apply <plan.json> [--commit] [--split-cohesive-units] [--runs-dir DIR]`

Move files according to the plan. Dry-run by default.

Options:

- `--commit` — actually create folders and move files. Without this flag the command prints the planned moves and exits.
- `--split-cohesive-units` — allow the plan to route members of a cohesion group to different destinations. Without this flag, any cohesion violation aborts the run before the first move.
- `--runs-dir DIR` — where to write the undo manifest. Default `~/.local/share/duplicate_cleaner/runs/`.

Examples:

```shell
uv run dc organize apply ~/plan.json                        # dry-run
uv run dc organize apply ~/plan.json --commit
uv run dc organize apply ~/plan.json --commit --split-cohesive-units
```

### `dc organize undo <manifest.json>`

Reverse every move recorded in the manifest. Same-volume moves are reversed with `os.rename`; cross-volume moves are reversed by recovering the source from Trash and then removing the destination copy after verifying its hash matches. Per-file drift skips that entry with a logged error; the rest of the manifest still restores.

```shell
uv run dc organize undo ~/.local/share/duplicate_cleaner/runs/<ts>/manifest.json
```

## `dc weights`

Print or reset scoring weights.

```shell
dc weights show
dc weights reset
```

## `dc cache`

Manage the SQLite cache.

```shell
dc cache stats
dc cache clear
```

`dc cache clear` truncates the `cloud_hash_cache` table too. The next cloud scan will re-download and re-hash every cloud file whose foreign hash algorithm does not match a local peer.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | User error — bad flag, missing config, invalid path. |
| 2 | Auth error — token expired, account not configured, refresh failed. |
| 3 | Source error — rate limited, network, permission, not found. |
| 4 | Data-safety abort — path validation failed, drift detected, invariant tripwire. |

Every non-zero exit prints a human-readable line explaining which category applied and what to try next.
