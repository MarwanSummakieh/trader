"""Risk and causality regressions that materially affect trading outcomes."""
from dataclasses import replace, fields
from datetime import datetime, time
import json

import numpy as np
import pandas as pd
import pytest

import config
from src.backtest import SimParams, simulate, compute_metrics, build_signal_frame
from src.ledger import Ledger
from src.risk import position_quantity
from src.validation import walk_forward, json_safe
from tests.test_backtest_sim import make_frame, PARAMS, ET
from tests.test_portfolio import make_analysis


def test_wider_stops_reduce_size_and_leverage_cannot_bypass_risk(portfolio, monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.005)
    monkeypatch.setattr(config, "LEVERAGE", 10.0)
    narrow = portfolio.open_position(make_analysis("NARROW", stop=95))
    wide = portfolio.open_position(make_analysis("WIDE", stop=90))
    assert narrow.quantity == pytest.approx(10)
    assert wide.quantity == pytest.approx(5)
    assert narrow.initial_risk * narrow.quantity == pytest.approx(50)
    assert wide.initial_risk * wide.quantity == pytest.approx(50)


def test_total_risk_budget_persists_across_restart(portfolio, ledger, monkeypatch):
    from src.portfolio import Portfolio
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.005)
    monkeypatch.setattr(config, "MAX_PORTFOLIO_RISK_PCT", 0.01)
    assert portfolio.open_position(make_analysis("A")) is not None
    assert portfolio.open_position(make_analysis("B")) is not None
    restarted = Portfolio(ledger, starting_capital=10000)
    assert restarted.open_risk == pytest.approx(100)
    assert restarted.open_position(make_analysis("C")) is None


def test_risk_budget_includes_slippage_and_fees(portfolio, monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.005)
    monkeypatch.setattr(config, "FEE_SLIPPAGE_PCT", 0.001)
    monkeypatch.setattr(config, "ALPACA_SEC_FEE_RATE", 0.0000278)
    monkeypatch.setattr(config, "ALPACA_FINRA_TAF_PER_SHARE", 0.000166)
    monkeypatch.setattr(config, "ALPACA_FINRA_TAF_CAP", 8.3)
    trade = portfolio.open_position(make_analysis())
    assert trade.initial_risk == pytest.approx(5.1)
    closed = portfolio.check_exits({"TEST": 95})[0]
    assert -closed.pnl <= 50 + 1e-8
    assert -closed.pnl > 49.99


def test_initial_risk_survives_trailing_and_config_changes(portfolio, monkeypatch):
    monkeypatch.setattr(config, "FEE_SLIPPAGE_PCT", 0.001)
    trade = portfolio.open_position(make_analysis())
    portfolio.check_exits({"TEST": 110})
    monkeypatch.setattr(config, "TAKE_PROFIT_R_MULT", 8)
    updated = portfolio.ledger.get_trade(trade.id)
    assert updated.stop_loss > trade.stop_loss
    assert portfolio._initial_risk(updated) == pytest.approx(5.1)


def test_legacy_ledger_migrates_without_changing_existing_trade(tmp_path):
    path = str(tmp_path / "legacy.db")
    ledger = Ledger(path)
    trade = ledger.open_trade("TEST", "stock", 100, 1, 95, 115, [])
    ledger._conn.execute("ALTER TABLE trades DROP COLUMN initial_risk")
    ledger._conn.commit()
    ledger._conn.close()
    migrated = Ledger(path)
    restored = migrated.get_trade(trade.id)
    assert restored.initial_risk is None
    assert restored.entry_price == 100
    assert restored.stop_loss == 95
    migrated._conn.close()


@pytest.mark.parametrize("entry,stop,target", [(float("nan"),95,115), (100,0,115),
    (100,101,115), (120,95,115), (100,95,float("inf"))])
def test_invalid_levels_never_reach_broker(portfolio, entry, stop, target):
    analysis = make_analysis()
    analysis.entry, analysis.stop_loss, analysis.take_profit = entry, stop, target
    assert portfolio.open_position(analysis) is None
    assert not portfolio.open_trades


