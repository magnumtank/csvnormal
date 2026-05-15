"""CSVNormal CLI — entry point for all commands."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from csvnormal import __version__
from csvnormal import logger as log
from csvnormal.config import (
    ALL_PRESERVE_FIELDS,
    CANONICAL_FIELDS,
    DEFAULT_CURRENCY,
    FIELD_GROUPS,
    FIELD_NOTES,
    NUMERIC_FIELDS,
    RATE_FIELDS,
    REQUIRED_FIELDS,
    settings,
)

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
    """Show the canonical output schema, grouped by category."""
    log.rule("Canonical Schema")
    log.blank()

    for group_name, fields in FIELD_GROUPS.items():
        table = Table(
            show_header=True,
            header_style="bold cyan",
            title=f"[bold white]{group_name}[/bold white]",
            title_justify="left",
            min_width=80,
        )
        table.add_column("Field", style="bold white", width=22)
        table.add_column("Req", width=5)
        table.add_column("Kind", width=10)
        table.add_column("Notes")

        for field in fields:
            required_mark = "[bold green]YES[/bold green]" if field in REQUIRED_FIELDS else "[dim]—[/dim]"
            if field in NUMERIC_FIELDS:
                kind = "[yellow]numeric[/yellow]"
            elif field in RATE_FIELDS:
                kind = "[magenta]rate[/magenta]"
            else:
                kind = "[cyan]text[/cyan]"
            note = FIELD_NOTES.get(field, "")
            table.add_row(field, required_mark, kind, note)

        log.console.print(table)
        log.blank()

    log.console.print(
        f"[dim]Total fields:[/dim] [bold]{len(CANONICAL_FIELDS)}[/bold]   "
        f"[dim]Numeric:[/dim] [bold]{len(NUMERIC_FIELDS)}[/bold]   "
        f"[dim]Rate:[/dim] [bold]{len(RATE_FIELDS)}[/bold]   "
        f"[dim]Default currency:[/dim] [bold cyan]{DEFAULT_CURRENCY}[/bold cyan]"
    )
    log.blank()
    log.info(f"Model: [bold]{settings.model}[/bold]")
    log.info(f"Output dir: [bold]{settings.output_dir}[/bold]")
    log.blank()
