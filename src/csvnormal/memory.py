"""
Mapping Memory — persistent column-name → canonical-field store.

The store accumulates from two sources:
  - AI decisions  (source="ai")   — recorded automatically after each `map` call
  - User corrections (source="user") — recorded after the `review` workflow

Lookup and record semantics:
  - Keys are normalized column names (lowercased, punctuation → space, whitespace collapsed)
  - User entries always replace AI entries for the same key
  - AI entries only update when new confidence is higher than stored confidence
  - Usage count increments on every lookup or record

Storage format: JSON at settings.memory_file (mappings_memory.json by default).
Thread-safety: single-process only (no locking). For web, replace _load/_save with DB.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from csvnormal.config import CANONICAL_FIELDS, settings

_VALID_CANONICALS: frozenset[str] = frozenset(CANONICAL_FIELDS) | {"__skip__", "__unknown__"}


# ── Column name normalizer ────────────────────────────────────────────────────


def normalize_col_name(raw: str) -> str:
    """
    Normalize a raw column name for memory keying.

    Examples:
      'FB Spend (USD)'  → 'fb spend usd'
      'Cost/Click'      → 'cost click'
      '  Impressions  ' → 'impressions'
    """
    name = raw.lower().strip()
    name = re.sub(r"[^\w\s]", " ", name)   # punctuation → space
    name = re.sub(r"\s+", " ", name).strip()  # collapse whitespace
    return name


# ── Models ────────────────────────────────────────────────────────────────────


class ColumnMemoryEntry(BaseModel):
    canonical: str
    confidence: float
    source: Literal["ai", "user"]
    usage_count: int = 1
    last_seen: str = ""   # ISO date (YYYY-MM-DD)


# ── Memory class ──────────────────────────────────────────────────────────────


class MappingMemory:
    """
    File-backed column mapping memory.

    For web deployment, replace _load() and save() with database reads/writes
    while keeping the rest of the interface intact.
    """

    VERSION = "1"

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path: Path = path or settings.memory_file
        self._entries: dict[str, ColumnMemoryEntry] = {}
        self._loaded: bool = False

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load entries from disk. Safe to call multiple times — only loads once."""
        if self._loaded:
            return
        if not self._path.exists():
            self._entries = {}
            self._loaded = True
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = {
                k: ColumnMemoryEntry.model_validate(v)
                for k, v in data.get("entries", {}).items()
            }
        except Exception:
            self._entries = {}
        self._loaded = True

    def save(self) -> None:
        """Persist current entries to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.VERSION,
            "entries": {k: v.model_dump() for k, v in self._entries.items()},
        }
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ── Lookup ────────────────────────────────────────────────────────────────

    def lookup_column(self, raw_name: str) -> Optional[ColumnMemoryEntry]:
        """Return the stored entry for a raw column name, or None."""
        self._ensure_loaded()
        return self._entries.get(normalize_col_name(raw_name))

    def apply_to_header(
        self,
        header: list[str],
        min_confidence: float = 0.85,
    ) -> dict[str, ColumnMemoryEntry]:
        """
        Return memory entries for header columns meeting the confidence threshold.
        Columns with no memory entry or below threshold are omitted.
        """
        self._ensure_loaded()
        result: dict[str, ColumnMemoryEntry] = {}
        for col in header:
            entry = self.lookup_column(col)
            if entry and entry.confidence >= min_confidence:
                result[col] = entry
        return result

    def covers_header(self, header: list[str], min_confidence: float = 0.85) -> bool:
        """Return True when every column in header has a memory entry above threshold."""
        known = self.apply_to_header(header, min_confidence)
        return len(known) == len(header)

    # ── Record ────────────────────────────────────────────────────────────────

    def record_column(
        self,
        raw_name: str,
        canonical: str,
        confidence: float,
        source: Literal["ai", "user"],
    ) -> None:
        """Record or update one column → canonical mapping."""
        self._ensure_loaded()
        if canonical not in _VALID_CANONICALS:
            return
        key = normalize_col_name(raw_name)
        today = date.today().isoformat()
        existing = self._entries.get(key)

        if existing is None:
            self._entries[key] = ColumnMemoryEntry(
                canonical=canonical,
                confidence=confidence,
                source=source,
                usage_count=1,
                last_seen=today,
            )
        else:
            # User corrections always win; AI only updates when confidence improves
            if source == "user" or (source == "ai" and confidence > existing.confidence):
                existing.canonical = canonical
                existing.confidence = confidence
                existing.source = source
            existing.usage_count += 1
            existing.last_seen = today

    def record_from_mapping(
        self,
        mapping_result: "MappingResult",  # type: ignore[name-defined]
        source: Literal["ai", "user"] = "ai",
    ) -> None:
        """Batch-record all column mappings from a MappingResult."""
        from csvnormal.ai_mapper import MappingResult  # local import avoids circular

        for raw_col, entry in mapping_result.column_mapping.items():
            self.record_column(raw_col, entry.canonical, entry.confidence, source)

    # ── Introspection ─────────────────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        self._ensure_loaded()
        return {
            "total": len(self._entries),
            "user_corrections": sum(1 for e in self._entries.values() if e.source == "user"),
            "ai_decisions": sum(1 for e in self._entries.values() if e.source == "ai"),
        }

    def list_entries(self) -> list[tuple[str, ColumnMemoryEntry]]:
        """Return all entries sorted by usage count descending, then key."""
        self._ensure_loaded()
        return sorted(
            self._entries.items(),
            key=lambda x: (-x[1].usage_count, x[0]),
        )

    def clear(self) -> None:
        """Remove all entries (does NOT save to disk — caller must call save())."""
        self._entries = {}
        self._loaded = True
