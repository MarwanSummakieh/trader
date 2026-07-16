"""Broker execution layer: the Portfolio/broker contract (who decides exits,
who reports fills) and AlpacaBroker's REST behaviour against a stubbed
transport. No test here touches the network."""

from typing import Optional

import config
from src.broker import AlpacaBroker, Fill, SimBroker, make_broker
from src.portfolio import Portfolio
from tests.test_portfolio import make_analysis


# ── Fake managed broker for Portfolio integration ─────────────────────────────

class FakeManagedBroker:
    manages_exits = True
    name = "fake"

    def __init__(self):
        self.exit_fill: Optional[Fill] = None
        self.raised: list[tuple[str, float]] = []
        self.raise_ok = True
        self.open_fill: Optional[Fill] = "default"
        # True/False/None, or a list of them consumed one per call
        self.position_exists = True

    def open(self, ticker, qty, entry_hint, stop, tp):
        if self.open_fill == "default":
            return Fill(entry_hint, qty)
        return self.open_fill

    def close(self, ticker, price_hint, reason):
        return Fill(price_hint, 0.0, reason)

    def raise_stop(self, ticker, new_stop):
        self.raised.append((ticker, new_stop))
        return self.raise_ok

    def detect_exit(self, ticker):
        return self.exit_fill

    def has_position(self, ticker):
        if isinstance(self.position_exists, list):
            return self.position_exists.pop(0) if self.position_exists else False
        return self.position_exists


def managed_portfolio(ledger):
    broker = FakeManagedBroker()
    return Portfolio(ledger, broker=broker, starting_capital=10_000.0), broker


# ── Portfolio ↔ managed broker contract ───────────────────────────────────────

