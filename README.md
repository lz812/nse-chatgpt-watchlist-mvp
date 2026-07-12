# NSE ChatGPT Premarket Watchlist MVP

A no-broker-API research prototype that:

1. Runs a point-in-time-safe daily scan in GitHub Actions.
2. Uses the current Nifty 500 list from NSE Archives.
3. Uses `yfinance` for last completed daily bars and global proxies.
4. Publishes a sanitized top-10 feed through GitHub Pages.
5. Lets a Custom GPT browse that feed, verify official sources, and add
   EIC / filing / DRHP analysis.

## Important boundary

This project creates a **premarket research watchlist**, not a real-time
trade signal. It has no broker-grade IEP/IEQ, spread, depth, VWAP, opening
range or current intraday relative volume.

The displayed Opportunity Score is **not a success probability**.

## Files

- `src/scan.py`: data fetch, point-in-time-safe features and ranking
- `.github/workflows/premarket.yml`: scheduled and manual workflow
- `docs/feed.json`: machine-readable feed
- `docs/latest.md`: readable feed for ChatGPT
- `docs/index.html`: GitHub Pages dashboard
- `CUSTOM_GPT_MVP_INSTRUCTIONS.md`: interim GPT instructions
- `DATA_CONTRACT.md`: field meanings and rules

## Setup

1. Create a new GitHub repository.
2. Upload this project.
3. Open **Settings → Pages**.
4. Under **Build and deployment → Source**, select **GitHub Actions**.
5. Open **Actions** and run **Build and deploy premarket watchlist** manually.
6. Wait for the workflow and the `github-pages` deployment to finish.
7. Open the Pages address shown by GitHub and confirm the watchlist loads.
8. Copy the GitHub Pages address into the Custom GPT instructions.

## Schedule

The workflow is scheduled on weekdays at:

- 08:35 IST
- 08:55 IST
- 09:05 IST
- 09:10 IST

GitHub scheduled jobs may start late. Use the manual **Run workflow** button
when timing matters.

## Security

This version needs no broker credentials and no API secrets.

Never upload:

- Kite keys or access tokens
- Broker account data
- Trading passwords or TOTP seeds
- Personal portfolio details
- Proprietary training databases

If the ranking code is proprietary, use two repositories:

- A private engine repository
- A public feed repository containing only `feed.json`, `latest.md` and
  `index.html`

## Local run

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python src/scan.py
```

## Later broker-data upgrade

A broker or authorised data feed can later replace only the data adapters.
The output contract can stay stable, so the Custom GPT does not need a full
redesign.


## Version 1.1 upgrade

The scanner now:

- Skips NSE equity weekends and published holidays
- Publishes a market-closed page on closed sessions
- Uses trading-session-aware freshness checks
- Adds prior-day high, low, pivot, range and close-location data
- Calculates an estimated previous-session volume profile for the leading pool
- Adds delayed/free global overnight high-low context

The repository carries `data/nse_equity_holidays.json` as a fallback and also
attempts to refresh the current-year calendar from official NSE sources.
Ad-hoc closures should still be verified against the latest exchange circular.
