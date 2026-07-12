#!/usr/bin/env python3
"""
NSE free-data premarket research watchlist.

Version 1.1 adds:
- NSE equity holiday/weekend handling.
- Trading-session-aware freshness checks.
- Prior-day high, low, range, pivot and close-location features.
- Previous-session 15-minute volume-profile estimates for top candidates.
- Global overnight high/low proxy context from liquid futures/FX symbols.

Important boundary
------------------
This is a research watchlist, not a real-time trading signal. It does not
provide broker-grade IEP/IEQ, spread, depth, live VWAP, current intraday RVOL,
or guaranteed real-time prices. The opportunity score is not a calibrated
probability of trade success.
"""

from __future__ import annotations

import html
import json
import math
import os
import traceback
from datetime import date, datetime, time as dt_time, timedelta, timezone
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
DATA_DIR = ROOT / "data"

NIFTY500_CSV = (
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
)
HOLIDAY_SOURCES = [
    "https://www.niftyindices.com/resources/holiday-calendar",
]
LOCAL_HOLIDAY_FILE = DATA_DIR / "nse_equity_holidays.json"

MAX_UNIVERSE = int(os.getenv("MAX_UNIVERSE", "500"))
TOP_N = min(max(int(os.getenv("TOP_N", "10")), 1), 10)
PROFILE_POOL_SIZE = min(
    max(int(os.getenv("PROFILE_POOL_SIZE", "30")), TOP_N),
    50,
)
MIN_HISTORY = 65
MIN_PRICE = 20.0
MIN_AVG_TURNOVER_CR = 5.0
DAILY_PERIOD = "18mo"
PROFILE_INTERVAL = "15m"
PROFILE_PERIOD = "10d"
PROFILE_BINS = 24
VALUE_AREA_FRACTION = 0.70

FREE_FEED_TRUST_CAP = 72
MODEL_VERSION = "free-premarket-v1.1.0"


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def clean_float(value: Any, digits: int = 2) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


# ---------------------------------------------------------------------------
# MARKET CALENDAR
# ---------------------------------------------------------------------------

def load_local_holidays(year: int) -> dict[date, str]:
    try:
        payload = json.loads(LOCAL_HOLIDAY_FILE.read_text(encoding="utf-8"))
        raw = payload.get("equity_trading_holidays", {}).get(str(year), {})
        return {
            datetime.strptime(day, "%Y-%m-%d").date(): str(description)
            for day, description in raw.items()
        }
    except Exception:
        return {}


