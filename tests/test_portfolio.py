"""Exit, trailing, sizing, and EOD logic — the paths that turn the validated
strategy into realized PnL. A silent regression here invalidates every
backtest number, so each branch is pinned."""

import config
from src.analyzer import Analysis


def make_analysis(ticker="TEST", price=100.0, stop=95.0, asset_type="stock"):
    tp = price + (price - stop) * config.TAKE_PROFIT_R_MULT
    return Analysis(
        ticker=ticker, asset_type=asset_type, price=price,
        score=70, score_breakdown={}, signals=[],
        entry=price, stop_loss=stop, take_profit=tp,
        rsi=60.0, macd_hist=0.1, volume_ratio=1.2, atr=2.0,
        ema9=99.0, ema21=98.0, vwap_val=99.5, adx_val=35.0,
        bb_pct=0.6, trend="bullish", regime_ok=True,
    )


# ── Sizing ────────────────────────────────────────────────────────────────────

def test_open_position_sizing(portfolio):
    trade = portfolio.open_position(make_analysis())
    assert trade is not None
    # 15% of 10k at leverage 1 → $1,500 margin → 15 shares at $100
    assert trade.entry_price == 100.0
    assert abs(trade.quantity - 15.0) < 1e-9
    assert abs(portfolio.capital_deployed - 1_500.0) < 1e-6


def test_entry_fill_includes_slippage(portfolio, monkeypatch):
    monkeypatch.setattr(config, "FEE_SLIPPAGE_PCT", 0.001)
    trade = portfolio.open_position(make_analysis())
    assert abs(trade.entry_price - 100.10) < 1e-9      # fills above signal


def test_leverage_scales_quantity_not_margin(portfolio, monkeypatch):
    monkeypatch.setattr(config, "LEVERAGE", 2.0)
    trade = portfolio.open_position(make_analysis())
    assert abs(trade.quantity - 30.0) < 1e-9           # 2x exposure
    assert abs(portfolio.capital_deployed - 1_500.0) < 1e-6  # same cash committed


def test_no_duplicate_ticker(portfolio):
    assert portfolio.open_position(make_analysis()) is not None
    assert portfolio.open_position(make_analysis()) is None


def test_max_positions_enforced(portfolio, monkeypatch):
    monkeypatch.setattr(config, "MAX_POSITIONS", 2)
    assert portfolio.open_position(make_analysis("A")) is not None
    assert portfolio.open_position(make_analysis("B")) is not None
    assert portfolio.can_open() is False
    assert portfolio.open_position(make_analysis("C")) is None


# ── Exits ─────────────────────────────────────────────────────────────────────

def test_stop_loss_exit(portfolio):
    portfolio.open_position(make_analysis())          # entry 100, stop 95
    closed = portfolio.check_exits({"TEST": 94.5})
    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].exit_price == 94.5
    assert abs(closed[0].pnl - (94.5 - 100.0) * 15.0) < 1e-6


def test_take_profit_exit(portfolio):
    portfolio.open_position(make_analysis())          # tp = 115
    closed = portfolio.check_exits({"TEST": 115.5})
    assert len(closed) == 1
    assert closed[0].exit_reason == "take_profit"


def test_exit_fill_includes_slippage(portfolio, monkeypatch):
    portfolio.open_position(make_analysis())
    monkeypatch.setattr(config, "FEE_SLIPPAGE_PCT", 0.001)
    closed = portfolio.check_exits({"TEST": 94.0})
    assert abs(closed[0].exit_price - 94.0 * 0.999) < 1e-9  # fills below observed


def test_no_price_no_exit(portfolio):
    portfolio.open_position(make_analysis())
    assert portfolio.check_exits({}) == []
    assert len(portfolio.open_trades) == 1


def test_between_stop_and_target_stays_open(portfolio):
    portfolio.open_position(make_analysis())
    assert portfolio.check_exits({"TEST": 100.0}) == []
    assert len(portfolio.open_trades) == 1


# ── Trailing stop ─────────────────────────────────────────────────────────────

def test_trailing_stop_raises_and_labels_trail(portfolio):
    portfolio.open_position(make_analysis())          # R = 5, trigger at +7.5
    assert portfolio.check_exits({"TEST": 108.0}) == []      # trail arms
    trade = portfolio.open_trades[0]
    assert abs(trade.stop_loss - (108.0 - 7.5)) < 1e-9       # 100.5, above entry

    closed = portfolio.check_exits({"TEST": 100.4})          # falls through trail
    assert len(closed) == 1
    assert closed[0].exit_reason == "trail_stop"             # not "stop_loss"
    assert closed[0].pnl > 0                                 # locked-in gain


def test_trailing_stop_never_lowers(portfolio):
    portfolio.open_position(make_analysis())
    portfolio.check_exits({"TEST": 110.0})            # stop → 102.5
    portfolio.check_exits({"TEST": 108.5})            # would imply 101.0 — ignored
    assert abs(portfolio.open_trades[0].stop_loss - 102.5) < 1e-9


def test_below_trigger_no_trail(portfolio):
    portfolio.open_position(make_analysis())
    portfolio.check_exits({"TEST": 106.0})            # +6 < +7.5 trigger
    assert portfolio.open_trades[0].stop_loss == 95.0


# ── Margin call ───────────────────────────────────────────────────────────────

def test_margin_call_binds_before_stop_when_levered(portfolio, monkeypatch):
    monkeypatch.setattr(config, "LEVERAGE", 10.0)
    # mc price = 100 * (1 - 0.9/10) = 91 — but stop 95 is higher, so the stop
    # still wins; use a wide stop to force the margin call to bind first.
    portfolio.open_position(make_analysis(stop=85.0))
    monkeypatch.setattr(config, "MARGIN_CALL_LOSS", 0.5)     # mc price = 95
    closed = portfolio.check_exits({"TEST": 94.0})
    assert len(closed) == 1
    assert closed[0].exit_reason == "margin_call"


# ── EOD liquidation ───────────────────────────────────────────────────────────

def test_eod_closes_stocks_only(portfolio):
    portfolio.open_position(make_analysis("AAPL", asset_type="stock"))
    portfolio.open_position(make_analysis("BTC-USD", asset_type="crypto"))
    closed = portfolio.eod_close_stocks({"AAPL": 101.0, "BTC-USD": 101.0})
    assert [t.ticker for t in closed] == ["AAPL"]
    assert closed[0].exit_reason == "eod_close"
    assert [t.ticker for t in portfolio.open_trades] == ["BTC-USD"]


def test_eod_missing_price_leaves_position_open(portfolio):
    portfolio.open_position(make_analysis("AAPL"))
    assert portfolio.eod_close_stocks({}) == []
    assert len(portfolio.open_trades) == 1            # retried, never faked


# ── Capital accounting ────────────────────────────────────────────────────────

def test_capital_reflects_realized_pnl(portfolio):
    portfolio.open_position(make_analysis())
    portfolio.check_exits({"TEST": 115.5})            # +15.5 * 15 shares
    assert abs(portfolio.capital - (10_000.0 + 15.5 * 15.0)) < 1e-6
    assert portfolio.capital_deployed == 0.0
