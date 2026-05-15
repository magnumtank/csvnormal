"""
Deterministic normalization — applies AI mappings to raw table rows.

Design rules:
  - Pure transformation: no AI calls, no file I/O (caller handles that).
  - Numeric values are NEVER cast, computed, or reformatted — exact strings only.
  - Missing canonical columns output as empty strings; they are never omitted.
  - __skip__ columns are silently dropped; __unknown__ columns are warned + dropped.
  - Platform column is resolved via MappingResult.get_platform() then config aliases.
  - Date fields are filled from DateResolution when the CSV has no date column.
  - Output column order follows CANONICAL_FIELDS exactly.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from csvnormal import logger as log
from csvnormal.ai_mapper import MappingResult
from csvnormal.config import (
    CANONICAL_FIELDS,
    DEFAULT_CURRENCY,
    FIELD_GROUPS,
)
from csvnormal.date_resolver import DateResolution
from csvnormal.table_detector import DetectedTable

# Fields whose values must never be touched regardless of what they contain
_PRESERVE = frozenset(CANONICAL_FIELDS)  # all canonical fields are preserved as-is


# ── Output models ─────────────────────────────────────────────────────────────


class NormalizedRow(BaseModel):
    """One output row — values keyed by canonical field name."""

    line_number: int               # source line number for traceability
    data: dict[str, str]          # canonical_field → exact string value


class NormalizationWarning(BaseModel):
    kind: str    # "unknown_column" | "missing_required" | "platform_unresolved"
    message: str
    source_column: Optional[str] = None
    line_number: Optional[int] = None


class NormalizeResult(BaseModel):
    """Full output of normalizing one DetectedTable."""

    model_config = {"arbitrary_types_allowed": True}

    # Provenance
    source_file: str
    table_index: int
    section_title: Optional[str]
    row_count: int
    column_count: int           # number of canonical columns present (non-empty)

    # The normalized rows
    rows: list[NormalizedRow]

    # Audit
    canonical_columns_used: list[str]    # canonical fields that had any data
    skipped_source_columns: list[str]    # __skip__
    unknown_source_columns: list[str]    # __unknown__
    date_resolution_source: str          # "csv_column" | "filename" | "user_prompt" | "none"
    client_name: str                     # as set by caller
    warnings: list[NormalizationWarning]

    def to_csv_string(self) -> str:
        """Render all rows as a CSV string (header + data)."""
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=CANONICAL_FIELDS,
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in self.rows:
            # Fill missing canonical fields with empty string
            full_row = {f: row.data.get(f, "") for f in CANONICAL_FIELDS}
            writer.writerow(full_row)
        return buf.getvalue()

    def to_audit_dict(self) -> dict:
        return self.model_dump(mode="json")


# ── Core transformation ───────────────────────────────────────────────────────


def normalize_table(
    table: DetectedTable,
    mapping: MappingResult,
    date_resolution: DateResolution | None = None,
    client_name: str = "",
) -> NormalizeResult:
    """
    Apply column mappings to a DetectedTable and produce canonical rows.

    Args:
        table:           detected table (header + data rows)
        mapping:         AI mapping result for this table
        date_resolution: resolved date info (may be None if CSV has date column)
        client_name:     overriding client name (from CLI flag or filename)

    Returns NormalizeResult with canonical rows + audit metadata.
    """
    warnings: list[NormalizationWarning] = []

    # ── Build the source→canonical column map ─────────────────────────────────
    # Map source column index → canonical field name (or None to drop)
    col_map: dict[int, str | None] = {}   # src_idx → canonical | None (drop)

    for idx, src_col in enumerate(table.header_row):
        canonical = mapping.get_canonical(src_col)   # None for skip/unknown
        if canonical is not None:
            col_map[idx] = canonical
        else:
            entry = mapping.column_mapping.get(src_col)
            if entry and entry.canonical == "__unknown__":
                warnings.append(NormalizationWarning(
                    kind="unknown_column",
                    message=f"Column '{src_col}' was not recognized — dropped from output",
                    source_column=src_col,
                ))
            col_map[idx] = None  # drop (skip or unknown)

    # ── Determine date-fill strategy ──────────────────────────────────────────
    # If the CSV has a date column mapped, we read from source rows.
    # Otherwise we fill from DateResolution.
    mapped_to_date = {v: k for k, v in col_map.items() if v == "date"}
    has_source_date_col = bool(mapped_to_date)

    date_fill = _build_date_fill(date_resolution, has_source_date_col)

    # ── Find platform source column (if any) ──────────────────────────────────
    platform_src_indices: list[int] = [
        idx for idx, canon in col_map.items() if canon == "platform"
    ]

    # ── Transform rows ────────────────────────────────────────────────────────
    normalized_rows: list[NormalizedRow] = []

    for raw_row in table.data_rows:
        row_data: dict[str, str] = {}

        # Copy mapped source columns (exact string — no coercion)
        for src_idx, canon in col_map.items():
            if canon is None:
                continue
            value = raw_row.cells[src_idx] if src_idx < len(raw_row.cells) else ""
            # Last-write-wins for duplicate source→same canonical
            if canon not in row_data or value:
                row_data[canon] = value

        # Resolve platform value
        if platform_src_indices:
            src_idx = platform_src_indices[0]
            raw_platform = raw_row.cells[src_idx] if src_idx < len(raw_row.cells) else ""
            resolved = mapping.get_platform(raw_platform) if raw_platform else None
            if resolved:
                row_data["platform"] = resolved
            else:
                warnings.append(NormalizationWarning(
                    kind="platform_unresolved",
                    message=f"Platform value '{raw_platform}' could not be normalized — kept as-is",
                    source_column=table.header_row[src_idx] if src_idx < len(table.header_row) else "",
                    line_number=raw_row.line_number,
                ))
                row_data["platform"] = raw_platform  # keep raw rather than blank

        # Fill date fields from DateResolution (only when no source date column)
        for field, value in date_fill.items():
            if field not in row_data:
                row_data[field] = value

        # Override/fill client name
        if client_name:
            row_data["client"] = client_name
        elif "client" not in row_data:
            row_data["client"] = ""

        # Fill currency default if not mapped from source
        if "currency" not in row_data or not row_data["currency"]:
            row_data["currency"] = DEFAULT_CURRENCY

        normalized_rows.append(NormalizedRow(
            line_number=raw_row.line_number,
            data=row_data,
        ))

    # ── Audit metadata ────────────────────────────────────────────────────────
    canonical_used: set[str] = set()
    for row in normalized_rows:
        for field, val in row.data.items():
            if val:
                canonical_used.add(field)

    return NormalizeResult(
        source_file=mapping.source_file,
        table_index=table.table_index,
        section_title=table.section_title,
        row_count=len(normalized_rows),
        column_count=len(canonical_used),
        rows=normalized_rows,
        canonical_columns_used=sorted(canonical_used, key=lambda f: CANONICAL_FIELDS.index(f) if f in CANONICAL_FIELDS else 999),
        skipped_source_columns=mapping.skipped_columns,
        unknown_source_columns=mapping.unknown_columns,
        date_resolution_source=date_resolution.source if date_resolution else "none",
        client_name=client_name,
        warnings=warnings,
    )


# ── Date fill helper ──────────────────────────────────────────────────────────


def _build_date_fill(
    dr: DateResolution | None,
    has_source_date_col: bool,
) -> dict[str, str]:
    """
    Build a dict of canonical date fields → fill values from a DateResolution.
    Returns empty dict if the CSV already has a date column or no resolution exists.
    """
    if has_source_date_col or dr is None or not dr.has_dates:
        return {}

    fill: dict[str, str] = {}

    if dr.start_date:
        fill["start_date"] = dr.start_date.isoformat()
    if dr.end_date:
        fill["end_date"] = dr.end_date.isoformat()
    if dr.month and dr.year:
        import calendar
        fill["month"] = f"{calendar.month_name[dr.month]} {dr.year}"
        fill["year"] = str(dr.year)
    elif dr.month:
        import calendar
        fill["month"] = calendar.month_name[dr.month]
    if dr.year:
        fill["year"] = str(dr.year)

    return fill


# ── File writer ───────────────────────────────────────────────────────────────


def write_canonical_csv(result: NormalizeResult, output_path: Path) -> None:
    """Write the canonical CSV to disk. Creates parent dirs if needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.to_csv_string(), encoding="utf-8")
    log.success(f"Canonical CSV written → [bold]{output_path}[/bold]")


def write_audit_json(result: NormalizeResult, mapping: MappingResult, output_path: Path) -> None:
    """Write the audit JSON sidecar (normalize result + mapping provenance)."""
    import json

    output_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "normalize": result.to_audit_dict(),
        "mapping": mapping.to_audit_dict(),
    }
    output_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    log.success(f"Audit JSON written    → [bold]{output_path}[/bold]")
