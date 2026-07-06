"""Pin the backtest simulator's fidelity rules (documented in src/backtest.py):
next-bar-open entries, stop-before-target pessimism, entry-bar exit skip,
same-session signals, EOD liquidation. The validated strategy numbers are
only as trustworthy as these behaviours."""

from datetime import datetime, time as dtime

import numpy as np
import pytz

from src.backtest import SignalData, SimParams, simulate

ET = pytz.timezone("America/New_York")


def make_frame(bars, day=1, signal_at=(0,), stop_dist=5.0, ticker="TEST"):
    """bars: list of (open, high, low, close); 5m spacing from 9:30 ET unless
    a (time, o, h, l, c) tuple gives an explicit bar time."""
    ts, ohlc = [], []
    for k, b in enumerate(bars):
        if len(b) == 5:
            t, o, h, l, c = b
        else:
            o, h, l, c = b
            t = dtime(9, 30 + 5 * k) if 30 + 5 * k < 60 else dtime(10, (30 + 5 * k) % 60)
        ts.append(ET.localize(datetime(2026, 7, day, t.hour, t.minute)))
        ohlc.append((o, h, l, c))
    n = len(ts)
    o, h, l, c = (np.array(x, dtype=float) for x in zip(*ohlc))
    sig = np.zeros(n, dtype=bool)
    for i in signal_at:
        sig[i] = True
    return SignalData(
        ticker=ticker, asset_type="stock",
        ts=np.array(ts, dtype=object), ts_pos={t: i for i, t in enumerate(ts)},
        date=np.array([t.date() for t in ts], dtype=object),
        open=o, high=h, low=l, close=c,
        score=np.full(n, 70), rsi=np.full(n, 60.0), vol_ratio=np.ones(n),
        trend_ok=np.ones(n, dtype=bool), regime_ok=np.ones(n, dtype=bool),
        stop_dist=np.full(n, stop_dist), bb_pct=np.full(n, 0.5),
        adx=np.full(n, 35.0), above_vwap=np.ones(n, dtype=bool),
        macd_pos=np.ones(n, dtype=bool), ema_aligned=np.ones(n, dtype=bool),
        signal=sig,
    )


PARAMS = SimParams(fee_slippage_pct=0.0, starting_capital=10_000.0,
                   position_size_pct=0.15,
                   sec_fee_rate=0.0, finra_taf_per_share=0.0, finra_taf_cap=0.0)


def run(frame):
    return simulate({frame.ticker: frame}, PARAMS)


def test_entry_fills_at_next_bar_open():
    f = make_frame([(100, 101, 99, 100),      # signal bar
                    (102, 103, 101, 102),     # entry bar — fills at open 102
                    (103, 104, 102, 103)])
    res = run(f)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_px == 102.0
    assert t.stop == 95.0                     # close[signal] - stop_dist
    assert t.tp == 115.0                      # close[signal] + 3R


def test_stop_assumed_before_target_same_bar():
    f = make_frame([(100, 101, 99, 100),
                    (100, 101, 99, 100),      # entry at 100
                    (100, 116, 94, 110)])     # touches both stop 95 and tp 115
    res = run(f)
    t = res.trades[0]
    assert t.reason == "stop_loss"
    assert t.exit_px == 95.0                  # pessimistic fill at the stop
    assert abs(t.pnl - (95.0 - 100.0) * 15.0) < 1e-9   # 15% of 10k / $100


def test_entry_bar_itself_not_exit_checked():
    f = make_frame([(100, 101, 99, 100),
                    (100, 101, 90, 100)])     # entry bar low pierces the stop
    res = run(f)
    t = res.trades[0]
    assert t.reason == "backtest_end"         # survived: first check is next bar


def test_gap_through_stop_skips_entry():
    f = make_frame([(100, 101, 99, 100),
                    (94, 95, 93, 94),         # opens below stop 95 — no entry
                    (94, 95, 93, 94)])
    res = run(f)
    assert res.trades == []


def test_signal_must_be_same_session():
    f1 = make_frame([(100, 101, 99, 100), (100, 101, 99, 100)],
                    day=1, signal_at=(1,))    # signal on day 1's last bar
    f2 = make_frame([(100, 101, 99, 100), (100, 101, 99, 100)],
                    day=2, signal_at=())
    merged = make_frame([(100, 101, 99, 100), (100, 101, 99, 100)], day=1,
                        signal_at=(1,))
    # stitch day 2 onto day 1's frame
    import dataclasses
    both = dataclasses.replace(
        merged,
        ts=np.concatenate([f1.ts, f2.ts]),
        ts_pos={t: i for i, t in enumerate(np.concatenate([f1.ts, f2.ts]))},
        date=np.concatenate([f1.date, f2.date]),
        open=np.concatenate([f1.open, f2.open]),
        high=np.concatenate([f1.high, f2.high]),
        low=np.concatenate([f1.low, f2.low]),
        close=np.concatenate([f1.close, f2.close]),
        score=np.concatenate([f1.score, f2.score]),
        rsi=np.concatenate([f1.rsi, f2.rsi]),
        vol_ratio=np.concatenate([f1.vol_ratio, f2.vol_ratio]),
        trend_ok=np.concatenate([f1.trend_ok, f2.trend_ok]),
        regime_ok=np.concatenate([f1.regime_ok, f2.regime_ok]),
        stop_dist=np.concatenate([f1.stop_dist, f2.stop_dist]),
        bb_pct=np.concatenate([f1.bb_pct, f2.bb_pct]),
        adx=np.concatenate([f1.adx, f2.adx]),
        above_vwap=np.concatenate([f1.above_vwap, f2.above_vwap]),
        macd_pos=np.concatenate([f1.macd_pos, f2.macd_pos]),
        ema_aligned=np.concatenate([f1.ema_aligned, f2.ema_aligned]),
        signal=np.concatenate([f1.signal, f2.signal]),
    )
    res = simulate({"TEST": both}, PARAMS)
    assert res.trades == []                   # overnight signal never fills


def test_eod_liquidation_at_1545():
    f = make_frame([
        (dtime(9, 30), 100, 101, 99, 100),    # signal
        (dtime(9, 35), 100, 101, 99, 100),    # entry at 100
        (dtime(10, 0), 101, 102, 100, 101),   # drifts, no exit
        (dtime(15, 45), 102, 103, 101, 102),  # forced flat at the open
    ], signal_at=(0,))
    res = run(f)
    t = res.trades[0]
    assert t.reason == "eod_close"
    assert t.exit_px == 102.0


def test_no_entries_after_cutoff():
    f = make_frame([
        (dtime(13, 0), 100, 101, 99, 100),    # signal after 12:00 cutoff
        (dtime(13, 5), 100, 101, 99, 100),
    ], signal_at=(0,))
    res = run(f)
    assert res.trades == []
