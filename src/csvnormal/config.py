"""Canonical schema definition and application settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"
FIXTURES_DIR = ROOT / "fixtures"
MEMORY_FILE = ROOT / "mappings_memory.json"

OUTPUT_DIR.mkdir(exist_ok=True)

# ── Canonical schema ──────────────────────────────────────────────────────────

CANONICAL_FIELDS: list[str] = [
    "client",
    "platform",
    "campaign",
    "date",
    "spend",
    "impressions",
    "clicks",
    "conversions",
    "currency",
]

REQUIRED_FIELDS: list[str] = ["client", "platform", "spend"]

NUMERIC_FIELDS: list[str] = ["spend", "impressions", "clicks", "conversions"]

# ── Platform normalization lookup ─────────────────────────────────────────────

PLATFORM_ALIASES: dict[str, str] = {
    # Meta / Facebook / Instagram
    "facebook": "meta",
    "fb": "meta",
    "fb ads": "meta",
    "meta": "meta",
    "meta ads": "meta",
    "instagram": "meta",
    "ig": "meta",
    # Google
    "google": "google",
    "google ads": "google",
    "gads": "google",
    "adwords": "google",
    "google adwords": "google",
    # TikTok
    "tiktok": "tiktok",
    "tik tok": "tiktok",
    "tt": "tiktok",
    # LinkedIn
    "linkedin": "linkedin",
    "li": "linkedin",
    # Twitter / X
    "twitter": "twitter",
    "x": "twitter",
    # Snapchat
    "snapchat": "snapchat",
    "snap": "snapchat",
    # Pinterest
    "pinterest": "pinterest",
    "pin": "pinterest",
    # Display / Programmatic
    "dv360": "dv360",
    "display video 360": "dv360",
    "programmatic": "programmatic",
    "ttd": "programmatic",
    "the trade desk": "programmatic",
}

# ── AI model settings ─────────────────────────────────────────────────────────

AI_MODEL: str = os.getenv("CSVNORMAL_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# ── Pydantic settings model ────────────────────────────────────────────────────


class AppSettings(BaseModel):
    model: str = Field(default=AI_MODEL)
    log_level: str = Field(default=os.getenv("CSVNORMAL_LOG_LEVEL", "INFO"))
    anthropic_api_key: str = Field(default=ANTHROPIC_API_KEY)
    output_dir: Path = Field(default=OUTPUT_DIR)
    memory_file: Path = Field(default=MEMORY_FILE)


settings = AppSettings()
