"""
config.py – Load environment settings from .env or Streamlit Cloud secrets.
All secrets and toggles live here; never hard-coded elsewhere.

Supports two config sources (in priority order):
  1. Streamlit Cloud secrets (st.secrets) — used when deployed
  2. .env file (python-dotenv) — used for local development
"""

import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=False)
except ImportError:
    pass

# Try to load Streamlit secrets (available on Streamlit Community Cloud)
_st_secrets = {}
try:
    import streamlit as st
    _st_secrets = dict(st.secrets)
except Exception:
    pass


def _optional(key: str, default: str = "") -> str:
    """Return config value: Streamlit secrets > env var > default."""
    if key in _st_secrets:
        return str(_st_secrets[key]).strip()
    return os.getenv(key, default).strip()


# ─────────────────────────────────────────────────────────────────────────────
# ESPN Fantasy Dashboard
# ─────────────────────────────────────────────────────────────────────────────

ESPN_S2: str = _optional("ESPN_S2")
ESPN_SWID: str = _optional("ESPN_SWID")
ESPN_LEAGUE_ID: int = int(_optional("ESPN_LEAGUE_ID", "0") or "0")
ESPN_TEAM_ID: int = int(_optional("ESPN_TEAM_ID", "0") or "0")
ESPN_SEASON: int = int(_optional("ESPN_SEASON", "2026") or "2026")

# Dashboard cache
CACHE_DIR: Path = ROOT / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS: int = int(_optional("CACHE_TTL_SECONDS", "600"))

# Projection tuning
WIN_PROB_SIGMA_PCT: float = float(_optional("WIN_PROB_SIGMA_PCT", "0.12"))
PROJECTION_LOOKBACK_DAYS: int = int(_optional("PROJECTION_LOOKBACK_DAYS", "14"))

# League meta
LEAGUE_TYPE: str = _optional("LEAGUE_TYPE", "Points League")
TRANSACTION_CAP: int = int(_optional("TRANSACTION_CAP", "3"))
ROSTER_SLOTS: str = _optional("ROSTER_SLOTS", "PG, SG, G, SF, PF, F, C, UT x3, BE x3, IR x2")

HAS_ESPN: bool = bool(ESPN_S2 and ESPN_SWID and ESPN_LEAGUE_ID)

# ─────────────────────────────────────────────────────────────────────────────
# Player Props (The Odds API)
# ─────────────────────────────────────────────────────────────────────────────

ODDS_API_KEY: str = _optional("ODDS_API_KEY")
HAS_ODDS_API: bool = bool(ODDS_API_KEY)
PROPS_CACHE_TTL: int = int(_optional("PROPS_CACHE_TTL", "86400"))

# Scoring weights: how each stat converts to fantasy points
SCORING_WEIGHTS: dict[str, float] = {
    "PTS": float(_optional("SCORING_PTS", "1")),
    "REB": float(_optional("SCORING_REB", "1")),
    "AST": float(_optional("SCORING_AST", "1.5")),
    "STL": float(_optional("SCORING_STL", "2.5")),
    "BLK": float(_optional("SCORING_BLK", "2.5")),
    "TOV": float(_optional("SCORING_TOV", "-2")),
    "3PM": float(_optional("SCORING_3PM", "1")),
    "FGM": float(_optional("SCORING_FGM", "1")),
    "FGA": float(_optional("SCORING_FGA", "-0.5")),
    "FTM": float(_optional("SCORING_FTM", "0.5")),
    "FTA": float(_optional("SCORING_FTA", "-0.25")),
}

USE_PROPS_PROJECTION: bool = _optional("USE_PROPS_PROJECTION", "true").lower() in ("true", "1", "yes")


def mask_secret(value: str) -> str:
    """Mask a secret for safe logging — show only last 4 chars."""
    if not value or len(value) < 8:
        return "***"
    return "..." + value[-4:]
