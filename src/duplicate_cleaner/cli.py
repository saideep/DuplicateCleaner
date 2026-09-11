"""Typer entrypoint — `dc scan | apply | undo | init | weights | cache`."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

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
    BUNDLED_GPHOTOS_CLIENT_ID,
    BUNDLED_GPHOTOS_CLIENT_SECRET,
    BUNDLED_ONEDRIVE_CLIENT_ID,
    BUNDLED_ONEDRIVE_CLIENT_SECRET,
    GDRIVE_AUTH_URL,
    GDRIVE_DEFAULT_SCOPES,
    GDRIVE_FULL_SCOPES,
    GDRIVE_REVOKE_URL,
    GDRIVE_TOKEN_URL,
    GPHOTOS_AUTH_URL,
    GPHOTOS_DEFAULT_SCOPES,
    GPHOTOS_TOKEN_URL,
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
from duplicate_cleaner.compare.tree import (
    ProjectDuplicateGroup,
    ProjectInfo,
    aggregate_project_duplicates,
    detect_project_dirs,
)
from duplicate_cleaner.config import (
    CONFIG_PATH,
    WEIGHTS_PATH,
    load_config,
    load_weights,
    write_default_config,
    write_default_weights,
)
from duplicate_cleaner.hash.pipeline import HashedRecord, hash_records
from duplicate_cleaner.hash.reconciliation import (
    make_budget,
    reconcile_cross_source,
)
from duplicate_cleaner.paths import validate_scan_root_candidate
from duplicate_cleaner.report.render import render_report
from duplicate_cleaner.report.schema import (
    ArchiveSkipEntry,
    Report,
    ReportGroup,
    ReportMember,
    ReportSignal,
    SingletonEntry,
    TreeDiffEntry,
)
from duplicate_cleaner.scan.walk import FileRecord, WalkStats
from duplicate_cleaner.score.rules import (
    is_project_tree_backup_copy,
    score_groups,
)
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


def _build_tree_report_groups(
    tree_groups: list[ProjectDuplicateGroup],
    projects: dict[Path, ProjectInfo],
    weights: dict[str, float],
) -> list[ReportGroup]:
    """Convert every :class:`ProjectDuplicateGroup` into a :class:`ReportGroup`.

    v0.4: each aggregate group produces one ``kind="tree"`` ReportGroup
    whose members carry directory paths.  The keeper member gets
    ``is_proposed_keeper=True``; every other member gets scored with the
    two tree-specific signals (backup-marker ancestor, older git HEAD).
    """
    out: list[ReportGroup] = []
    for i, tg in enumerate(tree_groups):
        members: list[ReportMember] = []
        for j, root in enumerate(tg.members):
            proj = projects[root]
            is_keeper = j == tg.proposed_keeper_index
            signals: list[ReportSignal] = []
            score = 0.0
            if is_keeper and tg.keeper_reason:
                signals.append(
                    ReportSignal(name=f"keeper: {tg.keeper_reason}", contribution=0.0)
                )
            if not is_keeper:
                if is_project_tree_backup_copy(root):
                    w = weights.get("is_project_tree_backup_copy", -5.0)
                    signals.append(
                        ReportSignal(
                            name="project tree under backup folder", contribution=w
                        )
                    )
                    score += w
                # ``git HEAD older`` fires when we can compare the keeper's
                # HEAD to this member's HEAD and this one is older.
                if tg.keeper_reason.startswith("newer git HEAD"):
                    w = weights.get("git_head_older", -3.0)
                    signals.append(
                        ReportSignal(
                            name="git HEAD older than keeper", contribution=w
                        )
                    )
                    score += w
                signals.append(
                    ReportSignal(
                        name=f"similarity {tg.similarity:.2%}",
                        contribution=0.0,
                    )
                )
            members.append(
                ReportMember(
                    path=root,
                    size=proj.total_bytes,
                    mtime=0.0,
                    hash="",
                    score=score,
                    signals=signals,
                    is_proposed_keeper=is_keeper,
                    is_informational=False,
                )
            )
        tree_diff_entries: list[TreeDiffEntry] = [
            TreeDiffEntry(
                relative_path=d.relative_path,
                hashes_per_member=list(d.hashes_per_member),
            )
            for d in tg.differing_files
        ]
        out.append(
            ReportGroup(
                id=f"tree-{i:04d}",
                kind="tree",
                size=tg.total_bytes,
                hash=f"tree-{i:04d}",
                reclaim_bytes=tg.total_bytes,
                members=members,
                identical_file_count=tg.identical_files,
                tree_diff=tree_diff_entries,
                similarity_pct=tg.similarity * 100.0,
            )
        )
    return out


def _filter_exact_groups_covered_by_trees(
    exact_groups: list[ReportGroup],
    project_roots: list[Path],
) -> tuple[list[ReportGroup], int]:
    """Drop exact-duplicate groups whose members ALL sit inside detected projects.

    v0.4: a project tree aggregate REPRESENTS all its members' duplicate
    file matches.  Continuing to emit per-file exact groups for members
    fully covered by aggregates buries the tree entry.  Returns the
    filtered list plus a count of dropped groups so the CLI summary can
    surface how many redundant rows were collapsed.
    """
    if not project_roots:
        return exact_groups, 0
    kept: list[ReportGroup] = []
    dropped = 0
    for g in exact_groups:
        if g.kind != "exact":
            kept.append(g)
            continue
        all_covered = True
        for m in g.members:
            mp = Path(m.path)
            if not any(_lexical_contains(root, mp) for root in project_roots):
                all_covered = False
                break
        if all_covered and g.members:
            dropped += 1
            continue
        kept.append(g)
    return kept, dropped


def _lexical_contains(parent: Path, child: Path) -> bool:
    """Purely-lexical containment check (no filesystem probe)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


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
    min_project_similarity: Annotated[
        float | None,
        typer.Option(
            "--min-project-similarity",
            help=(
                "Jaccard threshold above which two detected project "
                "directories collapse to a single tree-aggregate group. "
                "Overrides config.min_project_similarity (default 0.90)."
            ),
        ),
    ] = None,
) -> None:
    """Walk, hash, group, score, and write a report."""
    # v0.2 sub-milestone 5e: cross-source scan is enabled.  The 5b refusal
    # guard is gone; any registered cloud source id in ``--sources`` is
    # constructed at scan time and its ``list_files()`` stream is chained
    # into the local walk.  Same-algo cloud pairs (two Google accounts,
    # two OneDrive accounts) group directly via ``foreign_hash``.  A full
    # BLAKE3 reconcile across mixed algos (gdrive vs local, gdrive vs
    # onedrive) is deferred to 5f — until then those buckets surface as
    # separate per-source groups and the scorer's cross-source signals
    # gate any cross-source discard proposal.
    requested_sources = [s.strip() for s in sources.split(",") if s.strip()]
    registry_for_scan = AccountsRegistry()
    known_ids = {"local"} | {e.id for e in registry_for_scan.load()}
    for s in requested_sources:
        if s not in known_ids:
            console.print(
                f"[red]Refusing to scan[/red]: source {s!r} is not "
                "registered.  Run `dc auth list` to see registered "
                "accounts; add one with `dc auth add <provider>`."
            )
            raise typer.Exit(2)
    cloud_source_ids = [s for s in requested_sources if s != "local"]
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

        # v0.2 sub-milestone 5e: build cloud sources for every requested
        # non-local id.  Read-only scan (``is_read_only_scan=True``) is
        # correct here — the scanner never trashes; ``apply`` builds its
        # own sources with the flag off.  v0.2.1 wires the cross-algo
        # BLAKE3 reconciler (see ``hash/reconciliation.reconcile_cross_source``)
        # so mixed local + cloud size buckets converge to a single
        # canonical hash space, and same-algo cloud pairs continue to
        # group by ``foreign_hash`` for free.
        cloud_sources = _build_scan_sources(cloud_source_ids)

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
            # Chain every cloud source's ``list_files()`` after the local
            # walk.  Cloud records already carry ``foreign_hash`` +
            # ``source_id`` + ``is_shared``; the ``foreign_hash`` is
            # stamped as ``precomputed_full_hash`` so ``hash_records``
            # yields the record verbatim without a partial/full-hash pass.
            for cs in cloud_sources:
                for rec in cs.list_files():
                    file_count += 1
                    if rec.foreign_hash:
                        yield FileRecord(
                            path=rec.path,
                            size=rec.size,
                            mtime=rec.mtime,
                            inode=rec.inode,
                            dev=rec.dev,
                            nlink=rec.nlink,
                            is_archive_member=rec.is_archive_member,
                            is_bundle=rec.is_bundle,
                            precomputed_full_hash=rec.foreign_hash,
                            source_id=rec.source_id,
                            foreign_hash=rec.foreign_hash,
                            etag=rec.etag,
                            cloud_file_id=rec.cloud_file_id,
                            owner=rec.owner,
                            is_shared=rec.is_shared,
                        )
                    else:
                        # A cloud record without a foreign_hash cannot
                        # participate in exact-duplicate grouping without
                        # a reconcile download.  Deferred to 5f — for
                        # now, log and skip so the scan completes.
                        logging.getLogger(__name__).debug(
                            "Skipping cloud record %s from %s: no foreign_hash",
                            rec.path,
                            rec.source_id,
                        )

        hashed_iter = hash_records(
            _walk_iter(), store, include_singletons=True
        )
        # Materialise so we can index singletons AND groups without
        # re-walking the tree.
        all_hashed: list[HashedRecord] = list(hashed_iter)

        # v0.2.1: reconcile cross-source size buckets to a single
        # canonical hash space.  Same-algo buckets (all local BLAKE3, or
        # two gdrive accounts sharing MD5) already collide correctly and
        # cost zero I/O.  Cross-algo buckets (local BLAKE3 + gdrive MD5)
        # stream cloud bytes through ``Source.read_bytes`` to compute a
        # canonical BLAKE3 for every cloud member, cached by
        # ``(source_id, cloud_file_id, etag)`` so subsequent scans re-use
        # the result.  Any bucket whose reconciliation would blow the
        # cumulative download cap is recorded in ``not_yet_hashed_buckets``.
        sources_for_reconcile: dict[str, Any] = {
            local_source.id: local_source,
            **{cs.id: cs for cs in cloud_sources},
        }
        budget = make_budget(max_cloud_download_mb)
        all_hashed, not_yet_hashed = reconcile_cross_source(
            all_hashed,
            sources_for_reconcile,
            store,
            budget,
        )
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
                source_id=m.source_id,
                cloud_file_id=m.cloud_file_id,
                etag=m.etag,
                owner=m.owner,
                is_shared=m.is_shared,
                reconciled=m.reconciled,
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

    # v0.4 project-tree aggregation: after exact + archive groups are built,
    # collapse pairs of duplicate project directories into single "tree"
    # groups.  Skipped in discover mode (aggregation is a keeper-proposing
    # step and discover mode proposes zero keepers).  Any exact-duplicate
    # group whose members ALL sit inside detected project roots is
    # dropped from the report — the aggregate carries the same
    # information at directory granularity.
    tree_report_groups: list[ReportGroup] = []
    exact_groups_covered_dropped = 0
    project_roots_detected: list[Path] = []
    if not discover:
        projects = detect_project_dirs(all_hashed)
        # K4 (audit pass 14): CLI flag overrides Config default so the
        # value can be persisted in config.toml AND overridden per run.
        effective_similarity = (
            min_project_similarity
            if min_project_similarity is not None
            else cfg.min_project_similarity
        )
        aggregate = aggregate_project_duplicates(
            projects, threshold=effective_similarity
        )
        if aggregate:
            tree_report_groups = _build_tree_report_groups(
                aggregate, projects, weights
            )
            # Union of every member root in every aggregate group — a
            # per-file exact group whose members ALL sit inside one of
            # these roots is redundant with the tree entry.
            for tg in aggregate:
                project_roots_detected.extend(tg.members)
            report_groups, exact_groups_covered_dropped = (
                _filter_exact_groups_covered_by_trees(
                    report_groups, project_roots_detected
                )
            )
            # Roll up reclaim: subtract dropped exact groups' reclaim from
            # the running total (they no longer contribute), then add the
            # tree groups' aggregate reclaim.  Since we removed them
            # already we must not double-count; recompute total_reclaim
            # from scratch.
            total_reclaim = sum(rg.reclaim_bytes for rg in report_groups)
            for trg in tree_report_groups:
                report_groups.append(trg)
                total_reclaim += trg.reclaim_bytes

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
        not_yet_hashed_buckets=not_yet_hashed,
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
    if tree_report_groups:
        tbl.add_row("Project-tree groups", str(len(tree_report_groups)))
        tbl.add_row(
            "Per-file groups collapsed",
            str(exact_groups_covered_dropped),
        )
    tbl.add_row("Unique files", str(len(singletons)))
    tbl.add_row("Skipped archives", str(len(archive_skips)))
    if not_yet_hashed:
        tbl.add_row(
            "Cross-source buckets deferred", str(len(not_yet_hashed))
        )
    tbl.add_row("Reclaimable (bytes)", str(total_reclaim))
    tbl.add_row("HTML report", str(html_path))
    tbl.add_row("JSON report", str(json_path))
    console.print(tbl)
    if not_yet_hashed:
        console.print(
            f"[yellow]{len(not_yet_hashed)} cross-source size bucket(s) "
            "were left un-reconciled because reconciliation would exceed "
            "[bold]--max-cloud-download-mb[/bold].[/yellow] Raise the cap "
            "and re-scan to fold them into duplicate groups."
        )
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
    force_refresh: Annotated[
        bool,
        typer.Option(
            "--force-refresh",
            help=(
                "Force a token refresh for every registered cloud account "
                "before dispatch.  Useful for long-idle credentials or "
                "immediately after `dc auth add --force`."
            ),
        ),
    ] = False,
) -> None:
    """Move discarded duplicates to the Trash. Dry-run by default."""
    # v0.2 sub-phase 5c: build the sources map at apply time so
    # ``apply_report`` can dispatch cloud discards to their Source
    # implementations.  A pure-local report never needs the map — we still
    # build it (cheap) so a stale account_id in the report surfaces here
    # rather than during the move loop.
    registry = AccountsRegistry()
    sources_map: dict[str, Any] = {}
    try:
        sources_map = _build_sources_for_apply(
            registry, force_refresh=force_refresh
        )
    except ApplyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    # v0.4: project-tree discards are validated against active_homes as
    # a defense-in-depth check — a whole-directory move has a much larger
    # blast radius than a file move, so we require the target to sit
    # inside a declared active home.  Loading config here means the
    # apply path fails fast when active_homes is empty and a tree entry
    # is present, without embedding config discovery inside the mover.
    cfg_for_apply = None
    active_homes_for_apply: list[Path] | None = None
    try:
        cfg_for_apply = load_config()
    except FileNotFoundError:
        cfg_for_apply = None
    if cfg_for_apply is not None:
        active_homes_for_apply = list(cfg_for_apply.active_homes)

    try:
        result = apply_report(
            report_json,
            commit=commit,
            registry=registry,
            sources=sources_map or None,
            active_homes=active_homes_for_apply,
        )
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
        # v0.2 sub-phase 5c: apply_report now dispatches cloud discards
        # through Source.move_to_trash, so the summary carries a per-family
        # count.  ``moved_local`` / ``moved_cloud`` fall back to zero on a
        # v0.1.1-shaped result dict (no cloud entries).
        # v0.2 sub-phase 5d: cloud entries can be per-entry skipped
        # (SourceNotFoundError / SourcePermissionError during check_drift
        # or move_to_trash — the file was already gone or the account lost
        # permission).  Surface the skip count so users know the applied
        # set is smaller than the planned set.
        moved_local = int(result.get("moved_local", result.get("moved", 0)))
        moved_cloud = int(result.get("moved_cloud", 0))
        moved_tree = int(result.get("moved_tree", 0))
        skipped_cloud = int(result.get("skipped_cloud", 0))
        skipped_tree = int(result.get("skipped_tree", 0))
        console.print(
            f"[green]Moved {moved_local} local file(s), {moved_cloud} cloud "
            f"file(s), {moved_tree} project tree(s) to Trash.[/green]"
        )
        if skipped_cloud:
            console.print(
                f"[yellow]Skipped {skipped_cloud} cloud file(s)[/yellow] "
                "(source-not-found or permission errors — see logs)."
            )
        if skipped_tree:
            console.print(
                f"[yellow]Skipped {skipped_tree} project tree(s) due to "
                "errors[/yellow] (see logs)."
            )
        console.print(f"Manifest: {result['manifest_path']}")


