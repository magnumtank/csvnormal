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
from csvnormal.table_detector import DetectedTable, TableDetectionResult, detect_tables

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

    # ── Table detection ───────────────────────────────────────────────────────
    detection = detect_tables(result)
    _print_tables_overview(detection)
    _print_table_details(detection, verbose=verbose)

    _print_warnings(result, verbose=verbose)
    _print_missing_values(result, verbose=verbose)
    _print_sample_data(result, n=sample)
    _print_date_resolution(file, result)

    log.blank()
    log.success(
        f"Inspection complete — "
        f"{detection.total_tables} table(s) detected, "
        f"{detection.total_data_rows} data rows, "
        f"{len(result.warnings)} structural warnings."
    )
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


# ── table detection renderers ─────────────────────────────────────────────────


def _print_tables_overview(det: TableDetectionResult) -> None:
    log.blank()
    log.rule(f"Tables Detected  ({det.total_tables})")

    if not det.tables:
        log.warning("No logical tables found — file may be empty or entirely non-data.")
        return

    tbl = Table(show_header=True, header_style="bold cyan")
    tbl.add_column("Table", width=6, justify="center")
    tbl.add_column("Header line", width=12, justify="right")
    tbl.add_column("Cols", width=6, justify="right")
    tbl.add_column("Data rows", width=10, justify="right")
    tbl.add_column("Subtotals", width=10, justify="right")
    tbl.add_column("Date col", width=12)
    tbl.add_column("Confidence", width=14)
    tbl.add_column("Section title", width=30)

    conf_colour = {"HIGH": "green", "MEDIUM": "yellow", "LOW": "red", "VERY LOW": "bold red"}

    for t in det.tables:
        label = t.confidence_label
        colour = conf_colour.get(label, "white")
        date_info = (
            f"[green]{t.date_column_name}[/green]"
            if t.has_date_column
            else "[dim]none[/dim]"
        )
        title = (t.section_title or "")[: 28] or "[dim]—[/dim]"
        tbl.add_row(
            str(t.table_index),
            str(t.header_line_number),
            str(t.column_count),
            str(t.row_count),
            str(len(t.subtotal_rows)),
            date_info,
            f"[{colour}]{label} ({t.confidence:.0%})[/{colour}]",
            title,
        )

    log.console.print(tbl)


def _print_table_details(det: TableDetectionResult, verbose: bool) -> None:
    for t in det.tables:
        log.blank()
        title_part = f" — \"{t.section_title}\"" if t.section_title else ""
        log.rule(f"Table {t.table_index}{title_part}")

        # Column list (compact, 4 per row)
        cols = t.header_row
        rows_of_cols = [cols[i: i + 4] for i in range(0, len(cols), 4)]
        for chunk in rows_of_cols:
            log.console.print("  " + "   ".join(f"[dim]{i + rows_of_cols.index(chunk)*4}[/dim] [bold]{c}[/bold]" for i, c in enumerate(chunk)))

        log.blank()

        # Key column signals
        signals = []
        if t.has_spend_column:
            signals.append(f"[green]spend→ {t.spend_column_name}[/green]")
        else:
            signals.append("[red]no spend col[/red]")
        if t.has_delivery_column:
            signals.append(f"[green]delivery→ {t.delivery_column_name}[/green]")
        else:
            signals.append("[red]no delivery col[/red]")
        if t.has_date_column:
            signals.append(f"[green]date→ {t.date_column_name}[/green]")
        else:
            signals.append("[yellow]no date col[/yellow]")
        log.console.print("  Signals : " + "   ".join(signals))

        # Line range
        if t.start_line_number and t.end_line_number:
            log.console.print(
                f"  Lines   : {t.start_line_number}–{t.end_line_number}  "
                f"({t.row_count} data rows, {len(t.subtotal_rows)} subtotals)"
            )

        # Confidence detail
        conf_colour = {"HIGH": "green", "MEDIUM": "yellow", "LOW": "red", "VERY LOW": "bold red"}
        colour = conf_colour.get(t.confidence_label, "white")
        log.console.print(
            f"  Confid. : [{colour}]{t.confidence_label} ({t.confidence:.0%})[/{colour}]"
        )
        if verbose:
            for reason in t.confidence_reasons:
                log.console.print(f"    [dim]· {reason}[/dim]")

        # Warnings
        sev_colour = {"info": "cyan", "warn": "yellow", "error": "red"}
        for w in t.warnings:
            c = sev_colour.get(w.severity, "white")
            log.console.print(f"  [{c}]⚠ {w.message}[/{c}]")


