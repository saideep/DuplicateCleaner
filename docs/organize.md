# Organize (v0.3)

`dc organize` proposes and applies a domain-aware folder layout for the files that survive dedup. It complements `dc scan` and `dc apply`: dedup removes byte-identical copies, and organize gives what remains a home under a taxonomy tuned to your data — payslips, receipts, photos, music, projects, and so on. It never invents new copies, and it moves files only when you pass `--commit`. This is v0.3 and is in development; the surface described here is the design contract the tool builds against.

## Prerequisites

- `active_homes` set in `~/.config/duplicate_cleaner/config.toml` (same as for scan).
- Ideally, a recent `dc scan` + `dc apply` run against the roots you plan to organize. Organizing over a pile of duplicates works, but you will move the same content into many folders. If the last scan still has pending proposed discards, `dc organize discover` prints a soft warning and offers to continue. Set `enforce_dedup_ordering = true` in the config to upgrade this to a refusal.

## The three phases

`dc organize` is a read-plan-review-apply tool, exactly like `dc scan` + `dc apply`:

1. `dc organize discover` — walks the source(s), extracts signals (EXIF, ID3, PDF text, filename patterns, git markers), classifies each file into a domain, and writes a plan file. Read-only. No filesystem changes outside the plan artifacts and the SQLite cache.
2. `dc organize review` — an interactive Rich-based TUI (v0.3-a) that lets you edit the plan without hand-editing JSON. Or you can edit the plan JSON directly in your `$EDITOR` — the apply step re-validates the schema.
3. `dc organize apply` — dry-run by default. `--commit` creates target folders and moves files. An undo manifest is written before the first move so any run can be reversed.

### Worked example: an unsorted `~/Downloads`

Suppose `~/Downloads` contains three payslip PDFs, one Amazon receipt PDF, a folder of 50 Bali trip photos, a folder of a seven-track Miles Davis album, and one PDF the tool cannot classify.

```
uv run dc organize discover ~/Downloads --dest ~/organized --plan ~/plan.json
```

Discover output (abridged):

```
Scanned 61 files. Proposed layout:

  HR/Payslips/2024/                          3 files    confidence >= 0.83
  Finances/Receipts/2024/Amazon/             1 file     confidence 0.80
  Photos/2024/2024-06-15_to_2024-06-18/     50 files    photo_event cohesion
  Media/Music/Miles Davis/Kind of Blue/      7 files    music_album cohesion
  Unsorted/                                  1 file     confidence 0.20

Plan written to ~/plan.json
Review with:  uv run dc organize review ~/plan.json
```

`~/plan.json` (excerpt):

```jsonc
{
  "version": "0.3.0",
  "sources": ["local"],
  "dest_root": "/Users/vaannada/organized",
  "total_files": 61,
  "total_by_domain": {"HR": 3, "Finances": 1, "Photos": 50, "Media": 7, "Unsorted": 1},
  "cohesion_groups": [
    {"id": "event:2024-06-15_bali", "kind": "photo_event",
     "dest_domain": "Photos", "dest_subfolder": "2024/2024-06-15_to_2024-06-18",
     "member_paths": ["/Users/.../vacation-bali/IMG_5001.HEIC", "..."]},
    {"id": "music:Miles Davis/Kind of Blue", "kind": "music_album",
     "dest_domain": "Media", "dest_subfolder": "Music/Miles Davis/Kind of Blue",
     "member_paths": ["/Users/.../kind-of-blue/01 - So What.flac", "..."]}
  ],
  "entries": [
    {"source_id": "local",
     "src_path": "/Users/vaannada/Downloads/payslip_march_2024.pdf",
     "domain": "HR", "subfolder": "Payslips/2024",
     "filename": "payslip_march_2024.pdf", "confidence": 0.83,
     "signals_fired": [
       {"kind": "fname.keyword.payslip", "value": "payslip", "contribution": 0.50},
       {"kind": "pdf.class.payslip", "value": "0.73", "contribution": 0.66}],
     "alternatives": [], "cohesion_group": null}
  ],
  "unsorted": [
    {"src_path": "/Users/vaannada/Downloads/random-download.pdf",
     "domain": "Unsorted", "subfolder": "", "confidence": 0.20,
     "alternatives": [{"domain": "Finances", "subfolder": "Receipts/2024/UnknownVendor", "confidence": 0.20}]}
  ]
}
```