def _build_sources_for_apply(
    registry: AccountsRegistry,
    *,
    force_refresh: bool,
) -> dict[str, Any]:
    """Construct the ``source_id -> Source`` map for ``apply_report``.

    Walks :class:`AccountsRegistry` and instantiates one
    :class:`GoogleDriveSource` / :class:`OneDriveSource` per registered
    account, with ``is_read_only_scan=False`` so the source's trash
    dispatch is enabled.  ``force_refresh`` bypasses the cached access
    token and issues a proactive refresh on the OneDrive side; Google's
    ``google.oauth2.credentials.Credentials`` refreshes automatically when
    the API layer sees an expired token so no separate call is needed.

    Missing tokens (account registered but ``dc auth add`` was interrupted
    before write) are logged and skipped — a cloud discard against that
    source will surface later as an ApplyError in ``apply_report``.
    """
    tokens = TokenStore()
    out: dict[str, Any] = {}
    for entry in registry.load():
        # v0.6: iCloud has no token file — it's an accounts.toml-only entry
        # pointing at the local Photos.photoslibrary bundle.  Build the
        # source without touching TokenStore.
        if entry.type == "icloud":
            from duplicate_cleaner.sources.iclouddrive_photos import (
                iCloudPhotosSource,
            )

            out[entry.id] = iCloudPhotosSource(
                account_id=entry.id,
                photos_library_path=Path(entry.user) if entry.user else None,
                # Read-only permanently; the source refuses trash regardless
                # of this flag but we keep the tripwire pattern for parity.
                is_read_only_scan=True,
            )
            continue
        try:
            data = tokens.load(entry.id)
        except TokenPermissionError as e:
            raise ApplyError(
                f"Token for {entry.id!r} rejected: {e}. Fix file mode and retry."
            ) from e
        if data is None:
            log = logging.getLogger(__name__)
            log.warning(
                "No token for account %s (registered in accounts.toml but no "
                "token file); cloud discards for this source will fail.",
                entry.id,
            )
            continue
        kind = str(data.get("type", entry.type))
        if kind == "gdrive":
            out[entry.id] = _build_gdrive_source(
                entry.id, data, force_refresh=force_refresh
            )
        elif kind == "gphotos":
            out[entry.id] = _build_gphotos_source(
                entry.id, data, force_refresh=force_refresh
            )
        elif kind == "onedrive":
            out[entry.id] = _build_onedrive_source(
                entry.id, data, force_refresh=force_refresh
            )
        else:
            # Silently ignore unknown provider types — future providers can
            # be wired here without changing every apply-time call site.
            log = logging.getLogger(__name__)
            log.warning(
                "Unknown auth type %r for account %s — skipping in "
                "apply-time sources map.",
                kind,
                entry.id,
            )
    return out


