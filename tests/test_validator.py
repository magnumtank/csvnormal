"""
Tests for Phase 6: Validation Engine.

All tests are fully offline — no AI calls, no external I/O beyond tmp_path.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csvnormal.cli import app
from csvnormal.config import CANONICAL_FIELDS, DEFAULT_CURRENCY
from csvnormal.validator import (
    Severity,
    ValidationCheck,
    ValidationReport,
    check_currency_consistency,
    check_date_ordering,
    check_no_duplicates,
    check_numeric_values,
    check_required_fields,
    check_row_count,
    check_schema_coverage,
    check_value_ranges,
    load_canonical_csv,
    run_all_checks,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent / "fixtures"
runner = CliRunner()


def _make_rows(*rows: dict[str, str]) -> list[dict[str, str]]:
    """Build a list of canonical row dicts, filling missing fields with empty string."""
    return [{f: r.get(f, "") for f in CANONICAL_FIELDS} for r in rows]


def _good_row(**overrides: str) -> dict[str, str]:
    base = {
        "client": "Acme",
        "platform": "meta",
        "campaign": "Spring Sale",
        "date": "2024-03-01",
        "spend": "1000.00",
        "impressions": "50000",
        "clicks": "1200",
        "ctr": "2.40",
        "currency": "INR",
    }
    base.update(overrides)
    return {f: base.get(f, "") for f in CANONICAL_FIELDS}


def _write_canonical_csv(path: Path, rows: list[dict[str, str]]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CANONICAL_FIELDS, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({f: row.get(f, "") for f in CANONICAL_FIELDS})
    path.write_text(buf.getvalue(), encoding="utf-8")


# ── check_row_count ───────────────────────────────────────────────────────────

class TestRowCount:
    def test_passes_when_counts_match(self):
        c = check_row_count(10, 10)
        assert c.passed
        assert c.severity == Severity.ERROR

    def test_fails_when_counts_differ(self):
        c = check_row_count(9, 10)
        assert not c.passed
        assert c.severity == Severity.ERROR
        assert "9" in c.message and "10" in c.message

    def test_affected_rows_is_absolute_difference(self):
        c = check_row_count(7, 10)
        assert c.affected_rows == 3


# ── check_required_fields ─────────────────────────────────────────────────────

class TestRequiredFields:
    def test_passes_when_all_present(self):
        rows = _make_rows(_good_row())
        c = check_required_fields(rows)
        assert c.passed

    def test_fails_when_spend_missing(self):
        rows = _make_rows(_good_row(spend=""))
        c = check_required_fields(rows)
        assert not c.passed
        assert any("spend" in d for d in c.details)

    def test_fails_when_platform_missing(self):
        rows = _make_rows(_good_row(platform=""))
        c = check_required_fields(rows)
        assert not c.passed

    def test_fails_when_client_missing(self):
        rows = _make_rows(_good_row(client=""))
        c = check_required_fields(rows)
        assert not c.passed

    def test_multiple_bad_rows_reported(self):
        rows = _make_rows(
            _good_row(spend=""),
            _good_row(),
            _good_row(spend=""),
        )
        c = check_required_fields(rows)
        assert not c.passed
        assert c.affected_rows == 2

    def test_whitespace_only_counts_as_missing(self):
        rows = _make_rows(_good_row(spend="   "))
        c = check_required_fields(rows)
        assert not c.passed


# ── check_numeric_values ──────────────────────────────────────────────────────

class TestNumericValues:
    def test_passes_for_clean_numbers(self):
        rows = _make_rows(_good_row(spend="1000.00", ctr="2.40", cpm="127.55"))
        c = check_numeric_values(rows)
        assert c.passed

    def test_passes_for_empty_fields(self):
        rows = _make_rows(_good_row(spend="1000", impressions=""))
        c = check_numeric_values(rows)
        assert c.passed

    def test_passes_for_known_placeholders(self):
        """N/A, -, — are accepted in numeric fields."""
        rows = _make_rows(_good_row(spend="N/A", impressions="-"))
        c = check_numeric_values(rows)
        assert c.passed

    def test_fails_for_currency_symbol_not_stripped(self):
        """If normalizer left ₹ in the value, validation should catch it."""
        rows = _make_rows(_good_row(spend="₹1000"))
        c = check_numeric_values(rows)
        assert not c.passed

    def test_fails_for_percent_not_stripped(self):
        rows = _make_rows(_good_row(ctr="2.86%"))
        c = check_numeric_values(rows)
        assert not c.passed

    def test_fails_for_plain_text_in_numeric_field(self):
        rows = _make_rows(_good_row(impressions="lots"))
        c = check_numeric_values(rows)
        assert not c.passed
        assert any("impressions" in d for d in c.details)

    def test_negative_numbers_are_valid(self):
        """Negative values don't fail the numeric check (value_ranges catches that)."""
        rows = _make_rows(_good_row(spend="-100"))
        c = check_numeric_values(rows)
        assert c.passed

    def test_integer_values_pass(self):
        rows = _make_rows(_good_row(impressions="50000", clicks="1200"))
        c = check_numeric_values(rows)
        assert c.passed


