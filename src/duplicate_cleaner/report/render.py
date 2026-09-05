"""Render Report objects to self-contained HTML + editable JSON."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from duplicate_cleaner.report.schema import Report


def _human_bytes(n: int | float) -> str:
    x = float(n)
    if x < 1024:
        return f"{int(x)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        x /= 1024.0
        if x < 1024:
            return f"{x:.1f} {unit}"
    return f"{x:.1f} PB"


def _signed(n: float) -> str:
    return f"{n:+.2f}"


def render_report(report: Report, out_dir: Path) -> tuple[Path, Path]:
    """Write report.html and report.json under out_dir; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)

    templates_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "htm", "j2"]),
    )
    env.filters["human_bytes"] = _human_bytes
    env.filters["signed"] = _signed
    tmpl = env.get_template("report.html.j2")
    html = tmpl.render(report=report)

    html_path = out_dir / "report.html"
    json_path = out_dir / "report.json"
    html_path.write_text(html)
    json_path.write_text(report.model_dump_json(indent=2))
    return html_path, json_path