def _build_scan_sources(source_ids: list[str]) -> list[Any]:
    """Construct read-only cloud sources for ``dc scan --sources``.

    v0.2 sub-milestone 5e: symmetric to :func:`_build_sources_for_apply`
    but at scan time.  Every source is built with
    ``is_read_only_scan=True`` — the runtime tripwire so a bug in the
    scan path cannot trash a cloud file even accidentally.

    A missing token file is a hard error at scan time — silently omitting
    the source would produce a report with an incomplete picture and the
    scorer's cross-source signals depend on seeing every member.
    """
    if not source_ids:
        return []
    tokens = TokenStore()
    registry = AccountsRegistry()
    by_id: dict[str, AccountEntry] = {e.id: e for e in registry.load()}
    out: list[Any] = []
    for sid in source_ids:
        entry = by_id.get(sid)
        if entry is None:
            raise typer.BadParameter(
                f"source {sid!r} not registered — run "
                f"`dc auth add {sid.split(':', 1)[0]} <label>`"
            )
        kind_from_entry = str(entry.type or "")
        # iCloud has no token file — it's registered in accounts.toml only.
        if kind_from_entry == "icloud":
            from duplicate_cleaner.sources.iclouddrive_photos import (
                iCloudPhotosSource,
            )

            out.append(
                iCloudPhotosSource(
                    account_id=sid,
                    photos_library_path=Path(entry.user) if entry.user else None,
                    is_read_only_scan=True,
                )
            )
            continue
        data = tokens.load(sid)
        if data is None:
            raise typer.BadParameter(
                f"source {sid!r} has no stored token — re-run "
                "`dc auth add --force` for the account"
            )
        kind = str(data.get("type", entry.type))
        if kind == "gdrive":
            from duplicate_cleaner.sources.gdrive import GoogleDriveSource

            creds = _google_credentials_from_token(data)
            out.append(
                GoogleDriveSource(
                    account_id=sid,
                    credentials=creds,
                    is_read_only_scan=True,
                )
            )
        elif kind == "gphotos":
            from duplicate_cleaner.sources.gphotos import GooglePhotosSource

            creds = _google_credentials_from_token(
                data, default_scopes=list(GPHOTOS_DEFAULT_SCOPES)
            )
            out.append(
                GooglePhotosSource(
                    account_id=sid,
                    credentials=creds,
                    client_config={"user_email": str(data.get("user_email") or "")},
                    is_read_only_scan=True,
                )
            )
        elif kind == "onedrive":
            from duplicate_cleaner.sources.onedrive import OneDriveSource

            access_token = str(data.get("access_token") or "")
            if not access_token:
                access_token = _refresh_onedrive_token(
                    sid, tokens=tokens, initial_data=data
                )
            def _make_token_provider(t: str) -> Callable[[], str]:
                def _tp() -> str:
                    return t

                return _tp

            out.append(
                OneDriveSource(
                    account_id=sid,
                    token_provider=_make_token_provider(access_token),
                    is_read_only_scan=True,
                )
            )
        else:
            raise typer.BadParameter(f"Unknown source type: {kind!r}")
    return out


def _build_gdrive_source(
    account_id: str, data: dict[str, object], *, force_refresh: bool
) -> object:
    """Instantiate a GoogleDriveSource with trash dispatch enabled.

    ``google.oauth2.credentials.Credentials`` auto-refreshes on the next
    API call whenever ``refresh_token`` is set — no explicit refresh is
    required.  ``force_refresh=True`` calls ``creds.refresh`` proactively
    to surface a bad refresh token BEFORE the apply loop.
    """
    from duplicate_cleaner.sources.gdrive import GoogleDriveSource

    creds = _google_credentials_from_token(data)
    if force_refresh:
        try:
            from google.auth.transport.requests import (  # type: ignore[import-not-found]
                Request,
            )

            creds.refresh(Request())  # type: ignore[attr-defined]
        except Exception as e:
            raise ApplyError(
                f"--force-refresh: failed to refresh Google token for "
                f"{account_id!r}: {e}. Run `dc auth add gdrive --force`."
            ) from e
    return GoogleDriveSource(
        account_id=account_id,
        credentials=creds,
        is_read_only_scan=False,
    )


def _build_gphotos_source(
    account_id: str, data: dict[str, object], *, force_refresh: bool
) -> object:
    """Instantiate a GooglePhotosSource — read-only in v0.6.

    Symmetric with :func:`_build_gdrive_source`: same
    ``google.oauth2.credentials.Credentials`` refresh semantics.  v0.6
    keeps ``is_read_only_scan=True`` even at apply time — the source
    refuses trash calls until v0.6.1 wires the scope escalation.
    """
    from duplicate_cleaner.sources.gphotos import GooglePhotosSource

    creds = _google_credentials_from_token(
        data, default_scopes=list(GPHOTOS_DEFAULT_SCOPES)
    )
    if force_refresh:
        try:
            from google.auth.transport.requests import (  # type: ignore[import-not-found]
                Request,
            )

            creds.refresh(Request())  # type: ignore[attr-defined]
        except Exception as e:
            raise ApplyError(
                f"--force-refresh: failed to refresh Google Photos token for "
                f"{account_id!r}: {e}. Run `dc auth add gphotos --force`."
            ) from e
    return GooglePhotosSource(
        account_id=account_id,
        credentials=creds,
        client_config={"user_email": str(data.get("user_email") or "")},
        # v0.6: still read-only at apply time.  ``move_to_trash`` refuses
        # with a v0.6.1 deferral message either way.
        is_read_only_scan=True,
    )



