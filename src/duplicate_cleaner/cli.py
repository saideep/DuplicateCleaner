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
from duplicate_cleaner.compare.exact import group_by_hash
from duplicate_cleaner.config import (
    CONFIG_PATH,
    WEIGHTS_PATH,
    load_config,
    load_weights,
    write_default_config,
    write_default_weights,
)
from duplicate_cleaner.hash.pipeline import hash_records
from duplicate_cleaner.paths import validate_scan_root_candidate
from duplicate_cleaner.report.render import render_report
from duplicate_cleaner.report.schema import (
    Report,
    ReportGroup,
    ReportMember,
    ReportSignal,
)
from duplicate_cleaner.scan.walk import FileRecord, iter_files
from duplicate_cleaner.score.rules import score_groups
from duplicate_cleaner.store import Store

app = typer.Typer(help="Intelligent duplicate detection and cleanup.")
weights_app = typer.Typer(help="Inspect or reset scoring weights.")
cache_app = typer.Typer(help="Inspect or clear the local hash cache.")
app.add_typer(weights_app, name="weights")
app.add_typer(cache_app, name="cache")

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
) -> None:
    """Walk, hash, group, score, and write a report."""
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

    weights = load_weights()
    store = Store()

    file_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        walk_task = progress.add_task("Walking & hashing…", total=None)

        def _walk_iter() -> Iterator[FileRecord]:
            nonlocal file_count
            for rec in iter_files(
                roots,
                follow_symlinks=cfg.follow_symlinks,
                exclude_globs=cfg.exclude_globs,
                min_size_bytes=cfg.min_size_bytes,
            ):
                file_count += 1
                if file_count % 500 == 0:
                    progress.update(
                        walk_task, description=f"Walked {file_count} files…"
                    )
                yield rec

        hashed_iter = hash_records(_walk_iter(), store)
        groups = list(group_by_hash(hashed_iter, min_size_bytes=cfg.min_size_bytes))
        progress.update(
            walk_task,
            description=(
                f"Walked {file_count} files; found {len(groups)} groups."
            ),
        )

    scored = score_groups(groups, cfg, weights)

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
                is_proposed_keeper=m.is_proposed_keeper,
                is_informational=m.is_informational,
            )
            for m in sg.members
        ]
        store.record_group(
            "exact",
            [
                (m.path, m.score, m.is_proposed_keeper, m.is_informational)
                for m in sg.members
            ],
        )
        report_groups.append(
            ReportGroup(
                id=sg.id,
                hash=sg.hash,
                size=sg.size,
                reclaim_bytes=sg.reclaim_bytes,
                members=members,
            )
        )
        total_reclaim += sg.reclaim_bytes

    report = Report(
        roots=[Path(r).expanduser().resolve() for r in roots],
        total_files_scanned=file_count,
        total_groups=len(report_groups),
        total_reclaim_bytes=total_reclaim,
        groups=report_groups,
    )
    html_path, json_path = render_report(report, report_dir)
    store.close()

    tbl = Table(title="Scan complete")
    tbl.add_column("Metric")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Files scanned", str(file_count))
    tbl.add_row("Duplicate groups", str(len(report_groups)))
    tbl.add_row("Reclaimable (bytes)", str(total_reclaim))
    tbl.add_row("HTML report", str(html_path))
    tbl.add_row("JSON report", str(json_path))
    console.print(tbl)
    console.print(
        "[yellow]Note[/yellow]: APFS clone detection is not implemented in v0.1 "
        "(scheduled for v0.1.1). Reclaim estimates on cloned trees may be too "
        "high — see docs/safety.md."
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
