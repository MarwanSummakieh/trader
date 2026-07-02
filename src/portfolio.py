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
        self._starting_capital = starting_capital or config.STARTING_CAPITAL

    # ── Derived state ──────────────────────────────────────────────────────

    @property
    def capital(self) -> float:
        """Starting capital + all realized PnL to date."""
        return self._starting_capital + self.ledger.get_stats()["total_pnl"]

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
        """Cash committed as margin (== exposure at leverage 1). Assumes all
        open positions were opened at the current LEVERAGE setting — change
        leverage only while flat."""
        return sum(
            t.entry_price * t.quantity for t in self.open_trades
        ) / config.LEVERAGE

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

        # Fill above the signal price to model spread/slippage/fees.
        entry_fill = analysis.entry * (1 + config.FEE_SLIPPAGE_PCT)

        margin = min(
            self.capital * config.POSITION_SIZE_PCT,
            self.available_capital * 0.95,
        )
        quantity = margin * config.LEVERAGE / entry_fill

        trade = self.ledger.open_trade(
            ticker=analysis.ticker,
            asset_type=analysis.asset_type,
            entry_price=entry_fill,
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

    def _initial_risk(self, trade: Trade) -> float:
        """Initial risk per share (R). The stop moves as the trade trails, but
        the target never does — so R is recovered from the target distance."""
        return (trade.take_profit - trade.entry_price) / config.TAKE_PROFIT_R_MULT

    def _close_fill(self, price: float) -> float:
        """Exit fill below the observed price to model spread/slippage/fees."""
        return price * (1 - config.FEE_SLIPPAGE_PCT)

    def check_exits(self, current_prices: dict[str, float]) -> list[Trade]:
        """
        Close positions that hit stop-loss or take-profit.
        R-based trailing: once gain >= PROFIT_TRAIL_TRIGGER_R * R, the stop
        trails PROFIT_TRAIL_DISTANCE_R * R below the observed price.
        The stop only ever moves up — it never retracts.
        """
        closed: list[Trade] = []
        for trade in self.open_trades:
            price = current_prices.get(trade.ticker)
            if price is None:
                logger.warning(
                    "No price for %s — exits unchecked this cycle", trade.ticker
                )
                continue

            # ── R-based trailing stop ──────────────────────────────────────
            r = self._initial_risk(trade)
            gain = price - trade.entry_price
            if r > 0 and gain >= config.PROFIT_TRAIL_TRIGGER_R * r:
                new_stop = price - config.PROFIT_TRAIL_DISTANCE_R * r
                if new_stop > trade.stop_loss:
                    self.ledger.update_stop_loss(trade.id, new_stop)
                    logger.info(
                        "TRAIL %-8s  +%.1fR → stop raised $%.2f → $%.2f",
                        trade.ticker, gain / r, trade.stop_loss, new_stop,
                    )
                    # Refresh trade so the stop-loss check below uses the new value
                    trade = self.ledger.get_trade(trade.id)  # type: ignore[assignment]

            # ── Exit checks ────────────────────────────────────────────────
            # Broker liquidation level: loss = MARGIN_CALL_LOSS of committed
            # margin. Far below the stop at leverage 1; binds first only when
            # levered enough that margin runs out before the stop is reached.
            mc_price = trade.entry_price * (1 - config.MARGIN_CALL_LOSS / config.LEVERAGE)
            eff_stop = max(trade.stop_loss, mc_price)
            if price <= eff_stop:
                # A stop at/above entry can only be a trailed stop (initial
                # stops are strictly below entry) — label it separately so
                # signal quality can be analysed later.
                if trade.stop_loss >= trade.entry_price:
                    reason = "trail_stop"
                elif mc_price > trade.stop_loss:
                    reason = "margin_call"
                else:
                    reason = "stop_loss"
            elif price >= trade.take_profit:
                reason = "take_profit"
            else:
                continue

            closed_trade = self.ledger.close_trade(
                trade.id, self._close_fill(price), reason  # type: ignore[arg-type]
            )
            logger.info(
                "CLOSE %-8s @ $%8.2f  PnL $%+.2f (%+.1f%%)  [%s]",
                trade.ticker, price,
                closed_trade.pnl or 0, closed_trade.pnl_pct or 0, reason,
            )
            closed.append(closed_trade)
        return closed

    def eod_close_stocks(self, current_prices: dict[str, float]) -> list[Trade]:
        """Force-close open stock positions (called at 15:45 ET).
        Positions with no available price are left open (never faked as
        break-even) — the caller retries until none remain."""
        closed: list[Trade] = []
        for trade in self.open_trades:
            if trade.asset_type != "stock":
                continue
            price = current_prices.get(trade.ticker)
            if price is None:
                logger.warning(
                    "EOD: no price for %s — leaving open, will retry", trade.ticker
                )
                continue
            closed_trade = self.ledger.close_trade(
                trade.id, self._close_fill(price), "eod_close"  # type: ignore[arg-type]
            )
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
