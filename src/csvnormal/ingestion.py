"""
CSV ingestion — safe loading, encoding detection, row classification.

Design rules:
  - Every cell is loaded as a raw string. No type coercion ever happens here.
  - Numeric values are NEVER touched, cast, or interpreted.
  - All classification is deterministic — no AI calls in this module.
  - This module only returns data structures; all terminal output is in cli.py.
"""

from __future__ import annotations

import csv
import io
import re
from enum import Enum
from pathlib import Path
from typing import Optional

import chardet
from pydantic import BaseModel

# ── Row kind taxonomy ─────────────────────────────────────────────────────────


class RowKind(str, Enum):
    DATA = "data"
    BLANK = "blank"
    NOTE = "note"               # metadata / title / export-info rows
    SUBTOTAL = "subtotal"       # any row that aggregates values
    HEADER_REPEAT = "header_repeat"  # repeated header in a multi-table CSV
    MALFORMED = "malformed"     # wrong column count, un-parseable


# ── Pydantic models ───────────────────────────────────────────────────────────


class RowWarning(BaseModel):
    line_number: int    # 1-based line number in file
    kind: RowKind
    message: str


class RawRow(BaseModel):
    line_number: int    # 1-based
    kind: RowKind
    cells: list[str]   # exact string values — never modified
    warning: Optional[RowWarning] = None


class MissingValueWarning(BaseModel):
    line_number: int
    empty_columns: list[str]   # header names of empty cells
    empty_indices: list[int]   # 0-based column indices


class InspectResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    path: Path
    file_size_bytes: int
    encoding: str
    encoding_confidence: float

    # Header
    header_line_number: int     # 1-based
    header_row: list[str]

    # All rows after (and including) the header line
    rows: list[RawRow]
    warnings: list[RowWarning]
    missing_value_warnings: list[MissingValueWarning]

    # Date metadata
    has_date_column: bool
    date_column_name: Optional[str] = None

    # Row type counts — computed properties
    @property
    def data_rows(self) -> list[RawRow]:
        return [r for r in self.rows if r.kind == RowKind.DATA]

    @property
    def blank_rows(self) -> list[RawRow]:
        return [r for r in self.rows if r.kind == RowKind.BLANK]

    @property
    def subtotal_rows(self) -> list[RawRow]:
        return [r for r in self.rows if r.kind == RowKind.SUBTOTAL]

    @property
    def note_rows(self) -> list[RawRow]:
        return [r for r in self.rows if r.kind == RowKind.NOTE]

    @property
    def malformed_rows(self) -> list[RawRow]:
        return [r for r in self.rows if r.kind == RowKind.MALFORMED]

    @property
    def repeated_header_rows(self) -> list[RawRow]:
        return [r for r in self.rows if r.kind == RowKind.HEADER_REPEAT]


# ── Classification constants ──────────────────────────────────────────────────

_SUBTOTAL_MARKERS: frozenset[str] = frozenset({
    "total", "subtotal", "grand total", "sub total", "totals",
    "sum", "summary", "net total", "overall total", "combined total",
    "search subtotal", "social subtotal", "display subtotal",
})

_NOTE_PREFIXES: tuple[str, ...] = (
    "note:", "notes:", "generated", "report", "client:", "period:",
    "exported", "source:", "===", "---", "***", "confidential",
    "currency:", "all figures", "disclaimer", "figures in",
)

_DATE_COLUMN_NAMES: frozenset[str] = frozenset({
    "date", "day", "week", "period", "month", "year",
    "start date", "end date", "start_date", "end_date",
    "flight", "reporting date", "report date", "flight date",
    "activity date", "run date",
})

# Words commonly found in header rows — used to detect the header line
_HEADER_KEYWORDS: frozenset[str] = frozenset({
    "campaign", "spend", "impressions", "clicks", "date", "platform",
    "client", "advertiser", "cost", "ctr", "cpc", "cpm", "conversions",
    "revenue", "budget", "investment", "media cost", "account",
    "channel", "network", "flight", "ad", "placement", "reach",
})

# ── Utility functions ─────────────────────────────────────────────────────────


def _is_numeric(value: str) -> bool:
    """Return True if value looks like a number (int, float, negative, comma-formatted)."""
    v = value.strip().replace(",", "").lstrip("-").lstrip("+")
    if not v:
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


def _non_empty(cells: list[str]) -> list[str]:
    return [c for c in cells if c.strip()]