Review in the TUI, then apply:

```
uv run dc organize apply ~/plan.json                  # dry-run
# 61 planned moves, 0 collisions

uv run dc organize apply ~/plan.json --commit
# creates HR/, Finances/, Photos/, Media/, Unsorted/ under ~/organized
# writes ~/.local/share/duplicate_cleaner/runs/<ts>/manifest.json
# moves 61 files
```

Result:

```
~/organized/
  HR/Payslips/2024/payslip_march_2024.pdf
  HR/Payslips/2024/payslip_april_2024.pdf
  HR/Payslips/2024/payslip_may_2024.pdf
  Finances/Receipts/2024/Amazon/Amazon_receipt_Order_112-4839.pdf
  Finances/Receipts/2024/UnknownVendor/random-download.pdf
  Photos/2024/2024-06-15_to_2024-06-18/IMG_5001.HEIC
  Photos/2024/2024-06-15_to_2024-06-18/... (49 more)
  Media/Music/Miles Davis/Kind of Blue/01 - So What.flac
  Media/Music/Miles Davis/Kind of Blue/... (6 more)
```

Undo with the manifest path printed by the commit:

```
uv run dc organize undo ~/.local/share/duplicate_cleaner/runs/<ts>/manifest.json
```

## Taxonomy

The v0.3 rule catalog classifies every file into one of nine domains. Every rule uses the union of signals extracted during discovery — filename patterns, EXIF, ID3, PDF first-page text, PDF Info dict, git markers, MIME type, and mtime.

| Domain / subfolder template | What triggers it |
|---|---|
| `HR/Payslips/{year}/` | Filename matches `payslip|salary|pay.slip|pay.stub`, or PDF first-page classifier scores `payslip` above threshold (keywords like `Gross Pay`, `Net Pay`, `YTD`, `Employee ID`). Year from PDF creation date, falling back to filename date and then mtime. |
| `HR/OfferLetters/{year}-{employer}/` | Filename matches `offer|hire|welcome|joining`, or PDF classifier scores `offer_letter` (keywords like `pleased to offer`, `Position`, `Start Date`). Employer extracted from the top three lines of the PDF. |
| `HR/Tax/{year}/` | Filename matches `w2|1099|w-2|form.16|itr|1040`, or PDF classifier scores `tax_form`. |
| `HR/Employment/{year}/` | Fallback within HR: filename matches `nda|noc|contract.employment`, or PDF classifier scores `employment_misc`. |
| `Personal/IDs/` | Filename matches `passport|driver.license|pan|aadhaar|ssn|voter.id`, or PDF classifier scores `id_document`. Flat folder — no year subdivision, because IDs are not year-scoped. |
| `Personal/Insurance/{year}/` | Filename matches `insurance|policy|premium`, or PDF classifier scores `insurance` (`Policy Number`, `Premium`, `Coverage`, `Beneficiary`). |
| `Personal/Legal/` | Filename matches `contract|agreement|notary|will|poa`, or PDF classifier scores `legal` (`WHEREAS`, `hereby agree`, `NOTARY`). |
| `Finances/Receipts/{year}/{vendor}/` | Filename matches `receipt|order.confirm`, or PDF classifier scores `receipt`. Vendor from PDF header lines or filename tokens against the vendor list (`Amazon`, `Uber`, `DoorDash`, `Whole Foods`, `Home Depot`, `Costco`, `Target`, `Walmart`, `Apple`, `Best Buy`). Unmatched vendor becomes `UnknownVendor` and drops the entry below the confidence threshold. |
| `Finances/Statements/{year}/{institution}/` | Filename matches `statement|account.summary`, or PDF classifier scores `bank_statement`. Institution from filename tokens or PDF header against the bank list (`Chase`, `Wells Fargo`, `Bank of America`, `Citi`, `Discover`, `HDFC`, `ICICI`, `SBI`, `Amex`, `Capital One`). |
| `Finances/Invoices/{year}/` | Filename matches `invoice|bill|inv.no`, or PDF classifier scores `invoice`. |
| `Finances/Investments/{year}/` | PDF classifier scores `investment` (`Portfolio`, `Dividend`, `NAV`), or filename token matches a brokerage (`Vanguard`, `Fidelity`, `Charles Schwab`, `Robinhood`, `E*TRADE`, `Merrill`, `TD Ameritrade`). |
| `Photos/{year}/{event_folder}/` | Photo MIME + event clustering fires. Event folder from time-gap clustering (see event clustering section). |
| `Photos/{year}/{yyyy_mm}/` | Fallback for isolated photos with no event cluster. |
| `Videos/{year}/{event_folder}/` | Same as photo event but for video MIME. |
| `Videos/{year}/{yyyy_mm}/` | Fallback for isolated videos. |
| `Work/{year}/{client_or_project}/` | Office document (`.docx`, `.pptx`, `.xlsx`) with path tokens like `Client`, `Project`, `Proposal`, `Deck`. |
| `Work/{year}/` | Fallback for Office documents without a client hint. |
| `Projects/{repo_name}/` | Directory contains `.git/`, `package.json`, `Cargo.toml`, `pyproject.toml`, `go.mod`, `pom.xml`, or `.hg/`. Every file inside the directory moves atomically as a git-project cohesion unit. |
| `Media/Music/{artist}/{album}/` | Audio files whose ID3 tags include both `artist` (or `albumartist`) and `album`. Music album cohesion applies (see cohesion section). |
| `Media/Books/{topic}/` | PDF or EPUB whose PDF-text classifier assigns a topic cluster. Topic list is config-extendable via `[organize.books.topics]`. |
| `Unsorted/` | No rule fired above the confidence threshold, and no domain guess reached the 0.4 partial-guess threshold either. |
| `Unsorted/<Domain>/` | Best rule scored between 0.4 and the confidence threshold. The domain-hinted Unsorted subfolder helps you triage in bulk during review. |

