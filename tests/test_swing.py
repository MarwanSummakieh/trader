"""The RSI(2) swing entry family (2026-09-03): ledger strategy tagging and
migration, scanner selection + re-levelling, the rule/time exits in the
portfolio, analyzer features from completed daily bars, and the backtest
engine's mirror of all of it."""

import sqlite3
from dataclasses import replace
from datetime import datetime, time as dtime, timedelta

import numpy as np
import pandas as pd
import pytest
import pytz

import config
import src.scanner as scanner
from src.analyzer import analyze
from src.backtest import SignalData, SimParams, simulate
from src.ledger import Ledger
from src.portfolio import Portfolio
from src.scanner import get_buy_candidates
from tests.test_backtest_sim import make_frame
from tests.test_portfolio import make_analysis

ET = pytz.timezone("America/New_York")
T0930 = ET.localize(datetime(2026, 9, 2, 9, 40))     # Wed, inside the swing window
T1000 = ET.localize(datetime(2026, 9, 2, 10, 0))     # window closed


# ── Ledger ────────────────────────────────────────────────────────────────────

def test_strategy_persisted(ledger):
    t = ledger.open_trade(ticker="A", asset_type="stock", entry_price=100.0,
                          quantity=1.0, stop_loss=90.0, take_profit=130.0,
                          signals=[], strategy="swing")
    assert ledger.get_trade(t.id).strategy == "swing"
    t2 = ledger.open_trade(ticker="B", asset_type="stock", entry_price=100.0,
                           quantity=1.0, stop_loss=95.0, take_profit=115.0, signals=[])
    assert ledger.get_trade(t2.id).strategy == "momentum"