def _cell_matches_subtotal(cell: str) -> bool:
    lower = cell.strip().lower()
    # Exact match
    if lower in _SUBTOTAL_MARKERS:
        return True
    # Word-boundary match — avoids "sum" matching inside "summer", etc.
    for marker in _SUBTOTAL_MARKERS:
        pattern = r"\b" + re.escape(marker) + r"\b"
        if re.search(pattern, lower) and len(lower) <= len(marker) + 20:
            return True
    return False


# ── Encoding detection ────────────────────────────────────────────────────────


def detect_encoding(path: Path) -> tuple[str, float]:
    """
    Detect file encoding using chardet.
    Falls back to utf-8 if detection fails.
    Returns (encoding, confidence).
    """
    raw_bytes = path.read_bytes()
    result = chardet.detect(raw_bytes)
    encoding = result.get("encoding") or "utf-8"
    confidence = float(result.get("confidence") or 0.0)

    # Normalise common aliases
    encoding = encoding.lower()
    if encoding in ("ascii", "windows-1252", "iso-8859-1"):
        # These are all safely readable as utf-8 for ASCII range
        encoding = "utf-8"
    elif encoding.startswith("utf-8"):
        encoding = "utf-8"

    return encoding, confidence


# ── Header detection ──────────────────────────────────────────────────────────


def _find_header(all_rows: list[list[str]]) -> tuple[int, list[str]] | None:
    """
    Scan rows top-to-bottom for the first row that looks like a column header.

    A row is a header if:
      - It has >= 3 non-empty cells
      - At most half its cells are numeric
      - It matches at least 1 known header keyword  OR  has >= 4 non-numeric cells

    Returns (0-based row index, cells) or None.
    """
    for i, cells in enumerate(all_rows):
        ne = _non_empty(cells)
        if len(ne) < 3:
            continue

        numeric_count = sum(1 for c in ne if _is_numeric(c))
        text_count = len(ne) - numeric_count

        # Reject if more than half are numbers
        if numeric_count > len(ne) / 2:
            continue

        lower_set = {c.strip().lower() for c in ne}
        keyword_hits = lower_set & _HEADER_KEYWORDS

        # Strong signal: keyword match
        if keyword_hits:
            return i, cells

        # Weak signal: 4+ text cells and no numbers at all
        if text_count >= 4 and numeric_count == 0:
            return i, cells

    return None


# ── Row classifier ────────────────────────────────────────────────────────────


