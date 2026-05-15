"""
Terminal review workflow — human-in-the-loop correction of AI column mappings.

Design rules:
  - All terminal interaction is injected through prompt_fn so the module is
    fully testable without mocking builtins.
  - run_review() is the only public entry point; it returns a (possibly updated)
    MappingResult and a list of overridden column names.
  - Corrections are written to MappingMemory by the caller (cli.py); this module
    does not touch the memory file directly.
  - For web deployment: replace run_review() with a stateless helper
    get_review_plan() that returns which columns need review, and handle the
    interactive part as API request/response cycles in the frontend.
"""

from __future__ import annotations

from typing import Callable, Optional

from rich.table import Table
from rich.text import Text

from csvnormal import logger as log
from csvnormal.ai_mapper import ColumnMappingEntry, MappingResult
from csvnormal.config import CANONICAL_FIELDS

_VALID_CANONICALS: frozenset[str] = frozenset(CANONICAL_FIELDS) | {"__skip__", "__unknown__"}
_DEFAULT_THRESHOLD: float = 0.75


# ── Pure helpers (web-safe) ───────────────────────────────────────────────────


def get_review_plan(
    mapping: MappingResult,
    threshold: float = _DEFAULT_THRESHOLD,
    review_all: bool = False,
) -> list[str]:
    """
    Return the list of source column names that need human review.
    Pure function — no I/O. Safe to call from an API endpoint.
    """
    return [
        col
        for col, entry in mapping.column_mapping.items()
        if review_all or entry.confidence < threshold
    ]


def apply_override(
    mapping: MappingResult,
    overrides: dict[str, str],
) -> MappingResult:
    """
    Apply a dict of {source_col: new_canonical} overrides to a MappingResult.
    Returns a new MappingResult with updated column_mapping.
    Pure function — no I/O. Safe to call from an API endpoint.
    """
    updated = dict(mapping.column_mapping)
    for col, new_canonical in overrides.items():
        if col in updated and new_canonical in _VALID_CANONICALS:
            updated[col] = ColumnMappingEntry(
                canonical=new_canonical,
                confidence=1.0,
                reason="user override",
            )
    return mapping.model_copy(update={"column_mapping": updated})


# ── Terminal interaction ───────────────────────────────────────────────────────


def _print_full_mapping(mapping: MappingResult) -> None:
    tbl = Table(show_header=True, header_style="bold cyan", title="Column Mappings")
    tbl.add_column("Source column",   width=28)
    tbl.add_column("→ Canonical",     width=22)
    tbl.add_column("Conf",            width=6,  justify="right")
    tbl.add_column("Source",          width=8)
    tbl.add_column("Reason",          width=32)

    for col, entry in mapping.column_mapping.items():
        if entry.canonical == "__skip__":
            style, tag = "dim", "skip"
        elif entry.canonical == "__unknown__":
            style, tag = "yellow", "?"
        elif entry.confidence < 0.75:
            style, tag = "red", "⚠"
        elif entry.confidence < 0.90:
            style, tag = "yellow", "~"
        else:
            style, tag = "green", "✓"

        tbl.add_row(
            col,
            entry.canonical,
            f"{entry.confidence:.2f}",
            tag,
            entry.reason or "",
            style=style,
        )

    log.console.print(tbl)


def _review_one_column(
    source_col: str,
    current: ColumnMappingEntry,
    ask: Callable[[str], str],
) -> ColumnMappingEntry:
    """Prompt user for a single column. Returns unchanged entry if accepted."""
    log.console.print(
        f"\n  [bold]Column:[/bold] [cyan]{source_col}[/cyan]\n"
        f"  Suggestion: [green]{current.canonical}[/green]  "
        f"confidence {current.confidence:.2f}  |  {current.reason or '—'}"
    )
    log.console.print(
        f"  [dim]({', '.join(CANONICAL_FIELDS[:12])} … __skip__ __unknown__)[/dim]"
    )
    raw = ask("  Accept (Enter) or type a new canonical field: ").strip()

    if not raw:
        return current

    if raw in _VALID_CANONICALS:
        return ColumnMappingEntry(canonical=raw, confidence=1.0, reason="user override")

    log.warning(f"  '{raw}' is not a valid canonical field — keeping current.")
    return current


# ── Public entry point ────────────────────────────────────────────────────────


def run_review(
    mapping: MappingResult,
    threshold: float = _DEFAULT_THRESHOLD,
    review_all: bool = False,
    prompt_fn: Optional[Callable[[str], str]] = None,
) -> tuple[MappingResult, list[str]]:
    """
    Interactive terminal review of a MappingResult.

    Args:
        mapping:    MappingResult from Phase 4 AI mapping
        threshold:  columns with confidence < threshold are flagged
        review_all: if True, review every column regardless of confidence
        prompt_fn:  injectable input function (defaults to input(); replaced in tests)

    Returns:
        (updated_mapping, list_of_overridden_column_names)
    """
    ask = prompt_fn or (lambda msg: input(msg))

    _print_full_mapping(mapping)

    flagged = get_review_plan(mapping, threshold, review_all)

    if not flagged:
        log.success("All mappings are above the confidence threshold — nothing to review.")
        return mapping, []

    log.blank()
    log.warning(
        f"{len(flagged)} column(s) have confidence < {threshold:.0%}  "
        f"(shown in [red]red[/red] above)."
    )
    log.console.print(
        "\n  [bold]Options:[/bold]  "
        "[a] accept all   [r] review flagged columns   [q] quit without saving\n"
    )
    choice = ask("  > ").strip().lower()

    if choice == "q":
        log.info("Review cancelled — mapping unchanged.")
        return mapping, []

    if choice != "r":
        log.info("Accepted all mappings as-is.")
        return mapping, []

    # ── Review flagged columns one by one ─────────────────────────────────────
    updated_cols = dict(mapping.column_mapping)
    overridden: list[str] = []

    for col in flagged:
        original = updated_cols[col]
        updated = _review_one_column(col, original, ask)
        if updated.canonical != original.canonical:
            overridden.append(col)
        updated_cols[col] = updated

    log.blank()
    if overridden:
        log.success(
            f"Review complete — {len(overridden)} override(s): "
            + ", ".join(f"[cyan]{c}[/cyan]" for c in overridden)
        )
    else:
        log.success("Review complete — no changes made.")

    return mapping.model_copy(update={"column_mapping": updated_cols}), overridden
