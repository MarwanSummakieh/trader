"""
Execution layer. The Portfolio stays the single bookkeeper (SQLite ledger);
a Broker only executes orders and reports fills.

Two implementations:
- SimBroker (default): instant simulated fills with the same slippage model
  the backtest uses. Exit decisions stay in Portfolio.check_exits.
- AlpacaBroker: server-side bracket orders (entry + stop + take-profit live
  at the broker), so stops fire even if this process is down or its data
  feed stalls. The Portfolio reconciles exits instead of deciding them, and
  trailing raises the bracket's stop leg via order replace.

Alpaca notes:
- Bracket orders require whole-share quantities — fractional qty is rounded
  down (a position that rounds to 0 shares is skipped).
- Stocks only. Crypto symbols use different symbology and no bracket
  support; crypto is disabled in config anyway.
- CFD-style LEVERAGE/MARGIN_CALL_LOSS semantics apply only to SimBroker.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

import requests

import config

logger = logging.getLogger(__name__)

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"


@dataclass
class Fill:
    price: float
    qty: float
    reason: Optional[str] = None


class Broker(Protocol):
    manages_exits: bool
    name: str

    def open(self, ticker: str, qty: float, entry_hint: float,
             stop: float, tp: float) -> Optional[Fill]: ...
    def close(self, ticker: str, price_hint: float, reason: str) -> Optional[Fill]: ...
    def raise_stop(self, ticker: str, new_stop: float) -> bool: ...
    def detect_exit(self, ticker: str) -> Optional[Fill]: ...


class SimBroker:
    """Paper fills matching the backtest's slippage model exactly."""

    manages_exits = False
    name = "paper-sim"

    def open(self, ticker, qty, entry_hint, stop, tp):
        return Fill(entry_hint * (1 + config.FEE_SLIPPAGE_PCT), qty)

    def close(self, ticker, price_hint, reason):
        return Fill(price_hint * (1 - config.FEE_SLIPPAGE_PCT), 0.0, reason)

    def raise_stop(self, ticker, new_stop):
        return True

    def detect_exit(self, ticker):
        return None


