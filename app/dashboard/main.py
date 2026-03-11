"""
main.py - ESPN Fantasy Basketball Dashboard (Streamlit)  V1.5

Run:  streamlit run app/dashboard/main.py
  or: double-click dashboard.bat

V1.1 – Corrected games tracking, projection source policy, methodology expanders
V1.2 – Analytics tab: Best Current Team
V1.3 – Week selector: view any matchup week, project future matchups
V1.4 – Global team selector: switch perspective to any team in the league
V1.5 – Sportsbook props integration (The Odds API) for improved projections
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

import app.config as cfg
from app.espn.client import ESPNClient, ESPNAuthError
from app.espn.cache import CacheManager
from app.projections import (
    project_player, project_team, project_matchup,
    find_streaming_gaps, compute_games_tracking, detect_trends,
    _win_probability,
)
from app.analytics import best_current_team_analysis
from app.props import PropsClient

# ─── Shared methodology text ─────────────────────────────────────────────────

METHODOLOGY_PROJECTION = """
**Projection Source Policy** (per player, in priority order):
1. **Sportsbook Props** ⭐ - converts player prop lines (PTS/REB/AST/STL/BLK) from
   DraftKings/FanDuel into fantasy points using your league's scoring weights.
   Only available for players with games today.
2. **ESPN Projected** - ESPN's own projected avg pts/game (factors in matchups, minutes, trends)
3. **Weighted Recent** - `0.50 x last_7d_avg + 0.30 x last_15d_avg + 0.20 x season_avg`
   (weights renormalized if a window is unavailable)
4. **Season Average** - full-season applied average (last resort)
5. **Injury Zero** - player is OUT or in IR slot -> 0 points

**Projected Points** = `per_game_avg x games_remaining_this_week`
"""

METHODOLOGY_GAMES = """
**Games Remaining** = games from today through Sunday of the current matchup week (Mon-Sun),
sourced from each player's ESPN schedule.

**Daily Optimal Lineup** math: for each remaining game day, we count how many healthy non-IR
players have a game. With 10 starter slots (PG/SG/SF/PF/C/G/F/UT x3), if more than 10 players
have games on a single day, the overflow are **unavoidable bench games** regardless of lineup moves.

