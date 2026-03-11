"""
props.py  –  Sportsbook Player Props Integration (The Odds API)

Fetches daily NBA player prop lines (PTS, REB, AST, STL, BLK) from
The Odds API and converts them to projected fantasy points using the
league's scoring weights.

Usage:
    from app.props import PropsClient

    client = PropsClient(api_key="...", scoring_weights={...})
    props = client.get_today_props()          # {player_name: fpts_estimate}
    fpts  = client.estimate_fpts("LeBron James")  # single player lookup
"""

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─── Default ESPN Points League scoring weights ─────────────────────────────
# These map The Odds API market names to fantasy point values.
# Override via SCORING_WEIGHTS in config or .env.

DEFAULT_SCORING_WEIGHTS = {
    "PTS": 1.0,
    "REB": 1.0,
    "AST": 1.5,
    "STL": 2.5,
    "BLK": 2.5,
    "TOV": -2.0,
    "3PM": 1.0,
    "FGM": 1.0,
    "FGA": -0.5,
    "FTM": 0.5,
    "FTA": -0.25,
}

# Map The Odds API market keys to our stat abbreviations
_ODDS_API_MARKET_MAP = {
    "player_points": "PTS",
    "player_rebounds": "REB",
    "player_assists": "AST",
    "player_steals": "STL",
    "player_blocks": "BLK",
    "player_threes": "3PM",
    "player_turnovers": "TOV",
}

# Markets to request from The Odds API
_PROP_MARKETS = [
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_steals",
    "player_blocks",
    "player_threes",
]

# ─── FGM/FGA/FTM/FTA estimation from available props ────────────────────────
# Sportsbooks don't offer O/U lines for FGM/FGA/FTM/FTA, so we estimate them
# from PTS and 3PM using NBA averages:
#   - League avg eFG% ≈ 54%, so FGA ≈ PTS / (2 * 0.54) ≈ PTS * 0.926
#   - FGM ≈ FGA * 0.47 (league avg FG%)
#   - FTA ≈ PTS * 0.23 (league avg FTA rate relative to points)
#   - FTM ≈ FTA * 0.78 (league avg FT%)
# These are rough but better than ignoring these categories entirely.

def _estimate_shooting_stats(pts: float, threes: float) -> dict[str, float]:
    """
    Estimate FGM/FGA/FTM/FTA from a player's PTS and 3PM prop lines.

    Uses simplified NBA averages to back-calculate shooting stats.
    Not perfect, but captures the general magnitude for fantasy scoring.
    """
    if pts <= 0:
        return {}

    # Estimate FTA/FTM first (free throw component of scoring)
    fta = round(pts * 0.23, 1)       # ~23% of points come via FTA
    ftm = round(fta * 0.78, 1)       # ~78% FT%
    ft_points = ftm                   # each FTM = 1 point

    # Remaining points from field goals
    fg_points = pts - ft_points
    # FGM: 2-pointers worth 2, 3-pointers worth 3
    # fg_points = (FGM - 3PM) * 2 + 3PM * 3 = 2*FGM + 3PM
    # So FGM = (fg_points - 3PM) / 2
    fgm = round((fg_points - threes) / 2, 1) if fg_points > threes else 0.0

    # FGA: use ~47% league avg FG%
    fga = round(fgm / 0.47, 1) if fgm > 0 else 0.0

    return {
        "FGM": max(fgm, 0),
        "FGA": max(fga, 0),
        "FTM": max(ftm, 0),
        "FTA": max(fta, 0),
    }