def test_broker_fill_price_and_qty_recorded(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    broker.open_fill = Fill(100.25, 14.0)          # broker rounded/slipped
    trade = pf.open_position(make_analysis())
    assert trade.entry_price == 100.25
    assert trade.quantity == 14.0


def test_broker_open_refusal_means_no_trade(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    broker.open_fill = None
    assert pf.open_position(make_analysis()) is None
    assert pf.open_trades == []
    assert pf.can_open()                           # no capital consumed


def test_server_side_exit_reconciled(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())
    broker.exit_fill = Fill(115.0, 15.0, "take_profit")
    closed = pf.check_exits({"TEST": 114.0})       # local price is irrelevant
    assert len(closed) == 1
    assert closed[0].exit_reason == "take_profit"
    assert closed[0].exit_price == 115.0


def test_local_price_does_not_close_managed_position(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())              # stop 95
    closed = pf.check_exits({"TEST": 90.0})        # below stop, server silent
    assert closed == []                            # server is authoritative
    assert len(pf.open_trades) == 1


def test_managed_stop_exit_relabelled_as_trail_after_trailing(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())              # R=5, trigger +7.5
    pf.check_exits({"TEST": 108.0})                # trail → stop 100.5, at broker too
    assert broker.raised == [("TEST", 100.5)]
    broker.exit_fill = Fill(100.4, 15.0, "stop_loss")
    closed = pf.check_exits({"TEST": 100.4})
    assert closed[0].exit_reason == "trail_stop"   # stop was above entry


def test_failed_broker_raise_keeps_ledger_stop(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())
    broker.raise_ok = False
    pf.check_exits({"TEST": 108.0})
    # Ledger must never claim a tighter stop than the broker actually holds.
    assert pf.open_trades[0].stop_loss == 95.0


def test_managed_eod_close_without_price(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis("AAPL"))
    closed = pf.eod_close_stocks({})               # no local price needed
    assert len(closed) == 1
    assert closed[0].exit_reason == "eod_close"


# ── Orphaned trades (broker never opened them) ────────────────────────────────

def test_orphan_takes_three_strikes_then_closes_locally(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())              # entry 100, tp 115
    broker.position_exists = False                 # broker: "no such position"

    assert pf.check_exits({"TEST": 116.0}) == []   # strike 1 — frozen
    assert pf.check_exits({"TEST": 116.0}) == []   # strike 2 — frozen
    closed = pf.check_exits({"TEST": 116.0})       # strike 3 — local management
    assert len(closed) == 1
    assert closed[0].exit_reason == "take_profit"
    assert closed[0].exit_price == 116.0           # sim fill (fees zeroed)


def test_orphan_stop_loss_fires_locally(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())              # stop 95
    broker.position_exists = False
    for _ in range(2):
        pf.check_exits({"TEST": 94.0})
    closed = pf.check_exits({"TEST": 94.0})
    assert closed[0].exit_reason == "stop_loss"


def test_real_position_is_never_orphan_closed(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())
    broker.position_exists = True                  # broker really holds it
    for _ in range(10):
        assert pf.check_exits({"TEST": 116.0}) == []
    assert len(pf.open_trades) == 1                # server stays authoritative


def test_transport_errors_do_not_advance_or_reset_strikes(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())
    # False, False, None (unknown), False → 3rd real confirmation on call 4
    broker.position_exists = [False, False, None, False]
    for _ in range(3):
        assert pf.check_exits({"TEST": 116.0}) == []
    closed = pf.check_exits({"TEST": 116.0})
    assert len(closed) == 1


def test_orphan_trailing_updates_ledger_despite_broker(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis())              # R=5, trigger +7.5
    broker.position_exists = False
    broker.raise_ok = False                        # broker refuses raises
    pf.check_exits({"TEST": 108.0})                # strikes 1-2: broker path,
    pf.check_exits({"TEST": 108.0})                # refused → stop stays put
    assert pf.open_trades[0].stop_loss == 95.0
    pf.check_exits({"TEST": 108.0})                # strike 3: sim-managed
    assert abs(pf.open_trades[0].stop_loss - 100.5) < 1e-9


def test_orphaned_stock_eod_closes_locally(ledger, clean_config):
    pf, broker = managed_portfolio(ledger)
    pf.open_position(make_analysis("AAPL"))
    broker.position_exists = False
    broker.exit_fill = None
    for _ in range(3):
        pf.check_exits({"AAPL": 101.0})            # accumulate strikes
    closed = pf.eod_close_stocks({"AAPL": 101.0})
    assert len(closed) == 1
    assert closed[0].exit_reason == "eod_close"
    assert closed[0].exit_price == 101.0           # sim fill, not broker


# ── make_broker factory ───────────────────────────────────────────────────────

def test_factory_defaults_to_sim(clean_config, monkeypatch):
    monkeypatch.setattr(config, "BROKER", "paper", raising=False)
    assert isinstance(make_broker(), SimBroker)


def test_factory_refuses_live_endpoint_without_flag(clean_config, monkeypatch):
    monkeypatch.setattr(config, "BROKER", "alpaca", raising=False)
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k", raising=False)
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "s", raising=False)
    monkeypatch.setattr(config, "ALPACA_BASE_URL",
                        "https://api.alpaca.markets", raising=False)
    monkeypatch.setattr(config, "ALPACA_ALLOW_LIVE", False, raising=False)
    import pytest
    with pytest.raises(SystemExit):
        make_broker()


def test_factory_builds_alpaca_paper(clean_config, monkeypatch):
    monkeypatch.setattr(config, "BROKER", "alpaca", raising=False)
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k", raising=False)
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "s", raising=False)
    monkeypatch.setattr(config, "ALPACA_BASE_URL",
                        "https://paper-api.alpaca.markets", raising=False)
    assert isinstance(make_broker(), AlpacaBroker)


# ── AlpacaBroker REST behaviour (stubbed transport) ───────────────────────────

def stubbed(responses):
    """AlpacaBroker whose _req replays canned (status, body) responses keyed
    by (method, path); records every call. Unknown request → (0, None)."""
    b = AlpacaBroker("k", "s")
    calls = []

    def _req(method, path, json=None, params=None):
        calls.append((method, path, json, params))
        return responses.get((method, path), (0, None))

    b._req = _req
    return b, calls


def test_alpaca_open_places_bracket_and_returns_fill():
    b, calls = stubbed({
        ("POST", "/v2/orders"): (200, {"id": "o1", "status": "accepted"}),
        ("GET", "/v2/orders/o1"): (200, {"id": "o1", "status": "filled",
                                         "filled_avg_price": "100.07",
                                         "filled_qty": "14"}),
    })
    fill = b.open("AAPL", 14.9, entry_hint=100.0, stop=95.0, tp=115.0)
    assert fill.price == 100.07 and fill.qty == 14.0
    payload = calls[0][2]
    assert payload["order_class"] == "bracket"
    assert payload["qty"] == "14"                  # fractional rounded down
    assert payload["stop_loss"] == {"stop_price": "95.00"}
    assert payload["take_profit"] == {"limit_price": "115.00"}