Precedence on rule ties: higher `precedence` wins, then more-specific subfolder template, then alphabetical `rule_id`. Deterministic across runs.

## Cohesion preservation

Cohesive units move as one. The four kinds:

- **Music album.** In a directory where at least 80 percent of audio files share the same `(albumartist or artist, album)` tuple, the whole directory is a music album cohesion. Your Miles Davis album stays together at `Media/Music/Miles Davis/Kind of Blue/`, even if one track is missing an ID3 tag.
- **Book series.** In a directory where at least 80 percent of PDFs share a topic (from the PDF-text classifier) or a common filename prefix of at least four characters and the same PDF-Info author, the whole directory moves to `Media/Books/{topic}/`.
- **Photo or video event.** In a directory where at least 80 percent of media files fall into the same event cluster (see next section), the whole directory is an event cohesion. Your Bali trip photos all move together to `Photos/2024/2024-06-15_to_2024-06-18/`.
- **Git project.** A directory that contains any of `.git/`, `package.json`, `Cargo.toml`, `pyproject.toml`, `go.mod`, `pom.xml`, or `.hg/` is a project cohesion. Every file inside — including README, license, siblings without git markers of their own — moves atomically to `Projects/{repo_name}/`. Your git repo is never sliced apart.

A file can belong to at most one cohesion group. When a music file lives inside a git repo, project cohesion wins because splitting a repo is more damaging than splitting an album.

Cohesion is a promise the tool relies on. If your edits to the plan JSON leave one member of a cohesion group targeting a different folder from the rest, `dc organize apply` refuses to run and lists the violating entries. To deliberately split a cohesive unit, pass `--split-cohesive-units` on apply. This is the same shape of guard the mover uses for archive whole-delete: the invariant lives in one place and cannot be silently sidestepped.

## Confidence and Unsorted

Every classification carries a confidence in `[0, 1]`. The threshold is `organize_confidence_threshold` (default `0.75`). Files below the threshold go to `Unsorted/`:

- If the best rule still scored at least `0.4`, the destination is `Unsorted/<Domain>/` — for example, `Unsorted/Finances/`. This groups near-misses so you can triage them together during review rather than one at a time.
- If no rule cleared `0.4`, the destination is a flat `Unsorted/`.

