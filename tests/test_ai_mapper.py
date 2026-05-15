"""
Phase 4 — AI mapper tests.

All Anthropic API calls are mocked. Tests verify:
  - Prompt construction (pure functions, no API)
  - Pydantic model validation (valid/invalid responses)
  - Retry logic (mock failures then success)
  - MappingResult helpers
  - CLI map command (mocked API)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from csvnormal.ai_mapper import (
    AIResponseModel,
    ColumnMappingEntry,
    MappingResult,
    PlatformMappingEntry,
    _extract_json,
    _map_with_retry,
    map_table,
)
from csvnormal.cli import app
from csvnormal.config import CANONICAL_FIELDS
from csvnormal.ingestion import load_raw
from csvnormal.prompts import (
    _format_sample_rows,
    _unique_categoricals,
    build_mapping_prompt,
    build_repair_prompt,
)
from csvnormal.table_detector import detect_tables

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# ── Fixtures ──────────────────────────────────────────────────────────────────

CLEAN_FIXTURE = FIXTURES / "single_table_clean.csv"
INCONSISTENT_FIXTURE = FIXTURES / "inconsistent_headers.csv"
PLATFORM_FIXTURE = FIXTURES / "platform_variants.csv"


def _make_valid_response(header: list[str]) -> str:
    """Build a syntactically valid AI JSON response for any header list."""
    col_map = {
        col: {"canonical": "spend", "confidence": 0.9, "reason": "spend column"}
        for col in header
    }
    # Override obvious ones
    for col in header:
        low = col.lower()
        if "client" in low or "account" in low or "advertiser" in low:
            col_map[col] = {"canonical": "client", "confidence": 0.95, "reason": "advertiser name"}
        elif "platform" in low or "channel" in low or "network" in low:
            col_map[col] = {"canonical": "platform", "confidence": 0.95, "reason": "media channel"}
        elif "campaign" in low:
            col_map[col] = {"canonical": "campaign", "confidence": 0.98, "reason": "campaign name"}
        elif "date" in low or "week" in low or "period" in low:
            col_map[col] = {"canonical": "date", "confidence": 0.95, "reason": "date field"}
        elif "impression" in low or "imps" in low:
            col_map[col] = {"canonical": "impressions", "confidence": 0.97, "reason": "impressions count"}
        elif "click" in low:
            col_map[col] = {"canonical": "clicks", "confidence": 0.95, "reason": "clicks count"}
        elif "conv" in low or "purchase" in low:
            col_map[col] = {"canonical": "conversions", "confidence": 0.93, "reason": "conversion events"}
        elif "curr" in low:
            col_map[col] = {"canonical": "currency", "confidence": 0.98, "reason": "currency code"}
        elif "ctr" in low:
            col_map[col] = {"canonical": "ctr", "confidence": 0.99, "reason": "click-through rate"}
        elif "cpc" in low:
            col_map[col] = {"canonical": "cpc", "confidence": 0.98, "reason": "cost per click"}
        elif "cpm" in low:
            col_map[col] = {"canonical": "cpm", "confidence": 0.98, "reason": "cost per mille"}
        elif "cvr" in low:
            col_map[col] = {"canonical": "cvr", "confidence": 0.97, "reason": "conversion rate"}
        elif "cpa" in low:
            col_map[col] = {"canonical": "cpa", "confidence": 0.96, "reason": "cost per acquisition"}
        elif "revenue" in low:
            col_map[col] = {"canonical": "revenue", "confidence": 0.97, "reason": "attributed revenue"}
        elif "roas" in low:
            col_map[col] = {"canonical": "roas", "confidence": 0.99, "reason": "return on ad spend"}
        elif "reach" in low:
            col_map[col] = {"canonical": "reach", "confidence": 0.97, "reason": "unique reach"}

    return json.dumps({
        "table_type": "performance",
        "column_mapping": col_map,
        "platform_mapping": {
            "Meta Ads": {"canonical": "meta", "confidence": 0.99},
            "Google Ads": {"canonical": "google", "confidence": 0.99},
            "Facebook": {"canonical": "meta", "confidence": 0.99},
            "Instagram": {"canonical": "meta", "confidence": 0.98},
        },
        "warnings": [],
        "overall_confidence": 0.95,
    })


def _mock_client(response_text: str) -> MagicMock:
    """Return a mock Anthropic client that returns response_text."""
    mock_content = MagicMock()
    mock_content.text = response_text

    mock_response = MagicMock()
    mock_response.content = [mock_content]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


# ── Prompt builder tests (pure — no API) ──────────────────────────────────────


def test_build_mapping_prompt_contains_header():
    prompt = build_mapping_prompt(
        filename="test.csv",
        table_index=0,
        total_tables=1,
        section_title=None,
        header=["Client", "Spend", "Impressions"],
        sample_rows=[["Acme", "125000.00", "980000"]],
    )
    assert "Client" in prompt
    assert "Spend" in prompt
    assert "Impressions" in prompt


def test_build_mapping_prompt_contains_canonical_fields():
    prompt = build_mapping_prompt(
        filename="test.csv",
        table_index=0,
        total_tables=1,
        section_title=None,
        header=["Col"],
        sample_rows=[],
    )
    for field in ["spend", "impressions", "clicks", "ctr", "roas"]:
        assert field in prompt


def test_build_mapping_prompt_section_title():
    prompt = build_mapping_prompt(
        filename="test.csv",
        table_index=1,
        total_tables=2,
        section_title="Google Ads Q1",
        header=["Campaign", "Spend"],
        sample_rows=[],
    )
    assert "Google Ads Q1" in prompt


def test_format_sample_rows_basic():
    result = _format_sample_rows(
        ["Client", "Spend"],
        [["Acme Corp", "125000.00"], ["BrandX", "98000.00"]],
    )
    assert "Client" in result
    assert "Acme Corp" in result
    assert "125000.00" in result


def test_format_sample_rows_empty():
    result = _format_sample_rows(["Col"], [])
    assert "no sample rows" in result


def test_unique_categoricals_extracts_text_cols():
    cats = _unique_categoricals(
        ["Client", "Platform", "Spend"],
        [
            ["Acme", "Meta Ads", "1000.00"],
            ["Acme", "Google Ads", "2000.00"],
            ["BrandX", "Meta Ads", "3000.00"],
        ],
    )
    assert "Platform" in cats
    assert "Meta Ads" in cats["Platform"]
    assert "Google Ads" in cats["Platform"]
    # Spend is numeric — should not appear
    assert "Spend" not in cats


def test_build_repair_prompt_contains_error():
    prompt = build_repair_prompt("JSONDecodeError: unexpected char")
    assert "JSONDecodeError" in prompt


# ── JSON extraction ───────────────────────────────────────────────────────────


def test_extract_json_bare():
    raw = '{"key": "value"}'
    assert _extract_json(raw) == '{"key": "value"}'


def test_extract_json_with_markdown_fence():
    raw = "```json\n{\"key\": \"value\"}\n```"
    assert _extract_json(raw) == '{"key": "value"}'


def test_extract_json_with_plain_fence():
    raw = "```\n{\"key\": \"value\"}\n```"
    assert _extract_json(raw) == '{"key": "value"}'


# ── Pydantic model validation ─────────────────────────────────────────────────


def test_ai_response_model_valid():
    data = {
        "table_type": "performance",
        "column_mapping": {
            "Spend": {"canonical": "spend", "confidence": 0.99, "reason": "media spend"},
        },
        "platform_mapping": {},
        "warnings": [],
        "overall_confidence": 0.95,
    }
    model = AIResponseModel.model_validate(data)
    assert model.column_mapping["Spend"].canonical == "spend"


def test_ai_response_model_invalid_canonical():
    data = {
        "table_type": "performance",
        "column_mapping": {
            "Spend": {"canonical": "INVENTED_FIELD", "confidence": 0.9, "reason": "test"},
        },
        "overall_confidence": 0.9,
    }
    with pytest.raises(ValidationError):
        AIResponseModel.model_validate(data)


def test_ai_response_model_skip_is_valid():
    data = {
        "table_type": "performance",
        "column_mapping": {
            "Row #": {"canonical": "__skip__", "confidence": 1.0, "reason": "row index"},
        },
        "overall_confidence": 0.9,
    }
    model = AIResponseModel.model_validate(data)
    assert model.column_mapping["Row #"].canonical == "__skip__"


def test_ai_response_model_unknown_is_valid():
    data = {
        "table_type": "performance",
        "column_mapping": {
            "????": {"canonical": "__unknown__", "confidence": 0.1, "reason": "unclear"},
        },
        "overall_confidence": 0.5,
    }
    model = AIResponseModel.model_validate(data)
    assert model.column_mapping["????"].canonical == "__unknown__"


def test_ai_response_empty_column_mapping_invalid():
    data = {"table_type": "performance", "column_mapping": {}, "overall_confidence": 0.9}
    with pytest.raises(ValidationError):
        AIResponseModel.model_validate(data)


def test_platform_mapping_invalid_key():
    data = {
        "table_type": "performance",
        "column_mapping": {
            "Spend": {"canonical": "spend", "confidence": 0.9, "reason": "spend"},
        },
        "platform_mapping": {
            "Facebook": {"canonical": "INVENTED_PLATFORM", "confidence": 0.9},
        },
        "overall_confidence": 0.9,
    }
    with pytest.raises(ValidationError):
        AIResponseModel.model_validate(data)


def test_confidence_out_of_range():
    data = {
        "table_type": "performance",
        "column_mapping": {
            "Spend": {"canonical": "spend", "confidence": 1.5, "reason": "test"},
        },
        "overall_confidence": 0.9,
    }
    with pytest.raises(ValidationError):
        AIResponseModel.model_validate(data)


# ── map_table with mocked API ─────────────────────────────────────────────────


class TestMapTable:
    def setup_method(self):
        raw = load_raw(CLEAN_FIXTURE)
        det = detect_tables(raw)
        self.table = det.tables[0]

    def test_returns_mapping_result(self):
        response = _make_valid_response(self.table.header_row)
        client = _mock_client(response)
        result = map_table(self.table, CLEAN_FIXTURE, client=client)
        assert isinstance(result, MappingResult)

    def test_all_source_columns_mapped(self):
        response = _make_valid_response(self.table.header_row)
        client = _mock_client(response)
        result = map_table(self.table, CLEAN_FIXTURE, client=client)
        for col in self.table.header_row:
            assert col in result.column_mapping, f"Column '{col}' not in mapping"

    def test_provenance_fields(self):
        response = _make_valid_response(self.table.header_row)
        client = _mock_client(response)
        result = map_table(self.table, CLEAN_FIXTURE, client=client)
        assert result.source_file == CLEAN_FIXTURE.name
        assert result.table_index == 0
        assert result.model_used == "claude-sonnet-4-6"
        assert result.prompt_version == "v1"

    def test_raw_response_preserved(self):
        response = _make_valid_response(self.table.header_row)
        client = _mock_client(response)
        result = map_table(self.table, CLEAN_FIXTURE, client=client)
        assert result.raw_response == response

    def test_mapped_columns_helper(self):
        response = _make_valid_response(self.table.header_row)
        client = _mock_client(response)
        result = map_table(self.table, CLEAN_FIXTURE, client=client)
        assert "spend" in result.mapped_columns.values()

    def test_get_platform_fallback(self):
        response = _make_valid_response(self.table.header_row)
        client = _mock_client(response)
        result = map_table(self.table, CLEAN_FIXTURE, client=client)
        # Config-level alias fallback should work even if platform_mapping is empty
        assert result.get_platform("facebook") == "meta"

    def test_audit_dict_serialisable(self):
        response = _make_valid_response(self.table.header_row)
        client = _mock_client(response)
        result = map_table(self.table, CLEAN_FIXTURE, client=client)
        d = result.to_audit_dict()
        assert isinstance(d, dict)
        # Ensure it round-trips to JSON without error
        import json
        json.dumps(d, default=str)


# ── Retry logic ───────────────────────────────────────────────────────────────


def test_retry_on_invalid_json(tmp_path):
    """First call returns garbage; second call returns valid JSON."""
    raw = load_raw(CLEAN_FIXTURE)
    det = detect_tables(raw)
    table = det.tables[0]
    header = table.header_row

    valid_response = _make_valid_response(header)

    call_count = 0
    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_content = MagicMock()
        mock_content.text = "this is not json" if call_count == 1 else valid_response
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        return mock_response

    client = MagicMock()
    client.messages.create.side_effect = side_effect

    result = map_table(table, CLEAN_FIXTURE, client=client, max_retries=3)
    assert isinstance(result, MappingResult)
    assert call_count == 2


def test_retry_on_missing_columns(tmp_path):
    """First call omits some columns; second call covers all."""
    raw = load_raw(CLEAN_FIXTURE)
    det = detect_tables(raw)
    table = det.tables[0]
    header = table.header_row

    # Partial response — only maps first column
    partial = json.dumps({
        "table_type": "performance",
        "column_mapping": {
            header[0]: {"canonical": "client", "confidence": 0.9, "reason": "test"},
        },
        "overall_confidence": 0.5,
    })
    full = _make_valid_response(header)

    call_count = 0
    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_content = MagicMock()
        mock_content.text = partial if call_count == 1 else full
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        return mock_response

    client = MagicMock()
    client.messages.create.side_effect = side_effect

    result = map_table(table, CLEAN_FIXTURE, client=client, max_retries=3)
    assert isinstance(result, MappingResult)
    assert call_count == 2


def test_exhausted_retries_raises():
    raw = load_raw(CLEAN_FIXTURE)
    det = detect_tables(raw)
    table = det.tables[0]

    client = _mock_client("always invalid json !!!")
    with pytest.raises(RuntimeError, match="failed after"):
        map_table(table, CLEAN_FIXTURE, client=client, max_retries=2)


# ── CLI map command (mocked) ──────────────────────────────────────────────────


def _patch_map_table(fixture_path: Path):
    """Context manager that patches map_table to return a fake MappingResult."""
    raw = load_raw(fixture_path)
    det = detect_tables(raw)
    table = det.tables[0]
    response = _make_valid_response(table.header_row)
    fake_result = MappingResult(
        source_file=fixture_path.name,
        table_index=0,
        section_title=None,
        mapped_at="2024-03-01T00:00:00+00:00",
        model_used="claude-sonnet-4-6",
        prompt_version="v1",
        table_type="performance",
        column_mapping={
            col: ColumnMappingEntry(canonical="spend", confidence=0.9, reason="test")
            for col in table.header_row
        },
        platform_mapping={
            "Meta Ads": PlatformMappingEntry(canonical="meta", confidence=0.99),
        },
        warnings=[],
        overall_confidence=0.95,
        raw_response=response,
    )
    return patch("csvnormal.ai_mapper.map_table", return_value=fake_result)


def test_map_command_succeeds_with_mock():
    with _patch_map_table(CLEAN_FIXTURE):
        result = runner.invoke(app, ["map", str(CLEAN_FIXTURE), "--no-save"])
    assert result.exit_code == 0
    assert "Column Mappings" in result.output


def test_map_command_shows_platform_table():
    with _patch_map_table(CLEAN_FIXTURE):
        result = runner.invoke(app, ["map", str(CLEAN_FIXTURE), "--no-save"])
    assert result.exit_code == 0
    assert "Platform Mappings" in result.output
    assert "meta" in result.output


def test_map_command_missing_file():
    result = runner.invoke(app, ["map", "ghost.csv"])
    assert result.exit_code == 1


def test_map_command_saves_json(tmp_path):
    with patch("csvnormal.config.OUTPUT_DIR", tmp_path), \
         patch("csvnormal.cli.settings") as mock_settings, \
         _patch_map_table(CLEAN_FIXTURE):
        mock_settings.output_dir = tmp_path
        result = runner.invoke(app, ["map", str(CLEAN_FIXTURE)])
    assert result.exit_code == 0
    # JSON file should be created
    json_files = list(tmp_path.glob("*.json"))
    assert len(json_files) >= 1
