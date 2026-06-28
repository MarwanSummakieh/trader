import logging
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
            # Single ticker
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
    Optional eToro API client.
    Set ETORO_API_KEY env var to your key from eToro Settings → API.
    Used only for portfolio context — all market data comes from yfinance.
    """
    BASE = "https://api.etoro.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def get_portfolio(self) -> Optional[dict]:
        if not self.api_key:
            return None
        try:
            r = requests.get(
                f"{self.BASE}/sapi/v1/user/portfolio",
                headers=self.headers, timeout=5,
            )
            return r.json() if r.ok else None
        except Exception:
            return None

    def get_account_balance(self) -> Optional[float]:
        data = self.get_portfolio()
        if data:
            return data.get("creditByInstrumentType", {}).get("RealPortfolio", None)
        return None
