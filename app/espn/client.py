"""
client.py - ESPN Fantasy Basketball API client.

Wraps the espn-api package with:
  - Cookie auth from .env (ESPN_S2 + SWID)
  - Credential masking (never logs secrets)
  - Structured data extraction (returns plain dicts, not library objects)
  - Graceful error handling for auth failures
"""

import logging
from datetime import datetime, date, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class ESPNAuthError(Exception):
    """Raised when ESPN credentials are missing or rejected."""
    pass


class ESPNClient:
    """
    Lazy-loading ESPN Fantasy Basketball client.

    Does NOT authenticate on __init__ — waits until first data request
    so the dashboard can show a friendly error instead of crashing.
    """

    def __init__(self, league_id: int, team_id: int, season: int,
                 espn_s2: str, espn_swid: str):
        self.league_id = league_id
        self.team_id = team_id
        self.season = season
        self._espn_s2 = espn_s2
        self._espn_swid = espn_swid
        self._league = None

    def _ensure_connected(self):
        """Lazy-init the League object on first use."""
        if self._league is not None:
            return

        if not self._espn_s2 or not self._espn_swid:
            raise ESPNAuthError(
                "ESPN credentials missing from .env.\n"
                "Set ESPN_S2 and ESPN_SWID in your .env file.\n"
                "See README for how to find these in your browser DevTools."
            )

        try:
            from espn_api.basketball import League
        except ImportError:
            raise ESPNAuthError(
                "espn-api package not installed.\n"
                "Run: pip install espn-api"
            )

        try:
            logger.info("Connecting to ESPN league %d (season %d)...", self.league_id, self.season)
            self._league = League(
                league_id=self.league_id,
                year=self.season,
                espn_s2=self._espn_s2,
                swid=self._espn_swid,
            )
            logger.info("Connected: %s", self._league.settings.name if hasattr(self._league, 'settings') else "OK")
        except Exception as exc:
            err_str = str(exc).lower()
            if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                raise ESPNAuthError(
                    "ESPN returned 401/403 - cookies expired or invalid.\n"
                    "Fix: Open fantasy.espn.com in your browser, re-copy ESPN_S2 and SWID\n"
                    "from DevTools > Application > Cookies, and update your .env file."
                ) from exc
            raise ESPNAuthError(f"Failed to connect to ESPN: {exc}") from exc

    # ── Helper: Extract fantasy points ─────────────────────────────────────────

    def _extract_fpts(self, val):
        """Extract fantasy points from a stats value (could be dict with FPTS or a number)."""
        if isinstance(val, (int, float)):
            return round(float(val), 1)
        if isinstance(val, dict):
            fpts = val.get("FPTS", 0.0)
            if isinstance(fpts, (int, float)):
                return round(float(fpts), 1)
        return 0.0

    # ── Week date helpers ──────────────────────────────────────────────────────

    def week_reference_date(self, matchup_period: int) -> date:
        """
        Map a matchup period number to the Monday of that week.

        Uses the current matchup period + today's date as anchor, then offsets
        by the difference in weeks.  Returns a ``date`` that can be passed to
        ``_serialize_player`` so game counts reflect the target week.
        """
        self._ensure_connected()
        current_period = getattr(self._league, "currentMatchupPeriod", 1)
        today = date.today()
        current_monday = today - timedelta(days=today.weekday())
        weeks_delta = matchup_period - current_period
        return current_monday + timedelta(weeks=weeks_delta)

    def week_date_range(self, matchup_period: int) -> tuple[date, date]:
        """Return (monday, sunday) for a given matchup period."""
        monday = self.week_reference_date(matchup_period)
        return monday, monday + timedelta(days=6)

    # ── League info ───────────────────────────────────────────────────────────

    def get_league_info(self) -> dict:
        self._ensure_connected()
        settings = self._league.settings
        return {
            "name": getattr(settings, "name", f"League {self.league_id}"),
            "season": self.season,
            "league_id": self.league_id,
            "team_count": getattr(settings, "team_count", len(self._league.teams)),
            "current_matchup_period": getattr(self._league, "currentMatchupPeriod", 0),
            "current_scoring_period": getattr(self._league, "current_week", 0),
            "playoff_seed_count": getattr(settings, "playoff_team_count", 4),
        }

    # ── Teams ─────────────────────────────────────────────────────────────────

    def get_all_teams(self, reference_date: date = None) -> list[dict]:
        self._ensure_connected()
        teams = []
        for t in self._league.teams:
            teams.append(self._serialize_team(t, reference_date))
        return teams

    def get_my_team(self, reference_date: date = None) -> dict:
        self._ensure_connected()
        for t in self._league.teams:
            tid = getattr(t, "team_id", t) if not isinstance(t, int) else t
            if tid == self.team_id:
                return self._serialize_team(t, reference_date)
        raise ValueError(f"Team ID {self.team_id} not found in league")

    def _serialize_team(self, t, reference_date: date = None) -> dict:
        # Guard: t might be an int (team_id) in some ESPN API contexts
        if isinstance(t, (int, float)):
            return {
                "team_id": int(t), "team_name": f"Team {t}", "team_abbrev": "",
                "owner": "Unknown", "wins": 0, "losses": 0, "ties": 0,
                "points_for": 0.0, "points_against": 0.0, "standing": 0,
                "playoff_pct": 0.0, "streak_length": 0, "streak_type": "",
                "roster": [],
            }
        roster = []
        for p in getattr(t, "roster", []):
            try:
                roster.append(self._serialize_player(p, reference_date))
            except Exception as exc:
                logger.debug("Skipping player serialization: %s", exc)
        return {
            "team_id": getattr(t, "team_id", 0),
            "team_name": getattr(t, "team_name", "Unknown"),
            "team_abbrev": getattr(t, "team_abbrev", ""),
            "owner": getattr(t, "owner", "Unknown"),
            "wins": getattr(t, "wins", 0),
            "losses": getattr(t, "losses", 0),
            "ties": getattr(t, "ties", 0),
            "points_for": getattr(t, "points_for", 0.0),
            "points_against": getattr(t, "points_against", 0.0),
            "standing": getattr(t, "standing", 0),
            "playoff_pct": getattr(t, "playoff_pct", 0.0),
            "streak_length": getattr(t, "streak_length", 0),
            "streak_type": getattr(t, "streak_type", ""),
            "roster": roster,
        }

    def _serialize_player(self, p, reference_date: date = None) -> dict:
        # Extract average points from stats
        avg_points = 0.0
        total_points = 0.0
        games_played = 0

        # Try to get stats from the player object
        stats = getattr(p, "stats", {})
        if stats:
            # espn-api stores stats keyed by period
            # Look for total season stats or recent stats
            for period_key, period_stats in stats.items():
                if isinstance(period_stats, dict):
                    avg = period_stats.get("avg", {})
                    total = period_stats.get("total", {})
                    if isinstance(avg, dict) and "FPTS" in avg:
                        avg_points = avg["FPTS"]
                    elif isinstance(avg, (int, float)):
                        avg_points = float(avg)
                    if isinstance(total, dict) and "FPTS" in total:
                        total_points = total["FPTS"]
                    elif isinstance(total, (int, float)):
                        total_points = float(total)
                    if isinstance(period_stats, dict) and "GP" in period_stats:
                        games_played = period_stats["GP"]

        # Fallback: use avg_points attribute if available
        if avg_points == 0.0:
            avg_points = getattr(p, "avg_points", 0.0) or 0.0

        # ────────────────────────────────────────────────────────────────────────
        # NEW: Extract ESPN projected average from stats['YEAR_projected']
        # ────────────────────────────────────────────────────────────────────────
        espn_projected_avg = 0.0
        projected_key = f"{self.season}_projected"
        if stats and projected_key in stats:
            projected_data = stats[projected_key]
            if isinstance(projected_data, dict):
                applied_avg = projected_data.get("applied_avg")
                espn_projected_avg = self._extract_fpts(applied_avg)
        # Fallback to projected_avg_points attribute
        if espn_projected_avg == 0.0:
            espn_projected_avg = self._extract_fpts(getattr(p, "projected_avg_points", 0.0))

        # ────────────────────────────────────────────────────────────────────────
        # NEW: Extract recent period averages (last 7, 15, 30)
        # ────────────────────────────────────────────────────────────────────────
        last_7_avg = 0.0
        last_15_avg = 0.0
        last_30_avg = 0.0

        if stats:
            last_7_key = f"{self.season}_last_7"
            if last_7_key in stats:
                data = stats[last_7_key]
                if isinstance(data, dict):
                    applied_avg = data.get("applied_avg")
                    last_7_avg = self._extract_fpts(applied_avg)

            last_15_key = f"{self.season}_last_15"
            if last_15_key in stats:
                data = stats[last_15_key]
                if isinstance(data, dict):
                    applied_avg = data.get("applied_avg")
                    last_15_avg = self._extract_fpts(applied_avg)

            last_30_key = f"{self.season}_last_30"
            if last_30_key in stats:
                data = stats[last_30_key]
                if isinstance(data, dict):
                    applied_avg = data.get("applied_avg")
                    last_30_avg = self._extract_fpts(applied_avg)

        # ────────────────────────────────────────────────────────────────────────
        # NEW: Extract season average from stats['YEAR_total']
        # ────────────────────────────────────────────────────────────────────────
        season_avg = 0.0
        total_key = f"{self.season}_total"
        if stats and total_key in stats:
            data = stats[total_key]
            if isinstance(data, dict):
                applied_avg = data.get("applied_avg")
                season_avg = self._extract_fpts(applied_avg)

        # ────────────────────────────────────────────────────────────────────────
        # Extract player schedule and count games remaining for the TARGET WEEK.
        # Fantasy matchup weeks run Monday-Sunday.
        #
        # reference_date controls which week we calculate for:
        #   - None or today  → current week, only games >= today count
        #   - Future Monday  → all games Mon-Sun of that week count
        #   - Past Monday    → 0 remaining (all games already played)
        # ────────────────────────────────────────────────────────────────────────
        schedule_dict = getattr(p, "schedule", {})
        schedule_list = []          # all future games (full season)
        week_schedule = []          # games for the target week only
        games_remaining = 0
        today = date.today()
        ref = reference_date or today

        # Calculate target week boundaries (Mon-Sun)
        week_monday = ref - timedelta(days=ref.weekday())  # Monday
        week_sunday = week_monday + timedelta(days=6)      # Sunday

        # Determine the earliest game date that counts as "remaining"
        # Current week: only today onward.  Future week: whole week.
        # Past week: nothing remaining.
        if week_sunday < today:
            # Entire target week is in the past — nothing remaining
            earliest_remaining = None
        elif week_monday >= today:
            # Entire target week is in the future — all games count
            earliest_remaining = week_monday
        else:
            # We're mid-week — only today onward
            earliest_remaining = today

        if isinstance(schedule_dict, dict):
            for period_id, game_info in schedule_dict.items():
                if isinstance(game_info, dict):
                    game_date_obj = game_info.get("date")
                    # Convert datetime to date if needed
                    if isinstance(game_date_obj, datetime):
                        game_date = game_date_obj.date()
                    else:
                        game_date = game_date_obj

                    opponent = game_info.get("team", "")
                    entry = {
                        "period_id": period_id,
                        "opponent": opponent,
                        "date": game_date.isoformat() if isinstance(game_date, date) else str(game_date),
                    }
                    schedule_list.append(entry)

                    # Count as remaining if game is in the target week
                    if isinstance(game_date, date) and earliest_remaining is not None:
                        if week_monday <= game_date <= week_sunday and game_date >= earliest_remaining:
                            games_remaining += 1
                            week_schedule.append(entry)

        # ────────────────────────────────────────────────────────────────────────
        # NEW: Extract recent game log (last 10 games)
        # ────────────────────────────────────────────────────────────────────────
        recent_games = []
        if stats:
            # Collect all numeric keys (scoring period IDs)
            numeric_periods = []
            for key in stats.keys():
                # Check if key is purely numeric (period ID)
                if isinstance(key, str) and key.isdigit():
                    numeric_periods.append(key)

            # Sort by period descending (most recent first)
            numeric_periods.sort(key=lambda x: int(x), reverse=True)

            # Take last 10 and extract applied_total
            for period_id in numeric_periods[:10]:
                period_data = stats[period_id]
                if isinstance(period_data, dict):
                    applied_total = period_data.get("applied_total")
                    points = self._extract_fpts(applied_total)
                    recent_games.append({
                        "period_id": period_id,
                        "points": points,
                    })

        # Injury status
        injured = getattr(p, "injured", False)
        injury_status = getattr(p, "injuryStatus", "ACTIVE")
        if injury_status in ("ACTIVE", ""):
            injury_status = "ACTIVE"

        return {
            "player_id": getattr(p, "playerId", 0),
            "name": getattr(p, "name", "Unknown"),
            "pro_team": getattr(p, "proTeam", "FA"),
            "position": getattr(p, "position", ""),
            "eligible_slots": getattr(p, "eligibleSlots", []),
            "lineup_slot": getattr(p, "lineupSlot", ""),
            "avg_points": round(avg_points, 1),
            "total_points": round(total_points, 1),
            "games_played": games_played,
            "injured": injured,
            "injury_status": injury_status,
            "acquisition_type": getattr(p, "acquisitionType", ""),
            "percent_owned": getattr(p, "percent_owned", 0.0),
            # NEW FIELDS
            "espn_projected_avg": espn_projected_avg,
            "last_7_avg": last_7_avg,
            "last_15_avg": last_15_avg,
            "last_30_avg": last_30_avg,
            "season_avg": season_avg,
            "games_remaining": games_remaining,
            "week_schedule": week_schedule,
            "schedule": schedule_list,
            "recent_games": recent_games,
        }

    # ── Matchups / Scoreboard ─────────────────────────────────────────────────

    def get_scoreboard(self, matchup_period: int = 0, reference_date: date = None) -> list[dict]:
        """Get all matchups for a given period (0 = current)."""
        self._ensure_connected()
        try:
            if matchup_period > 0:
                box_scores = self._league.scoreboard(matchup_period)
            else:
                box_scores = self._league.scoreboard()
        except Exception as exc:
            logger.warning("Scoreboard fetch failed: %s", exc)
            return []

        matchups = []
        for box in box_scores:
            home = box.home_team
            away = box.away_team

            # home_team / away_team can be Team objects or ints depending on
            # the ESPN API version and matchup state (byes, playoffs, etc.)
            home_id = getattr(home, "team_id", home) if not isinstance(home, int) else home
            home_name = getattr(home, "team_name", f"Team {home_id}") if not isinstance(home, int) else f"Team {home}"
            away_id = getattr(away, "team_id", away) if not isinstance(away, int) else away
            away_name = getattr(away, "team_name", f"Team {away_id}") if not isinstance(away, int) else f"Team {away}"

            home_lineup = []
            away_lineup = []
            for p in getattr(box, "home_lineup", []):
                try:
                    home_lineup.append(self._serialize_player(p, reference_date))
                except Exception:
                    pass
            for p in getattr(box, "away_lineup", []):
                try:
                    away_lineup.append(self._serialize_player(p, reference_date))
                except Exception:
                    pass

            # Scores: prefer home_final_score (accumulated this period),
            # fall back to live_score, and always guard against None
            home_score = getattr(box, "home_final_score", None)
            if not home_score:
                home_score = getattr(box, "home_team_live_score", None)
            home_score = float(home_score) if home_score else 0.0

            away_score = getattr(box, "away_final_score", None)
            if not away_score:
                away_score = getattr(box, "away_team_live_score", None)
            away_score = float(away_score) if away_score else 0.0

            matchups.append({
                "home_team_id": home_id,
                "home_team_name": home_name,
                "home_score": home_score,
                "home_lineup": home_lineup,
                "away_team_id": away_id,
                "away_team_name": away_name,
                "away_score": away_score,
                "away_lineup": away_lineup,
            })
        return matchups

    # ── Free agents (simplified for V0) ───────────────────────────────────────

    def get_free_agents(self, size: int = 50, reference_date: date = None) -> list[dict]:
        """Get top available free agents."""
        self._ensure_connected()
        try:
            fas = self._league.free_agents(size=size)
            return [self._serialize_player(p, reference_date) for p in fas]
        except Exception as exc:
            logger.warning("Free agent fetch failed: %s", exc)
            return []

    # ── Schedule data (pro team games remaining) ──────────────────────────────

    def get_pro_schedule_for_week(self) -> dict[str, int]:
        """
        Return {pro_team_abbrev: games_this_week} for remaining games.

        V0 approach: Use a static lookup of NBA schedule data.
        The espn-api doesn't directly expose per-day pro schedules,
        so we'll estimate based on typical weekly game counts (3-4 per team).
        A future version can scrape the actual NBA schedule.

        Note: Individual player schedule data is available via player.schedule
        in the serialized player dict. This method provides a team-level overview
        for backward compatibility.
        """
        # V0: Return reasonable defaults (most NBA teams play 3-4 games/week)
        # This gets overridden by actual data if available from box scores
        nba_teams = [
            "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN",
            "DET", "GS", "HOU", "IND", "LAC", "LAL", "MEM", "MIA",
            "MIL", "MIN", "NO", "NY", "OKC", "ORL", "PHI", "PHX",
            "POR", "SA", "SAC", "TOR", "UTA", "WAS",
        ]
        # Default: 3 games per week (conservative estimate)
        return {team: 3 for team in nba_teams}

    # ── Draft results ────────────────────────────────────────────────────────

    def get_draft(self) -> list[dict]:
        """
        Return draft picks as a list of dicts.

        Each pick: {round_num, round_pick, player_name, player_id,
                     team_id, team_name, keeper}
        """
        self._ensure_connected()
        draft = getattr(self._league, "draft", [])
        picks = []
        for pick in draft:
            team_obj = getattr(pick, "team", None)
            if team_obj and not isinstance(team_obj, (int, float)):
                tid = getattr(team_obj, "team_id", 0)
                tname = getattr(team_obj, "team_name", f"Team {tid}")
            else:
                tid = int(team_obj) if team_obj else 0
                tname = f"Team {tid}"
            picks.append({
                "round_num": getattr(pick, "round_num", 0),
                "round_pick": getattr(pick, "round_pick", 0),
                "player_name": getattr(pick, "playerName", "Unknown"),
                "player_id": getattr(pick, "playerId", 0),
                "team_id": tid,
                "team_name": tname,
                "keeper": getattr(pick, "keeper_status", False),
            })
        return picks
