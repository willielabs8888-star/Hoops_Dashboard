"""
projections.py  –  V1.2 Projection engine (with sportsbook props support)

Projection source policy (applied per-player):
  0. Sportsbook Props – convert player prop lines (PTS/REB/AST/STL/BLK) to FPTS
  1. ESPN Projected   – use ESPN's own projected_avg_points if > 0 and player not OUT
  2. Weighted Recent  – fallback:  0.50 * last_7_avg  +  0.30 * last_15_avg  +  0.20 * season_avg
     (weights renormalized when a window is unavailable)
  3. Season Average   – if no recent windows available
  4. Injury Zero      – player is OUT / IR  → 0 points
  5. No Data          – no stats at all

Games tracking:
  - "games remaining" always means THIS matchup week (Mon-Sun), today → Sunday
  - IR players are EXCLUDED from potential active games
  - capture_rate = used_games / (used_games + remaining_potential_games)

Win probability:
  P(A wins) = Φ( (proj_A − proj_B) / σ )
  where σ = sigma_pct × mean(proj_A, proj_B)
"""

import math
from datetime import date, timedelta
from typing import Any


# ─── Player projection ───────────────────────────────────────────────────────

# Weights for weighted_recent (documented in definitions section)
_W7 = 0.50   # last 7 days
_W15 = 0.30  # last 15 days
_W_SEASON = 0.20  # season average


def project_player(
    player: dict,
    games_remaining: int = None,
    props_data: dict = None,
    use_props: bool = True,
) -> dict:
    """
    Project a single player's remaining points this matchup week.

    Parameters
    ----------
    player : dict
        Serialized player data from ESPN client.
    games_remaining : int, optional
        Override for games remaining this week.
    props_data : dict, optional
        {normalized_name: {"fpts_estimate": float, "props": {...}, ...}}
        from PropsClient.get_today_props().
    use_props : bool
        Whether to use sportsbook props as primary source (default True).

    Returns a copy of *player* with these extra keys:
        projected_points     – total projected remaining
        per_game_avg         – per-game number used
        projection_method    – one of: Sportsbook Props | ESPN Projected |
                               Weighted Recent | Season Average | Injury Zero | No Data
        projection_note      – human-readable explanation
        games_remaining      – games left this week
        is_active_projection – False when projection is 0 due to injury/IR
        props_detail         – dict of individual prop lines (if available)
    """
    injured = player.get("injured", False)
    status = player.get("injury_status", "ACTIVE")
    slot = player.get("lineup_slot", "")

    # ── Determine if player is OUT / IR ──────────────────────────────────────
    is_out = (
        status in ("OUT", "SUSPENSION")
        or slot in ("IR", "IL")
    )
    is_questionable = status in ("QUESTIONABLE", "DAY_TO_DAY", "DTD")

    # ── Games remaining (this week only) ─────────────────────────────────────
    if games_remaining is None:
        games_remaining = player.get("games_remaining", 0) or 0

    # ── Select projection source ─────────────────────────────────────────────
    per_game_avg = 0.0
    projection_method = "No Data"
    projection_note = ""
    props_detail = None

    if is_out:
        # Source: Injury Zero
        per_game_avg = 0.0
        projection_method = "Injury Zero"
        projection_note = f"Player {status}" if status != "ACTIVE" else "In IR slot"

    else:
        prop_match = None

        # Source 0: Sportsbook Props (highest priority)
        if use_props and props_data:
            prop_match = _match_player_to_props(player, props_data)

        if prop_match and prop_match.get("fpts_estimate", 0) > 0:
            fpts_est = prop_match["fpts_estimate"]
            per_game_avg = fpts_est  # This IS the per-game estimate
            projection_method = "Sportsbook Props"
            props_detail = prop_match.get("props", {})
            source = prop_match.get("source", "sportsbook")
            stat_parts = ", ".join(
                f"{k}={v}" for k, v in sorted(props_detail.items())
            )
            projection_note = f"Props ({source}): {stat_parts} → {fpts_est:.1f} FPTS"
        else:
            # Source 1: ESPN projected avg
            espn_proj = player.get("espn_projected_avg", 0.0) or 0.0
            if espn_proj > 0:
                per_game_avg = espn_proj
                projection_method = "ESPN Projected"
                projection_note = f"ESPN proj {espn_proj:.1f}/g"
            else:
                # Source 2: Weighted recent
                last_7 = player.get("last_7_avg", 0.0) or 0.0
                last_15 = player.get("last_15_avg", 0.0) or 0.0
                season_avg = (
                    player.get("season_avg", 0.0)
                    or player.get("avg_points", 0.0)
                    or 0.0
                )

                buckets = [
                    (last_7, _W7, "7d"),
                    (last_15, _W15, "15d"),
                    (season_avg, _W_SEASON, "szn"),
                ]
                available = [(v, w, lbl) for v, w, lbl in buckets if v > 0]

                if available:
                    total_w = sum(w for _, w, _ in available)
                    per_game_avg = sum(v * (w / total_w) for v, w, _ in available)
                    parts = ", ".join(f"{lbl}={v:.1f}" for v, _, lbl in available)
                    projection_method = "Weighted Recent"
                    projection_note = f"Weighted ({parts})"
                elif season_avg > 0:
                    # Source 3: bare season average
                    per_game_avg = season_avg
                    projection_method = "Season Average"
                    projection_note = f"Season avg {season_avg:.1f}/g"
                else:
                    projection_method = "No Data"
                    projection_note = "No stats available"

    # ── Risk flag for questionable players ───────────────────────────────────
    if is_questionable and projection_method not in ("Injury Zero", "No Data"):
        projection_note += f" [Risk: {status}]"

    # ── Final projected points ───────────────────────────────────────────────
    is_active = not is_out
    projected = round(per_game_avg * games_remaining, 1) if is_active else 0.0

    result = {
        **player,
        "games_remaining": games_remaining,
        "projected_points": projected,
        "per_game_avg": round(per_game_avg, 2),
        "projection_method": projection_method,
        "projection_note": projection_note,
        "is_active_projection": is_active,
    }
    if props_detail is not None:
        result["props_detail"] = props_detail
    return result


