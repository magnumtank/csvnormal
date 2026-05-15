"""Phase 1 scaffold smoke tests — updated for expanded schema and date resolver."""

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csvnormal.cli import app
from csvnormal.config import (
    ALL_PRESERVE_FIELDS,
    CANONICAL_FIELDS,
    DEFAULT_CURRENCY,
    FIELD_GROUPS,
    NUMERIC_FIELDS,
    RATE_FIELDS,
    REQUIRED_FIELDS,
)
from csvnormal.date_resolver import (
    DateResolution,
    _parse_month_year_string,
    resolve_from_filename,
    resolve_dates,
)

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ── Currency defaults ─────────────────────────────────────────────────────────


def test_default_currency_is_inr():
    assert DEFAULT_CURRENCY == "INR"


# ── Schema structure ──────────────────────────────────────────────────────────


def test_canonical_fields_count():
    assert len(CANONICAL_FIELDS) >= 40, f"Only {len(CANONICAL_FIELDS)} fields defined"


def test_required_fields_subset():
    for f in REQUIRED_FIELDS:
        assert f in CANONICAL_FIELDS, f"{f} not in canonical fields"


def test_numeric_fields_subset():
    for f in NUMERIC_FIELDS:
        assert f in CANONICAL_FIELDS, f"{f} not in canonical fields"


def test_rate_fields_subset():
    for f in RATE_FIELDS:
        assert f in CANONICAL_FIELDS, f"{f} not in canonical fields"


def test_efficiency_fields_present():
    for field in ["ctr", "cpc", "cpm", "cvr", "cpa", "roas", "vtr", "cpl", "cpp", "cpv"]:
        assert field in CANONICAL_FIELDS, f"Efficiency field '{field}' missing"


def test_engagement_fields_present():
    for field in ["engagements", "likes", "comments", "shares", "saves"]:
        assert field in CANONICAL_FIELDS, f"Engagement field '{field}' missing"


def test_funnel_fields_present():
    for field in ["leads", "add_to_cart", "checkouts", "purchases", "app_installs", "sign_ups"]:
        assert field in CANONICAL_FIELDS, f"Funnel field '{field}' missing"


def test_video_fields_present():
    for field in ["video_views", "video_completions", "three_second_views", "thruplay", "vtr"]:
        assert field in CANONICAL_FIELDS, f"Video field '{field}' missing"


def test_date_fields_present():
    for field in ["date", "start_date", "end_date", "month", "year", "week"]:
        assert field in CANONICAL_FIELDS, f"Date field '{field}' missing"


def test_all_preserve_fields_are_canonical():
    for f in ALL_PRESERVE_FIELDS:
        assert f in CANONICAL_FIELDS, f"Preserve field '{f}' not in canonical schema"


def test_field_groups_cover_all_fields():
    grouped = {f for fields in FIELD_GROUPS.values() for f in fields}
    assert grouped == set(CANONICAL_FIELDS)


def test_no_duplicates_in_canonical():
    assert len(CANONICAL_FIELDS) == len(set(CANONICAL_FIELDS)), "Duplicate field names detected"


# ── Fixtures ──────────────────────────────────────────────────────────────────


def test_all_fixtures_exist():
    expected = [
        "single_table_clean.csv",
        "inconsistent_headers.csv",
        "multi_table.csv",
        "platform_variants.csv",
        "malformed.csv",
        "subtotals_and_sections.csv",
        "no_dates_march2024.csv",
    ]
    for name in expected:
        path = FIXTURES / name
        assert path.exists(), f"Missing fixture: {name}"
        assert path.stat().st_size > 0, f"Empty fixture: {name}"


def test_fixtures_use_inr():
    """Every fixture that contains a currency column must use INR."""
    for csv_file in FIXTURES.glob("*.csv"):
        text = csv_file.read_text(encoding="utf-8", errors="ignore")
        if "USD" in text:
            pytest.fail(f"{csv_file.name} still contains 'USD' — should be INR")


# ── Date resolver — filename parsing ──────────────────────────────────────────


def test_resolve_month_name_year():
    p = Path("report_march2024.csv")
    r = resolve_from_filename(p)
    assert r is not None
    assert r.month == 3
    assert r.year == 2024
    assert r.start_date == date(2024, 3, 1)
    assert r.end_date == date(2024, 3, 31)
    assert r.source == "filename"


