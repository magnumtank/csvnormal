"""
AI mapping layer — sends table metadata to Claude, validates the JSON response.

Design rules:
  - Never sends raw file contents — only headers + sample rows (≤5 rows).
  - All API responses are validated with Pydantic before use.
  - Retry loop adds structured error feedback so the model can self-correct.
  - Raw AI response is preserved in MappingResult for full auditability.
  - Numeric values in sample rows are passed as strings — never interpreted.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field, field_validator, model_validator

from csvnormal import logger as log
from csvnormal.config import CANONICAL_FIELDS, CANONICAL_PLATFORMS, settings
from csvnormal.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_mapping_prompt, build_repair_prompt
from csvnormal.table_detector import DetectedTable

# ── Allowed sentinel values alongside canonical field names ───────────────────

_VALID_CANONICALS = set(CANONICAL_FIELDS) | {"__skip__", "__unknown__"}
_VALID_PLATFORMS  = set(CANONICAL_PLATFORMS) | {"__unknown__"}

# ── Pydantic models for the AI JSON response ──────────────────────────────────


class ColumnMappingEntry(BaseModel):
    canonical: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""

    @field_validator("canonical")
    @classmethod
    def canonical_must_be_valid(cls, v: str) -> str:
        if v not in _VALID_CANONICALS:
            raise ValueError(
                f"'{v}' is not a valid canonical field. "
                f"Must be one of the canonical fields, '__skip__', or '__unknown__'."
            )
        return v


class PlatformMappingEntry(BaseModel):
    canonical: str
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("canonical")
    @classmethod
    def platform_must_be_valid(cls, v: str) -> str:
        if v not in _VALID_PLATFORMS:
            # Soft fix: try lowercased lookup
            if v.lower() in _VALID_PLATFORMS:
                return v.lower()
            raise ValueError(
                f"'{v}' is not a valid platform key. "
                f"Must be one of: {sorted(_VALID_PLATFORMS)}"
            )
        return v


class AIResponseModel(BaseModel):
    """Strict Pydantic model for the raw AI JSON output."""

    table_type: str = "unknown"
    column_mapping: dict[str, ColumnMappingEntry]
    platform_mapping: dict[str, PlatformMappingEntry] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def columns_must_be_non_empty(self) -> "AIResponseModel":
        if not self.column_mapping:
            raise ValueError("column_mapping must not be empty")
        return self


class MappingResult(BaseModel):
    """Full output of a single table mapping — saved to disk and used in Phase 5."""

    model_config = {"arbitrary_types_allowed": True}

    # Provenance
    source_file: str
    table_index: int
    section_title: Optional[str]
    mapped_at: str                  # ISO 8601
    model_used: str
    prompt_version: str

    # Core mappings
    table_type: str
    column_mapping: dict[str, ColumnMappingEntry]
    platform_mapping: dict[str, PlatformMappingEntry]
    warnings: list[str]
    overall_confidence: float

    # Audit trail
    raw_response: str

    # ── Helpers for downstream phases ─────────────────────────────────────────

    def get_canonical(self, source_col: str) -> str | None:
        """Return the canonical field name for a source column, or None."""
        entry = self.column_mapping.get(source_col)
        if entry and entry.canonical not in ("__skip__", "__unknown__"):
            return entry.canonical
        return None

    def get_platform(self, source_value: str) -> str | None:
        """Return the canonical platform key for a source value, or None."""
        entry = self.platform_mapping.get(source_value)
        if entry and entry.canonical != "__unknown__":
            return entry.canonical
        # Fallback: check config-level aliases
        from csvnormal.config import PLATFORM_ALIASES
        return PLATFORM_ALIASES.get(source_value.strip().lower())

    @property
    def skipped_columns(self) -> list[str]:
        return [k for k, v in self.column_mapping.items() if v.canonical == "__skip__"]

    @property
    def unknown_columns(self) -> list[str]:
        return [k for k, v in self.column_mapping.items() if v.canonical == "__unknown__"]

    @property
    def mapped_columns(self) -> dict[str, str]:
        """source_col → canonical field for all successfully mapped columns."""
        return {
            k: v.canonical
            for k, v in self.column_mapping.items()
            if v.canonical not in ("__skip__", "__unknown__")
        }

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialisable dict for saving to disk."""
        return self.model_dump()


# ── JSON extraction ───────────────────────────────────────────────────────────


def _extract_json(text: str) -> str:
    """
    Strip markdown code fences if present and return the bare JSON string.
    Handles ```json ... ```, ``` ... ```, and bare JSON.
    """
    text = text.strip()
    # Remove opening fence (``` or ```json)
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
    # Remove closing fence
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