def _match_player_to_props(player: dict, props_data: dict) -> dict | None:
    """Try to match a roster player to their sportsbook prop data."""
    name = player.get("name", "")
    if not name:
        return None

    # Normalize: lowercase, strip suffixes
    normalized = name.strip().lower()
    for suffix in (" jr.", " jr", " sr.", " sr", " iii", " ii", " iv"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].strip()

    # 1. Exact match
    if normalized in props_data:
        return props_data[normalized]

    # 2. Last name + first initial
    parts = normalized.split()
    if len(parts) >= 2:
        last = parts[-1]
        first_initial = parts[0][0] if parts[0] else ""

        matches = []
        for pname, pdata in props_data.items():
            pparts = pname.split()
            if len(pparts) >= 2 and pparts[-1] == last:
                if pparts[0][0] == first_initial:
                    return pdata
                matches.append(pdata)

        # 3. Unique last name
        if len(matches) == 1:
            return matches[0]

    return None


# ─── Games tracking ──────────────────────────────────────────────────────────

_STARTER_SLOTS = 10  # PG, SG, SF, PF, C, G, F, UT ×3


def compute_games_tracking(team: dict) -> dict:
    """
    Analyse a team's roster for games utilisation THIS matchup week.

    Uses **daily optimal lineup** math rather than the current-slot snapshot:
    for each remaining game day, count how many healthy non-IR players have a
    game.  If more than STARTER_SLOTS (10) play that day, the overflow games
    are unavoidable bench waste.

    Definitions
    -----------
    active_lineup_remaining  – games you WILL capture (assumes optimal lineups)
    bench_remaining          – games that MUST go to waste (overflow days only)
    ir_remaining             – games for IR/IL players (excluded from potential)
    potential_remaining      – active + bench  (IR excluded)
    capture_rate             – active / potential
    daily_breakdown          – per-day detail for tooltip / debugging
    """
    # ── Partition roster into IR vs healthy/eligible ────────────────────────
    ir_remaining = 0
    eligible_players = []  # non-IR players (healthy or bench or OUT-but-not-IR)

    for player in team.get("roster", []):
        slot = player.get("lineup_slot", "")
        if slot in ("IR", "IL"):
            ir_remaining += player.get("games_remaining", 0) or 0
        else:
            eligible_players.append(player)

    # ── Build per-day game counts for eligible players ─────────────────────
    # day_map: {date_str: [player_name, ...]}
    day_map: dict[str, list[str]] = {}

    for player in eligible_players:
        name = player.get("name", "Unknown")
        status = player.get("injury_status", "ACTIVE")
        # OUT / SUSPENSION players can't play regardless of slot
        if status in ("OUT", "SUSPENSION"):
            continue

        for game in player.get("week_schedule", []):
            gdate_str = game.get("date", "")
            if not gdate_str:
                continue
            # Parse ISO date string
            try:
                gdate = date.fromisoformat(gdate_str)
            except (ValueError, TypeError):
                continue
            # week_schedule is already filtered to the target week by
            # _serialize_player, so no extra date filtering needed here.
            key = gdate.isoformat()
            day_map.setdefault(key, []).append(name)

    # ── Compute daily active vs bench ──────────────────────────────────────
    active_remaining = 0
    bench_remaining = 0
    daily_breakdown = []

    for day_str in sorted(day_map.keys()):
        players_playing = day_map[day_str]
        count = len(players_playing)
        active = min(count, _STARTER_SLOTS)
        bench = max(0, count - _STARTER_SLOTS)
        active_remaining += active
        bench_remaining += bench
        daily_breakdown.append({
            "date": day_str,
            "players_playing": count,
            "active": active,
            "bench_wasted": bench,
            "players": players_playing,
        })

    potential_remaining = active_remaining + bench_remaining  # IR excluded
    capture_rate = (
        active_remaining / potential_remaining
        if potential_remaining > 0 else 1.0
    )

    # ── Per-player summary (for roster table display) ──────────────────────
    per_player_list = []
    for player in team.get("roster", []):
        games_rem = player.get("games_remaining", 0) or 0
        slot = player.get("lineup_slot", "")
        in_ir = slot in ("IR", "IL")
        in_active_slot = slot not in ("IR", "IL", "BE")
        per_player_list.append({
            "name": player.get("name", "Unknown"),
            "lineup_slot": slot,
            "games_remaining": games_rem,
            "is_capturing": in_active_slot and not in_ir,
            "projected_points": player.get("projected_points", 0.0),
        })

    return {
        "active_lineup_remaining": active_remaining,
        "bench_remaining": bench_remaining,
        "ir_remaining": ir_remaining,
        "potential_remaining": potential_remaining,
        "capture_rate": round(capture_rate, 3),
        "per_player": per_player_list,
        "daily_breakdown": daily_breakdown,
    }


