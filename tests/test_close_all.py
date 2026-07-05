"""The close_all wind-down tool: closes what it can price, skips the rest."""

import config
from close_all import wind_down


def test_wind_down_closes_priced_and_skips_unpriced(ledger, clean_config, monkeypatch):
    monkeypatch.setattr(config, "FEE_SLIPPAGE_PCT", 0.001)
    a = ledger.open_trade(ticker="AAA", asset_type="stock", entry_price=100.0,
                          quantity=10.0, stop_loss=95.0, take_profit=115.0,
                          signals=[])
    ledger.open_trade(ticker="BBB", asset_type="crypto", entry_price=50.0,
                      quantity=5.0, stop_loss=47.0, take_profit=56.0,
                      signals=[])

    closed, skipped = wind_down(ledger, {"AAA": 104.0})   # no price for BBB
    assert (closed, skipped) == (1, 1)

    done = ledger.get_trade(a.id)
    assert done.exit_reason == "manual_close"
    assert abs(done.exit_price - 104.0 * 0.999) < 1e-9    # sim exit slippage
    assert [t.ticker for t in ledger.get_open_trades()] == ["BBB"]  # retryable
