# `dc migrate` — cloud-to-cloud consolidation

Copy files between cloud sources, verify each copy by hash, then optionally trash the source original. Every step is dry-run by default, every step writes an atomic manifest before the first byte moves, and every step can be reversed via `dc migrate undo`.

## Sub-commands

| Command | Description |
|---|---|
| `dc migrate plan --from A --to B --report DIR` | Enumerate source, decide per-file action, write `migration-plan.json` + `.html`. |
| `dc migrate copy PLAN.json [--commit]` | Execute the copy actions in the plan; writes a manifest. Dry-run by default. |
| `dc migrate verify MANIFEST.json [--full]` | Re-check destination-side metadata (default) or bytes (`--full`) for every done entry. |
| `dc migrate cleanup MANIFEST.json [--commit]` | Trash source originals for verified done entries. Dry-run by default. |
| `dc migrate undo MANIFEST.json` | Restore source originals (if cleaned up) and trash destination copies. |

## Workflow

The end-to-end flow is a four-step pipeline. Each step reads and mutates the same manifest file:

```shell
# 1. Plan — READ-ONLY. Writes migration-plan.{html,json} under DIR.
dc migrate plan --from onedrive:main --to gdrive:personal --report ~/dc-migrate
open ~/dc-migrate/migration-plan.html   # review before copying

# 2. Copy — dry-run first.
dc migrate copy ~/dc-migrate/migration-plan.json                    # summary only
dc migrate copy ~/dc-migrate/migration-plan.json --commit           # uploads

# The commit run writes a manifest under
# ~/.local/share/duplicate_cleaner/migrate-runs/<utc-timestamp>/manifest.json

# 3. Verify — metadata check by default; --full also hashes destination bytes.
dc migrate verify ~/.local/share/duplicate_cleaner/migrate-runs/<utc>/manifest.json
dc migrate verify <manifest> --full

# 4. Cleanup — only after verify passes. Trashes source originals.
dc migrate cleanup <manifest>            # dry-run
dc migrate cleanup <manifest> --commit   # actually trashes source originals

# Undo — reverses cleanup + copy. Trashes destination copies, restores source originals.
dc migrate undo <manifest>
```

Every command prints a summary table. Commit runs additionally print the manifest path so the next step is one copy-paste away.

## Filters (planning step)

```shell
dc migrate plan --from gdrive:personal --to onedrive:main \
    --filter '**/*.pdf' \
    --filter '**/*.docx' \
    --exclude '**/Cache/**' \
    --report ~/dc-plan
```

- `--filter GLOB` — include-only glob (repeatable).
- `--exclude GLOB` — exclude glob (repeatable).
- `--dest-size-limit-gb FLOAT` — override the provider-derived per-file cap.
- `--include-shared` — reserved; shared cloud files stay deferred.

## Copy options

- `--commit` — actually upload. Without this the copy step summarises the plan and exits.
- `--max-bandwidth-mbps FLOAT` — cap effective upload bandwidth in Mbps. The throttle inserts a per-chunk sleep so bulk migrations do not saturate the link. Default: no cap.
- `--resume-from PATH` — path to a prior manifest. Every entry with `state="done"` there is emitted as `state="skipped"` in the new manifest so a resumed run does not re-upload. Cleanup / undo still address the original copies via the prior manifest.
- `--out-manifest PATH` — override the manifest write location. Default: `~/.local/share/duplicate_cleaner/migrate-runs/<utc-timestamp>/manifest.json`.

## Plan actions (what each row means)

- **copy** — files not present at the destination; will be uploaded on `dc migrate copy --commit`.
- **skip** — already present at the destination with matching size + hash. Idempotent — re-planning the same run yields the same skip set.
- **defer** — shared cloud files (informational-only invariant) and Google-native docs (no downloadable bytes). Neither will be copied.
- **error** — file exceeds the destination's per-file cap (Google Drive 5 TB, OneDrive Personal 250 GB). Requires user attention; will never be uploaded.

