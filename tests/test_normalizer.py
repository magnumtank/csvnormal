"""
Tests for Phase 5: Deterministic Normalization.

All tests are fully offline — no AI calls, no file I/O beyond tmp_path.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from csvnormal.ai_mapper import ColumnMappingEntry, MappingResult, PlatformMappingEntry
from csvnormal.cli import app
from csvnormal.config import CANONICAL_FIELDS, DEFAULT_CURRENCY
from csvnormal.date_resolver import DateResolution
from csvnormal.ingestion import RawRow, RowKind
from csvnormal.normalizer import (
    NormalizeResult,
    NormalizationWarning,
    normalize_table,
    write_audit_json,
    write_canonical_csv,
)
from csvnormal.table_detector import DetectedTable, TableWarning

# ── Helpers ───────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent / "fixtures"
runner = CliRunner()


def _make_table(
    header: list[str],
    rows: list[list[str]],
    table_index: int = 0,
    section_title: str | None = None,
) -> DetectedTable:
    data_rows = [
        RawRow(line_number=i + 2, kind=RowKind.DATA, cells=row)
        for i, row in enumerate(rows)
    ]
    return DetectedTable(
        table_index=table_index,
        header_line_number=1,
        header_row=header,
        section_title=section_title,
        data_rows=data_rows,
        subtotal_rows=[],
        confidence=0.9,
        confidence_reasons=["test"],
        has_date_column=any("date" in h.lower() for h in header),
        has_spend_column=True,
        has_delivery_column=True,
        warnings=[],
    )


def _make_mapping(
    header: list[str],
    col_map: dict[str, str],           # source_col → canonical | __skip__ | __unknown__
    platform_map: dict[str, str] | None = None,
) -> MappingResult:
    column_mapping = {
        src: ColumnMappingEntry(
            canonical=canon,
            confidence=0.95,
            reason="test",
        )
        for src, canon in col_map.items()
    }
    # Fill any header cols not in col_map as __skip__
    for h in header:
        if h not in column_mapping:
            column_mapping[h] = ColumnMappingEntry(canonical="__skip__", confidence=0.5, reason="auto")

    pm = {
        k: PlatformMappingEntry(canonical=v, confidence=0.99)
        for k, v in (platform_map or {}).items()
    }

    raw_resp = json.dumps({
        "table_type": "performance",
        "column_mapping": {k: v.model_dump() for k, v in column_mapping.items()},
        "platform_mapping": {k: v.model_dump() for k, v in pm.items()},
        "warnings": [],
        "overall_confidence": 0.95,
    })

    return MappingResult(
        source_file="test.csv",
        table_index=0,
        section_title=None,
        mapped_at="2024-03-01T00:00:00+00:00",
        model_used="claude-sonnet-4-6",
        prompt_version="v1",
        table_type="performance",
        column_mapping=column_mapping,
        platform_mapping=pm,
        warnings=[],
        overall_confidence=0.95,
        raw_response=raw_resp,
    )


# ── Basic normalization ───────────────────────────────────────────────────────

class TestNormalizeBasic:
    def test_returns_normalize_result(self):
        header = ["Spend", "Impressions"]
        rows = [["1000", "50000"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend", "Impressions": "impressions"})

        result = normalize_table(table, mapping)
        assert isinstance(result, NormalizeResult)

    def test_row_count_matches_data_rows(self):
        header = ["Spend"]
        rows = [["100"], ["200"], ["300"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping)
        assert result.row_count == 3

    def test_numeric_formatting_stripped(self):
        """Formatting characters are stripped; the underlying number is preserved exactly."""
        header = ["Spend", "CTR", "CPM", "Revenue"]
        rows = [["1,25,000.50", "2.86%", "127.55", "₹1,250,000.00"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {
            "Spend": "spend", "CTR": "ctr", "CPM": "cpm", "Revenue": "revenue"
        })

        result = normalize_table(table, mapping)
        row = result.rows[0].data
        # Thousand-separator commas stripped, value preserved
        assert row["spend"] == "125000.50"
        # Percentage sign stripped
        assert row["ctr"] == "2.86"
        # Already clean — unchanged
        assert row["cpm"] == "127.55"
        # Currency symbol + commas stripped
        assert row["revenue"] == "1250000.00"

    def test_non_numeric_values_left_intact(self):
        """N/A, dashes, and similar placeholders are not corrupted."""
        header = ["Spend", "Conversions"]
        rows = [["N/A", "-"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend", "Conversions": "conversions"})

        result = normalize_table(table, mapping)
        row = result.rows[0].data
        assert row["spend"] == "N/A"
        assert row["conversions"] == "-"

    def test_decimal_precision_preserved(self):
        """No float() conversion — trailing zeros and long decimals survive exactly."""
        header = ["ROAS", "CPM", "Spend"]
        rows = [["10.00", "127.550000", "999999.999"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"ROAS": "roas", "CPM": "cpm", "Spend": "spend"})

        result = normalize_table(table, mapping)
        row = result.rows[0].data
        assert row["roas"] == "10.00"
        assert row["cpm"] == "127.550000"
        assert row["spend"] == "999999.999"

    def test_roas_x_suffix_stripped(self):
        """ROAS values written as '10.00x' have the 'x' stripped."""
        header = ["ROAS"]
        rows = [["10.00x"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"ROAS": "roas"})

        result = normalize_table(table, mapping)
        assert result.rows[0].data["roas"] == "10.00"

    def test_all_canonical_fields_in_csv_output(self):
        """to_csv_string must emit ALL canonical fields as columns."""
        header = ["Spend"]
        rows = [["500"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping)
        csv_str = result.to_csv_string()
        first_line = csv_str.splitlines()[0]
        for field in CANONICAL_FIELDS:
            assert field in first_line, f"Missing canonical field in header: {field}"

    def test_missing_canonical_fields_are_empty_strings(self):
        """Fields not present in source output as empty strings, never omitted."""
        header = ["Spend"]
        rows = [["999"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping)
        row = result.rows[0].data
        assert row.get("impressions", "") == "" or "impressions" not in row
        # CSV output must have the field (empty)
        csv_str = result.to_csv_string()
        lines = csv_str.splitlines()
        headers = lines[0].split(",")
        data = lines[1].split(",")
        imp_idx = headers.index("impressions")
        assert data[imp_idx] == ""


# ── Skip and unknown columns ──────────────────────────────────────────────────

class TestColumnDisposition:
    def test_skipped_columns_not_in_output(self):
        header = ["Spend", "Row Label", "Notes"]
        rows = [["1000", "Total", "FYI"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {
            "Spend": "spend",
            "Row Label": "__skip__",
            "Notes": "__skip__",
        })

        result = normalize_table(table, mapping)
        assert "Row Label" not in result.rows[0].data
        assert "Notes" not in result.rows[0].data
        assert result.skipped_source_columns == ["Row Label", "Notes"]

    def test_unknown_columns_dropped_with_warning(self):
        header = ["Spend", "WeirdMetric"]
        rows = [["500", "42"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend", "WeirdMetric": "__unknown__"})

        result = normalize_table(table, mapping)
        assert "WeirdMetric" not in result.rows[0].data
        assert result.unknown_source_columns == ["WeirdMetric"]
        assert any(w.kind == "unknown_column" for w in result.warnings)


# ── Platform resolution ───────────────────────────────────────────────────────

class TestPlatformResolution:
    def test_platform_resolved_from_mapping(self):
        header = ["Platform", "Spend"]
        rows = [["Meta Ads", "1000"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(
            header,
            {"Platform": "platform", "Spend": "spend"},
            platform_map={"Meta Ads": "meta"},
        )

        result = normalize_table(table, mapping)
        assert result.rows[0].data["platform"] == "meta"

    def test_platform_falls_back_to_config_alias(self):
        """If platform_mapping doesn't cover the value, config PLATFORM_ALIASES is used."""
        header = ["Source", "Spend"]
        rows = [["facebook", "500"]]
        table = _make_table(header, rows)
        # Provide no platform_map — fallback to config
        mapping = _make_mapping(header, {"Source": "platform", "Spend": "spend"})

        result = normalize_table(table, mapping)
        assert result.rows[0].data["platform"] == "meta"

    def test_unknown_platform_kept_raw_with_warning(self):
        header = ["Platform", "Spend"]
        rows = [["UnknownNetwork", "100"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Platform": "platform", "Spend": "spend"})

        result = normalize_table(table, mapping)
        assert result.rows[0].data["platform"] == "UnknownNetwork"
        assert any(w.kind == "platform_unresolved" for w in result.warnings)


# ── Date resolution fill ──────────────────────────────────────────────────────

class TestDateFill:
    def test_date_filled_from_csv_column(self):
        header = ["Date", "Spend"]
        rows = [["2024-03-01", "1000"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Date": "date", "Spend": "spend"})
        dr = DateResolution(source="filename", start_date=date(2024, 3, 1), end_date=date(2024, 3, 31), month=3, year=2024, raw="march2024", confidence=0.9)

        result = normalize_table(table, mapping, date_resolution=dr)
        # CSV date column takes precedence — source column value should be used
        assert result.rows[0].data["date"] == "2024-03-01"

    def test_start_end_date_filled_from_resolution(self):
        """When no date column in CSV, DateResolution fills start_date/end_date."""
        header = ["Spend"]
        rows = [["1000"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})
        dr = DateResolution(
            source="filename",
            start_date=date(2024, 3, 1),
            end_date=date(2024, 3, 31),
            month=3,
            year=2024,
            raw="march2024",
            confidence=0.9,
        )

        result = normalize_table(table, mapping, date_resolution=dr)
        row = result.rows[0].data
        assert row["start_date"] == "2024-03-01"
        assert row["end_date"] == "2024-03-31"
        assert row["month"] == "March 2024"
        assert row["year"] == "2024"

    def test_no_date_resolution_leaves_date_fields_blank(self):
        header = ["Spend"]
        rows = [["500"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping, date_resolution=None)
        row = result.rows[0].data
        assert row.get("start_date", "") == ""
        assert row.get("end_date", "") == ""


# ── Currency default ──────────────────────────────────────────────────────────

class TestCurrencyDefault:
    def test_currency_defaults_to_inr_when_not_mapped(self):
        header = ["Spend"]
        rows = [["2000"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping)
        assert result.rows[0].data["currency"] == DEFAULT_CURRENCY
        assert DEFAULT_CURRENCY == "INR"

    def test_mapped_currency_preserved(self):
        header = ["Spend", "Currency"]
        rows = [["500", "USD"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend", "Currency": "currency"})

        result = normalize_table(table, mapping)
        assert result.rows[0].data["currency"] == "USD"


# ── Client name ───────────────────────────────────────────────────────────────

class TestClientName:
    def test_client_name_set_by_caller(self):
        header = ["Spend"]
        rows = [["100"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping, client_name="Acme Corp")
        assert result.rows[0].data["client"] == "Acme Corp"

    def test_client_from_source_column(self):
        header = ["Client", "Spend"]
        rows = [["BrandX", "200"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Client": "client", "Spend": "spend"})

        result = normalize_table(table, mapping, client_name="")
        assert result.rows[0].data["client"] == "BrandX"

    def test_caller_client_overrides_source_column(self):
        header = ["Client", "Spend"]
        rows = [["OldName", "300"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Client": "client", "Spend": "spend"})

        result = normalize_table(table, mapping, client_name="NewName")
        assert result.rows[0].data["client"] == "NewName"


# ── CSV output ────────────────────────────────────────────────────────────────

class TestCsvOutput:
    def test_csv_string_has_correct_column_order(self):
        header = ["Spend", "Impressions", "Platform"]
        rows = [["1000", "50000", "meta"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {
            "Spend": "spend",
            "Impressions": "impressions",
            "Platform": "platform",
        })

        result = normalize_table(table, mapping)
        csv_str = result.to_csv_string()
        headers = csv_str.splitlines()[0].split(",")
        assert headers == CANONICAL_FIELDS

    def test_write_canonical_csv(self, tmp_path):
        header = ["Spend"]
        rows = [["42"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})
        result = normalize_table(table, mapping)

        out = tmp_path / "out.csv"
        write_canonical_csv(result, out)
        assert out.exists()
        content = out.read_text()
        assert "spend" in content
        assert "42" in content

    def test_write_audit_json(self, tmp_path):
        header = ["Spend"]
        rows = [["99"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})
        result = normalize_table(table, mapping)

        out = tmp_path / "audit.json"
        write_audit_json(result, mapping, out)
        assert out.exists()
        data = json.loads(out.read_text())
        assert "normalize" in data
        assert "mapping" in data
        assert data["normalize"]["row_count"] == 1


# ── Audit metadata ────────────────────────────────────────────────────────────

class TestAuditMetadata:
    def test_canonical_columns_used_sorted_by_schema_order(self):
        header = ["Impressions", "Spend"]
        rows = [["50000", "1000"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend", "Impressions": "impressions"})

        result = normalize_table(table, mapping)
        used = result.canonical_columns_used
        spend_idx = used.index("spend")
        imp_idx = used.index("impressions")
        assert spend_idx < imp_idx  # spend comes before impressions in schema

    def test_line_numbers_preserved(self):
        header = ["Spend"]
        rows = [["100"], ["200"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping)
        assert result.rows[0].line_number == 2
        assert result.rows[1].line_number == 3

    def test_to_audit_dict_is_json_serialisable(self):
        header = ["Spend"]
        rows = [["500"]]
        table = _make_table(header, rows)
        mapping = _make_mapping(header, {"Spend": "spend"})

        result = normalize_table(table, mapping)
        d = result.to_audit_dict()
        # Should round-trip through JSON without error
        assert json.loads(json.dumps(d, default=str))


# ── Multi-row fixture test ────────────────────────────────────────────────────

class TestMultiRowFixture:
    def test_clean_fixture_normalization(self):
        """End-to-end normalization of single_table_clean.csv with a mock mapping."""
        from csvnormal.ingestion import load_raw
        from csvnormal.table_detector import detect_tables

        fixture = FIXTURES / "single_table_clean.csv"
        raw = load_raw(fixture)
        detection = detect_tables(raw)
        assert detection.total_tables >= 1
        tbl = detection.tables[0]

        # Build mapping that covers the clean fixture headers
        col_map = {
            "Client": "client",
            "Platform": "platform",
            "Campaign Name": "campaign",
            "Date": "date",
            "Spend": "spend",
            "Impressions": "impressions",
            "Reach": "reach",
            "Clicks": "clicks",
            "Link Clicks": "link_clicks",
            "CTR": "ctr",
            "CPC": "cpc",
            "CPM": "cpm",
            "Conversions": "conversions",
            "CVR": "cvr",
            "CPA": "cpa",
            "Revenue": "revenue",
            "ROAS": "roas",
            "Currency": "currency",
        }
        mapping = _make_mapping(tbl.header_row, col_map, platform_map={"Meta Ads": "meta"})

        result = normalize_table(tbl, mapping)
        assert result.row_count == 6
        # Numeric values preserved
        assert result.rows[0].data["spend"] == "125000.00"
        assert result.rows[0].data["ctr"] == "2.86"
        assert result.rows[0].data["roas"] == "10.00"
        # Platform resolved
        assert result.rows[0].data["platform"] == "meta"

    def test_no_dates_fixture_fills_from_resolution(self):
        """Filename-derived dates are filled when CSV has no date column."""
        from csvnormal.ingestion import load_raw
        from csvnormal.table_detector import detect_tables

        fixture = FIXTURES / "no_dates_march2024.csv"
        raw = load_raw(fixture)
        detection = detect_tables(raw)
        tbl = detection.tables[0]

        col_map = {h: "__skip__" for h in tbl.header_row}
        # Map spend if present
        for h in tbl.header_row:
            if "spend" in h.lower():
                col_map[h] = "spend"
                break

        mapping = _make_mapping(tbl.header_row, col_map)
        dr = DateResolution(
            source="filename",
            start_date=date(2024, 3, 1),
            end_date=date(2024, 3, 31),
            month=3,
            year=2024,
            raw="march2024",
            confidence=0.9,
        )

        result = normalize_table(tbl, mapping, date_resolution=dr)
        assert result.rows[0].data["start_date"] == "2024-03-01"
        assert result.rows[0].data["month"] == "March 2024"


# ── CLI normalize command ─────────────────────────────────────────────────────

class TestNormalizeCli:
    def _fake_mapping(self, tbl):
        header = tbl.header_row
        col_map = {
            "Client": "client",
            "Platform": "platform",
            "Campaign Name": "campaign",
            "Date": "date",
            "Spend": "spend",
            "Impressions": "impressions",
            "Reach": "reach",
            "Clicks": "clicks",
            "Link Clicks": "link_clicks",
            "CTR": "ctr",
            "CPC": "cpc",
            "CPM": "cpm",
            "Conversions": "conversions",
            "CVR": "cvr",
            "CPA": "cpa",
            "Revenue": "revenue",
            "ROAS": "roas",
            "Currency": "currency",
        }
        return _make_mapping(
            header,
            {h: col_map.get(h, "__skip__") for h in header},
            platform_map={"Meta Ads": "meta"},
        )

    def test_normalize_with_mapping_file(self, tmp_path):
        """normalize command reads a saved mapping JSON and writes canonical CSV."""
        from csvnormal.ingestion import load_raw
        from csvnormal.table_detector import detect_tables

        fixture = FIXTURES / "single_table_clean.csv"
        raw = load_raw(fixture)
        tbl = detect_tables(raw).tables[0]
        fake_map = self._fake_mapping(tbl)

        # Save as an audit sidecar JSON (the format normalize --mapping accepts)
        mapping_json = tmp_path / "mapping.json"
        mapping_json.write_text(
            json.dumps({"mapping": fake_map.to_audit_dict()}, default=str),
            encoding="utf-8",
        )

        result = runner.invoke(app, [
            "normalize",
            str(fixture),
            "--mapping", str(mapping_json),
            "--output-dir", str(tmp_path),
        ])

        if result.exit_code != 0:
            print(result.output)
            if result.exception:
                import traceback
                traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
        assert result.exit_code == 0
        csv_files = list(tmp_path.glob("*_canonical.csv"))
        assert len(csv_files) == 1
        content = csv_files[0].read_text()
        assert "spend" in content
        assert "125000.00" in content

    def test_normalize_writes_audit_json(self, tmp_path):
        from csvnormal.ingestion import load_raw
        from csvnormal.table_detector import detect_tables

        fixture = FIXTURES / "single_table_clean.csv"
        raw = load_raw(fixture)
        tbl = detect_tables(raw).tables[0]
        fake_map = self._fake_mapping(tbl)

        mapping_json = tmp_path / "mapping.json"
        mapping_json.write_text(
            json.dumps({"mapping": fake_map.to_audit_dict()}, default=str),
            encoding="utf-8",
        )

        runner.invoke(app, [
            "normalize", str(fixture),
            "--mapping", str(mapping_json),
            "--output-dir", str(tmp_path),
        ])

        audit_files = list(tmp_path.glob("*_audit.json"))
        assert len(audit_files) == 1
        data = json.loads(audit_files[0].read_text())
        assert "normalize" in data
        assert "mapping" in data

    def test_normalize_missing_file_exits_1(self):
        result = runner.invoke(app, ["normalize", "ghost.csv"])
        assert result.exit_code == 1

    def test_normalize_no_ai_without_mapping_exits_1(self, tmp_path):
        fixture = FIXTURES / "single_table_clean.csv"
        result = runner.invoke(app, ["normalize", str(fixture), "--no-ai"])
        assert result.exit_code == 1

    def test_normalize_missing_mapping_file_exits_1(self, tmp_path):
        fixture = FIXTURES / "single_table_clean.csv"
        result = runner.invoke(app, [
            "normalize", str(fixture),
            "--mapping", str(tmp_path / "nonexistent.json"),
        ])
        assert result.exit_code == 1
