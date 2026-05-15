"""
Validation engine — verifies the integrity of a canonical CSV output.

Checks performed (in order):
  1. row_count         — normalized row count matches source (needs audit sidecar)
  2. required_fields   — client, platform, spend non-empty in every row
  3. numeric_values    — every non-empty numeric/rate field parses as a float
  4. no_duplicates     — no two rows share the same (platform, campaign, ad_set, date/start_date)
  5. date_ordering     — start_date <= end_date when both present
  6. value_ranges      — spend >= 0; CTR, CVR, VTR, engagement_rate in [0, 100]
  7. currency_consistency — all rows carry the same currency code
  8. schema_coverage   — informational: which canonical fields are entirely blank

Severity levels:
  error   → blocks downstream use; exit code 1
  warning → suspicious but not blocking
  info    → informational, always passes

Design rules:
  - Pure functions only — reads rows already in memory, no file I/O.
  - Never re-normalizes or modifies values.
  - All checks return ValidationCheck objects; no print statements here.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from csvnormal.config import ALL_PRESERVE_FIELDS, CANONICAL_FIELDS, REQUIRED_FIELDS

# ── Known non-numeric placeholders — these are valid in numeric fields ─────────

_NON_NUMERIC_OK = frozenset({
    "", "n/a", "na", "-", "—", "–", "null", "none", "0",
})

# ── Severity ──────────────────────────────────────────────────────────────────


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# ── Models ────────────────────────────────────────────────────────────────────


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    severity: Severity
    message: str
    details: list[str] = []   # up to 10 sample problem details
    affected_rows: int = 0


class ValidationReport(BaseModel):
    source_file: str
    canonical_row_count: int
    source_row_count: Optional[int] = None   # from audit sidecar if available
    checks: list[ValidationCheck]

    @property
    def passed(self) -> bool:
        """True only when no ERROR-severity check has failed."""
        return all(c.passed or c.severity != Severity.ERROR for c in self.checks)

    @property
    def error_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == Severity.WARNING)


# ── Row loader ─────────────────────────────────────────────────────────────────


def load_canonical_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """
    Read a canonical CSV into (header, rows).
    Rows are dicts keyed by canonical field name.
    Raises ValueError if the header is missing or malformed.
    """
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header row")
    header = list(reader.fieldnames)
    rows = [dict(r) for r in reader]
    return header, rows


# ── Individual checks ─────────────────────────────────────────────────────────


def check_row_count(
    canonical_rows: int,
    source_rows: int,
) -> ValidationCheck:
    passed = canonical_rows == source_rows
    return ValidationCheck(
        name="row_count",
        passed=passed,
        severity=Severity.ERROR,
        message=(
            f"Row count matches: {canonical_rows} rows"
            if passed
            else f"Row count mismatch: canonical has {canonical_rows}, source had {source_rows}"
        ),
        affected_rows=0 if passed else abs(canonical_rows - source_rows),
    )


def check_required_fields(
    rows: list[dict[str, str]],
    required: list[str] = REQUIRED_FIELDS,
) -> ValidationCheck:
    bad: list[str] = []
    for i, row in enumerate(rows, start=2):   # 2 = first data line (line 1 is header)
        for field in required:
            if not row.get(field, "").strip():
                bad.append(f"Row {i}: '{field}' is empty")
                if len(bad) >= 10:
                    break
        if len(bad) >= 10:
            break

    passed = len(bad) == 0
    return ValidationCheck(
        name="required_fields",
        passed=passed,
        severity=Severity.ERROR,
        message=(
            "All required fields present"
            if passed
            else f"Required fields missing in {len(bad)} instance(s)"
        ),
        details=bad[:10],
        affected_rows=len(bad),
    )


def check_numeric_values(
    rows: list[dict[str, str]],
    numeric_fields: list[str] = ALL_PRESERVE_FIELDS,
) -> ValidationCheck:
    bad: list[str] = []
    for i, row in enumerate(rows, start=2):
        for field in numeric_fields:
            val = row.get(field, "").strip()
            if not val or val.lower() in _NON_NUMERIC_OK:
                continue
            try:
                float(val)
            except ValueError:
                bad.append(f"Row {i}, '{field}': non-numeric value '{val}'")
                if len(bad) >= 10:
                    break
        if len(bad) >= 10:
            break

    passed = len(bad) == 0
    return ValidationCheck(
        name="numeric_values",
        passed=passed,
        severity=Severity.ERROR,
        message=(
            "All numeric fields contain valid numbers"
            if passed
            else f"Non-numeric values found in {len(bad)} instance(s)"
        ),
        details=bad[:10],
        affected_rows=len(bad),
    )


def check_no_duplicates(
    rows: list[dict[str, str]],
    key_fields: tuple[str, ...] = ("platform", "campaign", "ad_set", "date", "start_date"),
) -> ValidationCheck:
    seen: dict[tuple[str, ...], int] = {}
    dupes: list[str] = []

    for i, row in enumerate(rows, start=2):
        key = tuple(row.get(f, "").strip() for f in key_fields)
        if all(v == "" for v in key):
            continue   # blank key — can't meaningfully deduplicate
        if key in seen:
            dupes.append(
                f"Row {i} duplicates row {seen[key]}: "
                + ", ".join(f"{k}={v}" for k, v in zip(key_fields, key) if v)
            )
            if len(dupes) >= 10:
                break
        else:
            seen[key] = i

    passed = len(dupes) == 0
    return ValidationCheck(
        name="no_duplicates",
        passed=passed,
        severity=Severity.WARNING,
        message=(
            "No duplicate rows detected"
            if passed
            else f"{len(dupes)} duplicate key(s) found"
        ),
        details=dupes[:10],
        affected_rows=len(dupes),
    )


def check_date_ordering(rows: list[dict[str, str]]) -> ValidationCheck:
    bad: list[str] = []
    for i, row in enumerate(rows, start=2):
        s = row.get("start_date", "").strip()
        e = row.get("end_date", "").strip()
        if not s or not e:
            continue
        try:
            if date.fromisoformat(s) > date.fromisoformat(e):
                bad.append(f"Row {i}: start_date {s} > end_date {e}")
                if len(bad) >= 10:
                    break
        except ValueError:
            bad.append(f"Row {i}: unparseable date — start='{s}', end='{e}'")
            if len(bad) >= 10:
                break

    passed = len(bad) == 0
    return ValidationCheck(
        name="date_ordering",
        passed=passed,
        severity=Severity.ERROR,
        message=(
            "All date ranges are valid"
            if passed
            else f"Date ordering errors in {len(bad)} row(s)"
        ),
        details=bad[:10],
        affected_rows=len(bad),
    )


def check_value_ranges(rows: list[dict[str, str]]) -> ValidationCheck:
    """
    Warn when values fall outside expected bounds:
      - spend, budget, revenue, conversion_value: must be >= 0
      - ctr, cvr, vtr, engagement_rate: must be in [0, 100]
    """
    bad: list[str] = []

    non_negative = {"spend", "budget", "revenue", "conversion_value",
                    "impressions", "reach", "clicks", "link_clicks",
                    "conversions", "leads", "purchases", "app_installs"}
    pct_fields = {"ctr", "cvr", "vtr", "engagement_rate"}

    for i, row in enumerate(rows, start=2):
        for field in non_negative:
            val = row.get(field, "").strip()
            if not val or val.lower() in _NON_NUMERIC_OK:
                continue
            try:
                if float(val) < 0:
                    bad.append(f"Row {i}, '{field}': negative value '{val}'")
            except ValueError:
                pass
            if len(bad) >= 10:
                break

        for field in pct_fields:
            val = row.get(field, "").strip()
            if not val or val.lower() in _NON_NUMERIC_OK:
                continue
            try:
                v = float(val)
                if not (0 <= v <= 100):
                    bad.append(f"Row {i}, '{field}': {val} outside [0, 100]")
            except ValueError:
                pass
            if len(bad) >= 10:
                break

        if len(bad) >= 10:
            break

    passed = len(bad) == 0
    return ValidationCheck(
        name="value_ranges",
        passed=passed,
        severity=Severity.WARNING,
        message=(
            "All values within expected ranges"
            if passed
            else f"Out-of-range values in {len(bad)} instance(s)"
        ),
        details=bad[:10],
        affected_rows=len(bad),
    )


def check_currency_consistency(rows: list[dict[str, str]]) -> ValidationCheck:
    currencies: set[str] = set()
    for row in rows:
        c = row.get("currency", "").strip()
        if c:
            currencies.add(c)

    passed = len(currencies) <= 1
    return ValidationCheck(
        name="currency_consistency",
        passed=passed,
        severity=Severity.WARNING,
        message=(
            f"Consistent currency: {next(iter(currencies), 'none')}"
            if passed
            else f"Mixed currencies found: {sorted(currencies)}"
        ),
        affected_rows=0,
    )


def check_schema_coverage(rows: list[dict[str, str]]) -> ValidationCheck:
    """Informational: report canonical fields that are 100% blank."""
    blank_fields: list[str] = []
    for field in CANONICAL_FIELDS:
        if all(not r.get(field, "").strip() for r in rows):
            blank_fields.append(field)

    # Identity + spend always expected — flag those specifically
    expected = {"client", "platform", "spend"}
    missing_expected = expected & set(blank_fields)

    passed = True  # informational — never fails
    msg = (
        f"{len(blank_fields)} canonical fields are entirely blank"
        if blank_fields
        else "All canonical fields have at least one value"
    )

    return ValidationCheck(
        name="schema_coverage",
        passed=passed,
        severity=Severity.INFO,
        message=msg,
        details=blank_fields,
        affected_rows=0,
    )


# ── Public entry point ────────────────────────────────────────────────────────


def run_all_checks(
    rows: list[dict[str, str]],
    source_file: str,
    source_row_count: Optional[int] = None,
) -> ValidationReport:
    """
    Run all validation checks against a list of canonical rows.

    Args:
        rows:             list of dicts keyed by canonical field name
        source_file:      filename for display purposes
        source_row_count: if known (from audit JSON), enables row_count check

    Returns a ValidationReport with one ValidationCheck per check.
    """
    checks: list[ValidationCheck] = []

    if source_row_count is not None:
        checks.append(check_row_count(len(rows), source_row_count))

    checks.append(check_required_fields(rows))
    checks.append(check_numeric_values(rows))
    checks.append(check_no_duplicates(rows))
    checks.append(check_date_ordering(rows))
    checks.append(check_value_ranges(rows))
    checks.append(check_currency_consistency(rows))
    checks.append(check_schema_coverage(rows))

    return ValidationReport(
        source_file=source_file,
        canonical_row_count=len(rows),
        source_row_count=source_row_count,
        checks=checks,
    )
