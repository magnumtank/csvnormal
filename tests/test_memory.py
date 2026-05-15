"""Tests for Phase 8: Mapping Memory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csvnormal.cli import app
from csvnormal.memory import ColumnMemoryEntry, MappingMemory, normalize_col_name

runner = CliRunner()


# ── normalize_col_name ────────────────────────────────────────────────────────

class TestNormalizeColName:
    def test_lowercases(self):
        assert normalize_col_name("Spend") == "spend"

    def test_strips_whitespace(self):
        assert normalize_col_name("  Spend  ") == "spend"

    def test_replaces_punctuation_with_space(self):
        assert normalize_col_name("Cost/Click") == "cost click"

    def test_collapses_whitespace(self):
        assert normalize_col_name("FB  Spend") == "fb spend"

    def test_strips_parentheses_content_as_spaces(self):
        result = normalize_col_name("Spend (USD)")
        assert "spend" in result
        assert "usd" in result

    def test_same_logical_name_maps_to_same_key(self):
        assert normalize_col_name("FB Spend") == normalize_col_name("fb spend")
        assert normalize_col_name("Cost/Click") == normalize_col_name("Cost Click")


# ── MappingMemory persistence ─────────────────────────────────────────────────

class TestMappingMemoryPersistence:
    def test_empty_when_file_missing(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "memory.json")
        assert mem.stats()["total"] == 0

    def test_round_trips_to_disk(self, tmp_path):
        path = tmp_path / "memory.json"
        mem = MappingMemory(path=path)
        mem.record_column("Spend", "spend", 0.95, "ai")
        mem.save()

        mem2 = MappingMemory(path=path)
        entry = mem2.lookup_column("Spend")
        assert entry is not None
        assert entry.canonical == "spend"

    def test_save_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "memory.json"
        mem = MappingMemory(path=deep)
        mem.record_column("X", "spend", 0.9, "ai")
        mem.save()
        assert deep.exists()

    def test_corrupted_file_starts_fresh(self, tmp_path):
        path = tmp_path / "memory.json"
        path.write_text("not valid json", encoding="utf-8")
        mem = MappingMemory(path=path)
        assert mem.stats()["total"] == 0


# ── record_column ─────────────────────────────────────────────────────────────

class TestRecordColumn:
    def test_records_new_entry(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.95, "ai")
        assert mem.stats()["total"] == 1

    def test_invalid_canonical_not_stored(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("X", "not_a_field", 0.9, "ai")
        assert mem.stats()["total"] == 0

    def test_skip_and_unknown_are_valid(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Label", "__skip__", 0.9, "ai")
        mem.record_column("Weird", "__unknown__", 0.5, "ai")
        assert mem.stats()["total"] == 2

    def test_usage_count_increments(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.95, "ai")
        mem.record_column("Spend", "spend", 0.95, "ai")
        entry = mem.lookup_column("Spend")
        assert entry.usage_count == 2

    def test_user_overrides_ai(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Cost", "spend", 0.7, "ai")
        mem.record_column("Cost", "budget", 1.0, "user")
        entry = mem.lookup_column("Cost")
        assert entry.canonical == "budget"
        assert entry.source == "user"

    def test_ai_only_updates_on_higher_confidence(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.95, "ai")
        mem.record_column("Spend", "budget", 0.60, "ai")   # lower confidence — ignored
        entry = mem.lookup_column("Spend")
        assert entry.canonical == "spend"

    def test_ai_updates_when_confidence_higher(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.70, "ai")
        mem.record_column("Spend", "spend", 0.95, "ai")   # higher confidence — accepted
        entry = mem.lookup_column("Spend")
        assert entry.confidence == 0.95

    def test_user_never_overridden_by_ai(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Cost", "budget", 1.0, "user")
        mem.record_column("Cost", "spend", 0.99, "ai")    # even higher AI confidence — ignored
        entry = mem.lookup_column("Cost")
        assert entry.source == "user"
        assert entry.canonical == "budget"


# ── lookup_column ─────────────────────────────────────────────────────────────

class TestLookupColumn:
    def test_returns_none_for_unknown(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        assert mem.lookup_column("UnknownCol") is None

    def test_lookup_is_case_insensitive(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.9, "ai")
        assert mem.lookup_column("SPEND") is not None
        assert mem.lookup_column("spend") is not None

    def test_lookup_normalizes_punctuation(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Cost/Click", "cpc", 0.9, "ai")
        assert mem.lookup_column("Cost Click") is not None


# ── apply_to_header ───────────────────────────────────────────────────────────

class TestApplyToHeader:
    def test_returns_entries_above_threshold(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.95, "ai")
        mem.record_column("Impressions", "impressions", 0.70, "ai")   # below 0.85
        result = mem.apply_to_header(["Spend", "Impressions"], min_confidence=0.85)
        assert "Spend" in result
        assert "Impressions" not in result

    def test_returns_empty_for_unknown_header(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        result = mem.apply_to_header(["SomeNewCol"], min_confidence=0.85)
        assert result == {}


# ── covers_header ─────────────────────────────────────────────────────────────

class TestCoversHeader:
    def test_true_when_all_columns_known(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.95, "ai")
        mem.record_column("Platform", "platform", 0.99, "ai")
        assert mem.covers_header(["Spend", "Platform"])

    def test_false_when_one_column_missing(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.95, "ai")
        assert not mem.covers_header(["Spend", "UnknownCol"])

    def test_false_when_confidence_below_threshold(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Spend", "spend", 0.70, "ai")
        assert not mem.covers_header(["Spend"], min_confidence=0.85)


# ── record_from_mapping ───────────────────────────────────────────────────────

class TestRecordFromMapping:
    def test_records_all_columns(self, tmp_path):
        from csvnormal.ai_mapper import ColumnMappingEntry, MappingResult

        col_map = {
            "Spend": ColumnMappingEntry(canonical="spend", confidence=0.95, reason="test"),
            "CTR":   ColumnMappingEntry(canonical="ctr",   confidence=0.90, reason="test"),
        }
        mapping = MappingResult(
            source_file="test.csv", table_index=0, section_title=None,
            mapped_at="2024-03-01T00:00:00+00:00", model_used="claude-sonnet-4-6",
            prompt_version="v1", table_type="performance",
            column_mapping=col_map, platform_mapping={}, warnings=[],
            overall_confidence=0.92, raw_response="{}",
        )
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_from_mapping(mapping, source="ai")
        assert mem.stats()["total"] == 2
        assert mem.lookup_column("Spend").canonical == "spend"


# ── stats, list, clear ───────────────────────────────────────────────────────

class TestIntrospection:
    def test_stats_counts_by_source(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("A", "spend",  0.9, "ai")
        mem.record_column("B", "budget", 1.0, "user")
        s = mem.stats()
        assert s["total"] == 2
        assert s["user_corrections"] == 1
        assert s["ai_decisions"] == 1

    def test_list_sorted_by_usage_count(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("Rare", "spend", 0.9, "ai")
        for _ in range(3):
            mem.record_column("Frequent", "impressions", 0.9, "ai")
        entries = mem.list_entries()
        assert entries[0][0] == "frequent"

    def test_clear_removes_all(self, tmp_path):
        mem = MappingMemory(path=tmp_path / "m.json")
        mem.record_column("A", "spend", 0.9, "ai")
        mem.clear()
        assert mem.stats()["total"] == 0

    def test_clear_does_not_auto_save(self, tmp_path):
        path = tmp_path / "m.json"
        mem = MappingMemory(path=path)
        mem.record_column("A", "spend", 0.9, "ai")
        mem.save()
        mem.clear()
        # Without save(), the file still has the old entry
        mem2 = MappingMemory(path=path)
        assert mem2.stats()["total"] == 1


# ── CLI memory commands ───────────────────────────────────────────────────────

class TestMemoryCli:
    def test_memory_stats(self, tmp_path):
        result = runner.invoke(app, ["memory", "stats"])
        assert result.exit_code == 0
        assert "Total" in result.output or "entries" in result.output.lower()

    def test_memory_list_empty(self, tmp_path):
        result = runner.invoke(app, ["memory", "list"])
        assert result.exit_code == 0

    def test_memory_clear_with_yes_flag(self, tmp_path):
        result = runner.invoke(app, ["memory", "clear", "--yes"])
        assert result.exit_code == 0
