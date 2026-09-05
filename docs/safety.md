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

### Cloud safety (v0.2)

Cloud sources — Google Drive and OneDrive Personal — are held to the same safety invariants as local scans, with a small set of additional rules specific to remote APIs.

#### Cloud discards go to cloud trash, never hard-delete

`dc apply --commit` moves cloud entries to the provider's own trash / recycle bin:

- Google Drive: `files.update(fileId=..., body={"trashed": True})`. The file lands in the Drive trash where you can restore it from the web UI at [drive.google.com/drive/trash](https://drive.google.com/drive/trash) for 30 days by default (Google's own retention).
- OneDrive Personal: `DELETE /me/drive/items/{id}` which Microsoft documents as "moves to the recycle bin." You can restore it from the OneDrive web UI at [onedrive.live.com](https://onedrive.live.com/) recycle bin.

DuplicateCleaner never issues a hard-delete API call. There is no code path that permanently deletes a cloud file. The OAuth scopes it requests are read-and-trash only:

- Google: `https://www.googleapis.com/auth/drive.file` — grants access to files the tool created or the user explicitly picked. Includes trash and untrash. Does not include the broader `drive` scope required for permanent deletion.
- Microsoft Graph: `Files.ReadWrite` + `offline_access` — grants read, write, and move-to-recycle-bin. Not `Sites.FullControl.All` or the delegated variants needed for hard-delete.

The scopes are chosen so hard-delete is not physically possible from the tool. This is a load-bearing invariant, verified at CI-time and repeated in the audit log.

#### Undo restores via API

`dc undo` dispatches per-entry by `source_id`. For a cloud entry it calls the source's `restore_from_trash`:

- Google Drive: `files.update(fileId=..., body={"trashed": False})`. The file returns to its original path in Drive.
- OneDrive Personal: `POST /me/drive/items/{id}/restore`. If Personal does not support that endpoint on your account, DuplicateCleaner falls back to enumerating the recycle bin and restoring from there. If neither path works, `dc undo` prints a clear message pointing you at the OneDrive web UI to restore manually. The failure is recorded per-entry; other entries still restore.

The undo manifest records the cloud file ID and the source ID for every entry, so a re-run after a crash still knows which API call to make.

#### Shared cloud files are informational-only

Any file the provider reports as owned by someone else (Google `owners` field does not include the account's own address, or Microsoft `createdBy.user.id` differs from `/me`) is treated as **informational only**. The scanner never proposes it for deletion, regardless of scoring signals. This is enforced upstream of the scorer — no weight can override it.

Rationale: touching a shared file affects other people. A dedupe tool that trashes another user's file is a failure mode we categorically rule out. Even if the file is byte-identical to a local copy you own, the shared copy remains and you delete your local copy (or nothing).

Behaviour:

- In the HTML report, shared cloud files appear in the informational band alongside hardlink and APFS clone entries.
- If the entire group consists of shared cloud files (no non-informational member), no keeper is proposed and reclaim is 0.
- The `--skip-shared` flag on `dc scan` skips shared files entirely — they do not appear in the report at all.

#### Local wins any cross-source tie

For a group that contains both a local member (under an `active_home`) and a cloud member, the local member is always the proposed keeper. The `cloud_when_local_exists` scoring signal contributes -3 to every cloud member in such a group; the `+4` active-home bonus on the local member ensures the local wins.

This means the typical outcome of enabling a cloud source is:

- Cloud files that already have a local copy get proposed for cloud-trash.
- Cloud files that are unique to the cloud remain unchanged (singletons across sources are never a discard candidate).
- Shared cloud files never move regardless of local peers.

#### Cloud tokens are as sensitive as SSH keys

OAuth refresh tokens grant ongoing access to your Drive or OneDrive. Protect the token files at `~/.config/duplicate_cleaner/tokens/<id>.json`:

- The tokens directory is created with mode `0700` (owner-only). Each token file is written with mode `0600`.
- DuplicateCleaner re-checks permissions on every read and refuses to use a token that is group- or world-readable, printing a `chmod 600` fix.
- Do not commit token files. Do not paste them into logs, bug reports, or support threads.
- If a token leaks, revoke at the provider's connected-apps page ([Google](https://myaccount.google.com/permissions), [Microsoft](https://account.live.com/consent/Manage)) and run `dc auth add` again.

#### Data-safety invariants that apply to cloud entries

The same invariants that apply to local moves apply to cloud entries:

- **Manifest is written before the first move.** The mover writes the manifest with tempfile + fsync + `os.replace` + parent-dir fsync before issuing any `move_to_trash` API call. A crash mid-run leaves the manifest with every planned move intact; `dc undo` replays whatever the cloud trash still holds.
- **Per-entry re-verify.** Before trashing a cloud file, the mover fetches current metadata and compares etag against what the report recorded. If the etag has changed (someone else edited the file since the scan), the run aborts on drift — it does not skip-and-continue.
- **Singletons never discarded.** A file that appears exactly once across every configured source is never a discard candidate. Enforced in the scorer and re-checked in the mover.
- **Per-entry failures do not abort the run.** A single `NotFoundError` (someone else emptied the trash), `RateLimitError` (429 with no `Retry-After` left), or `AuthExpiredError` (token could not refresh) is recorded per-entry and the mover continues with the rest.

#### Summary invariants (v0.2)

Two additions to the safety model, tracked in [AUDIT_LOG.md](AUDIT_LOG.md#invariants-do-not-weaken):

- **Shared cloud files informational-only.** Any file the provider reports as owned by someone else is never proposed for deletion.
- **Cloud discards go to cloud trash; undo restores via API.** No code path hard-deletes a cloud file. Undo dispatches per-entry by `source_id` and reverses the same API call.

### Organize safety (v0.3)

`dc organize` inherits the whole safety envelope from `dc apply` and adds a small set of rules specific to moving files into a proposed folder tree. Behaviour is documented in [docs/organize.md](organize.md).

#### Dry-run default and `--commit` gate

`dc organize apply <plan.json>` prints the planned moves and exits. You must pass `--commit` explicitly for any file to move. This is the same reversed default as `dc apply`: the safe path is what you get when you type nothing extra.

#### Undo manifest is written before any move

Every commit writes a manifest to `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json` before the first move. The manifest records the source path, the destination path, the pre-move hash, and — for cross-volume moves — the Trash-relative source path. The manifest uses the same tempfile + `fsync` + `os.replace` + parent-dir fsync pattern as `dc apply`. A crash mid-run leaves the manifest with every planned move intact; `dc organize undo` replays what the filesystem or Trash still holds.

#### Cohesive units move atomically

Music albums, book series, git projects, and photo or video event clusters move as one unit. Every plan entry inside a cohesive unit carries a `cohesion_group` field in the JSON. `dc organize apply`:

1. Groups plan entries by `cohesion_group`.
2. Verifies every member of a group targets the same destination folder.
3. Aborts the run before the first move if any member differs and `--split-cohesive-units` was not passed. The error message lists the violating entries.

This is the same shape of guard the mover uses for whole-archive proposals: the invariant lives in one place and cannot be silently sidestepped. Splitting a cohesive unit is possible — you just have to say so with `--split-cohesive-units`.

#### Directory permissions default to 0o755

`os.makedirs(dest, mode=0o755, exist_ok=True)` matches the macOS umask and keeps shared-drive workflows unbroken. A folder under `/Volumes/Family/Photos/` needs to be readable by other admin-group members; a 0o700 default would break those workflows silently. The `organize_dir_mode` config knob is available for users whose target tree lives entirely under their home and who want owner-only access.

#### Path collision on move

If `<dest>/<filename>` already exists at move time:

1. If the existing file hashes identical to the source (BLAKE3, streamed), the tool skips the move, logs `already-present-at-dest` in the manifest, and does not delete the source. This is `organize`, not `dedup` — dedup happens separately upstream in the workflow.
2. Otherwise the tool appends `_<hash8>` to the stem, where `<hash8>` is the first eight hex characters of the source's BLAKE3 hash. `receipt.pdf` becomes `receipt_a3f1b2c9.pdf`. The collision is logged under `collisions[]` in the manifest.

The rename policy for user-supplied filenames is a separate concern and is user-locked to `preserve` by default. See below.

#### PDF classification is a hint, not a guarantee

The PDF classifier scores each first-page against a keyword lexicon and emits a `pdf.class.<label>` signal above threshold `0.6`. That signal feeds one classifier rule out of many; a file whose classifier score is low but whose filename or Info dict strongly suggests a class still gets classified. And a file whose classifier is uncertain drops below the confidence threshold and goes to Unsorted rather than to a specific domain. Every signal that fired is written to the plan JSON so you can audit exactly what the classifier saw.

#### Files never moved by organize

The following files are outside the domain of `dc organize` and are never proposed for movement:

- **Hardlinked-informational files.** Any member of a hardlink family flagged as informational by the walker is not proposed for a move, for the same reason it is not proposed for deletion — the on-disk extents are already shared, and moving one link changes where the shared inode is reachable from.
- **Archive members.** `dc organize` operates on filesystem objects. An entry inside a `.zip` or tarball is never proposed for movement — only whole archives are.
- **Singletons.** A file with only one copy across the discovered set is not a discard candidate, and if it is not classified into a domain above the confidence threshold it stays in Unsorted rather than getting moved to a maybe-wrong domain.
- **Cohesive-unit peers when one peer opts out.** If any member of a cohesion group is marked "leave in place" or targets a different destination, apply refuses to move the group without `--split-cohesive-units`.

#### Rename policy is user-locked

The default `rename_policy` is `preserve`. In this mode the tool never mutates filename bytes. The `date_prefix` and `date_event_prefix` modes exist for users who explicitly want the classification's date to be visible in the filename; they only ever run when the user has opted in via the config. Filename bytes are user territory.

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
