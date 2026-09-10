# `dc migrate` — cloud-to-cloud consolidation

*Planner only in v0.5-a. Copy / verify / cleanup / undo land in v0.5-b.*

## What it does

`dc migrate` copies files from one registered cloud source (`--from`) to another (`--to`), verifies each transferred file by hash on the destination, and can then optionally trash the source original — the same dry-run + undo pattern as `dc apply`.

v0.5-a ships **planning only**. `dc migrate plan --from <A> --to <B> --report DIR` enumerates the source, consults the destination for already-present files, and writes `migration-plan.html` + `migration-plan.json` under `DIR`. Nothing is copied and nothing is trashed. The plan is designed to be reviewed (and hand-edited if needed) before the `copy` step in v0.5-b.

## Sub-commands (planned surface)

| Command | Ships in | Description |
|---|---|---|
| `dc migrate plan --from A --to B --report DIR` | **v0.5-a** | Enumerate source, decide per-file action, write plan. |
| `dc migrate copy PLAN.json [--commit]` | v0.5-b | Execute the copy actions in the plan; dry-run by default. |
| `dc migrate verify PLAN.json` | v0.5-b | Re-hash destination copies and compare to the source. |
| `dc migrate cleanup PLAN.json --manifest M.json` | v0.5-b | Trash the source originals — only after successful verify. |
| `dc migrate undo MANIFEST.json` | v0.5-b | Undo cleanup by restoring source originals from cloud trash. |

## Example planning session

```shell
dc migrate plan --from onedrive:main --to gdrive:personal --report ~/dc-migrate-report
open ~/dc-migrate-report/migration-plan.html
```

The plan groups entries by action:

- **copy** — files not present at the destination; will be uploaded when v0.5-b runs.
- **skip** — already present at the destination with matching size + hash. Idempotent — re-planning the same run yields the same skip set.
- **defer** — shared cloud files (informational-only invariant) and Google-native docs (no downloadable bytes). Neither will be copied.
- **error** — file exceeds the destination's per-file cap (Google Drive 5 TB, OneDrive Personal 250 GB). Requires user attention; will never be uploaded.

## Filters

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
- `--include-shared` — reserved for future opt-in; not yet supported (shared files stay deferred).

## Safety recap

`dc migrate plan` preserves every v0.5 invariant already documented in `docs/AUDIT_LOG.md`:

- **BYO OAuth only.** No bundled client id is populated in the public repo.
- **Cloud paths never `.resolve()`d.** The planner works with `<source_id>://…` virtual paths verbatim.
- **Shared cloud files are informational.** Deferred, never in a `copy` action.
- **Google-native docs are informational.** Deferred, never in a `copy` action.
- **Rename policy: preserve.** The destination path mirrors the source directory structure. No filename mutation.
- **Read-only sources at plan time.** Both `--from` and `--to` are constructed with `is_read_only_scan=True`; no upload / trash / rename can fire from `dc migrate plan`.
- **Per-file destination cap.** Files exceeding the destination cap surface as `action="error"` with `size_limit_hit=True`; a v0.5-b `dc migrate copy` will refuse them.

The `Source.upload` protocol added in v0.5-a raises `SourcePermissionError` on any source constructed with `is_read_only_scan=True`, so a bug in the planner cannot accidentally write to the destination.
