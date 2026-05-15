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

# ── Default currency ──────────────────────────────────────────────────────────

DEFAULT_CURRENCY: str = "INR"

# ── Canonical schema — grouped by category ────────────────────────────────────
#
# Rules:
#   • All numeric values (counts, money, rates) are NEVER modified — exact
#     source string is preserved end-to-end.
#   • Fields absent from the source CSV are left as empty strings in output.
#   • Rate/efficiency fields (CTR, ROAS, etc.) are preserved as-is from source;
#     the system never calculates or derives them.

FIELD_GROUPS: dict[str, list[str]] = {
    "Identity": [
        "client",       # client / advertiser name, set per file
        "platform",     # normalised key: meta | google | tiktok | ...
        "campaign",     # campaign name
        "ad_set",       # ad set / ad group name
        "ad_name",      # individual ad name
        "placement",    # placement label (Feed, Stories, Search, etc.)
    ],
    "Time": [
        "date",         # row-level date (ISO 8601: YYYY-MM-DD)
        "start_date",   # period start when no row-level date
        "end_date",     # period end when no row-level date
        "month",        # month label (e.g. March 2024) resolved from filename/prompt
        "year",         # year resolved from filename/prompt
        "week",         # ISO week (e.g. 2024-W10)
    ],
    "Budget": [
        "spend",        # actual media spend — NEVER modified
        "budget",       # planned/allocated budget — NEVER modified
        "currency",     # ISO 4217 code, default INR
    ],
    "Delivery": [
        "impressions",  # total ad impressions — NEVER modified
        "reach",        # unique users reached — NEVER modified
        "frequency",    # avg impressions per user — NEVER modified
    ],
    "Clicks": [
        "clicks",       # total clicks — NEVER modified
        "link_clicks",  # link / outbound clicks only — NEVER modified
    ],
    "Engagement": [
        "engagements",       # total engagements (likes+comments+shares+saves) — NEVER modified
        "likes",             # NEVER modified
        "comments",          # NEVER modified
        "shares",            # NEVER modified
        "saves",             # NEVER modified
        "follows",           # new follows from ad — NEVER modified
    ],
    "Video": [
        "video_views",        # total video views — NEVER modified
        "video_completions",  # 100% completion views — NEVER modified
        "three_second_views", # 3-sec views (Meta) — NEVER modified
        "thruplay",           # ThruPlay completions (Meta) — NEVER modified
    ],
    "Funnel": [
        "conversions",    # total conversion events — NEVER modified
        "leads",          # lead form submissions — NEVER modified
        "add_to_cart",    # add-to-cart events — NEVER modified
        "checkouts",      # checkout initiations — NEVER modified
        "purchases",      # purchase events — NEVER modified
        "app_installs",   # app installs — NEVER modified
        "sign_ups",       # sign-up events — NEVER modified
    ],
    "Revenue": [
        "revenue",           # total attributed revenue — NEVER modified
        "conversion_value",  # raw conversion value from platform — NEVER modified
    ],
    "Efficiency": [
        # All rate/efficiency fields are preserved exactly as-is from source.
        # The system NEVER calculates these; if absent from source they are blank.
        "ctr",            # click-through rate (%)
        "cpm",            # cost per mille (per 1,000 impressions)
        "cpc",            # cost per click
        "cvr",            # conversion rate (%)
        "cpa",            # cost per action / acquisition
        "cpl",            # cost per lead
        "cpp",            # cost per purchase
        "cpv",            # cost per video view
        "roas",           # return on ad spend (revenue / spend)
        "vtr",            # video view-through rate (%)
        "engagement_rate",  # engagement rate (%)
    ],
    "Quality": [
        "quality_score",            # Google Ads quality score (1–10)
        "relevance_score",          # Meta ad relevance score
        "search_impression_share",  # Google search impression share (%)
    ],
}

# Flat ordered list derived from groups — preserves group order
CANONICAL_FIELDS: list[str] = [f for fields in FIELD_GROUPS.values() for f in fields]

REQUIRED_FIELDS: list[str] = ["client", "platform", "spend"]

# Raw count / money fields — exact string preservation enforced
NUMERIC_FIELDS: list[str] = [
    "spend", "budget", "impressions", "reach", "frequency",
    "clicks", "link_clicks",
    "engagements", "likes", "comments", "shares", "saves", "follows",
    "video_views", "video_completions", "three_second_views", "thruplay",
    "conversions", "leads", "add_to_cart", "checkouts", "purchases",
    "app_installs", "sign_ups",
    "revenue", "conversion_value",
]

# Rate / efficiency fields — also preserved exactly as-is (never computed)
RATE_FIELDS: list[str] = [
    "ctr", "cpm", "cpc", "cvr", "cpa", "cpl", "cpp", "cpv",
    "roas", "vtr", "engagement_rate",
    "quality_score", "relevance_score", "search_impression_share",
]

