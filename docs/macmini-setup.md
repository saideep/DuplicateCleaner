# Mac mini setup guide

Full setup for a fresh Mac mini running macOS 15 Sequoia or later. The reference hardware is an M4 or M4 Pro, but any Apple Silicon Mac mini (M1, M2, M4) works. All wheels used by DuplicateCleaner are Apple Silicon native.

## Prerequisites

Install the Apple Command Line Tools. This gives you `git`, the compilers Homebrew needs, and the standard developer headers.

```shell
xcode-select --install
```

Accept the license prompt and wait for the installer to finish before continuing.

## Step 1: Install Homebrew

Run the official installer. Follow any prompts about adding Homebrew to your shell path — on Apple Silicon this typically means adding `eval "$(/opt/homebrew/bin/brew shellenv)"` to `~/.zprofile`.

```shell
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Verify:

```shell
brew --version
```

## Step 2: Install system dependencies

```shell
brew install python@3.12 chromaprint ffmpeg uv
```

What each one is for:

- `python@3.12` — the interpreter DuplicateCleaner targets. Pinned to the 3.12.x series.
- `uv` — the package and virtual environment manager used to install and lock Python dependencies. Fast and deterministic.
- `chromaprint` — the C library that backs `pyacoustid` for audio fingerprinting. **Only required once v0.5 (audio near-duplicate) ships. Safe to skip for v0.1.**
- `ffmpeg` — used to extract video keyframes for video near-duplicate comparison. **Only required once v0.5 ships. Safe to skip for v0.1.**

If you want the minimum install for v0.1, run:

```shell
brew install python@3.12 uv
```

## Step 3: Clone the repository and install

```shell
git clone <your-repo-url> ~/DuplicateCleaner
cd ~/DuplicateCleaner
uv sync
```

`uv sync` reads `pyproject.toml` and `uv.lock`, creates `.venv/` inside the project, and installs the exact pinned dependency versions. The lockfile guarantees you get the same versions on every machine.

Verify the CLI is available:

```shell
uv run dc --help
```

## Step 4: First-run config

Generate the default config file:

```shell
uv run dc init
```

This writes `~/.config/duplicate_cleaner/config.toml` with default values, then prints the path so you can open it.

Before your first scan you **must** edit `active_homes` in that file. Set it to your live user directory:

```toml
active_homes = ["/Users/vaannada"]
```

### Why this matters

You likely have multiple `~/Documents`, `~/Desktop`, and `~/Downloads` folders scattered across old backups — one on the internal drive, one under `/Volumes/OldBackup/Users/vaannada/`, maybe another under `~/OldMacBackup/`. macOS gives all of them the same folder names, so a naive tool can't tell which is your real live workspace and which is archival.

`active_homes` is how you declare, explicitly, which paths represent your current live home directory. Files under any active home get a `+4` scoring bonus for "live"; files that look like a home directory but live outside every declared active home get `-4` for "archived." The tool refuses to guess. See [docs/config.md](config.md) for the full explanation and a worked example.

## Step 5: First scan (dry-run)

Run your first scan against a real directory. `dc scan` never modifies files — it only writes a report.

```shell
uv run dc scan ~/Documents ~/Desktop --report ~/dc-report
```

Output:

- `~/dc-report/report.html` — the human review view.
- `~/dc-report/report.json` — the machine-readable decision list. You can hand-edit this before `apply`.

## Step 6: Review the HTML report

Open the report in your browser:

```shell
open ~/dc-report/report.html
```

At the top you see a reclaim summary: total bytes recoverable and the group count by category. Below that, each duplicate group shows:

- The proposed keeper, highlighted.
- The proposed discards, crossed out, with a per-signal score breakdown (for example: `backup in path: -8, older mtime: -3, deeper: -1`).
- Thumbnails or previews inline when applicable.
- A radio group to override the keeper. Overrides go into `report.json` in later milestones — in v0.1, edit the JSON directly if you want to change a keeper.

*Screenshot: report top-level summary — TODO.*

*Screenshot: single duplicate group with score breakdown — TODO.*

## Step 7: Apply the plan

Apply is dry-run by default. Run it first with no flags to see what would move:

```shell
uv run dc apply ~/dc-report/report.json
```

Once you're satisfied, pass `--commit`:

```shell
uv run dc apply ~/dc-report/report.json --commit
```

This writes an undo manifest to `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json` before moving anything, then sends each proposed discard to the macOS Trash via `send2trash`.

## Step 8: Undo if needed

If a run moved something you did not want moved, restore it:

```shell
uv run dc undo ~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json
```

`dc undo` reads the manifest and restores every file from the Trash back to its original path. See [docs/safety.md](safety.md) for the fallback if the Trash has already been emptied.

## Troubleshooting

### Permission denied on `~/Library`

`~/Library` is excluded by default — DuplicateCleaner never scans it. If you passed `~/Library` explicitly on the command line, remove it. macOS also gates a handful of subpaths under System Integrity Protection; those are excluded whether you pass them or not.

### External drive detached mid-scan

The scanner persists partial results per directory to the SQLite cache. Re-running the same `dc scan` command after reattaching the drive picks up where it left off. Files already hashed are not re-hashed.

### Corrupted config

If the config file becomes malformed and `dc scan` refuses to start, regenerate it:

```shell
uv run dc init --force
```

This overwrites `~/.config/duplicate_cleaner/config.toml` with the defaults. Remember to re-set `active_homes` before scanning.

### `dc` command not found

You need to prefix commands with `uv run` unless you have activated the venv (`source .venv/bin/activate`). Inside the venv, `dc` works directly.

### Homebrew python conflicts with system python

macOS ships its own Python, which is not what you want. Always invoke DuplicateCleaner through `uv run dc` — `uv` uses the interpreter it manages, not the system one.

## Version pins

- Python: 3.12.x (see `requires-python = ">=3.12,<3.13"` in `pyproject.toml`).
- macOS: 15 Sequoia or later.
- Python package versions: pinned via `pyproject.toml` and locked in `uv.lock`. Do not edit either file by hand.
- Homebrew formulae: latest stable at install time. Chromaprint and ffmpeg are only used by the v0.5 audio and video comparators; their exact versions do not matter for v0.1.
