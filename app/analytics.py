"""
analytics.py  –  V1.2 League analytics

A)  Draft Winner   – rank teams by the current value of their draft picks
B)  Best Current Team – rank teams by current roster strength
C)  King of the Waiver Wire – rank teams by value added via non-draft acquisitions

Each function takes pre-fetched data (teams, draft picks) so it stays
cache-friendly and doesn't make extra API calls.
"""

from typing import Any


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _player_lookup(teams: list[dict]) -> dict[int, dict]:
    """Build {player_id: player_dict} across all rosters in the league."""
    lookup = {}
    for team in teams:
        for p in team.get("roster", []):
            pid = p.get("player_id", 0)
            if pid:
                lookup[pid] = {**p, "_team_id": team.get("team_id", 0),
                               "_team_name": team.get("team_name", "")}
    return lookup


# ─── A) Draft Winner ─────────────────────────────────────────────────────────

def draft_winner_analysis(teams: list[dict], draft_picks: list[dict]) -> dict:
    """
    Evaluate teams based on the players they drafted.

    For each draft pick we look up the player's current season stats
    (total_points, season_avg).  If the player is no longer on ANY roster
    (dropped), we still credit the drafting team for the pick but mark
    the player's current value as 0.

    Returns:
        {
            "rankings": [
                {
                    "team_name": str,
                    "team_id": int,
                    "draft_season_total": float,   # sum of season total pts from drafted players
                    "draft_per_game_strength": float,  # sum of season avg pts/g
                    "picks_count": int,
                    "top_picks": [                 # best 3 drafted players by season total
                        {"player": str, "round": int, "pick": int,
                         "season_total": float, "season_avg": float,
                         "still_rostered": bool}
                    ],
                }
            ],
            "best_pick_overall": dict,   # single best pick across the league
        }
    """
    # Build a lookup of all currently rostered players
    player_lookup = _player_lookup(teams)

    # Group draft picks by team
    team_picks: dict[int, list] = {}
    for pick in draft_picks:
        tid = pick.get("team_id", 0)
        if tid not in team_picks:
            team_picks[tid] = []
        team_picks[tid].append(pick)

    # Team name lookup
    team_names = {t["team_id"]: t["team_name"] for t in teams}

    rankings = []
    best_pick = None
    best_pick_total = 0.0

    for tid, picks in team_picks.items():
        season_total = 0.0
        per_game_total = 0.0
        pick_details = []

        for pick in picks:
            pid = pick.get("player_id", 0)
            player = player_lookup.get(pid)

            if player:
                p_total = player.get("total_points", 0.0) or 0.0
                p_avg = player.get("season_avg", 0.0) or player.get("avg_points", 0.0) or 0.0
                still_rostered = True
            else:
                # Player was dropped — we don't have their stats anymore
                p_total = 0.0
                p_avg = 0.0
                still_rostered = False

            season_total += p_total
            per_game_total += p_avg

            detail = {
                "player": pick.get("player_name", "Unknown"),
                "round": pick.get("round_num", 0),
                "pick": pick.get("round_pick", 0),
                "season_total": round(p_total, 1),
                "season_avg": round(p_avg, 1),
                "still_rostered": still_rostered,
            }
            pick_details.append(detail)

            if p_total > best_pick_total:
                best_pick_total = p_total
                best_pick = {**detail, "team_name": team_names.get(tid, f"Team {tid}")}

        # Sort picks by season total descending
        pick_details.sort(key=lambda x: x["season_total"], reverse=True)

        rankings.append({
            "team_name": team_names.get(tid, f"Team {tid}"),
            "team_id": tid,
            "draft_season_total": round(season_total, 1),
            "draft_per_game_strength": round(per_game_total, 1),
            "picks_count": len(picks),
            "top_picks": pick_details[:3],
        })

    # Sort by season total descending
    rankings.sort(key=lambda x: x["draft_season_total"], reverse=True)

    return {
        "rankings": rankings,
        "best_pick_overall": best_pick,
    }


# ─── B) Best Current Team ────────────────────────────────────────────────────