def test_pre_2026_09_ledger_is_migrated(tmp_path):
    """A ledger created before the strategy column existed must open, gain
    the column, and report its old rows as momentum trades."""
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
        asset_type TEXT NOT NULL, entry_time TEXT NOT NULL, exit_time TEXT,
        entry_price REAL NOT NULL, exit_price REAL, quantity REAL NOT NULL,
        stop_loss REAL NOT NULL, take_profit REAL NOT NULL, pnl REAL,
        pnl_pct REAL, exit_reason TEXT, signals TEXT)""")
    con.execute("""INSERT INTO trades (ticker, asset_type, entry_time, entry_price,
        quantity, stop_loss, take_profit, signals)
        VALUES ('OLD', 'stock', '2026-08-01T10:00:00-04:00', 100, 1, 95, 115, '[]')""")
    con.commit(); con.close()
    lg = Ledger(path)
    (t,) = lg.get_open_trades()
    assert t.ticker == "OLD" and t.strategy == "momentum"
    new = lg.open_trade(ticker="NEW", asset_type="stock", entry_price=1.0,
                        quantity=1.0, stop_loss=0.9, take_profit=1.3,
                        signals=[], strategy="swing")
    assert lg.get_trade(new.id).strategy == "swing"


# ── Scanner ───────────────────────────────────────────────────────────────────

def swinging(**kw):
    """Fails the momentum rule (ADX too low), passes the swing rule."""
    a = make_analysis(**kw)
    a.adx_val, a.rsi, a.regime_ok = 10.0, 40.0, True
    a.swing_ok, a.swing_exit_level = True, 101.0
    return a


def no_earnings(monkeypatch):
    monkeypatch.setattr(scanner, "in_earnings_window", lambda *a, **k: False)


def test_swing_candidate_relevelled(monkeypatch):
    no_earnings(monkeypatch)
    monkeypatch.setattr(config, "SWING_STOP_PCT", 0.10)
    monkeypatch.setattr(config, "TAKE_PROFIT_R_MULT", 3.0)
    (c,) = get_buy_candidates([swinging()], set(), now=T0930)
    assert c.strategy == "swing"
    assert c.stop_loss == pytest.approx(90.0)
    assert c.take_profit == pytest.approx(130.0)
    assert c.signals[0].startswith("Swing")


def test_swing_window_closes_at_cutoff(monkeypatch):
    no_earnings(monkeypatch)
    assert get_buy_candidates([swinging()], set(), now=T1000) == []


def test_swing_requires_flag_and_signal(monkeypatch):
    no_earnings(monkeypatch)
    monkeypatch.setattr(config, "SWING_ENABLED", False)
    assert get_buy_candidates([swinging()], set(), now=T0930) == []
    monkeypatch.setattr(config, "SWING_ENABLED", True)
    a = swinging(); a.swing_ok = False
    assert get_buy_candidates([a], set(), now=T0930) == []


def test_momentum_wins_when_both_fire(monkeypatch):
    no_earnings(monkeypatch)
    a = swinging()
    a.price, a.ema9, a.ema21, a.adx_val, a.rsi, a.atr_binding = 100.0, 99.0, 98.0, 35.0, 60.0, True
    (c,) = get_buy_candidates([a], set(), now=T0930)
    assert c.strategy == "momentum"
    assert c.stop_loss == 95.0                        # momentum levels kept


def test_swing_respects_existing_positions_and_earnings(monkeypatch):
    no_earnings(monkeypatch)
    assert get_buy_candidates([swinging()], {"TEST"}, now=T0930) == []
    monkeypatch.setattr(scanner, "in_earnings_window", lambda *a, **k: True)
    assert get_buy_candidates([swinging()], set(), now=T0930) == []


# ── Portfolio exits ───────────────────────────────────────────────────────────

def open_swing(portfolio, ticker="SW", price=100.0):
    a = make_analysis(ticker, price=price, stop=price * 0.9)
    a.strategy = "swing"
    return portfolio.open_position(a)


def next_sessions(n: int, hh=15, mm=50) -> datetime:
    day = np.busday_offset(datetime.now(ET).date(), n, roll="forward")
    d = pd.Timestamp(day).to_pydatetime()
    return ET.localize(datetime(d.year, d.month, d.day, hh, mm))


def test_swing_sized_smaller(portfolio):
    t = open_swing(portfolio)
    assert abs(t.entry_price * t.quantity - 1_000.0) < 1e-6   # 10% of 10k


def test_swing_exit_when_above_level_at_session_end(portfolio):
    open_swing(portfolio)
    closed = portfolio.check_exits({"SW": 103.0}, swing_levels={"SW": 102.0},
                                   now=next_sessions(1))
    assert [t.exit_reason for t in closed] == ["swing_exit"]
    assert closed[0].exit_price == 103.0


def test_swing_holds_below_level_or_before_window(portfolio):
    open_swing(portfolio)
    # below the level at the session end
    assert portfolio.check_exits({"SW": 101.0}, swing_levels={"SW": 102.0},
                                 now=next_sessions(1)) == []
    # above the level but mid-session
    assert portfolio.check_exits({"SW": 103.0}, swing_levels={"SW": 102.0},
                                 now=next_sessions(1, 12, 0)) == []
    # above the level but still the entry session
    assert portfolio.check_exits({"SW": 103.0}, swing_levels={"SW": 102.0},
                                 now=next_sessions(0)) == []
    # unknown level (no scan yet) -> no rule exit
    assert portfolio.check_exits({"SW": 103.0}, swing_levels={},
                                 now=next_sessions(1)) == []
    assert len(portfolio.open_trades) == 1


def test_swing_time_exit_after_max_hold(portfolio):
    open_swing(portfolio)
    assert portfolio.check_exits({"SW": 99.0}, swing_levels={"SW": 102.0},
                                 now=next_sessions(9)) == []
    closed = portfolio.check_exits({"SW": 99.0}, swing_levels={"SW": 102.0},
                                   now=next_sessions(10))
    assert [t.exit_reason for t in closed] == ["time_exit"]


def test_swing_stop_still_local(portfolio):
    open_swing(portfolio)
    closed = portfolio.check_exits({"SW": 89.0}, swing_levels={"SW": 102.0},
                                   now=next_sessions(1, 12, 0))
    assert [t.exit_reason for t in closed] == ["stop_loss"]


def test_momentum_trade_ignores_swing_levels(portfolio):
    portfolio.open_position(make_analysis("MO"))
    assert portfolio.check_exits({"MO": 103.0}, swing_levels={"MO": 102.0},
                                 now=next_sessions(1)) == []


def test_swing_rule_exit_goes_through_managed_broker(ledger, clean_config):
    from tests.test_broker import FakeManagedBroker
    broker = FakeManagedBroker()
    pf = Portfolio(ledger, broker=broker, starting_capital=10_000.0)
    open_swing(pf)
    closed = pf.check_exits({"SW": 103.0}, swing_levels={"SW": 102.0},
                            now=next_sessions(1))
    assert [t.exit_reason for t in closed] == ["swing_exit"]


# ── Analyzer ──────────────────────────────────────────────────────────────────

def test_analyzer_swing_features_use_completed_days_only(monkeypatch):
    monkeypatch.setattr(config, "SWING_RSI2_MAX", 5.0)
    now = datetime.now(ET).replace(second=0, microsecond=0)
    end = now - timedelta(minutes=now.minute % 5 + 5)
    idx = pd.date_range(end=end, periods=200, freq="5min", tz=ET)
    intra = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                          "Close": 100.0, "Volume": 1e6}, index=idx)
    # 260 completed sessions: a long uptrend, then two sharp down days (RSI2
    # collapses) — plus TODAY's in-progress row with an absurd close that
    # must be ignored.
    days = pd.bdate_range(end=now.date() - timedelta(days=1), periods=260)
    closes = np.linspace(80, 120, 260)
    closes[-2] = 112.0                       # two down days, still > SMA200 (~104.6)
    closes[-1] = 108.0
    daily = pd.DataFrame({"Open": closes, "High": closes + 1, "Low": closes - 1,
                          "Close": closes, "Volume": 5e6}, index=days)
    today = pd.DataFrame({"Open": 999.0, "High": 999.0, "Low": 999.0,
                          "Close": 999.0, "Volume": 5e6},
                         index=[pd.Timestamp(now.date())])
    a = analyze("TEST", intra, pd.concat([daily, today]), asset_type="stock")
    assert a is not None
    assert a.swing_ok is True
    assert a.swing_exit_level == pytest.approx(closes[-4:].mean())
    assert any("swing" in s.lower() for s in a.signals)
    # Not enough completed history -> fails closed
    b = analyze("TEST", intra, pd.concat([daily.tail(150), today]), asset_type="stock")
    assert b is not None and b.swing_ok is False and np.isnan(b.swing_exit_level)


# ── Backtest engine ───────────────────────────────────────────────────────────

def swing_frame():
    """Two sessions. Day 1: prior-close signal (rsi2=3, regime ok); entry at
    the 9:35 open. Day 2: 15:50 close above the exit level -> flat at the
    15:55 open."""
    bars = [
        (dtime(9, 30), 100, 101, 99, 100),
        (dtime(9, 35), 100, 101, 99, 100),
        (dtime(15, 50), 98, 99, 97, 98),
        (dtime(15, 55), 98, 99, 97, 98),
    ]
    f1 = make_frame(bars, day=1, signal_at=())
    f2 = make_frame([
        (dtime(9, 30), 99, 100, 98, 99),
        (dtime(15, 50), 104, 105, 103, 104),   # above level 101
        (dtime(15, 55), 104.5, 105, 104, 104.5),
    ], day=2, signal_at=())
    fields = {k: np.concatenate([getattr(f1, k), getattr(f2, k)])
              for k in ("ts", "date", "open", "high", "low", "close", "score", "rsi",
                        "vol_ratio", "trend_ok", "regime_ok", "stop_dist", "bb_pct",
                        "adx", "above_vwap", "macd_pos", "ema_aligned")}
    n = len(fields["ts"])
    date = fields["date"]
    new_day = np.r_[True, date[1:] != date[:-1]]
    day_idx = np.cumsum(new_day) - 1
    starts = np.maximum.accumulate(np.where(new_day, np.arange(n), 0))
    return SignalData(
        ticker="TEST", asset_type="stock",
        ts_pos={t: i for i, t in enumerate(fields["ts"])},
        atr_ok=np.zeros(n, dtype=bool),           # momentum gate closed
        bar_idx=np.arange(n) - starts, day_idx=day_idx,
        is_last_bar=np.r_[new_day[1:], True],
        swing_rsi2=np.where(day_idx == 0, 3.0, 50.0),
        swing_regime=np.ones(n, dtype=bool),
        swing_exit_level=np.full(n, 101.0),
        **fields,
    )


SWING = SimParams(fee_slippage_pct=0.0, starting_capital=10_000.0,
                  position_size_pct=0.15, swing_position_size_pct=0.10,
                  eod_close_stocks=False, swing_enabled=True, swing_rsi2_max=5.0,
                  swing_stop_pct=0.10, swing_max_hold_days=10, swing_entry_bars=6,
                  sec_fee_rate=0.0, finra_taf_per_share=0.0, finra_taf_cap=0.0)


def test_engine_swing_entry_and_rule_exit():
    res = simulate({"TEST": swing_frame()}, SWING)
    (t,) = res.trades
    assert t.strategy == "swing"
    assert t.entry_px == 100.0 and t.stop == pytest.approx(90.0)
    assert abs(t.margin - 1_000.0) < 1e-6           # swing sizing
    assert t.reason == "swing_exit" and t.exit_px == 104.5
    assert t.exit_ts.date() != t.entry_ts.date()


def test_engine_swing_time_exit():
    f = swing_frame()
    f.swing_exit_level[:] = 999.0                    # never above the level
    p = SimParams(**{**SWING.__dict__, "swing_max_hold_days": 1})
    (t,) = simulate({"TEST": f}, p).trades
    assert t.reason == "time_exit" and t.exit_px == 104.5


def test_engine_swing_respects_window_and_flag():
    f = swing_frame()
    p = SimParams(**{**SWING.__dict__, "swing_entry_bars": 1})
    # bar 0 is the signal bar (bar_idx 0 < 1 ok) -> still enters at bar 1
    assert len(simulate({"TEST": f}, p).trades) == 1
    f.bar_idx[f.day_idx == 0] = 6                     # every day-1 bar is past the window
    assert simulate({"TEST": f}, SWING).trades == []
    f = swing_frame()
    assert simulate({"TEST": f}, SimParams(**{**SWING.__dict__, "swing_enabled": False})).trades == []
