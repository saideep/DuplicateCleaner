# Configuration reference

DuplicateCleaner reads its runtime configuration from a single TOML file. Scoring weights live in a separate JSON file. Both are created on first run and can be regenerated at any time.

## File locations

- Runtime config: `~/.config/duplicate_cleaner/config.toml`
- Scoring weights: `~/.config/duplicate_cleaner/weights.json`
- SQLite cache: `~/.cache/duplicate_cleaner/cache.db`
- Undo manifests: `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json`

Create or reset the config with `dc init`. Pass `dc init --force` to overwrite an existing file.

## Config keys

### `active_homes`

Type: list of absolute path strings. Required — `dc scan` refuses to run if this is unset or empty.

Declares which paths on this machine represent your live user home directory. Any file under an active home is scored `+4` in the "right home" rule. Any file that looks like it lives in a home directory (matches `**/Desktop`, `**/Documents`, `**/Downloads`, `**/Pictures`, `**/Movies`, or `**/Music`) but is **not** under an active home is scored `-4`, i.e. treated as archival.

Example:

```toml
active_homes = ["/Users/vaannada"]
```

Multiple active homes are allowed if you legitimately work out of more than one:

```toml
active_homes = ["/Users/vaannada", "/Users/vaannada-work"]
```

### `min_size_bytes`

Type: integer. Default: `4096`.

Files smaller than this are skipped during scanning. The default matches the plan's 4 KB threshold. Tiny files rarely represent recoverable disk space and dominate the "same size" bucket with false coincidences.

```toml
min_size_bytes = 4096
```

### `exclude_globs`

Type: list of glob patterns. Default: `[]` (in addition to the hard-coded exclusions listed in [docs/safety.md](safety.md)).

Patterns match against the full absolute path. Standard `fnmatch`-style syntax with `**` for recursive match.

```toml
exclude_globs = [
    "**/node_modules/**",
    "**/*.tmp",
    "**/.venv/**",
    "**/build/**",
]
```

The hard-coded system exclusions (`~/Library`, `/System`, `/private`, iCloud placeholders, `.git/objects`) always apply and cannot be turned off by config.

### `follow_symlinks`

Type: boolean. Default: `false`.

When `false`, symlinks are recorded but not traversed and their targets are not scanned. When `true`, symlinks are followed and their targets are scanned as if they were regular files. Cycles are broken by tracking visited inode-plus-device pairs.

Leave this off unless you have a specific reason. Following symlinks on macOS can pull you into `/System`, `/private`, or user library trees you didn't intend to scan.

```toml
follow_symlinks = false
```

## Archive handling

### `max_archive_depth`

Type: integer. Default: `2`.

Maximum recursion depth for reading archives-inside-archives. A top-level `.zip` on disk is depth `0`; a `.tar.gz` inside that `.zip` is depth `1`; a `.zip` inside that tarball is depth `2`. Beyond the cap, the innermost archive is hashed as an opaque blob and its members are not enumerated.

Supported archive formats: `.zip`, `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz`.

```toml
max_archive_depth = 2
```