# ── map ───────────────────────────────────────────────────────────────────────


@app.command(name="map")
def map_cmd(
    file: Path = typer.Argument(..., help="CSV file to map."),
    table: Optional[int] = typer.Option(None, "--table", "-t", help="Only map this table index."),
    retries: int = typer.Option(3, "--retries", "-r", help="AI retry budget."),
    save: bool = typer.Option(True, "--save/--no-save", help="Save mapping JSON to output/."),
    no_memory: bool = typer.Option(False, "--no-memory", help="Skip memory lookup; always call AI."),
) -> None:
    """Send headers + sample rows to AI, receive and validate column mapping JSON."""
    import json as _json

    from csvnormal.ai_mapper import MappingResult, map_table
    from csvnormal.memory import MappingMemory

    log.rule(f"map  {file.name}")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)

    memory = MappingMemory()

    # ── Phase 2+3: load and detect tables ────────────────────────────────────
    log.info(f"Loading [bold]{file}[/bold] …")
    try:
        raw = load_raw(file)
    except Exception as exc:
        log.error(f"Failed to load: {exc}")
        raise typer.Exit(1)

    detection = detect_tables(raw)

    if detection.total_tables == 0:
        log.error("No tables detected — nothing to map.")
        raise typer.Exit(1)

    log.info(f"Detected [bold]{detection.total_tables}[/bold] table(s).")

    tables_to_map = (
        [detection.tables[table]]
        if table is not None
        else detection.tables
    )

    results: list[MappingResult] = []

    for tbl in tables_to_map:
        log.blank()
        log.rule(f"Table {tbl.table_index} — {tbl.row_count} data rows, {tbl.column_count} columns")
        if tbl.section_title:
            log.info(f"Section: [bold]{tbl.section_title}[/bold]")

        # ── Memory lookup (Phase 8) ───────────────────────────────────────────
        result: MappingResult | None = None
        if not no_memory and memory.covers_header(tbl.header_row):
            log.info(
                "[bold green]Memory hit[/bold green] — all columns known; skipping AI call."
            )
            result = _mapping_from_memory(memory, tbl, file)

        if result is None:
            try:
                result = map_table(tbl, source_file=file, max_retries=retries)
            except RuntimeError as exc:
                log.error(str(exc))
                raise typer.Exit(1)
            memory.record_from_mapping(result, source="ai")
            memory.save()

        results.append(result)
        _print_mapping_result(result)

        if save:
            _save_mapping(result, file)

    log.blank()
    log.success(
        f"Mapping complete — {len(results)} table(s) mapped. "
        + (f"Files saved to [bold]{settings.output_dir}/[/bold]" if save else "")
    )
    log.blank()


# ── map renderers ─────────────────────────────────────────────────────────────