# Union: every field whose numeric value must not change
ALL_PRESERVE_FIELDS: list[str] = NUMERIC_FIELDS + RATE_FIELDS

# ── Field metadata for schema display ────────────────────────────────────────

FIELD_NOTES: dict[str, str] = {
    "client": "Advertiser / client name — set per file",
    "platform": "Normalised key: meta | google | tiktok | linkedin | …",
    "campaign": "Campaign name from source",
    "ad_set": "Ad set / ad group name",
    "ad_name": "Individual ad name / creative label",
    "placement": "Feed, Stories, Search, Display, etc.",
    "date": "Row-level date — ISO 8601 (YYYY-MM-DD)",
    "start_date": "Period start — used when no row-level date",
    "end_date": "Period end — used when no row-level date",
    "month": "Resolved from filename or user prompt (e.g. March 2024)",
    "year": "Resolved from filename or user prompt",
    "week": "ISO week string (e.g. 2024-W10)",
    "spend": "Actual media cost — NEVER modified",
    "budget": "Planned / allocated budget — NEVER modified",
    "currency": f"ISO 4217 code — default {DEFAULT_CURRENCY}",
    "impressions": "Total ad impressions — NEVER modified",
    "reach": "Unique users reached — NEVER modified",
    "frequency": "Avg impressions per unique user — NEVER modified",
    "clicks": "Total clicks — NEVER modified",
    "link_clicks": "Outbound / link clicks only — NEVER modified",
    "engagements": "Total engagement events — NEVER modified",
    "likes": "Post likes — NEVER modified",
    "comments": "Post comments — NEVER modified",
    "shares": "Post shares — NEVER modified",
    "saves": "Post saves — NEVER modified",
    "follows": "New follows from ad — NEVER modified",
    "video_views": "Total video views — NEVER modified",
    "video_completions": "100% video completions — NEVER modified",
    "three_second_views": "3-second video views (Meta) — NEVER modified",
    "thruplay": "ThruPlay completions (Meta) — NEVER modified",
    "conversions": "Total conversion events — NEVER modified",
    "leads": "Lead form submissions — NEVER modified",
    "add_to_cart": "Add-to-cart events — NEVER modified",
    "checkouts": "Checkout initiations — NEVER modified",
    "purchases": "Purchase events — NEVER modified",
    "app_installs": "App install events — NEVER modified",
    "sign_ups": "Sign-up / registration events — NEVER modified",
    "revenue": "Total attributed revenue — NEVER modified",
    "conversion_value": "Raw conversion value from platform — NEVER modified",
    "ctr": "Click-through rate (%) — preserved as-is, never computed",
    "cpm": "Cost per 1,000 impressions — preserved as-is, never computed",
    "cpc": "Cost per click — preserved as-is, never computed",
    "cvr": "Conversion rate (%) — preserved as-is, never computed",
    "cpa": "Cost per action / acquisition — preserved as-is, never computed",
    "cpl": "Cost per lead — preserved as-is, never computed",
    "cpp": "Cost per purchase — preserved as-is, never computed",
    "cpv": "Cost per video view — preserved as-is, never computed",
    "roas": "Return on ad spend (revenue ÷ spend) — preserved as-is, never computed",
    "vtr": "Video view-through rate (%) — preserved as-is, never computed",
    "engagement_rate": "Engagement rate (%) — preserved as-is, never computed",
    "quality_score": "Google Ads quality score (1–10) — preserved as-is",
    "relevance_score": "Meta ad relevance / quality score — preserved as-is",
    "search_impression_share": "Google search impression share (%) — preserved as-is",
}

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
    "ig stories": "meta",
    "fb stories": "meta",
    # Google
    "google": "google",
    "google ads": "google",
    "gads": "google",
    "adwords": "google",
    "google adwords": "google",
    "google search": "google",
    "google display": "google",
    "gdn": "google",
    # TikTok
    "tiktok": "tiktok",
    "tik tok": "tiktok",
    "tt": "tiktok",
    "tiktok ads": "tiktok",
    # LinkedIn
    "linkedin": "linkedin",
    "li": "linkedin",
    "linkedin ads": "linkedin",
    # Twitter / X
    "twitter": "twitter",
    "x": "twitter",
    "twitter ads": "twitter",
    # Snapchat
    "snapchat": "snapchat",
    "snap": "snapchat",
    "snapchat ads": "snapchat",
    # Pinterest
    "pinterest": "pinterest",
    "pin": "pinterest",
    "pinterest ads": "pinterest",
    # YouTube
    "youtube": "youtube",
    "yt": "youtube",
    "youtube ads": "youtube",
    # Display / Programmatic
    "dv360": "dv360",
    "display video 360": "dv360",
    "programmatic": "programmatic",
    "ttd": "programmatic",
    "the trade desk": "programmatic",
    # Apple
    "apple search ads": "apple",
    "asa": "apple",
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
    default_currency: str = Field(default=DEFAULT_CURRENCY)


settings = AppSettings()
