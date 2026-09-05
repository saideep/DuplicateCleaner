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

## Deferred to v0.1.2

- Bundle filename Unicode NFC/NFD normalization (HFS+ ↔ APFS migration case)
- Singleton hash markers `"singleton-by-size:<size>:<path>"` AND `"singleton-by-partial:<partial>:<path>"` currently leak local path in JSON — replace both with an opaque token (pass-4 finding)
- Undo's `_src_is_in_trash` duplicates `paths.is_inside_any_trash`; consolidate (pass-4 finding)
- Add `test_wholly_duplicated_excludes_archive_with_nested_encrypted_member` (pass-4 finding)
- Add end-to-end test that H7 partial-unique singleton reaches `Report.singletons` (pass-4 finding)
- H4 nested-archive spool + hash is two BLAKE3 passes; single-pass optimisation possible (pass-4 finding)
- `max_workers` config exposed but not wired; either wire it or make CLI print a "single-threaded in v0.1.1" notice
