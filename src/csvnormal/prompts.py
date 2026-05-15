"""
Prompt templates for the AI mapping layer.

Design rules:
  - Pure functions only — no API calls, no side effects, no imports of anthropic.
  - All inputs are plain Python types so prompts are trivially testable.
  - The system prompt is versioned so audits can trace which prompt produced a mapping.
"""

from __future__ import annotations

from csvnormal.config import CANONICAL_FIELDS, CANONICAL_PLATFORMS, FIELD_GROUPS

PROMPT_VERSION = "v1"

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a precise data normalization assistant for performance marketing CSV files.
Your sole task is to produce a JSON column mapping from inconsistent source headers to a canonical schema.

Critical rules:
1. Map EVERY source column to exactly one of:
   - A canonical field name from the provided list
   - "__skip__"    → duplicate/redundant columns, row labels, units, notes
   - "__unknown__" → genuinely cannot determine (use sparingly)
2. Use ONLY canonical field names from the list — never invent new ones.
3. Platform values must map to the allowed platform keys listed — or "__unknown__".
4. Return ONLY the JSON object — no explanations, no markdown code fences, no extra text.
5. Confidence values must be floats between 0.0 and 1.0.
6. The "reason" field should be a short phrase (< 12 words).

Prompt version: {PROMPT_VERSION}"""

# ── Field list formatter ──────────────────────────────────────────────────────


def _format_canonical_fields() -> str:
    """Return a grouped, human-readable list of all canonical fields."""
    lines: list[str] = []
    for group, fields in FIELD_GROUPS.items():
        lines.append(f"  [{group}]")
        lines.append("    " + ", ".join(fields))
    return "\n".join(lines)


# ── Sample row formatter ──────────────────────────────────────────────────────


def _format_sample_rows(header: list[str], sample_rows: list[list[str]]) -> str:
    """Format header + sample rows as a compact pipe-delimited table."""
    if not sample_rows:
        return "  (no sample rows available)"

    # Cap column width for readability
    col_width = 20

    def _cell(s: str) -> str:
        s = s.strip()
        return s[:col_width].ljust(col_width) if len(s) <= col_width else s[:col_width - 1] + "…"

    header_line = "| " + " | ".join(_cell(h) for h in header) + " |"
    sep_line    = "|-" + "-|-".join("-" * col_width for _ in header) + "-|"
    data_lines  = [
        "| " + " | ".join(_cell(c if i < len(row) else "") for i, c in enumerate(header)) + " |"
        for row in sample_rows
        for c in [row]   # just to bind `row`
    ]
    # Rebuild properly
    data_lines = []
    for row in sample_rows:
        padded = [row[i] if i < len(row) else "" for i in range(len(header))]
        data_lines.append("| " + " | ".join(_cell(c) for c in padded) + " |")

    return "\n".join([header_line, sep_line] + data_lines)


# ── Unique categorical value extractor ────────────────────────────────────────


def _unique_categoricals(
    header: list[str],
    sample_rows: list[list[str]],
    max_unique: int = 15,
) -> dict[str, list[str]]:
    """
    Return unique values for columns that look categorical (text, few distinct values).
    Used to help the AI identify platform names and other categorical fields.
    """
    result: dict[str, list[str]] = {}
    for col_idx, col_name in enumerate(header):
        values: list[str] = []
        for row in sample_rows:
            if col_idx < len(row):
                v = row[col_idx].strip()
                if v and v not in values:
                    values.append(v)

        # Skip if all values look numeric
        non_numeric = [v for v in values if not _looks_numeric(v)]
        if not non_numeric:
            continue

        # Only include if few enough distinct values to be meaningful
        if 1 <= len(non_numeric) <= max_unique:
            result[col_name] = non_numeric[:max_unique]

    return result


def _looks_numeric(value: str) -> bool:
    try:
        float(value.replace(",", ""))
        return True
    except ValueError:
        return False


# ── JSON schema template ──────────────────────────────────────────────────────

_MAPPING_SCHEMA = """{
  "table_type": "<one of: performance | budget | creative | reach | unknown>",
  "column_mapping": {
    "<source_column_name>": {
      "canonical": "<canonical_field | __skip__ | __unknown__>",
      "confidence": <0.0-1.0>,
      "reason": "<brief phrase>"
    }
  },
  "platform_mapping": {
    "<source_platform_value>": {
      "canonical": "<canonical_platform_key | __unknown__>",
      "confidence": <0.0-1.0>
    }
  },
  "warnings": ["<any issues or ambiguities you noticed>"],
  "overall_confidence": <0.0-1.0>
}"""

# ── Main prompt builder ───────────────────────────────────────────────────────


def build_mapping_prompt(
    filename: str,
    table_index: int,
    total_tables: int,
    section_title: str | None,
    header: list[str],
    sample_rows: list[list[str]],
) -> str:
    """
    Build the user-turn message for the AI column mapping call.

    Args:
        filename:      source file name (not full path — no sensitive data)
        table_index:   0-based table index within the file
        total_tables:  total number of tables detected in the file
        section_title: optional section heading detected above the table
        header:        list of source column names
        sample_rows:   list of raw string rows (cells as strings)
    """
    section_part = f'"{section_title}"' if section_title else "(untitled)"
    platform_keys = ", ".join(CANONICAL_PLATFORMS)

    categorical = _unique_categoricals(header, sample_rows)
    cat_lines: list[str] = []
    for col_name, values in categorical.items():
        cat_lines.append(f'  Column "{col_name}": {values}')
    cat_block = "\n".join(cat_lines) if cat_lines else "  (none detected)"

    return f"""## Canonical field names (map source columns to these)
{_format_canonical_fields()}

## Canonical platform keys
{platform_keys}

## Source table to map
File        : {filename}
Table       : {table_index} of {total_tables - 1} (0-based) — {section_part}
Columns ({len(header)}): {header}

## Sample data (first rows)
{_format_sample_rows(header, sample_rows)}

## Unique values in text columns (use for platform_mapping)
{cat_block}

## Required JSON output
Return this exact JSON structure and nothing else:
{_MAPPING_SCHEMA}"""


# ── Retry repair prompt ───────────────────────────────────────────────────────


def build_repair_prompt(error_message: str) -> str:
    """Follow-up message when the AI returns invalid JSON."""
    return (
        f"Your previous response had a validation error:\n\n"
        f"  {error_message}\n\n"
        f"Please return ONLY the corrected JSON object with no additional text. "
        f"Every source column MUST appear in column_mapping."
    )
