"""Tests for Phase 7: Terminal Review Workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csvnormal.ai_mapper import ColumnMappingEntry, MappingResult
from csvnormal.cli import app
from csvnormal.review import apply_override, get_review_plan, run_review

runner = CliRunner()
FIXTURES = Path(__file__).parent.parent / "fixtures"


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_mapping(col_map: dict[str, tuple[str, float]]) -> MappingResult:
    """Build a MappingResult from {col: (canonical, confidence)} dict."""
    column_mapping = {
        col: ColumnMappingEntry(canonical=canon, confidence=conf, reason="test")
        for col, (canon, conf) in col_map.items()
    }
    raw = json.dumps({
        "table_type": "performance",
        "column_mapping": {k: v.model_dump() for k, v in column_mapping.items()},
        "platform_mapping": {},
        "warnings": [],
        "overall_confidence": 0.85,
    })
    return MappingResult(
        source_file="test.csv", table_index=0, section_title=None,
        mapped_at="2024-03-01T00:00:00+00:00", model_used="claude-sonnet-4-6",
        prompt_version="v1", table_type="performance",
        column_mapping=column_mapping, platform_mapping={},
        warnings=[], overall_confidence=0.85, raw_response=raw,
    )


# ── get_review_plan ───────────────────────────────────────────────────────────

class TestGetReviewPlan:
    def test_flags_low_confidence_columns(self):
        mapping = _make_mapping({"Spend": ("spend", 0.95), "Cost": ("cpc", 0.60)})
        flagged = get_review_plan(mapping, threshold=0.75)
        assert "Cost" in flagged
        assert "Spend" not in flagged

    def test_returns_all_when_review_all(self):
        mapping = _make_mapping({"Spend": ("spend", 0.99), "CTR": ("ctr", 0.99)})
        flagged = get_review_plan(mapping, review_all=True)
        assert set(flagged) == {"Spend", "CTR"}

    def test_returns_empty_when_all_above_threshold(self):
        mapping = _make_mapping({"Spend": ("spend", 0.95), "CTR": ("ctr", 0.90)})
        flagged = get_review_plan(mapping, threshold=0.75)
        assert flagged == []


# ── apply_override ────────────────────────────────────────────────────────────

class TestApplyOverride:
    def test_applies_valid_override(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        updated = apply_override(mapping, {"Cost": "spend"})
        assert updated.column_mapping["Cost"].canonical == "spend"
        assert updated.column_mapping["Cost"].confidence == 1.0
        assert updated.column_mapping["Cost"].reason == "user override"

    def test_ignores_invalid_canonical(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        updated = apply_override(mapping, {"Cost": "not_a_real_field"})
        assert updated.column_mapping["Cost"].canonical == "cpc"   # unchanged

    def test_ignores_unknown_source_column(self):
        mapping = _make_mapping({"Spend": ("spend", 0.95)})
        updated = apply_override(mapping, {"NoSuchCol": "budget"})
        assert "NoSuchCol" not in updated.column_mapping

    def test_skip_and_unknown_are_valid_overrides(self):
        mapping = _make_mapping({"Notes": ("__unknown__", 0.50)})
        updated = apply_override(mapping, {"Notes": "__skip__"})
        assert updated.column_mapping["Notes"].canonical == "__skip__"

    def test_original_not_mutated(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        apply_override(mapping, {"Cost": "spend"})
        assert mapping.column_mapping["Cost"].canonical == "cpc"   # original unchanged


# ── run_review (interactive — injected prompt) ────────────────────────────────

class TestRunReview:
    def test_no_flagged_columns_returns_unchanged(self):
        mapping = _make_mapping({"Spend": ("spend", 0.99)})
        result, overridden = run_review(mapping, threshold=0.75)
        assert overridden == []
        assert result.column_mapping["Spend"].canonical == "spend"

    def test_accept_all_via_a_choice(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        inputs = iter(["a"])
        result, overridden = run_review(
            mapping, threshold=0.75, prompt_fn=lambda _: next(inputs)
        )
        assert overridden == []
        assert result.column_mapping["Cost"].canonical == "cpc"

    def test_quit_returns_unchanged(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        inputs = iter(["q"])
        result, overridden = run_review(
            mapping, threshold=0.75, prompt_fn=lambda _: next(inputs)
        )
        assert overridden == []

    def test_review_and_enter_accepts_suggestion(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        # "r" = review, then Enter = accept suggestion
        inputs = iter(["r", ""])
        result, overridden = run_review(
            mapping, threshold=0.75, prompt_fn=lambda _: next(inputs)
        )
        assert overridden == []
        assert result.column_mapping["Cost"].canonical == "cpc"

    def test_review_and_type_override(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        inputs = iter(["r", "spend"])
        result, overridden = run_review(
            mapping, threshold=0.75, prompt_fn=lambda _: next(inputs)
        )
        assert "Cost" in overridden
        assert result.column_mapping["Cost"].canonical == "spend"
        assert result.column_mapping["Cost"].confidence == 1.0

    def test_invalid_input_keeps_original(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        inputs = iter(["r", "not_a_field"])
        result, overridden = run_review(
            mapping, threshold=0.75, prompt_fn=lambda _: next(inputs)
        )
        assert "Cost" not in overridden
        assert result.column_mapping["Cost"].canonical == "cpc"

    def test_multiple_flagged_columns_reviewed(self):
        mapping = _make_mapping({
            "CostA": ("cpc", 0.60),
            "CostB": ("cpa", 0.65),
        })
        # "r" = review, then override first, accept second
        inputs = iter(["r", "spend", ""])
        result, overridden = run_review(
            mapping, threshold=0.75, prompt_fn=lambda _: next(inputs)
        )
        assert "CostA" in overridden
        assert "CostB" not in overridden

    def test_review_all_flag_reviews_every_column(self):
        mapping = _make_mapping({
            "Spend": ("spend", 0.99),
            "Cost":  ("cpc",   0.60),
        })
        inputs = iter(["r", "", ""])   # accept both
        result, overridden = run_review(
            mapping, threshold=0.75, review_all=True,
            prompt_fn=lambda _: next(inputs)
        )
        assert overridden == []

    def test_original_mapping_not_mutated(self):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        original_canonical = mapping.column_mapping["Cost"].canonical
        inputs = iter(["r", "spend"])
        run_review(mapping, threshold=0.75, prompt_fn=lambda _: next(inputs))
        assert mapping.column_mapping["Cost"].canonical == original_canonical


# ── CLI review command ────────────────────────────────────────────────────────

class TestReviewCli:
    def _write_mapping_json(self, path: Path, mapping: MappingResult) -> None:
        path.write_text(
            json.dumps(mapping.to_audit_dict(), indent=2, default=str),
            encoding="utf-8",
        )

    def test_missing_file_exits_1(self):
        result = runner.invoke(app, ["review", "ghost.json"])
        assert result.exit_code == 1

    def test_all_high_confidence_no_review_needed(self, tmp_path):
        mapping = _make_mapping({"Spend": ("spend", 0.99)})
        path = tmp_path / "mapping.json"
        self._write_mapping_json(path, mapping)

        result = runner.invoke(app, ["review", str(path)])
        assert result.exit_code == 0
        assert "nothing to review" in result.output.lower()

    def test_low_confidence_triggers_review_flow(self, tmp_path):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        path = tmp_path / "mapping.json"
        self._write_mapping_json(path, mapping)

        # Simulate: user types "q" to quit
        result = runner.invoke(app, ["review", str(path)], input="q\n")
        assert result.exit_code == 0

    def test_override_written_back_to_file(self, tmp_path):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        path = tmp_path / "mapping.json"
        self._write_mapping_json(path, mapping)

        # Simulate: "r" (review), then "spend" (override)
        result = runner.invoke(app, ["review", str(path)], input="r\nspend\n")
        assert result.exit_code == 0

        saved = json.loads(path.read_text())
        # The saved file is updated MappingResult — check the canonical was changed
        assert saved["column_mapping"]["Cost"]["canonical"] == "spend"

    def test_no_save_flag_does_not_write_file(self, tmp_path):
        mapping = _make_mapping({"Cost": ("cpc", 0.60)})
        path = tmp_path / "mapping.json"
        self._write_mapping_json(path, mapping)
        mtime_before = path.stat().st_mtime

        runner.invoke(app, ["review", str(path), "--no-save"], input="r\nspend\n")
        assert path.stat().st_mtime == mtime_before   # file not touched
