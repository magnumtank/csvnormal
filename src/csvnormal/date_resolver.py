"""
Date resolution for CSVs that lack a date column.

Resolution priority:
  1. csv_column  — date column already present in the CSV (handled upstream)
  2. filename    — extract month/period from the file name using regex
  3. user_prompt — ask the user interactively for start/end dates or month(s)
  4. none        — user explicitly skipped; date fields left blank

The module never modifies numeric values and never infers or fabricates dates.
It only resolves period metadata that was already implicitly known (filename)
or explicitly supplied (user input).
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import typer
from pydantic import BaseModel

from csvnormal import logger as log

# ── Month name → number ───────────────────────────────────────────────────────

_MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_MONTH_ABBR = "|".join(sorted(_MONTH_MAP.keys(), key=len, reverse=True))

# ── Filename date patterns ────────────────────────────────────────────────────
# Each tuple: (compiled regex, extractor function → (start_date, end_date) | None)

def _month_start_end(month: int, year: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _quarter_start_end(q: int, year: int) -> tuple[date, date]:
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, start_month, 1), date(year, end_month, last_day)


# Pattern: 2024-03  or  2024_03
_RE_YYYY_MM = re.compile(r"(\d{4})[-_](\d{1,2})")

# Pattern: march_2024  or  march-2024  or  march2024  or  march 2024  or  Mar-24
_RE_MONTHNAME_YEAR = re.compile(
    rf"({_MONTH_ABBR})[-_ ]?(\d{{2,4}})", re.IGNORECASE
)

# Pattern: Q1_2024  or  Q1-2024  or  Q12024
_RE_QUARTER = re.compile(r"[Qq]([1-4])[-_]?(\d{4})")

# Pattern: 2024-03-01_2024-03-31  or  2024-03-01-2024-03-31
_RE_DATE_RANGE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[_\-to]+(\d{4}-\d{2}-\d{2})"
)

# Pattern: FY2024  or  FY24
_RE_FY = re.compile(r"FY(\d{2,4})", re.IGNORECASE)

# ── DateResolution model ──────────────────────────────────────────────────────


class DateResolution(BaseModel):
    source: Literal["csv_column", "filename", "user_prompt", "none"]
    start_date: date | None = None
    end_date: date | None = None
    month: int | None = None
    year: int | None = None
    raw: str = ""
    confidence: float = 0.0

    @property
    def has_dates(self) -> bool:
        return self.start_date is not None or self.month is not None

    @property
    def month_label(self) -> str:
        if self.month and self.year:
            return f"{calendar.month_name[self.month]} {self.year}"
        if self.month:
            return calendar.month_name[self.month]
        if self.start_date and self.end_date:
            return f"{self.start_date.isoformat()} → {self.end_date.isoformat()}"
        return ""


# ── Filename parser ───────────────────────────────────────────────────────────


def resolve_from_filename(path: Path) -> DateResolution | None:
    """Try to extract a date period from the file name. Returns None if nothing matches."""
    stem = path.stem  # filename without extension

    # Explicit date range: highest confidence
    m = _RE_DATE_RANGE.search(stem)
    if m:
        try:
            s = date.fromisoformat(m.group(1))
            e = date.fromisoformat(m.group(2))
            return DateResolution(
                source="filename",
                start_date=s,
                end_date=e,
                month=s.month if s.month == e.month else None,
                year=s.year,
                raw=m.group(0),
                confidence=0.95,
            )
        except ValueError:
            pass

    # Quarter: Q1_2024
    m = _RE_QUARTER.search(stem)
    if m:
        q, year = int(m.group(1)), int(m.group(2))
        s, e = _quarter_start_end(q, year)
        return DateResolution(
            source="filename",
            start_date=s,
            end_date=e,
            year=year,
            raw=m.group(0),
            confidence=0.9,
        )

    # Month name: march_2024
    m = _RE_MONTHNAME_YEAR.search(stem)
    if m:
        month_num = _MONTH_MAP.get(m.group(1).lower())
        year = int(m.group(2))
        if month_num:
            s, e = _month_start_end(month_num, year)
            return DateResolution(
                source="filename",
                start_date=s,
                end_date=e,
                month=month_num,
                year=year,
                raw=m.group(0),
                confidence=0.9,
            )

    # YYYY-MM: 2024-03
    m = _RE_YYYY_MM.search(stem)
    if m:
        year, month_num = int(m.group(1)), int(m.group(2))
        if 1 <= month_num <= 12:
            s, e = _month_start_end(month_num, year)
            return DateResolution(
                source="filename",
                start_date=s,
                end_date=e,
                month=month_num,
                year=year,
                raw=m.group(0),
                confidence=0.85,
            )

    return None


# ── Interactive user prompt ────────────────────────────────────────────────────


def prompt_user_for_dates(file_path: Path) -> DateResolution:
    """
    Interactively ask the user for date information.
    Called when the CSV has no date column and the filename yields nothing.
    """
    log.blank()
    log.warning(
        f"No date column detected in [bold]{file_path.name}[/bold] "
        "and could not infer period from filename."
    )
    log.info(
        "Please provide the reporting period. "
        "You can enter a month, a date range, or skip."
    )
    log.blank()

    log.console.print("  [bold cyan]Options:[/bold cyan]")
    log.console.print("    [bold]1[/bold]  Enter a single month  (e.g.  March 2024)")
    log.console.print("    [bold]2[/bold]  Enter a date range    (e.g.  2024-03-01  to  2024-03-31)")
    log.console.print("    [bold]3[/bold]  Enter multiple months (e.g.  Jan 2024, Feb 2024)")
    log.console.print("    [bold]s[/bold]  Skip — leave date fields blank")
    log.blank()

    choice = typer.prompt("Your choice", default="s").strip().lower()

    if choice == "1":
        return _prompt_single_month()
    elif choice == "2":
        return _prompt_date_range()
    elif choice == "3":
        return _prompt_multi_month()
    else:
        log.warning("Date fields will be left blank for this file.")
        return DateResolution(source="none", confidence=0.0)


def _prompt_single_month() -> DateResolution:
    raw = typer.prompt("Enter month and year (e.g. March 2024 or Mar-24)").strip()
    parsed = _parse_month_year_string(raw)
    if parsed:
        month_num, year = parsed
        s, e = _month_start_end(month_num, year)
        log.success(f"Resolved period: {s.isoformat()} → {e.isoformat()}")
        return DateResolution(
            source="user_prompt",
            start_date=s,
            end_date=e,
            month=month_num,
            year=year,
            raw=raw,
            confidence=1.0,
        )
    log.error(f"Could not parse '{raw}'. Leaving date fields blank.")
    return DateResolution(source="none", raw=raw, confidence=0.0)


def _prompt_date_range() -> DateResolution:
    start_raw = typer.prompt("Start date (YYYY-MM-DD)").strip()
    end_raw = typer.prompt("End date   (YYYY-MM-DD)").strip()
    try:
        s = date.fromisoformat(start_raw)
        e = date.fromisoformat(end_raw)
        if e < s:
            log.error("End date is before start date. Leaving date fields blank.")
            return DateResolution(source="none", confidence=0.0)
        log.success(f"Resolved period: {s.isoformat()} → {e.isoformat()}")
        return DateResolution(
            source="user_prompt",
            start_date=s,
            end_date=e,
            month=s.month if s.month == e.month else None,
            year=s.year,
            raw=f"{start_raw} to {end_raw}",
            confidence=1.0,
        )
    except ValueError as exc:
        log.error(f"Invalid date format: {exc}. Leaving date fields blank.")
        return DateResolution(source="none", confidence=0.0)


def _prompt_multi_month() -> DateResolution:
    """Accept comma-separated months; returns the overall start→end span."""
    raw = typer.prompt(
        "Enter months separated by commas (e.g. Jan 2024, Feb 2024, Mar 2024)"
    ).strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    dates: list[tuple[date, date]] = []
    for part in parts:
        parsed = _parse_month_year_string(part)
        if parsed:
            month_num, year = parsed
            dates.append(_month_start_end(month_num, year))
        else:
            log.warning(f"Could not parse '{part}' — skipping.")

    if not dates:
        log.error("No valid months found. Leaving date fields blank.")
        return DateResolution(source="none", confidence=0.0)

    overall_start = min(s for s, _ in dates)
    overall_end = max(e for _, e in dates)
    log.success(f"Resolved period: {overall_start.isoformat()} → {overall_end.isoformat()}")
    return DateResolution(
        source="user_prompt",
        start_date=overall_start,
        end_date=overall_end,
        year=overall_start.year,
        raw=raw,
        confidence=1.0,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_month_year_string(raw: str) -> tuple[int, int] | None:
    """Parse strings like 'March 2024', 'Mar-24', 'mar2024', '03/2024'."""
    raw = raw.strip()

    # Try month-name + year (handles: "March 2024", "Mar-24", "mar2024", "january 2024")
    m = _RE_MONTHNAME_YEAR.match(raw)
    if m:
        month_num = _MONTH_MAP.get(m.group(1).lower())
        year_raw = m.group(2)
        year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)
        if month_num:
            return month_num, year

    # Try MM/YYYY or MM-YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", raw)
    if m:
        month_num, year = int(m.group(1)), int(m.group(2))
        if 1 <= month_num <= 12:
            return month_num, year

    # Try YYYY-MM
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", raw)
    if m:
        year, month_num = int(m.group(1)), int(m.group(2))
        if 1 <= month_num <= 12:
            return month_num, year

    return None


# ── Top-level resolver ────────────────────────────────────────────────────────


def resolve_dates(
    file_path: Path,
    has_date_column: bool,
    interactive: bool = True,
) -> DateResolution:
    """
    Main entry point for date resolution.

    Args:
        file_path: the CSV being processed
        has_date_column: True if a date column was already found in the CSV
        interactive: if False, skip user prompting (useful in tests/pipelines)
    """
    if has_date_column:
        log.debug("Date column present in CSV — no period resolution needed.")
        return DateResolution(source="csv_column", confidence=1.0)

    log.debug(f"No date column detected. Trying filename: {file_path.name}")
    resolved = resolve_from_filename(file_path)

    if resolved:
        log.info(
            f"Date period resolved from filename: "
            f"[bold]{resolved.month_label}[/bold] "
            f"(confidence: {resolved.confidence:.0%})"
        )
        return resolved

    if interactive:
        return prompt_user_for_dates(file_path)

    log.warning("No date information found and interactive mode is off. Date fields will be blank.")
    return DateResolution(source="none", confidence=0.0)
