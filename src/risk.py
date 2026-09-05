"""Shared cost-aware position sizing for execution and historical simulation."""
import math


def position_quantity(*, capital, available, entry, stop, target, leverage,
                      position_pct, risk_pct, portfolio_risk_pct, open_risk,
                      slippage, sell_fee_per_share=0.0):
    """Cap notional AND estimated stop loss; gaps can still exceed this budget.

    open_risk reserves initial risk for each open position, even after trailing.
    A zero risk limit disables that cap for explicit research comparisons.
    """
    values = (capital, available, entry, stop, target, leverage, position_pct,
              risk_pct, portfolio_risk_pct, open_risk, slippage, sell_fee_per_share)
    if not all(math.isfinite(v) for v in values):
        return 0.0
    if not (capital > 0 and available > 0 and 0 < stop < entry < target
            and leverage > 0 and 0 < position_pct <= 1 and 0 <= slippage < 1
            and 0 <= risk_pct <= 1 and 0 <= portfolio_risk_pct <= 1
            and open_risk >= 0 and sell_fee_per_share >= 0):
        return 0.0
    loss = entry - stop * (1 - slippage) + sell_fee_per_share
    quantity = min(capital * position_pct, available * 0.95) * leverage / entry
    if risk_pct:
        quantity = min(quantity, capital * risk_pct / loss)
    if portfolio_risk_pct:
        quantity = min(quantity, max(0.0, capital * portfolio_risk_pct - open_risk) / loss)
    return max(0.0, quantity)
