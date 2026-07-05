"""The entry rule is the validated edge — every clause gets a test."""

from datetime import datetime

import pytz

import config
import src.scanner as scanner
from src.scanner import get_buy_candidates
from tests.test_portfolio import make_analysis

ET = pytz.timezone("America/New_York")
MORNING = ET.localize(datetime(2026, 7, 1, 10, 30))
AFTERNOON = ET.localize(datetime(2026, 7, 1, 13, 0))


def passing(**kw):
    """An Analysis that satisfies every entry clause."""
    a = make_analysis(**kw)
    a.price, a.ema9, a.ema21 = 100.0, 99.0, 98.0     # EMA stack aligned
    a.adx_val, a.rsi, a.regime_ok = 35.0, 60.0, True
    return a


def no_earnings(monkeypatch):
    monkeypatch.setattr(scanner, "in_earnings_window", lambda *a, **k: False)


def test_passing_candidate_selected(monkeypatch):
    no_earnings(monkeypatch)
    assert len(get_buy_candidates([passing()], set(), now=MORNING)) == 1


def test_ema_alignment_required(monkeypatch):
    no_earnings(monkeypatch)
    a = passing()
    a.ema9 = 97.0                                    # price > ema9 > ema21 broken
    assert get_buy_candidates([a], set(), now=MORNING) == []


def test_adx_minimum(monkeypatch):
    no_earnings(monkeypatch)
    a = passing()
    a.adx_val = config.ENTRY_ADX_MIN                 # must be strictly above
    assert get_buy_candidates([a], set(), now=MORNING) == []


def test_rsi_band_half_open(monkeypatch):
    no_earnings(monkeypatch)
    lo, hi = config.ENTRY_RSI_MIN, config.ENTRY_RSI_MAX
    for rsi, ok in [(lo - 1, False), (lo, True), (hi - 0.01, True), (hi, False)]:
        a = passing()
        a.rsi = rsi
        got = get_buy_candidates([a], set(), now=MORNING)
        assert bool(got) is ok, f"rsi={rsi}"


def test_regime_gate(monkeypatch):
    no_earnings(monkeypatch)
    a = passing()
    a.regime_ok = False
    assert get_buy_candidates([a], set(), now=MORNING) == []
    monkeypatch.setattr(config, "REQUIRE_DAILY_UPTREND", False)
    assert len(get_buy_candidates([a], set(), now=MORNING)) == 1


def test_stock_cutoff_blocks_afternoon_entries(monkeypatch):
    no_earnings(monkeypatch)
    assert get_buy_candidates([passing()], set(), now=AFTERNOON) == []
    crypto = passing(ticker="BTC-USD", asset_type="crypto")
    assert len(get_buy_candidates([crypto], set(), now=AFTERNOON)) == 1


def test_existing_position_excluded(monkeypatch):
    no_earnings(monkeypatch)
    assert get_buy_candidates([passing()], {"TEST"}, now=MORNING) == []


def test_earnings_window_blocks_stocks_not_crypto(monkeypatch):
    monkeypatch.setattr(scanner, "in_earnings_window", lambda *a, **k: True)
    stock, crypto = passing(), passing(ticker="BTC-USD", asset_type="crypto")
    got = get_buy_candidates([stock, crypto], set(), now=MORNING)
    assert [a.ticker for a in got] == ["BTC-USD"]
    monkeypatch.setattr(config, "EARNINGS_FILTER", False)
    assert len(get_buy_candidates([stock, crypto], set(), now=MORNING)) == 2