def best_current_team_analysis(teams: list[dict]) -> dict:
    """
    Rank teams by current roster strength.

    Metrics per team:
      - weighted_recent_strength: sum of each player's weighted recent avg
        (0.50 * last_7 + 0.30 * last_15 + 0.20 * season_avg), active players only
      - season_avg_strength: sum of each player's season avg, active players only
      - season_total_points: team's total points for (from standings)
      - top_contributors: top 3 players by weighted recent

    Returns:
        {"rankings": [...], "league_avg_strength": float}
    """
    rankings = []

    for team in teams:
        roster = team.get("roster", [])
        weighted_total = 0.0
        season_avg_total = 0.0
        contributors = []

        for p in roster:
            slot = p.get("lineup_slot", "")
            status = p.get("injury_status", "ACTIVE")
            # Only count non-IR, non-OUT players
            if slot in ("IR", "IL") or status in ("OUT", "SUSPENSION"):
                continue

            last_7 = p.get("last_7_avg", 0.0) or 0.0
            last_15 = p.get("last_15_avg", 0.0) or 0.0
            season_avg = p.get("season_avg", 0.0) or p.get("avg_points", 0.0) or 0.0

            # Weighted recent
            buckets = [(last_7, 0.50), (last_15, 0.30), (season_avg, 0.20)]
            avail = [(v, w) for v, w in buckets if v > 0]
            if avail:
                tw = sum(w for _, w in avail)
                weighted = sum(v * (w / tw) for v, w in avail)
            else:
                weighted = 0.0

            weighted_total += weighted
            season_avg_total += season_avg

            contributors.append({
                "player": p.get("name", "Unknown"),
                "weighted_recent": round(weighted, 1),
                "season_avg": round(season_avg, 1),
            })

        contributors.sort(key=lambda x: x["weighted_recent"], reverse=True)

        rankings.append({
            "team_name": team.get("team_name", "Unknown"),
            "team_id": team.get("team_id", 0),
            "weighted_recent_strength": round(weighted_total, 1),
            "season_avg_strength": round(season_avg_total, 1),
            "season_total_points": round(team.get("points_for", 0.0), 1),
            "top_contributors": contributors[:3],
        })

    rankings.sort(key=lambda x: x["weighted_recent_strength"], reverse=True)
    avg_str = (sum(r["weighted_recent_strength"] for r in rankings) / len(rankings)
               if rankings else 0.0)

    return {
        "rankings": rankings,
        "league_avg_strength": round(avg_str, 1),
    }


# ─── C) King of the Waiver Wire ──────────────────────────────────────────────

def waiver_wire_analysis(teams: list[dict], draft_picks: list[dict]) -> dict:
    """
    Quantify points generated from waiver/trade acquisitions.

    Logic: any player currently rostered whose acquisitionType != 'DRAFT'
    is a waiver/trade pickup.  We credit their full season total_points
    to the team that currently holds them.

    Limitations (documented):
      - Only counts players CURRENTLY on rosters (dropped waiver pickups not tracked)
      - Credits full season points (not just points-after-acquisition)
      - ESPN API doesn't expose transaction dates in the current wrapper

    Returns:
        {"rankings": [...], "best_pickup_overall": dict}
    """
    # Build set of drafted player IDs per team (original draft team)
    drafted_ids: set[int] = set()
    for pick in draft_picks:
        pid = pick.get("player_id", 0)
        if pid:
            drafted_ids.add(pid)

    rankings = []
    best_pickup = None
    best_pickup_total = 0.0

    for team in teams:
        tid = team.get("team_id", 0)
        tname = team.get("team_name", "Unknown")
        roster = team.get("roster", [])

        waiver_points = 0.0
        waiver_players = []

        for p in roster:
            acq = p.get("acquisition_type", "")
            pid = p.get("player_id", 0)

            # Identify non-draft acquisitions
            # Two checks: acquisitionType field AND not in the draft picks list
            is_waiver = (acq != "DRAFT") or (pid and pid not in drafted_ids)

            # Double-check: if acquisitionType says DRAFT and the player IS in
            # the draft list, they're a draft pick (not waiver)
            if acq == "DRAFT" and pid in drafted_ids:
                is_waiver = False

            if not is_waiver:
                continue

            p_total = p.get("total_points", 0.0) or 0.0
            p_avg = p.get("season_avg", 0.0) or p.get("avg_points", 0.0) or 0.0

            waiver_points += p_total
            detail = {
                "player": p.get("name", "Unknown"),
                "season_total": round(p_total, 1),
                "season_avg": round(p_avg, 1),
                "acquisition_type": acq,
            }
            waiver_players.append(detail)

            if p_total > best_pickup_total:
                best_pickup_total = p_total
                best_pickup = {**detail, "team_name": tname}

        waiver_players.sort(key=lambda x: x["season_total"], reverse=True)
        num_adds = len(waiver_players)

        rankings.append({
            "team_name": tname,
            "team_id": tid,
            "waiver_points": round(waiver_points, 1),
            "num_waiver_players": num_adds,
            "waiver_points_per_add": round(waiver_points / num_adds, 1) if num_adds > 0 else 0.0,
            "best_pickup": waiver_players[0] if waiver_players else None,
        })

    rankings.sort(key=lambda x: x["waiver_points"], reverse=True)

    return {
        "rankings": rankings,
        "best_pickup_overall": best_pickup,
    }
