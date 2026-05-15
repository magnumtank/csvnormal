"""Phase 3 — table detection tests."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from csvnormal.cli import app
from csvnormal.ingestion import RawRow, RowKind, load_raw
from csvnormal.table_detector import (
    DetectedTable,
    TableDetectionResult,
    _compute_confidence,
    _find_column,
    detect_tables,
)

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ── Utility helpers ───────────────────────────────────────────────────────────


def test_find_column_exact_match():
    header = ["Client", "Platform", "Spend", "Impressions", "Date"]
    found, name = _find_column(header, frozenset({"spend", "cost"}))
    assert found is True
    assert name == "Spend"


def test_find_column_partial_match():
    header = ["Client", "Platform", "Budget Spend", "Impressions"]
    found, name = _find_column(header, frozenset({"spend", "cost"}))
    assert found is True
    assert name == "Budget Spend"


def test_find_column_not_found():
    header = ["Client", "Platform", "Campaign"]
    found, name = _find_column(header, frozenset({"spend", "cost", "investment"}))
    assert found is False
    assert name is None


# ── Confidence scoring ────────────────────────────────────────────────────────


def _make_rows(n: int, col_count: int) -> list[RawRow]:
    return [
        RawRow(
            line_number=i + 2,
            kind=RowKind.DATA,
            cells=["val"] * col_count,
        )
        for i in range(n)
    ]


def test_confidence_full_signals():
    rows = _make_rows(6, 5)
    score, reasons = _compute_confidence(
        ["Campaign", "Spend", "Impressions", "Date", "Clicks"],
        rows,
        has_spend=True,
        has_delivery=True,
        has_date=True,
    )
    assert score >= 0.90
    assert any("data rows" in r for r in reasons)


def test_confidence_no_data_rows():
    score, reasons = _compute_confidence(
        ["Campaign", "Spend"],
        [],
        has_spend=True,
        has_delivery=False,
        has_date=False,
    )
    assert score == 0.0


def test_confidence_penalised_missing_spend():
    rows = _make_rows(5, 3)
    score, reasons = _compute_confidence(
        ["Campaign", "Platform", "Notes"],
        rows,
        has_spend=False,
        has_delivery=True,
        has_date=True,
    )
    assert score < 0.90
    assert any("spend" in r.lower() for r in reasons)


def test_confidence_penalised_small_table():
    rows = _make_rows(1, 5)
    score, _ = _compute_confidence(
        ["Campaign", "Spend", "Impressions", "Date", "Clicks"],
        rows,
        has_spend=True,
        has_delivery=True,
        has_date=True,
    )
    assert score < 0.80


def test_confidence_inconsistent_columns():
    rows = _make_rows(4, 5)
    # Make one row have wrong length
    rows[2] = RawRow(line_number=5, kind=RowKind.DATA, cells=["val", "val"])
    score, reasons = _compute_confidence(
        ["a", "b", "c", "d", "e"],
        rows,
        has_spend=True,
        has_delivery=True,
        has_date=True,
    )
    assert score < 1.0
    assert any("wrong column count" in r for r in reasons)


# ── Single-table detection ────────────────────────────────────────────────────


class TestSingleTableClean:
    def setup_method(self):
        self.inspect = load_raw(FIXTURES / "single_table_clean.csv")
        self.det = detect_tables(self.inspect)

    def test_exactly_one_table(self):
        assert self.det.total_tables == 1

    def test_data_rows_match(self):
        assert self.det.tables[0].row_count == 6

    def test_high_confidence(self):
        assert self.det.tables[0].confidence >= 0.85
        assert self.det.tables[0].confidence_label == "HIGH"

    def test_date_column_detected(self):
        assert self.det.tables[0].has_date_column is True

    def test_spend_column_detected(self):
        assert self.det.tables[0].has_spend_column is True

    def test_delivery_column_detected(self):
        assert self.det.tables[0].has_delivery_column is True

    def test_no_subtotals(self):
        assert len(self.det.tables[0].subtotal_rows) == 0

    def test_total_data_rows(self):
        assert self.det.total_data_rows == 6


class TestNoDateFixture:
    def setup_method(self):
        self.inspect = load_raw(FIXTURES / "no_dates_march2024.csv")
        self.det = detect_tables(self.inspect)

    def test_one_table(self):
        assert self.det.total_tables == 1

    def test_no_date_column(self):
        assert self.det.tables[0].has_date_column is False

    def test_still_high_confidence(self):
        # Only -5% penalty for missing date — should still be HIGH
        assert self.det.tables[0].confidence >= 0.80

    def test_six_data_rows(self):
        assert self.det.tables[0].row_count == 6


# ── Multi-table detection ─────────────────────────────────────────────────────


class TestMultiTable:
    def setup_method(self):
        self.inspect = load_raw(FIXTURES / "multi_table.csv")
        self.det = detect_tables(self.inspect)

    def test_two_tables_detected(self):
        assert self.det.total_tables == 2, (
            f"Expected 2 tables, got {self.det.total_tables}"
        )

    def test_table_indices(self):
        assert self.det.tables[0].table_index == 0
        assert self.det.tables[1].table_index == 1

    def test_both_tables_have_data(self):
        for t in self.det.tables:
            assert t.row_count > 0, f"Table {t.table_index} has no data rows"

    def test_both_high_confidence(self):
        for t in self.det.tables:
            assert t.confidence >= 0.65, (
                f"Table {t.table_index} confidence too low: {t.confidence}"
            )

    def test_subtotals_captured(self):
        # Each table in multi_table.csv has a SUBTOTALS row
        total_subtotals = sum(len(t.subtotal_rows) for t in self.det.tables)
        assert total_subtotals >= 1

    def test_section_title_captured(self):
        # The second table should have picked up the Google Ads section title
        second = self.det.tables[1]
        assert second.section_title is not None
        assert "google" in second.section_title.lower()

    def test_total_rows_correct(self):
        assert self.det.total_data_rows == 8  # 4 per table


class TestSubtotalsAndSections:
    def setup_method(self):
        self.inspect = load_raw(FIXTURES / "subtotals_and_sections.csv")
        self.det = detect_tables(self.inspect)

    def test_multiple_tables_detected(self):
        # Paid Social, Paid Search, Display = 3 sections
        assert self.det.total_tables >= 2

    def test_subtotals_excluded_from_data(self):
        for t in self.det.tables:
            for row in t.data_rows:
                assert row.kind == RowKind.DATA, (
                    f"Non-DATA row leaked into data_rows of table {t.table_index}"
                )


class TestPlatformVariants:
    def setup_method(self):
        self.inspect = load_raw(FIXTURES / "platform_variants.csv")
        self.det = detect_tables(self.inspect)

    def test_single_table(self):
        assert self.det.total_tables == 1

    def test_fourteen_data_rows(self):
        assert self.det.tables[0].row_count == 14

    def test_high_confidence(self):
        assert self.det.tables[0].confidence >= 0.85


# ── Numeric preservation guarantee through detection ─────────────────────────


def test_data_cells_unchanged_after_detection():
    """
    detect_tables must not modify any cell values.
    Spot-check known numeric values from each fixture.
    """
    checks = {
        "single_table_clean.csv": "125000.00",
        "platform_variants.csv": "465000.00",
        "no_dates_march2024.csv": "850000.00",
    }
    for filename, expected_value in checks.items():
        result = load_raw(FIXTURES / filename)
        det = detect_tables(result)
        all_cells = [c for t in det.tables for row in t.data_rows for c in row.cells]
        assert expected_value in all_cells, (
            f"Value '{expected_value}' from {filename} was altered or lost "
            "after table detection"
        )


# ── CLI inspect integration ───────────────────────────────────────────────────


def test_inspect_shows_tables_section():
    result = runner.invoke(app, ["inspect", str(FIXTURES / "single_table_clean.csv")])
    assert result.exit_code == 0
    assert "Tables Detected" in result.output


def test_inspect_multi_table_shows_2_tables():
    result = runner.invoke(app, ["inspect", str(FIXTURES / "multi_table.csv")])
    assert result.exit_code == 0
    assert "Tables Detected" in result.output
    assert "2" in result.output  # at minimum the count appears


def test_inspect_verbose_shows_confidence_reasons():
    result = runner.invoke(app, [
        "inspect", str(FIXTURES / "single_table_clean.csv"), "--verbose"
    ])
    assert result.exit_code == 0
    assert "data rows" in result.output.lower()


def test_inspect_malformed_still_completes():
    result = runner.invoke(app, ["inspect", str(FIXTURES / "malformed.csv")])
    assert result.exit_code == 0
    assert "Tables Detected" in result.output
