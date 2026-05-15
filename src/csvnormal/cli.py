"""CSVNormal CLI — entry point for all commands."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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
from csvnormal.date_resolver import resolve_dates
from csvnormal.ingestion import InspectResult, RowKind, load_raw

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
    sample: int = typer.Option(5, "--sample", "-n", help="Number of data rows to preview."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show all warnings."),
) -> None:
    """Detect tables, preview structure, show warnings."""
    log.rule(f"inspect  {file.name}")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)

    # ── Load ──────────────────────────────────────────────────────────────────
    log.info(f"Loading [bold]{file}[/bold] …")
    try:
        result = load_raw(file)
    except Exception as exc:
        log.error(f"Failed to load file: {exc}")
        raise typer.Exit(1)

    _print_file_info(result)
    _print_structure(result)
    _print_row_breakdown(result)
    _print_warnings(result, verbose=verbose)
    _print_missing_values(result, verbose=verbose)
    _print_sample_data(result, n=sample)
    _print_date_resolution(file, result)

    log.blank()
    log.success(f"Inspection complete — {len(result.data_rows)} data rows, "
                f"{len(result.warnings)} structural warnings, "
                f"{len(result.missing_value_warnings)} missing-value warnings.")
    log.blank()


# ── inspect sub-renderers ─────────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def _print_file_info(r: InspectResult) -> None:
    log.blank()
    log.rule("File Info")
    log.info(f"Path      : [bold]{r.path}[/bold]")
    log.info(f"Size      : [bold]{_fmt_bytes(r.file_size_bytes)}[/bold]")
    conf_colour = "green" if r.encoding_confidence >= 0.9 else "yellow"
    log.info(
        f"Encoding  : [bold]{r.encoding.upper()}[/bold]  "
        f"[{conf_colour}](confidence: {r.encoding_confidence:.0%})[/{conf_colour}]"
    )


def _print_structure(r: InspectResult) -> None:
    log.blank()
    log.rule("Header")
    log.info(f"Detected at line [bold]{r.header_line_number}[/bold] — "
             f"[bold]{len(r.header_row)}[/bold] column(s)")

    # Show header columns in a compact table
    tbl = Table(show_header=False, box=None, padding=(0, 1))
    tbl.add_column("idx", style="dim", width=4)
    tbl.add_column("name", style="bold white")

    for i, col in enumerate(r.header_row):
        tbl.add_row(str(i), col)

    log.console.print(tbl)

    # Date column status
    if r.has_date_column:
        log.success(f"Date column detected: [bold]{r.date_column_name}[/bold]")
    else:
        log.warning("No date column found — period will be resolved from filename or user input")


def _print_row_breakdown(r: InspectResult) -> None:
    log.blank()
    log.rule("Row Breakdown")

    kind_styles = {
        RowKind.DATA:           ("data",           "green"),
        RowKind.BLANK:          ("blank",          "dim"),
        RowKind.NOTE:           ("note",           "yellow"),
        RowKind.SUBTOTAL:       ("subtotal",       "red"),
        RowKind.HEADER_REPEAT:  ("header_repeat",  "cyan"),
        RowKind.MALFORMED:      ("malformed",      "bold red"),
    }

    counts = {
        RowKind.DATA:          len(r.data_rows),
        RowKind.BLANK:         len(r.blank_rows),
        RowKind.NOTE:          len(r.note_rows),
        RowKind.SUBTOTAL:      len(r.subtotal_rows),
        RowKind.HEADER_REPEAT: len(r.repeated_header_rows),
        RowKind.MALFORMED:     len(r.malformed_rows),
    }

    tbl = Table(show_header=True, header_style="bold cyan")
    tbl.add_column("Row type", width=18)
    tbl.add_column("Count", justify="right", width=8)
    tbl.add_column("Status", width=30)

    for kind, (label, colour) in kind_styles.items():
        count = counts[kind]
        if kind == RowKind.DATA:
            status = "[green]✓ will be normalized[/green]" if count else "[dim]none[/dim]"
        elif count == 0:
            status = "[dim]none[/dim]"
        else:
            status = f"[{colour}]⚠ will be skipped[/{colour}]"
        tbl.add_row(f"[{colour}]{label}[/{colour}]", str(count), status)

    log.console.print(tbl)


def _print_warnings(r: InspectResult, verbose: bool) -> None:
    if not r.warnings:
        return
    log.blank()
    log.rule(f"Structural Warnings  ({len(r.warnings)})")

    display = r.warnings if verbose else r.warnings[:10]
    for w in display:
        colour = {
            RowKind.NOTE:           "yellow",
            RowKind.BLANK:          "dim",
            RowKind.SUBTOTAL:       "red",
            RowKind.HEADER_REPEAT:  "cyan",
            RowKind.MALFORMED:      "bold red",
        }.get(w.kind, "yellow")
        log.console.print(f"  [{colour}]line {w.line_number:>4}[/{colour}]  {w.message}")

    if not verbose and len(r.warnings) > 10:
        log.console.print(
            f"  [dim]… {len(r.warnings) - 10} more warnings hidden — use --verbose to see all[/dim]"
        )


def _print_missing_values(r: InspectResult, verbose: bool) -> None:
    if not r.missing_value_warnings:
        return
    log.blank()
    log.rule(f"Missing Value Warnings  ({len(r.missing_value_warnings)})")

    display = r.missing_value_warnings if verbose else r.missing_value_warnings[:10]
    for mv in display:
        cols = ", ".join(f"[bold]{c}[/bold]" for c in mv.empty_columns[:5])
        if len(mv.empty_columns) > 5:
            cols += f" + {len(mv.empty_columns) - 5} more"
        log.console.print(f"  [yellow]line {mv.line_number:>4}[/yellow]  empty: {cols}")

    if not verbose and len(r.missing_value_warnings) > 10:
        log.console.print(
            f"  [dim]… {len(r.missing_value_warnings) - 10} more — use --verbose to see all[/dim]"
        )


def _print_sample_data(r: InspectResult, n: int) -> None:
    data = r.data_rows[:n]
    if not data:
        log.warning("No data rows found to preview.")
        return

    log.blank()
    log.rule(f"Sample Data  (first {len(data)} of {len(r.data_rows)} data rows)")

    # Cap columns shown at 10 to stay readable
    display_headers = r.header_row[:10]
    truncated = len(r.header_row) > 10

    tbl = Table(show_header=True, header_style="bold cyan")
    for h in display_headers:
        tbl.add_column(h, no_wrap=False, max_width=18)
    if truncated:
        tbl.add_column(f"… +{len(r.header_row) - 10} cols", style="dim")

    for row in data:
        cells = row.cells[:10]
        # Pad if row is shorter than header
        while len(cells) < len(display_headers):
            cells.append("")
        row_vals = list(cells)
        if truncated:
            row_vals.append("…")
        tbl.add_row(*row_vals)

    log.console.print(tbl)


def _print_date_resolution(file: Path, r: InspectResult) -> None:
    log.blank()
    log.rule("Date Resolution")
    date_res = resolve_dates(file, has_date_column=r.has_date_column, interactive=False)

    if date_res.source == "csv_column":
        log.success(f"Date column [bold]{r.date_column_name}[/bold] present — no period resolution needed.")
    elif date_res.source == "filename":
        log.success(
            f"Period resolved from filename: [bold]{date_res.month_label}[/bold]  "
            f"([dim]{date_res.start_date} → {date_res.end_date}[/dim])"
        )
        log.info("Run [bold]csvnormal normalize[/bold] and the period will be filled automatically.")
    else:
        log.warning(
            "Could not resolve date period from filename. "
            "Run [bold]csvnormal normalize[/bold] to be prompted for the reporting period."
        )


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
