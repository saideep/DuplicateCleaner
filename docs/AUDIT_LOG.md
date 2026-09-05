# Audit Log

Accumulates every audit round's findings, resolutions, decisions, and rejected alternatives across the lifetime of the project. Every future review agent MUST read this file before starting.

## Invariants (do not weaken)

These properties are load-bearing. Any change that weakens one of them is a ship-blocker regardless of local correctness.

- **No direct deletion**: `os.remove`, `os.unlink`, `Path.unlink`, `os.rmdir`, `shutil.rmtree`, `subprocess.run(["rm", ...])`, `os.system("rm ...")` MUST NOT appear in `src/`. `shutil.move` allowed only in `apply/undo.py`. Enforced by `tests/test_no_forbidden_calls.py`.
- **`dc apply` dry-run default**: `--commit` explicitly required to move anything.
- **Manifest written before any move**: tempfile + `f.flush()` + `os.fsync(fd)` + `os.replace(tmp, path)` + parent-dir fsync. Per-file re-flush after each successful move.
- **Every path from external JSON re-validated**: `report.roots`, per-member `path`, `manifest.original_path`, `manifest.trashed_at_path` — each `.resolve()`ed and checked against `EXCLUDED_ROOTS` + regex exclusions (`~/Library` across users, `/Volumes/*/System`, etc.) + `is_within(root)` containment where applicable.
- **`active_homes` validator**: rejects `/`, ≤2 resolved segments, non-existent paths, paths inside `EXCLUDED_ROOTS`. Stores resolved absolute paths. Same validator applied to `report.roots` and CLI scan roots.
- **Hardlink two-pass informational marking**: every peer sharing `(dev, inode)` is informational; entire-inode-family groups produce no keeper (reclaim = 0).
- **APFS clone informational marking**: peers sharing clone-family ID are informational, never proposed. (Feature broken in v0.1.1 — see H3; being fixed.)
- **`scan_stage` per-scan namespacing**: `scan_id` column isolates concurrent scans; 24h TTL sweep at start.
- **Per-file re-verify immediately before `send2trash`**: aborts the run on drift (does NOT skip-and-continue).
- **Trash-dir routing**: boot-volume → `~/.Trash`; `/Volumes/<VOL>/...` → `/Volumes/<VOL>/.Trashes/<uid>/`. Both mover-recording and undo-fallback use the same helper.
- **Archives**: whole-archive proposals only (never per-file inside an archive). Mover rejects any discard containing `::` unless outer archive is in whole-delete set. Undo must apply the same rejection to `original_path`.
- **Bundles**: `.app`, `.pages`, `.numbers`, `.keynote`, `.sparsebundle`, `.rtfd`, `.xcodeproj`, `.playground`, `.framework`, `.bundle` walked as atomic units. Hash = BLAKE3 over sorted `(rel, size, hash)` triples of all members.
- **Never scan**: `~/Library` (all users, regex), `/System`, `/Library`, `/Applications`, `/usr`, `/opt`, `/sbin`, `/bin`, `/etc`, `/private/etc`, `/var/{db,log,vm,root,audit,folders,tmp}` (+`/private/var/*`), `/Users/Shared`, `.Trashes/`, `.git/objects/`, iCloud `.icloud` placeholders. `/Volumes/*/System`, `/Volumes/*/Library`, `/Volumes/*/Applications`, `/Volumes/*/usr`, `/Volumes/*/opt`, `/Volumes/*/private/etc`, `/Volumes/*/private/var/{db,log,vm,root,audit}`, `/Volumes/*/Users/*/Library`.
- **Symlinks**: not followed by default. When followed, descendants `.resolve()`d before exclusion check.
- **`--discover` mode**: `proposed_keeper=None` on every group; `reclaim_bytes=0`; `apply_report` raises `ApplyError` on discover-mode input.
- **Singleton files never a discard candidate**: enforced in scorer AND mover.
- **Shared cloud files informational-only**: (v0.2) — never proposed for deletion.
- **Cloud discards go to cloud trash; undo restores via API** (v0.2): tool cannot hard-delete cloud files. OAuth scopes are trash-only (`drive.file`, `Files.ReadWrite`). Undo dispatches by `source_id` to the correct `Source.restore_from_trash`.
- **Cloud drift check via etag** (v0.2): immediately before `move_to_trash` on any cloud member, the source re-reads the file's current etag and verifies it matches the scan-time etag. Etag mismatch aborts the run (same semantics as size+mtime drift for local).
- **Manifest atomic-write applies to cloud entries too**: the tmpfile + fsync + os.replace + parent-dir-fsync pattern is unchanged; cloud manifest rows record `source_id`, `cloud_file_id`, `cloud_trash_id`, and pre-move `etag`.
- **Cohesive units move atomically** (v0.3): music albums, book series, git projects, and photo or video event clusters. Every plan entry in a cohesion group carries `cohesion_group`; `dc organize apply` refuses to run if members target different destinations unless `--split-cohesive-units` is passed. The invariant is checked before the first move; no partial split can happen mid-run.
- **Rename policy is user-locked** (v0.3): default `rename_policy = "preserve"`. In this mode filename bytes are never mutated by `dc organize`. `date_prefix` and `date_event_prefix` only apply when the user has explicitly opted in via config.

## Rejected alternatives (do not reopen without new info)

- **macOS Keychain for OAuth token storage** — rejected. User preference. Tokens live in `~/.config/duplicate_cleaner/tokens/<id>.json` mode 0600.
- **BYO-only OAuth clients** — rejected in favor of bundled default + BYO override (`--client-secret path.json`). Precedent: rclone, gsutil. User approved bundled.
- **`/private` blanket exclusion** — reverted. `/private/tmp` and `/private/var/folders` (initially unblocked to allow pytest `tmp_path`) exposed live app state. `/private/var/folders` and `/var/folders` re-blocked; pytest uses `--basetemp=/tmp/pytest-dc` under `/private/tmp` which remains scannable.
- **Adaptive weight learning (was v0.7)** — DROPPED per architect review. Rule-based scorer suffices for single-user; ML on <500 override examples adds noise not signal.
- **Perceptual near-dup ahead of organizer** — DROPPED sequencing. Reshuffled: organizer to v0.3, near-dup pushed to v0.7/v0.8.

## Round-by-round history

### v0.1 — exact-duplicate pipeline (shipped: bc974cc / d81fc0d, GitHub push confirmed 2026-09-05)

**First audit (Code Review pass 1 + Security pass 1)** — 12 safety findings + 6 correctness/perf + 2 test gaps = 20 items. 4 DATA-LOSS blockers:
- F1: mover didn't re-validate paths from report.json → fixed with `_validate_report_paths` + shared helper in `paths.py`.
- F2: symlink escape via unresolved descendants → walker resolves before exclusion check.
- F3: manifest write not atomic/fsync'd → tempfile + fsync + os.replace + parent-fsync pattern.
- F4: config accepted `/` as `active_homes` → Pydantic validator.

Other v0.1 fixes: F5 hardlink two-pass, F7 forbidden-calls grep expansion, F8 EXCLUDED_ROOTS regex for user Library, F9/F10 trash-dir routing, F11 per-file re-verify, F12 undo path validation, F13 cache COALESCE fix, F14 firmlink resolution, F15 git-cache memoization, F16 scan_stage streaming.

**Second audit (pass 2)** — 9 items after fix churn. G1 DATA-LOSS (report.roots itself unvalidated) fixed by extracting validator + applying same rules. G2 DATA-LOSS (`/private/var/folders` unblocked exposed live app state) fixed by re-blocking + pytest basetemp override. G3 (scan_stage race) fixed with per-scan UUID + TTL. G4-G9 remaining correctness/safety fixes all landed.

**Final state**: 110 tests passing, all 4 DATA-LOSS + 9 additional blockers cleared, APFS clone detection deferred to v0.1.1 with loud warnings.

### v0.1.1 — archives + bundles + monitoring + APFS clones + singletons + `--discover` (in fix cycle)

**Dev delivered**: 24 new tests (134 total). Ruff + mypy --strict clean.

**Third audit (Code Review pass 3 + Security pass 3)** — 11 findings. Blockers:
- H1 DATA-LOSS: whole-archive delete ignored encrypted/corrupt skipped members → user loses password-protected content.
- H2 DATA-LOSS: undo didn't validate `trashed_at_path` is inside a Trash directory → poisoned manifest can relocate `~/.ssh/id_rsa`.
- H3 broken feature: APFS clone bit `0x100` set on `commonattr` instead of `forkattr` → get_clone_id returns None always. Clones proposed for trash and reclaim over-reported.
- H4 DoS: nested archive `blob = reader.read()` no cap → OOM on large inner archives.

Other H5–H9 correctness/safety fixes: undo `::` guard, CPU throttle sampling bug, singleton emission gap, bundle symlink escape when --follow-symlinks, taskpolicy PATH-shim hijack.