# Audit pass 10 finding #6 — msal.PublicClientApplication cache.  MSAL
# builds an in-memory TokenCache internally, so re-constructing the app
# per refresh call was throwing away MSAL's own bookkeeping alongside the
# CPU cost of the constructor.  Cache once per account_id.  Single-threaded
# CLI so a plain dict is fine.
_MSAL_APP_CACHE: dict[str, Any] = {}


def _msal_app_for(account_id: str, client_id: str) -> Any:
    """Return a cached ``msal.PublicClientApplication`` for ``account_id``."""
    key = f"{account_id}|{client_id}"
    app_client = _MSAL_APP_CACHE.get(key)
    if app_client is not None:
        return app_client
    try:
        import msal  # type: ignore[import-not-found]
    except ImportError as e:
        raise ApplyError(
            "msal is required to refresh OneDrive tokens but is not "
            "installed.  Install with `pip install msal>=1.31`."
        ) from e
    app_client = msal.PublicClientApplication(
        client_id,
        authority="https://login.microsoftonline.com/consumers",
    )
    _MSAL_APP_CACHE[key] = app_client
    return app_client


def _refresh_onedrive_token(
    account_id: str,
    *,
    tokens: TokenStore | None = None,
    initial_data: dict[str, object] | None = None,
) -> str:
    """Refresh the OneDrive access_token for ``account_id`` and return it.

    Audit pass 10 finding #2: Microsoft rotates the ``refresh_token`` on
    every ``acquire_token_by_refresh_token`` call — if we drop the new
    value the stored refresh_token goes stale within days/weeks and every
    later ``dc apply --commit`` fails auth.  This helper persists the new
    refresh_token (and the new access_token) back into :class:`TokenStore`
    whenever the response carries one.

    ``initial_data`` is the pre-loaded token blob; the helper re-reads from
    disk if omitted so that concurrent refreshes converge.  ``tokens`` is
    injectable for tests.
    """
    store = tokens if tokens is not None else TokenStore()
    if initial_data is None:
        loaded = store.load(account_id)
        if loaded is None:
            raise ApplyError(
                f"OneDrive account {account_id!r} has no stored token; "
                "run `dc auth add onedrive --force` to re-authorize."
            )
        data: dict[str, Any] = dict(loaded)
    else:
        data = dict(initial_data)
    refresh_token = str(data.get("refresh_token") or "")
    if not refresh_token:
        raise ApplyError(
            f"OneDrive account {account_id!r} has no refresh_token; "
            "run `dc auth add onedrive --force` to re-authorize."
        )
    client_id = str(data.get("client_id") or BUNDLED_ONEDRIVE_CLIENT_ID)
    scopes_raw = data.get("scopes")
    scopes: list[str] = (
        [str(s) for s in scopes_raw]
        if isinstance(scopes_raw, list)
        else list(ONEDRIVE_DEFAULT_SCOPES)
    )
    # ``offline_access`` is a synthetic scope Microsoft strips from the
    # access-token response; MSAL rejects it as a duplicate when passed
    # explicitly.  Filter it out for the refresh call.
    scopes = [s for s in scopes if s != "offline_access"]
    app_client = _msal_app_for(account_id, client_id)
    result = app_client.acquire_token_by_refresh_token(
        refresh_token, scopes=scopes
    )
    if not isinstance(result, dict) or "access_token" not in result:
        err = (
            result.get("error_description")
            if isinstance(result, dict)
            else "unknown"
        )
        raise ApplyError(
            f"OneDrive token refresh for {account_id!r} failed: {err}. "
            "Run `dc auth add onedrive --force` to re-authorize."
        )
    access_token = str(result["access_token"])
    # Persist the rotated refresh_token if Microsoft supplied one.  A
    # successful response almost always carries a fresh refresh_token —
    # but tolerate the (rare) case where it does not by leaving the old
    # value in place.
    #
    # Audit pass 11: only touch TokenStore when the refresh_token actually
    # rotated.  Re-writing the file on every apply run (a) generates disk
    # churn on the encrypted token blob and (b) makes it harder to spot
    # real rotations in ``mtime`` / audit tools.  A response that omits
    # the field or repeats the current value → no write.
    new_refresh = result.get("refresh_token")
    should_persist = (
        isinstance(new_refresh, str)
        and bool(new_refresh)
        and new_refresh != refresh_token
    )
    if should_persist:
        updated = dict(data)
        updated["access_token"] = access_token
        updated["refresh_token"] = new_refresh
        log = logging.getLogger(__name__)
        log.debug(
            "Persisted rotated refresh_token for %s (MSAL rotation).",
            account_id,
        )
        try:
            store.save(account_id, updated)
        except Exception as save_exc:
            # A save failure MUST NOT abort the current apply — the returned
            # access_token is still valid for this run.  Surface as a
            # warning so the user knows to re-auth before the token
            # rotates again.
            log = logging.getLogger(__name__)
            log.warning(
                "Failed to persist rotated OneDrive token for %s: %s.  Next "
                "apply may require `dc auth add onedrive --force`.",
                account_id,
                save_exc,
            )
    return access_token


def _build_onedrive_source(
    account_id: str, data: dict[str, object], *, force_refresh: bool
) -> object:
    """Instantiate an OneDriveSource with trash dispatch enabled.

    Microsoft Graph does not auto-refresh the way google-auth does — we
    plumb ``msal`` here to refresh the access token from the stored
    refresh_token whenever it expires (or unconditionally when
    ``force_refresh`` is set).  The source consumes the token via a
    zero-arg callable so a refresh landing after construction is picked up
    on the very next request.

    Audit pass 10 finding #2: the returned refresh_token (Microsoft rotates
    it on every acquire_token_by_refresh_token call) is now persisted back
    to disk via :func:`_refresh_onedrive_token`.  Finding #6: the underlying
    ``msal.PublicClientApplication`` is cached in ``_MSAL_APP_CACHE``.
    """
    from duplicate_cleaner.sources.onedrive import OneDriveSource

    # Cache the last known access_token so a source constructed without a
    # network hop still works for offline unit tests.  The provider
    # callable refreshes when the cache is stale (or unconditionally on
    # ``force_refresh``).
    cached_token: dict[str, str] = {
        "access_token": str(data.get("access_token") or ""),
    }

    if force_refresh or not cached_token["access_token"]:
        # Proactively refresh on --force-refresh, or when we never had one
        # (import-time bug — safer to fail fast).
        cached_token["access_token"] = _refresh_onedrive_token(
            account_id, initial_data=data
        )

    def _token_provider() -> str:
        # If the cached token is empty (e.g. someone reset the dict), fall
        # back to a fresh refresh.  Real 401s surface as SourceAuthError
        # from the source's HTTP layer and are handled by the mover's abort
        # path — retrying inside the provider would loop.
        tok = cached_token.get("access_token") or ""
        if not tok:
            tok = _refresh_onedrive_token(account_id, initial_data=data)
            cached_token["access_token"] = tok
        return tok

    return OneDriveSource(
        account_id=account_id,
        token_provider=_token_provider,
        is_read_only_scan=False,
    )