def _print_mapping_result(r: "MappingResult") -> None:
    from csvnormal.ai_mapper import MappingResult

    log.blank()
    log.rule("Column Mappings")

    tbl = Table(show_header=True, header_style="bold cyan")
    tbl.add_column("Source column", style="bold white", width=26)
    tbl.add_column("→ Canonical field", width=22)
    tbl.add_column("Conf", width=6, justify="right")
    tbl.add_column("Reason", width=36)

    conf_colours = {"__skip__": "dim", "__unknown__": "red"}
    for src_col, entry in r.column_mapping.items():
        canon = entry.canonical
        colour = conf_colours.get(canon, "green" if entry.confidence >= 0.8 else "yellow")
        tbl.add_row(
            src_col,
            f"[{colour}]{canon}[/{colour}]",
            f"{entry.confidence:.0%}",
            entry.reason,
        )

    log.console.print(tbl)

    if r.platform_mapping:
        log.blank()
        log.rule("Platform Mappings")
        ptbl = Table(show_header=True, header_style="bold cyan")
        ptbl.add_column("Source value", style="bold white", width=26)
        ptbl.add_column("→ Canonical platform", width=22)
        ptbl.add_column("Conf", width=6, justify="right")

        for src_val, entry in r.platform_mapping.items():
            colour = "dim" if entry.canonical == "__unknown__" else "green"
            ptbl.add_row(
                src_val,
                f"[{colour}]{entry.canonical}[/{colour}]",
                f"{entry.confidence:.0%}",
            )
        log.console.print(ptbl)

    if r.warnings:
        log.blank()
        log.rule("AI Warnings")
        for w in r.warnings:
            log.warning(w)

    if r.unknown_columns:
        log.blank()
        log.warning(
            f"Unknown columns (need manual review): [bold]{', '.join(r.unknown_columns)}[/bold]"
        )

    conf_colour = "green" if r.overall_confidence >= 0.8 else "yellow" if r.overall_confidence >= 0.6 else "red"
    log.blank()
    log.console.print(
        f"  Overall confidence : [{conf_colour}]{r.overall_confidence:.0%}[/{conf_colour}]   "
        f"Table type : [bold]{r.table_type}[/bold]   "
        f"Model : [dim]{r.model_used}[/dim]"
    )


def _save_mapping(result: "MappingResult", source_file: Path) -> None:
    import json as _json

    stem = source_file.stem
    out_path = settings.output_dir / f"{stem}_table{result.table_index}_mapping.json"
    out_path.write_text(_json.dumps(result.to_audit_dict(), indent=2, default=str))
    log.success(f"Mapping saved → [bold]{out_path}[/bold]")


def _mapping_from_memory(
    memory: "MappingMemory",  # type: ignore[name-defined]
    tbl: "DetectedTable",
    source_file: Path,
) -> "MappingResult":
    """Build a MappingResult entirely from memory entries (no AI call)."""
    from datetime import UTC, datetime

    from csvnormal.ai_mapper import ColumnMappingEntry, MappingResult
    from csvnormal.memory import MappingMemory
    from csvnormal.prompts import PROMPT_VERSION

    col_map = {}
    for col in tbl.header_row:
        entry = memory.lookup_column(col)
        if entry:
            col_map[col] = ColumnMappingEntry(
                canonical=entry.canonical,
                confidence=entry.confidence,
                reason=f"memory ({entry.source})",
            )
        else:
            col_map[col] = ColumnMappingEntry(
                canonical="__unknown__",
                confidence=0.0,
                reason="not in memory",
            )

    return MappingResult(
        source_file=source_file.name,
        table_index=tbl.table_index,
        section_title=tbl.section_title,
        mapped_at=datetime.now(UTC).isoformat(),
        model_used="memory",
        prompt_version=PROMPT_VERSION,
        table_type="unknown",
        column_mapping=col_map,
        platform_mapping={},
        warnings=["Mapping sourced from memory — no AI call made."],
        overall_confidence=min(e.confidence for e in col_map.values()),
        raw_response="",
    )


# ── normalize ─────────────────────────────────────────────────────────────────