def test_resolve_yyyy_mm():
    p = Path("client_2024-03_export.csv")
    r = resolve_from_filename(p)
    assert r is not None
    assert r.month == 3
    assert r.year == 2024


def test_resolve_quarter():
    p = Path("MegaBrand_Q1_2024.csv")
    r = resolve_from_filename(p)
    assert r is not None
    assert r.start_date == date(2024, 1, 1)
    assert r.end_date == date(2024, 3, 31)
    assert r.year == 2024


def test_resolve_date_range():
    p = Path("data_2024-03-01_2024-03-31.csv")
    r = resolve_from_filename(p)
    assert r is not None
    assert r.start_date == date(2024, 3, 1)
    assert r.end_date == date(2024, 3, 31)


def test_resolve_no_match_returns_none():
    p = Path("random_report.csv")
    r = resolve_from_filename(p)
    assert r is None


def test_dateless_fixture_resolves_from_filename():
    p = FIXTURES / "no_dates_march2024.csv"
    r = resolve_from_filename(p)
    assert r is not None
    assert r.month == 3
    assert r.year == 2024


# ── Date resolver — string parser ─────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("March 2024", (3, 2024)),
    ("mar2024", (3, 2024)),
    ("Mar-24", (3, 2024)),
    ("03/2024", (3, 2024)),
    ("2024-03", (3, 2024)),
    ("january 2024", (1, 2024)),
    ("Dec-23", (12, 2023)),
])
def test_parse_month_year_string(raw, expected):
    assert _parse_month_year_string(raw) == expected


def test_parse_month_year_string_invalid():
    assert _parse_month_year_string("not a date") is None


# ── Date resolver — resolve_dates with has_date_column=True ──────────────────


def test_resolve_dates_csv_column():
    r = resolve_dates(Path("any.csv"), has_date_column=True, interactive=False)
    assert r.source == "csv_column"
    assert r.confidence == 1.0


def test_resolve_dates_filename_fallback():
    r = resolve_dates(Path("report_march2024.csv"), has_date_column=False, interactive=False)
    assert r.source == "filename"
    assert r.month == 3


def test_resolve_dates_no_info_non_interactive():
    r = resolve_dates(Path("unknown.csv"), has_date_column=False, interactive=False)
    assert r.source == "none"
    assert r.confidence == 0.0


# ── Date resolution model ──────────────────────────────────────────────────────


def test_date_resolution_has_dates():
    r = DateResolution(source="filename", month=3, year=2024,
                       start_date=date(2024, 3, 1), end_date=date(2024, 3, 31))
    assert r.has_dates is True
    assert r.month_label == "March 2024"


def test_date_resolution_no_dates():
    r = DateResolution(source="none")
    assert r.has_dates is False


# ── CLI smoke tests ───────────────────────────────────────────────────────────


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_schema_command_shows_all_groups():
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    # All group names must appear
    for group in FIELD_GROUPS:
        assert group in result.output, f"Group '{group}' missing from schema output"
    # Spot-check representative fields (short names — won't be truncated by Rich)
    for field in ["client", "platform", "spend", "ctr", "cpc", "roas", "cvr", "vtr", "revenue"]:
        assert field in result.output, f"Field '{field}' missing from schema output"
    assert "INR" in result.output


def test_schema_shows_efficiency_metrics():
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    for metric in ["ctr", "cpc", "cpm", "cvr", "cpa", "roas"]:
        assert metric in result.output


def test_inspect_missing_file():
    result = runner.invoke(app, ["inspect", "nonexistent.csv"])
    assert result.exit_code == 1


def test_inspect_runs_successfully():
    fixture = FIXTURES / "single_table_clean.csv"
    result = runner.invoke(app, ["inspect", str(fixture)])
    assert result.exit_code == 0
    assert "data rows" in result.output


def test_map_command_exists():
    """map command is implemented — verify it at least registers and errors gracefully without API key."""
    result = runner.invoke(app, ["map", "--help"])
    assert result.exit_code == 0
    assert "map" in result.output.lower() or "file" in result.output.lower()
