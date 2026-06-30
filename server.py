#!/usr/bin/env python3
"""
FastAPI dashboard server.
Reads from the same ledger.db the bot writes to — run both simultaneously.

  python main.py    # bot process (terminal window 1)
  python server.py  # web server  (terminal window 2)
  open http://localhost:5000
"""

import os
import sys
import time as _time
from datetime import datetime
from typing import Optional

import pytz
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(__file__))

import config
from src.data import get_current_prices, EToroClient
from src.ledger import Ledger
from src.portfolio import Portfolio

app = FastAPI(title="Day Trader Bot", docs_url=None, redoc_url=None)
ET = pytz.timezone("America/New_York")

_ledger = Ledger("ledger.db")
_portfolio = Portfolio(_ledger)
_etoro = (
    EToroClient(config.ETORO_PUBLIC_KEY, config.ETORO_PRIVATE_KEY)
    if config.ETORO_PUBLIC_KEY else None
)

# Cache live prices for 30 s to avoid hammering yfinance on every page refresh
_price_cache: dict[str, float] = {}
_price_cache_ts: float = 0.0
_PRICE_TTL = 30.0


def _cached_prices(tickers: list[str]) -> dict[str, float]:
    global _price_cache, _price_cache_ts
    if _time.time() - _price_cache_ts > _PRICE_TTL and tickers:
        _price_cache = get_current_prices(tickers)
        _price_cache_ts = _time.time()
    return _price_cache


def _market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return config.MARKET_OPEN <= now.time() <= config.MARKET_CLOSE


# ── API ────────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    stats = _ledger.get_stats()
    scans = _ledger.get_scan_results(1)
    last_scan = scans[0]["scanned_at"][11:16] if scans else None
    return {
        "time": datetime.now(ET).strftime("%H:%M:%S"),
        "timezone": "ET",
        "market_open": _market_open(),
        "capital": round(_portfolio.capital, 2),
        "starting_capital": config.STARTING_CAPITAL,
        "capital_deployed": round(_portfolio.capital_deployed, 2),
        "available_capital": round(_portfolio.available_capital, 2),
        "position_count": _portfolio.position_count,
        "max_positions": config.MAX_POSITIONS,
        "last_scan_time": last_scan,
    }


@app.get("/api/positions")
def positions():
    trades = _portfolio.open_trades
    if not trades:
        return []
    prices = _cached_prices([t.ticker for t in trades])
    return [
        {
            "id": t.id,
            "ticker": t.ticker,
            "asset_type": t.asset_type,
            "entry_price": round(t.entry_price, 4),
            "current_price": round(prices.get(t.ticker, t.entry_price), 4),
            "quantity": round(t.quantity, 4),
            "stop_loss": round(t.stop_loss, 4),
            "take_profit": round(t.take_profit, 4),
            "unrealized_pnl": round(
                (prices.get(t.ticker, t.entry_price) - t.entry_price) * t.quantity, 2
            ),
            "unrealized_pnl_pct": round(
                (prices.get(t.ticker, t.entry_price) - t.entry_price) / t.entry_price * 100, 2
            ),
            "entry_time": t.entry_time[11:16] if t.entry_time else "—",
        }
        for t in trades
    ]


@app.get("/api/scans")
def scans():
    return _ledger.get_scan_results(25)


@app.get("/api/trades")
def trades():
    return [
        {
            "ticker": t.ticker,
            "asset_type": t.asset_type,
            "entry_price": round(t.entry_price, 4),
            "exit_price": round(t.exit_price, 4) if t.exit_price is not None else None,
            "pnl": round(t.pnl, 2) if t.pnl is not None else None,
            "pnl_pct": round(t.pnl_pct, 2) if t.pnl_pct is not None else None,
            "exit_reason": t.exit_reason,
            "entry_time": t.entry_time[5:16] if t.entry_time else "—",
            "exit_time": t.exit_time[5:16] if t.exit_time else "—",
        }
        for t in _ledger.get_recent_trades(40)
    ]


@app.get("/api/stats")
def stats():
    s = _ledger.get_stats()
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in s.items()}


# Static frontend — must be registered last
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="warning")