def test_alpaca_open_refuses_zero_share_and_crypto():
    b, calls = stubbed({})
    assert b.open("AAPL", 0.7, 1000.0, 950.0, 1150.0) is None
    assert b.open("BTC-USD", 5.0, 100.0, 95.0, 115.0) is None
    assert calls == []                             # never hit the API


def test_alpaca_raise_stop_patches_stop_leg():
    b, calls = stubbed({
        ("GET", "/v2/orders"): (200, [
            {"id": "tp1", "side": "sell", "type": "limit"},
            {"id": "sl1", "side": "sell", "type": "stop"},
        ]),
        ("PATCH", "/v2/orders/sl1"): (200, {"id": "sl2"}),
    })
    assert b.raise_stop("AAPL", 100.5) is True
    patch = [c for c in calls if c[0] == "PATCH"][0]
    assert patch[2] == {"stop_price": "100.50"}


def test_alpaca_detect_exit_maps_leg_types():
    stop_fill = {"side": "sell", "status": "filled", "type": "stop",
                 "filled_avg_price": "94.90", "filled_qty": "14"}
    b, _ = stubbed({
        ("GET", "/v2/positions/AAPL"): (404, None),
        ("GET", "/v2/orders"): (200, [stop_fill]),
    })
    fill = b.detect_exit("AAPL")
    assert fill.reason == "stop_loss" and fill.price == 94.90


def test_alpaca_detect_exit_open_position_and_errors_return_none():
    b, _ = stubbed({("GET", "/v2/positions/AAPL"): (200, {"qty": "14"})})
    assert b.detect_exit("AAPL") is None           # still open
    b, _ = stubbed({})                             # transport error (status 0)
    assert b.detect_exit("AAPL") is None           # unknown ≠ exited


def test_alpaca_has_position_three_states():
    b, _ = stubbed({("GET", "/v2/positions/AAPL"): (200, {"qty": "14"})})
    assert b.has_position("AAPL") is True
    b, _ = stubbed({("GET", "/v2/positions/AAPL"): (404, None)})
    assert b.has_position("AAPL") is False
    b, _ = stubbed({})                             # transport error
    assert b.has_position("AAPL") is None          # unknown, caller must not act


def test_ledger_reuse_across_restart(tmp_path, clean_config):
    """Reusing an existing ledger.db (e.g. the server's old volume): capital
    and open positions must carry over into a fresh Portfolio instance."""
    from src.ledger import Ledger

    db = str(tmp_path / "carried.db")
    old = Portfolio(Ledger(db), starting_capital=10_000.0)
    t = old.open_position(make_analysis("WIN"))
    old.check_exits({"WIN": 115.5})                # +$232.50 realized
    old.open_position(make_analysis("HELD"))      # left open

    fresh = Portfolio(Ledger(db), starting_capital=10_000.0)
    assert abs(fresh.capital - (10_000.0 + 15.5 * 15.0)) < 1e-6
    assert [t.ticker for t in fresh.open_trades] == ["HELD"]
    assert fresh.open_trades[0].stop_loss == 95.0  # levels survive restart


def test_alpaca_close_cancels_legs_then_liquidates():
    b, calls = stubbed({
        ("GET", "/v2/orders"): (200, [{"id": "sl1", "side": "sell", "type": "stop"}]),
        ("DELETE", "/v2/orders/sl1"): (200, {}),
        ("DELETE", "/v2/positions/AAPL"): (200, {"id": "mkt1"}),
        ("GET", "/v2/orders/mkt1"): (200, {"status": "filled",
                                           "filled_avg_price": "101.90",
                                           "filled_qty": "14"}),
    })
    fill = b.close("AAPL", 102.0, "eod_close")
    assert fill.price == 101.90 and fill.reason == "eod_close"
    methods = [(c[0], c[1]) for c in calls]
    assert methods.index(("DELETE", "/v2/orders/sl1")) < \
        methods.index(("DELETE", "/v2/positions/AAPL"))
