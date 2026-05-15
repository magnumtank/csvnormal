"""
Table detection — groups classified rows from InspectResult into logical tables.

Design rules:
  - Builds on top of ingestion.py row classification; never re-reads the file.
  - A new table begins when a HEADER_REPEAT row is encountered.
  - Subtotal, blank, note, and malformed rows are skipped but tracked.
  - Confidence scores are computed deterministically from structural signals.
  - No AI calls in this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, computed_field

from csvnormal.ingestion import InspectResult, RawRow, RowKind

# ── Keyword sets for confidence scoring ──────────────────────────────────────

_SPEND_KEYWORDS: frozenset[str] = frozenset({
    "spend", "cost", "budget", "investment", "media cost",
    "budget spend", "search spend", "display budget",
})

_DELIVERY_KEYWORDS: frozenset[str] = frozenset({
    "impressions", "imps", "served imps", "reach", "clicks",
    "link clicks", "fb clicks", "fb imps",
})

_DATE_KEYWORDS: frozenset[str] = frozenset({
    "date", "day", "week", "period", "month", "year",
    "start date", "end date", "start_date", "end_date",
    "flight", "reporting date",
})

# ── Models ────────────────────────────────────────────────────────────────────


class TableWarning(BaseModel):
    message: str
    severity: str  # "info" | "warn" | "error"


class DetectedTable(BaseModel):
    """One logical table extracted from the CSV."""

    model_config = {"arbitrary_types_allowed": True}

    table_index: int            # 0-based
    header_line_number: int     # 1-based line in file where the header lives
    header_row: list[str]       # column names for this table
    section_title: Optional[str] = None  # nearest NOTE text before this header

    data_rows: list[RawRow]     # only RowKind.DATA rows
    subtotal_rows: list[RawRow] # RowKind.SUBTOTAL rows inside this table's span

    confidence: float           # 0.0 – 1.0
    confidence_reasons: list[str]

    has_date_column: bool
    date_column_name: Optional[str] = None

    has_spend_column: bool
    spend_column_name: Optional[str] = None

    has_delivery_column: bool
    delivery_column_name: Optional[str] = None

    warnings: list[TableWarning]

    @property
    def row_count(self) -> int:
        return len(self.data_rows)

    @property
    def column_count(self) -> int:
        return len(self.header_row)

    @property
    def start_line_number(self) -> Optional[int]:
        return self.data_rows[0].line_number if self.data_rows else None

    @property
    def end_line_number(self) -> Optional[int]:
        return self.data_rows[-1].line_number if self.data_rows else None

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.85:
            return "HIGH"
        if self.confidence >= 0.65:
            return "MEDIUM"
        if self.confidence >= 0.40:
            return "LOW"
        return "VERY LOW"


class TableDetectionResult(BaseModel):
    """Result of running detect_tables() on an InspectResult."""

    model_config = {"arbitrary_types_allowed": True}

    source_path: Path
    tables: list[DetectedTable]

    @property
    def total_tables(self) -> int:
        return len(self.tables)

    @property
    def total_data_rows(self) -> int:
        return sum(t.row_count for t in self.tables)

    @property
    def high_confidence_tables(self) -> list[DetectedTable]:
        return [t for t in self.tables if t.confidence >= 0.85]

    @property
    def needs_review(self) -> list[DetectedTable]:
        return [t for t in self.tables if t.confidence < 0.65]


# ── Column signal detection ────────────────────────────────────────────────────


def _find_column(header: list[str], keywords: frozenset[str]) -> tuple[bool, Optional[str]]:
    """Return (found, column_name) for the first header matching any keyword."""
    for col in header:
        if col.strip().lower() in keywords:
            return True, col.strip()
    # Partial match fallback
    for col in header:
        col_lower = col.strip().lower()
        for kw in keywords:
            if kw in col_lower:
                return True, col.strip()
    return False, None


# ── Confidence scorer ─────────────────────────────────────────────────────────


def _compute_confidence(
    header: list[str],
    data_rows: list[RawRow],
    has_spend: bool,
    has_delivery: bool,
    has_date: bool,
) -> tuple[float, list[str]]:
    """
    Compute a confidence score and list of reasons for a detected table.
    Score is 0.0–1.0. Reasons explain what helped or hurt confidence.
    """
    score = 1.0
    reasons: list[str] = []

    # ── Data row count ──
    if len(data_rows) == 0:
        return 0.0, ["No data rows — not a usable table"]
    if len(data_rows) < 2:
        score -= 0.30
        reasons.append(f"Only {len(data_rows)} data row — very thin table")
    elif len(data_rows) < 4:
        score -= 0.10
        reasons.append(f"Only {len(data_rows)} data rows — small table")
    else:
        reasons.append(f"{len(data_rows)} data rows")

    # ── Column consistency ──
    expected = len(header)
    inconsistent = [r for r in data_rows if len(r.cells) != expected]
    if inconsistent:
        ratio = len(inconsistent) / len(data_rows)
        penalty = round(0.25 * ratio, 2)
        score -= penalty
        reasons.append(
            f"{len(inconsistent)} row(s) have wrong column count "
            f"(expected {expected}) — confidence -{penalty:.0%}"
        )
    else:
        reasons.append(f"All {len(data_rows)} rows match {expected}-column schema")

    # ── Spend column ──
    if has_spend:
        reasons.append("Spend column present ✓")
    else:
        score -= 0.15
        reasons.append("No spend/cost column detected — confidence -15%")

    # ── Delivery column ──
    if has_delivery:
        reasons.append("Delivery column present ✓")
    else:
        score -= 0.10
        reasons.append("No impressions/clicks column detected — confidence -10%")

    # ── Date column ──
    if has_date:
        reasons.append("Date column present ✓")
    else:
        score -= 0.05
        reasons.append("No date column — period will be resolved from filename/prompt")

    return round(max(0.0, min(1.0, score)), 4), reasons


# ── Table builder ─────────────────────────────────────────────────────────────


def _build_table(
    index: int,
    header: list[str],
    header_line: int,
    section_title: Optional[str],
    data_rows: list[RawRow],
    subtotal_rows: list[RawRow],
) -> DetectedTable:
    warnings: list[TableWarning] = []

    # Column signals
    has_date, date_col = _find_column(header, _DATE_KEYWORDS)
    has_spend, spend_col = _find_column(header, _SPEND_KEYWORDS)
    has_delivery, delivery_col = _find_column(header, _DELIVERY_KEYWORDS)

    # Confidence
    confidence, reasons = _compute_confidence(
        header, data_rows, has_spend, has_delivery, has_date
    )

    # Structural warnings
    if not data_rows:
        warnings.append(TableWarning(
            message="Table has no data rows — will be skipped during normalization",
            severity="error",
        ))

    if not has_spend:
        warnings.append(TableWarning(
            message="Could not identify a spend/cost column",
            severity="warn",
        ))

    if not has_delivery:
        warnings.append(TableWarning(
            message="Could not identify an impressions/clicks column",
            severity="warn",
        ))

    if subtotal_rows:
        warnings.append(TableWarning(
            message=f"{len(subtotal_rows)} subtotal row(s) will be excluded from normalized output",
            severity="info",
        ))

    if data_rows:
        expected = len(header)
        bad = [r for r in data_rows if len(r.cells) != expected]
        if bad:
            warnings.append(TableWarning(
                message=(
                    f"{len(bad)} data row(s) have unexpected column count "
                    f"(expected {expected}); review before normalizing"
                ),
                severity="warn",
            ))

    return DetectedTable(
        table_index=index,
        header_line_number=header_line,
        header_row=header,
        section_title=section_title,
        data_rows=data_rows,
        subtotal_rows=subtotal_rows,
        confidence=confidence,
        confidence_reasons=reasons,
        has_date_column=has_date,
        date_column_name=date_col,
        has_spend_column=has_spend,
        spend_column_name=spend_col,
        has_delivery_column=has_delivery,
        delivery_column_name=delivery_col,
        warnings=warnings,
    )


# ── Main entry point ──────────────────────────────────────────────────────────


def detect_tables(result: InspectResult) -> TableDetectionResult:
    """
    Walk the classified rows from an InspectResult and group them into
    logical DetectedTable objects.

    Table boundaries are created when:
      - A HEADER_REPEAT row is encountered (new section with its own header)

    BLANK, NOTE, MALFORMED rows are ignored for boundary detection.
    SUBTOTAL rows are collected per-table but excluded from data.

    The most recent NOTE or HEADER_REPEAT text is tracked as section_title
    for the next table (e.g. "GOOGLE ADS PERFORMANCE REPORT — Q1 2024").
    """
    tables: list[DetectedTable] = []

    current_header: list[str] = result.header_row
    current_header_line: int = result.header_line_number
    current_section_title: Optional[str] = None
    current_data: list[RawRow] = []
    current_subtotals: list[RawRow] = []
    last_note_text: Optional[str] = None  # most recent meaningful NOTE text

    def _flush() -> None:
        """Close and store the current in-progress table."""
        nonlocal current_data, current_subtotals
        if current_data or current_subtotals:
            tables.append(_build_table(
                index=len(tables),
                header=current_header,
                header_line=current_header_line,
                section_title=current_section_title,
                data_rows=current_data,
                subtotal_rows=current_subtotals,
            ))
        current_data = []
        current_subtotals = []

    for row in result.rows:
        if row.kind == RowKind.DATA:
            current_data.append(row)
            last_note_text = None  # reset — we're inside a table now

        elif row.kind == RowKind.SUBTOTAL:
            current_subtotals.append(row)

        elif row.kind == RowKind.NOTE:
            # Capture as potential section title for the *next* table
            candidate = (row.cells[0].strip() if row.cells else "").strip("=- *")
            if candidate and len(candidate) > 4:
                last_note_text = candidate

        elif row.kind == RowKind.HEADER_REPEAT:
            # New table boundary — flush what we have
            _flush()

            # The HEADER_REPEAT row's cells become the new table's header
            current_header = [c.strip() for c in row.cells if c.strip() or True]
            # Keep only as many columns as there are cells
            current_header = [c.strip() for c in row.cells]
            current_header_line = row.line_number
            current_section_title = last_note_text
            last_note_text = None

        # BLANK and MALFORMED rows are silently skipped

    # Close the final table
    _flush()

    # Edge case: if no tables found at all (e.g. all rows were subtotals/blanks),
    # return an empty result rather than crashing.
    return TableDetectionResult(
        source_path=result.path,
        tables=tables,
    )
