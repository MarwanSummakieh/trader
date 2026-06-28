import logging
import uuid
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

import config

logger = logging.getLogger(__name__)


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
    """Fetch latest close prices for a list of tickers."""
    if not tickers:
        return {}
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
            val = close.dropna().iloc[-1] if not close.dropna().empty else None
            if val is not None:
                prices[tickers[0]] = float(val)
        else:
            latest = close.iloc[-1]
            for t in tickers:
                if t in latest.index and pd.notna(latest[t]):
                    prices[t] = float(latest[t])
    except Exception as e:
        logger.debug("Bulk price fetch failed: %s", e)
    return prices


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