# ─── Trend detection ─────────────────────────────────────────────────────────

def detect_trends(player: dict) -> dict:
    """
    Detect performance trends for a single player.

    Thresholds:
        HOT       – 7-day avg > season avg × 1.15
        COLD      – 7-day avg < season avg × 0.85
        RISING    – 15-day > 30-day × 1.10  AND  7-day > 15-day
        DECLINING – 15-day < 30-day × 0.90  AND  7-day < 15-day
    """
    trends = []
    name = player.get("name", "Unknown")

    last_7 = player.get("last_7_avg", 0.0) or 0.0
    last_15 = player.get("last_15_avg", 0.0) or 0.0
    last_30 = player.get("last_30_avg", 0.0) or 0.0
    season_avg = player.get("season_avg", 0.0) or player.get("avg_points", 0.0) or 0.0

    if season_avg <= 0:
        return {"player": name, "trends": []}

    if last_7 > 0 and last_7 > season_avg * 1.15:
        pct = ((last_7 - season_avg) / season_avg) * 100
        trends.append({
            "trend": "HOT",
            "detail": f"7d avg {last_7:.1f} vs season {season_avg:.1f} (+{pct:.1f}%)",
            "severity": "high" if pct > 25 else "medium",
        })

    if last_7 > 0 and last_7 < season_avg * 0.85:
        pct = ((season_avg - last_7) / season_avg) * 100
        trends.append({
            "trend": "COLD",
            "detail": f"7d avg {last_7:.1f} vs season {season_avg:.1f} (-{pct:.1f}%)",
            "severity": "high" if pct > 25 else "medium",
        })

    if (last_15 > 0 and last_30 > 0 and last_7 > 0
            and last_15 > last_30 * 1.10 and last_7 > last_15):
        trends.append({
            "trend": "RISING",
            "detail": f"30d: {last_30:.1f} -> 15d: {last_15:.1f} -> 7d: {last_7:.1f}",
            "severity": "medium",
        })

    if (last_15 > 0 and last_30 > 0 and last_7 > 0
            and last_15 < last_30 * 0.90 and last_7 < last_15):
        trends.append({
            "trend": "DECLINING",
            "detail": f"30d: {last_30:.1f} -> 15d: {last_15:.1f} -> 7d: {last_7:.1f}",
            "severity": "medium",
        })

    return {"player": name, "trends": trends}


