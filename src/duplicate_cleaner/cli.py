"""Typer entrypoint — `dc scan | apply | undo | init | weights | cache`."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from duplicate_cleaner import __version__
from duplicate_cleaner.apply.mover import ApplyError, apply_report
from duplicate_cleaner.apply.undo import restore_from_manifest
from duplicate_cleaner.auth import (
    ACCOUNTS_PATH,
    BUNDLED_GDRIVE_CLIENT_ID,
    BUNDLED_GDRIVE_CLIENT_SECRET,
    BUNDLED_ONEDRIVE_CLIENT_ID,
    BUNDLED_ONEDRIVE_CLIENT_SECRET,
    GDRIVE_AUTH_URL,
    GDRIVE_DEFAULT_SCOPES,
    GDRIVE_FULL_SCOPES,
    GDRIVE_REVOKE_URL,
    GDRIVE_TOKEN_URL,
    ONEDRIVE_AUTH_URL,
    ONEDRIVE_DEFAULT_SCOPES,
    ONEDRIVE_TOKEN_URL,
    AccountEntry,
    AccountsRegistry,
    DuplicateAccountError,
    OAuthFlowError,
    TokenPermissionError,
    TokenStore,
    load_client_secret_json,
    run_localhost_flow,
)
from duplicate_cleaner.auth.oauth_flow import revoke_token
from duplicate_cleaner.compare.archive import (
    is_archive_path,
    scan_archive,
)
from duplicate_cleaner.compare.exact import group_by_hash
from duplicate_cleaner.config import (
    CONFIG_PATH,
    WEIGHTS_PATH,
    load_config,
    load_weights,
    write_default_config,
    write_default_weights,
)
from duplicate_cleaner.hash.pipeline import HashedRecord, hash_records
from duplicate_cleaner.paths import validate_scan_root_candidate
from duplicate_cleaner.report.render import render_report
from duplicate_cleaner.report.schema import (
    ArchiveSkipEntry,
    Report,
    ReportGroup,
    ReportMember,
    ReportSignal,
    SingletonEntry,
)
from duplicate_cleaner.scan.walk import FileRecord, WalkStats
from duplicate_cleaner.score.rules import score_groups
from duplicate_cleaner.sources import LocalFileSystemSource
from duplicate_cleaner.store import CACHE_DIR, Store
from duplicate_cleaner.sys.apfs import get_clone_id
from duplicate_cleaner.sys.monitor import (
    DiskSpaceError,
    be_polite,
    check_free_disk,
    maybe_throttle,
    sample_resources,
)

app = typer.Typer(help="Intelligent duplicate detection and cleanup.")
weights_app = typer.Typer(help="Inspect or reset scoring weights.")
cache_app = typer.Typer(help="Inspect or clear the local hash cache.")
auth_app = typer.Typer(help="Manage cloud-account OAuth credentials.")
sources_app = typer.Typer(help="Inspect configured sources.")
app.add_typer(weights_app, name="weights")
app.add_typer(cache_app, name="cache")
app.add_typer(auth_app, name="auth")
app.add_typer(sources_app, name="sources")

console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@app.callback()
def _root(
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Verbose logging.")
    ] = False,
) -> None:
    _setup_logging(verbose)


@app.command()
def version() -> None:
    """Print the installed package version."""
    console.print(f"duplicate_cleaner {__version__}")


@app.command()
def init() -> None:
    """Create default config and weights files."""
    if not CONFIG_PATH.exists():
        write_default_config()
        console.print(f"[green]Wrote[/green] {CONFIG_PATH}")
    else:
        console.print(f"[yellow]Config already exists[/yellow]: {CONFIG_PATH}")

    if not WEIGHTS_PATH.exists():
        write_default_weights()
        console.print(f"[green]Wrote[/green] {WEIGHTS_PATH}")
    else:
        console.print(f"[yellow]Weights already exist[/yellow]: {WEIGHTS_PATH}")

    console.print(
        "\n[bold]Next[/bold]: edit config.toml and set "
        "[cyan]active_homes[/cyan] to your live home directory(ies) "
        "before scanning."
    )


def _expand_archive_members(
    rec: FileRecord,
    max_depth: int,
    archive_paths: list[Path],
    member_hashes_by_archive: dict[str, list[str]],
    archive_skips: list[ArchiveSkipEntry],
    skipped_outer_archives: set[str],
) -> Iterator[FileRecord]:
    """Yield synthetic FileRecords for every readable member of ``rec``.

    ``rec`` is a real on-disk archive. ``archive_paths`` accumulates every
    on-disk archive processed so the post-processing pass can build the
    whole-archive-delete proposals. Skips are recorded for the report and
    also indexed by outer-archive path in ``skipped_outer_archives`` — the
    whole-delete evaluator uses that index to guarantee no archive with an
    encrypted/corrupt/too-large member is ever proposed (H1).
    """
    result = scan_archive(rec.path, max_depth=max_depth)
    if result.members:
        archive_paths.append(rec.path)
        member_hashes_by_archive[str(rec.path)] = [
            m.full_hash for m in result.members
        ]
    for m in result.members:
        yield FileRecord(
            path=Path(m.virtual_path),
            size=m.size,
            mtime=rec.mtime,
            inode=0,
            dev=0,
            nlink=0,
            is_archive_member=True,
            precomputed_full_hash=m.full_hash,
        )
    if result.skips:
        # H1: attribute every skip to its outer on-disk archive path. The
        # outer path is the ``rec.path`` we just walked; any skip anywhere
        # in its member tree disqualifies the whole archive from
        # whole-delete. ``outer_archive_of`` handles both the "corrupt at
        # depth 1" case (skip.path == str(rec.path)) and the nested case
        # (skip.path == "outer.zip::mid.zip::..."), because the on-disk
        # outer archive is always the first segment.
        skipped_outer_archives.add(str(rec.path))
    for s in result.skips:
        archive_skips.append(
            ArchiveSkipEntry(path=s.path, reason=s.reason, error=s.error)
        )


def _build_whole_archive_groups(
    archive_paths: list[Path],
    member_hashes_by_archive: dict[str, list[str]],
    real_file_hashes: set[str],
    archive_own_hashes: dict[str, str],
    archive_sizes: dict[str, int],
    archive_mtimes: dict[str, float],
    skipped_outer_archives: set[str],
) -> list[ReportGroup]:
    """Emit a one-member "whole-archive-delete" ReportGroup per redundant archive.

    Only fires when every member's hash is present as a non-archive on-disk
    file elsewhere in the scan AND the archive's walk produced no skips
    (H1). Groups have kind ``"archive-whole"`` and a single member marked
    as a discard target — the mover trashes it and the contents are
    recoverable from the surviving on-disk copies.
    """
    from duplicate_cleaner.compare.archive import find_wholly_duplicated_archives

    proposals = find_wholly_duplicated_archives(
        archive_paths=archive_paths,
        member_hashes_by_archive=member_hashes_by_archive,
        hashes_with_ondisk_copy=real_file_hashes,
        skipped_archives=skipped_outer_archives,
    )
    groups: list[ReportGroup] = []
    for i, path in enumerate(proposals):
        key = str(path)
        h = archive_own_hashes.get(key, "")
        size = archive_sizes.get(key, 0)
        mtime = archive_mtimes.get(key, 0.0)
        signal = ReportSignal(
            name="every member duplicated on-disk", contribution=-1.0
        )
        member = ReportMember(
            path=path,
            size=size,
            mtime=mtime,
            hash=h or "",
            score=-1.0,
            signals=[signal],
            is_proposed_keeper=False,
            is_informational=False,
        )
        groups.append(
            ReportGroup(
                id=f"archive-{i:04d}",
                kind="archive-whole",
                size=size,
                hash=h or f"archive-{i:04d}",
                reclaim_bytes=size,
                members=[member],
            )
        )
    return groups


@app.command()
def scan(
    roots: Annotated[
        list[Path], typer.Argument(help="One or more directories to scan.")
    ],
    report_dir: Annotated[
        Path,
        typer.Option(
            "--report",
            help="Output directory for report.html and report.json.",
        ),
    ],
    min_size: Annotated[
        int | None,
        typer.Option(
            "--min-size", help="Override min_size_bytes from config."
        ),
    ] = None,
    follow_symlinks: Annotated[
        bool,
        typer.Option("--follow-symlinks", help="Follow symlinks during walk."),
    ] = False,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Extra exclude glob (repeatable)."),
    ] = None,
    discover: Annotated[
        bool,
        typer.Option(
            "--discover",
            help=(
                "Enumeration-only mode. No keepers proposed and dc apply "
                "will refuse to run against the resulting report."
            ),
        ),
    ] = False,
    sources: Annotated[
        str,
        typer.Option(
            "--sources",
            help=(
                "Comma-separated source ids (e.g. 'local,gdrive:personal'). "
                "Default: 'local' only."
            ),
        ),
    ] = "local",
    max_cloud_download_mb: Annotated[
        float,
        typer.Option(
            "--max-cloud-download-mb",
            help=(
                "Cap total cloud bytes downloaded this scan. Buckets whose "
                "reconciliation would exceed the cap are marked "
                "not-yet-hashed. Default 1000 MB."
            ),
        ),
    ] = 1000.0,
    cloud_hash_ttl_days: Annotated[
        float,
        typer.Option(
            "--cloud-hash-ttl-days",
            help=(
                "Discard cached cloud BLAKE3 hashes older than N days at scan "
                "start. Default 90."
            ),
        ),
    ] = 90.0,
) -> None:
    """Walk, hash, group, score, and write a report."""
    # B3: refuse non-local source ids until sub-phase 5 wires cloud sources
    # into the scan pipeline.  Accepting them silently produced reports
    # missing entire sources — surface the gap loudly instead.  Sub-phase 4
    # (OneDrive) registers accounts and exercises the source in tests but
    # still routes end-users through the same "not until sub-phase 5"
    # message so cross-source behaviour lands in one place.
    requested_sources = [s.strip() for s in sources.split(",") if s.strip()]
    for s in requested_sources:
        if s != "local":
            console.print(
                f"[red]Refusing to scan[/red]: source {s!r} is not enabled. "
                "Cross-source scan is not enabled until v0.2 sub-phase 5. "
                "Currently only 'local' is supported."
            )
            raise typer.Exit(2)
    if max_cloud_download_mb != 1000.0:
        log = logging.getLogger(__name__)
        log.warning(
            "--max-cloud-download-mb accepted (value=%s) but has no effect "
            "until v0.2 sub-phase 5 wires cloud sources into scan.",
            max_cloud_download_mb,
        )
    cfg = load_config()
    if not cfg.active_homes:
        console.print(
            "[red]Refusing to scan[/red]: no [cyan]active_homes[/cyan] declared in "
            f"{CONFIG_PATH}. Edit the file and set "
            "active_homes = ['/Users/you'] before scanning."
        )
        raise typer.Exit(2)

    # G2: validate every scan root the user typed against the same rules
    # as ``active_homes`` and ``report.roots``. ``dc scan /``, ``dc scan
    # /Users``, or ``dc scan /private`` must fail fast — not descend into
    # ~/Library, /System, /private/var/folders, etc.
    for r in roots:
        try:
            validate_scan_root_candidate(r)
        except ValueError as e:
            console.print(f"[red]Refusing to scan[/red]: {e}")
            raise typer.Exit(2) from e

    if min_size is not None:
        cfg = cfg.model_copy(update={"min_size_bytes": min_size})
    if follow_symlinks:
        cfg = cfg.model_copy(update={"follow_symlinks": True})
    if exclude:
        cfg = cfg.model_copy(
            update={"exclude_globs": [*cfg.exclude_globs, *exclude]}
        )

    # Pre-scan disk-space check — abort BEFORE we start hashing so an
    # already-full cache volume doesn't get any worse.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        check_free_disk(CACHE_DIR, cfg.min_free_disk_gb)
    except DiskSpaceError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e

    # Politeness — best-effort re-nice + I/O tier bump.
    be_polite()

    weights = load_weights()
    store = Store()
    # B12: sweep stale cloud-hash-cache rows on every scan start.  Prevents
    # unbounded growth of ``cloud_hash_cache`` after long stretches without a
    # cache clear.  Cheap SQL DELETE; rows only get "stale" 90+ days after
    # their last successful hash so warm entries are not touched.
    store.purge_stale_cloud_hashes(max_age_days=cloud_hash_ttl_days)

    file_count = 0
    archive_paths: list[Path] = []
    member_hashes_by_archive: dict[str, list[str]] = {}
    archive_skips: list[ArchiveSkipEntry] = []
    # H1: on-disk outer-archive paths whose walk produced ≥1 skip. Any
    # archive listed here is ineligible for whole-archive-delete.
    skipped_outer_archives: set[str] = set()
    walk_stats = WalkStats()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        walk_task = progress.add_task("Walking & hashing…", total=None)

        local_source = LocalFileSystemSource(
            roots=list(roots),
            follow_symlinks=cfg.follow_symlinks,
            exclude_globs=cfg.exclude_globs,
            min_size_bytes=cfg.min_size_bytes,
            bundle_extensions=cfg.bundle_extensions,
            stats=walk_stats,
        )

        def _walk_iter() -> Iterator[FileRecord]:
            nonlocal file_count
            for rec in local_source.list_files():
                file_count += 1
                if file_count % 500 == 0:
                    snap = sample_resources(
                        process=None,
                        scan_path=roots[0],
                        files_processed=file_count,
                    )
                    progress.update(
                        walk_task,
                        description=(
                            f"Walked {file_count} files · CPU "
                            f"{snap.cpu_pct:.0f}% · RSS "
                            f"{snap.rss_bytes // (1024 * 1024)} MB · "
                            f"free disk {snap.free_disk_gb:.1f} GB"
                        ),
                    )
                    # H6: reuse the cpu_pct from ``sample_resources`` — two
                    # back-to-back ``psutil.cpu_percent(interval=None)`` calls
                    # share an accumulator, so the second reading is ~0 and
                    # the throttle never fires. One sample per iteration.
                    maybe_throttle(
                        cfg.throttle_on_cpu_pct, cpu_pct=snap.cpu_pct
                    )
                yield rec
                # Fan out archive members into synthetic virtual records so
                # the exact-duplicate pipeline can group them alongside
                # real on-disk files.
                if not rec.is_bundle and is_archive_path(rec.path):
                    yield from _expand_archive_members(
                        rec,
                        cfg.max_archive_depth,
                        archive_paths,
                        member_hashes_by_archive,
                        archive_skips,
                        skipped_outer_archives,
                    )

        hashed_iter = hash_records(
            _walk_iter(), store, include_singletons=True
        )
        # Materialise so we can index singletons AND groups without
        # re-walking the tree.
        all_hashed: list[HashedRecord] = list(hashed_iter)
        groups = list(
            group_by_hash(iter(all_hashed), min_size_bytes=cfg.min_size_bytes)
        )
        progress.update(
            walk_task,
            description=(
                f"Walked {file_count} files; found {len(groups)} groups."
            ),
        )

    scored = score_groups(
        groups, cfg, weights, clone_id_lookup=get_clone_id
    )

    # Real (on-disk, non-archive-member) hashes are the alternative pool
    # used to decide whether a whole archive can be safely proposed for
    # discard.
    real_file_hashes: set[str] = set()
    archive_own_hashes: dict[str, str] = {}
    archive_sizes: dict[str, int] = {}
    archive_mtimes: dict[str, float] = {}
    for h in all_hashed:
        if h.is_archive_member:
            continue
        real_file_hashes.add(h.full_hash)
        # Record the on-disk hash and size for each archive path so the
        # archive-whole proposal has full metadata.
        if is_archive_path(h.path):
            archive_own_hashes[str(h.path)] = h.full_hash
            archive_sizes[str(h.path)] = h.size
            archive_mtimes[str(h.path)] = h.mtime

    report_groups: list[ReportGroup] = []
    total_reclaim = 0
    for sg in scored:
        members = [
            ReportMember(
                path=m.path,
                size=m.size,
                mtime=m.mtime,
                hash=m.hash,
                score=m.score,
                signals=[
                    ReportSignal(name=n, contribution=c) for n, c in m.signals
                ],
                is_proposed_keeper=m.is_proposed_keeper and not discover,
                is_informational=m.is_informational,
                is_archive_member=m.is_archive_member,
                is_bundle=m.is_bundle,
            )
            for m in sg.members
        ]
        store.record_group(
            "exact",
            [
                (m.path, m.score, m.is_proposed_keeper and not discover, m.is_informational)
                for m in sg.members
            ],
        )
        report_groups.append(
            ReportGroup(
                id=sg.id,
                kind="exact",
                hash=sg.hash,
                size=sg.size,
                reclaim_bytes=0 if discover else sg.reclaim_bytes,
                members=members,
            )
        )
        if not discover:
            total_reclaim += sg.reclaim_bytes

    # Archive-whole proposals (skipped in discover mode).
    if not discover:
        archive_groups = _build_whole_archive_groups(
            archive_paths=archive_paths,
            member_hashes_by_archive=member_hashes_by_archive,
            real_file_hashes=real_file_hashes,
            archive_own_hashes=archive_own_hashes,
            archive_sizes=archive_sizes,
            archive_mtimes=archive_mtimes,
            skipped_outer_archives=skipped_outer_archives,
        )
        for ag in archive_groups:
            report_groups.append(ag)
            total_reclaim += ag.reclaim_bytes

    # Singletons — hashes that appear exactly once across the whole scan,
    # excluding archive members (which are informational by construction).
    hash_counts: dict[str, int] = {}
    for h in all_hashed:
        if h.is_archive_member:
            continue
        hash_counts[h.full_hash] = hash_counts.get(h.full_hash, 0) + 1
    singletons: list[SingletonEntry] = []
    seen_singleton_hashes: set[str] = set()
    for h in all_hashed:
        if h.is_archive_member:
            continue
        if hash_counts.get(h.full_hash, 0) != 1:
            continue
        if h.full_hash in seen_singleton_hashes:
            continue
        seen_singleton_hashes.add(h.full_hash)
        singletons.append(
            SingletonEntry(
                path=h.path,
                size=h.size,
                mtime=h.mtime,
                hash=h.full_hash,
            )
        )

    report = Report(
        roots=[Path(r).expanduser().resolve() for r in roots],
        total_files_scanned=file_count,
        total_groups=len(report_groups),
        total_reclaim_bytes=total_reclaim,
        groups=report_groups,
        singletons=singletons,
        archive_skips=archive_skips,
        discover=discover,
    )
    html_path, json_path = render_report(report, report_dir)
    store.close()

    tbl = Table(title="Scan complete")
    tbl.add_column("Metric")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Files scanned", str(file_count))
    tbl.add_row("Bundles hashed", str(walk_stats.bundles_hashed))
    tbl.add_row("Duplicate groups", str(len(report_groups)))
    tbl.add_row("Unique files", str(len(singletons)))
    tbl.add_row("Skipped archives", str(len(archive_skips)))
    tbl.add_row("Reclaimable (bytes)", str(total_reclaim))
    tbl.add_row("HTML report", str(html_path))
    tbl.add_row("JSON report", str(json_path))
    console.print(tbl)
    if discover:
        console.print(
            "[cyan]Discovery mode[/cyan]: no keepers proposed. "
            "`dc apply` will refuse this report."
        )


@app.command()
def apply(
    report_json: Annotated[
        Path, typer.Argument(help="Path to report.json emitted by `dc scan`.")
    ],
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Actually move files to Trash. Dry-run without this flag.",
        ),
    ] = False,
) -> None:
    """Move discarded duplicates to the Trash. Dry-run by default."""
    try:
        result = apply_report(report_json, commit=commit)
    except ApplyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    if not commit:
        console.print(
            f"[cyan]DRY-RUN[/cyan]: {result['verified']}/{result['planned']} "
            "path(s) verified for move to Trash. "
            "Pass [bold]--commit[/bold] to actually move."
        )
        if result["changed_or_missing"]:
            console.print(
                f"[yellow]{len(result['changed_or_missing'])} path(s) "
                "changed since scan[/yellow]:"
            )
            for msg in result["changed_or_missing"][:10]:
                console.print(f"  • {msg}")
    else:
        console.print(
            f"[green]Moved {result['moved']} file(s) to Trash.[/green]"
        )
        console.print(f"Manifest: {result['manifest_path']}")
        # CR#1 (pass 9): surface cloud discards that were validated but not
        # trashed yet — until sub-phase 5c wires Source.move_to_trash, cloud
        # entries flow through _validate_report_paths and land in the report
        # but are NEVER sent to any cloud provider.  Silent-drop would erode
        # user trust.
        cloud_deferred = result.get("cloud_deferred", 0)
        if cloud_deferred:
            console.print(
                f"[yellow]{cloud_deferred} cloud discard(s) deferred — "
                "cloud apply lands in v0.2 sub-phase 5c.[/yellow]"
            )


@app.command()
def undo(
    manifest: Annotated[
        Path, typer.Argument(help="Path to manifest.json from a previous apply.")
    ],
) -> None:
    """Restore files from a previous apply run."""
    result = restore_from_manifest(manifest)
    console.print(
        f"Restored {result['restored']}/{result['total']} file(s)."
    )
    # CR#1 (pass 9): same treatment for cloud entries on undo.
    cloud_deferred = result.get("cloud_deferred", 0)
    if cloud_deferred:
        console.print(
            f"[yellow]{cloud_deferred} cloud entry/entries deferred — "
            "cloud restore lands in v0.2 sub-phase 5d.[/yellow]"
        )
    if result["errors"]:
        console.print(f"[yellow]{len(result['errors'])} error(s):[/yellow]")
        for e in result["errors"][:20]:
            console.print(f"  • {e}")


@weights_app.command("show")
def weights_show() -> None:
    """Print the current scoring weights."""
    weights = load_weights()
    tbl = Table(title="Scoring weights")
    tbl.add_column("Signal")
    tbl.add_column("Weight", justify="right")
    for k, v in sorted(weights.items()):
        tbl.add_row(k, f"{v:+.2f}")
    console.print(tbl)


@weights_app.command("reset")
def weights_reset() -> None:
    """Rewrite weights.json with the built-in defaults."""
    write_default_weights()
    console.print(f"[green]Reset weights[/green]: {WEIGHTS_PATH}")


@cache_app.command("stats")
def cache_stats() -> None:
    """Print counts from the local hash cache."""
    store = Store()
    stats = store.cache_stats()
    tbl = Table(title="Cache stats")
    tbl.add_column("Metric")
    tbl.add_column("Value", justify="right")
    for k, v in stats.items():
        tbl.add_row(k, str(v))
    console.print(tbl)
    store.close()


@cache_app.command("clear")
def cache_clear() -> None:
    """Truncate the hash cache."""
    store = Store()
    store.clear_cache()
    store.close()
    console.print("[green]Cache cleared.[/green]")


def _resolve_client_credentials(
    type_: str, client_secret: Path | None
) -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` — BYO override wins over bundled."""
    if client_secret is not None:
        return load_client_secret_json(client_secret)
    if type_ == "gdrive":
        client_id = BUNDLED_GDRIVE_CLIENT_ID
        # B1: refuse to launch a doomed OAuth flow when the bundled client id
        # is still the pre-release placeholder — Google would return a raw
        # ``invalid_client`` that is confusing to end users.
        if client_id.endswith("_TO_REPLACE"):
            console.print(
                "[red]This build's bundled Google OAuth client is not "
                "registered yet.[/red] Use [cyan]--client-secret path/to/oauth.json"
                "[/cyan] with your own Cloud Console-downloaded client "
                "credentials until the release build ships."
            )
            raise typer.Exit(1)
        return client_id, BUNDLED_GDRIVE_CLIENT_SECRET
    if type_ == "onedrive":
        client_id = BUNDLED_ONEDRIVE_CLIENT_ID
        # B1 mirror: refuse the placeholder Microsoft client id so users get
        # an actionable message instead of Entra's raw ``invalid_client``
        # response.  Microsoft public clients ship WITHOUT a secret (PKCE
        # only) so the returned secret is intentionally the empty string.
        if client_id.endswith("_TO_REPLACE"):
            console.print(
                "[red]This build's bundled Microsoft OAuth client is not "
                "registered yet.[/red] Use [cyan]--client-secret path/to/msal.json"
                "[/cyan] with your own Entra App Registration id until the "
                "release build ships."
            )
            raise typer.Exit(1)
        return client_id, BUNDLED_ONEDRIVE_CLIENT_SECRET
    raise typer.BadParameter(f"Unknown auth type: {type_}")


