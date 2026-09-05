# Configuration reference

DuplicateCleaner reads its runtime configuration from a single TOML file. Scoring weights live in a separate JSON file. Both are created on first run and can be regenerated at any time.

## File locations

- Runtime config: `~/.config/duplicate_cleaner/config.toml`
- Scoring weights: `~/.config/duplicate_cleaner/weights.json`
- Cloud accounts (v0.2): `~/.config/duplicate_cleaner/accounts.toml`
- OAuth tokens (v0.2): `~/.config/duplicate_cleaner/tokens/<account_id>.json` (mode `0600`)
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

## Cloud accounts (v0.2)

Cloud accounts are registered by `dc auth add` and stored in a separate file, `~/.config/duplicate_cleaner/accounts.toml`. You never edit this file by hand — the `dc auth` subcommands manage it — but the schema is documented here so you know what is on disk.

### `accounts.toml` schema

```toml
schema_version = 1

[[accounts]]
id = "gdrive:personal"
type = "gdrive"
user_email = "me@gmail.com"
added_at = 2026-09-05T00:00:00Z

[[accounts]]
id = "gdrive:family"
type = "gdrive"
user_email = "partner@gmail.com"
added_at = 2026-09-05T00:00:00Z

[[accounts]]
id = "onedrive:main"
type = "onedrive"
user_email = "me@outlook.com"
added_at = 2026-09-05T00:00:00Z
```

Fields:

- `id` — the source ID used in `dc scan --sources local,<id>,...`. Format: `<type>:<label>`. `type` is one of `gdrive`, `onedrive`. `label` is the string you passed to `--label` on `dc auth add`; defaults to `personal`.
- `type` — `gdrive` or `onedrive`. `gphotos` and `icloud` are reserved for v0.6.
- `user_email` — pulled from the provider on first authorization. Informational only; matching is by account ID.
- `added_at` — timestamp of the `dc auth add` call. Informational only.

The tokens live in `~/.config/duplicate_cleaner/tokens/<id>.json` (mode `0600`, directory `0700`). See [cloud-oauth-setup.md](cloud-oauth-setup.md) for the full walk-through and the token file schema.

Add and remove accounts with:

```shell
dc auth add gdrive --label family
dc auth remove gdrive:family
```

## Cross-source preference (v0.2)

When a duplicate group spans local and cloud sources — or two cloud sources — DuplicateCleaner needs an order-of-preference for who wins the "keeper" role.