Every Unsorted entry carries an `alternatives` array in the plan JSON — the top-three candidate destinations with their confidence scores. Reviewing an Unsorted file, you can either accept an alternative or type a fresh destination.

Promoting an Unsorted file during review:

- In the TUI: navigate to the file (`n` / `p`), then press `1`, `2`, or `3` to accept the corresponding alternative. Or press `d` to type a fresh destination via the fuzzy finder.
- In `$EDITOR`: edit the entry's `domain` and `subfolder`, and delete it from the top-level `unsorted` list. The apply step re-validates the schema.

## Review workflows

### Rich TUI (`dc organize review <plan.json>`)

A keyboard-driven editor for the plan file. Every action rewrites the plan JSON on `w`; nothing is written until you save.

Keybindings:

- `n` / `p` — next / previous entry.
- `N` / `P` — next / previous cohesion group.
- `d` — change destination for the current entry via a fuzzy finder over every folder in the plan (plus a free-text option).
- `1` / `2` / `3` — accept alternative 1, 2, or 3 for the current entry.
- `m` — merge the current cluster with another (prompts for target folder).
- `s` — split a cluster (prompts for which files to move out).
- `x` — mark the current cluster "leave in place" — remove all its entries from the plan.
- `!` — override the cohesion for one entry. Apply still refuses to commit the plan without `--split-cohesive-units`; this key just records your intent so the diff is visible on save.
- `w` — write plan JSON and exit.
- `q` — quit without saving (prompts for confirmation if there are unsaved changes).

TUI layout, sketched in ASCII:

```
+-------------------------------------------------------+
| Plan: ~/plan.json                          61 entries |
+-------------------------------------------------------+
| Entry 15 of 61                                        |
|                                                       |
|  src   : ~/Downloads/random-download.pdf              |
|  domain: Unsorted                                     |
|  dest  : Unsorted/random-download.pdf                 |
|  conf  : 0.20                                         |
|                                                       |
|  Signals fired:                                       |
|    (none above threshold)                             |
|                                                       |
|  Alternatives:                                        |
|    1  Finances/Receipts/2024/UnknownVendor  conf 0.20 |
|    2  Personal/Legal                        conf 0.15 |
|    3  Work/2024                             conf 0.10 |
+-------------------------------------------------------+
| n/p next  d change dest  1-3 alt  m merge  s split    |
| w write   q quit                                      |
+-------------------------------------------------------+
```

### Editing the JSON directly

The plan JSON is the source of truth. `$EDITOR` is a supported workflow — apply re-validates the schema, so a typo or a broken cohesion group is caught before any files move.

To change a destination, edit the entry's `domain` and `subfolder`. To split a cohesion group, edit each affected entry and pass `--split-cohesive-units` on apply. To promote an Unsorted file, move it from the `unsorted` array (or leave it there — the array is a convenience mirror; membership is derived from `domain == "Unsorted"`).

The generated HTML report at `<plan>.html` is a view only. Editing HTML does not mutate JSON. This mirrors the `dc scan` contract.

## Event clustering

Photos and videos with a resolvable capture timestamp (EXIF `DateTimeOriginal` for photos, container-metadata create date for videos) are grouped into events by time gap:

- All items sorted by capture time.
- A gap larger than `event_gap_hours` (default `12` hours) starts a new event.
- Events smaller than `min_event_photos` (default `5`) fall back to the monthly rule instead of getting their own folder.

If at least half the items in an event have GPS coordinates, the tool computes the event centroid and flags items more than `gps_km_threshold` (50 km) from the centroid as outliers. Outliers form a sub-event; if the sub-event is too small it falls back to monthly.

By default, event folders are named with dates only:

- Single-day event: `2024-06-15/`.
- Multi-day event: `2024-06-15_to_2024-06-18/`.

Pass `--enable-geocode` to add a locality suffix from a reverse-geocode of the event centroid, resolved via Nominatim. Example: `2024-06-15_to_2024-06-18_Bali_Indonesia/`. The geocode path is opt-in because it introduces a network dependency and rate-limits (Nominatim policy caps at one request per second per user-agent). Results are cached in the SQLite `file_signals` table under `gps.geocode`, keyed on the centroid rounded to three decimal places, so repeat organize runs are free of network calls. If Nominatim errors, the tool falls back to the date-only name; discovery never blocks on the network.