def test_crypto_has_no_equity_regulatory_fees(portfolio, monkeypatch):
    monkeypatch.setattr(config, "ALPACA_SEC_FEE_RATE", 0.01)
    trade = portfolio.open_position(make_analysis("BTC-USD", asset_type="crypto"))
    closed = portfolio.check_exits({"BTC-USD": 115})[0]
    assert closed.pnl == pytest.approx(15 * trade.quantity)


def test_entry_bar_loss_is_risk_capped_in_backtest():
    frame = make_frame([(100,101,99,100), (100,101,94,100)])
    result = simulate({"TEST":frame}, replace(PARAMS, risk_per_trade_pct=0.005))
    assert result.trades[0].pnl == pytest.approx(-50)
    assert result.trades[0].r_multiple == pytest.approx(-1)


def test_gap_beyond_target_is_not_an_entry():
    frame = make_frame([(100,101,99,100), (116,117,115,116)])
    assert simulate({"TEST":frame}, PARAMS).trades == []


def test_gap_target_precedes_later_intrabar_stop():
    frame = make_frame([(100,101,99,100), (100,101,99,100), (116,117,90,100)])
    trade = simulate({"TEST":frame}, PARAMS).trades[0]
    assert trade.reason == "take_profit"
    assert trade.exit_px == 116


def test_intrabar_exit_cannot_release_slot_for_earlier_open():
    bars = [(100,101,99,100), (100,101,99,100), (100,101,90,100)]
    a = make_frame(bars, ticker="A", signal_at=(0,))
    b = make_frame(bars, ticker="B", signal_at=(1,))
    result = simulate({"A":a, "B":b}, replace(PARAMS, max_positions=1))
    assert [trade.ticker for trade in result.trades] == ["A"]


def test_final_equity_and_first_day_include_all_costs():
    frame = make_frame([(100,101,99,100), (100,101,99,100)])
    params = replace(PARAMS, fee_slippage_pct=0.001, sec_fee_rate=0.0000278)
    result = simulate({"TEST":frame}, params)
    trade = result.trades[0]
    metrics = compute_metrics(result)
    assert result.equity.iloc[-1] == pytest.approx(10000 + trade.pnl)
    assert metrics["worst_day_pct"] == pytest.approx(trade.pnl / 100)
    assert metrics["max_dd_pct"] == pytest.approx(trade.pnl / 100)
    assert trade.r == pytest.approx(5.1)
    assert trade.r_multiple == pytest.approx(trade.pnl / (trade.qty * 5.1))


def test_no_trade_profit_factor_is_not_infinity():
    frame = make_frame([(100,101,99,100), (100,101,99,100)], signal_at=())
    assert compute_metrics(simulate({"TEST":frame}, PARAMS))["profit_factor"] == 0
    assert compute_metrics(simulate({}, PARAMS))["trades"] == 0


def test_crypto_cap_is_replayed():
    frame = make_frame([(100,101,99,100), (100,101,99,100)])
    frame.asset_type = "crypto"
    assert simulate({"TEST":frame}, replace(PARAMS, crypto_max_capital_pct=0)).trades == []


def history():
    parts = []
    for day in range(1,9):
        # Training prefers 2R; the future window would prefer 3R.
        high = 112 if day <= 4 else 116
        parts.append(make_frame([(time(9,30),100,101,99,100),
                                 (time(9,35),100,high,99,105),
                                 (time(15,45),105,106,104,105)], day=day))
    values = {}
    for field in fields(parts[0]):
        name = field.name
        value = getattr(parts[0], name)
        if isinstance(value, np.ndarray):
            values[name] = np.concatenate([getattr(part,name) for part in parts])
    values["ts_pos"] = {t:i for i,t in enumerate(values["ts"])}
    return {"TEST":replace(parts[0], **values)}


