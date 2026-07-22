"""Dashboard server: ONE process serves BOTH bot instances — `/` + `/api/*`
reads the stock ledger, `/crypto` + `/api/crypto/*` reads the crypto ledger.
A cross-wired router would show one instance's money under the other's page,
so these tests pin each router to its own DB and starting capital."""

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

import config
from src.ledger import Ledger


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """Import server.py against two fresh temp ledgers."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "stocks.db"))
    monkeypatch.setenv("CRYPTO_DB_PATH", str(tmp_path / "crypto.db"))
    monkeypatch.setenv("STARTING_CAPITAL", "10000")
    monkeypatch.setenv("CRYPTO_STARTING_CAPITAL", "1000")
    importlib.reload(config)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    # Positions/status quote open trades — never let tests touch the network.
    monkeypatch.setattr(
        server, "get_current_prices", lambda tickers: {t: 100.0 for t in tickers}
    )
    with TestClient(server.app) as client:
        yield server, client
    # Undo BEFORE the monkeypatch fixture's own teardown so config is
    # rebuilt from the real environment for later test files.
    sys.modules.pop("server", None)
    monkeypatch.undo()
    importlib.reload(config)


def _closed_trade(db_path: str, ticker: str, asset_type: str,
                  entry: float, exit_: float, qty: float):
    led = Ledger(db_path)
    t = led.open_trade(ticker=ticker, asset_type=asset_type, entry_price=entry,
                       quantity=qty, stop_loss=entry * 0.98,
                       take_profit=entry * 1.06, signals=[])
    led.close_trade(t.id, exit_, "take_profit")


def test_instances_read_separate_ledgers(srv):
    server, client = srv
    _closed_trade(config.DB_PATH, "NVDA", "stock", 100.0, 110.0, 10)   # +$100
    _closed_trade(config.CRYPTO_DB_PATH, "SOL-USD", "crypto", 50.0, 49.0, 5)  # -$5

    stock = client.get("/api/status").json()
    crypto = client.get("/api/crypto/status").json()
    assert stock["starting_capital"] == 10000
    assert stock["capital"] == 10100.0
    assert crypto["starting_capital"] == 1000
    assert crypto["capital"] == 995.0

    assert [t["ticker"] for t in client.get("/api/trades").json()] == ["NVDA"]
    assert [t["ticker"] for t in client.get("/api/crypto/trades").json()] == ["SOL-USD"]


def test_open_positions_stay_per_instance(srv):
    server, client = srv
    Ledger(config.CRYPTO_DB_PATH).open_trade(
        ticker="BTC-USD", asset_type="crypto", entry_price=90.0,
        quantity=1.0, stop_loss=88.0, take_profit=96.0, signals=[])

    assert client.get("/api/positions").json() == []
    crypto_pos = client.get("/api/crypto/positions").json()
    assert [p["ticker"] for p in crypto_pos] == ["BTC-USD"]
    assert crypto_pos[0]["current_price"] == 100.0   # stubbed quote


def test_crypto_market_is_always_open(srv):
    server, client = srv
    assert client.get("/api/crypto/status").json()["market_open"] is True
    # The stock pill follows US session hours — just prove it's independent.
    assert isinstance(client.get("/api/status").json()["market_open"], bool)


def test_crypto_page_serves_the_spa(srv):
    server, client = srv
    for path in ("/", "/crypto"):
        r = client.get(path)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert 'id="nav-crypto"' in r.text   # instance switcher present
