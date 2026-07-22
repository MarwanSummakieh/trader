"""Scan-universe selection: crypto must be scanned around the clock — from
the first cycle, on weekends, and outside US market hours — while stocks
stay gated on the (pre-EOD) session. A regression here would leave the
crypto instance idle until the stock market opened."""

import config
from src.bot import universes_to_scan
from src.universe import CRYPTO, STOCKS


def crypto_instance(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_STOCKS", False)
    monkeypatch.setattr(config, "ENABLE_CRYPTO", True)


def stock_instance(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_STOCKS", True)
    monkeypatch.setattr(config, "ENABLE_CRYPTO", False)


def test_crypto_scans_regardless_of_session(monkeypatch):
    crypto_instance(monkeypatch)
    for session_open in (True, False):
        stocks, crypto = universes_to_scan(session_open)
        assert crypto == CRYPTO
        assert stocks == []


def test_stocks_scan_only_during_session(monkeypatch):
    stock_instance(monkeypatch)
    assert universes_to_scan(True) == (STOCKS, [])
    assert universes_to_scan(False) == ([], [])


def test_dual_instance_outside_session_still_scans_crypto(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_STOCKS", True)
    monkeypatch.setattr(config, "ENABLE_CRYPTO", True)
    assert universes_to_scan(False) == ([], CRYPTO)
    assert universes_to_scan(True) == (STOCKS, CRYPTO)