# ─── Team projection ─────────────────────────────────────────────────────────

def project_team(
    team: dict,
    pro_schedule: dict[str, int] = None,
    props_data: dict = None,
    use_props: bool = True,
) -> dict:
    """Project a full team.  IR players get Injury Zero automatically."""
    if pro_schedule is None:
        pro_schedule = {}

    projected_roster = []
    total_projected = 0.0
    props_used = 0

    for player in team.get("roster", []):
        games = player.get("games_remaining", 0) or 0
        if games == 0:
            pro_team = player.get("pro_team", "FA")
            games = pro_schedule.get(pro_team, 3)

        proj_player = project_player(
            player, games,
            props_data=props_data,
            use_props=use_props,
        )
        projected_roster.append(proj_player)
        total_projected += proj_player["projected_points"]
        if proj_player.get("projection_method") == "Sportsbook Props":
            props_used += 1

    return {
        **team,
        "roster": projected_roster,
        "projected_total": round(total_projected, 1),
        "active_players": sum(1 for p in projected_roster if p["is_active_projection"]),
        "total_games": sum(
            p["games_remaining"] for p in projected_roster if p["is_active_projection"]
        ),
        "props_players": props_used,
    }


# ─── Matchup projection ──────────────────────────────────────────────────────

def project_matchup(
    home_team: dict,
    away_team: dict,
    pro_schedule: dict[str, int] = None,
    home_score: float = 0.0,
    away_score: float = 0.0,
    sigma_pct: float = 0.12,
    props_data: dict = None,
    use_props: bool = True,
) -> dict:
    if pro_schedule is None:
        pro_schedule = {}

    home_proj = project_team(home_team, pro_schedule, props_data, use_props)
    away_proj = project_team(away_team, pro_schedule, props_data, use_props)

    home_total = home_score + home_proj["projected_total"]
    away_total = away_score + away_proj["projected_total"]

    home_win_prob = _win_probability(home_total, away_total, sigma_pct)

    return {
        "home": home_proj,
        "away": away_proj,
        "home_current_score": home_score,
        "away_current_score": away_score,
        "home_projected_total": round(home_total, 1),
        "away_projected_total": round(away_total, 1),
        "projected_margin": round(home_total - away_total, 1),
        "home_win_prob": round(home_win_prob, 3),
        "away_win_prob": round(1 - home_win_prob, 3),
    }


# ─── Win probability ─────────────────────────────────────────────────────────

def _win_probability(proj_a: float, proj_b: float, sigma_pct: float = 0.12) -> float:
    """P(A wins) via normal CDF."""
    if proj_a == 0 and proj_b == 0:
        return 0.5
    mean_proj = (proj_a + proj_b) / 2
    sigma = sigma_pct * mean_proj
    if sigma <= 0:
        return 1.0 if proj_a > proj_b else 0.0
    z = (proj_a - proj_b) / sigma
    return _norm_cdf(z)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ─── Streaming gaps ──────────────────────────────────────────────────────────

def find_streaming_gaps(team: dict, pro_schedule: dict[str, int] = None) -> list[dict]:
    if pro_schedule is None:
        pro_schedule = {}

    gaps = []
    for p in team.get("roster", []):
        games = p.get("games_remaining", 0) or 0
        if games == 0:
            pro_team = p.get("pro_team", "FA")
            games = pro_schedule.get(pro_team, 0)

        slot = p.get("lineup_slot", "")

        if games == 0 and slot not in ("IR", "IL", "BE"):
            gaps.append({
                "player": p.get("name", "Unknown"),
                "position": p.get("position", ""),
                "reason": "0 games remaining this week",
                "suggestion": "Consider streaming this slot",
            })
        elif p.get("injured", False) and slot not in ("IR", "IL"):
            gaps.append({
                "player": p.get("name", "Unknown"),
                "position": p.get("position", ""),
                "reason": f"Injured ({p.get('injury_status', 'OUT')})",
                "suggestion": "Move to IR, stream the slot",
            })

    return gaps
