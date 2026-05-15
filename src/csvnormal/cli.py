"""CSVNormal CLI — entry point for all commands."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from csvnormal import __version__
from csvnormal import logger as log
from csvnormal.config import CANONICAL_FIELDS, NUMERIC_FIELDS, REQUIRED_FIELDS, settings

app = typer.Typer(
    name="csvnormal",
    help="Local-first AI-assisted CSV normalization for performance marketing data.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        log.console.print(f"csvnormal [bold cyan]v{__version__}[/bold cyan]")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


# ── inspect ───────────────────────────────────────────────────────────────────


@app.command()
def inspect(
    file: Path = typer.Argument(..., help="CSV file to inspect."),
) -> None:
    """Detect tables, preview structure, show warnings. [Phase 2+]"""
    log.rule("inspect")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)
    log.info(f"Target file: [bold]{file}[/bold]")
    log.warning("inspect command will be implemented in Phase 2 — CSV Ingestion.")
    log.blank()


# ── map ───────────────────────────────────────────────────────────────────────


@app.command(name="map")
def map_cmd(
    file: Path = typer.Argument(..., help="CSV file to map."),
) -> None:
    """Send headers + sample rows to AI, receive column mapping JSON. [Phase 4+]"""
    log.rule("map")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)
    log.info(f"Target file: [bold]{file}[/bold]")
    log.warning("map command will be implemented in Phase 4 — AI Mapping Layer.")
    log.blank()


# ── normalize ─────────────────────────────────────────────────────────────────


@app.command()
def normalize(
    file: Path = typer.Argument(..., help="CSV file to normalize."),
    mapping: Optional[Path] = typer.Option(None, "--mapping", "-m", help="Mapping JSON file."),
) -> None:
    """Apply deterministic mappings and produce canonical CSV. [Phase 5+]"""
    log.rule("normalize")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)
    log.info(f"Target file: [bold]{file}[/bold]")
    log.warning("normalize command will be implemented in Phase 5 — Deterministic Normalization.")
    log.blank()


# ── validate ──────────────────────────────────────────────────────────────────


@app.command()
def validate(
    file: Path = typer.Argument(..., help="Normalized CSV to validate."),
) -> None:
    """Verify row counts, numeric integrity, required fields. [Phase 6+]"""
    log.rule("validate")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)
    log.info(f"Target file: [bold]{file}[/bold]")
    log.warning("validate command will be implemented in Phase 6 — Validation Engine.")
    log.blank()


# ── review ────────────────────────────────────────────────────────────────────


@app.command()
def review(
    mapping: Path = typer.Argument(..., help="Mapping JSON file to review."),
) -> None:
    """Terminal-based manual mapping review and override workflow. [Phase 7+]"""
    log.rule("review")
    if not mapping.exists():
        log.error(f"File not found: {mapping}")
        raise typer.Exit(1)
    log.info(f"Mapping file: [bold]{mapping}[/bold]")
    log.warning("review command will be implemented in Phase 7 — Terminal Review Workflow.")
    log.blank()


# ── schema ────────────────────────────────────────────────────────────────────


@app.command()
def schema() -> None:
    """Show the canonical output schema."""
    log.rule("Canonical Schema")
    log.blank()

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Field", style="bold white", width=16)
    table.add_column("Required", width=10)
    table.add_column("Type", width=12)
    table.add_column("Notes")

    for field in CANONICAL_FIELDS:
        required = "YES" if field in REQUIRED_FIELDS else "—"
        req_style = "bold green" if field in REQUIRED_FIELDS else "dim"
        is_numeric = "numeric" if field in NUMERIC_FIELDS else "text"
        notes = {
            "client": "Client name — set per file",
            "platform": "Normalized platform key (meta, google, tiktok…)",
            "campaign": "Campaign name from source",
            "date": "ISO 8601 preferred (YYYY-MM-DD)",
            "spend": "NEVER modified — exact source value",
            "impressions": "NEVER modified — exact source value",
            "clicks": "NEVER modified — exact source value",
            "conversions": "NEVER modified — exact source value",
            "currency": "3-letter ISO code (USD, EUR…)",
        }.get(field, "")
        table.add_row(field, f"[{req_style}]{required}[/{req_style}]", is_numeric, notes)

    log.console.print(table)
    log.blank()
    log.info(f"Model: [bold]{settings.model}[/bold]")
    log.info(f"Output dir: [bold]{settings.output_dir}[/bold]")
    log.blank()
