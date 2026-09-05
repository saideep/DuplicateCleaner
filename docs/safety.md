# Safety design

DuplicateCleaner touches your filesystem and proposes deletions. Every guarantee below exists because the cost of an incorrect delete is high and the cost of an overly cautious "no-op" is low. This document is the design contract the Security audit checks against.

## Guarantees

### Trash, never `rm`

Every proposed discard is moved to the macOS Trash via the `send2trash` library, which calls the same Foundation API that Finder uses. DuplicateCleaner never calls `os.remove`, `os.unlink`, `shutil.rmtree`, or shells out to `rm`. There is no code path that permanently deletes a file.

Rationale: the Trash is a user-visible, user-managed staging area with a native "Put Back" affordance. Even after `dc undo` is gone or fails, you can restore from Finder as long as the Trash has not been emptied. `rm` has no such backstop.

### Dry-run by default on `dc apply`

`dc apply <report.json>` prints what it would move and exits. You must pass `--commit` explicitly to move anything.

Rationale: the default behavior of most CLI tools is to act. That is the wrong default for a tool whose action is deleting files. Making the safe path the default means a mis-typed command cannot cause data loss.

### Undo manifest is written before any move

For every commit run, DuplicateCleaner writes a manifest to `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json` *before* the first `send2trash` call. The manifest records every planned move as `(source_absolute_path, trash_relative_destination, sha256_of_source_at_time_of_move)`. The file is fsync'd to disk before any filesystem mutation happens.

Rationale: if the process crashes, is killed, or the machine loses power mid-run, the manifest still lists every intended move. `dc undo` can be re-run after the fact and will restore whatever the Trash still contains. Writing the manifest after the move would leave a gap where crashes lose the record.

### Excluded system paths (hard-coded)

These are never scanned, regardless of what you pass on the command line or what globs you configure:

- `~/Library` — macOS application state and caches. Deleting from here breaks apps.
- `/System` — SIP-protected macOS system directory.
- `/private` — system-managed private state (`/tmp`, `/var`, `/etc`).
- `/Volumes/*/System Volume Information` — Windows-formatted external drives.
- Any path containing `.git/objects` — deleting a git object breaks repository integrity.
- `.icloud` placeholder files — files not yet downloaded from iCloud; the placeholder itself is not a duplicate of anything.

These are enforced by the walker before any hashing happens. Configuration cannot disable them.

Rationale: these paths either belong to the OS, belong to a running application's private state, or (in the case of `.git/objects`) have integrity constraints that byte-equality alone cannot reason about. There is no legitimate reason a duplicate cleaner should touch them.

### Hard-link and APFS-clone handling

Files that share an inode (hard links) or share a clone lineage (APFS `clonefile`) already share their bytes on disk. Deleting one copy does not reclaim any space.

**Hard links** are fully handled in v0.1: any two members of a duplicate group with the same `(st_dev, st_ino)` are flagged as **informational**. If an entire group is one inode family, no keeper is proposed and no bytes are counted as reclaimable — `send2trash` on any one name would leave the shared inode alive under the other names. If a group mixes hardlinks with a separate byte-identical copy, every hardlinked member is informational and only the separate copy is eligible for the keeper role.

**APFS clones** are *not* detected in v0.1 — this ships in v0.1.1. Robust clone-family detection needs `getattrlist` with `ATTR_CMNEXT_CLONEID` (and equivalents) accessed via `ctypes`; the glue to get that right and portable across macOS releases is a v0.1.1 milestone. Until then:

- `dc scan` prints a warning after every scan reminding you that reclaim estimates may be over-reported on APFS-cloned trees.
- On a tree where you cloned rather than copied (`cp -c`, Finder duplicate on APFS, Time Machine local snapshots), two clones look byte-identical *and* have distinct inodes, so DuplicateCleaner will propose one for the Trash. Trashing one clone does not reclaim any space — it only detaches that name from the shared clone lineage.
- Workaround for v0.1: if you know a directory contains APFS clones, add it to `exclude_globs` until v0.1.1 ships.

Rationale: shipping a silent guess would violate the "the tool refuses to guess" contract. Making the limitation loud gives users the information to opt out.

### Symlinks not followed by default

Symbolic links are recorded but not traversed. Their targets are not scanned. This is configurable via `follow_symlinks` in the config file.

Rationale: on macOS, following symlinks can silently pull you into `/System`, `/private`, or arbitrary application-managed trees. Off-by-default keeps the scan bounded to what you explicitly named.

## Recovering after an accidental apply

If you commit a run and immediately realize you moved the wrong file:

1. Run `dc undo <path-to-manifest.json>`. The manifest path was printed at the top of the `apply --commit` output. The default location is `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json`.
2. If `dc undo` cannot find some of the files in the Trash (for example, you emptied it, or another cleanup tool ran), fall back to macOS Time Machine if you have a backup covering the run timestamp. The manifest lists exact absolute source paths.
3. As a last resort, use Finder's "Put Back" feature: open the Trash, right-click each file, and choose Put Back. Finder restores it to the original path recorded in the file's extended attributes at the time of trashing.

## Race conditions and mid-run failures

- **External drive detach mid-scan**: the scanner persists progress to the SQLite cache per directory. A re-run picks up where it left off. Partial results are never written to the report.
- **File modified between scan and apply**: `dc apply` re-stats every proposed discard and compares against the size and mtime recorded at scan time. If a file has changed, that group is skipped and logged. This prevents applying a stale plan to a file that was edited after the report was generated.
- **Two concurrent `dc apply` runs**: the SQLite cache uses `BEGIN IMMEDIATE` transactions and rejects a second writer. Only one commit run can be in flight at a time per machine.

## What Security is expected to audit

- Every filesystem write is either `send2trash` (in `apply/`) or a file rename inside a run-specific directory the tool owns.
- No shell-outs with user-controlled path arguments.
- Symlink handling honors the `follow_symlinks` config and does not escape excluded roots via a symlink target.
- Path exclusion matching is applied to the resolved absolute path, not the argument as typed.
- The undo manifest is written and fsync'd before the first mutation and is closed before subsequent moves so an incremental replay after a crash can complete.