@app.command()
def undo(
    manifest: Annotated[
        Path, typer.Argument(help="Path to manifest.json from a previous apply.")
    ],
    force_refresh: Annotated[
        bool,
        typer.Option(
            "--force-refresh",
            help=(
                "Force a token refresh for every registered cloud account "
                "before dispatch.  Useful after a long idle since the last "
                "apply."
            ),
        ),
    ] = False,
) -> None:
    """Restore files from a previous apply run."""
    # v0.2 sub-phase 5d: build the sources map so ``restore_from_manifest``
    # can dispatch each cloud manifest row to the correct source.  Local-
    # only manifests continue to work with the map built (unused) — the
    # extra registry-load is cheap and surfaces stale account state early.
    registry = AccountsRegistry()
    sources_map: dict[str, Any] = {}
    try:
        sources_map = _build_sources_for_apply(
            registry, force_refresh=force_refresh
        )
    except ApplyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    try:
        result = restore_from_manifest(
            manifest,
            registry=registry,
            sources=sources_map or None,
        )
    except Exception as e:
        # UndoError from a mid-run auth/rate abort surfaces here.
        console.print(f"[red]Undo aborted[/red]: {e}")
        raise typer.Exit(1) from e
    restored_local = int(result.get("restored_local", result.get("restored", 0)))
    restored_cloud = int(result.get("restored_cloud", 0))
    restored_tree = int(result.get("restored_tree", 0))
    skipped_cloud = int(result.get("skipped_cloud", 0))
    total = int(result.get("total", 0))
    console.print(
        f"Restored {restored_local} local, {restored_cloud} cloud, "
        f"{restored_tree} project tree(s) "
        f"(of {total} manifest entries)."
    )
    if skipped_cloud:
        console.print(
            f"[yellow]Skipped {skipped_cloud} cloud file(s)[/yellow] "
            "(recycle bin empty or provider does not support restore — "
            "see errors below)."
        )
    if result["errors"]:
        console.print(f"[yellow]{len(result['errors'])} error(s):[/yellow]")
        for msg in result["errors"][:20]:
            console.print(f"  • {msg}")


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
                "[red]DuplicateCleaner is BYO-only for cloud OAuth[/red] "
                "(the public repo does not bundle a personal Google client "
                "ID to avoid shared-quota / shared-revocation risk). Register "
                "your own OAuth client at [cyan]https://console.cloud.google.com"
                "[/cyan] (Desktop app type; enable Drive API) and pass the "
                "downloaded JSON via [cyan]--client-secret path/to/oauth.json"
                "[/cyan]. See docs/cloud-oauth-setup.md."
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
                "[red]DuplicateCleaner is BYO-only for cloud OAuth[/red] "
                "(the public repo does not bundle a personal Microsoft client "
                "ID to avoid shared-quota / shared-revocation risk). Register "
                "your own App Registration at [cyan]https://portal.azure.com"
                "[/cyan] (Personal Microsoft accounts, Public client, "
                "Files.ReadWrite scope) and pass the JSON via "
                "[cyan]--client-secret path/to/msal.json[/cyan]. "
                "See docs/cloud-oauth-setup.md."
            )
            raise typer.Exit(1)
        return client_id, BUNDLED_ONEDRIVE_CLIENT_SECRET
    if type_ == "gphotos":
        client_id = BUNDLED_GPHOTOS_CLIENT_ID
        # v0.6: BYO-only refusal identical in shape to the gdrive branch.
        # Google Photos uses the same Cloud Console OAuth surface but a
        # DIFFERENT enabled API — the message tells the user to enable
        # "Google Photos Library API" instead of Drive.
        if client_id.endswith("_TO_REPLACE"):
            console.print(
                "[red]DuplicateCleaner is BYO-only for cloud OAuth[/red] "
                "(the public repo does not bundle a personal Google client "
                "ID to avoid shared-quota / shared-revocation risk). Register "
                "your own OAuth client at [cyan]https://console.cloud.google.com"
                "[/cyan] (Desktop app type; enable [bold]Google Photos "
                "Library API[/bold]) and pass the downloaded JSON via "
                "[cyan]--client-secret path/to/oauth.json[/cyan]. "
                "See docs/cloud-oauth-setup.md."
            )
            raise typer.Exit(1)
        return client_id, BUNDLED_GPHOTOS_CLIENT_SECRET
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


def _auth_add_icloud(
    *,
    label: str | None,
    library_path: Path | None,
    force: bool,
) -> None:
    """Register a local Photos.photoslibrary bundle as an ``icloud`` account.

    Verifies the bundle exists and appends an accounts.toml row with the
    resolved path.  No OAuth, no token file — every future ``dc scan
    --sources icloud:<label>`` reads the bundle directly via osxphotos.
    """
    registry = AccountsRegistry()
    existing_ids = [e.id for e in registry.load()]
    account_id = _default_account_id("icloud", label, existing_ids)
    resolved = (
        Path(library_path).expanduser()
        if library_path is not None
        else Path.home() / "Pictures" / "Photos Library.photoslibrary"
    )
    if not resolved.exists():
        console.print(
            f"[red]Refusing to register[/red]: Photos library not found at "
            f"{resolved}.  Pass [cyan]--library-path[/cyan] or ensure "
            "Photos.app has run at least once so the bundle exists."
        )
        raise typer.Exit(2)
    if account_id in existing_ids and not force:
        console.print(
            f"[red]Account {account_id!r} already exists.[/red] "
            f"Use [cyan]dc auth remove {account_id}[/cyan] first, "
            "or pass [cyan]--force[/cyan] to overwrite."
        )
        raise typer.Exit(2)
    if account_id in existing_ids:
        registry.remove(account_id)
    try:
        registry.add(
            AccountEntry(
                id=account_id,
                type="icloud",
                label=label or account_id.split(":", 1)[-1],
                # ``user`` reused to carry the library path so a later
                # ``dc scan`` can reconstruct the source without a
                # separate side-table.  Not a real user identity — this
                # source has no OAuth identity by design.
                user=str(resolved),
                # Instance-relative call so tests that monkeypatch
                # ``duplicate_cleaner.cli.AccountsRegistry`` to a factory
                # (lambda) still reach the real staticmethod through the
                # constructed instance.
                added_ts=registry.now_ts(),
            )
        )
    except DuplicateAccountError as e:
        console.print(f"[yellow]Warning[/yellow]: {e}")
    console.print(
        f"[green]Registered[/green] {account_id} at {resolved} "
        "(no OAuth; iCloud Photos is read-only)."
    )


