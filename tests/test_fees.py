"""Alpaca cost model: the sell-side regulatory fee, and its application in
both the live/paper close path and the backtest simulator."""

import config
from src.fees import sell_regulatory_fee
from tests.test_portfolio import make_analysis


# ── The fee function ──────────────────────────────────────────────────────────

def test_sec_plus_taf():
    # 100 shares @ $50 = $5,000 proceeds
    fee = sell_regulatory_fee(50.0, 100.0, sec_rate=0.0000278,
                              taf_per_share=0.000166, taf_cap=8.30)
    assert abs(fee - (5000 * 0.0000278 + 100 * 0.000166)) < 1e-12  # 0.139 + 0.0166


def test_taf_is_capped():
    fee = sell_regulatory_fee(1.0, 1_000_000.0, sec_rate=0.0,
                              taf_per_share=0.000166, taf_cap=8.30)
    assert fee == 8.30                       # 1M * 0.000166 = 166, capped to 8.30


def test_zero_and_negative_guards():
    assert sell_regulatory_fee(0.0, 100.0) == 0.0
    assert sell_regulatory_fee(50.0, 0.0) == 0.0
    assert sell_regulatory_fee(50.0, -5.0) == 0.0


def test_defaults_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_SEC_FEE_RATE", 0.001)
    monkeypatch.setattr(config, "ALPACA_FINRA_TAF_PER_SHARE", 0.0)
    monkeypatch.setattr(config, "ALPACA_FINRA_TAF_CAP", 100.0)
    assert abs(sell_regulatory_fee(100.0, 10.0) - (1000 * 0.001)) < 1e-12


# ── Applied on close (portfolio) ──────────────────────────────────────────────

def test_close_nets_regulatory_fee(portfolio, monkeypatch):
    # Buys are fee-free; only the sell is charged. Use a pure SEC rate for a
    # clean expected value.
    monkeypatch.setattr(config, "ALPACA_SEC_FEE_RATE", 0.001)     # 0.1% of proceeds
    monkeypatch.setattr(config, "ALPACA_FINRA_TAF_PER_SHARE", 0.0)
    portfolio.open_position(make_analysis())          # 15 shares @ $100, no buy fee
    closed = portfolio.check_exits({"TEST": 116.0})   # take-profit; sell 15 @ $116
    fee = 116.0 * 15.0 * 0.001                        # $1.74
    # exit recorded net: 116 - fee/qty
    assert abs(closed[0].exit_price - (116.0 - fee / 15.0)) < 1e-9
    assert abs(closed[0].pnl - ((116.0 - fee / 15.0 - 100.0) * 15.0)) < 1e-6


def test_no_fee_when_rates_zero(portfolio):
    # clean_config zeros the fee rates → close price is the raw fill.
    portfolio.open_position(make_analysis())
    closed = portfolio.check_exits({"TEST": 116.0})   # take-profit
    assert closed[0].exit_price == 116.0


# ── Applied in the backtest ───────────────────────────────────────────────────

def test_backtest_pnl_nets_fees():
    from src.backtest import BTTrade
    from src.fees import sell_regulatory_fee

    t = BTTrade(ticker="X", asset_type="stock", score=70, entry_ts=None,
                entry_px=100.0, qty=10.0, stop=95.0, tp=115.0, r=5.0)
    t.exit_px = 110.0
    gross = (110.0 - 100.0) * 10.0
    t.fees = sell_regulatory_fee(110.0, 10.0, sec_rate=0.0000278,
                                 taf_per_share=0.000166, taf_cap=8.30)
    assert t.fees > 0
    assert abs(t.pnl - (gross - t.fees)) < 1e-12