The default is unambiguous: **local wins any cross-source tie.** Any file under an `active_home` beats any cloud copy. This is a load-bearing invariant, not a tunable — a group that mixes local and cloud always has the local member as keeper and the cloud member(s) as discard proposals. See [safety.md](safety.md#cloud-safety) for the invariant statement.

For the multi-cloud case (two Google accounts, or Google plus OneDrive, with no local peer) the config exposes a preference list:

```toml
[cross_source_preference]
retained_cloud_order = [
    "gdrive:personal",
    "gdrive:family",
    "onedrive:main",
]
```

Semantics:

- The list is consulted only when a group contains no local member and at least two cloud members.
- The earliest-listed source that has a member in the group wins the keeper role.
- Cloud sources not in the list are considered lower-priority than every listed source. Ordering among unlisted sources is stable but undefined; add them to the list if you care.
- The list has no effect on any group that contains a local member. Local always wins that comparison.

If the section is omitted, all cloud sources are considered peers and the scoring rules break the tie (path hints, mtime, filename patterns). Set the list explicitly if you want a deterministic outcome across scans.

### Worked example

`report.json` shows a group with two members:

- `gdrive:personal://My Drive/photos/beach.jpg`
- `onedrive:main://Pictures/beach.jpg`

With the config above (`gdrive:personal` first), the Google Drive copy is proposed as keeper and the OneDrive copy is proposed for the recycle bin.

Reverse the order and the OneDrive copy wins.

If you add a local member `/Users/vaannada/Pictures/beach.jpg` under an active home, that member becomes the keeper regardless of `retained_cloud_order` — the local-wins rule takes precedence.

## Organizer (v0.3)

`dc organize` reads a small set of extra keys from the same `config.toml`. Every key has a sensible default; the section as a whole is optional. Full behaviour is documented in [docs/organize.md](organize.md).

### `organize_confidence_threshold`

Type: float in `[0.0, 1.0]`. Default: `0.75`.

Every file the classifier processes carries a confidence score. Below this threshold the file goes to `Unsorted/`. A file whose best rule scored at least `0.4` lands in `Unsorted/<Domain>/` (a domain-hinted subfolder for bulk triage); anything below `0.4` goes to a flat `Unsorted/`.

```toml
[organize]
organize_confidence_threshold = 0.75
```

Raise the value to send more files to Unsorted (safer, more manual review). Lower it to accept more classifications automatically (faster, more chance of surprises).

### `organize_dir_mode`

Type: integer (octal literal recommended). Default: `0o755`.

Mode passed to `os.makedirs` when the apply step creates a destination folder. Default `0o755` matches the macOS umask and keeps shared-drive workflows unbroken (a folder under `/Volumes/Family/Photos/` needs to be readable by other admin-group members).

```toml
[organize]
organize_dir_mode = 0o755
```

Set to `0o700` if the target tree lives entirely under your home and you want owner-only access.

### `rename_policy`

Type: string. One of `preserve`, `date_prefix`, `date_event_prefix`. Default: `preserve`.

Controls whether the destination filename is derived from the source filename or from the file's classification.

- `preserve` — the destination filename is the source filename, byte-for-byte. The tool never mutates filenames in this mode. This is the user-locked default.
- `date_prefix` — prefix the filename with `YYYY-MM-DD_` from the classified year, month, and day.
- `date_event_prefix` — for photo or video event cohesions, prefix with the event date span; otherwise behaves like `date_prefix`.

```toml
[organize]
rename_policy = "preserve"
```

### `event_gap_hours`

Type: integer. Default: `12`.

Time gap in hours that starts a new photo or video event during clustering. Consecutive captures within the gap belong to the same event; a larger gap starts a new one.

```toml
[organize]
event_gap_hours = 12
```

### `min_event_photos`

Type: integer. Default: `5`.

Minimum number of items required for an event cluster to get its own folder. Smaller events fall back to the monthly rule (`Photos/{year}/{yyyy_mm}/`).

```toml
[organize]
min_event_photos = 5
```

### `enforce_dedup_ordering`

Type: boolean. Default: `false`.

Governs how `dc organize discover` reacts when the most recent `dc scan` still has pending proposed discards. `false` prints a soft warning and asks whether to continue. `true` refuses with an error message that includes the exact `dc apply` command to run first.

```toml
[organize]
enforce_dedup_ordering = false
```

Set to `true` in shared or automated environments where organizing over stale dedup output is a bug rather than a choice.

### `[organize.domains]` — per-domain overrides (reserved)

Reserved section for per-domain overrides once real-world usage identifies which domains benefit. The table structure lets you disable a domain, override a subfolder template, or adjust the domain's confidence weighting without editing code.

```toml
[organize.domains]
# Reserved for v0.3-post release. Examples that will be accepted:
# hr = { subfolder_template = "Employer/{employer}/{year}" }
# personal = { disabled = true }
```

If you configure this section today, it is loaded silently and applied where the code has caught up. Keys the tool does not yet understand are logged at DEBUG and otherwise ignored, so a config that anticipates a future release is safe to check in now.

### `[organize.books.topics]` — book topic list

Extensible list of book topics used by the `Media/Books/{topic}/` rule. Each entry names a topic and lists keywords that identify it in a PDF's first-page text.

```toml
[organize.books.topics]
python      = ["Python", "asyncio", "pytest", "Django"]
kubernetes  = ["Kubernetes", "kubectl", "Helm", "container orchestration"]
finance     = ["accounting", "double-entry", "balance sheet"]
```

Topics you add extend the shipped list. Topics you redefine (by using the same key as a shipped topic) replace the shipped keywords for that key.

### `[organize.finances.vendors]` — user vendor hints

Optional user hints layered on top of the shipped vendor lists for receipts, statements, and investments. Same shape as the shipped dictionaries.

```toml
[organize.finances.vendors]
receipts     = ["Etsy", "Trader Joe's"]
statements   = ["ExampleBank Regional"]
investments  = ["Betterment", "Wealthfront"]
```

Additions are unioned with the shipped list. Entries you list here are matched with the same case-insensitive substring rules used for the shipped names.

### Worked example

A user who wants Unsorted to be a wider net (so they review more manually), enables geocoding by leaving the CLI flag on their own responsibility, prefers organized folders to be owner-only, and adds two custom book topics:

```toml
active_homes = ["/Users/vaannada"]
min_size_bytes = 4096

[organize]
organize_confidence_threshold = 0.85
organize_dir_mode = 0o700
rename_policy = "preserve"
event_gap_hours = 8
enforce_dedup_ordering = true

[organize.books.topics]
sre        = ["site reliability", "SRE", "SLO", "error budget"]
philosophy = ["Nietzsche", "Kant", "Stoicism"]
```

## Tokens directory (v0.2)

```
~/.config/duplicate_cleaner/tokens/    (mode 0700)
├── gdrive:personal.json               (mode 0600)
├── gdrive:family.json                 (mode 0600)
└── onedrive:main.json                 (mode 0600)
```

Each token file contains the access token, refresh token, expiry, scopes, and client ID for one account. The directory is created with mode `0700` (owner-only) and each file with mode `0600`. DuplicateCleaner re-checks permissions on every read and refuses to use a token file that is group- or world-readable.

Do not commit these files. Do not paste them into bug reports or logs. Treat them like SSH private keys — a leaked refresh token grants ongoing access to your Drive or OneDrive until you revoke it at the provider's connected-apps page.

Full details, revocation URLs, and the JSON schema are in [cloud-oauth-setup.md](cloud-oauth-setup.md#token-storage).