@app.command()
def normalize(
    file: Path = typer.Argument(..., help="CSV file to normalize."),
    mapping_file: Optional[Path] = typer.Option(None, "--mapping", "-m", help="Mapping JSON file (skips AI call)."),
    client: str = typer.Option("", "--client", "-c", help="Client / advertiser name."),
    table: Optional[int] = typer.Option(None, "--table", "-t", help="Only normalize this table index."),
    retries: int = typer.Option(3, "--retries", "-r", help="AI retry budget (ignored with --mapping)."),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o", help="Override output directory."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Skip AI mapping — requires --mapping file."),
) -> None:
    """Load CSV, map columns via AI (or provided mapping), and write canonical CSV + audit JSON."""
    import json as _json

    from csvnormal.ai_mapper import MappingResult, map_table
    from csvnormal.normalizer import NormalizeResult, normalize_table, write_audit_json, write_canonical_csv

    log.rule(f"normalize  {file.name}")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)

    out_dir = output_dir or settings.output_dir

    # ── Load and detect ───────────────────────────────────────────────────────
    log.info(f"Loading [bold]{file}[/bold] …")
    try:
        raw = load_raw(file)
    except Exception as exc:
        log.error(f"Failed to load: {exc}")
        raise typer.Exit(1)

    detection = detect_tables(raw)
    if detection.total_tables == 0:
        log.error("No tables detected — nothing to normalize.")
        raise typer.Exit(1)

    # ── Date resolution ───────────────────────────────────────────────────────
    date_res = resolve_dates(file, raw)

    # ── Load or acquire mapping ───────────────────────────────────────────────
    preloaded_mapping: Optional[MappingResult] = None
    if mapping_file:
        if not mapping_file.exists():
            log.error(f"Mapping file not found: {mapping_file}")
            raise typer.Exit(1)
        try:
            data = _json.loads(mapping_file.read_text(encoding="utf-8"))
            payload = data.get("mapping", data)
            preloaded_mapping = MappingResult.model_validate(payload)
            log.info(f"Loaded mapping from [bold]{mapping_file.name}[/bold]")
        except Exception as exc:
            log.error(f"Failed to parse mapping file: {exc}")
            raise typer.Exit(1)

    if no_ai and preloaded_mapping is None:
        log.error("--no-ai requires a --mapping file.")
        raise typer.Exit(1)

    # ── Process tables ────────────────────────────────────────────────────────
    tables_to_process = (
        [detection.tables[table]] if table is not None else detection.tables
    )

    normalize_results: list[NormalizeResult] = []
    mapping_results: list[MappingResult] = []

    for tbl in tables_to_process:
        log.blank()
        log.rule(f"Table {tbl.table_index} — {tbl.row_count} rows, {tbl.column_count} columns")
        if tbl.section_title:
            log.info(f"Section: [bold]{tbl.section_title}[/bold]")

        if preloaded_mapping is not None:
            mapping_result = preloaded_mapping
            log.info("Using pre-loaded mapping.")
        else:
            log.info("Requesting AI column mapping …")
            try:
                mapping_result = map_table(tbl, source_file=file, max_retries=retries)
            except RuntimeError as exc:
                log.error(str(exc))
                raise typer.Exit(1)

        mapping_results.append(mapping_result)

        norm = normalize_table(
            table=tbl,
            mapping=mapping_result,
            date_resolution=date_res,
            client_name=client,
        )
        normalize_results.append(norm)

        _print_normalize_summary(norm)

        stem = f"{file.stem}_table{tbl.table_index}"
        csv_path = out_dir / f"{stem}_canonical.csv"
        audit_path = out_dir / f"{stem}_audit.json"
        write_canonical_csv(norm, csv_path)
        write_audit_json(norm, mapping_result, audit_path)

    log.blank()
    log.success(
        f"Normalization complete — {len(normalize_results)} table(s). "
        f"Output in [bold]{out_dir}/[/bold]"
    )
    log.blank()


# ── normalize renderers ───────────────────────────────────────────────────────


def _print_normalize_summary(norm: "NormalizeResult") -> None:  # type: ignore[name-defined]
    log.blank()
    log.info(
        f"  [bold]{norm.row_count}[/bold] rows normalized → "
        f"[bold]{norm.column_count}[/bold] canonical columns used"
    )
    if norm.skipped_source_columns:
        log.info(f"  Skipped columns : {norm.skipped_source_columns}")
    if norm.unknown_source_columns:
        log.warning(f"  Unknown columns : {norm.unknown_source_columns}")
    for w in norm.warnings:
        log.warning(f"  {w.message}")
    if norm.date_resolution_source not in ("csv_column", "none"):
        log.info(f"  Date source     : {norm.date_resolution_source}")


# ── validate ──────────────────────────────────────────────────────────────────