@auth_app.command("add")
def auth_add(
    type_: Annotated[
        str,
        typer.Argument(
            metavar="TYPE",
            help="Cloud provider type — 'gdrive', 'onedrive', 'gphotos', or 'icloud'.",
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
    library_path: Annotated[
        Path | None,
        typer.Option(
            "--library-path",
            help=(
                "iCloud only.  Path to the Photos Library.photoslibrary "
                "bundle (default: ~/Pictures/Photos Library.photoslibrary)."
            ),
        ),
    ] = None,
) -> None:
    """Register a cloud account by running the OAuth localhost flow."""
    if type_ == "icloud":
        _auth_add_icloud(label=label, library_path=library_path, force=force)
        return
    if type_ not in {"gdrive", "onedrive", "gphotos"}:
        console.print(
            f"[red]Unsupported auth type[/red]: {type_}. "
            "Supported: 'gdrive', 'onedrive', 'gphotos', 'icloud'."
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
    elif type_ == "gphotos":
        if full:
            # v0.6 deliberately locks the read-only scope; the full scope
            # (photoslibrary) requires a v0.6.1 escalation.  Surface the
            # request loudly so users can find the v0.6.1 upgrade path.
            console.print(
                "[yellow]Warning[/yellow]: --full for gphotos is deferred "
                "to v0.6.1; using photoslibrary.readonly for v0.6."
            )
        auth_url_base = GPHOTOS_AUTH_URL
        token_url = GPHOTOS_TOKEN_URL
        scopes = list(GPHOTOS_DEFAULT_SCOPES)
        extra_auth_params = {
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
    # v0.6: icloud has no token file; treat it separately.
    registry_for_test = AccountsRegistry()
    entry = registry_for_test.get(account_id)
    if entry is not None and entry.type == "icloud":
        from duplicate_cleaner.sources.iclouddrive_photos import iCloudPhotosSource

        try:
            src_ic = iCloudPhotosSource(
                account_id=account_id,
                photos_library_path=Path(entry.user) if entry.user else None,
            )
            it_ic = src_ic.list_files()
            first_ic = next(iter(it_ic), None)
            console.print(
                f"[green]OK[/green]: {account_id} — "
                + (
                    f"first photo: {first_ic.path}"
                    if first_ic is not None
                    else "empty library"
                )
            )
        except Exception as e:
            console.print(f"[red]{account_id} check failed[/red]: {e}")
            raise typer.Exit(1) from e
        return
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
    elif kind == "gphotos":
        try:
            creds = _google_credentials_from_token(
                data, default_scopes=list(GPHOTOS_DEFAULT_SCOPES)
            )
            from duplicate_cleaner.sources.gphotos import GooglePhotosSource

            src_gp = GooglePhotosSource(
                account_id=account_id,
                credentials=creds,
                client_config={"user_email": str(data.get("user_email") or "")},
            )
            it_gp = src_gp.list_files()
            first_gp = next(iter(it_gp), None)
            console.print(
                f"[green]OK[/green]: {account_id} — "
                + (
                    f"first photo: {first_gp.path}"
                    if first_gp is not None
                    else "empty photo library"
                )
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
        if refresh and (kind == "gdrive" or kind == "gphotos"):
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


# --------------------------------------------------------------------------- #
# v0.3 sub-milestone 5.3-a — organizer discovery.                             #
#                                                                             #
# Adds `dc organize discover` only.  Apply + undo land in 5.3-b; review TUI   #
# in 5.3-e.  Discovery is READ-ONLY — no filesystem writes to user            #
# directories, only the report artifacts and (optionally) the signal cache   #
# under ``~/.cache/duplicate_cleaner/``.                                      #
# --------------------------------------------------------------------------- #

organize_app = typer.Typer(help="Organize files into a domain-based taxonomy (v0.3).")
app.add_typer(organize_app, name="organize")


@organize_app.command("discover")
def organize_discover(
    roots: Annotated[
        list[Path], typer.Argument(help="One or more directories to discover.")
    ],
    report_dir: Annotated[
        Path,
        typer.Option(
            "--report",
            help="Output directory for organize-plan.html and organize-plan.json.",
        ),
    ],
    dest: Annotated[
        Path | None,
        typer.Option(
            "--dest",
            help="Destination root for the eventual apply step (annotated only).",
        ),
    ] = None,
    min_size: Annotated[
        int | None,
        typer.Option("--min-size", help="Override min_size_bytes from config."),
    ] = None,
    follow_symlinks: Annotated[
        bool,
        typer.Option("--follow-symlinks", help="Follow symlinks during walk."),
    ] = False,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Extra exclude glob (repeatable)."),
    ] = None,
    confidence_threshold: Annotated[
        float | None,
        typer.Option(
            "--confidence-threshold",
            help="Files below this classifier score go to Unsorted/.",
        ),
    ] = None,
    event_gap_hours: Annotated[
        int | None,
        typer.Option(
            "--event-gap-hours",
            help="Hours between successive photos that split an event.",
        ),
    ] = None,
    skip_dedup_check: Annotated[
        bool,
        typer.Option(
            "--skip-dedup-check",
            help="Suppress the pending-dedup-groups soft warning.",
        ),
    ] = False,
) -> None:
    """Read-only pass: walks roots, extracts signals, writes an organize plan."""
    from duplicate_cleaner.organize.discover import discover as run_discover
    from duplicate_cleaner.organize.render import render_plan

    cfg = load_config()
    if not cfg.active_homes:
        console.print(
            "[red]Refusing to run[/red]: no [cyan]active_homes[/cyan] declared "
            f"in {CONFIG_PATH}. Edit the file and set "
            "active_homes = ['/Users/you'] before running organize discover."
        )
        raise typer.Exit(2)

    for r in roots:
        try:
            validate_scan_root_candidate(r)
        except ValueError as e:
            console.print(f"[red]Refusing to run[/red]: {e}")
            raise typer.Exit(2) from e

    updates: dict[str, Any] = {}
    if min_size is not None:
        updates["min_size_bytes"] = min_size
    if follow_symlinks:
        updates["follow_symlinks"] = True
    if exclude:
        updates["exclude_globs"] = [*cfg.exclude_globs, *exclude]
    if confidence_threshold is not None:
        updates["organize_confidence_threshold"] = confidence_threshold
    if event_gap_hours is not None:
        updates["event_gap_hours"] = event_gap_hours
    if updates:
        cfg = cfg.model_copy(update=updates)

    _check_pending_dedup(cfg, skip_dedup_check)

    store = Store()
    try:
        plan, summary = run_discover(
            roots,
            cfg,
            store=store,
            dest_root=dest,
        )
    finally:
        store.close()

    html_path, json_path = render_plan(plan, report_dir)
    console.print(
        f"[green]Discovered[/green] {summary.files_scanned} files across "
        f"{summary.cohesion_groups} cohesion group(s); proposed taxonomy has "
        f"{len(summary.domains)} domain(s)."
    )
    console.print(f"Plan JSON: {json_path}")
    console.print(f"Plan HTML: {html_path}")


def _check_pending_dedup(cfg: Any, skip: bool) -> None:
    """Emit a soft warning (or hard refuse) if a dedup scan has pending groups.

    Non-fatal by default: organize before dedup is legitimate on a fresh
    external drive.  Users opt in to hard refusal via
    ``enforce_dedup_ordering = true`` in config.toml.
    """
    if skip:
        return
    try:
        store = Store()
    except OSError:
        return
    try:
        cur = store._conn.execute(
            "SELECT COUNT(*) AS n FROM group_members "
            "WHERE is_proposed_keeper = 0 AND is_informational = 0"
        )
        row = cur.fetchone()
    except Exception:
        row = None
    finally:
        store.close()
    if row is None:
        return
    pending = int(row["n"] or 0)
    if pending <= 0:
        return
    if getattr(cfg, "enforce_dedup_ordering", False):
        console.print(
            f"[red]Refusing to run[/red]: {pending} pending dedup discard(s) "
            "detected.  Run `dc apply <report.json> --commit` first, or set "
            "`enforce_dedup_ordering = false` in config.toml."
        )
        raise typer.Exit(2)
    console.print(
        f"[yellow]Warning[/yellow]: {pending} pending dedup discard(s) "
        "detected.  Organize runs best AFTER dedup — consider running "
        "`dc apply` first.  Pass `--skip-dedup-check` to suppress this warning."
    )


# --------------------------------------------------------------------------- #
# v0.3 sub-milestone 5.3-b — organizer apply + undo.                          #
# --------------------------------------------------------------------------- #


@organize_app.command("apply")
def organize_apply(
    plan_path: Annotated[
        Path, typer.Argument(help="Path to the organize-plan.json to apply.")
    ],
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Actually move files. Without this flag, apply is a dry-run.",
        ),
    ] = False,
    split_cohesive_units: Annotated[
        bool,
        typer.Option(
            "--split-cohesive-units",
            help="Allow a cohesion group's members to move to different folders.",
        ),
    ] = False,
    runs_dir: Annotated[
        Path | None,
        typer.Option(
            "--runs-dir",
            help="Where to write the undo manifest. "
            "Default ~/.local/share/duplicate_cleaner/organize-runs/.",
        ),
    ] = None,
) -> None:
    """Move files per the plan — dry-run unless --commit is passed."""
    from duplicate_cleaner.organize.mover import (
        OrganizeApplyError,
        OrganizeDriftError,
        apply_plan,
    )

    cfg = load_config()
    try:
        result = apply_plan(
            plan_path,
            commit=commit,
            split_cohesive_units=split_cohesive_units,
            config=cfg,
            runs_dir=runs_dir,
        )
    except OrganizeDriftError as e:
        console.print(f"[red]Drift detected[/red]: {e}")
        raise typer.Exit(2) from e
    except OrganizeApplyError as e:
        console.print(f"[red]Refusing to apply[/red]: {e}")
        raise typer.Exit(2) from e

    for w in result.warnings:
        console.print(f"[yellow]Warning[/yellow]: {w}")

    if not commit:
        console.print(
            f"[cyan]Dry-run[/cyan]: {result.planned} planned, "
            f"{result.verified} verified, {len(result.errors)} drift error(s). "
            "Pass --commit to move."
        )
        for err in result.errors:
            console.print(f"  [yellow]drift[/yellow]: {err}")
        return

    dest = result.dest_root
    console.print(
        f"[green]Moved[/green] {result.moved} file(s) to {dest}. "
        f"Manifest: {result.manifest_path}."
    )
    if result.collisions:
        console.print(
            f"[yellow]{len(result.collisions)} collision(s) renamed with "
            "_<hash8> suffix.[/yellow]"
        )
    if result.errors:
        console.print(
            f"[yellow]{len(result.errors)} error(s) — see manifest for detail.[/yellow]"
        )


@organize_app.command("undo")
def organize_undo(
    manifest_path: Annotated[
        Path,
        typer.Argument(help="Path to the organize-run manifest.json to reverse."),
    ],
) -> None:
    """Reverse every move recorded in an organize-run manifest."""
    from duplicate_cleaner.organize.undo import (
        OrganizeUndoError,
        restore_from_organize_manifest,
    )

    try:
        result = restore_from_organize_manifest(manifest_path)
    except OrganizeUndoError as e:
        console.print(f"[red]Refusing to restore[/red]: {e}")
        raise typer.Exit(2) from e

    console.print(
        f"[green]Restored[/green] {result.restored} of {result.total} file(s) "
        f"from manifest {manifest_path}."
    )
    for err in result.errors:
        console.print(f"  [yellow]skip[/yellow]: {err}")


# --------------------------------------------------------------------------- #
# v0.5-a — `dc migrate plan` (cloud-to-cloud consolidation, planner half).    #
#                                                                             #
# copy / verify / cleanup / undo land in v0.5-b.  The planner here is         #
# read-only: it enumerates the source, consults the destination for skip     #
# decisions, and writes a plan JSON + HTML.  BYO OAuth remains the only     #
# supported auth path.                                                        #
# --------------------------------------------------------------------------- #

migrate_app = typer.Typer(help="Cloud-to-cloud file consolidation.")
app.add_typer(migrate_app, name="migrate")


@migrate_app.command("plan")
def migrate_plan(
    from_: Annotated[
        str,
        typer.Option(
            "--from",
            help="Source account_id (e.g. 'gdrive:personal').",
        ),
    ],
    to: Annotated[
        str,
        typer.Option(
            "--to",
            help="Destination account_id (e.g. 'onedrive:main').",
        ),
    ],
    report: Annotated[
        Path,
        typer.Option(
            "--report",
            help="Output directory for migration-plan.html + migration-plan.json.",
        ),
    ],
    filter_glob: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            help=(
                "Include-only glob (repeatable). Example: --filter '**/*.pdf'."
            ),
        ),
    ] = None,
    exclude_glob: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Exclude glob (repeatable).",
        ),
    ] = None,
    include_shared: Annotated[
        bool,
        typer.Option(
            "--include-shared",
            help=(
                "v0.5-a: NOT YET SUPPORTED — shared cloud files remain "
                "informational-only.  Flag reserved for a future opt-in."
            ),
        ),
    ] = False,
    dest_size_limit_gb: Annotated[
        float | None,
        typer.Option(
            "--dest-size-limit-gb",
            help=(
                "Override destination per-file cap (GB). Default: provider "
                "limit (Google Drive 5 TB, OneDrive Personal 250 GB)."
            ),
        ),
    ] = None,
) -> None:
    """Read-only pass: enumerate --from, plan copies to --to, write a plan."""
    from duplicate_cleaner.migrate import (
        MigrationFilter,
        plan_migration,
        render_migration_plan,
    )

    if include_shared:
        console.print(
            "[yellow]Warning[/yellow]: --include-shared is not yet supported "
            "in v0.5-a; shared cloud files will still be deferred."
        )

    # Build source + dest sources using the same read-only construction
    # pattern as `dc scan`.  The destination is consulted read-only at
    # planning time; v0.5-b will construct it with is_read_only_scan=False
    # for the actual copy step.
    try:
        built = _build_scan_sources([from_, to])
    except typer.BadParameter as e:
        console.print(f"[red]Refusing to plan[/red]: {e}")
        raise typer.Exit(2) from e
    if len(built) != 2:
        console.print(
            f"[red]Refusing to plan[/red]: could not construct source + "
            f"destination (from={from_!r}, to={to!r})."
        )
        raise typer.Exit(2)
    src_source, dst_source = built[0], built[1]

    filt = MigrationFilter(
        include_globs=list(filter_glob or []),
        exclude_globs=list(exclude_glob or []),
        exclude_shared=not include_shared,
    )
    plan = plan_migration(
        src_source,
        dst_source,
        filter_=filt,
        dest_size_limit_gb=dest_size_limit_gb,
    )
    html_path, json_path = render_migration_plan(plan, report)
    counts = plan.counts_by_action
    tbl = Table(title=f"Migration plan: {from_} -> {to}")
    tbl.add_column("Action")
    tbl.add_column("Count", justify="right")
    for name in ("copy", "skip", "defer", "error"):
        tbl.add_row(name, str(counts.get(name, 0)))
    tbl.add_row("Plan JSON", str(json_path))
    tbl.add_row("Plan HTML", str(html_path))
    console.print(tbl)