Encrypted or corrupt archives are skipped and reported. Only whole archives are ever proposed for deletion — individual members are never proposed on their own. See [docs/safety.md](safety.md#archives) for the full contract.

## Bundle handling

### `bundle_extensions`

Type: list of extension strings (including the leading dot). Default:

```toml
bundle_extensions = [
    ".app",
    ".pages",
    ".numbers",
    ".keynote",
    ".rtfd",
    ".sparsebundle",
    ".xcodeproj",
    ".playground",
    ".framework",
    ".bundle",
]
```

Directories whose name ends in one of these extensions are treated as atomic units. The walker does not descend into them. Their contents are hashed as an ordered digest of members and duplicate bundles are proposed for the Trash as a single item.

Add extensions if your workflow uses other bundle-shaped directories (for example: `.logicx`, `.band`). Remove extensions only if you are confident the corresponding tool can survive partial-bundle deletions — for most macOS bundle types the answer is no.

## System monitoring

### `max_workers`

Type: integer. Default: `os.cpu_count() // 2`.

Number of parallel hash workers. The default halves the logical CPU count, leaving headroom for foreground apps on the same machine. Set higher to prioritize scan throughput; set lower to leave more capacity for other work.

```toml
max_workers = 4
```

### `throttle_on_cpu_pct`

Type: integer (0–100). Default: `85`.

When system CPU usage (sampled via `psutil`) exceeds this percentage, hashing pauses briefly to yield the machine. Setting the value to `100` effectively disables throttling.

```toml
throttle_on_cpu_pct = 85
```

To disable throttling entirely:

```toml
throttle_on_cpu_pct = 100
```

### `min_free_disk_gb`

Type: integer. Default: `5`.

Pre-scan free-space threshold on the cache volume (where `~/.cache/duplicate_cleaner/cache.db` lives). `dc scan` refuses to start if free space is below this many gigabytes. This protects against filling the disk with cache and report data during a large scan.

```toml
min_free_disk_gb = 5
```

Lower the threshold only if you understand the consequences of running the cache volume near-empty. Freeing space is almost always the right answer.

## The multi-home problem

macOS's directory conventions were designed for a single user home directory per machine. In practice, if you've done any of the following, that assumption breaks:

- Restored an old Mac's home directory to an external drive as a backup.
- Migrated between machines and kept the previous home directory around "just in case."
- Copied `~/Documents` or `~/Desktop` to another location before wiping a disk.

You now have multiple directories named `Documents`, `Desktop`, `Downloads`, and so on. They contain overlapping files. A naive duplicate finder cannot tell which is your real live workspace and which is a backup you should collapse.

Consider a concrete example. You have both:

- `/Users/vaannada/Documents/foo.pdf` — the live file, opened yesterday.
- `/Volumes/OldBackup/Users/vaannada/Documents/foo.pdf` — the same file from a 2023 backup.

Both are named `foo.pdf`, both are inside a `Documents` directory, and byte-for-byte they are identical. With `active_homes = ["/Users/vaannada"]` set:

- The live copy scores `+4` (under an active home) plus a small bonus for a more recent mtime — call it `+3` — for a total of `+7`.
- The backup copy scores `-4` (in a "home-shaped" folder but outside every active home) plus `-2` (on external drive when an internal copy exists) for a total of `-6`.

The live copy wins by `13` points and becomes the proposed keeper. The backup is proposed for the Trash. If you disagree with any single call, you edit `report.json` before running `dc apply`.

This is why `active_homes` is required and has no default. The tool refuses to guess which `~/Documents` is real, because guessing wrong loses data.

## Scoring weights

Weights live at `~/.config/duplicate_cleaner/weights.json`, created with defaults on first run. Each signal contributes a signed weighted delta to a file's score inside a duplicate group. The highest-scoring member becomes the proposed keeper.

Default signals:

| Signal | Default weight | Direction |
|---|---|---|
| Folder name contains `backup`, `old`, `copy`, `archive`, `duplicate`, `Copy of` | -8 | discard |
| Filename matches patterns like `foo (1).ext`, `foo copy.ext`, `foo-2.ext` | -6 | discard |
| Path is under a declared active home | +4 | keep |
| Path matches a home-like folder name but is outside every active home | -4 | discard |
| Inside a Downloads folder even when Downloads is active | -2 | discard (transit zone) |
| Deeper path (more segments) | -0.5 per level | prefer shallower |
| Most recent mtime among the group | +3 | keep |
| Oldest mtime — tie-breaker only | +1 | keep |
| Inside a git repo with a clean working tree | +2 | keep |
| On an external drive when a copy exists on the internal drive | -2 | discard external copy |
| Larger file size (matters for near-duplicates: higher-resolution image, higher-bitrate audio) | +2 | keep the fuller version |

Inspect and reset:

```shell
dc weights show
dc weights reset
```

Editing `weights.json` by hand is supported. Values are clamped to a sane range at load time so no single signal can dominate. Adaptive weight learning ships in v0.7 — until then, weights only change when you edit the file or run `dc weights reset`.

## Exclude glob syntax

Globs use the standard `fnmatch` extended syntax:

- `*` matches any character except path separators.
- `**` matches zero or more path segments.
- `?` matches a single character.
- `[abc]` matches one character from the set.

Examples:

```toml
exclude_globs = [
    "**/node_modules/**",   # any node_modules directory anywhere
    "**/*.tmp",             # every .tmp file
    "**/.DS_Store",         # macOS Finder metadata
    "**/build/**",          # build outputs
    "/Volumes/Time Machine Backups/**",  # a specific mount
]
```

All globs match against the absolute path of the file being considered. If any pattern matches, the file is skipped.
