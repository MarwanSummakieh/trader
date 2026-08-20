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
    """An Analysis that satisfies every STOCK entry clause."""
    a = make_analysis(**kw)
    a.price, a.ema9, a.ema21 = 100.0, 99.0, 98.0     # EMA stack aligned
    a.adx_val, a.rsi, a.regime_ok = 35.0, 60.0, True
    a.atr_binding = True                             # vol gate satisfied
    return a


def passing_crypto(ticker="BTC-USD", **kw):
    """An Analysis that satisfies every CRYPTO entry clause. Defaults to
    BTC itself so the BTC regime gate is satisfied by its own regime_ok."""
    a = make_analysis(ticker=ticker, asset_type="crypto", **kw)
    a.breakout_ok, a.regime_ok, a.volume_ratio = True, True, 1.5
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


def test_atr_stop_gate(monkeypatch):
    no_earnings(monkeypatch)
    a = passing()
    a.atr_binding = False                            # stop pinned at the floor
    assert get_buy_candidates([a], set(), now=MORNING) == []
    monkeypatch.setattr(config, "REQUIRE_ATR_STOP", False)
    assert len(get_buy_candidates([a], set(), now=MORNING)) == 1


def test_atr_gate_does_not_apply_to_crypto(monkeypatch):
    """passing_crypto() leaves atr_binding at its False default — the stock
    volatility gate must never block a crypto candidate."""
    no_earnings(monkeypatch)
    a = passing_crypto()
    assert a.atr_binding is False
    assert len(get_buy_candidates([a], set(), now=MORNING)) == 1


def test_stock_cutoff_blocks_afternoon_entries(monkeypatch):
    no_earnings(monkeypatch)
    assert get_buy_candidates([passing()], set(), now=AFTERNOON) == []
    crypto = passing_crypto()
    assert len(get_buy_candidates([crypto], set(), now=AFTERNOON)) == 1


def test_existing_position_excluded(monkeypatch):
    no_earnings(monkeypatch)
    assert get_buy_candidates([passing()], {"TEST"}, now=MORNING) == []


def test_earnings_window_blocks_stocks_not_crypto(monkeypatch):
    monkeypatch.setattr(scanner, "in_earnings_window", lambda *a, **k: True)
    stock, crypto = passing(), passing_crypto()
    got = get_buy_candidates([stock, crypto], set(), now=MORNING)
    assert [a.ticker for a in got] == ["BTC-USD"]
    monkeypatch.setattr(config, "EARNINGS_FILTER", False)
    assert len(get_buy_candidates([stock, crypto], set(), now=MORNING)) == 2


# ── Crypto rule: regime-gated 8h breakout (every clause gets a test) ─────────

def test_crypto_breakout_required(monkeypatch):
    no_earnings(monkeypatch)
    a = passing_crypto()
    a.breakout_ok = False
    assert get_buy_candidates([a], set(), now=MORNING) == []


def test_crypto_own_regime_required_and_not_toggleable(monkeypatch):
    no_earnings(monkeypatch)
    a = passing_crypto()
    a.regime_ok = False
    assert get_buy_candidates([a], set(), now=MORNING) == []
    # REQUIRE_DAILY_UPTREND is a stock knob — the crypto gates ARE the
    # strategy, so disabling it must NOT open the crypto regime gate.
    monkeypatch.setattr(config, "REQUIRE_DAILY_UPTREND", False)
    assert get_buy_candidates([a], set(), now=MORNING) == []


def test_crypto_volume_floor(monkeypatch):
    no_earnings(monkeypatch)
    a = passing_crypto()
    a.volume_ratio = config.CRYPTO_MIN_VOL_RATIO - 0.01
    assert get_buy_candidates([a], set(), now=MORNING) == []
    a.volume_ratio = config.CRYPTO_MIN_VOL_RATIO     # floor is inclusive
    assert len(get_buy_candidates([a], set(), now=MORNING)) == 1


def test_crypto_btc_gate_fails_closed(monkeypatch):
    no_earnings(monkeypatch)
    alt = passing_crypto(ticker="ETH-USD")
    # No BTC analysis in the scan → gate fails closed
    assert get_buy_candidates([alt], set(), now=MORNING) == []
    # BTC present but below its daily EMA50 → still closed
    btc = passing_crypto()
    btc.regime_ok = False
    assert get_buy_candidates([alt, btc], set(), now=MORNING) == []
    # BTC in an uptrend → open (BTC itself fails its own regime clause)
    btc.regime_ok = True
    got = get_buy_candidates([alt, btc], set(), now=MORNING)
    assert {a.ticker for a in got} == {"ETH-USD", "BTC-USD"}
    # Gate disabled via config → alt passes without BTC data
    monkeypatch.setattr(config, "CRYPTO_REQUIRE_BTC_UPTREND", False)
    assert len(get_buy_candidates([alt], set(), now=MORNING)) == 1


def test_crypto_ignores_stock_momentum_clauses(monkeypatch):
    """The crypto rule is independent: bad RSI/ADX/EMA-stack must not block."""
    no_earnings(monkeypatch)
    a = passing_crypto()
    a.rsi, a.adx_val, a.ema9 = 90.0, 5.0, 200.0
    assert len(get_buy_candidates([a], set(), now=MORNING)) == 1