# ── API call with retry ───────────────────────────────────────────────────────


def _call_api(
    client: anthropic.Anthropic,
    messages: list[dict[str, str]],
    attempt: int,
) -> str:
    """Single API call. Returns raw text response."""
    t0 = time.monotonic()
    response = client.messages.create(
        model=settings.model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    elapsed = time.monotonic() - t0
    log.ai_response(f"Response received in {elapsed:.1f}s (attempt {attempt + 1})")
    return response.content[0].text


def _map_with_retry(
    client: anthropic.Anthropic,
    user_prompt: str,
    source_columns: list[str],
    max_retries: int = 3,
) -> tuple[AIResponseModel, str]:
    """
    Call the AI and parse the JSON response. Retry with structured error
    feedback on JSON/validation failure.

    Returns (AIResponseModel, raw_response_text).
    """
    messages: list[dict[str, str]] = [{"role": "user", "content": user_prompt}]
    last_error: Exception | None = None
    raw: str = ""

    for attempt in range(max_retries):
        if attempt > 0:
            wait = 2 ** (attempt - 1)
            log.warning(f"Retrying in {wait}s (attempt {attempt + 1}/{max_retries})…")
            time.sleep(wait)

        try:
            raw = _call_api(client, messages, attempt)
        except anthropic.APIError as exc:
            last_error = exc
            log.error(f"API error: {exc}")
            # Don't retry on auth errors — they won't resolve
            if isinstance(exc, anthropic.AuthenticationError):
                raise
            messages = messages + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": build_repair_prompt(str(exc))},
            ]
            continue

        # Validate JSON
        try:
            json_str = _extract_json(raw)
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            last_error = exc
            log.warning(f"JSON parse error: {exc}")
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": build_repair_prompt(f"JSONDecodeError: {exc}")},
            ]
            continue

        # Validate against Pydantic model
        try:
            # Ensure every source column is present in column_mapping
            missing = [c for c in source_columns if c not in data.get("column_mapping", {})]
            if missing:
                raise ValueError(
                    f"column_mapping is missing entries for: {missing[:5]}"
                    + (" (and more)" if len(missing) > 5 else "")
                )
            parsed = AIResponseModel.model_validate(data)
            return parsed, raw
        except (ValueError, Exception) as exc:
            last_error = exc
            log.warning(f"Validation error: {exc}")
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": build_repair_prompt(str(exc))},
            ]
            continue

    raise RuntimeError(
        f"AI mapping failed after {max_retries} attempts. Last error: {last_error}"
    )


# ── Public entry point ────────────────────────────────────────────────────────

_SAMPLE_ROWS = 5   # how many data rows to send to the AI


def map_table(
    table: DetectedTable,
    source_file: Path,
    client: anthropic.Anthropic | None = None,
    max_retries: int = 3,
) -> MappingResult:
    """
    Map one DetectedTable's columns via the AI.

    Args:
        table:       the DetectedTable produced by Phase 3
        source_file: path to the original CSV (used for metadata only)
        client:      optional pre-built Anthropic client (for testing)
        max_retries: retry budget for JSON/validation failures

    Returns a MappingResult ready for Phase 5 normalization.
    """
    if client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file."
            )
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Build sample data (raw strings — never coerced)
    sample_rows: list[list[str]] = [
        row.cells for row in table.data_rows[:_SAMPLE_ROWS]
    ]

    log.info(
        f"Sending table {table.table_index} to AI "
        f"({len(table.header_row)} columns, {len(sample_rows)} sample rows)…"
    )

    user_prompt = build_mapping_prompt(
        filename=source_file.name,
        table_index=table.table_index,
        total_tables=1,           # caller fills in total if needed
        section_title=table.section_title,
        header=table.header_row,
        sample_rows=sample_rows,
    )

    parsed, raw = _map_with_retry(
        client=client,
        user_prompt=user_prompt,
        source_columns=table.header_row,
        max_retries=max_retries,
    )

    return MappingResult(
        source_file=source_file.name,
        table_index=table.table_index,
        section_title=table.section_title,
        mapped_at=datetime.now(UTC).isoformat(),
        model_used=settings.model,
        prompt_version=PROMPT_VERSION,
        table_type=parsed.table_type,
        column_mapping=parsed.column_mapping,
        platform_mapping=parsed.platform_mapping,
        warnings=parsed.warnings,
        overall_confidence=parsed.overall_confidence,
        raw_response=raw,
    )
