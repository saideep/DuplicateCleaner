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

## Deferred to v0.1.2

- Bundle filename Unicode NFC/NFD normalization (HFS+ ↔ APFS migration case)
- Singleton hash markers `"singleton-by-size:<size>:<path>"` AND `"singleton-by-partial:<partial>:<path>"` currently leak local path in JSON — replace both with an opaque token (pass-4 finding)
- Undo's `_src_is_in_trash` duplicates `paths.is_inside_any_trash`; consolidate (pass-4 finding)
- Add `test_wholly_duplicated_excludes_archive_with_nested_encrypted_member` (pass-4 finding)
- Add end-to-end test that H7 partial-unique singleton reaches `Report.singletons` (pass-4 finding)
- H4 nested-archive spool + hash is two BLAKE3 passes; single-pass optimisation possible (pass-4 finding)
- `max_workers` config exposed but not wired; either wire it or make CLI print a "single-threaded in v0.1.1" notice
