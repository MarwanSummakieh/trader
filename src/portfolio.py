"""
Position management: open/close paper trades, enforce risk rules,
handle end-of-day stock liquidation.
"""

import logging
from typing import Optional

from .analyzer import Analysis
from .ledger import Ledger, Trade
import config

logger = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, ledger: Ledger, starting_capital: float = None):
        self.ledger = ledger
        self.capital = starting_capital or config.STARTING_CAPITAL

    # ── Derived state ──────────────────────────────────────────────────────

    @property
    def open_trades(self) -> list[Trade]:
        return self.ledger.get_open_trades()

    @property
    def open_tickers(self) -> set[str]:
        return {t.ticker for t in self.open_trades}

    @property
    def position_count(self) -> int:
        return len(self.open_trades)

    @property
    def capital_deployed(self) -> float:
        return sum(t.entry_price * t.quantity for t in self.open_trades)

    @property
    def available_capital(self) -> float:
        return self.capital - self.capital_deployed

    def can_open(self) -> bool:
        slot_ok = self.position_count < config.MAX_POSITIONS
        size = self.capital * config.POSITION_SIZE_PCT
        capital_ok = self.available_capital >= size * 0.5
        return slot_ok and capital_ok

    # ── Trade actions ──────────────────────────────────────────────────────

    def open_position(self, analysis: Analysis) -> Optional[Trade]:
        if not self.can_open():
            return None
        if analysis.ticker in self.open_tickers:
            return None

        position_value = min(
            self.capital * config.POSITION_SIZE_PCT,
            self.available_capital * 0.95,
        )
        quantity = position_value / analysis.entry

        trade = self.ledger.open_trade(
            ticker=analysis.ticker,
            asset_type=analysis.asset_type,
            entry_price=analysis.entry,
            quantity=quantity,
            stop_loss=analysis.stop_loss,
            take_profit=analysis.take_profit,
            signals=analysis.signals,
        )
        logger.info(
            "OPEN  %-8s @ $%8.2f  SL $%8.2f  TP $%8.2f  qty %.3f",
            analysis.ticker, analysis.entry, analysis.stop_loss,
            analysis.take_profit, quantity,
        )
        return trade

    def check_exits(self, current_prices: dict[str, float]) -> list[Trade]:
        """Close positions that have hit stop-loss or take-profit."""
        closed: list[Trade] = []
        for trade in self.open_trades:
            price = current_prices.get(trade.ticker)
            if price is None:
                continue
            if price <= trade.stop_loss:
                reason = "stop_loss"
            elif price >= trade.take_profit:
                reason = "take_profit"
            else:
                continue
            closed_trade = self.ledger.close_trade(trade.id, price, reason)  # type: ignore[arg-type]
            logger.info(
                "CLOSE %-8s @ $%8.2f  PnL $%+.2f (%+.1f%%)  [%s]",
                trade.ticker, price,
                closed_trade.pnl or 0, closed_trade.pnl_pct or 0, reason,
            )
            closed.append(closed_trade)
        return closed

    def eod_close_stocks(self, current_prices: dict[str, float]) -> list[Trade]:
        """Force-close all open stock positions (called at 15:45 ET)."""
        closed: list[Trade] = []
        for trade in self.open_trades:
            if trade.asset_type != "stock":
                continue
            price = current_prices.get(trade.ticker, trade.entry_price)
            closed_trade = self.ledger.close_trade(trade.id, price, "eod_close")  # type: ignore[arg-type]
            logger.info(
                "EOD   %-8s @ $%8.2f  PnL $%+.2f (%+.1f%%)",
                trade.ticker, price, closed_trade.pnl or 0, closed_trade.pnl_pct or 0,
            )
            closed.append(closed_trade)
        return closed

    def unrealized_pnl(self, current_prices: dict[str, float]) -> float:
        return sum(
            (current_prices.get(t.ticker, t.entry_price) - t.entry_price) * t.quantity
            for t in self.open_trades
        )
