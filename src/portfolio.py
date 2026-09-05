"""
Position management: open/close paper trades, enforce risk rules,
handle end-of-day stock liquidation.
"""

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pytz

from .analyzer import Analysis
from .broker import Broker, SimBroker
from .fees import sell_regulatory_fee
from .ledger import Ledger, Trade
from .risk import position_quantity
import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


class Portfolio:
    # Consecutive "broker has no such position" confirmations before an open
    # ledger trade is declared orphaned and managed locally (3 × the monitor
    # interval — enough to rule out API races around a bracket-leg fill).
    ORPHAN_STRIKES = 3

    def __init__(self, ledger: Ledger, broker: Optional[Broker] = None,
                 starting_capital: float = None):
        self.ledger = ledger
        self.broker = broker or SimBroker()
        self._sim = SimBroker()          # executes fills for orphaned trades
        self._orphan_strikes: dict[str, int] = {}
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

    @property
    def open_risk(self) -> float:
        total = 0.0
        for trade in self.open_trades:
            risk = max(self._initial_risk(trade), 0.0)
            original_stop = max(0.0, trade.entry_price - risk)
            fees = (sell_regulatory_fee(original_stop, trade.quantity)
                    if trade.asset_type == "stock" else 0.0)
            total += (risk + original_stop * config.FEE_SLIPPAGE_PCT) * trade.quantity + fees
        return total

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
        size_pct = (config.SWING_POSITION_SIZE_PCT if analysis.strategy == "swing"
                    else config.POSITION_SIZE_PCT)
        margin = min(
            self.capital * size_pct,
            self.available_capital * 0.95,
        )
        if quantity <= 0:
            logger.info("SKIP %s: invalid levels or risk budget exhausted", analysis.ticker)
            return None
        margin = quantity * expected_fill / config.LEVERAGE

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
            strategy=analysis.strategy,
        )
        logger.info(
            "OPEN  %-8s @ $%8.2f  SL $%8.2f  TP $%8.2f  qty %.3f  [%s, %s]",
            analysis.ticker, fill.price, analysis.stop_loss,
            analysis.take_profit, fill.qty, self.broker.name, analysis.strategy,
        )
        return trade

    def _initial_risk(self, trade: Trade) -> float:
        """Actual initial fill-to-stop risk; legacy rows retain their old fallback."""
        if trade.initial_risk is not None:
            return trade.initial_risk
        # Legacy rows have no original stop. Preserve their historical trailing
        # behavior until they close; all new trades persist actual initial risk.
        return (trade.take_profit - trade.entry_price) / config.TAKE_PROFIT_R_MULT

    def _record_close(self, trade: Trade, gross_price: float, reason: str) -> Trade:
        """Record a close net of Alpaca sell-side regulatory fees (SEC +
        FINRA TAF). `gross_price` is the executed fill; the fee is folded
        into the effective exit price so realized PnL / pnl_pct reflect every
        real cost (buys are fee-free; spread/slippage is already in the fill).
        """
        reg_fee = (sell_regulatory_fee(gross_price, trade.quantity)
                   if trade.asset_type == "stock" else 0.0)
        net_price = gross_price - (reg_fee / trade.quantity if trade.quantity else 0.0)
        return self.ledger.close_trade(trade.id, net_price, reason)

    def _swing_exit_reason(
        self, trade: Trade, price: float,
        swing_levels: Optional[dict[str, float]], now: Optional[datetime],
    ) -> Optional[str]:
        """Rule exits for swing trades, evaluated in the last 15 minutes of
        a session (EOD_CLOSE_TIME..MARKET_CLOSE) on sessions AFTER entry:
        - "swing_exit": price above the mean of the prior 4 daily closes,
          i.e. today's close lifts the 5-day SMA (the Connors exit)
        - "time_exit": SWING_MAX_HOLD_DAYS sessions elapsed
        Unknown level (no scan yet this session) -> no rule exit; the
        stop / target legs still protect the position."""
        now = now or datetime.now(ET)
        if now.weekday() >= 5:
            return None
        if not (config.EOD_CLOSE_TIME <= now.time() <= config.MARKET_CLOSE):
            return None
        entry_day = datetime.fromisoformat(trade.entry_time).date()
        held = int(np.busday_count(entry_day, now.date()))   # sessions since entry
        if held < 1:
            return None
        level = (swing_levels or {}).get(trade.ticker)
        if level is not None and price > level:
            return "swing_exit"
        if held >= config.SWING_MAX_HOLD_DAYS:
            return "time_exit"
        return None

    def check_exits(
        self, current_prices: dict[str, float],
        swing_levels: Optional[dict[str, float]] = None,
        now: Optional[datetime] = None,
    ) -> list[Trade]:
        """
        Close positions that hit stop-loss or take-profit.

        Swing trades additionally exit by rule (see _swing_exit_reason);
        `swing_levels` maps ticker -> mean of the prior 4 daily closes,
        supplied by the bot from its latest scan.
        R-based trailing: once gain >= PROFIT_TRAIL_TRIGGER_R * R, the stop
        trails PROFIT_TRAIL_DISTANCE_R * R below the observed price.
        The stop only ever moves up — it never retracts.

        When the broker manages exits (server-side bracket legs), exits the
        broker already executed are reconciled into the ledger and trailing
        raises the broker's stop order; the local stop/target/margin-call
        checks run only for the simulated broker.

        Orphan fallback: a ledger trade the managing broker never opened
        (e.g. filled by the simulator before a broker switch) can never be
        exited broker-side — without a fallback it stays frozen forever
        while holding capital. Once the broker denies holding the position
        ORPHAN_STRIKES cycles in a row, the trade is managed locally with
        simulated fills until it closes.
        """
        closed: list[Trade] = []
        for trade in self.open_trades:
            exec_broker: Broker = self.broker

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
                    self._orphan_strikes.pop(trade.ticker, None)
                    continue

                # ── Orphan tracking ────────────────────────────────────────
                has = self.broker.has_position(trade.ticker)
                if has is False:
                    n = self._orphan_strikes.get(trade.ticker, 0) + 1
                    self._orphan_strikes[trade.ticker] = n
                    if n == self.ORPHAN_STRIKES:
                        logger.warning(
                            "%s: open in ledger but %s holds no such position "
                            "(%d checks) — managing it locally from here on",
                            trade.ticker, self.broker.name, n,
                        )
                elif has is True:
                    self._orphan_strikes.pop(trade.ticker, None)
                # has is None → transport error, leave the count unchanged

                if self._orphan_strikes.get(trade.ticker, 0) >= self.ORPHAN_STRIKES:
                    exec_broker = self._sim   # local stop/target management

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
                    if exec_broker.raise_stop(trade.ticker, new_stop):
                        self.ledger.update_stop_loss(trade.id, new_stop)
                        logger.info(
                            "TRAIL %-8s  +%.1fR → stop raised $%.2f → $%.2f",
                            trade.ticker, gain / r, trade.stop_loss, new_stop,
                        )
                        # Refresh so the stop-loss check below sees the new value
                        trade = self.ledger.get_trade(trade.id)  # type: ignore[assignment]

            # ── Swing rule / time exits (bot-initiated, any broker) ───────
            if trade.strategy == "swing":
                reason = self._swing_exit_reason(trade, price, swing_levels, now)
                if reason is not None:
                    fill = exec_broker.close(trade.ticker, price, reason)
                    if fill is not None:
                        closed_trade = self._record_close(trade, fill.price, reason)
                        logger.info(
                            "CLOSE %-8s @ $%8.2f  PnL $%+.2f (%+.1f%%)  [%s @ %s]",
                            trade.ticker, fill.price, closed_trade.pnl or 0,
                            closed_trade.pnl_pct or 0, reason, exec_broker.name,
                        )
                        closed.append(closed_trade)
                        self._orphan_strikes.pop(trade.ticker, None)
                    continue   # a failed close is retried next cycle

            if exec_broker.manages_exits:
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

            fill = exec_broker.close(trade.ticker, price, reason)
            if fill is None:
                continue
            closed_trade = self._record_close(trade, fill.price, reason)
            logger.info(
                "CLOSE %-8s @ $%8.2f  PnL $%+.2f (%+.1f%%)  [%s @ %s]",
                trade.ticker, price, closed_trade.pnl or 0,
                closed_trade.pnl_pct or 0, reason, exec_broker.name,
            )
            closed.append(closed_trade)
            self._orphan_strikes.pop(trade.ticker, None)
        return closed

    def eod_close_stocks(self, current_prices: dict[str, float]) -> list[Trade]:
        """Force-close open stock positions (called at 15:45 ET).
        Positions the broker can't fill are left open (never faked as
        break-even) — the caller retries until none remain."""
        closed: list[Trade] = []
        for trade in self.open_trades:
            if trade.asset_type != "stock":
                continue
            # Orphaned trades (see check_exits) are liquidated locally —
            # the managing broker holds nothing to close.
            orphaned = self._orphan_strikes.get(trade.ticker, 0) >= self.ORPHAN_STRIKES
            exec_broker: Broker = self._sim if orphaned else self.broker
            price = current_prices.get(trade.ticker)
            if price is None and not exec_broker.manages_exits:
                # The simulator needs a price to fill against; a managing
                # broker liquidates at market without one.
                logger.warning(
                    "EOD: no price for %s — leaving open, will retry", trade.ticker
                )
                continue
            fill = exec_broker.close(trade.ticker, price or 0.0, "eod_close")
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