Use `--enable-geocode` when you have GPS-tagged photos and want city or region names in the folder tree. Leave it off when you want a fully offline run, when your photos lack GPS data, or when you care about privacy — the geocode centroid does not identify a person, but it does hit an external service.

## PDF content classification

For every PDF the tool reads:

1. The Info dict via `pikepdf`. This gives you title, author, producer, creation date, keywords — cheap and always available.
2. The first-page text via `pdfplumber`, truncated to 8 KB. The classifier only reads the first page because letterhead-style keywords sit at the top and reading the whole file is not cheap.
3. Optionally, if `--ocr` is set AND the text extraction returned less than 100 characters AND `pytesseract` is importable, the tool OCRs a rasterization of the first page. OCR is opt-in because it is 10 to 50 times slower than text extraction.

The classifier scores each class as `raw_score / max_score`, where `raw_score` is the sum of weights for keywords found on the first page and `max_score` is the sum of all weights for that class. The class-to-keyword lexicon lives in `organize/pdf_classify.py`. Sketched for the two most common classes:

- `payslip`: `Gross Pay` (3), `Net Pay` (3), `Employee ID` (2), `YTD` (1), `Basic` (1), `HRA` (1), `Deductions` (2), `Pay Period` (2).
- `receipt`: `Receipt` (2), `Order #` (2), `Order Number` (2), `Subtotal` (1), `Tax` (1), `Total` (1), `Payment Method` (2), `Thank you` (1).

If two classes exceed the threshold (0.6), both are recorded but only the higher one feeds the classifier's rule. The other is written to the plan's `alternatives`, so the reviewer can accept it if the primary guess is wrong.

Year and vendor extraction from a PDF is layered:

- Year: PDF creation date first, then a date in the first-page text, then a date in the filename, then mtime year.
- Vendor / institution / employer: the top three non-empty lines of the first page (the letterhead), matched against the shipped dictionaries. Falls back to filename tokens matched against the same dictionaries.

Custom classifiers and vendor hints are extensible via `~/.config/duplicate_cleaner/organize.toml`. See `docs/config.md` for the extension points.

The PDF classifier is a hint, not a guarantee. Every score is written to the plan's `signals_fired` list so you can audit exactly which keywords fired and edit the outcome. Files where the classifier is uncertain go to Unsorted rather than to a specific domain.

## Rename policy

Filenames are user-territory. The tool defaults to leaving them alone. Three modes, set via `rename_policy` in the config:

- `preserve` (default). The destination filename is the source filename, unchanged. Byte-for-byte.
- `date_prefix`. Prefix the filename with `YYYY-MM-DD_` derived from the file's classified year, month, and day. Example: `payslip_march_2024.pdf` becomes `2024-03-15_payslip_march_2024.pdf` under HR/Payslips/2024/.
- `date_event_prefix`. For files inside a photo or video event cohesion, prefix with the event's date span; for other files, behaves like `date_prefix`. Example: `IMG_5001.HEIC` becomes `2024-06-15_IMG_5001.HEIC` inside `Photos/2024/2024-06-15_to_2024-06-18/`.

Rename policy is user-locked. The tool will not silently mutate filename bytes; the policy is only ever applied when you have explicitly opted in via the config.

## CLI reference

Full command surface for v0.3. Every subcommand accepts `--help`.

### `dc organize discover`

```
dc organize discover [ROOTS...] [--sources SRC_LIST] [--dest DIR]
                     [--plan PATH] [--confidence-threshold FLOAT]
                     [--event-gap-hours INT] [--enable-geocode] [--ocr]
                     [--skip-dedup-check]
```

Options:

- `ROOTS` — local directories to include (for the `local` source). Ignored for cloud sources; those enumerate their entire account.
- `--sources` — comma-separated source IDs. Default `local`. Cross-source support is deferred to sub-milestone v0.3-g.
- `--dest DIR` — root of the target tree. Default `~/organized`.
- `--plan PATH` — where to write the plan JSON. Default `~/organize-plan.json`. A `<plan>.html` view is written alongside.
- `--confidence-threshold FLOAT` — override `organize_confidence_threshold` for this run. Range `[0.0, 1.0]`. Default from config (`0.75`).
- `--event-gap-hours INT` — override `event_gap_hours` for this run. Default from config (`12`).
- `--enable-geocode` — reverse-geocode event centroids via Nominatim to add a locality suffix to event folder names. Off by default.
- `--ocr` — OCR PDFs whose text extraction returns less than 100 characters. Requires `pytesseract` (install with the `[ocr]` extra) and a system `tesseract` binary.
- `--skip-dedup-check` — suppress the soft warning when pending duplicate proposals exist. Use in scripts.

Example:

```
uv run dc organize discover ~/Downloads ~/OldMac \
    --dest ~/organized --plan ~/plan.json --enable-geocode
```

### `dc organize review`

```
dc organize review <plan.json>
```

Starts the Rich TUI. Rewrites the plan JSON on `w`. See the "Review workflows" section for keybindings. If your terminal is not TTY, the command exits with an error pointing you at `$EDITOR`.

### `dc organize apply`

```
dc organize apply <plan.json> [--commit] [--split-cohesive-units]
                              [--runs-dir DIR]
```

Options:

- `--commit` — actually create folders and move files. Without this flag, apply prints the planned moves and exits.
- `--split-cohesive-units` — allow the plan to route members of a cohesion group to different destinations. Without this flag, any cohesion violation aborts the run before the first move.
- `--runs-dir DIR` — where to write the undo manifest. Default `~/.local/share/duplicate_cleaner/runs/`.

Example:

```
uv run dc organize apply ~/plan.json                # dry-run
uv run dc organize apply ~/plan.json --commit       # actually move
```

### `dc organize undo`

```
dc organize undo <manifest.json>
```

Reverses every move in the manifest. For same-volume moves, this is an `os.rename` back to the original path. For cross-volume moves (external drive to internal home, or the reverse), the tool recovers the source from the Trash entry recorded in the manifest, then removes the destination copy after verifying its hash matches.

## Cross-source note

v0.3 ships local-only organize. Cloud organizing (moving Google Drive or OneDrive files into a folder tree the tool has proposed) is v0.3-g, a deferred sub-milestone. When v0.3-g lands, `--sources local,gdrive:personal` on discover will populate both source IDs and apply will dispatch cloud moves through the source API. Until then, `--sources` values other than `local` are rejected.

## Undo

Every commit run writes a manifest to `~/.local/share/duplicate_cleaner/runs/<timestamp>/manifest.json` before the first move. The manifest records every planned move, the pre-move hash, the destination, and — for cross-volume moves — the Trash-relative source path. `dc organize undo <manifest.json>` reverses the run:

- Same-volume moves: `os.rename(dst, src)` restores the atomic in-place move.
- Cross-volume moves: the tool locates the source in the Trash (via the recorded `trashed_at_path` or a basename-plus-size fallback), then removes the destination copy after verifying its hash matches.

If a file has been edited between the run and the undo, that entry is skipped with an error and the rest of the manifest still restores. This matches the per-file re-verify contract in `dc undo` for dedup.

## Rejected alternatives

Documented here so we do not relitigate them.

- **ML classifiers over rules.** Rejected. A rule catalog is transparent — every decision surfaces the exact signals that fired, and edits to the rules are code review-able. Training a classifier requires hundreds of labeled examples per class, which no single user has. Adaptive weight learning is on the roadmap once the decisions log has enough overrides to learn from, but the base classifier stays rule-based.
- **Auto-download originals from iCloud (and similar) during discover.** Rejected. Discover works from local file bytes and cloud metadata. Automatically downloading originals from an iCloud placeholder would cause surprise bandwidth on a metered connection and slow the scan for no user benefit. iCloud placeholders remain skipped.
- **Online geocoding by default.** Rejected. Event folder names default to dates only. Users opt in with `--enable-geocode`. The default keeps the tool offline and predictable; the opt-in path adds locality names when you want them.
- **HTML plan editor.** Deferred to v0.3-f as a view-only render. Editing the plan happens in the TUI or in `$EDITOR`. The HTML is a review companion, not a source of truth.
