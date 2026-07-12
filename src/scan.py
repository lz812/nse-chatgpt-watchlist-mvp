#!/usr/bin/env python3
"""
Free-data NSE premarket watchlist generator.

Purpose
-------
Build a point-in-time-safe, PRELIMINARY top-10 watchlist before the NSE open
without a broker API. It uses:
- The current Nifty 500 constituent CSV from NSE Archives.
- Last completed daily bars from Yahoo Finance through yfinance.
- Overnight/global proxy data from yfinance.

It intentionally does NOT produce:
- A calibrated success probability.
- A live executable entry.
- Broker-grade pre-open IEP/IEQ, depth, spread, VWAP or intraday RVOL.

The custom GPT should add official filing/news/EIC research and classify the
result as a watchlist until live confirmation is available.
"""

from __future__ import annotations

import html
import json
import math
import os
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf


IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

NIFTY500_CSV = (
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
)

MAX_UNIVERSE = int(os.getenv("MAX_UNIVERSE", "500"))
TOP_N = min(max(int(os.getenv("TOP_N", "10")), 1), 10)
MIN_HISTORY = 65
MIN_PRICE = 20.0
MIN_AVG_TURNOVER_CR = 5.0
YF_PERIOD = "18mo"

# A free-data feed is deliberately capped below 100 trust because it is not
# broker-grade, real-time, or contractually guaranteed.
FREE_FEED_TRUST_CAP = 72


@dataclass
class Candidate:
    rank: int
    symbol: str
    company: str
    industry: str
    last_completed_session: str
    previous_close: float
    avg_turnover_20d_cr: float
    previous_day_rvol: float
    atr14_pct: float
    rsi14: float
    momentum_5d_pct: float
    momentum_20d_pct: float
    above_sma50: bool
    above_sma200: bool | None
    near_52w_high: bool | None
    technical_score: float
    liquidity_score: float
    macro_alignment_score: float
    preliminary_opportunity_score: float
    trust_rate: int
    status: str
    reason_codes: list[str]
    missing_fields: list[str]


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def fetch_universe() -> pd.DataFrame:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; research-watchlist/1.0)",
        "Accept": "text/csv,*/*",
    }
    response = requests.get(NIFTY500_CSV, headers=headers, timeout=25)
    response.raise_for_status()
    universe = pd.read_csv(StringIO(response.text))

    normalized = {str(c).strip().lower(): c for c in universe.columns}
    symbol_col = normalized.get("symbol")
    company_col = normalized.get("company name")
    industry_col = normalized.get("industry")

    if symbol_col is None:
        raise RuntimeError("NSE constituent file has no Symbol column")

    result = pd.DataFrame(
        {
            "symbol": universe[symbol_col].astype(str).str.strip().str.upper(),
            "company": (
                universe[company_col].astype(str).str.strip()
                if company_col is not None
                else universe[symbol_col].astype(str).str.strip()
            ),
            "industry": (
                universe[industry_col].astype(str).str.strip()
                if industry_col is not None
                else "Unknown"
            ),
        }
    )
    result = result.drop_duplicates("symbol")
    result = result[result["symbol"].str.match(r"^[A-Z0-9&-]+$", na=False)]
    return result.head(MAX_UNIVERSE).reset_index(drop=True)


def fetch_prices(symbols: list[str]) -> pd.DataFrame:
    tickers = [f"{s}.NS" for s in symbols]
    data = yf.download(
        tickers=tickers,
        period=YF_PERIOD,
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
        actions=False,
    )
    if data.empty:
        raise RuntimeError("yfinance returned no NSE data")
    return data


