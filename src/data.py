import logging
import threading
import time
import uuid
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests
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
    """90-day daily OHLCV for trend context."""
    try:
        df = yf.download(
            ticker, period="90d", interval="1d",
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


class EToroClient:
    """
    eToro public API client (read-only portfolio/balance context).

    Requires two keys from eToro Settings → Trading → API Key Management:
      ETORO_API_KEY  = the "API Key"  (x-api-key header)
      ETORO_USER_KEY = the "User Key" (x-user-key header)

    All market data for analysis still comes from yfinance.
    eToro's trading/execution endpoints require partner-level access and are
    not used here — this is a paper-trading bot.
    """
    BASE = "https://public-api.etoro.com/api/v1"

    def __init__(self, public_key: str, private_key: str):
        self.public_key = public_key
        self.private_key = private_key

    def _headers(self) -> dict:
        return {
            "x-api-key": self.public_key,
            "x-user-key": self.private_key,
            "x-request-id": str(uuid.uuid4()),
            "Accept": "application/json",
        }

    def _get(self, path: str) -> Optional[dict]:
        try:
            r = requests.get(
                f"{self.BASE}{path}",
                headers=self._headers(),
                timeout=5,
            )
            if r.ok:
                return r.json()
            logger.debug("eToro API %s → %d %s", path, r.status_code, r.text[:120])
            return None
        except Exception as e:
            logger.debug("eToro API request failed: %s", e)
            return None

    def test_connection(self) -> tuple[bool, str]:
        """Returns (success, message). Call this at startup."""
        if not self.public_key or not self.private_key:
            return False, "Missing ETORO_PUBLIC_KEY or ETORO_PRIVATE_KEY in .env"
        data = self._get("/trading/info/portfolio")
        if data is not None:
            return True, "eToro API connected"
        # Try the authenticated user profile endpoint as fallback
        data = self._get("/user/profile")
        if data is not None:
            return True, "eToro API connected (profile endpoint)"
        return False, "eToro API auth failed — check both keys in .env"

    def get_portfolio(self) -> Optional[dict]:
        return self._get("/trading/info/portfolio")

    def get_demo_portfolio(self) -> Optional[dict]:
        return self._get("/trading/info/demo/portfolio")

    def get_balance(self) -> Optional[dict]:
        return self._get("/account/balance")

    def get_account_balance(self) -> Optional[float]:
        """Returns total real account balance in USD, or None on failure."""
        data = self.get_balance()
        if not data:
            return None
        # eToro balance response shape varies — try common field names
        for field in ("totalBalance", "balance", "amount", "totalEquity", "equity"):
            if field in data and data[field] is not None:
                return float(data[field])
        # Sometimes nested under a key
        if isinstance(data, list) and data:
            entry = data[0]
            for field in ("totalBalance", "balance", "amount"):
                if field in entry:
                    return float(entry[field])
        return None

    def get_open_positions(self) -> list[dict]:
        """Returns list of open positions from the real portfolio."""
        portfolio = self.get_portfolio()
        if not portfolio:
            return []
        # Common shapes: {"positions": [...]} or {"openTrades": [...]} or list directly
        if isinstance(portfolio, list):
            return portfolio
        return (
            portfolio.get("positions")
            or portfolio.get("openTrades")
            or portfolio.get("trades")
            or []
        )
