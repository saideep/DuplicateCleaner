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

**Hard links** are handled via `(st_dev, st_ino)` matching: any two members of a duplicate group with the same inode are flagged as **informational**. If an entire group is one inode family, no keeper is proposed and no bytes are counted as reclaimable — `send2trash` on any one name would leave the shared inode alive under the other names. If a group mixes hardlinks with a separate byte-identical copy, every hardlinked member is informational and only the separate copy is eligible for the keeper role.

**APFS clones** are detected via `getattrlist` with the `ATTR_CMNEXT_CLONEID` attribute (accessed through `ctypes` against `libSystem`). Each file that is part of a clone family reports a non-zero clone ID; every file that shares the same clone ID shares the same on-disk extents. DuplicateCleaner records the clone ID on the two-pass scan alongside `(st_dev, st_ino)` and marks matching-clone-ID members of a duplicate group as **informational**, exactly like hard links. Consequences:

- On a tree produced with `cp -c`, Finder Duplicate on APFS, or Time Machine local snapshots, all clones of a file end up in the same informational band and none is proposed for the Trash.
- Reclaim totals reflect only the bytes you would actually recover — no more over-reporting on cloned trees.
- If a clone family and a separately-copied byte-identical file both exist, the separate copy is eligible for the keeper or discard role and the clones remain informational.

The clone ID probe is a two-pass informational marker: pass one collects `(size, path, inode, clone_id)` during the walk; pass two, after hash-based grouping, promotes any duplicate-group member whose `(st_dev, st_ino)` or clone ID matches another member to informational.

Rationale: shared on-disk extents mean a delete cannot reclaim space. Proposing such a delete would mislead the user about the outcome.

### Archives

Archive formats `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, and `.tar.xz` are recursed into during scanning so DuplicateCleaner can tell you that two archives contain the same set of files. The rules that guard this recursion:

- **Whole-archive proposals only.** DuplicateCleaner proposes discards at the archive level. It never proposes to delete an individual member inside an archive. Editing a `.zip` to remove one entry is a mutation the tool refuses to make.
- **Encrypted and corrupt archives are skipped.** If a member cannot be read (encrypted, truncated, or otherwise malformed) the archive is skipped and an entry is written to the report noting the reason. Nothing about the archive is proposed for deletion.
- **Nesting depth cap.** Recursion into archives-inside-archives is capped at `max_archive_depth` (default `2`). Beyond the cap, the inner archive is hashed as an opaque blob.
- **Exclusions do not follow inside archives.** `exclude_globs` matches on filesystem paths. It is not consulted for archive members. If you need to keep an archive out of scope entirely, exclude the archive itself by its filesystem path.

Rationale: an archive is a single filesystem object. The only safe deletion granularity is the whole archive.

### Bundles

macOS bundles are directory structures the operating system presents to the user as a single item. DuplicateCleaner treats the following extensions as atomic: `.app`, `.pages`, `.numbers`, `.keynote`, `.rtfd`, `.sparsebundle`, `.xcodeproj`, `.playground`, `.framework`, `.bundle`. The walker never descends into these directories. The bundle's contents are hashed as a single unit (an ordered digest of members) and duplicate bundles are proposed as whole units.

Rationale: users think of `Pages.app`, `MyDoc.pages`, and `MyProject.xcodeproj` as files, not folders. Deleting individual internal resources would break the bundle. The list of extensions is exposed via `bundle_extensions` in the config so you can add or remove entries.

### System monitoring

`dc scan` is designed to run on a machine that is also being used interactively.

- **Pre-scan disk check.** Before the walker starts, DuplicateCleaner checks free space on the cache volume (where the SQLite cache lives). If free space is below `min_free_disk_gb` (default `5`), the scan refuses to start. Free space, or lower the threshold in config, then retry.
- **Throttling.** A `psutil`-based monitor samples system CPU. When system CPU exceeds `throttle_on_cpu_pct` (default `85`), the hashing pool pauses briefly to yield the machine. Set `throttle_on_cpu_pct = 100` in config to disable throttling.
- **Politeness.** The scan process re-nices itself via `os.nice(10)` and issues a best-effort `taskpolicy -c background` (macOS-only, ignored if unavailable). Both mechanisms tell the scheduler to prefer foreground applications.
- **Live progress.** The progress bar reports live CPU %, RAM MB, free disk GB, and files processed so you can see the scan's footprint without leaving Terminal.

Overrides:

- Increase worker count with `max_workers` in the config (default `os.cpu_count() // 2`).
- Disable CPU throttling with `throttle_on_cpu_pct = 100`.
- Lower the disk threshold with `min_free_disk_gb = 1` if you understand the risk of running the cache volume near-empty.

Rationale: a duplicate cleaner is a background hygiene task, not a foreground compute job. Defaults keep the machine responsive; overrides are available for the small number of users who want to hand the whole machine to a scan.

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