Deferred to v0.1.2: bundle Unicode NFC/NFD normalization, singleton hash marker leaks paths, `max_workers` not wired to threadpool.

**Fix in flight**. Next re-audit will use fork agents + this AUDIT_LOG.md + high-standards scaffold.

**Fourth audit (Security pass 4 — first fork+scaffold round, 2026-09-05)** — verdict: **Ready with post-release notes.** All 9 H-fixes verified closed. Two DATA-LOSS blockers (H1 archive-encrypted-member, H2 undo-trashed-at-path-containment) closed cleanly. H3 APFS clone bit verified against real clonefile(2) on APFS. H4 nested-archive OOM closed via SpooledTemporaryFile cap. H5-H9 all landed. Three new findings, all deferred to v0.1.2:
- safety/PLAUSIBLE: bundle hardlink to excluded file (H8 symmetric — resolve() doesn't dereference hardlinks).
- simplification/CONFIRMED: nested archive is read twice (spool pass + hash pass); could be single-pass.
- simplification/CONFIRMED: singleton-by-partial marker leaks path into JSON (same shape as the size marker already documented for v0.1.2).

Category counts: 1 safety (PLAUSIBLE), 2 simplification (CONFIRMED). Zero DATA-LOSS, zero invariant-weakening.

**Fourth audit (Code Review pass 4, fork+scaffold, 2026-09-05)** — verified H1–H9 all correctly fixed under the new AUDIT_SCAFFOLD contract. No DATA-LOSS. No invariants weakened. 5 findings, all deferrable: 2 `simplification` (H7's `singleton-by-partial:...` marker leaks path — same class as v0.1.2-deferred `singleton-by-size` marker; undo.py's `_src_is_in_trash` duplicates `paths.is_inside_any_trash`), 2 `test-coverage` (no test proves nested-skip attributes to outer-archive path in `skipped_outer_archives`; no end-to-end test that H7 partial-unique singleton reaches `Report.singletons`), 1 `simplification` (H4 nested-archive double-hashes bytes; single-pass optimisation possible). **Verdict: Ready with post-release notes.** v0.1.1 clears ship.

**Tenth audit (Code Review pass 10 + Security pass 10, coordinator-direct review, 2026-09-05)** — verdict: **Ready with post-release notes.** Zero DATA-LOSS in 5c, zero invariants weakened. Verified: drift check runs BEFORE move_to_trash on every cloud dispatch; SourceDriftError aborts the whole run and flushes the manifest first; is_read_only_scan tripwire fires before drift-check + any HTTP call; cloud Path never .resolve()d; manifest atomic-write applies to cloud rows; token in memory only (msal default is in-memory TokenCache, no disk writes); gdrive/onedrive check_drift etag composites match list_files stamping shape; ordering local→cloud in the dispatch loop preserved.

Category counts: 1 safety (PLAUSIBLE, latent for 5d), 2 correctness (1 PLAUSIBLE + 1 CONFIRMED), 2 test-coverage (CONFIRMED), 2 simplification (CONFIRMED). Zero DATA-LOSS in 5c. Zero invariant-weakening.

Must-address in 5d (or same release):
- safety/PLAUSIBLE (latent DATA-LOSS for 5d): mover skips cloud entries on SourceNotFoundError from check_drift/move_to_trash by log-and-continue but leaves the manifest row with cloud_trash_id=None. 5d undo dispatch MUST check `cloud_trash_id is not None` before calling Source.restore_from_trash — else a file another client trashed (which caused our 404) gets silently un-trashed on `dc undo`, reversing user intent.
- correctness/PLAUSIBLE: `cli._build_onedrive_source._refresh_now` drops the rotated refresh_token in MSAL's response and only stashes access_token in an in-memory cache; the on-disk refresh_token becomes stale as soon as Microsoft rotates. Persist the new refresh_token back via TokenStore.save when the response carries one.
- test-coverage/CONFIRMED: no test proves a multi-entry mid-batch abort flushes the manifest with entries 1..(n-1) stamped as trashed. Add a 3-cloud-entries test where entry 3 raises SourceDriftError and assert on-disk manifest.
- test-coverage/CONFIRMED: `test_apply_cloud_entry_calls_source_move_to_trash` does not assert check_drift → move_to_trash ORDER via `src.method_calls[0..1]`; a swap regression would pass.
- correctness/CONFIRMED: CLI apply summary hides skipped cloud entries (no `skipped_cloud` counter surfaced from the mover result dict).
- simplification/CONFIRMED: `msal.PublicClientApplication` reconstructed on every `_refresh_now` call; cache once per account.
- simplification/CONFIRMED: OneDrive `check_drift` fall-through to raw Graph eTag is unreachable dead code under 5c's composite-etag emission.

### v0.2 — cloud sources (sub-milestone 5c: Source.move_to_trash wired from mover + pre-trash drift check)

**Sub-milestone 5c Dev delivered** (2026-09-05): 12 new tests (276 → 288 total).  Ruff clean on every changed source and test file.  Zero-behavior-change for local scans; the v0.1.1 rails still run for every `source_id == "local"` entry.

Shipped:
- `sources/base.py` — new `SourceDriftError(SourceError)` subclass. `Source` protocol gains `check_drift(record) -> None`, default raises `NotImplementedError` for backwards compat with any future source that hasn't wired it yet.  Exported from `sources/__init__.py`.
- `sources/gdrive.py::GoogleDriveSource.check_drift` — issues `files.get(fileId=..., fields="id,modifiedTime")`, rebuilds the composite `f"{cloud_file_id}:{modifiedTime}"`, compares against `record.etag`. Mismatch → `SourceDriftError`. Source-id + cloud_file_id shape guards fire BEFORE the URL is built (Security pass 7 mirror).
- `sources/onedrive.py::OneDriveSource.check_drift` — issues `GET /me/drive/items/{id}?$select=id,eTag,lastModifiedDateTime`. Primary check is the composite `f"{cloud_file_id}:{lastModifiedDateTime}"`; the raw Graph `eTag` is checked as a defense-in-depth secondary because Graph does not always echo it on `$select`. Same shape guards + URL encoding as `move_to_trash` / `restore_from_trash`.
- `sources/local.py::LocalFileSystemSource.check_drift` — mirrors `_verify_unchanged` (size + mtime) via a `Source`-compatible signature. The mover's existing `_verify_unchanged` pathway is what actually runs for local dispatch; this method exists so a caller that wants a uniform `Source.check_drift` interface can use it too.
- `apply/mover.py::apply_report` — new keyword arg `sources: dict[str, Source] | None = None`. `None` refuses any cloud discard with a clear `ApplyError` up-front (local-only mode). When cloud entries exist the mover looks up each `source_id` in the map, tripwires `is_read_only_scan == False`, runs `check_drift(record)` (aborts on `SourceDriftError`), then `move_to_trash(record)`. Retry logic already lives inside each source's implementation via `tenacity`. Manifest entry stamps `source_id`, `cloud_file_id`, `cloud_trash_id`, `etag`, `original_path` (opaque cloud display string); the tempfile + fsync + `os.replace` + parent-fsync atomic-write pattern applies to cloud entries too. Per-move flush after every successful move preserved (G6 mirror for cloud).
- `apply/mover.py` — new `plan_cloud_moves(report)` and `_member_to_cloud_record(m)` helpers.  Result dict now reports `planned_local` / `planned_cloud` / `moved_local` / `moved_cloud` alongside legacy `planned` / `moved`.  `cloud_deferred` stays for backcompat but is always 0 in 5c.
- `apply/mover.py` — batch failure handling per design doc §5.4:
  - `PathChangedError` (local drift), `SourceDriftError`, `SourceAuthError`, `SourceRateLimitError` — abort mid-apply. Manifest flushed first so entries 1..(n-1) are recoverable via `dc undo`.
  - `SourceNotFoundError`, `SourcePermissionError`, `OSError` (local `send2trash`) — logged per-entry, apply continues.
- `cli.py::apply` — builds the `sources` map at apply time by walking `AccountsRegistry.load()` and instantiating `GoogleDriveSource` / `OneDriveSource` per registered account with `is_read_only_scan=False`. Prints the operation summary: `"Moved N local file(s), N cloud file(s) to Trash."` New `--force-refresh` flag proactively refreshes tokens before dispatch. `msal.PublicClientApplication.acquire_token_by_refresh_token` now wires OneDrive token refresh (Security pass 8 finding: `msal` was declared but unused; this is where it lands). Google's `google.oauth2.credentials.Credentials` handles refresh automatically on API calls; `--force-refresh` calls `creds.refresh(Request())` proactively.

Tests shipped:
- `tests/test_apply_cloud_dispatch.py` (new file, 8 tests):
  - `test_apply_cloud_entry_calls_source_move_to_trash` — cloud discard invokes check_drift + move_to_trash with the right FileRecord (source_id, cloud_file_id, etag).
  - `test_apply_cloud_entry_refused_when_source_not_in_map` — mismatched sources map → `ApplyError`, no source method called.
  - `test_apply_cloud_entry_drift_check_fires` — `SourceDriftError` from check_drift aborts the run; move_to_trash never fires.
  - `test_apply_cloud_manifest_records_source_metadata` — manifest entry carries source_id, cloud_file_id, cloud_trash_id, etag; trashed_at_path stays null for cloud.
  - `test_apply_mixed_local_and_cloud` — 2 local + 3 cloud entries; local goes through the trash_fn, cloud through the sources map; manifest has both shapes.
  - `test_apply_cloud_shared_file_refused` — is_shared=True → refused at validate time before any dispatch.
  - `test_apply_read_only_source_refused` — is_read_only_scan=True trips BEFORE check_drift fires.
  - `test_apply_cloud_entry_local_only_mode_refuses` — sources=None with any cloud entry → `ApplyError`.
- `tests/test_sources_gdrive.py` — 2 new tests:
  - `test_check_drift_no_change_returns_none` — matching modifiedTime returns None; `fields="id,modifiedTime"` verified.
  - `test_check_drift_mismatch_raises_source_drift_error` — different modifiedTime → `SourceDriftError`.
- `tests/test_sources_onedrive.py` — 2 new tests:
  - `test_check_drift_no_change_returns_none` — matching lastModifiedDateTime returns None; `$select=id,eTag,lastModifiedDateTime` verified.
  - `test_check_drift_mismatch_raises_source_drift_error` — different lastModifiedDateTime → `SourceDriftError`.
- `tests/test_mover_source_id_dispatch.py::test_cloud_path_never_resolved` — updated for 5c: injects a MagicMock source; asserts `check_drift`/`move_to_trash` aren't called under dry-run; `cloud_deferred == 0`, `planned_cloud == 1`.

Invariants preserved (all AUDIT_LOG items above):
- All 276 pre-existing tests still pass unchanged; 12 new tests added (288 total).
- `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py`.
- Cloud `Path` NEVER `.resolve()`d in the new dispatch code (regression test locks it via `test_cloud_path_never_resolved`).
- `is_read_only_scan` tripwire fires BEFORE any HTTP call — asserted by `test_apply_read_only_source_refused`.
- Drift check happens BEFORE `move_to_trash`; etag mismatch aborts the entire run (not skip-and-continue) — matches design doc §5.4 and AUDIT_LOG invariants.
- Manifest tempfile + fsync + `os.replace` + parent-fsync pattern preserved for cloud entries; per-move flush after every successful cloud move.
- `sources/base.py` `check_drift` default raises `NotImplementedError` — existing structurally-typed source implementations without `check_drift` would surface loudly rather than silently skip the drift check.
- Ruff clean on every changed source and test file. Mypy `--strict` clean on all changed files (pre-existing `unused-ignore` warnings on 3rd-party import stubs are unchanged — documented in sub-milestone 2 notes).

Explicitly deferred to sub-milestone 5d/5e:
- Wiring `Source.restore_from_trash` from `restore_from_manifest` for cloud manifest rows (undo dispatch).
- Cross-source scoring (`cloud_when_local_exists`, `is_singleton_across_sources`, `retained_cloud_order`).
- `FakeCloudSource` end-to-end integration test.

### v0.2 — cloud sources (sub-milestone 5b: mover source_id dispatch + Part B DATA-LOSS closers)

**Sub-milestone 5b Dev delivered** (2026-09-05): 24 new tests (252 → 276 total). Ruff clean on every changed source file. Alt-C dispatch pattern from design doc §4.2 landed; three latent DATA-LOSS findings from Security pass 7 closed cleanly. `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py`.

Part A shipped:
- `paths.py` — new `validate_cloud_entry(member, registry)` helper enforces the Alt-C rails on one `ReportMember`: `source_id` present in `AccountsRegistry.list()`, `cloud_file_id` matches the per-provider shape regex (`^[a-zA-Z0-9_-]{20,}$` for gdrive; `^[a-zA-Z0-9!]{20,}$` for onedrive), `etag` non-empty (required by 5c drift check), `is_shared` False (invariant). Companion `validate_cloud_manifest_entry(entry_dict, registry)` for the undo side (no is_shared column on manifest rows).
- `apply/mover.py::_validate_report_paths` — routes per-member on `FileRecord.source_id`. Local branch runs the v0.1.1 rails unchanged. Cloud branch runs `validate_cloud_entry` only — cloud `Path` is NEVER `.resolve()`d, per design invariant. `AccountsRegistry` is a new optional parameter defaulting to a lazily-loaded instance so every v0.1.1 caller works untouched.
- `apply/mover.py::plan_moves` — filters out `source_id != "local"` so the local trash loop never dispatches a cloud entry. `_count_cloud_discards` surfaces the cloud-count in the result dict as `cloud_deferred`; sub-phase 5c wires the actual `Source.move_to_trash` here. TODO(sub-phase 5c) marker placed inside `apply_report`.
- `apply/undo.py::restore_from_manifest` — mirror dispatch: local entries flow through the existing F12/H2/H5 rails; cloud entries run `validate_cloud_manifest_entry`. The archive-member `::` scan is scoped to `source_id == "local"` entries only (a cloud `original_path` is opaque display data, not a filesystem target). `cloud_deferred` surfaces in the result dict; TODO(sub-phase 5d) marker placed on the cloud branch.

Part B shipped — Security pass 7 latent DATA-LOSS closers:
- `sources/onedrive.py` — `_ONEDRIVE_ID_RE = re.compile(r"^[A-Za-z0-9!]{1,120}$")` at module level. `move_to_trash`, `restore_from_trash`, and `read_bytes` validate `cloud_file_id` / `loc.cloud_file_id` shape FIRST, raise `SourceError` on mismatch, then URL-encode via `urllib.parse.quote(id, safe='')` before URL interpolation (belt-and-braces).
- `sources/onedrive.py` — client now uses `follow_redirects=False`; the `_BearerAuth` flow strips the Authorization header when the request host is not `graph.microsoft.com` (defense-in-depth). `read_bytes` explicitly follows the Graph 302 to Azure CDN with a NEW `_build_unauth_http_client()` that has NO auth wired in — bearer never lands on `blob.core.windows.net`.
- `sources/onedrive.py::list_files` — `@odata.nextLink` validated with `link.startswith(GRAPH_ROOT + "/")` before use; any non-Graph origin raises `SourceError` (MITM / proxy-injection signal) rather than blindly following the URL.
- `sources/gdrive.py` — analogous `_GDRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")` + `_validate_cloud_file_id` at module level; `move_to_trash`, `restore_from_trash`, and `read_bytes` validate + URL-encode.

Tests shipped:
- `tests/test_mover_source_id_dispatch.py` — 7 tests covering local rails unchanged, unknown source_id refusal, missing/malformed cloud_file_id, missing etag, is_shared=True refusal, and the "cloud path never resolved" regression (patches `resolve_for_check` and asserts no cloud Path reaches it).
- `tests/test_undo_validation.py` — 11 tests covering symmetric undo dispatch, mixed local+cloud manifests (local restores, cloud deferred), the F12 rail still fires on local entries, the H5 archive-member check scoped to local, and a parameterized cloud_file_id shape regression.
- `tests/test_sources_onedrive.py` — 4 new tests: bad-shape refusal for move + restore, bearer stripped on cross-origin redirect (fake Graph 302 to `blob.core.windows.net`, verify second-hop client is the unauth factory), and `@odata.nextLink` origin refusal (evil.example.com aborts).
- `tests/test_sources_gdrive.py` — 2 new tests: bad-shape refusal for move + restore. Realistic 20+ char Base64url ids substituted throughout the existing tests to align with the shape gate.

Test helper fix — `tests/test_sources_onedrive.py::_install_fake_httpx` now installs a fake `TransportError` class in addition to `HTTPStatusError` so the pass-8 `_is_retryable_http_error` classifier does not `AttributeError` when the real httpx package is absent. This closes a pre-existing test-infrastructure bug uncovered while validating baseline test counts.

Invariants preserved:
- 252 pre-existing tests still pass unchanged; 24 new tests added.
- `test_no_forbidden_calls.py` still green over `paths.py` / `apply/mover.py` / `apply/undo.py` / `sources/onedrive.py` / `sources/gdrive.py`. `shutil.move` still confined to `apply/undo.py`.
- Cloud `Path` never resolved. `test_cloud_path_never_resolved` and `test_undo_cloud_path_never_resolved` lock this in by patching `resolve_for_check` and asserting no `gdrive:` path reaches it.
- Alt-C dispatch pattern followed exactly per design doc §2 recommendation.
- Ruff clean on every changed source and test file.

Explicitly deferred to sub-phase 5c/5d/5e (per design doc §10):
- Actual wiring of `Source.move_to_trash` / `Source.restore_from_trash` from the mover / undo (validation only in 5b — the TODO markers are in place).
- Pre-trash etag drift check via `Source.get_metadata(record, refresh=True)`.
- Cross-source scoring (`cloud_when_local_exists`, `retained_cloud_order`).
- `FakeCloudSource` end-to-end integration test.

### v0.2 — cloud sources (sub-milestone 5a: schema surgery)

Sub-milestone 5a Dev delivered (2026-09-05): schema fields added to `ReportMember` + `ManifestEntry`, backward-compat loader (v0.1.1 reports load with `source_id="local"` default), 9 new tests (215 → 224 total). Zero behavior change on local reports. Sub-milestone 5b next: mover dispatches on source_id.

Shipped:
- `ReportMember` gains five trailing defaulted fields per design doc §3.1: `source_id="local"`, `cloud_file_id: str | None = None`, `etag: str | None = None`, `owner: str | None = None`, `is_shared: bool = False`. A v0.1.1 report.json (no `version` key, no `source_id` on members) loads cleanly and every member defaults to `source_id="local"` — no forced re-scan.
- `Report.version` default bumped from `"0.1.1"` to `"0.2.0"`. Older markers are still accepted on read (Pydantic field is a plain `str`).
- New `ManifestEntry` and `Manifest` Pydantic models in `report/schema.py` per design doc §3.2. `ManifestEntry` fields: `original_path`, `size`, `mtime`, `hash`, `trashed_at_path`, plus v0.2 additions `source_id="local"`, `cloud_file_id=None`, `cloud_trash_id=None`, `etag=None`. `Manifest` envelope: `manifest_version="0.2.0"`, `created_at`, `roots: list[str] = []`, `entries: list[ManifestEntry]`.
- `apply/mover.py::apply_report` now constructs each manifest row via `ManifestEntry(...).model_dump()` and each flushed envelope via `Manifest(...).model_dump()`. Every v0.2 write stamps `source_id="local"` + null cloud fields + `manifest_version="0.2.0"` explicitly (no missing-vs-null ambiguity).
- `tests/test_schema_backward_compat.py` — 9 tests: v0.1.1 report loads with default `source_id`; v0.1.1 report `apply_report --dry-run` still works; v0.2 report round-trips; v0.2 report always serializes `source_id` explicitly; v0.1.1 manifest row loads with default `source_id`; v0.1.1 manifest envelope loads with default `manifest_version`; v0.2 manifest round-trips; v0.2 manifest local entry serializes `source_id` explicitly; mover writes v0.2 shape on disk end-to-end.

Deferred to sub-milestone 5b (mover dispatch):
- `_validate_report_paths` cloud vs local branching per design §4.2.
- `validate_cloud_entry` helper in `sources/base.py`.
- `AccountsRegistry.load()` integration into apply-time authorization set.

Invariants preserved:
- All 215 pre-existing tests still pass unchanged.
- `test_no_forbidden_calls.py` still passes.
- Ruff clean on every touched file.
- `mypy --strict` on `src/duplicate_cleaner/report/schema.py` clean; the pre-existing `unused-ignore` warnings across `src/` (documented in sub-milestone 2 notes for the stub-installed env) are unchanged.
- On-disk manifest gains `manifest_version` + `source_id` + null cloud fields; every v0.1.1 manifest still round-trips through `restore_from_manifest` (undo reads via `dict.get()` with the same defaults).

Notes for review:
- The mover's `_flush_manifest` now populates `manifest.roots` from `report.roots`. `apply/undo.py::restore_from_manifest` already validates that array with `validate_scan_root_candidate` when present, so pytest tmp_path values under `/tmp/pytest-dc` continue to pass (≥3 resolved segments, not in `EXCLUDED_ROOTS`).
- Cloud-side dispatch, drift check, and undo integration are NOT included — every read/write path in this sub-milestone still runs the v0.1.1 local rails. The pass-5/pass-6 `Path("gdrive:x://foo")` landmine remains latent (nothing feeds cloud entries through the mover yet) and will be closed by sub-milestone 5b.

### v0.2 — cloud sources (sub-milestone 4: OneDriveSource read + trash + restore)

**Sub-milestone 4 Dev delivered** (2026-09-05): 25 new tests for OneDrive + 3 new tests in `test_cli_auth.py` = 28 new tests. Ruff clean on all new/modified files (auth/clients.py, auth/__init__.py, sources/onedrive.py, sources/__init__.py, cli.py, tests/test_sources_onedrive.py, tests/test_cli_auth.py). All 252 tests passing (215 baseline + 9 existing schema-backcompat + 25 onedrive + 3 cli-auth-onedrive). `test_no_forbidden_calls.py` still green — no destructive filesystem calls added; OneDrive trash flows through Microsoft Graph's Recycle Bin API. `shutil.move` still confined to `apply/undo.py`.

Shipped:
- `sources/onedrive.py` — new file. `OneDriveSource` mirrors `GoogleDriveSource` shape. Wire choice per design: raw `httpx` + `msal`, no `msgraph-sdk`.
  - Enumerate via `GET /me/drive/root/delta` (paginated via `@odata.nextLink`). First call returns every non-deleted item. `deleted != null` filtered, folders (`folder` field) filtered, items missing `file.hashes.sha256Hash` skipped with debug log (OneDrive Business `quickXorHash`-only items — out of scope for v0.2).
  - Foreign hash format: `sha256:<hex-lower>` (lower-cased for consistency with BLAKE3's hexdigest).
  - `remoteItem` present ⇒ `is_shared=True` (shared-with-you drive item — authoritative signal per Graph docs). Falls back to `createdBy.user.id != account_user_id` when supplied; B2 mirror ensures missing `createdBy.user.id` defaults to `owner_is_me=False` (not True) so the informational-only invariant is preserved.
  - Path scheme: `Path(f"onedrive:<account_id>://<parent_path>/<name>")`. Colon-boundary in Graph's `parentReference.path` (e.g. `/drive/root:/Documents/Sub`) is stripped so the display path shows `Documents/Sub/file.pdf`. Forward-compatible with sub-phase 5 Alt-C: no `Path.resolve()` semantics required in sub-phase 4 code.
  - `move_to_trash`: `DELETE /me/drive/items/{id}` (204 No Content on success). Returns `TrashedLocation(source_id, cloud_file_id=id, cloud_trash_id=id)` — OneDrive Personal keeps the same id after moving to the Recycle Bin.
  - `restore_from_trash`: `POST /me/drive/items/{id}/restore`. When Graph returns HTTP 501 OR any status carrying `error.code == "notSupported"` (documented Personal-only quirk), `SourceError` fires with an actionable message pointing at `https://onedrive.live.com/?id=recyclebin`.
  - Retry via tenacity on 429/5xx — same `_graph_call` wrapper shape as `_drive_call` in gdrive.py.
  - `_raise_mapped` translates Graph errors: 401 → `SourceAuthError`, 403 → `SourcePermissionError`, 404 → `SourceNotFoundError`, 429/5xx (post-retry) → `SourceRateLimitError`.
  - Read-only-scan tripwire: `is_read_only_scan=True` default. `move_to_trash` raises `PermissionError` before any HTTP call — parity with GoogleDriveSource.
  - `httpx` and `msal` imported lazily so the module can be `py_compile`'d and unit-tested with mocks even without the runtime deps installed. `client_factory` constructor param lets tests inject a MagicMock directly.
- `auth/clients.py` — adds `BUNDLED_ONEDRIVE_CLIENT_ID = "BUNDLED_ONEDRIVE_CLIENT_ID_TO_REPLACE"` sentinel (same pattern as gdrive), `BUNDLED_ONEDRIVE_CLIENT_SECRET = ""` (Microsoft public clients don't need a secret with PKCE), `ONEDRIVE_AUTH_URL` / `ONEDRIVE_TOKEN_URL` (`/consumers/` authority — rejects Business tenants at token endpoint), `ONEDRIVE_LOGOUT_URL` (unused for revoke since Microsoft has no programmatic revoke endpoint), `ONEDRIVE_DEFAULT_SCOPES = ("Files.ReadWrite", "offline_access", "User.Read")`, and `GRAPH_ROOT` constant.
- `cli.py` — `_resolve_client_credentials` gains an `onedrive` branch with the same `_TO_REPLACE` sentinel guard (B1 pattern). `dc auth add onedrive` reuses the shared `run_localhost_flow` runner with Microsoft endpoints + `prompt=select_account`. `dc auth test onedrive:<label>` runs a single-item list against Graph to prove the token still works. `dc auth remove onedrive:<label>` best-effort deletes local token; Microsoft has no revocation endpoint so it surfaces a link to `https://account.live.com/consent/Manage` for full app-level revoke. Same duplicate-account guard (B4) applies; same "unknown auth type" refusal for `dropbox` / etc.
- `sources/__init__.py` — exports `OneDriveSource`.
- `pyproject.toml` — adds `httpx>=0.27.0,<0.28` and `msal>=1.31.0,<2` to core deps.

Explicitly deferred to sub-phase 5 (unchanged from sub-phase 3):
- Cloud path validation architecture (`Path("onedrive:x://foo")` non-absolute). Same TODO marker in `apply/mover.py::_validate_report_paths` covers both providers.
- Wiring `Source.move_to_trash` / `Source.restore_from_trash` from the mover for cloud discards.
- Manifest schema extension (`source_id`, `cloud_file_id`, `cloud_trash_id`) — schema already backward-compatible with local-only manifests.
- Cross-source scoring (`cloud_when_local_exists`, singleton-across-sources).
- Microsoft Business tenant rejection at auth-add time (check token `tid` claim). Currently the `/consumers/` authority ensures Personal-only sign-in at the OAuth flow level; a token-claim double-check is desirable but not blocking for sub-phase 4.

Invariants preserved:
- All 215 baseline tests still pass; 28 new tests added.
- `test_no_forbidden_calls.py` still passes. No destructive filesystem calls added; OneDrive trash flows through Graph's Recycle Bin.
- `is_read_only_scan=True` tripwire enforced on `OneDriveSource` before any HTTP call.
- Shared cloud files (`remoteItem` present OR `createdBy.user.id != account_user_id`) marked `is_shared=True` — informational-only invariant preserved.
- OneDrive path scheme `onedrive:<account_id>://<path>` chosen for compatibility with sub-phase 5 Alt-C dispatch on `FileRecord.source_id`. Sub-phase 4 code does NOT rely on `Path.resolve()` semantics.
- B1 placeholder guard applied to onedrive — `dc auth add onedrive` exits 1 loudly instead of surfacing a raw `invalid_client` from Entra.
- No new hard-coded credentials. `BUNDLED_ONEDRIVE_CLIENT_ID_TO_REPLACE` is the pre-release sentinel, actively gated at the `_resolve_client_credentials` boundary.
- Ruff clean on every changed source and test file. Mypy — same `unused-ignore` env quirks as the audit log documented for sub-phase 2; no new structural mypy errors introduced.

Notes for review:
- OneDrive Personal historically returns 501 for `POST /items/{id}/restore`. The design contract specifies "raise `SourceError` with a clear message pointing at the OneDrive web recycle bin"; we honour that AND additionally detect `error.code == "notSupported"` regardless of HTTP status, since Graph is inconsistent between 400 / 500 / 501 for this case. A 404 (permanently deleted) still surfaces as `SourceNotFoundError` per the shared contract.
- The `account_user_id` constructor param on `OneDriveSource` is optional; when absent, non-`remoteItem` items default to `is_shared=True` (B2 mirror — never default to owner-is-me). CLI-level wiring can populate this via `GET /me` at auth-add time; that hookup lands cleanly in sub-phase 5.

**Security pass 7 (fork+scaffold, 2026-09-05)** — verdict: **Ready with post-release notes.** Zero DATA-LOSS, zero invariants weakened. 7 findings. Sub-phase 5a schema surgery verified truly zero-behavior-change: no code path in sub-phase 5a dispatches on `source_id`; local rails run for every manifest entry; a poisoned v0.2 manifest with `source_id="gdrive:evil"` cannot trigger cloud action because the mover-to-source dispatch does not exist yet (deferred to 5b/5c). All 7 findings are latent under the sub-phase 4 code (mover does not wire cloud yet). Category counts: 3 safety (PLAUSIBLE), 1 correctness (CONFIRMED), 1 simplification (CONFIRMED), 1 test-coverage (CONFIRMED), 1 latent-DATA-LOSS-shape (PLAUSIBLE, folded into safety). Must-fix BEFORE sub-phase 5b lands:

- safety/PLAUSIBLE (would become DATA-LOSS at 5b): `onedrive.py:414` — `move_to_trash` and `restore_from_trash` interpolate `cloud_file_id` into the URL path with no shape check or URL-encoding. A poisoned manifest with `cloud_file_id="root:/../foo"` would target the wrong item. Sub-phase 5b's `validate_cloud_entry` regex closes it; land the regex check inside the source method too as belt-and-suspenders.
- safety/PLAUSIBLE (token exfil): `onedrive.py:76` — `httpx.Client(auth=_BearerAuth(...), follow_redirects=True)` forwards the Bearer to non-Graph origins on 302 redirects (Azure CDN for /content; any @odata.nextLink pointing elsewhere). Strip Authorization on cross-origin redirects, or manually re-issue the request without auth on the 302 target.
- safety/PLAUSIBLE (token exfil): `onedrive.py:252` — `@odata.nextLink` is used verbatim as the next GET URL. Validate `link.startswith(GRAPH_ROOT + "/")` before assigning to `next_url`.

Should-fix in sub-phase 5:
- correctness/CONFIRMED: `onedrive.py:97` — 501 is classified retryable by `_is_retryable_status`; Personal's restore-not-supported case spends 5 retries + exponential backoff before surfacing the actionable error. Exclude 501 from the retry classifier and short-circuit on `error.code == "notSupported"` body.
- safety/PLAUSIBLE: `onedrive.py:463` — the Personal-restore-unsupported SourceError embeds `loc.cloud_file_id!r` in the exception string. Drop it; the recycle-bin URL alone is actionable.
- simplification/CONFIRMED: `pyproject.toml:24` — `msal>=1.31.0` is a core dep but not imported anywhere in `src/`. Move to `[project.optional-dependencies].onedrive` or defer until sub-phase 5 wires refresh.
- test-coverage/CONFIRMED: `tests/test_sources_onedrive.py` — 25 tests, none pin cloud_file_id shape refusal, nextLink origin refusal, or Bearer-strip-on-cross-origin-redirect. Add three targeted tests to lock the safety findings above once fixed.

Verified positives:
- `test_no_forbidden_calls.py` still green over `onedrive.py` and `schema.py`. `shutil.move` still confined to `apply/undo.py`.
- `is_read_only_scan=True` tripwire fires at `onedrive.py:405-409` before URL build, before any HTTP call.
- `remoteItem` presence → `is_shared=True` (line 333-334); B2 mirror preserves `is_shared=True` default when `account_user_id`/`owner_user_id` absent (line 336-343). Informational-only invariant preserved.
- B1 placeholder guard for OneDrive client id (`cli.py:718-732`) mirrors the gdrive shape correctly; `dc auth add onedrive` exits 1 with actionable text instead of Entra's raw `invalid_client`.
- Schema back-compat: `test_v0_1_1_report_apply_dry_run_still_works` is a real load_report+apply_report integration test — v0.1.1 JSON with no `version` marker loads, every member defaults to `source_id="local"`, `apply_report --dry-run` produces byte-identical planned/verified counts.
- `Manifest.model_dump_json()` only serializes non-sensitive fields (`manifest_version`, `created_at`, `roots`, `entries` with local + cloud id/etag fields). No token/refresh material can leak through the schema.
- `_validate_report_paths` at `apply/mover.py:52-114` still rejects cloud discards by construction: `resolve_for_check(Path("onedrive:x://..."))` fails `is_within(root)` against any legitimate `report.roots` entry. The TODO(sub-phase 5) landmine at mover.py:46-51 remains latent — no cloud entry can reach a trash operation until 5b/5c.
- `restore_from_manifest` in `apply/undo.py` never dispatches on `source_id` in 5a; a poisoned `source_id="gdrive:evil"` in a v0.2 manifest cannot dispatch to a cloud path because dispatch does not exist yet. Zero-behavior-change confirmed.

215 baseline + 25 onedrive + 3 cli-auth-onedrive + 9 schema-back-compat = 252 tests passing. Ruff clean.

### v0.2 — cloud sources (sub-milestone 3: GoogleDriveSource trash + restore + bundled audit fixes)

**Sub-milestone 3 Dev delivered** (2026-09-05): 16 new tests (215 total, 199 → 215). Ruff clean on all new/modified src + test files. `test_no_forbidden_calls.py` still green under the new path-relative allowlist match. Zero-behavior-change for local scans; default `--sources local` unchanged.

Part A shipped:
- `GoogleDriveSource.move_to_trash` — calls `files.update(fileId, body={"trashed": True})`; retries 429/5xx via the existing `_drive_call` tenacity loop; returns `TrashedLocation` with `cloud_file_id = cloud_trash_id = record.cloud_file_id` (Google Drive keeps the id after trashing). Raises `PermissionError` when `is_read_only_scan=True` (scan-side tripwire).
- `GoogleDriveSource.restore_from_trash` — calls `files.update(fileId, body={"trashed": False})`. Rejects a mismatched `TrashedLocation.source_id`.  On 404 (trash emptied) raises `SourceNotFoundError`.  On 403 raises `SourcePermissionError`; on 401 `SourceAuthError`; on final-attempt 429/5xx `SourceRateLimitError`.
- `sources/base.py` grows a typed exception hierarchy: `SourceNotFoundError`, `SourcePermissionError`, `SourceRateLimitError`, `SourceAuthError` — all extend `SourceError`.  Wired through `sources/__init__.py`.

Part B shipped (audit follow-ups):
- **B1** — `_resolve_client_credentials` refuses when the bundled client id ends with `_TO_REPLACE`; prints "use --client-secret" guidance and exits 1 instead of failing at Google with a raw `invalid_client`.
- **B2** — `GoogleDriveSource._item_to_record` now defaults `owners[0].me` to `False`. A shared-with-me file whose response omits `me` is correctly marked `is_shared=True` (invariant preserved).
- **B3** — `dc scan --sources non-local` is refused with a `sub-phase 5` message; `--max-cloud-download-mb` non-default value emits a warning that the flag has no effect yet.  A new `--cloud-hash-ttl-days` config knob replaces the previous hard-coded 90-day sweep.
- **B4** — `dc auth add gdrive` on an existing account_id prompts (`stdin.isatty()`) or refuses (`--force` required) BEFORE the OAuth flow runs. Token file no longer overwritten before `DuplicateAccountError`.
- **B5** — `reconcile_bucket` same-algo shortcut fires for any shared algo, not just blake3. Two md5-carrying members from different accounts group without a byte download.
- **B6** — `run_localhost_flow` uses `hmac.compare_digest` for the OAuth state check.
- **B7** — `AccountsRegistry.load` calls `enforce_secure_mode` and raises `AccountsRegistryPermissionError` on a 0o644 accounts.toml (symmetric to `TokenStore.load`).
- **B8** — `TokenStore._ensure_dir` post-chmod verifies the mode; raises `TokenPermissionError` if the dir mode does not equal 0o700 after chmod.
- **B9** — `LocalFileSystemSource.restore_from_trash` now runs the H2/F12/H5 rails via a shared `apply.undo.validate_restore_paths` helper. A poisoned `TrashedLocation.local_trashed_at_path` outside a known Trash dir raises `UndoError` before any shutil.move fires.
- **B10** — `test_no_forbidden_calls.py::_SHUTIL_MOVE_ALLOWED_FILES` renamed to `_SHUTIL_MOVE_ALLOWED_RELPATHS`; matches full path-relative-to-src (`apply/undo.py`) instead of basename. Meta-test guards against basename regression.
- **B11** — `_default_trash_fn` extracted to `apply/trash.py::default_trash_fn`. Both `apply/mover.py` and `sources/local.py` import from the single canonical spelling.
- **B12** — `dc scan` invokes `store.purge_stale_cloud_hashes(max_age_days=cloud_hash_ttl_days)` on startup. Test-covered via a monkeypatched spy.
- **B13** — dead code removed from `hash/reconciliation.py` (`_unused_path_placeholder`, `CLOUD_HASH_TRIPLE`, orphaned `Path` import).

Explicitly deferred to sub-phase 5:
- Cloud path validation architecture (`Path("gdrive:x://foo")` non-absolute). A `# TODO(sub-phase 5): cloud path validation` marker was placed atop `_validate_report_paths` in `apply/mover.py`.
- Wiring `Source.move_to_trash` / `Source.restore_from_trash` from the mover for cloud discards.
- Manifest schema extension (source_id, cloud_file_id, cloud_trash_id).
- Cross-source scoring (`cloud_when_local_exists`, singleton-across-sources).

Invariants preserved:
- All 199 pre-existing tests still pass; 16 new tests added (208 → 215 total after replacing 2 obsoleted `NotImplementedError` tests).
- `test_no_forbidden_calls.py` still passes with the extended path-relative allowlist.
- Ruff clean on every source file and every test file touched.
- No new hard-coded credentials.  The pre-release `BUNDLED_GDRIVE_CLIENT_ID_TO_REPLACE` sentinel is now actively gated by `_resolve_client_credentials`.
- `shutil.move` remains confined to `apply/undo.py`.

### v0.2 — cloud sources (sub-milestone 2: shared OAuth infra + GoogleDriveSource read-only)

**Sub-milestone 2 Dev delivered** (2026-09-05): 41 new tests (199 total, 158 existing + 41 new). Ruff clean on all new files. Zero-behavior-change for local scans (default `--sources local`).

Shipped:
- New package `auth/` with `TokenStore` (0o600 file / 0o700 dir mode enforcement on read AND write; atomic tempfile + `os.fsync` + `os.replace` + parent-dir fsync; delete via `send2trash` to avoid violating the no-`os.remove` forbidden-calls invariant), `AccountsRegistry` (`accounts.toml` with unique-id enforcement, atomic write, 0o600 mode), and generic stdlib-only `run_localhost_flow` PKCE + S256 loopback runner reused by OneDrive in sub-phase 4.
- Bundled OAuth client constants at `auth/clients.py` — placeholder `BUNDLED_GDRIVE_CLIENT_ID_TO_REPLACE`; TODO tracked here for the real Cloud Console registration before v0.2 ships. BYO override via `--client-secret path.json` (`load_client_secret_json` handles both `installed` and `web` envelopes).
- `sources/gdrive.py` — `GoogleDriveSource` implementing `Source` for read + metadata only. `move_to_trash` and `restore_from_trash` raise `NotImplementedError` for sub-phase 2. Filters trashed items, Google-native docs (`application/vnd.google-apps.*`), and marks shared / owner-not-me items with `is_shared=True`. Composite `etag = f"{cloud_file_id}:{modifiedTime}"`. Google SDK imported lazily so tests never require `googleapiclient`. Tenacity retry on 429/5xx.
- `hash/reconciliation.py` — `reconcile_bucket` with same-algo skip, `cloud_hash_cache` lookup, per-scan download budget (`--max-cloud-download-mb`, default 1000 MB), deferred-bucket recording. Cache-invalidation via etag primary key.
- `store.py` gains `get_cloud_hash` / `put_cloud_hash` / `purge_stale_cloud_hashes` helpers (90-day TTL sweep).
- `cli.py` grows `dc auth add/list/test/remove`, `dc sources list`, `dc scan --sources ... --max-cloud-download-mb ...`. `--sources` and cap flag accepted but wire-through into hash pipeline lands in sub-phase 5.
- Dependencies added to `pyproject.toml`: `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `tenacity`.

Invariants preserved:
- All 158 pre-existing tests pass unchanged.
- `test_no_forbidden_calls.py` still passes. Token / accounts-file deletion uses `send2trash`; atomic writes use `os.replace`; no new `os.remove/unlink` in `src/`.
- `is_read_only_scan=True` tripwire enforced on both `LocalFileSystemSource` (sub-phase 1) and `GoogleDriveSource` (sub-phase 2).
- Token files chmod 0o600, parent dir 0o700, mode verified on read (test proves rejection of 0o644 files).
- Shared cloud files: `is_shared=True` propagated on the FileRecord; scorer wiring lands in sub-phase 5 but the marker is already correct.
- Manifest atomic-write pattern is unchanged; cloud entries reuse the same tmpfile + fsync + replace + parent-fsync pattern from `apply/mover.py`.
- Bundled client id is a compiled-in placeholder — hard-coded credentials-in-code marker (`BUNDLED_GDRIVE_CLIENT_ID_TO_REPLACE`) is the only violation of "no hard-coded credentials" and is called out as pre-release TODO.

Notes for review:
- Path("gdrive:x://foo") collapses `//` to `/`; the string form ends up `gdrive:x:/foo`. That is intentional and does not collide with local paths, but review agents should be aware that the design doc's `://` visual is not literal on disk.
- mypy `--strict` in the current dev env (mypy 2.3.1 with third-party stubs installed) reports the same `unused-ignore` warnings on 5 pre-existing files as on the 2 new files that follow the same `# type: ignore[import-untyped]` pattern. Under the project-pinned mypy `>=1.11.2,<2` in a stub-free env these clear.

**Sixth audit (Code Review pass 6, fork+scaffold, 2026-09-05)** — verdict: **Ready with must-fix follow-ups.** Zero DATA-LOSS, zero invariants weakened. 11 findings.

Category counts: 4 safety (all PLAUSIBLE), 3 correctness (CONFIRMED), 3 simplification (CONFIRMED), 1 test-coverage (CONFIRMED).

Must-fix BEFORE sub-phase 5 (cross-source wiring):
- correctness/CONFIRMED: `_resolve_client_credentials` returns the `BUNDLED_GDRIVE_CLIENT_ID_TO_REPLACE` placeholder with no guard; `dc auth add gdrive` fails with an unhelpful Google error until the constant is registered. Add a `.endswith('_TO_REPLACE')` check with actionable text.
- safety/PLAUSIBLE (would become DATA-LOSS at sub-phase 5): `GoogleDriveSource._item_to_record` uses `first.get('me', True)` — defaults `is_shared` to False when API omits `me`. A shared-with-me file gets marked as owned, violating the `Shared cloud files informational-only` invariant once sub-phase 5 wires scoring. Change default to False.
- safety/PLAUSIBLE (landmine at sub-phase 5): `Path("gdrive:x://foo")` is non-absolute on POSIX; when sub-phase 5 feeds it into `apply/mover.py::_validate_report_paths`, `resolve_for_check` prepends CWD. Need a `is_cloud_path` predicate + source-dispatched validator BEFORE local exclusion rules run on cloud paths. Same architectural gap pass 5 flagged for `restore_from_trash`.

Should-fix in sub-phase 3:
- correctness/CONFIRMED: `--sources` and `--max-cloud-download-mb` on `dc scan` are accepted but silently discarded (`_ = sources; _ = max_cloud_download_mb`). Refuse non-`local` values until sub-phase 5, or emit a warning.
- correctness/CONFIRMED: `dc auth add gdrive` re-run on an existing account_id silently overwrites the token before catching `DuplicateAccountError`. Check registry first; refuse or explicit re-authorise.
- simplification/CONFIRMED: `reconcile_bucket` same-algo shortcut only fires when `shared == 'blake3'`. Two files that both carry `md5:...` still trigger a download. Extend the shortcut to any shared algo.
- safety/PLAUSIBLE: OAuth `state` comparison uses `!=` — replace with `hmac.compare_digest`.
- safety/PLAUSIBLE: `AccountsRegistry.load` never calls `enforce_secure_mode`; a 0o644 accounts.toml is loaded silently. Symmetrise with TokenStore.
- safety/PLAUSIBLE: `TokenStore._ensure_dir` swallows chmod failures. Prefer raising or post-chmod mode-verify.
- simplification/CONFIRMED: dead code `_unused_path_placeholder` + unused `CLOUD_HASH_TRIPLE` in `reconciliation.py`.
- test-coverage/CONFIRMED: `purge_stale_cache` is never invoked from the scan flow. Wire it into `cli.scan` when sub-phase 5 lands (or now) and add a test.

### v0.2 — cloud sources (sub-milestone 1: LocalFileSystemSource refactor)

**Sub-milestone 1 Dev delivered** (2026-09-05): 10 new tests (158 total). Ruff clean; mypy `--strict` clean on new files. Zero-behavior-change refactor: `iter_files` still exported and used unchanged by 10+ tests. `FileRecord` gained 6 trailing defaulted fields (`source_id`, `foreign_hash`, `etag`, `cloud_file_id`, `owner`, `is_shared`) — no existing construction site edited. SQLite schema migrated to v2 with `schema_meta` version marker; `cloud_hash_cache` table created eagerly. `LocalFileSystemSource` wraps `iter_files` / `send2trash` / `apply.undo.local_restore`; `is_read_only_scan=True` default tripwire prevents accidental trash from scan paths.

**Sixth audit (Security pass 5, fork+scaffold, 2026-09-05)** — verdict: **Ready with post-release notes.** Zero DATA-LOSS, zero invariants weakened. 8 findings — 3 safety-PLAUSIBLE, 2 simplification, 2 test-coverage, 1 correctness. Highlights:
- safety/CONFIRMED-latent: `GoogleDriveSource` sets `FileRecord.path = Path("gdrive:personal://drive_path")` — a RELATIVE path. `resolve_for_check` joins to `os.getcwd()`; `validate_not_excluded` mostly passes and `is_within(resolved, roots)` fails unless roots include cwd. Sub-phase 2 doesn't wire cloud entries into `apply` so this is latent. **Sub-phase 5 MUST design report.roots + `validate_not_excluded` to understand a `<source_id>://` scheme or explicitly gate cloud discards through a separate path.** This is the biggest architectural item for sub-phase 5.
- safety/PLAUSIBLE: `AccountsRegistry.load` never calls `enforce_secure_mode`; a mode-0o644 `accounts.toml` (from a manual edit or migration) is silently accepted. `enforce_secure_mode` exists but is unused.
- safety/PLAUSIBLE: `run_localhost_flow` uses `!=` for state comparison instead of `hmac.compare_digest`. Loopback-only attack surface makes it a non-realistic timing side-channel; the scaffold alternatives-check still notes `compare_digest` as the standard.
- correctness/PLAUSIBLE: `_resolve_client_credentials` returns `("BUNDLED_GDRIVE_CLIENT_ID_TO_REPLACE", "")` today; `dc auth add gdrive` hits Google with an invalid client_id and Google's raw "invalid_client" bubbles up. A pre-check in `_resolve_client_credentials` should raise a clear "this build's bundled client id is a placeholder" error until real registration lands.
- simplification/CONFIRMED: `client_secret` is stored in every token file alongside `refresh_token`. Google requires it on refresh so it must live somewhere, but the JSON-per-account layout means the same secret is duplicated across every account token file. A separate `clients.json` (mode 0o600) referenced by token entries would deduplicate.
- simplification/PLAUSIBLE: `GoogleDriveSource._folder_cache` grows unbounded across a scan. On drives with ~100K folders it holds ~100K entries in RAM. `functools.lru_cache` with a size cap of ~10K is proportional.
- test-coverage/CONFIRMED: no test asserts that a symlink whose target has 0o600 mode but non-JSON content is rejected cleanly (currently fails with `JSONDecodeError`, which is fine but should be a specific assertion).
- test-coverage/CONFIRMED: no test locks in the containment behavior of `Path("gdrive:x://foo").resolve()` — if a future Python changes the semantics of colon in Path parts, the latent-cloud-path finding could become active without a red test.

Verified positives:
- `_pkce_pair`: `secrets.token_bytes(48)` + SHA-256 → S256 challenge — correct per RFC 7636.
- Token storage: `mkstemp` creates 0o600 by default, `os.chmod` re-applies, `os.replace` on macOS replaces the directory entry — no world-readable race window. Mode enforced on read via `mode_bits & 0o077`, `TokenPermissionError` raised on violation.
- `_path_for` rejects `/`, `.`, `..` in `account_id`, catching path-traversal via `--label ../../../etc` even though `_default_account_id` does not itself sanitize the label.
- `test_no_forbidden_calls.py` still green over `auth/`, `sources/gdrive.py`, `hash/reconciliation.py`. Token/accounts delete uses `send2trash`. Atomic writes use `os.replace`. No new `os.remove/unlink/shutil.move` in `src/`.
- `is_read_only_scan=True` default on `GoogleDriveSource` raises `PermissionError` before any I/O — parity with `LocalFileSystemSource`.
- OAuth token exchange over HTTPS: Python 3.12 `urllib.request.urlopen` validates TLS by default; no `context=ssl._create_unverified_context()` anywhere.

Regression surface for sub-phase 5:
- The cloud-path validation gap (finding #1) is the load-bearing item. Design must specify: does `report.roots` include synthetic `gdrive:personal://` roots, or does `_validate_report_paths` grow a `source_id`-aware branch, or does `apply` dispatch by `source_id` before path validation runs? Currently unspecified.

**Fifth audit (Code Review pass 5, fork+scaffold, 2026-09-05)** — verdict: **Ready with post-release notes.** Zero DATA-LOSS, zero invariants weakened. 6 findings, all deferrable:
- safety/PLAUSIBLE: `LocalFileSystemSource.restore_from_trash` bypasses the H2/F12/H5 poisoned-manifest guards that `restore_from_manifest` enforces. Must fix BEFORE sub-phase 5 wires the mover to call `Source.restore_from_trash` directly.
- safety/PLAUSIBLE: `test_no_forbidden_calls.py::_SHUTIL_MOVE_ALLOWED_FILES` matches on basename (`"undo.py"`) — any future `undo.py` under any directory would silently be allowed `shutil.move`. Switch to path-relative match (`"apply/undo.py"`).
- simplification/CONFIRMED: `_default_local_trash_fn` duplicates `apply/mover.py::_default_trash_fn`. Extract shared helper before sub-phase 5.
- test-coverage/CONFIRMED: no test for schema migration idempotency after partial-crash re-open.
- simplification/PLAUSIBLE: `ForeignHash = str` alias adds no enforcement — either `NewType` it or drop it.
- simplification/PLAUSIBLE: `LocalFileSystemSource.__init__` eight-param surface will churn as walker gains options; consider `WalkConfig` dataclass.

Category counts: 2 safety (PLAUSIBLE), 3 simplification (1 CONFIRMED / 2 PLAUSIBLE), 1 test-coverage (CONFIRMED). Zero DATA-LOSS, zero invariant-weakening.

**Seventh audit (Code Review pass 7 + Security pass 6, coordinator-direct review, 2026-09-05)** — verdict: **Ready with post-release notes.** Fork agents stalled at 600s watchdog + classifier temporarily unavailable; coordinator performed the review directly by reading key files (`sources/gdrive.py`, `apply/undo.py`, `apply/trash.py`, `cli.py`, `auth/oauth_flow.py`, `auth/accounts.py`, `auth/tokens.py`, `hash/reconciliation.py`, `tests/test_no_forbidden_calls.py`). Verified clean: Part A trash+restore (tripwire order correct, retry via tenacity, `_raise_mapped` covers 401/403/404/429/5xx), B1–B13 all landed correctly. `hmac.compare_digest`, `_SHUTIL_MOVE_ALLOWED_RELPATHS = {"apply/undo.py"}`, `AccountsRegistryPermissionError` + `enforce_secure_mode()` on load, `purge_stale_cloud_hashes` wired at scan startup, shared `validate_restore_paths` prevents H2/F12/H5 bypass via `local_restore`, forward-compat with sub-phase 5 Alt-C dispatch pattern (source_id-based, no Path-string sniffing).

One deferrable finding:
- simplification/PLAUSIBLE: `dc auth add --force` overwrites the token file locally but does NOT call the provider's `/revoke` endpoint on the previous refresh token. Old refresh token stays valid on Google's side until manually pruned or the user rotates via Google Account settings. Hygiene, not DATA-LOSS.

Category counts: 1 simplification (PLAUSIBLE). Zero DATA-LOSS. Zero invariant-weakening. 215/215 tests passing, ruff clean.

**Ninth audit (Code Review pass 9 + Security pass 8, coordinator-direct review, 2026-09-05)** — verdict: **Ready with must-fix follow-ups.** Zero DATA-LOSS, zero invariants weakened. Reviewed sub-milestone 5b (Alt-C source_id dispatch in mover + undo, plus three Part-B Security-pass-7 latent-DATA-LOSS closers on onedrive/gdrive). Verified clean: (a) `_validate_report_paths` dispatches on `m.source_id` only — no Path-string sniffing; cloud Path never resolves (regression-test-locked via patched `resolve_for_check`). (b) `plan_moves` filters `source_id != "local"` so cloud entries never reach `_verify_unchanged` or `tf(p)`. (c) Undo H5 archive-member scan scoped to LOCAL entries. (d) `_ONEDRIVE_ID_RE` and `_GDRIVE_ID_RE` block `root:/../foo`, `..%2f`, and empty strings; URL-encoded via `urllib.parse.quote(id, safe='')`. (e) `follow_redirects=False` on primary Graph client; `_BearerAuth.auth_flow` strips Authorization on non-Graph host; `read_bytes` uses `_build_unauth_http_client()` for 302 target. (f) `@odata.nextLink` validated against `GRAPH_ROOT + "/"`. (g) `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py`.

Category counts: 1 correctness (CONFIRMED), 3 simplification (CONFIRMED), 2 safety (PLAUSIBLE), 1 test-coverage (CONFIRMED). Zero DATA-LOSS. Zero invariant-weakening.

Must-fix in same release (sub-phase 5b post-release polish or rolled into 5c):
- correctness/CONFIRMED: `cli.py::apply` and `cli.py::undo` do NOT surface `result["cloud_deferred"]`. On a mixed report `dc apply --commit` prints "Moved N file(s) to Trash" counting only local moves; user may believe cloud discards succeeded. Adds a one-line warning: `"Deferred N cloud discard(s) to sub-phase 5c."`.
- simplification/CONFIRMED: `paths.validate_cloud_entry` and `paths.validate_cloud_manifest_entry` each call `registry.load()` per-entry. On a manifest with 100 cloud rows that is 100 disk reads + 100 `enforce_secure_mode` stat calls. Compute `authorized` set once at the top of `_validate_report_paths` / `restore_from_manifest` and pass through.
- simplification/CONFIRMED: `sources/gdrive.py::_quote_cloud_file_id` is defined but never called — googleapiclient handles URL encoding internally via `fileId=` param. Either delete the helper or wire it in for defense-in-depth parity with onedrive.

Deferrable (post-release notes for 5c/5d):
- safety/PLAUSIBLE: `onedrive.py:59` — `_ONEDRIVE_ID_RE = ^[A-Za-z0-9!]{1,120}$` accepts single-char ids; the docstring's claim that it is "tighter than the sub-phase 5b validator" is wrong (paths.py enforces `{20,}` minimum). Tighten source-side minimum to 20 or fix the comment.
- safety/PLAUSIBLE: `onedrive.py:488` — 302 Location header used verbatim with an unauth client. No allowlist that Location points to a Microsoft-controlled origin (e.g. `*.blob.core.windows.net`). Bearer is safe (unauth client) but a compromised Graph response could redirect `read_bytes` to an attacker-controlled origin, corrupting reconcile-time byte comparisons. Low realism; strict origin allowlist trivial to add.
- test-coverage/CONFIRMED: `test_bearer_token_stripped_on_cross_origin_redirect` monkeypatches `_build_unauth_http_client` — the fake CDN client is a bare stub, so a regression that reintroduced Authorization on the primary client's redirect flow would not be caught. Add a direct unit test that constructs the REAL `_BearerAuth` with a mocked non-Graph request and asserts Authorization is popped.

**Eighth audit (Code Review pass 8, coordinator-direct review, 2026-09-05)** — verdict: **Ready with must-fix follow-ups.** Zero DATA-LOSS, zero invariants weakened. Reviewed sub-milestones 4 (OneDriveSource) + 5a (schema surgery) end-to-end. Verified clean: `is_read_only_scan=True` tripwire fires before any HTTP call; `deleted` presence checked BEFORE `remoteItem` so a deleted-and-shared item is filtered (not surfaced); `file.hashes.sha256Hash` missing is a clean skip via `log.debug`; `foreign_hash` is `sha256:` prefixed with `sha256.lower()` (case normalised); DELETE → 204 handled via `raise_for_status`; POST /restore actionable-error path triggers on `status == 501 OR error.code == "notSupported"`; `_raise_mapped` covers 401/403/404/429/5xx; `@odata.nextLink` pagination follows correctly (0-item page with nextLink still advances); schema round-trip preserves `source_id="local"` explicitly on every member and manifest entry; v0.1.1 report loads with default `source_id="local"`.

Category counts: 1 correctness (CONFIRMED), 3 simplification (2 CONFIRMED / 1 PLAUSIBLE), 1 safety (PLAUSIBLE), 1 test-coverage (PLAUSIBLE). Zero DATA-LOSS. Zero invariant-weakening.

Must-fix BEFORE sub-milestone 5b (mover dispatch):
- correctness/CONFIRMED: `sources/onedrive.py::_is_retryable_http_error` only checks `httpx.HTTPStatusError` — transient `httpx.TimeoutException` / `httpx.ConnectError` / `httpx.NetworkError` are NOT retried. gdrive relies on google-api-python-client's built-in retry; raw httpx needs explicit coverage.

Should-fix same release:
- safety/PLAUSIBLE: `OneDriveSource.move_to_trash` accepts any FileRecord without checking `record.source_id == self.id`. Asymmetric with `restore_from_trash` which does the check. Adding the guard is defense-in-depth for sub-phase 5b dispatch.
- simplification/CONFIRMED: `apply/mover.py::_flush_manifest` is O(N²) — every successful move validates every existing entry via `ManifestEntry.model_validate(e) for e in entries` then dumps them all. Fix: keep validated `ManifestEntry` instances in the entries list (or skip re-validation on flush).
- simplification/CONFIRMED: `msal>=1.31.0,<2` is in `pyproject.toml` core deps but not imported anywhere in `src/` or `tests/`. Aspirational for sub-phase 5 token refresh — move to sub-phase 5 pyproject changes or add a comment linking to the intended use.

Deferrable:
- simplification/PLAUSIBLE: 501 is classified retryable by `_is_retryable_status`, so `OneDrivePersonal /restore` burns ~31 s of exponential backoff (5 attempts × 1/2/4/8/16 s) before finally emitting the actionable "restore via web UI" error. Excluding 501 from retry (or short-circuiting when body carries `notSupported`) shortens the delay to sub-second.
- test-coverage/PLAUSIBLE: design doc §11.6 explicitly calls for a `test_cloud_path_repr` locking in the `Path("<source_id>://foo")` → `<source_id>:/foo` collapse behavior. Absent — a future Python change to colon-in-path semantics could silently reactivate the pass-6 landmine.

## Deferred to v0.1.2

- Bundle filename Unicode NFC/NFD normalization (HFS+ ↔ APFS migration case)
- Singleton hash markers `"singleton-by-size:<size>:<path>"` AND `"singleton-by-partial:<partial>:<path>"` currently leak local path in JSON — replace both with an opaque token (pass-4 finding)
- Undo's `_src_is_in_trash` duplicates `paths.is_inside_any_trash`; consolidate (pass-4 finding)
- Add `test_wholly_duplicated_excludes_archive_with_nested_encrypted_member` (pass-4 finding)
- Add end-to-end test that H7 partial-unique singleton reaches `Report.singletons` (pass-4 finding)
- H4 nested-archive spool + hash is two BLAKE3 passes; single-pass optimisation possible (pass-4 finding)
- `max_workers` config exposed but not wired; either wire it or make CLI print a "single-threaded in v0.1.1" notice