# --------------------------------------------------------------------------- #
# v0.5-b — `dc migrate copy | verify | cleanup | undo`.                       #
# --------------------------------------------------------------------------- #

MIGRATE_RUNS_DIR = (
    Path.home() / ".local" / "share" / "duplicate_cleaner" / "migrate-runs"
)


def _build_migrate_sources(
    source_ids: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Construct the write-enabled ``source_id -> Source`` map for migrate.

    Mirrors :func:`_build_sources_for_apply` but keyed on an explicit id
    list (from the plan/manifest's ``source_id`` + ``dest_id``) so we don't
    build sources for accounts the current run does not touch.  Every
    source is constructed with ``is_read_only_scan=False`` — the same
    tripwire the migrate mover / cleanup / undo modules re-check as
    defense-in-depth.
    """
    if not source_ids:
        return {}
    tokens = TokenStore()
    registry = AccountsRegistry()
    by_id: dict[str, AccountEntry] = {e.id: e for e in registry.load()}
    out: dict[str, Any] = {}
    for sid in source_ids:
        entry = by_id.get(sid)
        if entry is None:
            raise typer.BadParameter(
                f"source {sid!r} not registered — run "
                f"`dc auth add {sid.split(':', 1)[0]} <label>`"
            )
        # v0.6: icloud + gphotos are read-only sources; migrate can enumerate
        # them as source but never as destination.  The mover / cleanup
        # tripwires refuse a read-only source with a typed error, so we
        # construct them with the read-only flag on.
        if entry.type == "icloud":
            from duplicate_cleaner.sources.iclouddrive_photos import (
                iCloudPhotosSource,
            )

            out[sid] = iCloudPhotosSource(
                account_id=sid,
                photos_library_path=Path(entry.user) if entry.user else None,
                is_read_only_scan=True,
            )
            continue
        data = tokens.load(sid)
        if data is None:
            raise typer.BadParameter(
                f"source {sid!r} has no stored token — re-run "
                "`dc auth add --force` for the account"
            )
        kind = str(data.get("type", entry.type))
        if kind == "gdrive":
            out[sid] = _build_gdrive_source(
                sid, data, force_refresh=force_refresh
            )
        elif kind == "gphotos":
            out[sid] = _build_gphotos_source(
                sid, data, force_refresh=force_refresh
            )
        elif kind == "onedrive":
            out[sid] = _build_onedrive_source(
                sid, data, force_refresh=force_refresh
            )
        else:
            raise typer.BadParameter(f"Unknown source type: {kind!r}")
    return out


def _default_migrate_manifest_path() -> Path:
    """Return a ``migrate-runs/<utc-timestamp>/manifest.json`` under HOME."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return MIGRATE_RUNS_DIR / ts / "manifest.json"


@migrate_app.command("copy")
def migrate_copy(
    plan_path: Annotated[
        Path,
        typer.Argument(help="Path to migration-plan.json from `dc migrate plan`."),
    ],
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Actually upload copies. Dry-run without this flag.",
        ),
    ] = False,
    max_bandwidth_mbps: Annotated[
        float | None,
        typer.Option(
            "--max-bandwidth-mbps",
            help="Cap effective upload bandwidth (Mbps). Default unlimited.",
        ),
    ] = None,
    resume_from: Annotated[
        Path | None,
        typer.Option(
            "--resume-from",
            help=(
                "Path to a prior migration manifest.  Entries with "
                "state='done' are marked skipped in this run so a resume "
                "does not re-upload."
            ),
        ),
    ] = None,
    out_manifest: Annotated[
        Path | None,
        typer.Option(
            "--out-manifest",
            help=(
                "Where to write the migration manifest.  Default "
                "~/.local/share/duplicate_cleaner/migrate-runs/<utc>/manifest.json."
            ),
        ),
    ] = None,
) -> None:
    """Execute the copy actions in the plan — dry-run unless --commit is passed."""
    from duplicate_cleaner.migrate.mover import (
        MigrationError as _MigrationError,
    )
    from duplicate_cleaner.migrate.mover import (
        execute_migration,
    )
    from duplicate_cleaner.migrate.plan import MigrationPlan

    plan = MigrationPlan.model_validate_json(plan_path.read_text())
    manifest_path = out_manifest or _default_migrate_manifest_path()
    try:
        sources_map = _build_migrate_sources([plan.source_id, plan.dest_id])
    except typer.BadParameter as e:
        console.print(f"[red]Refusing to copy[/red]: {e}")
        raise typer.Exit(2) from e
    try:
        result = execute_migration(
            plan_path,
            manifest_path,
            commit=commit,
            sources_by_id=sources_map,
            resume_from=resume_from,
            max_bandwidth_mbps=max_bandwidth_mbps,
        )
    except _MigrationError as e:
        console.print(f"[red]Migration aborted[/red]: {e}")
        raise typer.Exit(1) from e

    tbl = Table(title="Migration copy result")
    tbl.add_column("Metric")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Planned entries", str(result.planned))
    tbl.add_row("Copied", str(result.copied))
    tbl.add_row("Skipped", str(result.skipped))
    tbl.add_row("Deferred", str(result.deferred))
    tbl.add_row("Errored", str(result.errored))
    tbl.add_row("Committed", "yes" if result.committed else "no (dry-run)")
    if result.manifest_path:
        tbl.add_row("Manifest", str(result.manifest_path))
    console.print(tbl)
    if result.errors:
        console.print(
            f"[yellow]{len(result.errors)} error(s):[/yellow]"
        )
        for msg in result.errors[:20]:
            console.print(f"  • {msg}")
    if not commit:
        console.print(
            "[cyan]DRY-RUN[/cyan]: pass [bold]--commit[/bold] to upload."
        )