@app.command()
def validate(
    file: Path = typer.Argument(..., help="Canonical CSV to validate."),
    audit: Optional[Path] = typer.Option(None, "--audit", "-a", help="Audit JSON sidecar (enables row-count check)."),
    strict: bool = typer.Option(False, "--strict", help="Exit 1 on warnings too, not just errors."),
) -> None:
    """Verify row counts, numeric integrity, required fields, and value ranges."""
    import json as _json

    from csvnormal.validator import ValidationReport, load_canonical_csv, run_all_checks

    log.rule(f"validate  {file.name}")
    if not file.exists():
        log.error(f"File not found: {file}")
        raise typer.Exit(1)

    # ── Load canonical CSV ────────────────────────────────────────────────────
    try:
        header, rows = load_canonical_csv(file)
    except Exception as exc:
        log.error(f"Failed to read CSV: {exc}")
        raise typer.Exit(1)

    log.info(f"Loaded [bold]{len(rows)}[/bold] rows, [bold]{len(header)}[/bold] columns.")

    # ── Try to find audit sidecar ─────────────────────────────────────────────
    source_row_count: Optional[int] = None
    audit_path = audit

    # Auto-discover: same stem but _audit.json
    if audit_path is None:
        candidate = file.parent / file.name.replace("_canonical.csv", "_audit.json")
        if candidate.exists():
            audit_path = candidate
            log.info(f"Auto-detected audit sidecar: [bold]{candidate.name}[/bold]")

    if audit_path and audit_path.exists():
        try:
            data = _json.loads(audit_path.read_text(encoding="utf-8"))
            norm = data.get("normalize", data)
            source_row_count = norm.get("row_count")
        except Exception:
            log.warning("Could not parse audit JSON — skipping row count check.")

    # ── Run checks ────────────────────────────────────────────────────────────
    report = run_all_checks(rows, source_file=file.name, source_row_count=source_row_count)

    _print_validation_report(report)

    # ── Exit code ─────────────────────────────────────────────────────────────
    if not report.passed:
        log.error(
            f"Validation failed — {report.error_count} error(s), "
            f"{report.warning_count} warning(s)."
        )
        raise typer.Exit(1)

    if strict and report.warning_count > 0:
        log.error(f"Strict mode: {report.warning_count} warning(s) treated as errors.")
        raise typer.Exit(1)

    log.success(
        f"Validation passed — {report.error_count} error(s), "
        f"{report.warning_count} warning(s)."
    )
    log.blank()


# ── validate renderer ─────────────────────────────────────────────────────────


def _print_validation_report(report: "ValidationReport") -> None:  # type: ignore[name-defined]
    from csvnormal.validator import Severity

    log.blank()

    tbl = Table(show_header=True, header_style="bold cyan")
    tbl.add_column("Check", style="bold white", width=24)
    tbl.add_column("Result", width=8, justify="center")
    tbl.add_column("Details", width=60)

    icons = {
        (True,  Severity.ERROR):   ("[green]PASS[/green]",   "green"),
        (True,  Severity.WARNING): ("[green]PASS[/green]",   "green"),
        (True,  Severity.INFO):    ("[cyan]INFO[/cyan]",     "cyan"),
        (False, Severity.ERROR):   ("[red]FAIL[/red]",       "red"),
        (False, Severity.WARNING): ("[yellow]WARN[/yellow]", "yellow"),
        (False, Severity.INFO):    ("[cyan]INFO[/cyan]",     "cyan"),
    }

    for check in report.checks:
        icon_str, row_style = icons[(check.passed, check.severity)]
        detail_text = check.message
        if check.details:
            detail_text += "\n" + "\n".join(f"  • {d}" for d in check.details[:5])
            if len(check.details) > 5:
                detail_text += f"\n  … and {len(check.details) - 5} more"
        tbl.add_row(
            check.name,
            Text.from_markup(icon_str),
            detail_text,
            style=row_style if not check.passed else "",
        )

    log.console.print(tbl)
    log.blank()


# ── review ────────────────────────────────────────────────────────────────────


