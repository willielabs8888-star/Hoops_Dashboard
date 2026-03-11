# Fantasy Basketball Dashboard

ESPN Fantasy Basketball dashboard with live projections, matchup analysis, and sportsbook-powered player projections.

## Features

- **Matchup Overview** — Win probability, projected totals, game tracking
- **All Matchups** — League-wide matchup projections ranked by margin
- **Team Detail** — Per-player projections, games tracking, capture rate
- **Streaming Picks** — Roster gaps and top free agents
- **Player Trends** — Hot/cold/rising/declining player detection
- **League Analytics** — Current roster strength rankings
- **Sportsbook Props** — Uses DraftKings/FanDuel player prop lines for more accurate projections
- **Global Team Selector** — View any team's perspective from the sidebar

## Setup

1. Clone this repo
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `secrets.toml.template` to `.env` and fill in your values
4. Run: `streamlit run app/dashboard/main.py`

## Deploy to Streamlit Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Deploy pointing to `app/dashboard/main.py`
4. Paste your secrets into Settings > Secrets (use `secrets.toml.template` as reference)

## ESPN Cookies

To find your ESPN cookies:
1. Go to fantasy.espn.com and log in
2. Open DevTools (F12) > Application > Cookies > espn.com
3. Copy `espn_s2` and `SWID` values

## Player Props (Optional)

Sign up for a free API key at [the-odds-api.com](https://the-odds-api.com) (500 requests/month free) to enable sportsbook-based projections.
