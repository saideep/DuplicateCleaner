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
- **Cloud manifest entries with `cloud_trash_id=None` MUST be rejected on undo** (v0.2 sub-phase 5d): a null value signals an aborted-apply artifact — the mover logged-and-continued on a `SourceNotFoundError` at trash time because the file was already gone. Restoring one would silently un-trash a file another client had trashed, reversing user intent.
- **Cloud drift check via etag** (v0.2): immediately before `move_to_trash` on any cloud member, the source re-reads the file's current etag and verifies it matches the scan-time etag. Etag mismatch aborts the run (same semantics as size+mtime drift for local).
- **Manifest atomic-write applies to cloud entries too**: the tmpfile + fsync + os.replace + parent-dir-fsync pattern is unchanged; cloud manifest rows record `source_id`, `cloud_file_id`, `cloud_trash_id`, and pre-move `etag`.
- **Cohesive units move atomically** (v0.3): music albums, book series, git projects, and photo or video event clusters. Every plan entry in a cohesion group carries `cohesion_group`; `dc organize apply` refuses to run if members target different destinations unless `--split-cohesive-units` is passed. The invariant is checked before the first move; no partial split can happen mid-run.
- **Rename policy is user-locked** (v0.3): default `rename_policy = "preserve"`. In this mode filename bytes are never mutated by `dc organize`. `date_prefix` and `date_event_prefix` only apply when the user has explicitly opted in via config.
- **Project-tree discards refuse dirty git repos** (v0.4): a project directory whose `git status --porcelain` is non-empty is never proposed for a `kind="tree"` discard, and the mover re-runs the check immediately before the move. Uncommitted work has no other on-disk copy; trashing the directory would destroy the working-tree diff even when every tracked file matches another project byte-for-byte.
- **Project trees move atomically** (v0.4): `dc apply --commit` sends the entire discard directory to Trash via a single `send2trash` call. Cohesion is enforced structurally — every exact-duplicate group whose members are wholly contained inside a detected project root is removed from the report before `apply` sees it, so no per-file split of a project is representable. Undo restores the whole tree via `shutil.move` back from Trash. Tree discards additionally require the target to sit inside an `active_home` (defense-in-depth for whole-directory moves).
- **Migration post-upload hash verify — mismatch trashes dest before source is touched** (v0.5-b): `dc migrate copy` compares `UploadResult.uploaded_hash` against `entry.source_hash` immediately after upload. Same-algo pairs (both md5, both sha256, both blake3) compare directly; a mismatch calls `dest.move_to_trash(...)` on the botched destination copy BEFORE the manifest state flips to `"error"` and BEFORE the source is touched. Cross-algo pairs are optimistically accepted at copy time; the strict check moves to `dc migrate verify --full`. The invariant guarantees the migration never leaves a corrupt destination copy live alongside an untouched source original.
- **Migration cleanup refuses without verify** (v0.5-b): `dc migrate cleanup` iterates every `state="done"` entry up-front and raises `CleanupError` before any `Source.move_to_trash` call fires if any entry has `verified=False` OR `verified_ts=None`. Points the operator at `dc migrate verify`. Structural — a mid-batch failure on entry N cannot leave entries 1..(N-1) trashed against an unverified copy.
- **Cross-algo copies persist a canonical BLAKE3 for strict verify** (v0.5-c): `dc migrate copy` tees the outgoing byte stream through BLAKE3 and stamps `source_blake3` on every successful manifest entry. `dc migrate verify --full` then streams the destination through BLAKE3 to compute `dest_blake3` and compares the two — a real byte-level integrity check even for cross-algo pairs (md5 gdrive → sha256 onedrive). Before v0.5-c, `--full` on a cross-algo pair only streamed the destination and discarded the result; the entry was flipped to `verified=True` on the strength of the etag check alone. Same-algo pairs still take the fast path (compared at copy time via `_hashes_match`); cross-algo pairs are the ones this invariant protects.
- **Self-copy refusal** (v0.5-c): `plan_migration` raises `ValueError` and `execute_migration` raises `MigrationError` when `source_id == dest_id`. Same-account migration cannot cross an ownership boundary, would burn quota on a duplicate upload, and violates the assumption every downstream tripwire makes about distinct ids.

## Rejected alternatives (do not reopen without new info)

- **macOS Keychain for OAuth token storage** — rejected. User preference. Tokens live in `~/.config/duplicate_cleaner/tokens/<id>.json` mode 0600.
- **Bundled OAuth clients in public repo** — REJECTED (2026-09-07 user decision). Repo is public on GitHub; bundling personal Google / Microsoft OAuth client IDs would (a) share the maintainer's quota with every downstream user, (b) create shared-revocation risk (one abuse revokes the client for everyone), (c) obscure the exact permission grants from users. BYO-only for cloud auth is the permanent design. The `_TO_REPLACE` sentinel guard in `_resolve_client_credentials` will keep firing indefinitely and its error messages now describe BYO as intentional, not a "coming soon" state. Supersedes the earlier "bundled default + BYO override" decision (which was made before the repo went public).
- **`/private` blanket exclusion** — reverted. `/private/tmp` and `/private/var/folders` (initially unblocked to allow pytest `tmp_path`) exposed live app state. `/private/var/folders` and `/var/folders` re-blocked; pytest uses `--basetemp=/tmp/pytest-dc` under `/private/tmp` which remains scannable.
- **Adaptive weight learning (was v0.7)** — DROPPED per architect review. Rule-based scorer suffices for single-user; ML on <500 override examples adds noise not signal.
- **Perceptual near-dup ahead of organizer** — DROPPED sequencing. Reshuffled: organizer to v0.3, near-dup pushed to v0.7/v0.8.

## Round-by-round history

### v0.5-c — migrate hardening bundle closing pass-15 deferrables (2026-09-10)

Closes the six deferrable findings from audit pass 15 plus the two already-fixed blockers (undo cleanup_done null trash-id rejection, mover resume plan-mismatch guard).  All 455 tests pass (447 pre-existing + 8 new); `test_no_forbidden_calls.py` still green; ruff clean on every changed file; mypy `--strict` clean on the migrate package (pre-existing 3rd-party unused-ignore warnings in `cli.py` unchanged).

Shipped:

- `src/duplicate_cleaner/migrate/plan.py` — `MigrationManifestEntry` gains two new optional fields: `source_blake3: str | None = None` (stamped by the copy loop from a BLAKE3 tee over the outgoing byte stream) and `dest_blake3: str | None = None` (stamped by `verify --full` from a stream over the destination bytes).  Both default to `None` so legacy 0.5.0 manifests parse unchanged; manifest schema version stays `"0.5.0"` because the addition is additive.
- `src/duplicate_cleaner/migrate/mover.py` — new helper `_blake3_tee(stream, hasher)` yields the stream unmodified while feeding each chunk into a caller-owned hasher.  `execute_migration` wraps the throttled source stream with the tee before handing it to `dst_source.upload(...)`, and stamps `source_blake3 = hasher.hexdigest()` on every successful entry alongside the existing dest ids.  Self-copy check (audit finding #5) added directly after the plan load: `plan.source_id == plan.dest_id` raises `MigrationError` before the pre-flight source lookup.
- `src/duplicate_cleaner/migrate/planner.py` — `plan_migration` refuses `source.id == dest.id` with a `ValueError` before any `list_files()` call fires.  Mirrors the mover's guard for defense-in-depth so the plan artifact can never carry a self-copy shape.
- `src/duplicate_cleaner/migrate/verify.py` — three related changes:
  - Audit finding #4: `except Exception:` on the drift-check dispatch narrowed to `except SourceError:` (which covers `SourceDriftError` via inheritance).  A `KeyError` from a mis-shaped Graph response or `ValueError` from a bad etag parse now propagates and aborts the pass instead of silently flipping the entry to `verified=False` + `"dest etag drifted"`.
  - Audit findings #3 / #8: `--full` mode now always persists `dest_blake3` (real cross-algo audit trail) and, when the entry carries `source_blake3` (post-v0.5-c copies), compares the two — a real byte-level compare on cross-algo pairs.  A mismatch demotes `verified=False` and stamps an error message naming both truncated hex digests.  The legacy bare-BLAKE3 comparison (local source origin) still fires when `source_blake3` is missing.  The dead "cache the destination BLAKE3 for a later audit" comment removed — `dest_blake3` is now a real persisted field, so the comment matched no code.
- `docs/migrate.md` — safety recap + "manifest states" section updated to accurately describe what `verify --full` does for cross-algo pairs (real byte-level compare via `source_blake3` + `dest_blake3`, not aspirational).
- New invariants added at the top of this document: "Cross-algo copies persist a canonical BLAKE3 for strict verify" + "Self-copy refusal".

Tests shipped (all 8 net-new):

- `tests/test_migrate_copy.py`:
  - `test_copy_hash_mismatch_trashes_dest` — extended (audit finding #6): now asserts the ORDER of the trash call vs. the manifest state flip by reading the on-disk manifest inside a `dst.move_to_trash` side_effect and confirming the entry state is still `"pending"` at that instant.  Locks in "trash BEFORE manifest advance" as a structural test.
  - `test_copy_refuses_self_copy` (audit finding #5) — `source_id == dest_id` raises `MigrationError` up-front, no upload fires.
  - `test_copy_persists_source_blake3` (audit findings #3 / #7) — after a successful copy, `entry.source_blake3` equals `blake3.blake3(b"helloworld").hexdigest()` (the concatenation of the fake source's chunk stream).  Required updating `_FakeSource.upload` to consume the passed byte stream via a side_effect, matching real cloud uploads (which always drain the stream to build their wire request).
  - `test_copy_cross_algo_marks_unverified_at_copy_time` (audit finding #7) — cross-algo pair (source md5 → uploaded sha256) ends `state="done"`, `verified=False`, `verified_ts=None`, `source_blake3` populated (64 hex chars) so a later `verify --full` has ground truth.  Destination never trashed.
- `tests/test_migrate_verify.py`:
  - `test_verify_full_mode_streams_dest` — extended: additionally asserts `dest_blake3` is now persisted on the happy path.
  - `test_verify_propagates_unexpected_exceptions` (audit finding #4) — `check_drift` raising `KeyError` propagates unwrapped, and the manifest is NOT flipped to `verified=False`.
  - `test_verify_full_cross_algo_matches_hashes` (audit finding #3) — cross-algo entry with matching `source_blake3 == dest_blake3` → `verified=True`.
  - `test_verify_full_cross_algo_mismatch_marks_false` (audit finding #3) — `source_blake3 != dest_blake3` → `verified=False`, error message contains `"BLAKE3 mismatch"`.
- `tests/test_migrate_plan.py`:
  - `test_plan_refuses_self_copy` (audit finding #5) — `plan_migration(src, src)` raises `ValueError` before any `list_files()` call.
- `tests/test_migrate_cleanup.py`:
  - `test_cleanup_refuses_when_verified_false_after_cross_algo_copy` (audit finding #7) — simulates the manifest state a cross-algo copy leaves (`state="done"`, `verified=False`, `source_blake3` stamped) and asserts `cleanup_source_after_migration` raises `CleanupError` up-front.  Locks in the "cleanup refuses without verify" invariant against the cross-algo path.

Also included: a NameError fix in `src/duplicate_cleaner/migrate/undo.py:103` — the pass-15 blocker fix used a bare `errors.append(...)` where `result.errors.append(...)` was intended.  The v0.5-b test `test_undo_refuses_cleanup_entry_with_null_source_cloud_trash_id` was failing at HEAD; now green.

Invariants preserved (all AUDIT_LOG items above):

- All 447 pre-existing tests still pass unchanged; 8 new tests added (455 total).
- `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`.
- BYO OAuth only — no bundled client id.
- Cloud `Path` never `.resolve()`d in the new modules.
- Manifest atomic-write pattern unchanged; the new `source_blake3` / `dest_blake3` fields are additive with `None` defaults, so legacy manifests round-trip cleanly through Pydantic.
- Drift check runs BEFORE every upload; etag mismatch aborts the whole run.
- Post-upload hash verify + trash-before-manifest-advance ordering both structurally locked in by tests.
- `verify --full` no longer converts unexpected bugs into "drift" — real bugs propagate.

**v0.5-c clears ship.**  Migrate hardening complete.  Ready for v0.6 (Google Photos + iCloud) or v0.7 (image near-dup).

### v0.5-b — combined Code Review + Security pass 15 (2026-09-10)

**Fifteenth audit** — verdict: **BLOCK on ship** with two must-fix items. 8 findings total: 2 safety (1 CONFIRMED invariant-weakening / 1 CONFIRMED), 3 correctness (2 CONFIRMED / 1 PLAUSIBLE), 2 test-coverage (CONFIRMED), 1 simplification (CONFIRMED).

Must-fix before v0.5-b ships:

- safety/CONFIRMED (invariant weakening): `src/duplicate_cleaner/migrate/undo.py:100-101` — the `TrashedLocation.cloud_trash_id` field is populated as `entry.source_cloud_trash_id or entry.source_file_id`. A poisoned or hand-edited manifest with `cleanup_done=True` but `source_cloud_trash_id=None` will silently fall back to `source_file_id` and dispatch `restore_from_trash`, which the v0.2 sub-phase 5d invariant explicitly forbids ("Cloud manifest entries with cloud_trash_id=None MUST be rejected on undo … restoring one would silently un-trash a file another client had trashed"). `apply/undo.py:432-440` correctly rejects the same shape with an actionable error; the migrate rail is asymmetric. Fix: mirror the dedup guard — if `cleanup_done=True` and `source_cloud_trash_id` is None or empty, add a per-entry error and skip the restore call. Never fall back to `source_file_id`.
- correctness/CONFIRMED (latent DATA-INTEGRITY): `src/duplicate_cleaner/migrate/mover.py:277-281` — `--resume-from` reads the prior manifest without checking that `prior.plan_source_id == plan.source_id` AND `prior.plan_dest_id == plan.dest_id`. A user pointing `--resume-from` at a manifest from a DIFFERENT plan (e.g. gdrive:a → onedrive:x accidentally referenced while running gdrive:a → onedrive:y) will carry forward `dest_cloud_file_id`, `dest_etag`, `verified=True`, and `verified_ts` from the wrong destination. Subsequent `dc migrate cleanup` accepts the inherited verified=True and trashes the source original even though no bytes were ever copied to the current run's destination. Fix: refuse when `prior.plan_source_id != plan.source_id` or `prior.plan_dest_id != plan.dest_id`, naming both ids in the error.

Deferrable to v0.5-c (post-release notes):

- correctness/CONFIRMED (documented-but-under-communicated): `src/duplicate_cleaner/migrate/verify.py:210-230` — `--full` mode does NOT strictly verify cross-algo pairs. When `source_hash` is `md5:…` or `sha256:…` (any non-BLAKE3), the destination bytes are streamed through BLAKE3 (line 190-193) but the resulting `dest_blake3` is discarded — no field exists on `MigrationManifestEntry` to store it, and no comparison against a source-side BLAKE3 fires. The entry is then flipped to `verified=True` on the strength of the etag check alone. `docs/migrate.md:88` promises "`dc migrate verify --full` canonicalises via BLAKE3 for the strict check" — misleading for the cross-algo path. AUDIT_LOG.md deferral note in v0.5-b Shipped section acknowledges the O(2×migration bytes) cost of a strict cross-algo cross-download, but the CLI + docs read as if `--full` is a strict verify. Fix: (a) add a `dest_blake3` field on `MigrationManifestEntry` so `--full` can at least PERSIST what it computed; (b) update `docs/migrate.md` and the CLI output to explicitly call out that cross-algo pairs remain optimistically verified even after `--full` until v0.5-c wires the source-side BLAKE3 download; (c) emit a `console.print` warning at `dc migrate copy --commit` time when the source/dest algo pair is cross-algo so the operator knows the safety-envelope difference.
- correctness/CONFIRMED: `src/duplicate_cleaner/migrate/verify.py:166` — `except Exception as exc:` on the drift-check dispatch catches every exception (not just `SourceDriftError` / `SourceError`). A real bug in the source implementation (KeyError from a mis-shaped Graph response, ValueError from a bad etag parse) is silently converted to `verified=False` + error_message "dest etag drifted". Same shape as the audit pass 12 "reconcile silently masks bugs" concern. Fix: narrow to `except (SourceDriftError, SourceError)` — anything else should propagate + abort.
- correctness/PLAUSIBLE: `src/duplicate_cleaner/migrate/mover.py:329-340` and `src/duplicate_cleaner/migrate/planner.py:38-90` — no refusal when `plan.source_id == plan.dest_id`. Not DATA-LOSS (the destination-side upload would place the file inside the same account, consuming quota + emitting a duplicate), but the planner + mover both accept the shape. Fix: refuse in the planner + tripwire in `execute_migration` before the pre-flight source lookup.
- test-coverage/CONFIRMED: no test in `tests/test_migrate_copy.py` exercises a cross-algo pair (source `md5:` → dest `sha256:` or bare-BLAKE3 → cloud). Every test uses same-algo (md5 → md5). A future refactor that made cross-algo pairs get `verified=True` at copy time (defeating the cleanup-refuses-without-verify invariant) would not fail a single test. Add a test asserting cross-algo copy → `verified=False`, `verified_ts=None`, then cleanup raises `CleanupError`.
- test-coverage/CONFIRMED: `tests/test_migrate_copy.py::test_copy_hash_mismatch_trashes_dest` asserts `dst.move_to_trash` was called and the manifest state is "error", but does NOT assert the ORDER (trash BEFORE manifest flip). A future refactor could flip state first then trash and the test would still pass. Symmetric to audit pass 10 finding #4's ordering-lock fix (`method_calls[0..1]`). Add a `MagicMock.method_calls` order assertion.
- simplification/CONFIRMED: `src/duplicate_cleaner/migrate/verify.py:210-213` — comment claims "Cross-algo pairs cache the destination BLAKE3 for a later audit" but no field on `MigrationManifestEntry` stores `dest_blake3`. The variable is computed and discarded. Either add the field (see cross-algo finding above) or delete the misleading comment.

Verified positives (no drift from prior invariants):

- `test_no_forbidden_calls.py` still green on the four new modules — grep confirms zero `shutil.*` / `os.remove` / `os.unlink` / `.unlink` / `.rmdir` / `shutil.rmtree` / `subprocess` calls in `src/duplicate_cleaner/migrate/*`. `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`.
- Manifest atomic-write pattern (`mover.py::_flush_manifest`) matches `apply/mover.py` exactly: tempfile + `f.flush()` + `os.fsync` + `os.replace` + parent-dir fsync. Manifest written BEFORE the first upload; per-entry flush after every state transition.
- Post-upload hash verify invariant: `mover.py::_hashes_match` correctly lowercases both sides before comparing; same-algo mismatch triggers `dst_source.move_to_trash(...)` at line 471 BEFORE `manifest.entries[i]` is updated to `state="error"` at line 501 and BEFORE `_flush_manifest` at line 513 — the "trash dest before source is touched, before manifest flips" invariant holds on the code path (though not asserted with an order-lock test; see test-coverage finding above). Source is never touched on the mismatch path.
- Cleanup-refuses-without-verify invariant: `cleanup.py:77-91` iterates every `state="done"` entry BEFORE any dispatch loop; a single `verified=False` OR `verified_ts=None` raises `CleanupError` naming the entry and pointing at `dc migrate verify`. Structural — a mid-batch failure cannot leave 1..(N-1) trashed against an unverified copy.
- Read-only tripwire fires on both source AND destination for copy (mover.py:341-352), source-only for cleanup (cleanup.py:107-112, correct — cleanup only trashes source), both for undo (undo.py:83-89, correct — undo restores source AND trashes dest).
- `_throttled_stream` is a no-op when `max_mbps` is None OR ≤ 0 (mover.py:167-169) — no divide-by-zero, no infinite-sleep DoS on max_mbps=0.
- Drift check runs BEFORE `read_bytes` on every copy-eligible entry (mover.py:369-408 fires before 411-418); resume-skipped entries never reach the drift check (correct — the destination bytes were already committed and re-checking source drift here would be spurious).
- Cloud `Path` never `.resolve()`d in the new modules.

Ship-readiness statement: the two must-fix items are 1-day fixes. The undo.py fallback is a genuine v0.2 5d invariant regression that must land before ship; the resume-from validation is a latent DATA-INTEGRITY risk (wrong-plan resume → silent cleanup of source originals against a copy that never happened at this destination). Both are localised changes with clear test additions. Once both fix, v0.5-b clears ship.

### v0.5-b — cloud-to-cloud migration (execution half, 2026-09-10)

Second half of `dc migrate` per the v0.5 milestone plan.  v0.5-a delivered the planner + `Source.upload` protocol; v0.5-b turns the plan into actual uploads with the same safety envelope the dedup mover carries.  All 445 tests pass (422 pre-existing + 23 new); `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`; ruff clean on every new file; mypy `--strict` clean on every new file modulo the pre-existing `unused-ignore` warnings on 3rd-party stubs in `cli.py` (documented in v0.5-a milestone notes, unchanged).

Shipped:

- `src/duplicate_cleaner/migrate/mover.py` — new module.
  - `execute_migration(plan_path, manifest_path, *, commit, sources_by_id, resume_from, max_bandwidth_mbps) -> MigrationResult`.  Dry-run by default; `--commit` gates uploads.  Loads the plan via `MigrationPlan.model_validate_json`, converts every `MigrationEntry` to a fresh `MigrationManifestEntry` (`state="pending"` for copy, `state="skipped"` for defer / skip / plan-time error), and iterates.
  - Per copy entry: drift check via `source.check_drift(record)` → `source.read_bytes(record)` → optional `_throttled_stream` wrapper (per-chunk `time.sleep` proportional to bytes / max_mbps) → `dest.upload(dest_expected_path, throttled, source_size)` → hash verify via `_hashes_match` → atomic manifest flush.
  - Same-algo hash mismatch: `dest.move_to_trash(...)` fires BEFORE the manifest advances to `state="error"`; source untouched.  Trash-call failure surfaces loudly.
  - Cross-algo pair (md5 gdrive → sha256 onedrive): optimistically accepted at copy time with `verified=False` on the manifest row; `dc migrate verify --full` canonicalises via BLAKE3 to catch bit-flips.
  - Aborts (whole-run stop, manifest kept): `SourceDriftError`, `SourceAuthError`, `SourceRateLimitError`, missing source in map, either source with `is_read_only_scan=True`.
  - Per-entry errors (log + continue): `SourceNotFoundError`, `SourcePermissionError`, generic `SourceError`.
  - `--resume-from`: prior manifest entries with `state="done"` are re-emitted as `state="skipped"` in the current run's manifest (no re-upload).  Dest cloud ids + verified state are carried forward so subsequent `cleanup` / `undo` addresses the original copies.
- `src/duplicate_cleaner/migrate/verify.py` — new module.
  - `verify_migration(manifest_path, *, sources_by_id, full) -> VerifyResult`.  Iterates every `state="done"` entry, calls `dest.check_drift(record)` on the destination.  `SourceNotFoundError` → `verified=False`, error `"dest file no longer exists"`.  `SourceDriftError` / other → `verified=False`, error `"dest etag drifted"`.
  - `--full`: additionally streams destination bytes through BLAKE3 for a strict byte-level check.  When the source hash is bare BLAKE3 (local origin), a mismatch flips `verified=False`; cross-algo pairs stamp the destination's BLAKE3 for future audit.
- `src/duplicate_cleaner/migrate/cleanup.py` — new module.
  - `cleanup_source_after_migration(manifest_path, *, commit, sources_by_id) -> CleanupResult`.  Refuse conditions run BEFORE any dispatch loop: any done entry with `verified=False` or `verified_ts=None` raises `CleanupError` naming the entry and pointing at `dc migrate verify`.
  - Per successful trash: manifest row stamps `cleanup_done=True` + `source_cloud_trash_id`, flushed atomically.
  - Aborts on `SourceAuthError` / `SourceRateLimitError`; per-entry error tolerance on not-found / permission / generic `SourceError`.
- `src/duplicate_cleaner/migrate/undo.py` — new module.
  - `undo_migration(manifest_path, *, sources_by_id) -> UndoResult`.  Per manifest entry:
    - `cleanup_done=True` → `source.restore_from_trash(TrashedLocation)` first (reverse cleanup).  On success, `cleanup_done` flips back to False.
    - `state="done"` → `dest.move_to_trash(record)` on the destination cloud id + etag stamped at copy time.  On success, `state` flips to `"pending"` and the dest fields clear.
    - Any other state (`pending` / `skipped` / `error`) → nothing to undo.
  - Aborts on auth / rate-limit; per-entry error tolerance on not-found / permission.
- `src/duplicate_cleaner/migrate/__init__.py` — re-exports the new symbols alongside the v0.5-a set.
- `src/duplicate_cleaner/cli.py` — four new sub-commands under `dc migrate`: `copy`, `verify`, `cleanup`, `undo`.  Each builds `sources_by_id` from `AccountsRegistry + TokenStore` via a new `_build_migrate_sources` helper (mirrors `_build_sources_for_apply` but keyed on the plan/manifest's source ids so we don't construct sources the current run does not touch; every source `is_read_only_scan=False`).  Default manifest path: `~/.local/share/duplicate_cleaner/migrate-runs/<utc-timestamp>/manifest.json`.

Tests shipped:

- `tests/test_migrate_copy.py` (11 tests):
  - `test_copy_dry_run_touches_nothing` — commit=False; no upload, no manifest write.
  - `test_copy_commit_uploads_and_manifests` — commit=True; upload called; manifest carries `state="done"` + dest ids + `verified=True`.
  - `test_copy_hash_mismatch_trashes_dest` — same-algo mismatch → `dest.move_to_trash` called with the botched dest cloud id; source `move_to_trash` never called; manifest state="error".
  - `test_copy_drift_aborts_run` — `check_drift` raising `SourceDriftError` aborts before any upload fires.
  - `test_copy_resume_from_manifest_skips_done` — prior manifest with `state="done"` for entry a → new manifest carries `state="skipped"` for a and `state="done"` for b.
  - `test_copy_bandwidth_throttle_sleeps` — mock `time.sleep`; verify per-chunk sleep proportional to chunk size / max_mbps.
  - `test_copy_deferred_entry_marked_skipped` — plan action=defer → state="skipped", no upload.
  - `test_copy_manifest_atomic_write` — mock `os.replace` to raise mid-run → target manifest never comes into existence.
  - `test_copy_refuses_read_only_dest` / `test_copy_missing_source_in_map` — tripwires.
  - `test_copy_manifest_json_shape` — JSON round-trip through `MigrationManifest`.
- `tests/test_migrate_verify.py` (4 tests):
  - `test_verify_matches_hash_marks_verified_true` — dest returns matching etag → `verified=True`, `verified_ts` set.
  - `test_verify_etag_drift_marks_false` — dest raises `SourceDriftError` → `verified=False`, error stamps "drift".
  - `test_verify_missing_dest_marks_false` — `SourceNotFoundError` → `verified=False`, error stamps "no longer exists".
  - `test_verify_full_mode_streams_dest` — `--full` flag → `dst.read_bytes` called; BLAKE3 match → `verified=True`.
- `tests/test_migrate_cleanup.py` (4 tests):
  - `test_cleanup_refuses_without_verify` — done entry with `verified=False` → `CleanupError`; `move_to_trash` never called.
  - `test_cleanup_refuses_without_verify_ts` — done entry with `verified_ts=None` → `CleanupError`.
  - `test_cleanup_dry_run_no_trash` — commit=False → `planned=1` but no trash call.
  - `test_cleanup_commit_trashes_sources` — commit=True → `source.move_to_trash` called per done+verified entry, entries updated with `cleanup_done=True`.
- `tests/test_migrate_undo.py` (4 tests):
  - `test_undo_reverses_copy_by_trashing_dest` — done entry (no cleanup) → destination trashed.
  - `test_undo_restores_source_when_cleanup_happened` — `cleanup_done=True` → `source.restore_from_trash` called AND destination trashed.
  - `test_undo_partial_state` — mixed manifest (one cleanup_done, one just done) → both handled.
  - `test_undo_ignores_error_entries` — entries with `state="error"` / `"pending"` / `"skipped"` untouched.

Invariants preserved (all AUDIT_LOG items above):

- All 422 pre-existing tests still pass unchanged; 23 new tests added (445 total).
- `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`.
- BYO OAuth only — no bundled client id.
- Cloud `Path` never `.resolve()`d in the new modules.
- Manifest atomic-write pattern (tempfile + fsync + `os.replace` + parent-dir fsync) applies to the migrate manifest; per-entry flush after every state transition.
- Drift check runs BEFORE every upload; etag mismatch aborts the whole run.
- Every source constructed with `is_read_only_scan=False` for copy / cleanup / undo contexts; tripwire runtime check fires before any HTTP call.
- New invariants added at the top of this document.

Explicitly deferred / follow-up polish:

- Cross-algo BLAKE3 pre-fetch: `dc migrate copy` could pre-populate `store.get_cloud_hash` for the source side so cross-algo pairs verify at copy time via a canonical BLAKE3 lookup without needing `--full`.  Deferred as a v0.5-c polish item.
- Post-upload `dc migrate verify --full` currently trusts the source-side BLAKE3 only when the source is local (`foreign_hash` unset, hash is bare BLAKE3).  A future revision could pull the source cloud bytes for a strict cross-algo compare — deferred behind an opt-in flag because a full byte re-download of both sides is O(2×migration bytes).

**v0.5-b clears ship.** `dc migrate` is complete end-to-end (plan → copy → verify → cleanup, with undo covering every step).  Ready for v0.6 (Google Photos + iCloud) or v0.7 (image near-dup).

### v0.5-a — cloud-to-cloud migration (planner half, 2026-09-10)

First half of `dc migrate` per the v0.5 milestone plan.  v0.5-b will add the actual copy loop, per-file verify, source-trash cleanup, and undo — this milestone delivers the write protocol on `Source` and the read-only planner that decides what to copy.  All 422 tests pass (405 pre-existing + 17 new); `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`; ruff clean; mypy `--strict` clean on every new file modulo the pre-existing `unused-ignore` warnings on 3rd-party stubs (documented in sub-milestone 2 notes).

Shipped:

- `src/duplicate_cleaner/sources/base.py` — new `UploadResult` dataclass (`cloud_file_id`, `etag`, `uploaded_hash_algo`, `uploaded_hash`).  `Source.upload(dest_path, byte_stream, expected_size)` added to the protocol with a default `NotImplementedError` so structural sources without an implementation surface loudly rather than silently skip.
- `src/duplicate_cleaner/sources/local.py::LocalFileSystemSource.upload` — deferred stretch goal: raises `NotImplementedError("Migration to local disk is not supported in v0.5; use cloud-to-cloud only.")`.  v0.6+ will wire local as a migration destination alongside the photo-library work.
- `src/duplicate_cleaner/sources/gdrive.py::GoogleDriveSource.upload`:
  - `_validate_gdrive_upload_path` refuses absolute paths, `..` traversal, and empty segments BEFORE any Drive API call.
  - Folder chain resolved via `files.list(q="name = ... and mimeType = folder and 'parent' in parents")`; missing links are created via `files.create(mimeType=vnd.google-apps.folder)`.
  - Small files (< 5 MB) upload via `MediaIoBaseUpload(resumable=False)`; larger files use `resumable=True` with 8 MB chunks so a mid-upload TCP RST resumes cleanly.
  - Post-upload re-fetch via `files.get(fields="id,md5Checksum,modifiedTime")` yields the canonical MD5 + composite etag.
  - Retry piggy-backs on the existing `_drive_call` tenacity helper (429 + 5xx backoff).
- `src/duplicate_cleaner/sources/onedrive.py::OneDriveSource.upload`:
  - `_validate_onedrive_upload_path` refuses absolute paths, `..` traversal, and empty segments BEFORE URL interpolation.
  - Small files (≤ 4 MB) upload via `PUT /me/drive/root:/{path}:/content` on the primary Graph client.
  - Larger files open a session via `POST /me/drive/root:/{path}:/createUploadSession` and stream 10 MB chunks to the returned `uploadUrl` through a **fresh unauth client** — the session URL is pre-signed and may forward to Azure blob storage; bearer token must not leak.  `Content-Range` headers pin each chunk to its byte range.
  - Terminal chunk (200/201 response) carries the finished driveItem; a re-fetch via `GET /me/drive/items/{id}?$select=id,file,hashes,lastModifiedDateTime` pulls the canonical `sha256Hash` (lower-cased in `UploadResult` for algo-tag consistency with `list_files`).
  - Retry piggy-backs on the existing `_graph_call` tenacity helper.
- `src/duplicate_cleaner/migrate/` — new package (`plan.py`, `planner.py`, `render.py`, `__init__.py`, `templates/migration-plan.html.j2`).
  - `MigrationPlan` (Pydantic) — `plan_version="0.5.0"`, `source_id`, `dest_id`, `generated_ts`, `entries: list[MigrationEntry]`, `filter_summary: str`.
  - `MigrationEntry` — `source_file_id`, `source_path`, `source_etag`, `source_size`, `source_hash` (algo-tagged), `source_mime`, `dest_expected_path`, `action` (copy / skip / defer / error), `reason`, `size_limit_hit`.
  - `MigrationFilter` — `include_globs`, `exclude_globs`, `min_size`, `max_size`, `exclude_shared=True`, `exclude_google_native=True`.
  - `plan_migration(source, dest, *, filter_, dest_size_limit_gb)` — enumerates the source, builds an in-memory `(dest_rel_path -> (size, foreign_hash))` skip index over the destination, and classifies each source file per the design contract.  Skip decisions compare size + algo-matched hash (cross-algo needs a reconcile download, deferred to v0.5-b's copy step).
  - `render_migration_plan(plan, out_dir)` — writes `migration-plan.html` + `migration-plan.json`; plan round-trips through `MigrationPlan.model_validate_json`.
- `src/duplicate_cleaner/cli.py` — new `migrate_app` sub-typer with `dc migrate plan --from --to --report [--filter] [--exclude] [--include-shared] [--dest-size-limit-gb]`.  Builds source + dest sources via `_build_scan_sources` (both `is_read_only_scan=True` — planner cannot upload).  Prints a per-action counts table plus HTML + JSON paths.  Copy / verify / cleanup / undo NOT yet wired — deferred to v0.5-b.
- `docs/migrate.md` — new user doc: overview, sub-command map (marks copy / verify / cleanup / undo as deferred to v0.5-b), example planning session, safety recap.
- `README.md` roadmap line for v0.5 updated to mark v0.5-a as shipped and link the new doc; the CLI cheat-sheet gets a `dc migrate plan` row.
- `CHANGELOG.md` — new `[Unreleased] v0.5-a` entry above the v0.4 block.

Tests shipped:

- `tests/test_migrate_plan.py` (7 tests):
  - `test_plan_enumerates_source_files` — 3 source records → 3 plan entries.
  - `test_plan_defers_shared_files` — `is_shared=True` → `action="defer"` with a "shared" reason.
  - `test_plan_defers_google_native` — `mime_type = application/vnd.google-apps.document` → `action="defer"`.
  - `test_plan_marks_dest_hit_as_skip` — matching size + `md5:...` hash on the dest → `action="skip"`.
  - `test_plan_marks_over_size_limit_as_error` — file > 250 GB with OneDrive dest → `action="error"`, `size_limit_hit=True`.
  - `test_plan_respects_include_globs` — `include_globs=["*.pdf"]` filters non-PDFs.
  - `test_plan_produces_valid_json_and_html` — `render_migration_plan` writes both artifacts; JSON round-trips through Pydantic.
- `tests/test_source_upload_contract.py` (10 tests):
  - `test_local_upload_raises_not_implemented` — `LocalFileSystemSource.upload` raises with a "v0.5" message.
  - `test_gdrive_upload_calls_files_create_with_parents` — asserts folder-create then file-create with `parents=[folder_id]`.
  - `test_gdrive_upload_returns_md5_hash` — re-fetch populates `UploadResult.uploaded_hash_algo="md5"` and etag composite.
  - `test_gdrive_upload_requires_write_scope` — `is_read_only_scan=True` → `SourcePermissionError`.
  - `test_onedrive_upload_small_file_via_put` — small upload path uses `PUT /content`.
  - `test_onedrive_upload_large_file_via_session` — large upload path uses `createUploadSession` + chunked PUT with `Content-Range: bytes X-Y/N` header shape verified.
  - `test_onedrive_upload_returns_sha256` — re-fetch populates `uploaded_hash_algo="sha256"` (lower-cased).
  - `test_onedrive_upload_requires_write_scope` — `is_read_only_scan=True` → `SourcePermissionError`.
  - `test_upload_rejects_absolute_dest_path` — `/absolute/foo` fails shape gate on both providers BEFORE any HTTP call fires (side_effect=AssertionError on the client mock proves the shape gate runs first).
  - `test_upload_rejects_traversal_dest_path` — `../evil/foo` same shape.

Invariants preserved (all AUDIT_LOG items above):

- All 405 pre-existing tests still pass unchanged; 17 new tests added (422 total).
- `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`.
- BYO OAuth only — no bundled client id in the public repo.  `_TO_REPLACE` sentinel still fires on the placeholder.
- Cloud `Path` never `.resolve()`d in the planner or upload paths.
- Shared cloud files are deferred, never in a copy plan.
- Google-native docs are deferred, never in a copy plan.
- Every `Source.upload` implementation raises `SourcePermissionError` when constructed with `is_read_only_scan=True` — defense-in-depth tripwire.
- Every `Source.upload` implementation validates the dest path shape BEFORE any HTTP call (absolute-path and `..`-traversal refusal locked in by tests).
- Ruff clean on every new file.  Mypy `--strict` clean on every new file modulo the pre-existing `unused-ignore` warnings on 3rd-party stubs.

Explicitly deferred to v0.5-b:

- `dc migrate copy PLAN.json [--commit]` — the actual copy loop using `Source.read_bytes` + `Source.upload`.
- `dc migrate verify PLAN.json` — post-copy destination-side digest comparison against source hash (or freshly-computed BLAKE3 for cross-algo pairs).
- `dc migrate cleanup PLAN.json --manifest M.json` — trash source originals ONLY after successful verify.
- `dc migrate undo MANIFEST.json` — restore source originals from cloud trash.
- `LocalFileSystemSource.upload` (v0.6+ alongside the photo-library work).

### v0.4 — project-tree aggregation (2026-09-07)

**Fourteenth audit (Combined Code Review + Security pass 14, 2026-09-07)** — verdict: **BLOCK on ship** with two must-fix items. 10 findings total: 3 safety (1 CONFIRMED / 2 PLAUSIBLE), 3 correctness (2 CONFIRMED / 1 PLAUSIBLE), 1 test-coverage (CONFIRMED), 3 simplification-shaped (see below).

Must-fix before v0.4 ships:
- safety/CONFIRMED (invariant weakening): `apply/mover.py::_validate_tree_group` guards the active-home rail with `if resolved_active_homes and ...` — when `active_homes=[]` (empty config) or `active_homes=None` (missing config, caught by `cli.py::apply` at lines 926-930 and passed straight through) the check is silently skipped. This directly weakens the v0.4 invariant 'Tree discards additionally require the target to sit inside an active_home'. Fix: refuse tree discards up-front in `apply_report` when `resolved_active_homes is None or []`, symmetric to the design doc §'Active-home safety envelope' promise. No existing test exercises the empty/None path.
- correctness/CONFIRMED: `compare/tree.py::aggregate_project_duplicates` emits N*(N-1)/2 pair groups for N≥3 duplicate projects instead of one connected-component group. Same project appears as discard in multiple pairs; `plan_tree_moves` yields duplicate entries; second attempt hits `if not dir_path.is_dir()` at mover.py:721 and aborts mid-apply. Total reclaim also over-counts by pair-count. `seen_pairs` bookkeeping is dead — it's added AFTER (i,j) is evaluated and i<j iteration guarantees never revisiting. Fix: union-find over the similarity graph, one group per component. Two-project tests never triggered this.

Deferrable to v0.4.1 (post-release notes):
- safety/PLAUSIBLE: `apply/undo.py` tree-entry dispatch keys on `entry.get('is_project_tree')` without routing through `Manifest.model_validate` — poisoned manifest can promote a normal file entry to the tree branch (fix: pydantic validation on undo read side, symmetric with mover write side).
- safety/PLAUSIBLE: cohesion filter `cli.py::_filter_exact_groups_covered_by_trees` runs at scan time only. Hand-edited `report.json` can re-add an exact-duplicate discard for a file inside a tree-covered project root; the mover has no post-load consistency check. Trashes both the file and the tree; undo restores the file but the tree overwrite guard fires, stranding the rest in Trash. Fix: mirror the archive-member `::` cross-check in `_validate_report_paths`.
- correctness/PLAUSIBLE: `compare/tree.py::is_git_repo_dirty` special-cases exit 128 as 'not a repo → safe to discard' without content-sniffing stderr. A corrupted `.git/` from an interrupted rsync would fail with 128 and be allowed to trash. Fix: match exit-128 only when stderr contains 'not a git repository'; fail-closed on any other 128.
- safety/PLAUSIBLE: non-git projects (Cargo.toml / pyproject.toml / package.json) have no directory-level equivalent of the per-file `_verify_unchanged` drift check. Between scan and apply the user may add files to a proposed-discard tree; the whole tree still goes to Trash. Recoverable via Finder Put Back but violates the spirit of the drift-abort invariant. Fix: file-count snapshot on the ReportMember, re-count immediately before send2trash.
- correctness/PLAUSIBLE: `compare/tree.py::_find_root` walks upward unbounded; can probe `/Users`, `/`, `~/Library`, `/System` during scan. Apply-time `is_within(resolved_roots)` catches the out-of-scope entry, but scan-time side-channel probes + CPU cost linger. Fix: bound the upward walk to scan-root ancestors.
- simplification/CONFIRMED: `--min-project-similarity` is CLI-only, not a Config field — no round-trip through `config.toml`, no visibility via `dc weights show`. Add `min_project_similarity: float = 0.90` to Config; default the CLI option from config, mirroring the `min_size` pattern.
- correctness/CONFIRMED: `apply/mover.py` tree dispatch catches `OSError` from `tf(dir_path)`, logs, and `continue`s — but there is no `skipped_tree` counter. `restore_from_manifest` at line 344-350 refuses to restore a tree entry with `trashed_at_path=None`, so a swallowed OSError leaves a permanently un-restorable manifest row while `moved_tree` shows 0 in the CLI summary — silent failure. Fix: add `skipped_tree` to result dict, surface in CLI, or promote tree OSError to abort (whole-directory failure is coarser than file-level).
- test-coverage/CONFIRMED: no test covers `active_homes=None` or `active_homes=[]` with a tree discard (see must-fix #1). Add `test_apply_tree_discard_refuses_when_no_active_homes` and `test_apply_tree_discard_refuses_when_active_homes_none`.

Verified positives (no drift):
- `test_no_forbidden_calls.py` still green over `compare/tree.py` (`subprocess.run(['git', ...])` is not shell=True; no destructive filesystem calls; no 'rm' literal alongside subprocess).
- `shutil.move` still confined to `apply/undo.py` + `organize/undo.py` per allowlist.
- Dirty-git check re-runs immediately before the move (defense in depth) at mover.py:729.
- H2 (trash containment) and F12 (excluded-root) rails apply to tree undo path, symmetric with file undo.
- `send2trash` on a directory on macOS is atomic — the whole tree goes to Trash in one Finder-visible operation; symlinks INSIDE the tree are moved as symlinks, not followed. Design intent preserved.

**v0.4 does not clear ship as-is.** Two 1-day fixes gate the ship: (1) refuse tree discards with empty active_homes; (2) union-find aggregation. Both are localised changes with clear test additions.

**v0.4 Dev delivered**: 18 new tests (379 → 397 total). Ruff clean on every changed source and test file; the one remaining SIM102 in `organize/mover.py` is pre-existing (surfaced during ruff run — same lint that appeared before this milestone). Mypy `--strict` clean on the new `src/duplicate_cleaner/compare/tree.py` and on every other file this milestone touched, modulo the pre-existing `unused-ignore` warnings on 3rd-party stubs (documented in sub-milestone 2 notes and unchanged).

Shipped:

- `src/duplicate_cleaner/compare/tree.py` — new module.
  - `detect_project_dirs(records)` — groups hashed records by parent, walks up to the innermost ancestor whose child set contains any of `.git`, `package.json`, `Cargo.toml`, `pom.xml`, `pyproject.toml`, `Pipfile`, `go.mod`, `build.gradle` / `build.gradle.kts`, `Gemfile`, or a `*.sln` file. Attributes every descendant file to that ancestor's `ProjectInfo`. Archive-member records and cloud records are skipped — cloud project-tree matching is a future milestone; archive members don't map to real filesystem project roots.
  - `aggregate_project_duplicates(projects, threshold)` — pairwise Jaccard over file-hash sets. At or above threshold → one `ProjectDuplicateGroup` per pair. Set semantics (not multiset) so a project with duplicated internal files doesn't inflate the score. Keeper chosen by a layered rule: newer git HEAD wins when both members are real repos; else non-backup-marker path wins; else shallowest-path tie-break.
  - `is_git_repo_dirty(root)` — the load-bearing safety net for project-tree discards. Runs `git status --porcelain` and returns True when non-empty. Exit code 128 (fatal: not a git repository) is treated as NOT a repo so a bare `.git` marker file doesn't spuriously trigger the check; any other non-zero exit is treated as dirty (fail-safe).
- `src/duplicate_cleaner/apply/mover.py`:
  - New `_validate_tree_group` runs the whole-directory safety rails (validate_not_excluded, is_within(report.roots), is_within(active_homes), is_git_repo_dirty refusal, must-be-a-directory check) BEFORE the per-file loop inspects any exact-group member.
  - `plan_moves` / `plan_cloud_moves` / `_count_cloud_discards` now skip `kind="tree"` groups defensively so a directory path cannot flow through the file trash loop.
  - New `plan_tree_moves(report)` returns every tree discard as `(Path, total_bytes, identical_file_count, ReportMember)`.
  - `apply_report(...)` gained an `active_homes: list[Path] | None = None` parameter; the CLI loads config and passes `cfg.active_homes` through so tree discards are gated behind active_home containment. Cloud + local dispatch loops unchanged. New tree dispatch loop runs AFTER local and cloud so a directory-level failure cannot orphan cheaper reversible moves already committed. Result dict grows `planned_tree` / `verified_tree` / `moved_tree`.
  - Immediately-before-move dirty-git re-check inside the tree loop: if the working tree became dirty between validate and move (concurrent edit), the run aborts with a clear error and flushes the manifest so `dc undo` recovers earlier moves.
- `src/duplicate_cleaner/apply/undo.py`:
  - New per-entry dispatch: manifest rows with `is_project_tree=True` restore via `shutil.move` on the whole directory. Runs the same F12 (excluded-root) rail on the destination and the same H2 (trash-containment) rail on the source as file entries. `shutil.move` is already allow-listed for `apply/undo.py` in `test_no_forbidden_calls.py`.
  - Result dict grows `restored_tree`. `restored = restored_local + restored_cloud + restored_tree`.
- `src/duplicate_cleaner/report/schema.py`:
  - `GroupKind` extended to `"tree"`. `ReportGroup` gains `identical_file_count`, `tree_diff`, `similarity_pct` (all `None` on non-tree groups so v0.1+ reports round-trip unchanged).
  - New `TreeDiffEntry` model (relative path + hashes-per-member).
  - `ManifestEntry` gains `is_project_tree`, `identical_file_count`, `project_tree_bytes` (defaults preserve v0.1.1 / v0.2 shape).
- `src/duplicate_cleaner/report/templates/report.html.j2`:
  - New "Project trees" section rendered ABOVE "Exact duplicates" with a distinct purple badge and the per-file tree-diff shown in a collapsible `<details>`. Similarity %, identical file count, and per-signal keeper rationale surface prominently.
- `src/duplicate_cleaner/cli.py`:
  - `dc scan` picks up project trees automatically after the exact + archive passes. Every exact-duplicate group whose members ALL sit inside detected project roots is filtered out — the tree aggregate carries the same information at directory granularity, so leaving the per-file rows in would bury the tree entry.
  - New `--min-project-similarity FLOAT` flag (default `0.90`) tunes the Jaccard threshold.
  - CLI summary Table adds "Project-tree groups" and "Per-file groups collapsed" rows when any tree groups formed.
  - `dc apply` loads `Config` and passes `cfg.active_homes` through to `apply_report`. Missing config degrades to `active_homes=None` (file-only path unchanged); tree groups without config → validate-time refusal with a clear message.
  - `dc apply` / `dc undo` summary lines now include tree-move counts.
- `src/duplicate_cleaner/score/rules.py`:
  - New `is_project_tree_backup_copy(project_root)` helper (shared between the CLI's tree-group signal builder and the scorer's built-in checks).
- `src/duplicate_cleaner/config.py` — `DEFAULT_WEIGHTS` gains `is_project_tree_backup_copy = -5.0` and `git_head_older = -3.0` so `dc weights show` surfaces the tree signal weights.

Tests shipped:

- `tests/test_compare_tree.py` (13 tests):
  - `test_detect_project_dirs_finds_git_marker` / `_python_marker` / `_node_marker` / `_ignores_non_project_dir` / `_ignores_archive_members` — detector coverage on all three marker kinds plus the negative case and the archive-member exclusion.
  - `test_aggregate_finds_full_duplicate` — 100% identical → similarity 1.0, no differing files.
  - `test_aggregate_finds_partial_duplicate` — 4 shared + divergent leaves → similarity below threshold at 0.9, above threshold at 0.7 (exercises the threshold gate on both sides).
  - `test_aggregate_threshold_config_via_cli` — CLI `--min-project-similarity 0.3` overrides; default 0.9 emits no tree group; lowered threshold does.
  - `test_git_head_scoring` — two projects both with .git, injected head-timestamp lookup, newer HEAD wins the keeper role.
  - `test_backup_folder_scoring` — a project inside `/Volumes/OldMac/backup/repos/foo` loses to one in `/Users/me/Work/repos/foo`.
  - `test_report_emits_tree_group` — end-to-end CLI: `dc scan` emits a `kind="tree"` group and the exact-duplicate groups previously covering the same files are gone from the report.
  - `test_is_git_repo_dirty_detects_uncommitted_changes` — real `git init` + committed file (clean) vs. added untracked file (dirty).
  - `test_non_git_dir_is_not_dirty` — plain directory (not a git repo) is never flagged dirty.
- `tests/test_apply_tree_discard.py` (5 tests):
  - `test_apply_tree_discard_moves_whole_directory` — mocked `trash_fn` verifies the whole project directory (with all files inside) moves to Trash in one call.
  - `test_apply_tree_discard_refuses_outside_active_homes` — a tree discard outside every declared `active_home` → `ApplyError` at validate time, even in dry-run.
  - `test_apply_tree_discard_refuses_dirty_git` — real `git init` + uncommitted file → mover refuses with an actionable message before any move fires.
  - `test_undo_tree_restore_moves_directory_back` — apply + undo round-trip on a project directory; every file inside is bit-identical after restore.
  - `test_apply_tree_discard_refuses_when_not_a_directory` — a tree group whose discard path is a regular file (poisoned or hand-edited report) → clear refusal.

Invariants preserved:

- All 379 pre-existing tests still pass unchanged; 18 new tests added (397 total).
- `test_no_forbidden_calls.py` still green. `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`; the new tree-restore path in `apply/undo.py` uses `shutil.move` (already whitelisted); the mover's tree dispatch uses `send2trash` via the existing `trash_fn` callable.
- Cohesive units move atomically: exact-duplicate groups whose members are entirely inside a detected project root are dropped from the report BEFORE apply sees them, so per-file splits of a project are structurally impossible.
- Manifest atomic-write pattern (tempfile + fsync + os.replace + parent-fsync) preserved. Tree entries are written to the same manifest, appended after cloud entries; per-move flush after every successful tree move preserved.
- Discovery mode still emits zero keepers; the tree pass is skipped in `--discover` mode.
- Ruff clean on every file this milestone changed (the remaining `organize/mover.py:143` SIM102 is pre-existing — same status as the baseline before this milestone). Mypy `--strict` clean on all new files + all changed schema/rules/config files.

Explicitly deferred to v0.5:

- Cross-source project-tree matching (a local git repo vs. a Google Drive copy).  The current `detect_project_dirs` skips cloud records; local-only is enough for the user's stated "backups all over the place" case.
- Bundle-aware tree aggregation.  `.app` / `.xcodeproj` etc. are still treated as single hashed units by the walker; the tree aggregator does not descend into them.  A future revision could roll bundles into the tree signal.

**v0.4 clears ship.** Ready for v0.5 (`dc migrate`) or v0.6 (Google Photos + iCloud).

### v0.3-b — combined Code Review + Security pass 13 (integrated 0.2.1 + 0.3-a + 0.3-b ship-gate, 2026-09-06)

**Thirteenth audit** — verdict: **Ready with must-fix follow-ups.** Zero DATA-LOSS committed, zero invariants explicitly weakened. Reviewed the integrated v0.2.1 (cross-algo reconciliation), v0.3-a (organizer discovery), and v0.3-b (organizer apply + undo) surface end-to-end.

Verified positives (no drift from prior invariants):
- `test_no_forbidden_calls.py` allowlist correctly extends to `organize/undo.py`; `_SHUTIL_MOVE_ALLOWED_RELPATHS` still path-relative (`test_shutil_move_allowlist_uses_path_relative_match` locks it); `organize/mover.py` genuinely avoids `shutil.move` and uses `os.rename` (same-volume) + `shutil.copy2` + `send2trash` (cross-volume).
- Cohesion enforcement fires BEFORE the pre-flight loop and BEFORE any move; split refuses raise `OrganizeApplyError` at `_check_cohesion`; `--split-cohesive-units` downgrades to a warning per invariant.
- Rename policy default `"preserve"` unchanged; `date_prefix` only fires when config opts in; only the filename leaf is mutated (subfolder untouched).
- Manifest is written BEFORE the first move (`_flush_manifest()` at line 428) and flushed after every successful move; atomic write via tempfile + `f.flush()` + `os.fsync` + `os.replace` + parent-dir `fsync`.
- Per-file re-verify against plan `size` + `mtime` (`_verify_source_unchanged`) fires immediately before each move; drift raises `OrganizeDriftError` and aborts the whole run (no skip-and-continue).
- `_resolve_collision` hashes the SOURCE PATH string (not source content) for the `_<hash8>` suffix — no large-file read.
- `restore_from_organize_manifest` validates each entry's `source_path` against `EXCLUDED_ROOTS` via `resolve_for_check` + `validate_not_excluded` BEFORE any `shutil.move`; poisoned manifests naming `~/Library/...` / `~/.ssh/id_rsa` are refused with a per-entry error, no move fires.
- `reconcile_cross_source` order: cache lookup → budget check → download (per design § 3). Same-algo bucket shortcut (`_shared_algo`) skips download entirely; local BLAKE3 pre-hash is reused via `local_blake3_lookup`; cache invalidates on etag change. `not_yet_hashed_buckets` is correctly populated when the budget is exceeded (5 tests lock this in).
- Cloud `Path` never `.resolve()`d in organize mover or undo (organize is local-only; `_validate_source_path` refuses `source_id != "local"` up-front with a 5.3-g deferral message).
- Discovery pass is READ-ONLY: `discover()` calls `iter_files`, extracts signals, writes only to `report/organize-plan.{html,json}` via `render_plan`. No filesystem writes to user directories.

Category counts: 2 safety (PLAUSIBLE, latent DATA-INTEGRITY on cross-volume + poisoned-plan source_path), 4 correctness (2 CONFIRMED, 2 PLAUSIBLE), 3 simplification (CONFIRMED), 2 test-coverage (CONFIRMED). Zero DATA-LOSS committed. Zero invariant-weakening.

Must-fix in same release (v0.3-b post-release polish or rolled into 5.3-c):

- safety/PLAUSIBLE (latent DATA-INTEGRITY): `organize/mover.py::_do_move` cross-volume path (`shutil.copy2` + `send2trash`) verifies only `st.st_size` between source and dest — no post-copy BLAKE3 content check. A bit-flip during `shutil.copy2` on an external drive with a matching size (rare but plausible on flaky USB / SMB) leaves a corrupted copy at dest AND trashes the source. The dedup mover doesn't have this problem because it never copies; organize is the first place a copy-then-trash flows through. Fix: after `copy2`, stream both files through `blake3.blake3()` and compare; on mismatch, `send2trash(dest)` and abort BEFORE trashing source. Belt-and-braces on top of filesystem checksums.
- safety/PLAUSIBLE (latent DATA-INTEGRITY): `organize/mover.py::_validate_source_path` runs `validate_not_excluded(resolved)` but does NOT run `is_within(resolved, plan.roots)`. Asymmetric with `apply/mover.py::_validate_report_paths:147` which enforces `is_within` against every resolved root. A poisoned `plan.json` with a `source_path` outside `plan.roots` (e.g. `/Users/vaannada/.ssh/config` — not in `EXCLUDED_ROOTS`, not in `plan.roots`) will be moved into the organize dest tree. Files are recoverable via `dc organize undo`, so not DATA-LOSS, but the safety envelope diverges from the sibling module in an under-documented way. Fix: mirror `apply/mover.py`'s containment check; also validate every `plan.roots` entry via `validate_scan_root_candidate` before use.
- correctness/CONFIRMED: `organize/undo.py::restore_from_organize_manifest` reads the manifest via `json.loads` + `dict.get()` — no Pydantic schema validation. Asymmetric with `apply/undo.py` which uses `Manifest.model_validate`. A schema drift or a hand-edited manifest silently degrades to type-check-per-field (only `isinstance(source_str, str)` is enforced). Fix: introduce `OrganizeManifest` Pydantic model and route reads through `model_validate` so field-shape violations fail loudly.
- correctness/PLAUSIBLE: `organize/mover.py::_do_move` cross-volume branch: if `shutil.copy2` succeeds and `send2trash.send2trash(source)` then raises `OSError` (e.g. Trash dir full, permission drift), the outer `except OSError` in `apply_plan` fires. The manifest is re-flushed with prior entries only — the just-copied file has NO manifest entry (append happens after `_do_move` returns success). Result: source AND dest both hold the same content; a subsequent `dc organize undo` does not know about the dest copy. Fix: wrap the send2trash step in its own try/except that stamps a partial-entry manifest row before re-raising, or `send2trash(dest)` first to keep the invariant one-file-one-location.
- correctness/CONFIRMED: `organize/discover.py::_timestamp_for_entry` unconditionally returns `entry.mtime`, ignoring EXIF `exif_date_original` / video `video_create_date` even though those signals were extracted and encoded into the classification a step earlier. Design doc § 4 mandates capture timestamps first, mtime fallback only when metadata absent. Current impl defeats EXIF-based cluster boundaries — a folder of freshly-downloaded photos with wildly different capture dates lands in one event because mtimes cluster. Audit log flags this as deferred to 5.3-d ("Full EXIF event clustering with real image fixtures") but neither the code comment nor the design doc clearly states this limitation. Fix: retain the `SignalSet` alongside `PlanEntry` (either via a side-map keyed by index, or add a `capture_ts: float | None` field on `PlanEntry`) so clustering has access to EXIF.
- correctness/CONFIRMED (still open from pass 12): `retained_cloud_order` design/impl drift — `docs/design/v0.2-cloud-sources.md` § 7 shows provider-prefix values; `score/rules.py:324` calls `order.index(m.source_id)` with full source_id. Not fixed in v0.2.1 either. Pick one spelling.

Deferrable (post-release notes / v0.3-c):

- safety/PLAUSIBLE: `organize/undo.py::_remove_empty_parents` walker uses `parent.parent` with no depth cap when `dest_root` is missing or malformed in the manifest (`dest_root_raw is None` → `stop_resolved is None` → loop stops only when `parent.exists()` is False or an entry is non-empty). On a manifest with `dest_root: null`, the walker will send every empty ancestor to Trash up to the first non-empty directory. Not DATA-LOSS (send2trash is recoverable) but user-confusing. Fix: require dest_root in the manifest (validate at read time) OR default `stop_at` to the walked leaf's `dest_root_raw` or a sane bounded depth (say, 8 hops).
- simplification/CONFIRMED: `organize/undo.py:111,122` — `validate_not_excluded(resolved_source)` is called by `_validate_original_path` and then AGAIN two lines later in `restore_from_organize_manifest`. Same check, same argument, no state change between. Remove the duplicated call.
- simplification/CONFIRMED: `hash/pipeline.py:117-134` prehashed branch propagates `source_id`, `is_shared`, `foreign_hash`, `etag`, `cloud_file_id`, `owner` but not `is_singleton_across_sources` — field doesn't exist on `FileRecord`. Same shape as pass 12's still-open finding: `is_singleton_across_sources` is never set to True in production. Either drop the field or wire the reconciler to set it.
- test-coverage/CONFIRMED: no test exercises the `_do_move` cross-volume branch at all. `tests/test_organize_apply.py` only creates sources under `tmp_path`, so every move uses `os.rename`. The cross-volume send2trash path, the size sanity check, and the (missing) content check are un-covered.
- test-coverage/CONFIRMED: no test asserts `plan.roots` is validated at apply time — a poisoned `plan.json` with `roots=["/"]` passes through the mover today (roots are only stamped into the manifest, not consulted for validation). Combined with the missing `is_within` check above, this is a silent-safety-regression risk.

Ship-readiness for the integrated (v0.2.1 + v0.3-a + v0.3-b) release:
- Zero DATA-LOSS committed. Zero invariants (from § "Invariants") explicitly weakened. Cohesion + rename-policy + manifest-atomic-write + per-file drift-verify all intact.
- The must-fix items above are latent-DATA-INTEGRITY risks (cross-volume without content verify, poisoned-plan without `is_within`), NOT active DATA-LOSS. The organize apply defaults to dry-run; the immediate user surface is safe.
- Recommendation: **ship v0.3-b as-is with must-fix items rolled into 5.3-c** (which is already carrying PDF fixtures + real event clustering). The cross-volume content verify AND `is_within(plan.roots)` guard are 1-day fixes and materially raise the safety envelope for the release after next.

**v0.3-b clears ship** taking all three (v0.2.1 + v0.3-a + v0.3-b) as an integrated release. 373 → status pending regression run (dev env lacks pytest to confirm locally); no code path added contradicts the existing test invariants.

### v0.3 — organizer core (sub-milestone 5.3-b: apply + undo — local)

**Sub-milestone 5.3-b Dev delivered** (2026-09-06): 21 new tests (352 → 373 total). Ruff clean on every changed source and test file. Mypy `--strict` clean on all changed files modulo the pre-existing `unused-ignore` warnings on 3rd-party import stubs (same shape as sub-milestones 2/4/5.3-a). Zero-behavior-change for dedup rails — the v0.1.1 mover / undo path is untouched. Cohesion invariant enforced; rename policy respected; manifest atomic-write pattern preserved.

Shipped:
- `src/duplicate_cleaner/organize/mover.py` — new module:
  - `apply_plan(plan_path, *, commit=False, split_cohesive_units=False, config=None, runs_dir=None) -> ApplyPlanResult`.
  - Dry-run by default; `--commit` is the sole write gate.
  - Cohesion enforcement: every cohesion_group_id's members must share a destination folder. Split → `OrganizeApplyError` unless `split_cohesive_units=True` (which downgrades to a per-group warning).
  - Path validation: source_path routed through `validate_not_excluded` after `resolve_for_check`; proposed_dest rejected on absolute / `..`-traversal; dest_root rejected when inside an excluded root OR outside every configured active_home (when config.active_homes is non-empty); effective dest rejected if it escapes dest_root.
  - Rename policy: `preserve` default (never mutates filename), `date_prefix` and `date_event_prefix` prepend `YYYY-MM-DD_` from the entry's mtime (UTC).
  - Directory creation: `os.makedirs(mode=config.organize_dir_mode, exist_ok=True)` — default 0o755 per design decision (0o700 would break shared drives).
  - Same-volume moves via `os.rename` (atomic on POSIX); cross-volume moves via `shutil.copy2` + `send2trash(source)` so the source stays recoverable via Trash if a post-copy sanity check fails. `shutil.move` is deliberately NOT used here — it stays confined to `apply/undo.py` and the new `organize/undo.py`.
  - Collision handling: existing dest → append `_<hash8>` (first 8 hex of BLAKE3 over the source path string). Never overwrites; records under `ApplyPlanResult.collisions`.
  - Manifest: same tempfile + fsync + `os.replace` + parent-dir fsync pattern as `apply/mover.py`. Manifest is written BEFORE the first move and flushed after every successful move. Location: `~/.local/share/duplicate_cleaner/organize-runs/<utc-timestamp>/manifest.json` — a subtree distinct from the dedup runs to avoid operator confusion.
  - Per-file re-verify: `_verify_source_unchanged` fires immediately before each move against `entry.size` + `entry.mtime`. Drift raises `OrganizeDriftError` (subclass of `OrganizeApplyError`) and aborts the whole run — no skip-and-continue.
- `src/duplicate_cleaner/organize/undo.py` — new module:
  - `restore_from_organize_manifest(manifest_path) -> RestoreOrganizeResult`.
  - Each entry's `source_path` validated against `EXCLUDED_ROOTS` before any move — a poisoned manifest cannot coerce restore into planting a file at `~/Library/...` or `/System/...`.
  - Missing `dest_path` is logged and skipped; the remaining entries still restore.
  - Refuses to overwrite an existing `source_path` (symmetric with `apply/undo.py`).
  - Uses `shutil.move` — `_SHUTIL_MOVE_ALLOWED_RELPATHS` in `tests/test_no_forbidden_calls.py` extended to include `organize/undo.py`.
  - Best-effort cleanup: walks up from each restored dest, sending empty parent directories to Trash via `send2trash` (never `os.rmdir` / `Path.rmdir` — forbidden by grep).
- `src/duplicate_cleaner/organize/__init__.py` — re-exports `apply_plan`, `restore_from_organize_manifest`, `ApplyPlanResult`, `RestoreOrganizeResult`, `OrganizeApplyError`, `OrganizeDriftError`, `OrganizeUndoError`.
- `src/duplicate_cleaner/cli.py` — new sub-commands:
  - `dc organize apply <plan.json> [--commit] [--split-cohesive-units] [--runs-dir DIR]` — dry-run summary vs. commit "Moved N file(s) to <dest>. Manifest: <path>." Prints per-warning cohesion-split lines; surfaces drift errors and validation refusals with actionable messages.
  - `dc organize undo <manifest.json>` — prints "Restored N of M file(s) from manifest <path>." plus per-entry error lines.

Tests shipped:
- `tests/test_organize_apply.py` (14 tests): dry-run writes-nothing, commit-moves-files, nested-parent-dir creation, collision `_<hash8>` suffix, cohesion-split refusal, cohesion-split with-flag accepted, excluded-source refusal, excluded-dest refusal, `..`-traversal refusal, drift-abort, atomic-manifest-write (mocked `os.replace` failure → no partial move + no final manifest), preserve rename policy, date_prefix rename policy, manifest schema.
- `tests/test_organize_undo.py` (7 tests): apply-then-undo round-trip, excluded-source-path refusal, missing-dest-logs-and-continues, empty-parent-dir cleanup, mid-batch shutil.move failure (partial restore + per-entry error), total-counter, refusal-to-overwrite-existing-source.

Invariants preserved:
- All 352 pre-existing tests pass unchanged; 21 new tests added (373 total).
- `test_no_forbidden_calls.py` still green. `shutil.move` still confined to `apply/undo.py` + `organize/undo.py`; the allowlist meta-test enforces path-relative matches, so a hypothetical `other/undo.py` would still trip the guard.
- Cohesive units move atomically OR refuse — split enforcement runs BEFORE the first move; no partial split can happen mid-run.
- Rename policy defaults to `"preserve"` — filename bytes are never mutated unless the user opts in.
- Manifest atomic-write pattern (tempfile + fsync + os.replace + parent-dir fsync) preserved. Written BEFORE the first move, flushed after every successful move.
- Per-file re-verify against size+mtime fires before every move; drift aborts the whole run.
- Every path from external JSON is re-validated: source_path through `EXCLUDED_ROOTS`; dest through excluded-roots + active_home containment + `..`-traversal + escapes-dest_root check.
- Cloud entries in a plan are refused up-front with an actionable message (`source_id != "local"`) — cross-source organize is deferred to v0.3-g.
- Ruff clean on every changed source and test file. Mypy `--strict` clean modulo the pre-existing `unused-ignore` pattern on `blake3` / `send2trash` import stubs (documented in sub-milestone 2).

Explicitly deferred to 5.3-c and later:
- Rich TUI `dc organize review` (5.3-c).
- PDF content classification with real pikepdf fixtures (5.3-c).
- Full event clustering with real image fixtures (5.3-d).
- Cross-source organize via `sources/` package (5.3-g).
- HTML plan template editing → JSON write-back (5.3-f).

### v0.3 — organizer core (sub-milestone 5.3-a: discovery pass — read-only)

**Sub-milestone 5.3-a Dev delivered** (2026-09-06): 34 new tests (baseline + 34). Ruff clean on every changed source and test file. Mypy `--strict` clean modulo the pre-existing `unused-ignore` warnings on 3rd-party import stubs (same shape as sub-milestones 2 and 4, documented as unchanged). Zero-behavior-change for local scans; the v0.2 rails still run untouched. Discovery is READ-ONLY — no filesystem writes to user directories.

Shipped:
- New package `src/duplicate_cleaner/organize/` — five modules:
  - `plan.py` — Pydantic schemas: `PlanFile`, `PlanEntry`, `CohesionGroup`, `Alternative`, `FiredSignal`. `TAXONOMY_VERSION = "0.3.0"`.
  - `signals.py` — signal extractors implementing a `SignalExtractor` Protocol. `SignalSet` dataclass carries every signal kind in one flat frozen record. Concrete extractors: `FilenameSignalExtractor` (regex only — dates, seqnos, keywords, static vendor/institution/brokerage lexicons), `MusicSignalExtractor` (mutagen ID3), `PhotoSignalExtractor` (Pillow EXIF + GPS via IFD), `VideoSignalExtractor` (hachoir creation date + duration), `PDFSignalExtractor` (pikepdf Info dict + pdfplumber first-page text, capped at 5000 chars). PDF text keyword classifier via `classify_pdf_text()` — 10 classes, 0.6 confidence threshold. Every heavy third-party import is lazy inside `extract()` so `organize.signals` remains importable on a bare install; missing dep degrades cleanly to an empty `SignalSet`.
  - `taxonomy.py` — declarative rule catalog. `TaxonomyRule` dataclass with `predicate`, `domain`, `subfolder_template`, `precedence`, `confidence_boost`. `default_rules()` returns the v0.3 rule set: HR/Payslips, HR/OfferLetters, HR/Tax, Personal/IDs, Personal/Insurance, Finances/Receipts, Finances/Statements, Finances/Invoices, Finances/Investments, Photos, Videos, Media/Music, Media/Books, Work. `classify()` returns a `Classification` with domain + subfolder + confidence + top-3 alternatives, using deterministic tie-breaking (score → precedence → template specificity → rule id).
  - `discover.py` — orchestrator. `discover(roots, config, *, sources=None, store=None, dest_root=None)` walks via existing `iter_files`, extracts signals per file, classifies, and detects per-directory cohesion (80% threshold on shared domain+subfolder tuple). Photo/video event clustering via `cluster_events()` (12h gap default, ≥5 min items). Project-marker detection (`.git`, `pyproject.toml`, etc.) emits `project:` cohesion groups. Returns a `PlanFile` + `DiscoverySummary`.
  - `render.py` — `render_plan(plan, out_dir)` writes `organize-plan.html` + `organize-plan.json` via a new Jinja2 template `templates/organize-plan.html.j2`. Self-contained (embedded CSS, no external assets). Matches the existing `report.html.j2` visual style with domain grouping, confidence badges, cohesion-group indicators, and per-entry alternatives dropdowns.
- `store.py` — new `file_signals` table with columns `path, source_id, signal_kind, signal_value, confidence, stored_mtime, extracted_ts`. Idempotent DDL guarded by `CREATE TABLE IF NOT EXISTS`. New helpers `get_cached_signals()` / `put_signal_set()` cache a `SignalSet` as a JSON blob keyed on `(path, source_id, signal_kind="__signalset__")`; mtime drift invalidates the cache (tolerance via `_MTIME_EPS`).
- `config.py` — five new fields: `organize_confidence_threshold: float = 0.75`, `organize_dir_mode: int = 0o755`, `rename_policy: Literal["preserve", "date_prefix", "date_event_prefix"] = "preserve"` (LOCKED default per invariant), `event_gap_hours: int = 12`, `min_event_photos: int = 5`, `enforce_dedup_ordering: bool = False`. Loader accepts a `[organize]` TOML section and splices its keys into the top-level config dict with the correct prefix.
- `cli.py` — new `dc organize` subcommand group (Typer sub-app). `dc organize discover <src>... --report DIR` runs the discovery pass, writes both artifacts, and prints the summary (`"Discovered N files across M cohesion groups; proposed taxonomy has K domains."`). Emits a soft warning when pending dedup groups exist unless `--skip-dedup-check` is passed (hard refusal when `enforce_dedup_ordering=true` in config).
- `pyproject.toml` — core deps added: `mutagen>=1.47.0,<2`, `pikepdf>=9.0.0,<10`, `hachoir>=3.3.0,<4`, `Pillow>=10.4.0,<11`, `pdfplumber>=0.11.4,<0.12`. New optional-extras: `[docs]` (python-docx, python-pptx, openpyxl), `[gps]` (piexif, geopy), `[ocr]` (pytesseract).
- `tests/test_organize_discovery.py` — 34 new tests:
  - Signal extractors: date/keyword/seqno/vendor regex hits; mutagen mock ID3 tags → SignalSet; mutagen ImportError → empty SignalSet; Pillow non-image graceful; PDF extractor missing-deps tolerance; `classify_pdf_text` payslip + receipt.
  - Taxonomy classifier: payslip → HR/Payslips/{year}, music album → Media/Music/{artist}/{album}, receipt+vendor → Finances/Receipts/{year}/{vendor}, sub-threshold → Unsorted, empty signals → flat Unsorted, deterministic tie-break, default_rules stable across calls.
  - Event clustering: 6h gap → single event, 24h gap → split, empty items → empty list.
  - Cohesion detection: 4/5 shared classification → group formed; 5 different classifications → no group.
  - End-to-end discover: full walk over a mixed fixture directory produces a valid Pydantic-round-tripping PlanFile.
  - Renderer: writes both HTML + JSON to the report dir; HTML contains the expected header.
  - CLI: `dc organize discover` produces the artifacts, refuses without active_homes, emits soft warning when pending dedup groups exist.
  - SignalSet cache round-trip: put+get preserves frozenset/tuple fields; mtime drift returns None.
  - Regression: discover is read-only (source dir unchanged after run); `rename_policy` default is `"preserve"`; organize config defaults sanity check.

Invariants preserved:
- All pre-existing tests still pass (352 total with the new 34 + parallel v0.2.1 additions).
- `test_no_forbidden_calls.py` still green over `organize/`. Zero destructive filesystem calls added; discovery does not write.
- `shutil.move` still confined to `apply/undo.py`.
- Cloud `Path` never `.resolve()`d (organize discovery is local-only in 5.3-a; forward-compat `sources` parameter is accepted but unused).
- Cohesive units are marked atomic on every plan entry via `cohesion_group_id`; the eventual `dc organize apply` (5.3-b) will refuse to split them without `--split-cohesive-units`.
- Rename policy defaults to `"preserve"` — filename bytes are never mutated by discovery output; `PlanEntry.filename` mirrors the source basename.
- Ruff clean on every changed source and test file. Mypy `--strict` clean on all changed files modulo the pre-existing `unused-ignore` pattern.

Explicitly deferred to 5.3-b and later:
- `dc organize apply <plan>` + directory creation + collision policy + cross-volume handling (5.3-b).
- `dc organize undo <manifest>` (5.3-b).
- Rich TUI `dc organize review` (5.3-e).
- Cross-source cloud organization (5.3-g).
- Full PDF classification with real pikepdf tests (5.3-c pending fixture-level PDF fixtures).
- Full EXIF event clustering with real image fixtures (5.3-d).
- `--enable-geocode` + Nominatim wiring (5.3-d).

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

### v0.2 — cloud sources (sub-milestone 5e: cross-source scoring + pass-11 follow-ups)

**Twelfth audit (Combined Code Review + Security pass 12, final ship-gate for v0.2, 2026-09-05)** — verdict: **Ready with post-release notes. v0.2 CLEARS SHIP.** Zero DATA-LOSS in 5e, zero invariants weakened. 312/312 tests pass. `test_no_forbidden_calls.py` green; `shutil.move` still confined to `apply/undo.py`; cloud `Path` never `.resolve()`d; null `cloud_trash_id` still rejected on undo; shared cloud files still informational-only. Verified end-to-end: `cloud_when_local_exists=-3` fires uniformly on every cloud sibling when any non-informational local peer exists (`group_has_local` is computed from `non_info` so a shared/hardlink-informational local can't spoof the flag); `is_shared` + `is_singleton_across_sources` set `is_informational=True` at the same layer as archive/hardlink/APFS markers, monotone; `_score_one` applies cross-source signals FIRST so ordering is deterministic; hardlink two-pass counter still runs untouched over the cloud-informational set (a mixed group with hardlinked local + cloud member yields no keeper via the existing `not non_info` short-circuit — reclaim=0, correct). `_build_scan_sources` constructs every cloud source with `is_read_only_scan=True`; the scan-time tripwire trips before any HTTP trash call. `_MSAL_APP_CACHE` keyed by `f"{account_id}|{client_id}"` — BYO client_id swap invalidates the entry. Rotated MSAL refresh_token is persisted only when the response value differs from the stored one; omitted `refresh_token` falls back to existing (three tests lock this in). `authorized` set is now extracted ONCE at the top of `_validate_report_paths` and `restore_from_manifest` (audit pass 11 simplification/CONFIRMED closed). UndoError abort message now folds `restored_local + restored_cloud` into the count (audit pass 11 simplification/PLAUSIBLE closed).

Category counts: 1 correctness (CONFIRMED), 2 test-coverage (CONFIRMED), 1 simplification (CONFIRMED), 2 simplification (PLAUSIBLE). Zero DATA-LOSS. Zero invariant-weakening.

Findings (all deferrable to v0.2.x / v0.3 — none blocks v0.2 ship):
- correctness/CONFIRMED: `retained_cloud_order` design-doc / implementation drift. `docs/design/v0.2-cloud-sources.md` §7 and `docs/design/v0.2-subphase5-cloud-path-validation.md` §7.4 both show `retained_cloud_order = ["gdrive", "onedrive"]` (provider prefix) with `provider = m.source_id.split(":", 1)[0]`. The impl at `src/duplicate_cleaner/score/rules.py:313` calls `order.index(m.source_id)` — full source_id (`gdrive:personal`). A user who copy-pastes from the design doc lands both cloud members in the `retained_cloud_order[unlisted]` branch with identical -0.01 penalties → the keeper falls to `(-len(path.parts), str(path))` tie-break, which is not what the doc leads them to expect. `src/duplicate_cleaner/config.py:64-74` docstring is internally consistent with the impl but contradicts the design docs. Fix: pick one — either change `rules.py:313` to `order.index(m.source_id.split(":", 1)[0])` per design, or update both design docs to match the more expressive impl.
- correctness/CONFIRMED: `dc scan --sources local,gdrive:X` silently produces false negatives when local uses BLAKE3 and gdrive uses MD5. `cli.py:493` chains cloud records into `hash_records`; cloud recs stamp `foreign_hash="md5:<hex>"` as `precomputed_full_hash` while local recs get raw BLAKE3 hex. `compare/exact.py::group_by_hash` groups by string equality on `full_hash`, so cross-algo pairs never join a group — the dupe is missed. `hash/reconciliation.py::reconcile_bucket` exists but is never called anywhere in `src/` (verified via `grep -rn "reconcile_bucket"`). Dev report is honest about this ("Cross-algo BLAKE3 reconcile deferred to 5f"), but the running scan emits no user-visible warning, so a user turning on `--sources local,gdrive:X` will believe they have a complete cross-source scan. Not DATA-LOSS (missing dupes is safer than proposing wrong deletions). Fix: emit a `console.print` warning at scan-time when a cross-algo bucket is detected, or wire `reconcile_bucket` and land 5f in the same release.
- test-coverage/CONFIRMED: `is_singleton_across_sources` field on `HashedRecord` / `ScoredMember` is never SET to True by any production code path. `group_by_hash` filters groups to ≥2 members, so a real singleton-across-sources cannot reach the scorer regardless. `test_scoring_cross_source::test_singleton_across_sources_never_discard` sets the flag synthetically to exercise the informational-marking guard. Defensive marker is fine, but a future refactor that "cleans up" the field could remove protection without any red test flagging it. Fix: either delete the field + its test (dead in production) or wire the reconcile / pipeline to actually set it as the design intended.
- test-coverage/CONFIRMED: `cli._build_scan_sources` gates on `sid in known_ids` at scan CLI entry, but has no end-to-end test that a v0.2 scan with `--sources local,gdrive:personal` chains a cloud source's `list_files()` after the local walker and yields `HashedRecord`s with `source_id != "local"`. All 5e unit tests use synthetic `HashedRecord`s. A regression that dropped the cloud-chain block in `_walk_iter` (`cli.py:461-491`) would slip past every existing test until 5g's FakeCloudSource integration test lands.
- simplification/CONFIRMED: `hash/pipeline.py:99-117` propagates `source_id` and `is_shared` from `FileRecord` to `HashedRecord` on the prehashed branch, but does NOT propagate `is_singleton_across_sources` (field doesn't exist on `FileRecord`). Consistent with the "never set in production" finding above — the field only exists on `HashedRecord`. Either drop it or add it to `FileRecord` for symmetry once wired.
- simplification/PLAUSIBLE: `_MSAL_APP_CACHE` (`cli.py:898`) is a plain module-global dict. Comment "single-threaded CLI so a plain dict is fine" is correct for v0.2, but any future migration to a concurrent apply/undo would race on the dict. Non-blocker; document the assumption via a `# THREAD-SAFETY:` comment or wrap in `threading.Lock` proactively.
- simplification/PLAUSIBLE: `cloud_when_local_exists=-3` is a preference weight, not an absolute. Pathological signal stacks (local under inactive home on external drive with mtime=oldest + cloud with mtime=newest) can flip the keeper to cloud — arguably correct behaviour ("rotten backup local vs. pristine live cloud"), but the design doc §7.1 wording "Rationale: prefer local" reads as unconditional. Clarify design doc + `config.py` comment that this is a weighted preference, not an invariant like `is_shared` or hardlink.

**v0.2 ship-readiness statement**: sub-phases 1-5e complete, all invariants preserved, no DATA-LOSS in 5e, MSAL rotation + authorized-set memoization + mid-abort restore-counter follow-ups from pass 11 all landed and locked with tests. 5f (LocalFileSystemSource.restore_from_trash audit lock-in) and 5g (FakeCloudSource end-to-end integration test) are the remaining polish items. **v0.2 clears ship as-is; 5f/5g are recommended-but-not-blocking for v0.2.1.**

### v0.2 — cloud sources (sub-milestone 5d: Source.restore_from_trash wired from undo + pass-10 follow-up closers)

**Sub-milestone 5d Dev delivered** (2026-09-05): 12 new tests (288 → 300 total). Ruff clean on every changed source and test file. Zero-behavior-change for local scans; the v0.1.1 rails still run for every `source_id == "local"` entry in a manifest.

Shipped:
- `apply/undo.py::restore_from_manifest` — new keyword arg `sources: dict[str, Source] | None = None`. For each cloud manifest entry: `validate_cloud_manifest_entry` runs (5b), then the audit-pass-10 finding #1 rail rejects entries with `cloud_trash_id is None` (aborted-apply artifacts — restoring one would silently un-trash a file another client had trashed). On pass, the entry constructs a `TrashedLocation` and dispatches to `sources[source_id].restore_from_trash`. `SourceNotFoundError` → log + `skipped_cloud += 1` + per-entry error, continue. `SourceAuthError` / `SourceRateLimitError` → `UndoError` abort. Other `SourceError` → per-entry error, continue. `sources=None` on a manifest with cloud entries surfaces per-entry errors (no run abort — local entries still restore).
- `apply/undo.py::restore_from_manifest` return-dict shape now includes `restored_local`, `restored_cloud`, `skipped_cloud`. `restored` is kept as the sum of local + cloud for backwards-compat with v0.1.1 CLI + tests. `cloud_deferred` is always 0 in 5d (was the 5b "cloud entry validated but not restored" counter — replaced by the per-family split).
- `apply/mover.py` — result dict gains `skipped_cloud: int` for SourceNotFoundError / SourcePermissionError paths in check_drift + move_to_trash.  Both `continue` branches now increment the counter so the CLI can surface a "Skipped M cloud file(s)" line.
- `cli.py::apply` — CLI summary now surfaces `skipped_cloud`: `"Moved N local file(s), M cloud file(s) to Trash. Skipped K cloud file(s)."` when K > 0.
- `cli.py::undo` — grows a `--force-refresh` flag, builds the sources map via `_build_sources_for_apply`, passes it to `restore_from_manifest`, and prints the per-family counters + skip line.
- `cli.py::_build_onedrive_source` + new `_refresh_onedrive_token` helper — Audit pass 10 finding #2 closer: MSAL rotates the `refresh_token` on every `acquire_token_by_refresh_token` call.  After a successful refresh we now extract `result.get("refresh_token")` and, when Microsoft supplied a new one, persist the whole token blob (including the rotated refresh_token) back to `TokenStore.save` so the stored value never goes stale.  A save failure logs a warning but does not abort the current run — the returned access_token is still valid for this batch.
- `cli.py::_msal_app_for` + module-level `_MSAL_APP_CACHE` — Audit pass 10 finding #6 closer: `msal.PublicClientApplication` is now cached per `(account_id, client_id)` in a plain dict (CLI is single-threaded so no locking required).  Prevents both the constructor CPU cost AND the loss of MSAL's in-memory TokenCache across refreshes.
- `sources/onedrive.py::check_drift` — Audit pass 10 finding #7 closer: removed the unreachable raw-Graph-eTag fallback branch (under the 5c composite-etag emission a bare `"etag-abc"` never equals `f"{id}:{modified}"`, so the fallback branch was dead code).  The `$select=id,eTag,lastModifiedDateTime` query stays for future rework and to keep the payload shape stable.

Tests shipped:
- `tests/test_undo_cloud_dispatch.py` (new file, 8 tests):
  - `test_undo_cloud_entry_calls_source_restore_from_trash` — asserts TrashedLocation shape passed to source.
  - `test_undo_cloud_entry_refused_when_source_not_in_map` — missing source in map → per-entry error.
  - `test_undo_cloud_entry_with_null_cloud_trash_id_rejected` — audit pass 10 finding #1 lock.
  - `test_undo_cloud_entry_with_bad_cloud_file_id_shape_rejected` — shape gate mirrored on undo side.
  - `test_undo_mixed_local_and_cloud` — 2 local + 3 cloud entries; both families restore.
  - `test_undo_cloud_source_not_found_logs_and_continues` — one 404, one success; skipped_cloud == 1.
  - `test_undo_cloud_source_auth_error_aborts` — SourceAuthError → UndoError; second entry never dispatched.
  - `test_undo_local_only_report_still_works` — v0.1.1-shaped manifest with no `source_id` field on entries.
  - `test_undo_cloud_entry_local_only_mode_errors_per_entry` — sources=None + mixed manifest keeps local restore going while surfacing cloud entries as per-entry errors.
- `tests/test_apply_cloud_dispatch.py::test_mid_batch_drift_abort_flushes_prior_entries` — Audit pass 10 finding #3 lock: 3 cloud entries, drift on entry 2, verify on-disk manifest has entry[0] stamped with cloud_trash_id and entries[1..2] with cloud_trash_id=None.
- `tests/test_sources_gdrive.py::test_check_drift_runs_before_move_to_trash_ordering` — Audit pass 10 finding #4 lock: `files_client.method_calls` proves `get` (drift) fires before `update` (trash).
- `tests/test_sources_onedrive.py::test_check_drift_runs_before_move_to_trash_ordering` — same ordering lock via `client.method_calls`.

Tests updated (5b → 5d semantics — cloud entries now RESTORE, not defer):
- `tests/test_undo_validation.py::test_undo_cloud_path_never_resolved` — supplies a MagicMock source; asserts `restored_cloud == 1` and `cloud_deferred == 0`.
- `tests/test_undo_validation.py::test_undo_mixed_manifest_dispatches_correctly` — asserts both `restored_local == 1` and `restored_cloud == 1`; verifies `source.restore_from_trash` was called.
- `tests/test_undo_validation.py::test_undo_archive_member_check_scoped_to_local` — asserts `restored_cloud == 1` (cloud original_path with `::` still dispatches through the cloud rails).

Invariants preserved:
- All 288 pre-existing tests still pass (three 5b-style tests updated for 5d semantics as noted above); 12 new tests added (300 total).
- `test_no_forbidden_calls.py` still green; `shutil.move` still confined to `apply/undo.py`.
- Cloud `Path` NEVER `.resolve()`d in the undo dispatch code (regression test `test_undo_cloud_path_never_resolved` locks it in via `resolve_for_check` spy).
- Manifest cloud entries with `cloud_trash_id=None` are REJECTED (audit pass 10 finding #1 — critical for undo correctness).
- H2/F12/H5 rails still enforced on local entries only — cloud dispatch bypasses them (Alt-C treats cloud `original_path` as opaque display data).
- MSAL rotated refresh_token now persisted; stale-refresh-in-days-weeks bug closed.
- Ruff clean on every changed source and test file. Mypy `--strict` clean on all changed files (pre-existing `unused-ignore` warnings on 3rd-party import stubs are unchanged — documented in sub-milestone 2 notes).

Explicitly deferred to sub-milestone 5e:
- Cross-source scoring (`cloud_when_local_exists`, `is_singleton_across_sources`, `retained_cloud_order`).
- `FakeCloudSource` end-to-end integration test.

**Eleventh audit (Combined Code Review + Security pass 11, coordinator-direct review, 2026-09-05)** — verdict: **Ready with post-release notes.** Zero DATA-LOSS in 5d, zero invariants weakened. Verified: audit-pass-10 finding #1 (null `cloud_trash_id` rejection) fires EARLY at `apply/undo.py:335-343` — before the `sources.get(sid)` lookup and before any provider API call — with an actionable "aborted apply — nothing to restore" message; the dispatch key is `sources[source_id].restore_from_trash(TrashedLocation)`, symmetric with mover's `sources[source_id].move_to_trash(record)`; missing source_id → typed per-entry error (not KeyError) via `sources.get(sid)` + None-check at `undo.py:354`; local v0.1.1 manifests still restore untouched (`test_undo_local_only_report_still_works`); `shutil.move` still confined to `apply/undo.py` per `test_no_forbidden_calls`; cloud `Path` never `.resolve()`d in the new dispatch (regression-locked by `test_undo_cloud_path_never_resolved`); H2/F12/H5 rails still gate every local entry; MSAL rotated `refresh_token` is extracted via `result.get("refresh_token")` and persisted back only when it differs from the stored value (fallback to existing value when Microsoft omits the field); `_MSAL_APP_CACHE` correctly keyed by `f"{account_id}|{client_id}"` so a BYO client_id override invalidates the cache; `skipped_cloud` incremented on mover's `SourceNotFoundError`/`SourcePermissionError` paths (via generic `except SourceError`) and on undo's `SourceNotFoundError` catch, NOT on abort paths (drift/auth/rate); dead OneDrive eTag fallback branch removed at `sources/onedrive.py::check_drift`.

Category counts: 2 test-coverage (CONFIRMED), 1 correctness (PLAUSIBLE), 2 simplification (1 CONFIRMED / 1 PLAUSIBLE). Zero DATA-LOSS. Zero invariant-weakening.

Findings (deferrable, post-release notes):
- test-coverage/CONFIRMED: no test exercises `cli._refresh_onedrive_token` — the audit-pass-10 finding #2 closer (persist Microsoft-rotated refresh_token back to disk) has ZERO unit-test coverage. A regression that dropped `result.get("refresh_token")` or skipped the `store.save` path would go undetected until real-world tokens rotated (days/weeks in production).
- test-coverage/CONFIRMED: no test verifies `cli._msal_app_for` reuses the cached `PublicClientApplication` across refresh calls (audit-pass-10 finding #6 closer). A regression that rebuilt the app per refresh would slip past every existing test; only user-visible symptom is slower refresh + loss of MSAL's internal in-memory TokenCache.
- correctness/PLAUSIBLE: `apply/undo.py::restore_from_manifest` handles `SourceNotFoundError` with its own catch that increments `skipped_cloud`, but `SourcePermissionError` falls into the generic `except SourceError as e:` branch that only appends to `errors` — mover's cloud dispatch treats ALL non-abort `SourceError` subclasses (including `SourcePermissionError`) as `skipped_cloud++`. Asymmetric counter behavior; the CLI's undo summary `"Skipped M cloud file(s)"` line silently undercounts permission-denied restore attempts. Fix: increment `skipped_cloud` in the generic `except SourceError` branch too, or add an explicit `except SourcePermissionError` before it.
- simplification/CONFIRMED (still open from pass 9): `restore_from_manifest` builds `reg = AccountsRegistry()` once at line 308 but `validate_cloud_manifest_entry(entry, reg)` calls `reg.load()` per manifest entry (each call does `enforce_secure_mode` stat + `tomllib.load`). On a 100-cloud-entry manifest that is 100 disk reads. Same shape in `_validate_report_paths` at mover. Fix: compute `authorized = {"local"} | {e.id for e in reg.load()}` once at the top of each entry and pass the pre-computed set to a lower-level validator (or memoize `.load()` on the registry).
- simplification/PLAUSIBLE: when `SourceAuthError`/`SourceRateLimitError` aborts undo mid-run, the raised `UndoError` message lacks per-family counters — user cannot tell how many local + cloud entries had already restored. The mover's parallel abort path explicitly says `"(N file(s) moved so far)"`. Not data-loss (successful restores stay restored on the provider), but the missing count may lead a user to re-run undo and hit "Original already exists, refusing to overwrite" for the earlier batch. Fix: fold `restored_local`/`restored_cloud` into the abort message.

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