## Manifest states (what each row means after copy)

- **pending** — the copy loop enqueued this entry but never got to it (crash mid-run). Recoverable via re-running `dc migrate copy --resume-from` against the same plan.
- **done** — uploaded + hash verified (same-algo pairs) or optimistically accepted (cross-algo pairs; the destination's canonical BLAKE3 lands via `dc migrate verify --full`).
- **skipped** — non-copy plan action (defer / skip / plan-time error) OR a resume-time skip (prior manifest already marked this entry done).
- **error** — the copy failed. `error_message` on the row names the cause: upload rejection, hash mismatch, drift, permission. A hash mismatch trashes the botched destination before flipping the state so the invariant "cloud discards go to cloud trash" holds even on the failure path.

## Safety recap

Every invariant from `docs/AUDIT_LOG.md` is preserved:

- **Dry-run default.** `dc migrate copy` / `cleanup` refuse to fire without `--commit`.
- **Atomic manifest.** Every state transition is flushed via tempfile + fsync + `os.replace` + parent-dir fsync BEFORE the next entry is touched. A crash mid-run leaves a replayable artifact.
- **Drift check before every upload.** Same semantics as the dedup mover — etag mismatch aborts the whole run.
- **Post-upload hash verify.** Same-algo (both md5, both sha256, both blake3) → strict compare; mismatch trashes the destination and marks the entry `state="error"` BEFORE the source is touched. Cross-algo pairs (md5 gdrive → sha256 onedrive) are optimistically accepted at copy time; `dc migrate verify --full` canonicalises via BLAKE3 for the strict check.
- **Cleanup refuses without verify.** Any `state="done"` entry with `verified=False` OR `verified_ts=None` raises `CleanupError` up-front, before any `move_to_trash` call fires. Points the operator at `dc migrate verify` for the fix.
- **BYO OAuth only.** No bundled client id in the public repo.
- **Cloud paths never `.resolve()`d.** Every source path in the manifest is opaque display data on cloud entries.
- **Cloud discards go to cloud trash.** Provider trash / recycle bin only; the tool never hard-deletes cloud files. Undo restores via the same API.
- **Read-only tripwire.** Every source constructed with `is_read_only_scan=True` refuses upload / trash / restore. `dc migrate plan` uses read-only sources; `copy` / `cleanup` / `undo` require write-enabled sources — the runtime check fires before any HTTP call.

## Example run

```shell
$ dc migrate plan --from onedrive:main --to gdrive:personal --report ~/dc-migrate
                Migration plan: onedrive:main -> gdrive:personal
┏━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Action     ┃     Count ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ copy       │       412 │
│ skip       │        58 │
│ defer      │         3 │
│ error      │         0 │
│ Plan JSON  │ …/plan.json │
│ Plan HTML  │ …/plan.html │
└────────────┴───────────┘

$ dc migrate copy ~/dc-migrate/migration-plan.json --commit --max-bandwidth-mbps 50
                     Migration copy result
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Metric          ┃     Value ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ Planned entries │       473 │
│ Copied          │       412 │
│ Skipped         │        58 │
│ Deferred        │         3 │
│ Errored         │         0 │
│ Committed       │       yes │
│ Manifest        │ …/manifest.json │
└─────────────────┴───────────┘

$ dc migrate verify …/manifest.json
                Migration verify result
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Metric          ┃     Value ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ Done entries    │       412 │
│ Verified        │       412 │
│ Drifted         │         0 │
│ Missing on dest │         0 │
│ Errored         │         0 │
└─────────────────┴───────────┘

$ dc migrate cleanup …/manifest.json --commit
                Migration cleanup result
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Metric          ┃     Value ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ Planned trash   │       412 │
│ Trashed         │       412 │
│ Skipped         │         0 │
│ Committed       │       yes │
└─────────────────┴───────────┘
```

If anything goes wrong, `dc migrate undo <manifest>` reverses the cleanup + copy for every entry the manifest recorded.