class PropsClient:
    """
    Fetches NBA player prop lines from The Odds API and converts to fantasy points.

    Parameters
    ----------
    api_key : str
        The Odds API key (free tier: 500 requests/month).
    scoring_weights : dict
        {stat_abbrev: points_per_unit} – e.g. {"PTS": 1, "REB": 1, "STL": 2, ...}
    cache_dir : Path
        Directory for caching prop data (avoids burning API quota).
    cache_ttl : int
        Cache time-to-live in seconds (default: 3600 = 1 hour).
    preferred_books : list[str]
        Sportsbooks to prefer for lines, in priority order.
        Default: ["draftkings", "fanduel", "bovada"]
    """

    BASE_URL = "https://api.the-odds-api.com/v4"
    SPORT = "basketball_nba"

    def __init__(
        self,
        api_key: str,
        scoring_weights: dict = None,
        cache_dir: Path = None,
        cache_ttl: int = 3600,
        preferred_books: list[str] = None,
    ):
        self.api_key = api_key
        self.weights = scoring_weights or DEFAULT_SCORING_WEIGHTS.copy()
        self.cache_dir = cache_dir or Path("cache/props")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl = cache_ttl
        self.preferred_books = preferred_books or [
            "draftkings", "fanduel", "bovada", "betmgm"
        ]
        # In-memory cache for the current session
        self._props_cache: dict[str, float] = {}
        self._cache_timestamp: float = 0
        self._remaining_requests: Optional[int] = None

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_today_props(self, force_refresh: bool = False) -> dict[str, dict]:
        """
        Fetch all available player prop lines for today's NBA games.

        Returns
        -------
        dict[str, dict]
            {normalized_player_name: {
                "fpts_estimate": float,   # estimated fantasy points
                "props": {stat: line},    # individual stat lines
                "source": str,            # sportsbook used
                "game": str,              # "LAL vs BOS" style
            }}
        """
        # Check memory cache
        if not force_refresh and self._props_cache and (
            time.time() - self._cache_timestamp < self.cache_ttl
        ):
            return self._props_cache

        # Check disk cache
        disk_data = self._read_disk_cache()
        if not force_refresh and disk_data is not None:
            self._props_cache = disk_data
            self._cache_timestamp = time.time()
            return disk_data

        # Fetch fresh data
        try:
            props_data = self._fetch_all_props()
            self._props_cache = props_data
            self._cache_timestamp = time.time()
            self._write_disk_cache(props_data)
            return props_data
        except Exception as e:
            logger.error("Failed to fetch props: %s", e)
            # Return stale cache if available
            if self._props_cache:
                logger.info("Returning stale props cache")
                return self._props_cache
            return {}

    def estimate_fpts(self, player_name: str) -> Optional[float]:
        """
        Get the estimated fantasy points for a single player based on prop lines.

        Returns None if no prop data is available for this player.
        """
        props = self.get_today_props()
        normalized = self._normalize_name(player_name)

        # Try exact match first
        if normalized in props:
            return props[normalized].get("fpts_estimate")

        # Try fuzzy match (last name)
        last_name = normalized.split()[-1] if " " in normalized else normalized
        for pname, pdata in props.items():
            if pname.endswith(last_name):
                return pdata.get("fpts_estimate")

        return None

    def get_player_props(self, player_name: str) -> Optional[dict]:
        """Get full prop data for a single player (lines + estimate)."""
        props = self.get_today_props()
        normalized = self._normalize_name(player_name)

        if normalized in props:
            return props[normalized]

        # Fuzzy match
        last_name = normalized.split()[-1] if " " in normalized else normalized
        for pname, pdata in props.items():
            if pname.endswith(last_name):
                return pdata

        return None

    def get_remaining_requests(self) -> Optional[int]:
        """Return remaining API requests this month (from last response header)."""
        return self._remaining_requests

    # ─── Fetching ────────────────────────────────────────────────────────────

    def _fetch_all_props(self) -> dict[str, dict]:
        """Fetch props for all today's games from The Odds API."""
        import urllib.request
        import urllib.error
        import urllib.parse

        # Step 1: Get today's events (games)
        events = self._fetch_events()
        if not events:
            logger.info("No NBA events found for today")
            return {}

        logger.info("Found %d NBA events for today", len(events))

        # Step 2: For each event, fetch player props
        all_props: dict[str, dict] = {}

        for event in events:
            event_id = event.get("id")
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            game_label = f"{away} @ {home}"

            try:
                event_props = self._fetch_event_props(event_id)
                parsed = self._parse_event_props(event_props, game_label)
                all_props.update(parsed)
            except Exception as e:
                logger.warning("Failed to fetch props for %s: %s", game_label, e)

        logger.info("Fetched props for %d players", len(all_props))
        return all_props

    def _fetch_events(self) -> list[dict]:
        """Fetch today's NBA events from The Odds API."""
        import urllib.request
        import urllib.error

        # Use dateFormat=iso and filter for today
        params = {
            "apiKey": self.api_key,
            "dateFormat": "iso",
        }
        url = f"{self.BASE_URL}/sports/{self.SPORT}/events?{self._urlencode(params)}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                self._update_quota(resp)
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ValueError("Invalid Odds API key. Check ODDS_API_KEY in .env")
            raise

        # Filter for events happening today (UTC)
        today = date.today()
        today_events = []
        for event in data:
            commence = event.get("commence_time", "")
            try:
                event_date = datetime.fromisoformat(
                    commence.replace("Z", "+00:00")
                ).date()
                if event_date == today:
                    today_events.append(event)
            except (ValueError, AttributeError):
                pass

        return today_events

    def _fetch_event_props(self, event_id: str) -> dict:
        """Fetch player prop odds for a specific event."""
        import urllib.request
        import urllib.error

        markets_str = ",".join(_PROP_MARKETS)
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": markets_str,
            "oddsFormat": "american",
        }
        url = (
            f"{self.BASE_URL}/sports/{self.SPORT}/events/{event_id}/odds"
            f"?{self._urlencode(params)}"
        )

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            self._update_quota(resp)
            return json.loads(resp.read().decode())

    def _parse_event_props(self, event_data: dict, game_label: str) -> dict[str, dict]:
        """
        Parse The Odds API response into per-player prop lines.

        The response has bookmakers -> markets -> outcomes structure.
        We extract the Over line for each player/stat combo.
        """
        players: dict[str, dict] = {}  # {name: {"props": {stat: line}, ...}}

        bookmakers = event_data.get("bookmakers", [])

        # Sort bookmakers by our preference order
        def book_priority(b):
            key = b.get("key", "")
            try:
                return self.preferred_books.index(key)
            except ValueError:
                return 999

        bookmakers_sorted = sorted(bookmakers, key=book_priority)

        for book in bookmakers_sorted:
            book_key = book.get("key", "unknown")

            for market in book.get("markets", []):
                market_key = market.get("key", "")
                stat = _ODDS_API_MARKET_MAP.get(market_key)
                if not stat:
                    continue

                for outcome in market.get("outcomes", []):
                    # We want the Over line — that's the projected total
                    if outcome.get("name") != "Over":
                        continue

                    player_name = outcome.get("description", "")
                    line = outcome.get("point", 0.0)

                    if not player_name or line <= 0:
                        continue

                    normalized = self._normalize_name(player_name)

                    if normalized not in players:
                        players[normalized] = {
                            "props": {},
                            "source": book_key,
                            "game": game_label,
                            "raw_name": player_name,
                        }

                    # Only set if not already set (first book in priority wins)
                    if stat not in players[normalized]["props"]:
                        players[normalized]["props"][stat] = line

        # Calculate fantasy point estimates and add estimated shooting stats
        for name, pdata in players.items():
            pts = pdata["props"].get("PTS", 0)
            threes = pdata["props"].get("3PM", 0)
            if pts > 0 and "FGM" not in pdata["props"]:
                shooting_est = _estimate_shooting_stats(pts, threes)
                pdata["props"].update(shooting_est)
            pdata["fpts_estimate"] = self._calculate_fpts(pdata["props"])

        return players

    def _calculate_fpts(self, props: dict[str, float]) -> float:
        """
        Convert individual stat props into an estimated fantasy point total.

        Uses the league scoring weights applied to each prop line.
        The Over line roughly equals the sportsbook's projected total for that stat.

        For stats not available as props (FGM/FGA/FTM/FTA), estimates them
        from PTS and 3PM using NBA league averages.
        """
        # Start with direct prop stats
        all_stats = dict(props)

        # Estimate FGM/FGA/FTM/FTA from PTS and 3PM if not already present
        pts = all_stats.get("PTS", 0)
        threes = all_stats.get("3PM", 0)
        if pts > 0 and "FGM" not in all_stats:
            shooting_est = _estimate_shooting_stats(pts, threes)
            all_stats.update(shooting_est)

        total = 0.0
        for stat, line in all_stats.items():
            weight = self.weights.get(stat, 0.0)
            total += line * weight
        return round(total, 1)

    # ─── Caching ─────────────────────────────────────────────────────────────

    def _disk_cache_path(self) -> Path:
        return self.cache_dir / f"props_{date.today().isoformat()}.json"

    def _read_disk_cache(self) -> Optional[dict]:
        """Read today's cached props from disk if fresh enough."""
        path = self._disk_cache_path()
        if not path.exists():
            return None

        try:
            mtime = path.stat().st_mtime
            if time.time() - mtime > self.cache_ttl:
                return None
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def _write_disk_cache(self, data: dict):
        """Write props data to disk cache."""
        try:
            # Clean up old cache files (keep last 3 days)
            for old_file in self.cache_dir.glob("props_*.json"):
                try:
                    file_date = old_file.stem.replace("props_", "")
                    if date.fromisoformat(file_date) < date.today() - timedelta(days=3):
                        old_file.unlink()
                except (ValueError, OSError):
                    pass

            with open(self._disk_cache_path(), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to write props cache: %s", e)

    # ─── Utilities ───────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize player name for matching: lowercase, strip suffixes."""
        name = name.strip().lower()
        # Remove common suffixes
        for suffix in (" jr.", " jr", " sr.", " sr", " iii", " ii", " iv"):
            if name.endswith(suffix):
                name = name[: -len(suffix)].strip()
        return name

    @staticmethod
    def _urlencode(params: dict) -> str:
        import urllib.parse
        return urllib.parse.urlencode(params)

    def _update_quota(self, response):
        """Track remaining API requests from response headers."""
        remaining = response.headers.get("x-requests-remaining")
        if remaining is not None:
            try:
                self._remaining_requests = int(remaining)
            except ValueError:
                pass


# ─── Name matching utility ──────────────────────────────────────────────────

def match_prop_to_roster(
    player_name: str,
    roster: list[dict],
    props_data: dict[str, dict],
) -> Optional[dict]:
    """
    Try to match a roster player to their prop data.

    Uses progressively looser matching:
    1. Exact normalized name match
    2. Last name + first initial match
    3. Last name only (if unique in props_data)

    Returns the prop dict or None.
    """
    normalized = PropsClient._normalize_name(player_name)

    # 1. Exact match
    if normalized in props_data:
        return props_data[normalized]

    # 2. Last name match
    parts = normalized.split()
    if len(parts) >= 2:
        last = parts[-1]
        first_initial = parts[0][0] if parts[0] else ""

        matches = []
        for pname, pdata in props_data.items():
            pparts = pname.split()
            if len(pparts) >= 2 and pparts[-1] == last:
                if pparts[0][0] == first_initial:
                    return pdata  # first initial + last name match
                matches.append(pdata)

        # 3. Unique last name match
        if len(matches) == 1:
            return matches[0]

    return None
