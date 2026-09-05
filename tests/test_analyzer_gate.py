"""The stock volatility gate compares 1.5*ATR(5m) against ATR_GATE_MULT of
the STOP_LOSS_PCT floor. Pin the arithmetic on synthetic bars so a change to
either knob cannot silently move the entry threshold."""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import pytz

import config
from src.analyzer import analyze

ET = pytz.timezone("America/New_York")


def frames(bar_range_pct: float, price: float = 100.0):
    """200 completed 5m bars with a constant high-low range (so ATR ==
    range) ending just now, plus 60 flat daily bars."""
    now = datetime.now(ET).replace(second=0, microsecond=0)
    end = now - timedelta(minutes=now.minute % 5 + 5)   # last completed bar
    idx = pd.date_range(end=end, periods=200, freq="5min", tz=ET)
    rng = price * bar_range_pct
    intra = pd.DataFrame({
        "Open": price, "High": price + rng / 2, "Low": price - rng / 2,
        "Close": price, "Volume": 1_000_000.0,
    }, index=idx)
    didx = pd.date_range(end=now.date(), periods=60, freq="D", tz=ET)
    daily = pd.DataFrame({
        "Open": price, "High": price + 1, "Low": price - 1,
        "Close": price, "Volume": 5_000_000.0,
    }, index=didx)
    return intra, daily


@pytest.mark.parametrize("mult,bar_range_pct,expected", [
    # floor = 2% of price; gate line = mult * floor / 1.5 in ATR terms
    (1.0, 0.0140, True),    # 1.5*1.4% = 2.1% > 2.0%
    (1.0, 0.0120, False),   # 1.5*1.2% = 1.8% < 2.0%
    (0.7, 0.0120, True),    # 1.8% > 0.7*2.0% = 1.4%
    (0.7, 0.0090, False),   # 1.35% < 1.4%
])
def test_gate_threshold_scales_with_mult(monkeypatch, mult, bar_range_pct, expected):
    monkeypatch.setattr(config, "STOP_LOSS_PCT", 0.02)
    monkeypatch.setattr(config, "ATR_GATE_MULT", mult)
    intra, daily = frames(bar_range_pct)
    a = analyze("TEST", intra, daily, asset_type="stock")
    assert a is not None
    assert a.atr_binding is expected
    # The stop itself is unchanged by the gate: still max(1.5*ATR, floor)
    assert a.stop_loss == pytest.approx(
        a.price - max(1.5 * a.atr, a.price * 0.02), rel=1e-6)