@app.command()
def review(
    mapping_file: Path = typer.Argument(..., help="Mapping JSON file to review."),
    threshold: float = typer.Option(0.75, "--threshold", "-t", help="Confidence threshold — columns below this are flagged."),
    all_columns: bool = typer.Option(False, "--all", "-a", help="Review every column, not just low-confidence ones."),
    save: bool = typer.Option(True, "--save/--no-save", help="Save corrected mapping back to the same JSON file."),
) -> None:
    """Interactively review and correct AI column mappings; corrections are saved to memory."""
    import json as _json

    from csvnormal.ai_mapper import MappingResult
    from csvnormal.memory import MappingMemory
    from csvnormal.review import run_review

    log.rule(f"review  {mapping_file.name}")
    if not mapping_file.exists():
        log.error(f"File not found: {mapping_file}")
        raise typer.Exit(1)

    # Load mapping
    try:
        data = _json.loads(mapping_file.read_text(encoding="utf-8"))
        payload = data.get("mapping", data)
        mapping_result = MappingResult.model_validate(payload)
    except Exception as exc:
        log.error(f"Failed to parse mapping file: {exc}")
        raise typer.Exit(1)

    log.info(
        f"Loaded mapping: [bold]{len(mapping_result.column_mapping)}[/bold] columns, "
        f"table type: [bold]{mapping_result.table_type}[/bold], "
        f"overall confidence: [bold]{mapping_result.overall_confidence:.0%}[/bold]"
    )
    log.blank()

    memory = MappingMemory()

    # Run interactive review
    updated_mapping, overridden = run_review(
        mapping=mapping_result,
        threshold=threshold,
        review_all=all_columns,
    )

    # Record everything to memory (user corrections + accepted AI decisions)
    for col, entry in updated_mapping.column_mapping.items():
        src = "user" if col in overridden else "ai"
        memory.record_column(col, entry.canonical, entry.confidence, source=src)
    memory.save()

    if overridden:
        log.info(f"Memory updated with {len(overridden)} user correction(s).")

    # Save corrected mapping back to file
    if save and overridden:
        mapping_file.write_text(
            _json.dumps(updated_mapping.to_audit_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        log.success(f"Corrected mapping saved → [bold]{mapping_file}[/bold]")
    elif not overridden:
        log.info("No changes — mapping file unchanged.")

    log.blank()


# ── memory subcommand group ───────────────────────────────────────────────────

memory_app = typer.Typer(name="memory", help="Inspect and manage the column mapping memory.", no_args_is_help=True)
app.add_typer(memory_app, name="memory")


@memory_app.command(name="stats")
def memory_stats() -> None:
    """Show summary statistics for the mapping memory."""
    from csvnormal.memory import MappingMemory

    mem = MappingMemory()
    s = mem.stats()
    log.rule("Mapping Memory — Stats")
    log.info(f"  Total entries     : [bold]{s['total']}[/bold]")
    log.info(f"  User corrections  : [bold cyan]{s['user_corrections']}[/bold cyan]")
    log.info(f"  AI decisions      : [bold]{s['ai_decisions']}[/bold]")
    log.info(f"  File              : [dim]{mem._path}[/dim]")
    log.blank()


@memory_app.command(name="list")
def memory_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Max entries to show."),
    user_only: bool = typer.Option(False, "--user-only", help="Show only user corrections."),
) -> None:
    """List all remembered column → canonical mappings."""
    from csvnormal.memory import MappingMemory

    mem = MappingMemory()
    entries = mem.list_entries()

    if user_only:
        entries = [(k, v) for k, v in entries if v.source == "user"]

    log.rule(f"Mapping Memory — {len(entries)} entries")
    if not entries:
        log.info("  Memory is empty.")
        log.blank()
        return

    tbl = Table(show_header=True, header_style="bold cyan")
    tbl.add_column("Normalized key",  width=28)
    tbl.add_column("→ Canonical",     width=22)
    tbl.add_column("Conf",            width=6,  justify="right")
    tbl.add_column("Source",          width=8)
    tbl.add_column("Used",            width=6,  justify="right")
    tbl.add_column("Last seen",       width=12)

    for key, entry in entries[:limit]:
        style = "cyan" if entry.source == "user" else ""
        tbl.add_row(
            key,
            entry.canonical,
            f"{entry.confidence:.2f}",
            f"[cyan]{entry.source}[/cyan]" if entry.source == "user" else entry.source,
            str(entry.usage_count),
            entry.last_seen,
            style=style,
        )

    log.console.print(tbl)
    if len(entries) > limit:
        log.info(f"  … and {len(entries) - limit} more. Use --limit to see more.")
    log.blank()


@memory_app.command(name="clear")
def memory_clear(
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Delete all entries from the mapping memory."""
    from csvnormal.memory import MappingMemory

    mem = MappingMemory()
    s = mem.stats()

    if not confirm:
        typer.confirm(
            f"This will delete all {s['total']} memory entries. Continue?",
            abort=True,
        )

    mem.clear()
    mem.save()
    log.success("Memory cleared.")
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
