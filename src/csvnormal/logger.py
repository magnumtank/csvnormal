"""Centralized rich-based logging for CSVNormal."""

import os
from enum import Enum

from rich.console import Console
from rich.theme import Theme

_THEME = Theme(
    {
        "info": "bold cyan",
        "debug": "dim white",
        "warning": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "ai": "bold magenta",
        "label": "bold white",
        "value": "white",
    }
)

console = Console(theme=_THEME, highlight=False)


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


def _active_level() -> LogLevel:
    raw = os.getenv("CSVNORMAL_LOG_LEVEL", "INFO").upper()
    try:
        return LogLevel(raw)
    except ValueError:
        return LogLevel.INFO


_LEVEL_ORDER = {LogLevel.DEBUG: 0, LogLevel.INFO: 1, LogLevel.WARNING: 2, LogLevel.ERROR: 3}


def _should_log(level: LogLevel) -> bool:
    return _LEVEL_ORDER[level] >= _LEVEL_ORDER[_active_level()]


def info(msg: str) -> None:
    if _should_log(LogLevel.INFO):
        console.print(f"[info]INFO[/info]  {msg}")


def debug(msg: str) -> None:
    if _should_log(LogLevel.DEBUG):
        console.print(f"[debug]DEBUG[/debug] {msg}")


def warning(msg: str) -> None:
    if _should_log(LogLevel.WARNING):
        console.print(f"[warning]WARN[/warning]  {msg}")


def error(msg: str) -> None:
    if _should_log(LogLevel.ERROR):
        console.print(f"[error]ERROR[/error] {msg}")


def ai_response(msg: str) -> None:
    if _should_log(LogLevel.INFO):
        console.print(f"[ai]AI   [/ai]  {msg}")


def success(msg: str) -> None:
    if _should_log(LogLevel.INFO):
        console.print(f"[success]OK   [/success] {msg}")


def rule(title: str = "") -> None:
    console.rule(f"[label]{title}[/label]" if title else "")


def blank() -> None:
    console.print()
