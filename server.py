#!/usr/bin/env python3
"""
FastAPI dashboard server.

Serves BOTH bot instances from one process:
  /        + /api/...         → stock instance  (config.DB_PATH)
  /crypto  + /api/crypto/...  → crypto instance (config.CRYPTO_DB_PATH)

Each bot writes its own ledger; this server only reads them — run all three
processes simultaneously:

  python main.py                                     # stock bot
  ENABLE_STOCKS=0 ENABLE_CRYPTO=1 \
    DB_PATH=ledger-crypto.db STARTING_CAPITAL=1000 \
    BROKER=paper python main.py                      # crypto bot
  python server.py                                   # this server
  open http://localhost:5000
"""

import os
import sys
from datetime import datetime

import pytz
import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(__file__))

import config
from src.data import get_current_prices
from src.ledger import Ledger
from src.portfolio import Portfolio

app = FastAPI(title="Day Trader Bot", docs_url=None, redoc_url=None)
ET = pytz.timezone("America/New_York")

# Live prices are TTL-cached inside src.data.get_current_prices, so page
# refreshes don't hammer yfinance.


def _stock_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return config.MARKET_OPEN <= now.time() <= config.MARKET_CLOSE


# ── API (one router per bot instance) ─────────────────────────────────────────

def make_api(ledger: Ledger, portfolio: Portfolio, starting_capital: float,
             always_open: bool) -> APIRouter:
    """Endpoints bound to one instance's ledger. `always_open` marks the
    24/7 crypto market (the stock instance follows US session hours)."""
    router = APIRouter()

    @router.get("/status")
    def status():
        scans = ledger.get_scan_results(1)
        last_scan = scans[0]["scanned_at"][11:16] if scans else None
        open_trades = portfolio.open_trades
        unrealized = 0.0
        if open_trades:
            prices = get_current_prices([t.ticker for t in open_trades])
            unrealized = portfolio.unrealized_pnl(prices)
        return {
            "version": config.VERSION,
            "time": datetime.now(ET).strftime("%H:%M:%S"),
            "timezone": "ET",
            "market_open": True if always_open else _stock_market_open(),
            "capital": round(portfolio.capital, 2),
            "starting_capital": starting_capital,
            "capital_deployed": round(portfolio.capital_deployed, 2),
            "available_capital": round(portfolio.available_capital, 2),
            "unrealized_pnl": round(unrealized, 2),
            "position_count": portfolio.position_count,
            "max_positions": config.MAX_POSITIONS,
            "last_scan_time": last_scan,
        }

    @router.get("/positions")
    def positions():
        trades = portfolio.open_trades
        if not trades:
            return []
        prices = get_current_prices([t.ticker for t in trades])
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
                "strategy": t.strategy,
            }
            for t in trades
        ]

    @router.get("/scans")
    def scans():
        return ledger.get_scan_results(25)

    @router.get("/trades")
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
                "strategy": t.strategy,
                "entry_time": t.entry_time[5:16].replace("T", " ") if t.entry_time else "—",
                "exit_time": t.exit_time[5:16].replace("T", " ") if t.exit_time else "—",
            }
            for t in ledger.get_recent_trades(40)
        ]

    @router.get("/stats")
    def stats():
        s = ledger.get_stats()
        out = {k: round(v, 2) if isinstance(v, float) else v for k, v in s.items()}
        today = ledger.get_stats_for_day(datetime.now(ET).strftime("%Y-%m-%d"))
        out["today"] = {k: round(v, 2) if isinstance(v, float) else v
                        for k, v in today.items()}
        return out

    return router


_stock_ledger = Ledger(config.DB_PATH)
app.include_router(
    make_api(_stock_ledger, Portfolio(_stock_ledger),
             config.STARTING_CAPITAL, always_open=False),
    prefix="/api",
)

_crypto_ledger = Ledger(config.CRYPTO_DB_PATH)
app.include_router(
    make_api(_crypto_ledger,
             Portfolio(_crypto_ledger,
                       starting_capital=config.CRYPTO_STARTING_CAPITAL),
             config.CRYPTO_STARTING_CAPITAL, always_open=True),
    prefix="/api/crypto",
)


# ── Frontend ───────────────────────────────────────────────────────────────────

# Same single-page app as / — it switches to the crypto API by pathname.
@app.get("/crypto")
def crypto_page():
    return FileResponse("static/index.html")


# Static frontend — must be registered last
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="warning")