@migrate_app.command("verify")
def migrate_verify(
    manifest_path: Annotated[
        Path,
        typer.Argument(help="Path to migration manifest.json to verify."),
    ],
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help=(
                "Stream destination bytes through BLAKE3 for a strict "
                "byte-level check. Slower but catches provider-side bit-flips."
            ),
        ),
    ] = False,
) -> None:
    """Re-check every done manifest entry against destination-side metadata."""
    from duplicate_cleaner.migrate.mover import _load_manifest
    from duplicate_cleaner.migrate.verify import (
        VerifyError as _VerifyError,
    )
    from duplicate_cleaner.migrate.verify import (
        verify_migration,
    )

    manifest = _load_manifest(manifest_path)
    try:
        sources_map = _build_migrate_sources(
            [manifest.plan_source_id, manifest.plan_dest_id]
        )
    except typer.BadParameter as e:
        console.print(f"[red]Refusing to verify[/red]: {e}")
        raise typer.Exit(2) from e
    try:
        result = verify_migration(
            manifest_path,
            sources_by_id=sources_map,
            full=full,
        )
    except _VerifyError as e:
        console.print(f"[red]Verify aborted[/red]: {e}")
        raise typer.Exit(1) from e
    tbl = Table(title="Migration verify result")
    tbl.add_column("Metric")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Done entries", str(result.total_done))
    tbl.add_row("Verified", str(result.verified))
    tbl.add_row("Drifted", str(result.drifted))
    tbl.add_row("Missing on dest", str(result.missing))
    tbl.add_row("Errored", str(result.errored))
    console.print(tbl)
    if result.errors:
        console.print(f"[yellow]{len(result.errors)} error(s):[/yellow]")
        for msg in result.errors[:20]:
            console.print(f"  • {msg}")


@migrate_app.command("cleanup")
def migrate_cleanup(
    manifest_path: Annotated[
        Path,
        typer.Argument(help="Path to migration manifest.json to clean up."),
    ],
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Actually trash source originals. Dry-run without this flag.",
        ),
    ] = False,
) -> None:
    """Trash source originals for verified done entries. Dry-run by default."""
    from duplicate_cleaner.migrate.cleanup import (
        CleanupError as _CleanupError,
    )
    from duplicate_cleaner.migrate.cleanup import (
        cleanup_source_after_migration,
    )
    from duplicate_cleaner.migrate.mover import _load_manifest

    manifest = _load_manifest(manifest_path)
    try:
        sources_map = _build_migrate_sources([manifest.plan_source_id])
    except typer.BadParameter as e:
        console.print(f"[red]Refusing to clean up[/red]: {e}")
        raise typer.Exit(2) from e
    try:
        result = cleanup_source_after_migration(
            manifest_path,
            commit=commit,
            sources_by_id=sources_map,
        )
    except _CleanupError as e:
        console.print(f"[red]Refusing to clean up[/red]: {e}")
        raise typer.Exit(2) from e
    tbl = Table(title="Migration cleanup result")
    tbl.add_column("Metric")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Planned trash", str(result.planned))
    tbl.add_row("Trashed", str(result.trashed))
    tbl.add_row("Skipped", str(result.skipped))
    tbl.add_row("Committed", "yes" if result.committed else "no (dry-run)")
    console.print(tbl)
    if result.errors:
        console.print(f"[yellow]{len(result.errors)} error(s):[/yellow]")
        for msg in result.errors[:20]:
            console.print(f"  • {msg}")


@migrate_app.command("undo")
def migrate_undo(
    manifest_path: Annotated[
        Path,
        typer.Argument(help="Path to migration manifest.json to reverse."),
    ],
) -> None:
    """Restore source originals and trash destination copies from a migration run."""
    from duplicate_cleaner.migrate.mover import _load_manifest
    from duplicate_cleaner.migrate.undo import (
        UndoMigrationError as _UndoMigrationError,
    )
    from duplicate_cleaner.migrate.undo import (
        undo_migration,
    )

    manifest = _load_manifest(manifest_path)
    try:
        sources_map = _build_migrate_sources(
            [manifest.plan_source_id, manifest.plan_dest_id]
        )
    except typer.BadParameter as e:
        console.print(f"[red]Refusing to undo[/red]: {e}")
        raise typer.Exit(2) from e
    try:
        result = undo_migration(manifest_path, sources_by_id=sources_map)
    except _UndoMigrationError as e:
        console.print(f"[red]Undo aborted[/red]: {e}")
        raise typer.Exit(1) from e
    tbl = Table(title="Migration undo result")
    tbl.add_column("Metric")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Manifest entries", str(result.total))
    tbl.add_row("Source originals restored", str(result.restored_source))
    tbl.add_row("Dest copies trashed", str(result.trashed_dest))
    console.print(tbl)
    if result.errors:
        console.print(f"[yellow]{len(result.errors)} error(s):[/yellow]")
        for msg in result.errors[:20]:
            console.print(f"  • {msg}")


def _google_credentials_from_token(
    data: dict[str, object],
    *,
    default_scopes: list[str] | None = None,
) -> object:
    """Build a google.oauth2 Credentials object from a stored token blob."""
    from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]

    raw_scopes = data.get("scopes")
    fallback = default_scopes if default_scopes is not None else list(GDRIVE_DEFAULT_SCOPES)
    scopes: list[str] = (
        [str(s) for s in raw_scopes]
        if isinstance(raw_scopes, list)
        else fallback
    )
    return Credentials(
        token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or "") or None,
        token_uri=GDRIVE_TOKEN_URL,
        client_id=str(data.get("client_id") or ""),
        client_secret=str(data.get("client_secret") or ""),
        scopes=scopes,
    )
