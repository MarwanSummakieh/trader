import logging
import threading
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger(__name__)

# Short-TTL price cache so monitor/display/dashboard don't each hammer
# yfinance with duplicate requests (rate limiting silently breaks exits).
_PRICE_TTL_SECS = 20.0
_price_cache: dict[str, tuple[float, float]] = {}  # ticker → (price, fetched_at)
_price_lock = threading.Lock()


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns that yfinance sometimes returns."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(level=1, axis=1)
    return df


def get_intraday(ticker: str, interval: str = "5m") -> Optional[pd.DataFrame]:
    """5-day intraday OHLCV. Returns None if insufficient data."""
    try:
        df = yf.download(
            ticker, period="5d", interval=interval,
            progress=False, auto_adjust=True, multi_level_index=False,
        )
        df = _normalize(df).dropna(subset=["Close", "Volume"])
        return df if len(df) >= 30 else None
    except Exception as e:
        logger.debug("Intraday fetch failed %s: %s", ticker, e)
        return None


def get_daily(ticker: str) -> Optional[pd.DataFrame]:
    """1-year daily OHLCV: trend context (EMA50 / ADX) plus the swing
    rule's 200-day SMA and RSI(2), which need >= 200 completed sessions."""
    try:
        df = yf.download(
            ticker, period="1y", interval="1d",
            progress=False, auto_adjust=True, multi_level_index=False,
        )
        df = _normalize(df).dropna(subset=["Close", "Volume"])
        return df if len(df) >= 50 else None
    except Exception as e:
        logger.debug("Daily fetch failed %s: %s", ticker, e)
        return None


def get_current_prices(tickers: list[str]) -> dict[str, float]:
    """
    Latest close prices for a list of tickers.
    Uses a short TTL cache, then a bulk download, then per-ticker fallbacks
    for anything the bulk call missed. Missing tickers are logged loudly —
    callers treat an absent price as "cannot check exits".
    """
    if not tickers:
        return {}
    now = time.time()
    prices: dict[str, float] = {}

    with _price_lock:
        stale = []
        for t in tickers:
            hit = _price_cache.get(t)
            if hit and now - hit[1] <= _PRICE_TTL_SECS:
                prices[t] = hit[0]
            else:
                stale.append(t)

    if stale:
        fetched = _fetch_prices_bulk(stale)
        # Per-ticker fallback for anything the bulk call missed
        for t in stale:
            if t not in fetched:
                single = _fetch_price_single(t)
                if single is not None:
                    fetched[t] = single
        with _price_lock:
            for t, p in fetched.items():
                _price_cache[t] = (p, now)
        prices.update(fetched)

    missing = [t for t in tickers if t not in prices]
    if missing:
        logger.warning("No price available for: %s", ", ".join(missing))
    return prices


def _fetch_prices_bulk(tickers: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    try:
        raw = yf.download(
            tickers, period="1d", interval="1m",
            progress=False, auto_adjust=True, multi_level_index=True,
        )
        if raw.empty:
            return prices
        close = raw["Close"]
        if isinstance(close, pd.Series):
            clean = close.dropna()
            if not clean.empty:
                prices[tickers[0]] = float(clean.iloc[-1])
        else:
            for t in tickers:
                if t in close.columns:
                    clean = close[t].dropna()
                    if not clean.empty:
                        prices[t] = float(clean.iloc[-1])
    except Exception as e:
        logger.warning("Bulk price fetch failed: %s", e)
    return prices


def _fetch_price_single(ticker: str) -> Optional[float]:
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m")
        clean = hist["Close"].dropna()
        if not clean.empty:
            return float(clean.iloc[-1])
    except Exception as e:
        logger.warning("Single price fetch failed %s: %s", ticker, e)
    return None


# ── Earnings calendar ─────────────────────────────────────────────────────────
# Entries on earnings reaction days tested at 18% win rate / -0.41R (the gap,
# not the trend, sets the price and the momentum signal is stale). The window
# covers the announcement day and the following session.

_earnings_cache: dict[str, tuple[set, float]] = {}  # ticker → (window days, fetched_at)
_earnings_lock = threading.Lock()
_EARNINGS_TTL_SECS = 12 * 3600.0


def get_earnings_window_days(ticker: str) -> set:
    """ET dates on/around earnings announcements for a ticker (cached 12h).
    Empty set for ETFs and on fetch failure — failing open is correct here
    since this is a filter, not a data dependency."""
    now = time.time()
    with _earnings_lock:
        hit = _earnings_cache.get(ticker)
        if hit and now - hit[1] <= _EARNINGS_TTL_SECS:
            return hit[0]
    days: set = set()
    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if df is not None:
            for d in df.index:
                dd = d.date()
                days.add(dd)
                days.add(dd + timedelta(days=1))
                # After-close announcement on Friday reacts on Monday
                if (dd + timedelta(days=1)).weekday() == 5:
                    days.add(dd + timedelta(days=3))
    except Exception as e:
        logger.debug("Earnings dates fetch failed %s: %s", ticker, e)
    with _earnings_lock:
        _earnings_cache[ticker] = (days, now)
    return days


def in_earnings_window(ticker: str, day: date) -> bool:
    return day in get_earnings_window_days(ticker)