# ── check_no_duplicates ───────────────────────────────────────────────────────

class TestNoDuplicates:
    def test_passes_with_unique_rows(self):
        rows = _make_rows(
            _good_row(date="2024-03-01"),
            _good_row(date="2024-03-02"),
        )
        c = check_no_duplicates(rows)
        assert c.passed

    def test_fails_with_identical_key_rows(self):
        rows = _make_rows(_good_row(), _good_row())
        c = check_no_duplicates(rows)
        assert not c.passed
        assert c.severity == Severity.WARNING

    def test_different_campaigns_are_not_duplicates(self):
        rows = _make_rows(
            _good_row(campaign="Campaign A"),
            _good_row(campaign="Campaign B"),
        )
        c = check_no_duplicates(rows)
        assert c.passed

    def test_blank_key_rows_skipped(self):
        """Rows where all key fields are blank don't trigger duplicate warning."""
        rows = _make_rows(
            {f: "" for f in CANONICAL_FIELDS},
            {f: "" for f in CANONICAL_FIELDS},
        )
        c = check_no_duplicates(rows)
        assert c.passed


# ── check_date_ordering ───────────────────────────────────────────────────────

class TestDateOrdering:
    def test_passes_when_start_before_end(self):
        rows = _make_rows(_good_row(start_date="2024-03-01", end_date="2024-03-31"))
        c = check_date_ordering(rows)
        assert c.passed

    def test_passes_when_same_day(self):
        rows = _make_rows(_good_row(start_date="2024-03-15", end_date="2024-03-15"))
        c = check_date_ordering(rows)
        assert c.passed

    def test_passes_when_dates_absent(self):
        rows = _make_rows(_good_row())   # no start_date/end_date
        c = check_date_ordering(rows)
        assert c.passed

    def test_fails_when_start_after_end(self):
        rows = _make_rows(_good_row(start_date="2024-03-31", end_date="2024-03-01"))
        c = check_date_ordering(rows)
        assert not c.passed
        assert c.severity == Severity.ERROR

    def test_fails_on_unparseable_date(self):
        rows = _make_rows(_good_row(start_date="32/13/2024", end_date="2024-03-31"))
        c = check_date_ordering(rows)
        assert not c.passed


# ── check_value_ranges ────────────────────────────────────────────────────────

class TestValueRanges:
    def test_passes_for_normal_values(self):
        rows = _make_rows(_good_row(spend="1000", ctr="2.5"))
        c = check_value_ranges(rows)
        assert c.passed

    def test_warns_on_negative_spend(self):
        rows = _make_rows(_good_row(spend="-500"))
        c = check_value_ranges(rows)
        assert not c.passed
        assert c.severity == Severity.WARNING

    def test_warns_on_ctr_over_100(self):
        rows = _make_rows(_good_row(ctr="150"))
        c = check_value_ranges(rows)
        assert not c.passed

    def test_passes_on_zero_values(self):
        rows = _make_rows(_good_row(spend="0", impressions="0", ctr="0"))
        c = check_value_ranges(rows)
        assert c.passed

    def test_passes_on_placeholder_values(self):
        rows = _make_rows(_good_row(spend="N/A", ctr="-"))
        c = check_value_ranges(rows)
        assert c.passed

    def test_warns_on_negative_impressions(self):
        rows = _make_rows(_good_row(impressions="-1000"))
        c = check_value_ranges(rows)
        assert not c.passed


# ── check_currency_consistency ────────────────────────────────────────────────

class TestCurrencyConsistency:
    def test_passes_with_single_currency(self):
        rows = _make_rows(_good_row(currency="INR"), _good_row(currency="INR"))
        c = check_currency_consistency(rows)
        assert c.passed
        assert "INR" in c.message

    def test_warns_with_mixed_currencies(self):
        rows = _make_rows(_good_row(currency="INR"), _good_row(currency="USD"))
        c = check_currency_consistency(rows)
        assert not c.passed
        assert c.severity == Severity.WARNING

    def test_passes_with_all_blank_currency(self):
        rows = _make_rows(_good_row(currency=""), _good_row(currency=""))
        c = check_currency_consistency(rows)
        assert c.passed


# ── check_schema_coverage ─────────────────────────────────────────────────────

class TestSchemaCoverage:
    def test_always_passes_informational(self):
        rows = _make_rows(_good_row())
        c = check_schema_coverage(rows)
        assert c.passed
        assert c.severity == Severity.INFO

    def test_reports_blank_fields(self):
        rows = _make_rows({f: "" for f in CANONICAL_FIELDS})
        c = check_schema_coverage(rows)
        assert c.passed  # informational
        assert len(c.details) > 0   # all fields blank

    def test_no_blank_fields_message(self):
        # Give every field a value
        row = {f: "1" for f in CANONICAL_FIELDS}
        rows = [row]
        c = check_schema_coverage(rows)
        assert "at least one value" in c.message


# ── run_all_checks ────────────────────────────────────────────────────────────

