"""Render a :class:`MigrationPlan` to HTML + JSON on disk."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from duplicate_cleaner.migrate.plan import MigrationPlan


def _human_bytes(n: int | float) -> str:
    x = float(n)
    if x < 1024:
        return f"{int(x)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        x /= 1024.0
        if x < 1024:
            return f"{x:.1f} {unit}"
    return f"{x:.1f} PB"


def render_migration_plan(
    plan: MigrationPlan, out_dir: Path
) -> tuple[Path, Path]:
    """Write ``migration-plan.html`` and ``migration-plan.json`` under ``out_dir``.

    Returns ``(html_path, json_path)`` so the CLI can print both.  The
    JSON round-trips through :class:`MigrationPlan` unchanged so v0.5-b's
    ``dc migrate copy`` can consume it with a plain ``model_validate_json``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    templates_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "htm", "j2"]),
    )
    env.filters["human_bytes"] = _human_bytes
    tmpl = env.get_template("migration-plan.html.j2")
    html = tmpl.render(plan=plan)

    html_path = out_dir / "migration-plan.html"
    json_path = out_dir / "migration-plan.json"
    html_path.write_text(html)
    json_path.write_text(plan.model_dump_json(indent=2))
    return html_path, json_path