class AlpacaBroker:
    manages_exits = True
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, base_url: str = ALPACA_PAPER_URL):
        self._base = base_url.rstrip("/")
        self._s = requests.Session()
        self._s.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type": "application/json",
        })

    # ── Transport ──────────────────────────────────────────────────────────

    def _req(self, method: str, path: str, json: Optional[dict] = None,
             params: Optional[dict] = None) -> tuple[int, Optional[dict | list]]:
        """(status_code, parsed body). Status 0 = transport error — callers
        must treat 0 as 'unknown', never as 'position gone' or 'order dead'."""
        try:
            r = self._s.request(method, f"{self._base}{path}",
                                json=json, params=params, timeout=15)
        except requests.RequestException as e:
            logger.warning("Alpaca %s %s transport error: %s", method, path, e)
            return 0, None
        body = None
        if r.text:
            try:
                body = r.json()
            except ValueError:
                pass
        if r.status_code >= 400 and r.status_code != 404:
            logger.warning("Alpaca %s %s -> %s %s",
                           method, path, r.status_code, r.text[:200])
        return r.status_code, body

    # ── Account ────────────────────────────────────────────────────────────

    def account(self) -> Optional[dict]:
        status, body = self._req("GET", "/v2/account")
        return body if status == 200 else None

    def test_connection(self) -> tuple[bool, str]:
        acct = self.account()
        if acct is None:
            return False, "Alpaca connection failed (check keys / endpoint)"
        mode = "paper" if "paper" in self._base else "LIVE"
        return True, (f"Alpaca {mode} account ok — equity "
                      f"${float(acct.get('equity', 0)):,.2f}")

    # ── Order plumbing ─────────────────────────────────────────────────────

    def _poll_filled(self, order_id: str, timeout_s: float = 15.0) -> Optional[dict]:
        deadline = time.monotonic() + timeout_s
        while True:
            status, body = self._req("GET", f"/v2/orders/{order_id}")
            if status == 200 and body and body.get("status") == "filled":
                return body
            if time.monotonic() >= deadline:
                return None
            time.sleep(1.0)

    def _open_orders(self, ticker: str) -> list[dict]:
        status, body = self._req("GET", "/v2/orders",
                                 params={"status": "open", "symbols": ticker})
        return body if status == 200 and isinstance(body, list) else []

    def _cancel_open_orders(self, ticker: str) -> None:
        for o in self._open_orders(ticker):
            self._req("DELETE", f"/v2/orders/{o['id']}")

    # ── Broker interface ───────────────────────────────────────────────────

    def open(self, ticker, qty, entry_hint, stop, tp):
        if "-USD" in ticker:
            logger.warning("AlpacaBroker is stocks-only, refusing %s", ticker)
            return None
        whole_qty = int(qty)   # bracket orders don't support fractional shares
        if whole_qty < 1:
            logger.warning("OPEN %s skipped — %.2f shares rounds to 0", ticker, qty)
            return None
        status, order = self._req("POST", "/v2/orders", json={
            "symbol": ticker,
            "qty": str(whole_qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{tp:.2f}"},
            "stop_loss": {"stop_price": f"{stop:.2f}"},
        })
        if status != 200 or not order:
            return None
        filled = self._poll_filled(order["id"])
        if filled is None:
            # Never leave an un-tracked order behind: cancel, then re-check —
            # the fill may have raced the cancel.
            self._req("DELETE", f"/v2/orders/{order['id']}")
            status, body = self._req("GET", f"/v2/orders/{order['id']}")
            if status == 200 and body and body.get("status") == "filled":
                filled = body
            else:
                if body and body.get("status") == "partially_filled":
                    logger.error("OPEN %s partially filled then canceled — "
                                 "flattening", ticker)
                    self.close(ticker, entry_hint, "partial_fill_flatten")
                return None
        return Fill(float(filled["filled_avg_price"]), float(filled["filled_qty"]))

    def raise_stop(self, ticker, new_stop):
        stop_orders = [o for o in self._open_orders(ticker)
                       if o.get("side") == "sell" and o.get("type") == "stop"]
        if not stop_orders:
            logger.warning("TRAIL %s: no server-side stop order found", ticker)
            return False
        status, _ = self._req("PATCH", f"/v2/orders/{stop_orders[0]['id']}",
                              json={"stop_price": f"{new_stop:.2f}"})
        return status == 200

    def detect_exit(self, ticker):
        status, _ = self._req("GET", f"/v2/positions/{ticker}")
        if status == 200:
            return None            # still open
        if status != 404:
            return None            # transport/API error — unknown, don't act
        # Position is gone: find the exit fill among recent closed orders.
        status, orders = self._req("GET", "/v2/orders", params={
            "status": "closed", "symbols": ticker,
            "direction": "desc", "limit": 20,
        })
        if status != 200 or not isinstance(orders, list):
            return None
        for o in orders:
            if o.get("side") == "sell" and o.get("status") == "filled":
                reason = {"stop": "stop_loss", "limit": "take_profit"}.get(
                    o.get("type"), "manual_close")
                return Fill(float(o["filled_avg_price"]),
                            float(o["filled_qty"]), reason)
        logger.error("%s: position gone but no exit fill found — "
                     "will retry next cycle", ticker)
        return None

    def close(self, ticker, price_hint, reason):
        self._cancel_open_orders(ticker)   # bracket legs would reject the sell
        status, order = self._req("DELETE", f"/v2/positions/{ticker}")
        if status == 404:
            # Already flat (e.g. a bracket leg raced us) — reconcile instead.
            return self.detect_exit(ticker)
        if status != 200 or not order:
            return None
        filled = self._poll_filled(order["id"])
        if filled is None:
            logger.error("CLOSE %s: liquidation order not filled in time — "
                         "will retry next cycle", ticker)
            return None
        return Fill(float(filled["filled_avg_price"]),
                    float(filled["filled_qty"]), reason)


def make_broker() -> Broker:
    """Build the configured broker. Fails fast on unsafe/incomplete config."""
    if config.BROKER == "alpaca":
        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise SystemExit("BROKER=alpaca but ALPACA_API_KEY/ALPACA_SECRET_KEY unset")
        if "paper" not in config.ALPACA_BASE_URL and not config.ALPACA_ALLOW_LIVE:
            raise SystemExit(
                "Refusing non-paper Alpaca endpoint without ALPACA_ALLOW_LIVE=1 — "
                "this bot has never traded real money; validate on paper first."
            )
        return AlpacaBroker(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
                            config.ALPACA_BASE_URL)
    if config.BROKER != "paper":
        raise SystemExit(f"Unknown BROKER={config.BROKER!r} (use 'paper' or 'alpaca')")
    return SimBroker()