**Active Games** = games you WILL capture assuming you set optimal daily lineups.
**Bench Games** = games that MUST go to waste (overflow on days with 11+ healthy players playing).
**Potential Remaining** = active + bench. IR players are **excluded** (can't play without roster move).
**Capture Rate** = `active / potential`. 100% means no wasted games this week.
"""

METHODOLOGY_WIN_PROB = """
**Win Probability** uses a normal approximation:

`P(win) = Phi( (my_projected_total - opp_projected_total) / sigma )`

where `sigma = 12% x average(my_total, opp_total)` and Phi is the standard normal CDF.
This accounts for variance in weekly scoring.
"""

METHODOLOGY_TRENDS = """
**Trend Detection** compares recent averages to season averages:

- **HOT**: 7-day avg > season avg x 1.15 (15%+ above season pace)
- **COLD**: 7-day avg < season avg x 0.85 (15%+ below season pace)
- **RISING**: 15-day > 30-day x 1.10 AND 7-day > 15-day (sustained upward trajectory)
- **DECLINING**: 15-day < 30-day x 0.90 AND 7-day < 15-day (sustained downward trajectory)

Severity is **high** when the deviation exceeds 25%, **medium** otherwise.
"""

METHODOLOGY_STREAMERS = """
**Roster Gaps** flags:
- Active-slot players with 0 games remaining this week (dead slot)
- Injured players not on IR (wasting an active slot)

**Free Agent Projections** use the same projection source policy as rostered players.
Sort by projected points this week to find the best streaming pickups.
"""

METHODOLOGY_ANALYTICS = """
**Best Current Team** — Ranks teams by current roster strength (ignoring draft).

- `Weighted Recent Strength` = sum of each active player's weighted recent avg:
  `0.50 x last_7d + 0.30 x last_15d + 0.20 x season_avg`
- `Season Avg Strength` = sum of season averages
- IR and OUT players excluded from strength calculations
"""


# ─── Streamlit config ────────────────────────────────────────────────────────

st.set_page_config(
    page_title="FantasyBot Dashboard",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_client():
    return ESPNClient(
        league_id=cfg.ESPN_LEAGUE_ID,
        team_id=cfg.ESPN_TEAM_ID,
        season=cfg.ESPN_SEASON,
        espn_s2=cfg.ESPN_S2,
        espn_swid=cfg.ESPN_SWID,
    )


@st.cache_resource
def get_cache():
    return CacheManager(cfg.CACHE_DIR, cfg.CACHE_TTL_SECONDS)


@st.cache_resource
def get_props_client():
    """Initialize the sportsbook props client (if API key is configured)."""
    if not cfg.HAS_ODDS_API:
        return None
    return PropsClient(
        api_key=cfg.ODDS_API_KEY,
        scoring_weights=cfg.SCORING_WEIGHTS,
        cache_dir=cfg.CACHE_DIR / "props",
        cache_ttl=cfg.PROPS_CACHE_TTL,
    )


def load_data(client, cache, selected_period: int = None):
    league_info = cache.get_or_fetch("league_info", client.get_league_info)
    current_period = league_info.get("current_matchup_period", 0)
    period = selected_period or current_period

    # Compute reference_date for the target week
    ref_date = client.week_reference_date(period)

    # Teams with schedule data recalculated for the target week
    teams = cache.get_or_fetch(
        f"teams_{period}",
        lambda: client.get_all_teams(reference_date=ref_date),
    )
    scoreboard = cache.get_or_fetch(
        f"scoreboard_{period}",
        lambda: client.get_scoreboard(period, reference_date=ref_date),
    )
    pro_schedule = cache.get_or_fetch(
        "pro_schedule",
        client.get_pro_schedule_for_week,
    )
    return league_info, teams, scoreboard, pro_schedule


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not cfg.HAS_ESPN:
        st.error("ESPN credentials not configured. See .env file.")
        st.stop()

    client = get_client()
    cache = get_cache()

    # ── First load: get league info for sidebar (before week selector) ─────
    try:
        league_info = cache.get_or_fetch("league_info", client.get_league_info)
    except ESPNAuthError as e:
        st.error(f"ESPN Authentication Failed: {e}")
        st.stop()
    except Exception as e:
        st.error(f"Failed to load ESPN data: {e}")
        st.stop()

    league_name = league_info.get("name", f"League {cfg.ESPN_LEAGUE_ID}")
    current_period = league_info.get("current_matchup_period", 0)

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("🏀 FantasyBot")
        st.caption(f"{league_name} | {cfg.ESPN_SEASON}")
        st.divider()

        # Week selector
        max_week = current_period + 4  # allow projecting a few weeks ahead
        selected_period = st.number_input(
            "Matchup Week",
            min_value=1,
            max_value=max_week,
            value=current_period,
            step=1,
            key="week_selector",
        )

        # Show date range for selected week
        try:
            wk_mon, wk_sun = client.week_date_range(selected_period)
            st.caption(f"{wk_mon.strftime('%a %b %d')} – {wk_sun.strftime('%a %b %d')}")
        except Exception:
            pass

        is_current_week = (selected_period == current_period)
        is_future_week = (selected_period > current_period)

        if is_current_week:
            st.markdown("**Viewing: Current Week**")
        elif is_future_week:
            st.markdown(f"**Viewing: Week {selected_period}** (future)")
        else:
            st.markdown(f"**Viewing: Week {selected_period}** (past)")

        st.divider()

    # ── Load data for selected week ───────────────────────────────────────
    try:
        league_info, teams, scoreboard, pro_schedule = load_data(
            client, cache, selected_period=selected_period
        )
    except ESPNAuthError as e:
        st.error(f"ESPN Authentication Failed: {e}")
        st.stop()
    except Exception as e:
        st.error(f"Failed to load ESPN data: {e}")
        st.info("Try 'Refresh Data' in the sidebar.")
        st.stop()

    teams_by_id = {t["team_id"]: t for t in teams}

    # ── Team selector (sidebar, needs teams loaded first) ─────────────────
    with st.sidebar:
        # Initialize session state for selected team
        if "selected_team_id" not in st.session_state:
            st.session_state.selected_team_id = cfg.ESPN_TEAM_ID

        team_options = sorted(teams, key=lambda t: t["team_name"])
        team_labels = [t["team_name"] for t in team_options]
        team_ids = [t["team_id"] for t in team_options]

        # Find default index
        try:
            default_team_idx = team_ids.index(st.session_state.selected_team_id)
        except ValueError:
            default_team_idx = 0

        selected_team_label = st.selectbox(
            "🏀 View As Team",
            team_labels,
            index=default_team_idx,
            key="team_selector",
        )
        selected_team_id = team_ids[team_labels.index(selected_team_label)]
        st.session_state.selected_team_id = selected_team_id

        st.markdown(f"**League Type:** {cfg.LEAGUE_TYPE}")
        st.divider()
        cache_key = f"teams_{selected_period}"
        st.markdown(f"**Cache:** {cache.cache_age_str(cache_key)}")
        if st.button("🔄 Refresh Data", use_container_width=True):
            cache.invalidate_all()
            st.cache_resource.clear()
            st.rerun()
        st.divider()
        st.caption("V1.5 Props + Team Selector")

    my_team = teams_by_id.get(st.session_state.selected_team_id, {})

    # ── Load sportsbook props (current week only) ─────────────────────────
    props_client = get_props_client()
    props_data = {}
    if props_client and is_current_week and cfg.USE_PROPS_PROJECTION:
        try:
            props_data = props_client.get_today_props()
            if props_data:
                remaining = props_client.get_remaining_requests()
                with st.sidebar:
                    st.success(f"Props loaded: {len(props_data)} players")
                    if remaining is not None:
                        st.caption(f"API quota: {remaining} requests remaining")
        except Exception as e:
            with st.sidebar:
                st.warning(f"Props unavailable: {e}")

    # ── Future/past week banner ───────────────────────────────────────────
    if not is_current_week:
        try:
            wk_mon, wk_sun = client.week_date_range(selected_period)
            date_str = f"{wk_mon.strftime('%b %d')} – {wk_sun.strftime('%b %d')}"
        except Exception:
            date_str = f"Week {selected_period}"
        if is_future_week:
            st.info(
                f"Viewing **Week {selected_period}** ({date_str}) — "
                f"projections use current rosters and ESPN projected averages. "
                f"Scores show 0-0 (not yet played)."
            )
        else:
            st.info(
                f"Viewing **Week {selected_period}** ({date_str}) — "
                f"this is a past week. Scores reflect final results."
            )

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Overview", "⚔️ Matchups", "👤 Team Detail",
        "📈 Trends", "🏆 Analytics"
    ])

    active_team_id = st.session_state.selected_team_id

    with tab1:
        render_overview(my_team, teams, scoreboard, pro_schedule, league_info,
                        selected_period, is_future_week, active_team_id, props_data)
    with tab2:
        render_matchups(teams_by_id, scoreboard, pro_schedule, props_data)
    with tab3:
        render_team_detail(teams, pro_schedule, props_data)
    with tab4:
        render_trends(teams)
    with tab5:
        render_analytics(teams)


# ─── Tab 1: Overview ─────────────────────────────────────────────────────────

def render_overview(my_team, teams, scoreboard, pro_schedule, league_info,
                    selected_period=None, is_future_week=False, active_team_id=None,
                    props_data=None):
    st.header("League Overview")

    # Definitions expander
    with st.expander("Definitions / Methodology"):
        st.markdown(METHODOLOGY_PROJECTION)
        st.markdown(METHODOLOGY_GAMES)
        st.markdown(METHODOLOGY_WIN_PROB)

    # Find selected team's matchup
    my_id = active_team_id or cfg.ESPN_TEAM_ID
    my_matchup = None
    for m in scoreboard:
        if m["home_team_id"] == my_id or m["away_team_id"] == my_id:
            my_matchup = m
            break

    week_label = f"Week {selected_period}" if selected_period else "This Week"
    teams_by_id = {t["team_id"]: t for t in teams}

    if my_matchup:
        # ── Current / past week: full matchup view ──────────────────────────
        team_label = my_team.get("team_name", "My Team")
        st.subheader(f"{team_label} Matchup — {week_label}")

        if my_matchup["home_team_id"] == my_id:
            me_name, me_score = my_matchup["home_team_name"], my_matchup["home_score"]
            opp_name, opp_score, opp_id = my_matchup["away_team_name"], my_matchup["away_score"], my_matchup["away_team_id"]
        else:
            me_name, me_score = my_matchup["away_team_name"], my_matchup["away_score"]
            opp_name, opp_score, opp_id = my_matchup["home_team_name"], my_matchup["home_score"], my_matchup["home_team_id"]

        me_proj = project_team(my_team, pro_schedule, props_data)
        opp_team = teams_by_id.get(opp_id, {})
        opp_proj = project_team(opp_team, pro_schedule, props_data)

        me_total = me_score + me_proj["projected_total"]
        opp_total = opp_score + opp_proj["projected_total"]
        win_prob = _win_probability(me_total, opp_total, cfg.WIN_PROB_SIGMA_PCT)

        # Metrics row
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric(f"{me_name} Proj Total", f"{me_total:.0f}",
                       delta=f"+{me_proj['projected_total']:.0f} remaining")
        with c2:
            st.metric(f"{opp_name} Proj Total", f"{opp_total:.0f}",
                       delta=f"+{opp_proj['projected_total']:.0f} remaining")
        with c3:
            margin = me_total - opp_total
            st.metric("Projected Margin",
                       f"{'+' if margin >= 0 else ''}{margin:.0f}",
                       delta="Favorable" if margin > 0 else "Behind")
        with c4:
            st.metric("Win Probability", f"{win_prob:.0%}")

        st.markdown(f"**Current Score:** {me_name} **{me_score:.0f}** vs {opp_name} **{opp_score:.0f}**")

        # Games tracking
        _render_games_tracking_comparison(me_name, me_proj, opp_name, opp_proj)

        # Win prob gauge
        fig = go.Figure(go.Indicator(
            mode="gauge+number", value=win_prob * 100,
            title={"text": "Win Probability"},
            gauge={
                "axis": {"range": [0, 100]},
                "bar": {"color": "#4CAF50" if win_prob > 0.5 else "#F44336"},
                "steps": [
                    {"range": [0, 40], "color": "#FFCDD2"},
                    {"range": [40, 60], "color": "#FFF9C4"},
                    {"range": [60, 100], "color": "#C8E6C9"},
                ],
            },
        ))
        fig.update_layout(height=250, margin=dict(t=40, b=10, l=30, r=30))
        st.plotly_chart(fig, use_container_width=True)

    elif is_future_week:
        # ── Future week: no scoreboard — show team projection view ──────────
        team_label = my_team.get("team_name", "My Team")
        st.subheader(f"{team_label} Projection — {week_label}")

        me_proj = project_team(my_team, pro_schedule, props_data)
        me_tr = compute_games_tracking(me_proj)

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Projected Total", f"{me_proj['projected_total']:.0f}")
        with c2:
            st.metric("Active Games", me_tr["active_lineup_remaining"])
        with c3:
            st.metric("Bench Games (wasted)", me_tr["bench_remaining"])
        with c4:
            st.metric("Capture Rate", f"{me_tr['capture_rate']:.0%}")

        # Daily breakdown
        daily = me_tr.get("daily_breakdown", [])
        if daily:
            st.subheader("Daily Game Breakdown")
            daily_rows = []
            for d in daily:
                daily_rows.append({
                    "Date": d["date"],
                    "Players Playing": d["players_playing"],
                    "Active (captured)": d["active"],
                    "Bench (wasted)": d["bench_wasted"],
                    "Players": ", ".join(sorted(d["players"])),
                })
            st.dataframe(pd.DataFrame(daily_rows), width="stretch", hide_index=True)

        # League-wide projections for this future week
        st.subheader(f"League Projections — {week_label}")
        league_rows = []
        for t in teams:
            t_proj = project_team(t, pro_schedule, props_data)
            t_tr = compute_games_tracking(t_proj)
            is_me = t["team_id"] == my_id
            league_rows.append({
                "Team": t["team_name"] + (" *" if is_me else ""),
                "Projected Pts": round(t_proj["projected_total"], 1),
                "Active Games": t_tr["active_lineup_remaining"],
                "Bench (wasted)": t_tr["bench_remaining"],
                "Potential Games": t_tr["potential_remaining"],
                "Capture Rate": f"{t_tr['capture_rate']:.0%}",
            })
        league_rows.sort(key=lambda x: x["Projected Pts"], reverse=True)
        st.dataframe(pd.DataFrame(league_rows), width="stretch", hide_index=True)

        # Bar chart of projected totals
        df = pd.DataFrame(league_rows)
        fig = px.bar(df, x="Team", y="Projected Pts",
                     title=f"Projected Points — {week_label}",
                     color="Active Games", color_continuous_scale="Viridis")
        fig.update_layout(height=400, xaxis_tickangle=-45, margin=dict(t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No active matchup found for your team this period.")

    # Standings
    st.subheader("Standings")
    rows = []
    for t in sorted(teams, key=lambda x: (-x["wins"], -x["points_for"])):
        rows.append({
            "Team": t["team_name"], "W": t["wins"], "L": t["losses"],
            "PF": round(t["points_for"], 1), "PA": round(t["points_against"], 1),
            "Diff": round(t["points_for"] - t["points_against"], 1),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


# ─── Shared: games tracking comparison ────────────────────────────────────────

def _render_games_tracking_comparison(me_name, me_proj, opp_name, opp_proj):
    """Render side-by-side games tracking for two teams."""
    me_tr = compute_games_tracking(me_proj)
    opp_tr = compute_games_tracking(opp_proj)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**{me_name}**")
        st.markdown(
            f"- Active games: **{me_tr['active_lineup_remaining']}**\n"
            f"- Bench games (wasted): {me_tr['bench_remaining']}\n"
            f"- Potential (excl IR): **{me_tr['potential_remaining']}**\n"
            f"- IR games (excluded): {me_tr['ir_remaining']}\n"
            f"- Capture rate: **{me_tr['capture_rate']:.0%}**\n"
            f"- Projected pts: **{me_proj['projected_total']:.0f}**"
        )
    with c2:
        st.markdown(f"**{opp_name}**")
        st.markdown(
            f"- Active games: **{opp_tr['active_lineup_remaining']}**\n"
            f"- Bench games (wasted): {opp_tr['bench_remaining']}\n"
            f"- Potential (excl IR): **{opp_tr['potential_remaining']}**\n"
            f"- IR games (excluded): {opp_tr['ir_remaining']}\n"
            f"- Capture rate: **{opp_tr['capture_rate']:.0%}**\n"
            f"- Projected pts: **{opp_proj['projected_total']:.0f}**"
        )


# ─── Tab 2: Matchups ─────────────────────────────────────────────────────────

def render_matchups(teams_by_id, scoreboard, pro_schedule, props_data=None):
    st.header("All Matchups")

    with st.expander("Definitions / Methodology"):
        st.markdown(METHODOLOGY_PROJECTION)
        st.markdown(METHODOLOGY_WIN_PROB)

    if not scoreboard:
        # Future week: show league-wide team projections instead
        if teams_by_id:
            st.caption("No scoreboard data for this week. Showing all teams ranked by projected points.")
            rows = []
            for t in teams_by_id.values():
                t_proj = project_team(t, pro_schedule, props_data)
                t_tr = compute_games_tracking(t_proj)
                rows.append({
                    "Team": t.get("team_name", "Unknown"),
                    "Projected Pts": round(t_proj["projected_total"], 1),
                    "Active Games": t_tr["active_lineup_remaining"],
                    "Bench (wasted)": t_tr["bench_remaining"],
                    "Potential Games": t_tr["potential_remaining"],
                    "Capture Rate": f"{t_tr['capture_rate']:.0%}",
                })
            rows.sort(key=lambda x: x["Projected Pts"], reverse=True)
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.info("No matchup data available.")
        return

    summaries = []
    for m in scoreboard:
        home = teams_by_id.get(m["home_team_id"], {"team_name": "Unknown", "roster": []})
        away = teams_by_id.get(m["away_team_id"], {"team_name": "Unknown", "roster": []})
        proj = project_matchup(
            home, away, pro_schedule,
            home_score=m.get("home_score", 0) or 0,
            away_score=m.get("away_score", 0) or 0,
            sigma_pct=cfg.WIN_PROB_SIGMA_PCT,
            props_data=props_data,
        )
        summaries.append({
            "matchup": proj,
            "home_name": home.get("team_name", "Unknown"),
            "away_name": away.get("team_name", "Unknown"),
        })

    rows = []
    for ms in sorted(summaries, key=lambda x: abs(x["matchup"]["projected_margin"]), reverse=True):
        p = ms["matchup"]
        rows.append({
            "Home": ms["home_name"], "Away": ms["away_name"],
            "Home Score": p["home_current_score"], "Away Score": p["away_current_score"],
            "Home Proj": p["home_projected_total"], "Away Proj": p["away_projected_total"],
            "Margin": p["projected_margin"],
            "Home Win%": f"{p['home_win_prob']:.0%}",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader("Matchup Details")
    for ms in summaries:
        p = ms["matchup"]
        with st.expander(f"{ms['home_name']} vs {ms['away_name']}  |  "
                         f"Proj: {p['home_projected_total']:.0f} - {p['away_projected_total']:.0f}"):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**{ms['home_name']}** — Projected: {p['home_projected_total']:.0f}")
                _render_roster_table(p["home"]["roster"])
            with c2:
                st.markdown(f"**{ms['away_name']}** — Projected: {p['away_projected_total']:.0f}")
                _render_roster_table(p["away"]["roster"])


# ─── Tab 3: Team Detail ──────────────────────────────────────────────────────

def render_team_detail(teams, pro_schedule, props_data=None):
    st.header("Team Detail")

    with st.expander("Definitions / Methodology"):
        st.markdown(METHODOLOGY_PROJECTION)
        st.markdown(METHODOLOGY_GAMES)

    team_names = [f"{t['team_name']} (ID: {t['team_id']})" for t in teams]
    default_idx = next((i for i, t in enumerate(teams) if t["team_id"] == cfg.ESPN_TEAM_ID), 0)
    selected = st.selectbox("Select Team", team_names, index=default_idx)
    team = teams[team_names.index(selected)]

    proj = project_team(team, pro_schedule, props_data)
    tracking = compute_games_tracking(proj)

    # Summary metrics
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Record", f"{team['wins']}-{team['losses']}")
    with c2:
        st.metric("Points For", f"{team['points_for']:.0f}")
    with c3:
        st.metric("Projected Remaining", f"{proj['projected_total']:.0f}")
    with c4:
        rate = tracking["capture_rate"]
        st.metric("Capture Rate", f"{rate:.0%}",
                   delta="Good" if rate >= 0.8 else "Low - optimize lineup",
                   delta_color="normal" if rate >= 0.8 else "inverse")

    # Games tracking
    st.subheader("Games Tracking (This Week)")
    g1, g2, g3, g4 = st.columns(4)
    with g1:
        st.metric("Active Lineup Games", tracking["active_lineup_remaining"])
    with g2:
        st.metric("Bench Games", tracking["bench_remaining"])
    with g3:
        st.metric("Potential Remaining", tracking["potential_remaining"],
                   help="Active + Bench games. IR excluded.")
    with g4:
        st.metric("IR Games (excluded)", tracking["ir_remaining"])

    # Roster table
    st.subheader("Roster")
    _render_roster_table(proj["roster"], show_chart=True)


# ─── Tab 4: Streamers ────────────────────────────────────────────────────────

def render_streamers(my_team, pro_schedule, client, cache, selected_period=None, props_data=None):
    st.header("Streaming Opportunities")

    with st.expander("Definitions / Methodology"):
        st.markdown(METHODOLOGY_STREAMERS)
        st.markdown(METHODOLOGY_PROJECTION)

    gaps = find_streaming_gaps(my_team, pro_schedule)
    if gaps:
        st.subheader("Roster Gaps")
        st.dataframe(pd.DataFrame(gaps), width="stretch", hide_index=True)
    else:
        st.success("No obvious roster gaps this week.")

    st.subheader("Top Free Agents")

    ref_date = None
    if selected_period:
        try:
            ref_date = client.week_reference_date(selected_period)
        except Exception:
            pass

    try:
        cache_key = f"free_agents_{selected_period}" if selected_period else "free_agents"
        free_agents = cache.get_or_fetch(
            cache_key, lambda: client.get_free_agents(size=50, reference_date=ref_date))
    except Exception as e:
        st.warning(f"Could not load free agents: {e}")
        free_agents = []

    if free_agents:
        positions = sorted(set(p.get("position", "") for p in free_agents if p.get("position")))
        sel_pos = st.multiselect("Filter by position", positions, default=[])

        fa_rows = []
        for p in free_agents:
            pos = p.get("position", "")
            if sel_pos and pos not in sel_pos:
                continue
            proj_p = project_player(p, props_data=props_data)
            fa_rows.append({
                "Player": p.get("name", ""),
                "Pos": pos,
                "Team": p.get("pro_team", "FA"),
                "Avg/G": proj_p.get("per_game_avg", 0.0),
                "Source": proj_p.get("projection_method", ""),
                "Games": proj_p.get("games_remaining", 0),
                "Projected": proj_p.get("projected_points", 0.0),
                "% Owned": p.get("percent_owned", 0),
                "Status": p.get("injury_status", "ACTIVE"),
                "Note": proj_p.get("projection_note", ""),
            })

        fa_rows.sort(key=lambda x: x["Projected"], reverse=True)
        if fa_rows:
            st.dataframe(pd.DataFrame(fa_rows), width="stretch", hide_index=True)
        else:
            st.info("No free agents match your filter.")
    else:
        st.info("No free agent data available.")


# ─── Tab 5: Trends ───────────────────────────────────────────────────────────

def render_trends(teams):
    st.header("Player Trends & Alerts")

    with st.expander("Definitions / Methodology"):
        st.markdown(METHODOLOGY_TRENDS)

    team_names = [f"{t['team_name']} (ID: {t['team_id']})" for t in teams]
    default_idx = next((i for i, t in enumerate(teams) if t["team_id"] == cfg.ESPN_TEAM_ID), 0)
    selected = st.selectbox("Select Team for Trends", team_names, index=default_idx, key="trend_team")
    team = teams[team_names.index(selected)]

    roster = team.get("roster", [])
    if not roster:
        st.info("No roster data.")
        return

    all_alerts = []
    for player in roster:
        result = detect_trends(player)
        for trend in result.get("trends", []):
            all_alerts.append({
                "Player": result["player"],
                "Trend": trend["trend"],
                "Detail": trend["detail"],
                "Severity": trend["severity"],
            })

    if all_alerts:
        severity_order = {"high": 0, "medium": 1, "low": 2}
        all_alerts.sort(key=lambda x: severity_order.get(x["Severity"], 3))

        icons = {"HOT": "🔥", "COLD": "🥶", "RISING": "📈", "DECLINING": "📉"}
        for a in all_alerts:
            icon = icons.get(a["Trend"], "ℹ️")
            st.markdown(f"{icon} **{a['Player']}** — **{a['Trend']}** ({a['Severity']}) — {a['Detail']}")

        st.divider()
        st.dataframe(pd.DataFrame(all_alerts), width="stretch", hide_index=True)
    else:
        st.info("No significant trends detected. Players are performing near their season averages.")

    # Comparison chart
    st.subheader("Season vs Recent Performance")
    chart_data = []
    for player in roster:
        name = player.get("name", "Unknown")
        season = player.get("season_avg", 0.0) or player.get("avg_points", 0.0) or 0.0
        last_7 = player.get("last_7_avg", 0.0) or 0.0
        espn_proj = player.get("espn_projected_avg", 0.0) or 0.0
        if season > 0:
            chart_data.append({
                "Player": name,
                "Season Avg": round(season, 1),
                "Last 7d Avg": round(last_7, 1),
                "ESPN Projected": round(espn_proj, 1),
            })

    if chart_data:
        df = pd.DataFrame(chart_data).sort_values("Season Avg", ascending=False).head(15)
        fig = go.Figure()
        fig.add_trace(go.Bar(name="Season Avg", x=df["Player"], y=df["Season Avg"], marker_color="#90CAF9"))
        fig.add_trace(go.Bar(name="Last 7d Avg", x=df["Player"], y=df["Last 7d Avg"], marker_color="#4CAF50"))
        fig.add_trace(go.Bar(name="ESPN Projected", x=df["Player"], y=df["ESPN Projected"], marker_color="#FFA726"))
        fig.update_layout(barmode="group", height=400, margin=dict(t=40, b=10), xaxis_tickangle=-45,
                          title="Season Avg vs Last 7 Days vs ESPN Projected")
        st.plotly_chart(fig, use_container_width=True)


# ─── Tab 6: Analytics ────────────────────────────────────────────────────────

def render_analytics(teams):
    st.header("League Analytics")

    with st.expander("Definitions / Methodology"):
        st.markdown(METHODOLOGY_ANALYTICS)

    st.subheader("Best Current Team")
    st.caption("Who has the strongest roster right now?")

    result = best_current_team_analysis(teams)
    rankings = result["rankings"]

    rows = []
    for i, r in enumerate(rankings):
        rows.append({
            "Rank": i + 1,
            "Team": r["team_name"],
            "Weighted Recent": r["weighted_recent_strength"],
            "Season Avg": r["season_avg_strength"],
            "Season Total PF": r["season_total_points"],
            "Top Player": r["top_contributors"][0]["player"] if r["top_contributors"] else "-",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.caption(f"League avg weighted recent strength: **{result['league_avg_strength']}**")

    # Chart: weighted recent vs season avg
    df = pd.DataFrame(rows)
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Weighted Recent", x=df["Team"], y=df["Weighted Recent"],
                         marker_color="#4CAF50"))
    fig.add_trace(go.Bar(name="Season Avg", x=df["Team"], y=df["Season Avg"],
                         marker_color="#90CAF9"))
    fig.update_layout(barmode="group", height=400, xaxis_tickangle=-45,
                      title="Current Roster Strength: Weighted Recent vs Season Average",
                      margin=dict(t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)

    # Expandable details
    for r in rankings:
        with st.expander(f"{r['team_name']} — Strength: {r['weighted_recent_strength']}"):
            if r["top_contributors"]:
                st.dataframe(pd.DataFrame(r["top_contributors"]),
                             width="stretch", hide_index=True)


# ─── Shared: roster table ────────────────────────────────────────────────────

def _render_roster_table(roster: list[dict], show_chart: bool = False):
    if not roster:
        st.info("No roster data.")
        return

    rows = []
    for p in roster:
        rows.append({
            "Player": p.get("name", ""),
            "Pos": p.get("position", ""),
            "Team": p.get("pro_team", ""),
            "Slot": p.get("lineup_slot", ""),
            "Avg/G": p.get("per_game_avg", p.get("avg_points", 0.0)),
            "Source": p.get("projection_method", ""),
            "Games": p.get("games_remaining", 0),
            "Projected": p.get("projected_points", 0.0),
            "Status": p.get("injury_status", "ACTIVE"),
            "Note": p.get("projection_note", ""),
        })

    df = pd.DataFrame(rows).sort_values("Projected", ascending=False).reset_index(drop=True)
    st.dataframe(df, width="stretch", hide_index=True)

    if show_chart and len(df) > 0:
        chart_df = df[df["Projected"] > 0].head(15)
        if not chart_df.empty:
            fig = px.bar(chart_df, x="Player", y="Projected", color="Source",
                         title="Projected Points by Player (colored by source)",
                         labels={"Projected": "Projected Pts", "Player": ""})
            fig.update_layout(height=350, margin=dict(t=40, b=10), xaxis_tickangle=-45)
            st.plotly_chart(fig, use_container_width=True)


# ─── Entry ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