def _default_account_id(type_: str, label: str | None, existing_ids: list[str]) -> str:
    """Return a fresh ``id`` following the ``<type>:<label>`` convention."""
    if label:
        return f"{type_}:{label}"
    base = f"{type_}:personal"
    if base not in existing_ids:
        return base
    i = 2
    while f"{base}-{i}" in existing_ids:
        i += 1
    return f"{base}-{i}"


@auth_app.command("add")
def auth_add(
    type_: Annotated[
        str,
        typer.Argument(
            metavar="TYPE",
            help="Cloud provider type — 'gdrive' or 'onedrive'.",
        ),
    ],
    label: Annotated[
        str | None,
        typer.Option("--label", help="Custom label; becomes the account_id suffix."),
    ] = None,
    client_secret: Annotated[
        Path | None,
        typer.Option(
            "--client-secret",
            help="Optional path to a downloaded Cloud Console client_secret JSON.",
        ),
    ] = None,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help=(
                "Google only. Request 'drive' scope instead of 'drive.file'. "
                "Requires additional consent."
            ),
        ),
    ] = False,
    port_hint: Annotated[
        int,
        typer.Option(
            "--port",
            help="OAuth callback port hint (0 = random). For testing only.",
        ),
    ] = 0,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Overwrite an existing account with the same id (non-interactive "
                "callers must pass this to re-authorise)."
            ),
        ),
    ] = False,
) -> None:
    """Register a cloud account by running the OAuth localhost flow."""
    if type_ not in {"gdrive", "onedrive"}:
        console.print(
            f"[red]Unsupported auth type[/red]: {type_}. "
            "Supported: 'gdrive', 'onedrive'."
        )
        raise typer.Exit(2)
    registry = AccountsRegistry()
    tokens = TokenStore()
    existing_ids = [e.id for e in registry.load()]
    account_id = _default_account_id(type_, label, existing_ids)
    # B4: refuse to overwrite an existing account before starting the OAuth
    # flow — the previous flow silently clobbered the token file (via
    # ``TokenStore.save``) BEFORE the ``DuplicateAccountError`` check tripped.
    if account_id in existing_ids and not force:
        import sys

        if sys.stdin.isatty():
            confirm = typer.confirm(
                f"Account {account_id!r} already registered. Re-authorize?",
                default=False,
            )
            if not confirm:
                console.print("[yellow]Aborted.[/yellow]")
                raise typer.Exit(1)
        else:
            console.print(
                f"[red]Account {account_id!r} already exists.[/red] "
                f"Use [cyan]dc auth remove {account_id}[/cyan] first, "
                "or pass [cyan]--force[/cyan] to overwrite."
            )
            raise typer.Exit(2)
    try:
        client_id, secret = _resolve_client_credentials(type_, client_secret)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Failed to load client secret[/red]: {e}")
        raise typer.Exit(2) from e

    if type_ == "gdrive":
        auth_url_base = GDRIVE_AUTH_URL
        token_url = GDRIVE_TOKEN_URL
        scopes = list(GDRIVE_FULL_SCOPES if full else GDRIVE_DEFAULT_SCOPES)
        extra_auth_params: dict[str, str] = {
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
    else:  # onedrive
        if full:
            console.print(
                "[yellow]Warning[/yellow]: --full is a Google-only flag; "
                "OneDrive always uses Files.ReadWrite + offline_access."
            )
        auth_url_base = ONEDRIVE_AUTH_URL
        token_url = ONEDRIVE_TOKEN_URL
        scopes = list(ONEDRIVE_DEFAULT_SCOPES)
        # Entra requires ``prompt=select_account`` for the multi-account
        # flow so the user can pick between signed-in identities.  We do
        # not send ``access_type`` (Google-only).
        extra_auth_params = {"prompt": "select_account"}

    console.print(f"Starting OAuth flow for [cyan]{account_id}[/cyan]…")
    try:
        token_data = run_localhost_flow(
            auth_url_base=auth_url_base,
            client_id=client_id,
            client_secret=secret or None,
            scopes=scopes,
            token_url=token_url,
            port_hint=port_hint,
            extra_auth_params=extra_auth_params,
        )
    except OAuthFlowError as e:
        console.print(f"[red]OAuth flow failed[/red]: {e}")
        raise typer.Exit(1) from e

    token_data.update(
        {
            "account_id": account_id,
            "type": type_,
            "client_id": client_id,
            "client_secret": secret,
        }
    )
    tokens.save(account_id, token_data)
    user_email = str(token_data.get("user_email") or token_data.get("email") or "")
    # B4: when re-authorising an existing id (interactive confirm or --force),
    # drop the stale accounts.toml row so ``registry.add`` succeeds without
    # tripping DuplicateAccountError.
    if account_id in existing_ids:
        registry.remove(account_id)
    try:
        registry.add(
            AccountEntry(
                id=account_id,
                type=type_,
                label=label or account_id.split(":", 1)[-1],
                user=user_email,
                added_ts=AccountsRegistry.now_ts(),
            )
        )
    except DuplicateAccountError as e:
        console.print(f"[yellow]Warning[/yellow]: {e}")
    console.print(
        f"[green]Authorized[/green] {account_id}"
        + (f" as {user_email}" if user_email else "")
    )


@auth_app.command("list")
def auth_list() -> None:
    """Print all configured accounts (no token values)."""
    registry = AccountsRegistry()
    accounts = registry.load()
    if not accounts:
        console.print(f"No accounts registered at {ACCOUNTS_PATH}.")
        return
    tbl = Table(title="Configured accounts")
    tbl.add_column("id")
    tbl.add_column("type")
    tbl.add_column("user")
    tbl.add_column("added")
    for e in accounts:
        tbl.add_row(e.id, e.type, e.user or "-", e.added_ts or "-")
    console.print(tbl)


@auth_app.command("test")
def auth_test(
    account_id: Annotated[str, typer.Argument(help="Account id from `dc auth list`.")],
) -> None:
    """Verify a token still works by reading a single file from the provider."""
    tokens = TokenStore()
    try:
        data = tokens.load(account_id)
    except TokenPermissionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if data is None:
        console.print(f"[red]No token for[/red] {account_id}. Run `dc auth add`.")
        raise typer.Exit(1)
    kind = str(data.get("type", ""))
    if kind == "gdrive":
        try:
            creds = _google_credentials_from_token(data)
            # Lazy import so unit tests + no-deps environments can import cli.py.
            from duplicate_cleaner.sources.gdrive import GoogleDriveSource

            src = GoogleDriveSource(account_id=account_id, credentials=creds)
            # A trivial 1-item listing exercises token refresh + API round-trip.
            it = src.list_files()
            first = next(iter(it), None)
            console.print(
                f"[green]OK[/green]: {account_id} — "
                + (f"first file: {first.path}" if first is not None else "empty drive")
            )
        except Exception as e:
            console.print(f"[red]{account_id} check failed[/red]: {e}")
            raise typer.Exit(1) from e
    elif kind == "onedrive":
        try:
            # Bearer token is used as-is here — an expired access_token
            # will surface as a SourceAuthError from the source layer,
            # prompting the user to re-run ``dc auth add --force``.
            from duplicate_cleaner.sources.onedrive import OneDriveSource

            access_token = str(data.get("access_token") or "")
            if not access_token:
                console.print(
                    f"[red]{account_id}: no access_token in stored blob[/red]"
                )
                raise typer.Exit(1)
            src_od = OneDriveSource(
                account_id=account_id,
                token_provider=lambda: access_token,
            )
            it_od = src_od.list_files()
            first_od = next(iter(it_od), None)
            console.print(
                f"[green]OK[/green]: {account_id} — "
                + (
                    f"first file: {first_od.path}"
                    if first_od is not None
                    else "empty drive"
                )
            )
        except Exception as e:
            console.print(f"[red]{account_id} check failed[/red]: {e}")
            raise typer.Exit(1) from e
    else:
        console.print(f"[yellow]auth test not implemented for type[/yellow]: {kind}")
        raise typer.Exit(0)


@auth_app.command("remove")
def auth_remove(
    account_id: Annotated[str, typer.Argument(help="Account id to remove.")],
) -> None:
    """Best-effort revoke at the provider, delete the token, drop from accounts.toml."""
    tokens = TokenStore()
    registry = AccountsRegistry()
    data = None
    try:
        data = tokens.load(account_id)
    except TokenPermissionError as e:
        console.print(f"[yellow]Warning[/yellow]: {e}")
    if data is not None:
        refresh = str(data.get("refresh_token") or data.get("access_token") or "")
        kind = str(data.get("type", ""))
        if refresh and kind == "gdrive":
            ok = revoke_token(GDRIVE_REVOKE_URL, refresh)
            if not ok:
                console.print(
                    "[yellow]Warning[/yellow]: provider revoke call failed — "
                    "local token will still be removed."
                )
        elif kind == "onedrive":
            # Microsoft does not expose a token-revocation endpoint the
            # way Google does; ``/logout`` invalidates the browser
            # session only.  The user-actionable step is to visit
            # https://account.live.com/consent/Manage and remove the app
            # — we surface that link and delete the local token below.
            console.print(
                "[cyan]Note[/cyan]: Microsoft does not provide a "
                "programmatic revoke.  To fully revoke access visit "
                "https://account.live.com/consent/Manage and remove "
                "DuplicateCleaner from the app list."
            )
    tokens.delete(account_id)
    removed = registry.remove(account_id)
    console.print(
        f"[green]Removed[/green] {account_id}"
        + ("" if removed else " (no accounts.toml entry to drop)")
    )


@sources_app.command("list")
def sources_list() -> None:
    """List every source id available: 'local' plus each registered cloud account."""
    tbl = Table(title="Sources")
    tbl.add_column("id")
    tbl.add_column("type")
    tbl.add_column("user")
    tbl.add_row("local", "local", "-")
    for e in AccountsRegistry().load():
        tbl.add_row(e.id, e.type, e.user or "-")
    console.print(tbl)


def _google_credentials_from_token(data: dict[str, object]) -> object:
    """Build a google.oauth2 Credentials object from a stored token blob."""
    from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]

    raw_scopes = data.get("scopes")
    scopes: list[str] = (
        [str(s) for s in raw_scopes]
        if isinstance(raw_scopes, list)
        else list(GDRIVE_DEFAULT_SCOPES)
    )
    return Credentials(
        token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or "") or None,
        token_uri=GDRIVE_TOKEN_URL,
        client_id=str(data.get("client_id") or ""),
        client_secret=str(data.get("client_secret") or ""),
        scopes=scopes,
    )