def get_ticker_frame(download: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    try:
        if isinstance(download.columns, pd.MultiIndex):
            # group_by='ticker' generally creates ticker at level 0.
            if ticker in download.columns.get_level_values(0):
                frame = download[ticker].copy()
            elif ticker in download.columns.get_level_values(1):
                frame = download.xs(ticker, axis=1, level=1).copy()
            else:
                return None
        else:
            frame = download.copy()

        frame.columns = [str(c).lower() for c in frame.columns]
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(set(frame.columns)):
            return None

        frame = frame[list(required)].copy()
        frame = frame.replace([np.inf, -np.inf], np.nan)
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
        frame.index = pd.to_datetime(frame.index)

        # At premarket time, only last COMPLETED Indian session may be used.
        today_ist = now_ist().date()
        local_dates = pd.Series(frame.index.date, index=frame.index)
        frame = frame[local_dates < today_ist]
        return frame.sort_index()
    except Exception:
        return None


def rsi14(close: pd.Series) -> float:
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(14).mean().iloc[-1]
    avg_loss = losses.rolling(14).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return float("nan")
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def atr14_pct(frame: pd.DataFrame) -> float:
    prev_close = frame["close"].shift(1)
    tr = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    close = frame["close"].iloc[-1]
    if pd.isna(atr) or close <= 0:
        return float("nan")
    return float(atr / close * 100)


def last_return(ticker: str) -> float | None:
    try:
        frame = yf.download(
            ticker,
            period="7d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            actions=False,
        )
        if frame.empty:
            return None
        close = frame["Close"].dropna()
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        if len(close) < 2:
            return None
        return float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
    except Exception:
        return None


def fetch_macro() -> dict[str, float | None]:
    return {
        "qqq_ret_pct": last_return("QQQ"),
        "soxx_ret_pct": last_return("SOXX"),
        "crude_ret_pct": last_return("CL=F"),
        "gold_ret_pct": last_return("GC=F"),
        "usd_inr_ret_pct": last_return("USDINR=X"),
        "india_vix_ret_pct": last_return("^INDIAVIX"),
    }


def score_macro_alignment(industry: str, macro: dict[str, float | None]) -> tuple[float, list[str]]:
    industry_l = industry.lower()
    score = 5.0
    reasons: list[str] = []

    qqq = macro.get("qqq_ret_pct")
    soxx = macro.get("soxx_ret_pct")
    crude = macro.get("crude_ret_pct")

    if any(k in industry_l for k in ("information technology", "software", "computers")):
        if qqq is not None and qqq > 0.5:
            score += 2.5
            reasons.append("GLOBAL_TECH_POSITIVE")
        if soxx is not None and soxx > 1.0:
            score += 2.5
            reasons.append("SEMICONDUCTOR_PROXY_POSITIVE")

    if any(k in industry_l for k in ("oil", "gas", "petroleum", "exploration")):
        if crude is not None and crude > 1.0:
            score += 3.0
            reasons.append("CRUDE_POSITIVE_FOR_ENERGY")

    if any(k in industry_l for k in ("airline", "aviation", "paint", "tyre")):
        if crude is not None and crude > 2.0:
            score -= 2.0
            reasons.append("CRUDE_COST_HEADWIND")

    return float(min(max(score, 0.0), 10.0)), reasons


def compute_candidate(
    symbol: str,
    company: str,
    industry: str,
    frame: pd.DataFrame,
    macro: dict[str, float | None],
) -> dict[str, Any] | None:
    if frame is None or len(frame) < MIN_HISTORY:
        return None

    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    last_close = float(close.iloc[-1])

    if last_close < MIN_PRICE:
        return None

    avg_volume_20 = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else float("nan")
    prev_rvol = float(volume.iloc[-1] / avg_volume_20) if avg_volume_20 > 0 else float("nan")
    avg_turnover_cr = float((close.iloc[-20:] * volume.iloc[-20:]).mean() / 1e7)

    if not math.isfinite(avg_turnover_cr) or avg_turnover_cr < MIN_AVG_TURNOVER_CR:
        return None

    sma50 = float(close.iloc[-50:].mean())
    sma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else float("nan")
    rsi = rsi14(close)
    atr_pct = atr14_pct(frame)
    mom5 = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else float("nan")
    mom20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else float("nan")

    high_52 = float(frame["high"].iloc[-252:].max()) if len(frame) >= 252 else float("nan")
    near_52 = bool(last_close >= high_52 * 0.95) if math.isfinite(high_52) else None

    above50 = bool(last_close > sma50)
    above200 = bool(last_close > sma200) if math.isfinite(sma200) else None

    reasons: list[str] = []
    missing: list[str] = []

    technical = 0.0

    # Previous-day volume shock: available before the next open.
    if math.isfinite(prev_rvol):
        if prev_rvol >= 2.5:
            technical += 12
            reasons.append("PRIOR_DAY_RVOL_VERY_HIGH")
        elif prev_rvol >= 1.7:
            technical += 9
            reasons.append("PRIOR_DAY_RVOL_HIGH")
        elif prev_rvol >= 1.2:
            technical += 5
            reasons.append("PRIOR_DAY_RVOL_ELEVATED")
    else:
        missing.append("previous_day_rvol")

    if above50:
        technical += 7
        reasons.append("ABOVE_SMA50")

    if above200 is True:
        technical += 5
        reasons.append("ABOVE_SMA200")
    elif above200 is None:
        missing.append("above_sma200")

    if math.isfinite(rsi):
        if 50 <= rsi <= 68:
            technical += 6
            reasons.append("RSI_CONSTRUCTIVE")
        elif rsi > 75:
            technical -= 3
            reasons.append("RSI_OVEREXTENDED")
    else:
        missing.append("rsi14")

    if math.isfinite(mom5):
        if 0.5 <= mom5 <= 8:
            technical += 6
            reasons.append("MOMENTUM_5D_POSITIVE")
        elif mom5 < -5:
            technical -= 3
            reasons.append("MOMENTUM_5D_WEAK")
    else:
        missing.append("momentum_5d")

    if math.isfinite(mom20):
        if 1 <= mom20 <= 18:
            technical += 5
            reasons.append("MOMENTUM_20D_POSITIVE")
        elif mom20 < -10:
            technical -= 3
            reasons.append("MOMENTUM_20D_WEAK")
    else:
        missing.append("momentum_20d")

    if near_52 is True:
        technical += 4
        reasons.append("NEAR_52W_HIGH")
    elif near_52 is None:
        missing.append("near_52w_high")

    if math.isfinite(atr_pct):
        if 1.5 <= atr_pct <= 5.5:
            technical += 5
            reasons.append("ATR_TRADEABLE")
        elif atr_pct > 8:
            technical -= 3
            reasons.append("ATR_EXTREME")
    else:
        missing.append("atr14_pct")

    technical = float(min(max(technical, 0.0), 45.0))

    # Liquidity score, up to 35.
    if avg_turnover_cr >= 250:
        liquidity = 35.0
        reasons.append("LIQUIDITY_VERY_HIGH")
    elif avg_turnover_cr >= 100:
        liquidity = 30.0
        reasons.append("LIQUIDITY_HIGH")
    elif avg_turnover_cr >= 50:
        liquidity = 24.0
        reasons.append("LIQUIDITY_GOOD")
    elif avg_turnover_cr >= 20:
        liquidity = 17.0
        reasons.append("LIQUIDITY_ACCEPTABLE")
    else:
        liquidity = 10.0

    macro_score, macro_reasons = score_macro_alignment(industry, macro)
    reasons.extend(macro_reasons)

    # 90 possible points at this stage. The remaining 10 are intentionally
    # reserved for official catalyst/pre-open verification by the custom GPT.
    preliminary = technical + liquidity + macro_score
    preliminary = float(min(max(preliminary / 90 * 100, 0.0), 100.0))

    trust = FREE_FEED_TRUST_CAP
    trust -= 5 * len(missing)
    age_days = (now_ist().date() - frame.index[-1].date()).days
    if age_days > 4:
        trust -= 20
        reasons.append("LAST_SESSION_POSSIBLY_STALE")
    elif age_days > 2:
        trust -= 7
    trust = int(min(max(trust, 0), FREE_FEED_TRUST_CAP))

    if preliminary >= 76 and trust >= 60:
        status = "PRIMARY_WATCH"
    elif preliminary >= 64 and trust >= 52:
        status = "SECONDARY_WATCH"
    else:
        status = "CONDITIONAL"

    return {
        "symbol": symbol,
        "company": company,
        "industry": industry,
        "last_completed_session": frame.index[-1].date().isoformat(),
        "previous_close": round(last_close, 2),
        "avg_turnover_20d_cr": round(avg_turnover_cr, 2),
        "previous_day_rvol": round(prev_rvol, 2) if math.isfinite(prev_rvol) else None,
        "atr14_pct": round(atr_pct, 2) if math.isfinite(atr_pct) else None,
        "rsi14": round(rsi, 1) if math.isfinite(rsi) else None,
        "momentum_5d_pct": round(mom5, 2) if math.isfinite(mom5) else None,
        "momentum_20d_pct": round(mom20, 2) if math.isfinite(mom20) else None,
        "above_sma50": above50,
        "above_sma200": above200,
        "near_52w_high": near_52,
        "technical_score": round(technical, 1),
        "liquidity_score": round(liquidity, 1),
        "macro_alignment_score": round(macro_score, 1),
        "preliminary_opportunity_score": round(preliminary, 1),
        "trust_rate": trust,
        "status": status,
        "reason_codes": reasons[:15],
        "missing_fields": missing,
    }


def safe_number(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def build_markdown(feed: dict[str, Any]) -> str:
    lines = [
        "# NSE Premarket Research Watchlist",
        "",
        f"- Generated: **{feed['generated_at']}**",
        f"- Data status: **{feed['data_status']}**",
        f"- Actionable: **No — research watchlist only**",
        f"- Universe processed: **{feed['universe_processed']}**",
        f"- Model: **{feed['model_version']}**",
        "",
        "> This free-data feed is not broker-grade and does not include a "
        "live spread, depth, IEP/IEQ, VWAP, opening range or intraday RVOL. "
        "Do not treat it as an executable buy/sell signal.",
        "",
        "## Candidates",
        "",
        "| Rank | Symbol | Industry | Status | Opportunity | Trust | Prev close | Prev RVOL | ATR% |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for c in feed["candidates"]:
        lines.append(
            f"| {c['rank']} | {c['symbol']} | {c['industry']} | {c['status']} | "
            f"{c['preliminary_opportunity_score']:.1f} | {c['trust_rate']} | "
            f"{c['previous_close']:.2f} | "
            f"{c['previous_day_rvol'] if c['previous_day_rvol'] is not None else 'NA'} | "
            f"{c['atr14_pct'] if c['atr14_pct'] is not None else 'NA'} |"
        )

    lines.extend(
        [
            "",
            "## Required GPT verification",
            "",
            "For every candidate, verify official NSE announcements, current "
            "pre-open information when accessible, sector context, adverse "
            "governance/regulatory events, and data freshness. For an IPO or "
            "new listing, verify SEBI DRHP/RHP and the applicable special "
            "pre-open session.",
            "",
            "## Macro snapshot",
            "",
            "```json",
            json.dumps(feed["macro"], indent=2),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def build_html(feed: dict[str, Any]) -> str:
    rows = []
    for c in feed["candidates"]:
        rows.append(
            "<tr>"
            f"<td>{c['rank']}</td>"
            f"<td><strong>{html.escape(c['symbol'])}</strong></td>"
            f"<td>{html.escape(c['company'])}</td>"
            f"<td>{html.escape(c['industry'])}</td>"
            f"<td>{html.escape(c['status'])}</td>"
            f"<td>{c['preliminary_opportunity_score']:.1f}</td>"
            f"<td>{c['trust_rate']}</td>"
            f"<td>{c['previous_close']:.2f}</td>"
            f"<td>{c['previous_day_rvol'] if c['previous_day_rvol'] is not None else 'NA'}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Premarket Research Watchlist</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:1200px;margin:40px auto;padding:0 18px;line-height:1.45}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#f5f5f5}}
.notice{{padding:14px;border:1px solid #c9a227;background:#fffbea}}
.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:20px 0}}
.card{{border:1px solid #ddd;padding:12px;border-radius:8px}}
code{{background:#f5f5f5;padding:2px 4px}}
</style>
</head>
<body>
<h1>NSE Premarket Research Watchlist</h1>
<div class="notice"><strong>Research-only feed.</strong> This is not an executable
real-time signal and does not include broker-grade pre-open or intraday data.</div>
<div class="meta">
<div class="card"><strong>Generated</strong><br>{html.escape(feed['generated_at'])}</div>
<div class="card"><strong>Data status</strong><br>{html.escape(feed['data_status'])}</div>
<div class="card"><strong>Universe processed</strong><br>{feed['universe_processed']}</div>
<div class="card"><strong>Model</strong><br>{html.escape(feed['model_version'])}</div>
</div>
<table>
<thead><tr><th>Rank</th><th>Symbol</th><th>Company</th><th>Industry</th>
<th>Status</th><th>Opportunity</th><th>Trust</th><th>Prev close</th><th>Prev RVOL</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<h2>How the Custom GPT should use this</h2>
<p>Verify official NSE announcements, current pre-open information when available,
sector conditions and negative risk events. For IPOs, verify the DRHP/RHP on SEBI.
Keep every candidate as a watchlist item until live price/volume confirmation exists.</p>
<p>Structured data: <a href="feed.json"><code>feed.json</code></a></p>
</body>
</html>
"""


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    generated = now_ist()

    try:
        universe = fetch_universe()
        prices = fetch_prices(universe["symbol"].tolist())
        macro = fetch_macro()

        candidates: list[dict[str, Any]] = []
        for row in universe.itertuples(index=False):
            ticker = f"{row.symbol}.NS"
            frame = get_ticker_frame(prices, ticker)
            result = compute_candidate(
                symbol=row.symbol,
                company=row.company,
                industry=row.industry,
                frame=frame,
                macro=macro,
            )
            if result is not None:
                candidates.append(result)

        candidates.sort(
            key=lambda c: (
                c["preliminary_opportunity_score"],
                c["trust_rate"],
                c["avg_turnover_20d_cr"],
            ),
            reverse=True,
        )
        candidates = candidates[:TOP_N]
        for rank, candidate in enumerate(candidates, 1):
            candidate["rank"] = rank

        if not candidates:
            raise RuntimeError("No candidates passed minimum data and liquidity checks")

        macro_clean = {k: safe_number(v) for k, v in macro.items()}

        feed = {
            "generated_at": generated.isoformat(),
            "valid_until": generated.replace(hour=9, minute=15, second=0, microsecond=0).isoformat(),
            "market": "NSE",
            "market_phase": "PREMARKET_RESEARCH",
            "data_status": "FREE_DELAYED_OR_END_OF_DAY",
            "actionable": False,
            "model_version": "free-premarket-v1.0.0",
            "universe_source": NIFTY500_CSV,
            "universe_requested": len(universe),
            "universe_processed": len(candidates),
            "macro": macro_clean,
            "candidates": candidates,
            "limitations": [
                "No broker-grade real-time data",
                "No guaranteed pre-open IEP or IEQ",
                "No live spread or market depth",
                "No live VWAP, opening range or intraday RVOL",
                "Opportunity score is not a calibrated probability",
                "yfinance is an unofficial research data source",
            ],
            "required_next_checks": [
                "Official NSE announcements",
                "Official NSE pre-open page when accessible",
                "Sector and broad-market conditions",
                "Adverse governance or regulatory events",
                "SEBI DRHP/RHP for IPOs and new listings",
                "Post-open live confirmation before any trade",
            ],
        }

        (DOCS / "feed.json").write_text(
            json.dumps(feed, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (DOCS / "latest.md").write_text(build_markdown(feed), encoding="utf-8")
        (DOCS / "index.html").write_text(build_html(feed), encoding="utf-8")
        print(f"Generated {len(candidates)} candidates at {generated.isoformat()}")
        return 0

    except Exception as exc:
        error_feed = {
            "generated_at": generated.isoformat(),
            "market": "NSE",
            "data_status": "ERROR",
            "actionable": False,
            "model_version": "free-premarket-v1.0.0",
            "error": str(exc),
        }
        (DOCS / "feed.json").write_text(
            json.dumps(error_feed, indent=2),
            encoding="utf-8",
        )
        (DOCS / "latest.md").write_text(
            "# NSE Premarket Research Watchlist\n\n"
            f"**Status:** ERROR\n\n**Generated:** {generated.isoformat()}\n\n"
            f"**Message:** {exc}\n",
            encoding="utf-8",
        )
        (DOCS / "index.html").write_text(
            "<!doctype html><meta charset='utf-8'>"
            "<h1>NSE Premarket Research Watchlist</h1>"
            f"<p><strong>ERROR:</strong> {html.escape(str(exc))}</p>",
            encoding="utf-8",
        )
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
