"""
Position management: open/close paper trades, enforce risk rules,
handle end-of-day stock liquidation.
"""

import logging
from typing import Optional

from .analyzer import Analysis
from .broker import Broker, SimBroker
from .fees import sell_regulatory_fee
from .ledger import Ledger, Trade
import config

logger = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, ledger: Ledger, broker: Optional[Broker] = None,
                 starting_capital: float = None):
        self.ledger = ledger
        self.broker = broker or SimBroker()
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

    @property
    def crypto_capital_deployed(self) -> float:
        """Margin committed to crypto positions (see capital_deployed)."""
        return sum(
            t.entry_price * t.quantity
            for t in self.open_trades if t.asset_type == "crypto"
        ) / config.LEVERAGE

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

        # Size against the expected fill (signal price + slippage); the
        # broker reports the actual fill price/qty it achieved.
        expected_fill = analysis.entry * (1 + config.FEE_SLIPPAGE_PCT)
        margin = min(
            self.capital * config.POSITION_SIZE_PCT,
            self.available_capital * 0.95,
        )

        # Crypto trades 24/7; without a budget it would consume all buying
        # power overnight and leave nothing for the stock session open.
        if analysis.asset_type == "crypto":
            crypto_budget = self.capital * config.CRYPTO_MAX_CAPITAL_PCT
            if self.crypto_capital_deployed + margin > crypto_budget:
                logger.info(
                    "SKIP  %-8s — crypto budget reached "
                    "($%.0f of $%.0f committed)",
                    analysis.ticker, self.crypto_capital_deployed, crypto_budget,
                )
                return None

        quantity = margin * config.LEVERAGE / expected_fill

        fill = self.broker.open(
            analysis.ticker, quantity,
            entry_hint=analysis.entry,
            stop=analysis.stop_loss, tp=analysis.take_profit,
        )
        if fill is None:
            return None

        trade = self.ledger.open_trade(
            ticker=analysis.ticker,
            asset_type=analysis.asset_type,
            entry_price=fill.price,
            quantity=fill.qty,
            stop_loss=analysis.stop_loss,
            take_profit=analysis.take_profit,
            signals=analysis.signals,
        )
        logger.info(
            "OPEN  %-8s @ $%8.2f  SL $%8.2f  TP $%8.2f  qty %.3f  [%s]",
            analysis.ticker, fill.price, analysis.stop_loss,
            analysis.take_profit, fill.qty, self.broker.name,
        )
        return trade

    def _initial_risk(self, trade: Trade) -> float:
        """Initial risk per share (R). The stop moves as the trade trails, but
        the target never does — so R is recovered from the target distance."""
        return (trade.take_profit - trade.entry_price) / config.TAKE_PROFIT_R_MULT

    def _record_close(self, trade: Trade, gross_price: float, reason: str) -> Trade:
        """Record a close net of Alpaca sell-side regulatory fees (SEC +
        FINRA TAF). `gross_price` is the executed fill; the fee is folded
        into the effective exit price so realized PnL / pnl_pct reflect every
        real cost (buys are fee-free; spread/slippage is already in the fill).
        """
        reg_fee = sell_regulatory_fee(gross_price, trade.quantity)
        net_price = gross_price - (reg_fee / trade.quantity if trade.quantity else 0.0)
        return self.ledger.close_trade(trade.id, net_price, reason)

    def check_exits(self, current_prices: dict[str, float]) -> list[Trade]:
        """
        Close positions that hit stop-loss or take-profit.
        R-based trailing: once gain >= PROFIT_TRAIL_TRIGGER_R * R, the stop
        trails PROFIT_TRAIL_DISTANCE_R * R below the observed price.
        The stop only ever moves up — it never retracts.

        When the broker manages exits (server-side bracket legs), exits the
        broker already executed are reconciled into the ledger and trailing
        raises the broker's stop order; the local stop/target/margin-call
        checks run only for the simulated broker.
        """
        closed: list[Trade] = []
        for trade in self.open_trades:
            # ── Broker-side exits (authoritative when managed) ─────────────
            if self.broker.manages_exits:
                fill = self.broker.detect_exit(trade.ticker)
                if fill is not None:
                    reason = fill.reason or "manual_close"
                    # A stop at/above entry can only be a trailed stop
                    # (initial stops are strictly below entry).
                    if reason == "stop_loss" and trade.stop_loss >= trade.entry_price:
                        reason = "trail_stop"
                    closed_trade = self._record_close(trade, fill.price, reason)
                    logger.info(
                        "CLOSE %-8s @ $%8.2f  PnL $%+.2f (%+.1f%%)  [%s @ %s]",
                        trade.ticker, fill.price, closed_trade.pnl or 0,
                        closed_trade.pnl_pct or 0, reason, self.broker.name,
                    )
                    closed.append(closed_trade)
                    continue

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
                    # Broker first: the ledger must never claim a tighter stop
                    # than the one actually resting at the broker.
                    if self.broker.raise_stop(trade.ticker, new_stop):
                        self.ledger.update_stop_loss(trade.id, new_stop)
                        logger.info(
                            "TRAIL %-8s  +%.1fR → stop raised $%.2f → $%.2f",
                            trade.ticker, gain / r, trade.stop_loss, new_stop,
                        )
                        # Refresh so the stop-loss check below sees the new value
                        trade = self.ledger.get_trade(trade.id)  # type: ignore[assignment]

            if self.broker.manages_exits:
                continue   # stop/target execution lives at the broker

            # ── Local exit checks (simulated broker only) ──────────────────
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

            fill = self.broker.close(trade.ticker, price, reason)
            if fill is None:
                continue
            closed_trade = self._record_close(trade, fill.price, reason)
            logger.info(
                "CLOSE %-8s @ $%8.2f  PnL $%+.2f (%+.1f%%)  [%s]",
                trade.ticker, price,
                closed_trade.pnl or 0, closed_trade.pnl_pct or 0, reason,
            )
            closed.append(closed_trade)
        return closed

    def eod_close_stocks(self, current_prices: dict[str, float]) -> list[Trade]:
        """Force-close open stock positions (called at 15:45 ET).
        Positions the broker can't fill are left open (never faked as
        break-even) — the caller retries until none remain."""
        closed: list[Trade] = []
        for trade in self.open_trades:
            if trade.asset_type != "stock":
                continue
            price = current_prices.get(trade.ticker)
            if price is None and not self.broker.manages_exits:
                # The simulator needs a price to fill against; a managing
                # broker liquidates at market without one.
                logger.warning(
                    "EOD: no price for %s — leaving open, will retry", trade.ticker
                )
                continue
            fill = self.broker.close(trade.ticker, price or 0.0, "eod_close")
            if fill is None:
                logger.warning(
                    "EOD: close failed for %s — leaving open, will retry",
                    trade.ticker,
                )
                continue
            closed_trade = self._record_close(
                trade, fill.price, fill.reason or "eod_close"
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
