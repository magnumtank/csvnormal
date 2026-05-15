"""Phase 1 scaffold smoke tests."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from csvnormal.cli import app
from csvnormal.config import CANONICAL_FIELDS, NUMERIC_FIELDS, REQUIRED_FIELDS

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ── Schema tests ──────────────────────────────────────────────────────────────


def test_canonical_fields_exist():
    assert len(CANONICAL_FIELDS) >= 9


def test_required_fields_subset():
    for f in REQUIRED_FIELDS:
        assert f in CANONICAL_FIELDS, f"{f} not in canonical fields"


def test_numeric_fields_subset():
    for f in NUMERIC_FIELDS:
        assert f in CANONICAL_FIELDS, f"{f} not in canonical fields"


# ── Fixture tests ─────────────────────────────────────────────────────────────


def test_all_fixtures_exist():
    expected = [
        "single_table_clean.csv",
        "inconsistent_headers.csv",
        "multi_table.csv",
        "platform_variants.csv",
        "malformed.csv",
        "subtotals_and_sections.csv",
    ]
    for name in expected:
        path = FIXTURES / name
        assert path.exists(), f"Missing fixture: {name}"
        assert path.stat().st_size > 0, f"Empty fixture: {name}"


# ── CLI smoke tests ───────────────────────────────────────────────────────────


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_schema_command():
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    for field in CANONICAL_FIELDS:
        assert field in result.output


def test_inspect_missing_file():
    result = runner.invoke(app, ["inspect", "nonexistent.csv"])
    assert result.exit_code == 1


def test_inspect_warns_phase_not_ready():
    fixture = FIXTURES / "single_table_clean.csv"
    result = runner.invoke(app, ["inspect", str(fixture)])
    assert result.exit_code == 0
    assert "Phase 2" in result.output or "WARN" in result.output


def test_map_warns_phase_not_ready():
    fixture = FIXTURES / "single_table_clean.csv"
    result = runner.invoke(app, ["map", str(fixture)])
    assert result.exit_code == 0
    assert "Phase 4" in result.output or "WARN" in result.output