class TestRunAllChecks:
    def test_returns_report(self):
        rows = _make_rows(_good_row())
        report = run_all_checks(rows, source_file="test.csv")
        assert isinstance(report, ValidationReport)

    def test_includes_row_count_check_when_given(self):
        rows = _make_rows(_good_row())
        report = run_all_checks(rows, source_file="test.csv", source_row_count=1)
        names = [c.name for c in report.checks]
        assert "row_count" in names

    def test_excludes_row_count_when_not_given(self):
        rows = _make_rows(_good_row())
        report = run_all_checks(rows, source_file="test.csv")
        names = [c.name for c in report.checks]
        assert "row_count" not in names

    def test_report_passes_for_clean_data(self):
        rows = _make_rows(_good_row())
        report = run_all_checks(rows, source_file="test.csv", source_row_count=1)
        assert report.passed
        assert report.error_count == 0

    def test_report_fails_on_error(self):
        rows = _make_rows(_good_row(spend=""))   # required field missing
        report = run_all_checks(rows, source_file="test.csv")
        assert not report.passed
        assert report.error_count >= 1

    def test_warnings_dont_fail_report(self):
        rows = _make_rows(
            _good_row(currency="INR"),
            _good_row(currency="USD"),   # mixed currency → warning
        )
        report = run_all_checks(rows, source_file="test.csv")
        assert report.warning_count >= 1
        assert report.passed   # warnings alone don't fail


# ── load_canonical_csv ────────────────────────────────────────────────────────

class TestLoadCanonicalCsv:
    def test_loads_header_and_rows(self, tmp_path):
        rows = _make_rows(_good_row())
        csv_path = tmp_path / "test_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        header, loaded = load_canonical_csv(csv_path)
        assert "spend" in header
        assert len(loaded) == 1
        assert loaded[0]["spend"] == "1000.00"

    def test_all_canonical_fields_in_header(self, tmp_path):
        rows = _make_rows(_good_row())
        csv_path = tmp_path / "test_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        header, _ = load_canonical_csv(csv_path)
        for field in CANONICAL_FIELDS:
            assert field in header


# ── CLI validate command ──────────────────────────────────────────────────────

class TestValidateCli:
    def test_passes_clean_csv(self, tmp_path):
        rows = _make_rows(_good_row())
        csv_path = tmp_path / "out_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        result = runner.invoke(app, ["validate", str(csv_path)])
        assert result.exit_code == 0
        assert "PASS" in result.output or "passed" in result.output.lower()

    def test_fails_on_missing_required_field(self, tmp_path):
        rows = _make_rows(_good_row(spend=""))
        csv_path = tmp_path / "out_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        result = runner.invoke(app, ["validate", str(csv_path)])
        assert result.exit_code == 1
        assert "FAIL" in result.output or "failed" in result.output.lower()

    def test_auto_discovers_audit_sidecar(self, tmp_path):
        rows = _make_rows(_good_row())
        csv_path = tmp_path / "myfile_table0_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        # Write matching audit JSON with correct row count
        audit = {"normalize": {"row_count": 1}}
        (tmp_path / "myfile_table0_audit.json").write_text(
            json.dumps(audit), encoding="utf-8"
        )

        result = runner.invoke(app, ["validate", str(csv_path)])
        assert result.exit_code == 0
        assert "row_count" in result.output

    def test_row_count_mismatch_fails(self, tmp_path):
        rows = _make_rows(_good_row())
        csv_path = tmp_path / "myfile_table0_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        audit = {"normalize": {"row_count": 99}}   # wrong
        (tmp_path / "myfile_table0_audit.json").write_text(
            json.dumps(audit), encoding="utf-8"
        )

        result = runner.invoke(app, ["validate", str(csv_path)])
        assert result.exit_code == 1

    def test_strict_mode_fails_on_warnings(self, tmp_path):
        rows = _make_rows(
            _good_row(currency="INR"),
            _good_row(currency="USD"),
        )
        csv_path = tmp_path / "out_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        result = runner.invoke(app, ["validate", str(csv_path), "--strict"])
        assert result.exit_code == 1

    def test_normal_mode_passes_with_warnings(self, tmp_path):
        rows = _make_rows(
            _good_row(currency="INR"),
            _good_row(currency="USD"),
        )
        csv_path = tmp_path / "out_canonical.csv"
        _write_canonical_csv(csv_path, rows)

        result = runner.invoke(app, ["validate", str(csv_path)])
        assert result.exit_code == 0

    def test_missing_file_exits_1(self):
        result = runner.invoke(app, ["validate", "ghost_canonical.csv"])
        assert result.exit_code == 1

    def test_explicit_audit_flag(self, tmp_path):
        rows = _make_rows(_good_row())
        csv_path = tmp_path / "out_canonical.csv"
        audit_path = tmp_path / "out_audit.json"
        _write_canonical_csv(csv_path, rows)
        audit_path.write_text(json.dumps({"normalize": {"row_count": 1}}), encoding="utf-8")

        result = runner.invoke(app, [
            "validate", str(csv_path), "--audit", str(audit_path)
        ])
        assert result.exit_code == 0
        assert "row_count" in result.output
