"""
Alpaca US-equity trading costs.

Alpaca charges $0 commission on US stocks/ETFs. The real costs are:
  1. the bid/ask spread on market orders — modeled per side by
     config.FEE_SLIPPAGE_PCT in the fill price (entry higher, exit lower);
  2. small regulatory pass-through fees on SELLS only — SEC fee (a fraction
     of proceeds) plus FINRA TAF (per share, capped). Buys carry neither.

Fees are cents per trade for this universe, but modeled so backtest, paper
and live PnL are honest net of every real cost. Both the live/paper close
path (portfolio) and the backtest simulator route sells through this.
"""

from __future__ import annotations

import config


def sell_regulatory_fee(
    price: float,
    qty: float,
    sec_rate: float | None = None,
    taf_per_share: float | None = None,
    taf_cap: float | None = None,
) -> float:
    """Total SEC + FINRA TAF charged when selling `qty` shares at `price`.

    Rates default to config; the backtest passes them explicitly so a sweep
    is self-contained and tests can pin them.
    """
    if qty <= 0 or price <= 0:
        return 0.0
    sec_rate = config.ALPACA_SEC_FEE_RATE if sec_rate is None else sec_rate
    taf_per_share = config.ALPACA_FINRA_TAF_PER_SHARE if taf_per_share is None else taf_per_share
    taf_cap = config.ALPACA_FINRA_TAF_CAP if taf_cap is None else taf_cap

    sec = price * qty * sec_rate
    taf = min(qty * taf_per_share, taf_cap)
    return sec + taf