def _classify_row(
    cells: list[str],
    header_col_count: int,
    header_cells_lower: list[str],
) -> tuple[RowKind, str]:
    """
    Classify a single row. Returns (RowKind, human-readable reason).
    Does NOT mutate cells.
    """
    ne = _non_empty(cells)

    # ── Blank ──
    if not ne:
        return RowKind.BLANK, "Empty row"

    # ── Note / metadata (single-cell or known note prefix) ──
    first_cell = cells[0].strip().lower() if cells else ""

    if len(ne) == 1:
        val = ne[0].lower()
        if any(val.startswith(p) for p in _NOTE_PREFIXES):
            return RowKind.NOTE, f"Metadata row: \"{ne[0][:60]}\""
        if re.match(r"^[=\-\*]{3,}", val):
            return RowKind.NOTE, f"Section divider: \"{ne[0][:40]}\""
        # Single-cell row with report-like content
        if len(ne[0]) > 20 and not _is_numeric(ne[0]):
            return RowKind.NOTE, f"Title/header row: \"{ne[0][:60]}\""

    # ── Note prefix on first non-empty cell ──
    if any(first_cell.startswith(p) for p in _NOTE_PREFIXES):
        return RowKind.NOTE, f"Note row: \"{cells[0].strip()[:60]}\""

    # ── Subtotal ──
    for cell in cells:
        if _cell_matches_subtotal(cell):
            return RowKind.SUBTOTAL, f"Subtotal-like row — cell: \"{cell.strip()[:40]}\""

    # ── Repeated header ──
    ne_lower = [c.strip().lower() for c in ne]
    matching_headers = sum(1 for c in ne_lower if c in header_cells_lower)
    if header_col_count > 0 and matching_headers >= min(3, header_col_count // 2):
        return RowKind.HEADER_REPEAT, f"Repeated header row ({matching_headers} matching columns)"

    # ── Malformed (wrong column count) ──
    if header_col_count > 0:
        col_diff = abs(len(cells) - header_col_count)
        # Allow up to 2 extra/missing columns (trailing commas, minor issues)
        threshold = max(3, int(header_col_count * 0.3))
        if col_diff > threshold:
            return (
                RowKind.MALFORMED,
                f"Expected {header_col_count} columns, got {len(cells)}",
            )

    return RowKind.DATA, ""


# ── Missing value detection ───────────────────────────────────────────────────


def _find_missing_values(
    row: RawRow,
    header_row: list[str],
) -> MissingValueWarning | None:
    """Return a MissingValueWarning if any cell in a DATA row is empty."""
    empty_indices = [i for i, c in enumerate(row.cells) if not c.strip()]
    if not empty_indices:
        return None

    # Only report columns that exist in the header
    empty_cols = [
        header_row[i] if i < len(header_row) else f"col_{i}"
        for i in empty_indices
    ]
    return MissingValueWarning(
        line_number=row.line_number,
        empty_columns=empty_cols,
        empty_indices=empty_indices,
    )


# ── Date column detection ─────────────────────────────────────────────────────


def _detect_date_column(header_row: list[str]) -> tuple[bool, Optional[str]]:
    """Check if any header cell matches a known date column name."""
    for col in header_row:
        if col.strip().lower() in _DATE_COLUMN_NAMES:
            return True, col.strip()
    return False, None


# ── Main entry point ──────────────────────────────────────────────────────────


def load_raw(path: Path) -> InspectResult:
    """
    Safely load a CSV file.

    - Detects encoding via chardet
    - Reads all cells as raw strings (no type coercion)
    - Finds the header row
    - Classifies every subsequent row
    - Collects warnings without crashing

    Raises FileNotFoundError if path does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    file_size = path.stat().st_size
    encoding, enc_confidence = detect_encoding(path)

    # Read raw text — fall back to latin-1 if UTF-8 fails
    try:
        raw_text = path.read_text(encoding=encoding, errors="replace")
    except (UnicodeDecodeError, LookupError):
        raw_text = path.read_text(encoding="latin-1", errors="replace")
        encoding = "latin-1"
        enc_confidence = 0.5

    # Parse with csv.reader — gives us list[list[str]], no coercion
    reader = csv.reader(io.StringIO(raw_text))
    all_rows: list[list[str]] = list(reader)

    # Locate the header row
    header_result = _find_header(all_rows)
    if header_result is None:
        # Fallback: treat row 0 as header even if it doesn't look like one
        header_idx, header_cells = 0, all_rows[0] if all_rows else []
    else:
        header_idx, header_cells = header_result

    # Strip whitespace from header cells
    header_cells = [c.strip() for c in header_cells]
    header_col_count = len(header_cells)
    header_cells_lower = [c.lower() for c in header_cells]

    warnings: list[RowWarning] = []
    missing_value_warnings: list[MissingValueWarning] = []
    rows: list[RawRow] = []

    # Classify all rows AFTER the header line
    for raw_idx, cells in enumerate(all_rows):
        line_number = raw_idx + 1  # 1-based

        if raw_idx <= header_idx:
            # Rows before the header (including header itself) are already handled
            if raw_idx < header_idx:
                # Pre-header rows are always notes/metadata
                ne = _non_empty(cells)
                msg = f"Pre-header metadata: \"{cells[0].strip()[:60]}\"" if ne else "Empty pre-header row"
                kind = RowKind.NOTE if ne else RowKind.BLANK
                w = RowWarning(line_number=line_number, kind=kind, message=msg)
                row = RawRow(line_number=line_number, kind=kind, cells=cells, warning=w)
                rows.append(row)
                warnings.append(w)
            continue  # skip the header row itself in the rows list

        # Strip cell whitespace but preserve exact values
        stripped_cells = [c.strip() for c in cells]
        kind, reason = _classify_row(stripped_cells, header_col_count, header_cells_lower)

        w: RowWarning | None = None
        if kind != RowKind.DATA:
            w = RowWarning(line_number=line_number, kind=kind, message=reason)
            warnings.append(w)

        row = RawRow(line_number=line_number, kind=kind, cells=stripped_cells, warning=w)
        rows.append(row)

        # Check for missing values in data rows
        if kind == RowKind.DATA:
            mv = _find_missing_values(row, header_cells)
            if mv:
                missing_value_warnings.append(mv)

    has_date_col, date_col_name = _detect_date_column(header_cells)

    return InspectResult(
        path=path,
        file_size_bytes=file_size,
        encoding=encoding,
        encoding_confidence=enc_confidence,
        header_line_number=header_idx + 1,
        header_row=header_cells,
        rows=rows,
        warnings=warnings,
        missing_value_warnings=missing_value_warnings,
        has_date_column=has_date_col,
        date_column_name=date_col_name,
    )
