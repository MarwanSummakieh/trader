"""
SQLite paper-trading ledger.
Every buy and sell is recorded; PnL is calculated on close.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class Trade:
    id: Optional[int]
    ticker: str
    asset_type: str
    entry_time: str       # ISO-8601
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    signals: str          # JSON array
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    def unrealized_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity


class Ledger:
    def __init__(self, db_path: str = "ledger.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets the dashboard read while the bot writes without
        # "database is locked" errors (two processes share this file).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init()

    def _init(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT    NOT NULL,
                asset_type  TEXT    NOT NULL,
                entry_time  TEXT    NOT NULL,
                exit_time   TEXT,
                entry_price REAL    NOT NULL,
                exit_price  REAL,
                quantity    REAL    NOT NULL,
                stop_loss   REAL    NOT NULL,
                take_profit REAL    NOT NULL,
                pnl         REAL,
                pnl_pct     REAL,
                exit_reason TEXT,
                signals     TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT    NOT NULL,
                asset_type   TEXT    NOT NULL,
                score        INTEGER NOT NULL,
                price        REAL    NOT NULL,
                rsi          REAL,
                volume_ratio REAL,
                trend        TEXT,
                signals      TEXT,
                stop_loss    REAL,
                take_profit  REAL,
                scanned_at   TEXT    NOT NULL
            )
        """)
        self._conn.commit()

    # ── Writes ────────────────────────────────────────────────────────────

    def open_trade(
        self,
        ticker: str,
        asset_type: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        signals: list[str],
    ) -> Trade:
        now = datetime.now(ET).isoformat()
        cur = self._conn.execute(
            """INSERT INTO trades
               (ticker, asset_type, entry_time, entry_price, quantity,
                stop_loss, take_profit, signals)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, asset_type, now, entry_price, quantity,
             stop_loss, take_profit, json.dumps(signals)),
        )
        self._conn.commit()
        return self.get_trade(cur.lastrowid)  # type: ignore[arg-type]

    def update_stop_loss(self, trade_id: int, new_stop: float) -> None:
        """Raise the stop loss on an open trade (profit lock / trailing stop)."""
        self._conn.execute(
            "UPDATE trades SET stop_loss=? WHERE id=? AND exit_time IS NULL",
            (new_stop, trade_id),
        )
        self._conn.commit()

    def close_trade(self, trade_id: int, exit_price: float, reason: str) -> Trade:
        trade = self.get_trade(trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        pnl = (exit_price - trade.entry_price) * trade.quantity
        pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
        now = datetime.now(ET).isoformat()
        self._conn.execute(
            "UPDATE trades SET exit_time=?, exit_price=?, pnl=?, pnl_pct=?, exit_reason=? WHERE id=?",
            (now, exit_price, pnl, pnl_pct, reason, trade_id),
        )
        self._conn.commit()
        return self.get_trade(trade_id)  # type: ignore[return-value]

    # ── Reads ─────────────────────────────────────────────────────────────

    def get_trade(self, trade_id: int) -> Optional[Trade]:
        row = self._conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return _row(row) if row else None

    def get_open_trades(self) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_recent_trades(self, n: int = 20) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE exit_time IS NOT NULL ORDER BY exit_time DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [_row(r) for r in rows]

    def get_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT pnl, pnl_pct FROM trades WHERE exit_time IS NOT NULL"
        ).fetchall()
        if not rows:
            return {
                "total_trades": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "avg_pnl_pct": 0.0,
                "best_trade": 0.0, "worst_trade": 0.0,
            }
        pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
        pnl_pcts = [r["pnl_pct"] for r in rows if r["pnl_pct"] is not None]
        wins = [p for p in pnls if p > 0]
        return {
            "total_trades": len(pnls),
            "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
            "total_pnl": sum(pnls),
            "avg_pnl_pct": sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0,
            "best_trade": max(pnls) if pnls else 0.0,
            "worst_trade": min(pnls) if pnls else 0.0,
        }


    # History is retained (not overwritten) so score-vs-outcome analysis is
    # possible later; rows older than the retention window are purged.
    SCAN_RETENTION_DAYS = 30

    def save_scan_results(self, analyses: list) -> None:
        """Append the latest sweep; purge rows past the retention window."""
        now_dt = datetime.now(ET)
        now = now_dt.isoformat()
        cutoff = (now_dt - timedelta(days=self.SCAN_RETENTION_DAYS)).isoformat()
        self._conn.execute("DELETE FROM scan_results WHERE scanned_at < ?", (cutoff,))
        for a in analyses:
            self._conn.execute(
                """INSERT INTO scan_results
                   (ticker, asset_type, score, price, rsi, volume_ratio, trend,
                    signals, stop_loss, take_profit, scanned_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a.ticker, a.asset_type, a.score, a.price, a.rsi, a.volume_ratio,
                 a.trend, json.dumps(a.signals), a.stop_loss, a.take_profit, now),
            )
        self._conn.commit()

    def get_scan_results(self, limit: int = 25) -> list[dict]:
        """Top results from the most recent sweep only."""
        rows = self._conn.execute(
            """SELECT * FROM scan_results
               WHERE scanned_at = (SELECT MAX(scanned_at) FROM scan_results)
               ORDER BY score DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _row(r: sqlite3.Row) -> Trade:
    return Trade(
        id=r["id"],
        ticker=r["ticker"],
        asset_type=r["asset_type"],
        entry_time=r["entry_time"],
        exit_time=r["exit_time"],
        entry_price=r["entry_price"],
        exit_price=r["exit_price"],
        quantity=r["quantity"],
        stop_loss=r["stop_loss"],
        take_profit=r["take_profit"],
        pnl=r["pnl"],
        pnl_pct=r["pnl_pct"],
        exit_reason=r["exit_reason"],
        signals=r["signals"],
    )
