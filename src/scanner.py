"""
Universe scanner: downloads data + runs analysis for every ticker in parallel,
then filters and ranks results.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from typing import Optional

import pytz

from .analyzer import analyze, Analysis
from .data import get_intraday, get_daily, in_earnings_window
import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


def _scan_one(ticker: str, asset_type: str) -> Optional[Analysis]:
    try:
        intra = get_intraday(ticker)
        daily = get_daily(ticker)
        return analyze(ticker, intra, daily, asset_type=asset_type)
    except Exception as e:
        logger.debug("Scan failed %s: %s", ticker, e)
        return None


def scan_universe(
    stocks: list[str],
    crypto: list[str],
    max_workers: int = 10,
) -> list[Analysis]:
    """
    Scan all tickers concurrently.
    Returns results sorted by score descending (best opportunities first).
    """
    tasks = [(t, "stock") for t in stocks] + [(t, "crypto") for t in crypto]
    results: list[Analysis] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_scan_one, t, at): (t, at) for t, at in tasks}
        for fut in as_completed(futures):
            a = fut.result()
            if a is not None:
                results.append(a)

    results.sort(key=lambda a: a.score, reverse=True)
    return results


# Regime driver for every crypto entry (see _crypto_entry_ok).
BTC_TICKER = "BTC-USD"


def get_buy_candidates(
    analyses: list[Analysis],
    existing_tickers: set[str],
    now: Optional[datetime] = None,
) -> list[Analysis]:
    """
    Entry rules per asset class — each validated (or honestly documented)
    on its own data:

    Stocks — momentum (2026-07-02, 59d/5m, train+holdout; volatility gate
    added 2026-08-20):
    - Intraday EMA stack aligned: price > EMA9 > EMA21
    - Daily ADX > ENTRY_ADX_MIN (trending, not chopping)
    - RSI in [ENTRY_RSI_MIN, ENTRY_RSI_MAX) — momentum, not blow-off
    - Regime: price above daily EMA50 (unless disabled)
    - Volatility: 1.5*ATR must clear ATR_GATE_MULT of the STOP_LOSS_PCT
      floor (unless disabled) — dead tape has no reachable 3R target and
      backtests negative however entered; the bot stands aside instead
    - Entries before STOCK_ENTRY_CUTOFF ET only (afternoon entries tested
      worse even with overnight holds)

    Positions are held overnight (EOD_CLOSE_STOCKS off by default since
    2026-09-03): exits are the stop / 3R target / trail only.

    Stocks — swing (2026-09-03, daily bars 2024-09..2026-09 + 5m parity),
    tried only for names that FAILED the momentum rule:
    - Prior completed daily close: RSI(2) < SWING_RSI2_MAX and above the
      200-day SMA (both fail closed on warmup)
    - Entries before SWING_ENTRY_CUTOFF ET only (first ~30 min)
    - Levels: SWING_STOP_PCT disaster stop, target 3R above; the working
      exit is Portfolio.check_exits' rule (price above the 5-day SMA at
      the session end) or SWING_MAX_HOLD_DAYS sessions

    Crypto — regime-gated 8h breakout (2026-07-22; see config.py for the
    research verdict — edge unproven, gates chosen for capital
    preservation):
    - Last completed 5m close above the prior CRYPTO_BREAKOUT_BARS-bar high
    - Own regime: price above its daily EMA50 (NOT toggleable — the gates
      ARE the strategy)
    - BTC regime: BTC above its daily EMA50; fails closed when BTC is
      missing from the scan
    - Volume >= CRYPTO_MIN_VOL_RATIO x its 20-bar average

    Common: not already in a position. The composite score is deliberately
    NOT used here — it showed no predictive power in backtesting. It
    remains for display/analysis only.
    """
    now = now or datetime.now(ET)
    btc = next((a for a in analyses if a.ticker == BTC_TICKER), None)
    btc_uptrend = bool(btc and btc.regime_ok)

    candidates: list[Analysis] = []
    for a in analyses:
        if a.ticker in existing_tickers:
            continue
        if a.asset_type == "crypto":
            if _crypto_entry_ok(a, btc_uptrend):
                candidates.append(a)
        elif _stock_entry_ok(a, now):
            candidates.append(a)
        elif _swing_entry_ok(a, now):
            candidates.append(_as_swing(a))
    if not config.EARNINGS_FILTER:
        return candidates
    # Earnings check last — it's a (cached) network call, so only survivors
    # pay for it. Entries on reaction days tested at 18% win rate / -0.41R.
    return [
        a for a in candidates
        if a.asset_type != "stock" or not in_earnings_window(a.ticker, now.date())
    ]


def _stock_entry_ok(a: Analysis, now: datetime) -> bool:
    return (
        a.price > a.ema9 > a.ema21
        and a.adx_val > config.ENTRY_ADX_MIN
        and config.ENTRY_RSI_MIN <= a.rsi < config.ENTRY_RSI_MAX
        and (a.regime_ok or not config.REQUIRE_DAILY_UPTREND)
        and (a.atr_binding or not config.REQUIRE_ATR_STOP)
        and now.time() < config.STOCK_ENTRY_CUTOFF
    )


def _swing_entry_ok(a: Analysis, now: datetime) -> bool:
    return (
        config.SWING_ENABLED
        and a.swing_ok
        and now.time() < config.SWING_ENTRY_CUTOFF
    )


def _as_swing(a: Analysis) -> Analysis:
    """Re-level a stock Analysis for the swing rule: wide disaster stop,
    target 3R above (rarely reached — the rule exit does the work)."""
    risk = a.price * config.SWING_STOP_PCT
    return replace(
        a, strategy="swing",
        stop_loss=a.price - risk,
        take_profit=a.price + risk * config.TAKE_PROFIT_R_MULT,
        signals=["Swing: RSI(2) pullback entry"] + list(a.signals),
    )


def _crypto_entry_ok(a: Analysis, btc_uptrend: bool) -> bool:
    return (
        a.breakout_ok
        and a.regime_ok
        and a.volume_ratio >= config.CRYPTO_MIN_VOL_RATIO
        and (btc_uptrend or not config.CRYPTO_REQUIRE_BTC_UPTREND)
    )
