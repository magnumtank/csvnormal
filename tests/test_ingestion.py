"""Phase 2 — CSV ingestion tests."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from csvnormal.cli import app
from csvnormal.ingestion import (
    RowKind,
    _classify_row,
    _find_header,
    _is_numeric,
    detect_encoding,
    load_raw,
)

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ── Utility helpers ───────────────────────────────────────────────────────────


def test_is_numeric_integers():
    assert _is_numeric("1000") is True
    assert _is_numeric("0") is True
    assert _is_numeric("-500") is True


def test_is_numeric_floats():
    assert _is_numeric("1062500.00") is True
    assert _is_numeric("2.86") is True
    assert _is_numeric("127.55") is True


def test_is_numeric_comma_formatted():
    assert _is_numeric("1,062,500.00") is True
    assert _is_numeric("8,330,000") is True


def test_is_numeric_non_numeric():
    assert _is_numeric("Campaign") is False
    assert _is_numeric("Meta Ads") is False
    assert _is_numeric("") is False
    assert _is_numeric("NOTE:") is False


# ── Header detection ──────────────────────────────────────────────────────────


def test_find_header_simple():
    rows = [
        ["Client", "Platform", "Campaign", "Date", "Spend"],
        ["Acme", "Meta", "Spring", "2024-03-01", "1500.00"],
    ]
    result = _find_header(rows)
    assert result is not None
    idx, cells = result
    assert idx == 0
    assert "Client" in cells


def test_find_header_skips_metadata_rows():
    rows = [
        ["Report exported from MediaTool"],
        ["Generated: 2024-04-01"],
        [],
        ["Client", "Platform", "Campaign", "Date", "Spend", "Impressions"],
        ["Widgets Inc", "Meta Ads", "Q1", "2024-01-15", "1062500.00", "8330000"],
    ]
    result = _find_header(rows)
    assert result is not None
    idx, cells = result
    assert idx == 3
    assert "Client" in cells


def test_find_header_none_if_no_text_rows():
    rows = [
        ["1000", "2000", "3000"],
        ["4000", "5000", "6000"],
    ]
    # No header keywords → None
    result = _find_header(rows)
    assert result is None


# ── Row classifier ────────────────────────────────────────────────────────────

HEADER = ["Client", "Platform", "Campaign", "Date", "Spend", "Impressions"]
HEADER_LOWER = [h.lower() for h in HEADER]
COL_COUNT = len(HEADER)


def test_classify_blank_row():
    kind, msg = _classify_row(["", "", "", ""], COL_COUNT, HEADER_LOWER)
    assert kind == RowKind.BLANK


def test_classify_note_row_single_cell():
    kind, msg = _classify_row(["NOTE: All figures in INR"], COL_COUNT, HEADER_LOWER)
    assert kind == RowKind.NOTE


def test_classify_note_row_report_title():
    kind, msg = _classify_row(["FACEBOOK PERFORMANCE REPORT — Q1 2024"], COL_COUNT, HEADER_LOWER)
    assert kind == RowKind.NOTE


def test_classify_subtotal_row():
    kind, msg = _classify_row(["", "TOTAL", "", "", "33601.25", ""], COL_COUNT, HEADER_LOWER)
    assert kind == RowKind.SUBTOTAL


def test_classify_subtotal_grand_total():
    kind, msg = _classify_row(["GRAND TOTAL", "", "", "", "999.00", ""], COL_COUNT, HEADER_LOWER)
    assert kind == RowKind.SUBTOTAL


def test_classify_malformed_too_few_cols():
    kind, msg = _classify_row(["Acme", "Meta"], COL_COUNT, HEADER_LOWER)
    assert kind == RowKind.MALFORMED


def test_classify_repeated_header():
    kind, msg = _classify_row(
        ["Client", "Platform", "Campaign", "Date", "Investment", "Imps"],
        COL_COUNT, HEADER_LOWER,
    )
    assert kind == RowKind.HEADER_REPEAT


def test_classify_data_row():
    kind, msg = _classify_row(
        ["Acme Corp", "Meta Ads", "Spring Sale", "2024-03-01", "125000.00", "980000"],
        COL_COUNT, HEADER_LOWER,
    )
    assert kind == RowKind.DATA


# ── load_raw — fixture integration ────────────────────────────────────────────


class TestSingleTableClean:
    def setup_method(self):
        self.result = load_raw(FIXTURES / "single_table_clean.csv")

    def test_encoding_detected(self):
        assert self.result.encoding in ("utf-8", "ascii")

    def test_header_at_line_1(self):
        assert self.result.header_line_number == 1

    def test_correct_column_count(self):
        assert len(self.result.header_row) == 18  # fixture: Client…Currency = 18 cols

    def test_data_rows_count(self):
        assert len(self.result.data_rows) == 6

    def test_no_structural_warnings(self):
        assert len(self.result.warnings) == 0

    def test_date_column_detected(self):
        assert self.result.has_date_column is True
        assert self.result.date_column_name == "Date"

    def test_numeric_values_unchanged(self):
        # First data row spend must be exactly as in source
        first = self.result.data_rows[0]
        spend_idx = self.result.header_row.index("Spend")
        assert first.cells[spend_idx] == "125000.00"


class TestMalformed:
    def setup_method(self):
        self.result = load_raw(FIXTURES / "malformed.csv")

    def test_has_warnings(self):
        assert len(self.result.warnings) > 0

    def test_detects_note_rows(self):
        assert len(self.result.note_rows) > 0

    def test_detects_subtotal_rows(self):
        assert len(self.result.subtotal_rows) > 0

    def test_detects_malformed_rows(self):
        assert len(self.result.malformed_rows) > 0

    def test_date_column_detected(self):
        assert self.result.has_date_column is True

    def test_data_rows_are_genuine(self):
        # Data rows should only contain rows with actual campaign data
        for row in self.result.data_rows:
            non_empty = [c for c in row.cells if c.strip()]
            assert len(non_empty) >= 3, f"Suspiciously sparse data row at line {row.line_number}"


class TestNoDateFixture:
    def setup_method(self):
        self.result = load_raw(FIXTURES / "no_dates_march2024.csv")

    def test_no_date_column(self):
        assert self.result.has_date_column is False

    def test_data_rows_present(self):
        assert len(self.result.data_rows) == 6

    def test_no_structural_warnings(self):
        assert len(self.result.warnings) == 0


class TestSubtotalsFixture:
    def setup_method(self):
        self.result = load_raw(FIXTURES / "subtotals_and_sections.csv")

    def test_detects_subtotal_rows(self):
        assert len(self.result.subtotal_rows) > 0

    def test_detects_note_rows(self):
        assert len(self.result.note_rows) > 0

    def test_date_column_detected(self):
        assert self.result.has_date_column is True


class TestPlatformVariantsFixture:
    def setup_method(self):
        self.result = load_raw(FIXTURES / "platform_variants.csv")

    def test_no_structural_warnings(self):
        assert len(self.result.warnings) == 0

    def test_data_rows_count(self):
        assert len(self.result.data_rows) == 14


# ── Numeric preservation guarantee ───────────────────────────────────────────


def test_numeric_values_never_modified():
    """
    Hard guarantee: every numeric cell in the source must survive unchanged
    through load_raw. This tests the invariant at the ingestion layer.
    """
    known_values = {
        "single_table_clean.csv": ["125000.00", "980000", "2.86", "10.00"],
        "platform_variants.csv": ["465000.00", "5100000", "10.32"],
        "malformed.csv": ["1062500.00", "8330000", "2.25"],
    }
    for filename, values in known_values.items():
        result = load_raw(FIXTURES / filename)
        all_cells = [c for row in result.rows for c in row.cells]
        for val in values:
            assert val in all_cells, (
                f"Value '{val}' from {filename} was not preserved in loaded cells"
            )


# ── CLI inspect command ───────────────────────────────────────────────────────


def test_inspect_clean_fixture():
    result = runner.invoke(app, ["inspect", str(FIXTURES / "single_table_clean.csv")])
    assert result.exit_code == 0
    assert "data" in result.output
    assert "Header" in result.output


def test_inspect_malformed_shows_warnings():
    result = runner.invoke(app, ["inspect", str(FIXTURES / "malformed.csv")])
    assert result.exit_code == 0
    assert "Warning" in result.output or "warn" in result.output.lower()
    assert "subtotal" in result.output.lower() or "note" in result.output.lower()


def test_inspect_no_dates_warns_date_resolution():
    result = runner.invoke(app, ["inspect", str(FIXTURES / "no_dates_march2024.csv")])
    assert result.exit_code == 0
    # Should report date resolved from filename
    assert "march" in result.output.lower() or "2024" in result.output


def test_inspect_missing_file_exits_1():
    result = runner.invoke(app, ["inspect", "ghost.csv"])
    assert result.exit_code == 1


def test_inspect_shows_sample_data():
    result = runner.invoke(app, ["inspect", str(FIXTURES / "single_table_clean.csv")])
    assert result.exit_code == 0
    assert "Sample Data" in result.output
    # Rich wraps wide cells, so check for the first word of the client name
    assert "Acme" in result.output


def test_inspect_verbose_flag():
    result = runner.invoke(app, [
        "inspect", str(FIXTURES / "malformed.csv"), "--verbose"
    ])
    assert result.exit_code == 0