def test_walk_forward_selects_on_past_not_future():
    candidates = [replace(PARAMS, take_profit_r_mult=2), PARAMS]
    report = walk_forward(history(), candidates, PARAMS, folds=1, min_trades=3)
    fold = report["folds"][0]
    assert fold["selected_params"]["take_profit_r_mult"] == 2
    assert fold["train_end_exclusive"] == fold["test_start"] == "2026-07-05"
    assert fold["baseline"]["return_pct"] > fold["selected"]["return_pct"]
    assert report["out_of_sample"]["selected"]["trades"] == 4
    json.dumps(json_safe(report), allow_nan=False)


def test_walk_forward_stays_cash_without_sufficient_evidence():
    report = walk_forward(history(), [PARAMS], PARAMS, folds=2, min_trades=100)
    assert all(fold["selected_params"] is None for fold in report["folds"])
    assert report["out_of_sample"]["selected"]["trades"] == 0
    assert report["out_of_sample"]["selected"]["return_pct"] == 0
    assert report["out_of_sample"]["baseline"]["trades"] > 0


def test_future_liquidity_does_not_change_past_signals():
    daily_index = pd.date_range("2026-01-01", periods=100, tz=ET)
    daily = pd.DataFrame({"Open":100., "High":101., "Low":99., "Close":100.,
                          "Volume":1_000_000.}, index=daily_index)
    index = pd.date_range("2026-03-01 09:30", periods=100, freq="5min", tz=ET)
    intra = pd.DataFrame({"Open":100., "High":101., "Low":99., "Close":100.,
                          "Volume":10000.}, index=index)
    first = build_signal_frame("TEST", "stock", intra, daily)
    daily.loc[daily.index >= "2026-03-02", "Volume"] = 1
    second = build_signal_frame("TEST", "stock", intra, daily)
    np.testing.assert_array_equal(first.score, second.score)


def test_live_analysis_ignores_current_daily_candle(monkeypatch):
    from src import analyzer
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return ET.localize(datetime(2026,4,10,11,5))
    monkeypatch.setattr(analyzer, "datetime", Clock)
    index = pd.date_range("2026-04-10 03:00", periods=97, freq="5min", tz=ET)
    intra = pd.DataFrame({"Open":100., "High":101., "Low":99., "Close":100.,
                          "Volume":10000.}, index=index)
    daily_index = pd.date_range("2026-01-01", "2026-04-10", tz=ET)
    daily = pd.DataFrame({"Open":90., "High":91., "Low":89., "Close":90.,
                          "Volume":1_000_000.}, index=daily_index)
    first = analyzer.analyze("TEST", intra, daily)
    daily.iloc[-1, daily.columns.get_loc("Close")] = 10000
    second = analyzer.analyze("TEST", intra, daily)
    assert first is not None and second is not None
    assert first.regime_ok == second.regime_ok
    assert first.adx_val == second.adx_val


def test_prior_daily_context_works_when_current_day_is_missing():
    daily_index = pd.date_range("2026-01-01", periods=60, tz=ET)
    daily = pd.DataFrame({"Open":90., "High":91., "Low":89., "Close":90.,
                          "Volume":1_000_000.}, index=daily_index)
    index = pd.date_range("2026-03-02 09:30", periods=60, freq="5min", tz=ET)
    intra = pd.DataFrame({"Open":100., "High":101., "Low":99., "Close":100.,
                          "Volume":10000.}, index=index)
    frame = build_signal_frame("TEST", "stock", intra, daily)
    assert frame.regime_ok.all()
    assert frame.score[-1] >= 0


def test_paper_and_backtest_share_cost_aware_sizing(portfolio, monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.005)
    monkeypatch.setattr(config, "MAX_PORTFOLIO_RISK_PCT", 0.025)
    monkeypatch.setattr(config, "FEE_SLIPPAGE_PCT", 0.001)
    paper = portfolio.open_position(make_analysis())
    frame = make_frame([(100,101,99,100), (100,101,99,100)])
    params = replace(PARAMS, risk_per_trade_pct=0.005, max_portfolio_risk_pct=0.025,
                     fee_slippage_pct=0.001)
    simulated = simulate({"TEST":frame}, params).trades[0]
    assert simulated.qty == pytest.approx(paper.quantity)
    assert simulated.r == pytest.approx(paper.initial_risk)