def fetch_official_holidays(year: int) -> tuple[dict[date, str], list[str]]:
    """
    Best-effort refresh from official NSE/NSE Indices pages.

    Those pages occasionally change structure or use client-side rendering, so
    the repository also carries a verified local fallback calendar.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; nse-research-watchlist/1.1)",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.nseindia.com/",
    }
    merged: dict[date, str] = {}
    used_sources: list[str] = []

    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=12)
    except Exception:
        pass

    for source in HOLIDAY_SOURCES:
        try:
            response = session.get(source, headers=headers, timeout=20)
            response.raise_for_status()
            tables = pd.read_html(StringIO(response.text))

            found_for_source = 0
            for table in tables:
                table.columns = [
                    " ".join(map(str, c)).strip().lower()
                    if isinstance(c, tuple)
                    else str(c).strip().lower()
                    for c in table.columns
                ]
                date_columns = [c for c in table.columns if "date" in c]
                if not date_columns:
                    continue

                description_columns = [
                    c for c in table.columns
                    if any(k in c for k in ("description", "occasion", "holiday"))
                ]
                day_col = date_columns[0]
                desc_col = description_columns[0] if description_columns else None

                parsed = pd.to_datetime(
                    table[day_col],
                    dayfirst=True,
                    errors="coerce",
                )
                for idx, parsed_date in parsed.items():
                    if pd.isna(parsed_date) or parsed_date.year != year:
                        continue
                    description = (
                        str(table.loc[idx, desc_col]).strip()
                        if desc_col is not None
                        else "NSE equity trading holiday"
                    )
                    merged[parsed_date.date()] = description
                    found_for_source += 1

            if found_for_source:
                used_sources.append(source)
        except Exception:
            continue

    return merged, used_sources


def load_market_calendar(year: int) -> tuple[dict[date, str], dict[str, Any]]:
    local = load_local_holidays(year)
    remote, remote_sources = fetch_official_holidays(year)

    merged = dict(local)
    merged.update(remote)

    if remote:
        status = "OFFICIAL_REMOTE_PLUS_LOCAL"
    elif local:
        status = "LOCAL_VERIFIED_FALLBACK"
    else:
        status = "UNVERIFIED"

    return merged, {
        "calendar_status": status,
        "remote_sources_used": remote_sources,
        "local_holiday_count": len(local),
        "remote_holiday_count": len(remote),
        "merged_holiday_count": len(merged),
    }


def is_trading_day(day: date, holidays: dict[date, str]) -> bool:
    return day.weekday() < 5 and day not in holidays


def previous_trading_day(day: date, holidays: dict[date, str]) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(cursor, holidays):
            return cursor
        cursor -= timedelta(days=1)
    raise RuntimeError("Unable to determine previous trading day")


def next_trading_day(day: date, holidays: dict[date, str]) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(cursor, holidays):
            return cursor
        cursor += timedelta(days=1)
    raise RuntimeError("Unable to determine next trading day")


def market_closure_reason(day: date, holidays: dict[date, str]) -> str | None:
    if day.weekday() == 5:
        return "Saturday"
    if day.weekday() == 6:
        return "Sunday"
    return holidays.get(day)


# ---------------------------------------------------------------------------
# UNIVERSE AND DAILY DATA
# ---------------------------------------------------------------------------

def fetch_universe() -> pd.DataFrame:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; nse-research-watchlist/1.1)",
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


def fetch_daily_prices(symbols: list[str]) -> pd.DataFrame:
    tickers = [f"{symbol}.NS" for symbol in symbols]
    data = yf.download(
        tickers=tickers,
        period=DAILY_PERIOD,
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
        actions=False,
    )
    if data.empty:
        raise RuntimeError("yfinance returned no NSE daily data")
    return data


def extract_ticker_frame(
    download: pd.DataFrame,
    ticker: str,
    required: tuple[str, ...] = ("open", "high", "low", "close", "volume"),
) -> pd.DataFrame | None:
    try:
        if isinstance(download.columns, pd.MultiIndex):
            if ticker in download.columns.get_level_values(0):
                frame = download[ticker].copy()
            elif ticker in download.columns.get_level_values(1):
                frame = download.xs(ticker, axis=1, level=1).copy()
            else:
                return None
        else:
            frame = download.copy()

        frame.columns = [str(c).lower() for c in frame.columns]
        if not set(required).issubset(frame.columns):
            return None

        frame = frame[list(required)].copy()
        frame = frame.replace([np.inf, -np.inf], np.nan)
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
        frame.index = pd.to_datetime(frame.index)
        return frame.sort_index()
    except Exception:
        return None


def completed_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    today = now_ist().date()
    local_dates = pd.Series(frame.index.date, index=frame.index)
    return frame[local_dates < today].copy()


# ---------------------------------------------------------------------------
# MACRO AND OVERNIGHT PROXIES
# ---------------------------------------------------------------------------

def last_daily_return(ticker: str) -> float | None:
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


def ensure_ist_index(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(timezone.utc)
    frame.index = index.tz_convert(IST)
    return frame


def fetch_overnight_proxy(
    ticker: str,
    start_ist: datetime,
    end_ist: datetime,
) -> dict[str, Any] | None:
    try:
        frame = yf.download(
            ticker,
            period="5d",
            interval="30m",
            auto_adjust=True,
            progress=False,
            actions=False,
        )
        if frame.empty:
            return None

        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [
                c[0] if isinstance(c, tuple) else c
                for c in frame.columns
            ]

        frame.columns = [str(c).lower() for c in frame.columns]
        required = {"open", "high", "low", "close"}
        if not required.issubset(frame.columns):
            return None

        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame = ensure_ist_index(frame)
        window = frame[(frame.index >= start_ist) & (frame.index <= end_ist)]
        if window.empty:
            return None

        start_price = float(window["open"].iloc[0])
        last_price = float(window["close"].iloc[-1])
        high_price = float(window["high"].max())
        low_price = float(window["low"].min())

        return {
            "ticker": ticker,
            "window_start": start_ist.isoformat(),
            "window_end": end_ist.isoformat(),
            "start": clean_float(start_price),
            "last": clean_float(last_price),
            "high": clean_float(high_price),
            "low": clean_float(low_price),
            "change_pct": clean_float(
                (last_price / start_price - 1) * 100
                if start_price > 0 else None
            ),
            "range_pct": clean_float(
                (high_price - low_price) / start_price * 100
                if start_price > 0 else None
            ),
            "data_source": "yfinance_free_delayed_or_unofficial",
        }
    except Exception:
        return None


def fetch_macro(
    expected_previous_session: date,
) -> dict[str, Any]:
    end_ist = now_ist()
    start_ist = datetime.combine(
        expected_previous_session,
        dt_time(hour=15, minute=30),
        tzinfo=IST,
    )

    overnight_symbols = {
        "nasdaq_futures": "NQ=F",
        "sp500_futures": "ES=F",
        "crude_oil": "CL=F",
        "gold": "GC=F",
        "usd_inr": "USDINR=X",
    }

    overnight = {
        name: fetch_overnight_proxy(ticker, start_ist, end_ist)
        for name, ticker in overnight_symbols.items()
    }

    return {
        "previous_us_session": {
            "qqq_ret_pct": clean_float(last_daily_return("QQQ")),
            "soxx_ret_pct": clean_float(last_daily_return("SOXX")),
        },
        "india_vix_previous_session_ret_pct": clean_float(
            last_daily_return("^INDIAVIX")
        ),
        "overnight_proxies": overnight,
        "overnight_definition": (
            "Global futures/FX window from the prior NSE close to scan time. "
            "NSE cash equities do not trade overnight."
        ),
    }


def macro_alignment_score(
    industry: str,
    macro: dict[str, Any],
) -> tuple[float, list[str]]:
    industry_lower = industry.lower()
    score = 5.0
    reasons: list[str] = []

    previous_us = macro.get("previous_us_session", {})
    overnight = macro.get("overnight_proxies", {})

    qqq = previous_us.get("qqq_ret_pct")
    soxx = previous_us.get("soxx_ret_pct")
    nq_change = (overnight.get("nasdaq_futures") or {}).get("change_pct")
    crude_change = (overnight.get("crude_oil") or {}).get("change_pct")

    if any(k in industry_lower for k in ("information technology", "software", "computers")):
        if qqq is not None and qqq > 0.5:
            score += 1.5
            reasons.append("US_TECH_PREVIOUS_SESSION_POSITIVE")
        if soxx is not None and soxx > 1.0:
            score += 1.5
            reasons.append("SEMICONDUCTOR_PREVIOUS_SESSION_POSITIVE")
        if nq_change is not None and nq_change > 0.35:
            score += 2.0
            reasons.append("NASDAQ_FUTURES_OVERNIGHT_POSITIVE")

    if any(k in industry_lower for k in ("oil", "gas", "petroleum", "exploration")):
        if crude_change is not None and crude_change > 1.0:
            score += 3.0
            reasons.append("CRUDE_OVERNIGHT_POSITIVE_FOR_ENERGY")

    if any(k in industry_lower for k in ("airline", "aviation", "paint", "tyre")):
        if crude_change is not None and crude_change > 2.0:
            score -= 2.0
            reasons.append("CRUDE_OVERNIGHT_COST_HEADWIND")

    return float(min(max(score, 0.0), 10.0)), reasons


# ---------------------------------------------------------------------------
# TECHNICALS, PRIOR-DAY LEVELS AND VOLUME PROFILE
# ---------------------------------------------------------------------------

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
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(14).mean().iloc[-1]
    close = frame["close"].iloc[-1]
    if pd.isna(atr) or close <= 0:
        return float("nan")
    return float(atr / close * 100)


def count_missing_trading_sessions(
    last_session: date,
    expected_session: date,
    holidays: dict[date, str],
) -> int:
    if last_session >= expected_session:
        return 0
    missing = 0
    cursor = last_session + timedelta(days=1)
    while cursor <= expected_session and missing < 20:
        if is_trading_day(cursor, holidays):
            missing += 1
        cursor += timedelta(days=1)
    return missing


def preliminary_candidate(
    symbol: str,
    company: str,
    industry: str,
    frame: pd.DataFrame,
    macro: dict[str, Any],
    expected_previous_session: date,
    holidays: dict[date, str],
) -> dict[str, Any] | None:
    frame = completed_daily_frame(frame)
    if len(frame) < MIN_HISTORY:
        return None

    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    last_close = float(close.iloc[-1])
    prior_open = float(frame["open"].iloc[-1])
    prior_high = float(frame["high"].iloc[-1])
    prior_low = float(frame["low"].iloc[-1])
    last_session = frame.index[-1].date()

    if last_close < MIN_PRICE:
        return None

    average_volume_20 = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else float("nan")
    previous_day_rvol = (
        float(volume.iloc[-1] / average_volume_20)
        if average_volume_20 > 0
        else float("nan")
    )
    average_turnover_cr = float(
        (close.iloc[-20:] * volume.iloc[-20:]).mean() / 1e7
    )
    if (
        not math.isfinite(average_turnover_cr)
        or average_turnover_cr < MIN_AVG_TURNOVER_CR
    ):
        return None

    sma50 = float(close.iloc[-50:].mean())
    sma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else float("nan")
    current_rsi = rsi14(close)
    current_atr_pct = atr14_pct(frame)
    momentum_5 = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
    momentum_20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100)

    high_52 = float(frame["high"].iloc[-252:].max()) if len(frame) >= 252 else float("nan")
    near_52 = bool(last_close >= high_52 * 0.95) if math.isfinite(high_52) else None
    above_50 = bool(last_close > sma50)
    above_200 = bool(last_close > sma200) if math.isfinite(sma200) else None

    prior_range = max(prior_high - prior_low, 0.0)
    prior_range_pct = (
        prior_range / prior_open * 100
        if prior_open > 0
        else float("nan")
    )
    close_location = (
        (last_close - prior_low) / prior_range
        if prior_range > 0
        else 0.5
    )
    distance_to_high_pct = (
        (prior_high - last_close) / last_close * 100
        if last_close > 0
        else float("nan")
    )
    distance_to_low_pct = (
        (last_close - prior_low) / last_close * 100
        if last_close > 0
        else float("nan")
    )

    pivot = (prior_high + prior_low + last_close) / 3
    resistance_1 = 2 * pivot - prior_low
    support_1 = 2 * pivot - prior_high

    reasons: list[str] = []
    missing_fields: list[str] = []

    # Technical component: 0-40.
    technical_raw = 0.0
    if math.isfinite(previous_day_rvol):
        if previous_day_rvol >= 2.5:
            technical_raw += 12
            reasons.append("PRIOR_DAY_RVOL_VERY_HIGH")
        elif previous_day_rvol >= 1.7:
            technical_raw += 9
            reasons.append("PRIOR_DAY_RVOL_HIGH")
        elif previous_day_rvol >= 1.2:
            technical_raw += 5
            reasons.append("PRIOR_DAY_RVOL_ELEVATED")
    else:
        missing_fields.append("previous_day_rvol")

    if above_50:
        technical_raw += 7
        reasons.append("ABOVE_SMA50")
    if above_200 is True:
        technical_raw += 5
        reasons.append("ABOVE_SMA200")
    elif above_200 is None:
        missing_fields.append("above_sma200")

    if math.isfinite(current_rsi):
        if 50 <= current_rsi <= 68:
            technical_raw += 6
            reasons.append("RSI_CONSTRUCTIVE")
        elif current_rsi > 75:
            technical_raw -= 3
            reasons.append("RSI_OVEREXTENDED")
    else:
        missing_fields.append("rsi14")

    if 0.5 <= momentum_5 <= 8:
        technical_raw += 6
        reasons.append("MOMENTUM_5D_POSITIVE")
    elif momentum_5 < -5:
        technical_raw -= 3
        reasons.append("MOMENTUM_5D_WEAK")

    if 1 <= momentum_20 <= 18:
        technical_raw += 5
        reasons.append("MOMENTUM_20D_POSITIVE")
    elif momentum_20 < -10:
        technical_raw -= 3
        reasons.append("MOMENTUM_20D_WEAK")

    if near_52 is True:
        technical_raw += 4
        reasons.append("NEAR_52W_HIGH")

    if math.isfinite(current_atr_pct):
        if 1.5 <= current_atr_pct <= 5.5:
            technical_raw += 5
            reasons.append("ATR_TRADEABLE")
        elif current_atr_pct > 8:
            technical_raw -= 3
            reasons.append("ATR_EXTREME")
    else:
        missing_fields.append("atr14_pct")

    technical_score = min(max(technical_raw / 45 * 40, 0.0), 40.0)

    # Liquidity component: 0-25.
    if average_turnover_cr >= 250:
        liquidity_score = 25.0
        reasons.append("LIQUIDITY_VERY_HIGH")
    elif average_turnover_cr >= 100:
        liquidity_score = 22.0
        reasons.append("LIQUIDITY_HIGH")
    elif average_turnover_cr >= 50:
        liquidity_score = 18.0
        reasons.append("LIQUIDITY_GOOD")
    elif average_turnover_cr >= 20:
        liquidity_score = 13.0
        reasons.append("LIQUIDITY_ACCEPTABLE")
    else:
        liquidity_score = 8.0

    # Prior-day level structure: 0-15.
    prior_level_score = 0.0
    if close_location >= 0.80:
        prior_level_score += 6
        reasons.append("CLOSE_NEAR_PRIOR_DAY_HIGH")
    elif close_location >= 0.60:
        prior_level_score += 3
        reasons.append("CLOSE_IN_UPPER_PRIOR_RANGE")
    elif close_location <= 0.20:
        prior_level_score -= 2
        reasons.append("CLOSE_NEAR_PRIOR_DAY_LOW")

    if distance_to_high_pct <= 1.0:
        prior_level_score += 4
        reasons.append("WITHIN_1PCT_OF_PRIOR_HIGH")

    if 1.5 <= prior_range_pct <= 6.0:
        prior_level_score += 3
        reasons.append("PRIOR_RANGE_TRADEABLE")
    elif prior_range_pct > 10:
        prior_level_score -= 2
        reasons.append("PRIOR_RANGE_EXTREME")

    if previous_day_rvol >= 1.5 and close_location >= 0.65:
        prior_level_score += 2
        reasons.append("VOLUME_AND_CLOSE_LOCATION_ALIGNED")

    prior_level_score = min(max(prior_level_score, 0.0), 15.0)

    macro_score, macro_reasons = macro_alignment_score(industry, macro)
    reasons.extend(macro_reasons)

    base_score = (
        technical_score
        + liquidity_score
        + prior_level_score
        + macro_score
    )  # 0-90 before profile.

    missing_sessions = count_missing_trading_sessions(
        last_session,
        expected_previous_session,
        holidays,
    )
    trust = FREE_FEED_TRUST_CAP
    trust -= 4 * len(missing_fields)
    if missing_sessions:
        trust -= min(30, 12 * missing_sessions)
        reasons.append("DAILY_DATA_BEHIND_EXPECTED_SESSION")
    trust = int(min(max(trust, 0), FREE_FEED_TRUST_CAP))

    return {
        "symbol": symbol,
        "company": company,
        "industry": industry,
        "last_completed_session": last_session.isoformat(),
        "previous_open": clean_float(prior_open),
        "previous_high": clean_float(prior_high),
        "previous_low": clean_float(prior_low),
        "previous_close": clean_float(last_close),
        "previous_range_pct": clean_float(prior_range_pct),
        "previous_close_location": clean_float(close_location, 3),
        "distance_to_previous_high_pct": clean_float(distance_to_high_pct),
        "distance_to_previous_low_pct": clean_float(distance_to_low_pct),
        "classical_pivot": clean_float(pivot),
        "pivot_resistance_1": clean_float(resistance_1),
        "pivot_support_1": clean_float(support_1),
        "avg_turnover_20d_cr": clean_float(average_turnover_cr),
        "previous_day_rvol": clean_float(previous_day_rvol),
        "atr14_pct": clean_float(current_atr_pct),
        "rsi14": clean_float(current_rsi, 1),
        "momentum_5d_pct": clean_float(momentum_5),
        "momentum_20d_pct": clean_float(momentum_20),
        "above_sma50": above_50,
        "above_sma200": above_200,
        "near_52w_high": near_52,
        "technical_score": clean_float(technical_score, 1),
        "liquidity_score": clean_float(liquidity_score, 1),
        "prior_level_score": clean_float(prior_level_score, 1),
        "macro_alignment_score": clean_float(macro_score, 1),
        "volume_profile_score": None,
        "base_score_before_profile": clean_float(base_score, 1),
        "preliminary_opportunity_score": clean_float(base_score / 90 * 100, 1),
        "trust_rate": trust,
        "reason_codes": reasons[:18],
        "missing_fields": missing_fields,
    }


def fetch_intraday_download(symbols: list[str]) -> pd.DataFrame | None:
    if not symbols:
        return None
    tickers = [f"{symbol}.NS" for symbol in symbols]
    try:
        data = yf.download(
            tickers=tickers,
            period=PROFILE_PERIOD,
            interval=PROFILE_INTERVAL,
            auto_adjust=False,
            group_by="ticker",
            threads=True,
            progress=False,
            actions=False,
        )
        return None if data.empty else data
    except Exception:
        return None


def previous_session_intraday(
    frame: pd.DataFrame,
    expected_previous_session: date,
) -> pd.DataFrame | None:
    try:
        frame = ensure_ist_index(frame)
        session = frame[
            (frame.index.date == expected_previous_session)
            & (frame.index.time >= dt_time(hour=9, minute=15))
            & (frame.index.time <= dt_time(hour=15, minute=30))
        ].copy()
        if len(session) < 8:
            return None
        return session
    except Exception:
        return None


def calculate_volume_profile(
    session: pd.DataFrame,
    prior_close: float,
) -> dict[str, Any] | None:
    session = session.copy()
    session["volume"] = pd.to_numeric(
        session["volume"],
        errors="coerce",
    ).fillna(0)

    total_volume = float(session["volume"].sum())
    session_low = float(session["low"].min())
    session_high = float(session["high"].max())
    if total_volume <= 0 or session_high <= session_low:
        return None

    typical_price = (
        session["high"] + session["low"] + session["close"]
    ) / 3
    edges = np.linspace(session_low, session_high, PROFILE_BINS + 1)
    indices = np.digitize(typical_price.to_numpy(), edges, right=False) - 1
    indices = np.clip(indices, 0, PROFILE_BINS - 1)
    volume_by_bin = np.bincount(
        indices,
        weights=session["volume"].to_numpy(),
        minlength=PROFILE_BINS,
    )

    poc_index = int(np.argmax(volume_by_bin))
    included = {poc_index}
    cumulative = float(volume_by_bin[poc_index])
    target = total_volume * VALUE_AREA_FRACTION
    left = poc_index - 1
    right = poc_index + 1

    while cumulative < target and (left >= 0 or right < PROFILE_BINS):
        left_volume = volume_by_bin[left] if left >= 0 else -1
        right_volume = volume_by_bin[right] if right < PROFILE_BINS else -1

        if right_volume > left_volume:
            included.add(right)
            cumulative += float(right_volume)
            right += 1
        else:
            included.add(left)
            cumulative += float(left_volume)
            left -= 1

    min_bin = min(included)
    max_bin = max(included)
    poc = (edges[poc_index] + edges[poc_index + 1]) / 2
    value_area_low = edges[min_bin]
    value_area_high = edges[max_bin + 1]
    vwap = float(
        (typical_price * session["volume"]).sum() / total_volume
    )
    session_midpoint = (session_low + session_high) / 2

    if prior_close > value_area_high:
        state = "ABOVE_VALUE"
        profile_score = 8.0
    elif prior_close < value_area_low:
        state = "BELOW_VALUE"
        profile_score = -3.0
    elif prior_close >= poc:
        state = "IN_VALUE_ABOVE_POC"
        profile_score = 5.0
    else:
        state = "IN_VALUE_BELOW_POC"
        profile_score = 2.0

    if poc > session_midpoint:
        profile_score += 1.5
    if prior_close > vwap:
        profile_score += 1.0
    profile_score = min(max(profile_score, -5.0), 10.0)

    return {
        "profile_interval": PROFILE_INTERVAL,
        "profile_session": session.index[0].date().isoformat(),
        "point_of_control": clean_float(poc),
        "value_area_high": clean_float(value_area_high),
        "value_area_low": clean_float(value_area_low),
        "previous_session_vwap": clean_float(vwap),
        "value_area_width_pct": clean_float(
            (value_area_high - value_area_low) / prior_close * 100
            if prior_close > 0 else None
        ),
        "profile_state_at_close": state,
        "volume_profile_score": clean_float(profile_score, 1),
        "bars_used": int(len(session)),
        "profile_method": (
            "15-minute typical-price volume bins with contiguous 70% value area"
        ),
    }


def enrich_with_profiles(
    candidates: list[dict[str, Any]],
    expected_previous_session: date,
) -> None:
    pool = candidates[:PROFILE_POOL_SIZE]
    download = fetch_intraday_download([c["symbol"] for c in pool])

    for candidate in pool:
        profile = None
        if download is not None:
            ticker = f"{candidate['symbol']}.NS"
            frame = extract_ticker_frame(download, ticker)
            if frame is not None:
                session = previous_session_intraday(
                    frame,
                    expected_previous_session,
                )
                if session is not None:
                    profile = calculate_volume_profile(
                        session,
                        float(candidate["previous_close"]),
                    )

        if profile is None:
            candidate["volume_profile"] = None
            candidate["volume_profile_score"] = None
            candidate["trust_rate"] = max(
                0,
                candidate["trust_rate"] - 8,
            )
            candidate["missing_fields"].append(
                "previous_session_volume_profile"
            )
            candidate["reason_codes"].append(
                "VOLUME_PROFILE_UNAVAILABLE"
            )
            profile_points = 0.0
        else:
            candidate["volume_profile"] = profile
            candidate["volume_profile_score"] = profile[
                "volume_profile_score"
            ]
            candidate["reason_codes"].append(
                f"PROFILE_{profile['profile_state_at_close']}"
            )
            profile_points = float(profile["volume_profile_score"])

        enhanced = (
            float(candidate["technical_score"])
            + float(candidate["liquidity_score"])
            + float(candidate["prior_level_score"])
            + float(candidate["macro_alignment_score"])
            + profile_points
        )
        candidate["enhanced_opportunity_score"] = clean_float(
            min(max(enhanced, 0.0), 100.0),
            1,
        )

    # Candidates outside the profile pool retain their base score and are
    # unlikely to survive the final top-10 rerank.
    for candidate in candidates[PROFILE_POOL_SIZE:]:
        candidate["volume_profile"] = None
        candidate["enhanced_opportunity_score"] = clean_float(
            float(candidate["base_score_before_profile"]),
            1,
        )


def assign_status(candidate: dict[str, Any]) -> str:
    score = float(candidate["enhanced_opportunity_score"])
    trust = int(candidate["trust_rate"])
    if score >= 74 and trust >= 60:
        return "PRIMARY_WATCH"
    if score >= 62 and trust >= 52:
        return "SECONDARY_WATCH"
    return "CONDITIONAL"


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def build_markdown(feed: dict[str, Any]) -> str:
    if feed.get("market_status") == "ERROR":
        return (
            "# NSE Premarket Research Watchlist\n\n"
            f"- Generated: **{feed.get('generated_at', 'unknown')}**\n"
            "- Status: **ERROR**\n"
            f"- Message: **{feed.get('error', 'Unknown error')}**\n\n"
            "No watchlist was published. Review the GitHub Actions log.\n"
        )

    if feed.get("market_status") == "CLOSED":
        return (
            "# NSE Premarket Research Watchlist\n\n"
            f"- Generated: **{feed['generated_at']}**\n"
            f"- Market status: **CLOSED**\n"
            f"- Reason: **{feed['closure_reason']}**\n"
            f"- Next expected trading day: **{feed['next_trading_day']}**\n\n"
            "No watchlist was generated because the NSE equity market is closed.\n"
        )

    lines = [
        "# NSE Premarket Research Watchlist",
        "",
        f"- Generated: **{feed['generated_at']}**",
        f"- Market status: **{feed['market_status']}**",
        f"- Data status: **{feed['data_status']}**",
        f"- Actionable: **No — research watchlist only**",
        f"- Universe eligible: **{feed['universe_eligible']}**",
        f"- Model: **{feed['model_version']}**",
        "",
        "> The free-data feed does not include broker-grade IEP/IEQ, spread, "
        "depth, current-day VWAP, opening range or intraday RVOL. The prior-day "
        "volume profile is estimated from free 15-minute bars.",
        "",
        "## Candidates",
        "",
        "| Rank | Symbol | Status | Score | Trust | PDH | PDL | Prev RVOL | Profile | POC | VAH | VAL |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|",
    ]

    for candidate in feed["candidates"]:
        profile = candidate.get("volume_profile") or {}
        lines.append(
            f"| {candidate['rank']} | {candidate['symbol']} | "
            f"{candidate['status']} | "
            f"{candidate['enhanced_opportunity_score']:.1f} | "
            f"{candidate['trust_rate']} | "
            f"{candidate['previous_high']:.2f} | "
            f"{candidate['previous_low']:.2f} | "
            f"{candidate['previous_day_rvol'] if candidate['previous_day_rvol'] is not None else 'NA'} | "
            f"{profile.get('profile_state_at_close', 'NA')} | "
            f"{profile.get('point_of_control', 'NA')} | "
            f"{profile.get('value_area_high', 'NA')} | "
            f"{profile.get('value_area_low', 'NA')} |"
        )

    lines.extend(
        [
            "",
            "## Overnight context",
            "",
            "NSE cash shares do not trade overnight. The feed therefore uses "
            "global futures and FX as contextual overnight high/low proxies.",
            "",
            "```json",
            json.dumps(
                feed.get("macro", {}).get("overnight_proxies", {}),
                indent=2,
            ),
            "```",
            "",
            "## Required GPT verification",
            "",
            "Verify official NSE announcements, current pre-open information "
            "when available, sector context, adverse governance/regulatory "
            "events, and the scan timestamp. For IPOs, verify SEBI DRHP/RHP "
            "and the applicable special pre-open session.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_html(feed: dict[str, Any]) -> str:
    if feed.get("market_status") == "ERROR":
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Watchlist Error</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:900px;margin:50px auto;padding:0 20px;line-height:1.5}}
.notice{{border:1px solid #b91c1c;background:#fff1f2;padding:20px;border-radius:10px}}
</style></head><body>
<h1>NSE Premarket Research Watchlist</h1>
<div class="notice">
<h2>Generation error</h2>
<p>{html.escape(str(feed.get('error', 'Unknown error')))}</p>
<p><strong>Generated:</strong> {html.escape(str(feed.get('generated_at', 'unknown')))}</p>
</div></body></html>"""

    if feed.get("market_status") == "CLOSED":
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Market Closed</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:900px;margin:50px auto;padding:0 20px;line-height:1.5}}
.notice{{border:1px solid #c9a227;background:#fffbea;padding:20px;border-radius:10px}}
</style></head><body>
<h1>NSE Premarket Research Watchlist</h1>
<div class="notice">
<h2>Market closed</h2>
<p><strong>Reason:</strong> {html.escape(feed['closure_reason'])}</p>
<p><strong>Generated:</strong> {html.escape(feed['generated_at'])}</p>
<p><strong>Next expected trading day:</strong> {html.escape(feed['next_trading_day'])}</p>
</div></body></html>"""

    rows = []
    for candidate in feed["candidates"]:
        profile = candidate.get("volume_profile") or {}
        rows.append(
            "<tr>"
            f"<td>{candidate['rank']}</td>"
            f"<td><strong>{html.escape(candidate['symbol'])}</strong></td>"
            f"<td>{html.escape(candidate['company'])}</td>"
            f"<td>{html.escape(candidate['industry'])}</td>"
            f"<td>{html.escape(candidate['status'])}</td>"
            f"<td>{candidate['enhanced_opportunity_score']:.1f}</td>"
            f"<td>{candidate['trust_rate']}</td>"
            f"<td>{candidate['previous_high']:.2f}</td>"
            f"<td>{candidate['previous_low']:.2f}</td>"
            f"<td>{candidate['previous_day_rvol'] if candidate['previous_day_rvol'] is not None else 'NA'}</td>"
            f"<td>{html.escape(str(profile.get('profile_state_at_close', 'NA')))}</td>"
            f"<td>{profile.get('point_of_control', 'NA')}</td>"
            f"<td>{profile.get('value_area_high', 'NA')}</td>"
            f"<td>{profile.get('value_area_low', 'NA')}</td>"
            "</tr>"
        )

    overnight = feed.get("macro", {}).get("overnight_proxies", {})
    overnight_rows = []
    for name, payload in overnight.items():
        payload = payload or {}
        overnight_rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{payload.get('change_pct', 'NA')}</td>"
            f"<td>{payload.get('high', 'NA')}</td>"
            f"<td>{payload.get('low', 'NA')}</td>"
            f"<td>{payload.get('range_pct', 'NA')}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Premarket Research Watchlist</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:1450px;margin:35px auto;padding:0 18px;line-height:1.45}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:28px}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#f5f5f5}}
.notice{{padding:14px;border:1px solid #c9a227;background:#fffbea}}
.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:20px 0}}
.card{{border:1px solid #ddd;padding:12px;border-radius:8px}}
.small{{font-size:13px;color:#444}}
</style>
</head>
<body>
<h1>NSE Premarket Research Watchlist</h1>
<div class="notice"><strong>Research-only feed.</strong> Prior-session levels
and a 15-minute volume-profile estimate are included, but this is not an
executable real-time signal.</div>
<div class="meta">
<div class="card"><strong>Generated</strong><br>{html.escape(feed['generated_at'])}</div>
<div class="card"><strong>Market</strong><br>{html.escape(feed['market_status'])}</div>
<div class="card"><strong>Calendar</strong><br>{html.escape(feed['market_calendar']['calendar_status'])}</div>
<div class="card"><strong>Eligible universe</strong><br>{feed['universe_eligible']}</div>
<div class="card"><strong>Model</strong><br>{html.escape(feed['model_version'])}</div>
</div>

<h2>Top candidates</h2>
<table>
<thead><tr><th>Rank</th><th>Symbol</th><th>Company</th><th>Industry</th>
<th>Status</th><th>Score</th><th>Trust</th><th>PDH</th><th>PDL</th>
<th>Prev RVOL</th><th>Profile state</th><th>POC</th><th>VAH</th><th>VAL</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>

<h2>Global overnight proxies</h2>
<p class="small">NSE cash shares do not trade overnight. These are delayed/free
global futures and FX context measured from the previous NSE close to scan time.</p>
<table>
<thead><tr><th>Proxy</th><th>Change %</th><th>High</th><th>Low</th><th>Range %</th></tr></thead>
<tbody>{''.join(overnight_rows)}</tbody>
</table>

<h2>How the Custom GPT should use this</h2>
<p>Verify official NSE announcements, current pre-open information when
available, sector conditions and negative risk events. For IPOs, verify the
DRHP/RHP on SEBI. Keep every candidate as a watchlist item until live
price/volume confirmation exists.</p>
<p>Structured data: <a href="feed.json"><code>feed.json</code></a></p>
</body>
</html>"""


def write_feed(feed: dict[str, Any]) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "feed.json").write_text(
        json.dumps(feed, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (DOCS / "latest.md").write_text(
        build_markdown(feed),
        encoding="utf-8",
    )
    (DOCS / "index.html").write_text(
        build_html(feed),
        encoding="utf-8",
    )


def main() -> int:
    generated = now_ist()
    holidays, calendar_metadata = load_market_calendar(generated.year)
    closure_reason = market_closure_reason(generated.date(), holidays)

    if closure_reason:
        feed = {
            "generated_at": generated.isoformat(),
            "market": "NSE",
            "market_status": "CLOSED",
            "closure_reason": closure_reason,
            "next_trading_day": next_trading_day(
                generated.date(),
                holidays,
            ).isoformat(),
            "data_status": "MARKET_CLOSED",
            "actionable": False,
            "model_version": MODEL_VERSION,
            "market_calendar": calendar_metadata,
            "candidates": [],
        }
        write_feed(feed)
        print(f"NSE market closed: {closure_reason}")
        return 0

    expected_previous_session = previous_trading_day(
        generated.date(),
        holidays,
    )

    try:
        universe = fetch_universe()
        daily_download = fetch_daily_prices(
            universe["symbol"].tolist()
        )
        macro = fetch_macro(expected_previous_session)

        candidates: list[dict[str, Any]] = []
        frames_successfully_read = 0

        for row in universe.itertuples(index=False):
            ticker = f"{row.symbol}.NS"
            frame = extract_ticker_frame(daily_download, ticker)
            if frame is None:
                continue
            frames_successfully_read += 1
            candidate = preliminary_candidate(
                symbol=row.symbol,
                company=row.company,
                industry=row.industry,
                frame=frame,
                macro=macro,
                expected_previous_session=expected_previous_session,
                holidays=holidays,
            )
            if candidate is not None:
                candidates.append(candidate)

        candidates.sort(
            key=lambda candidate: (
                candidate["base_score_before_profile"],
                candidate["trust_rate"],
                candidate["avg_turnover_20d_cr"],
            ),
            reverse=True,
        )

        eligible_count = len(candidates)

        enrich_with_profiles(
            candidates,
            expected_previous_session,
        )

        candidates.sort(
            key=lambda candidate: (
                candidate["enhanced_opportunity_score"],
                candidate["trust_rate"],
                candidate["avg_turnover_20d_cr"],
            ),
            reverse=True,
        )
        candidates = candidates[:TOP_N]

        for rank, candidate in enumerate(candidates, 1):
            candidate["rank"] = rank
            candidate["status"] = assign_status(candidate)

        if not candidates:
            raise RuntimeError(
                "No candidates passed data and liquidity checks"
            )

        feed = {
            "generated_at": generated.isoformat(),
            "valid_until": generated.replace(
                hour=9,
                minute=15,
                second=0,
                microsecond=0,
            ).isoformat(),
            "market": "NSE",
            "market_status": "OPEN_TODAY_PREMARKET_RESEARCH",
            "expected_previous_session": expected_previous_session.isoformat(),
            "data_status": "FREE_DELAYED_OR_END_OF_DAY",
            "actionable": False,
            "model_version": MODEL_VERSION,
            "market_calendar": calendar_metadata,
            "universe_source": NIFTY500_CSV,
            "universe_requested": len(universe),
            "universe_frames_read": frames_successfully_read,
            "universe_eligible": eligible_count,
            "profile_pool_size": PROFILE_POOL_SIZE,
            "macro": macro,
            "candidates": candidates,
            "limitations": [
                "No broker-grade real-time data",
                "No guaranteed pre-open IEP or IEQ",
                "No live spread or market depth",
                "No current-day VWAP, opening range or intraday RVOL",
                "Opportunity score is not a calibrated probability",
                "yfinance is an unofficial research data source",
                "Prior-session volume profile is an estimate from 15-minute bars",
                "NSE cash equities have no stock-specific overnight session",
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

        write_feed(feed)
        print(
            f"Generated {len(candidates)} candidates at "
            f"{generated.isoformat()}"
        )
        return 0

    except Exception as exc:
        error_feed = {
            "generated_at": generated.isoformat(),
            "market": "NSE",
            "market_status": "ERROR",
            "data_status": "ERROR",
            "actionable": False,
            "model_version": MODEL_VERSION,
            "market_calendar": calendar_metadata,
            "error": str(exc),
            "candidates": [],
        }
        write_feed(error_feed)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
