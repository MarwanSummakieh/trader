"""EOD liquidation is a mode, not a fixture: with EOD_CLOSE_STOCKS off the
bot must never flatten at 15:45 (positions are held overnight); with it on,
the flatten fires once per session after EOD_CLOSE_TIME."""

from datetime import datetime

import pytz

import config
from src.bot import TradingBot

ET = pytz.timezone("America/New_York")


def bot_at(monkeypatch, hh, mm, eod_on, closed_today=False):
    monkeypatch.setattr(config, "EOD_CLOSE_STOCKS", eod_on)
    b = TradingBot.__new__(TradingBot)          # skip ledger/broker setup
    b._eod_closed_today = closed_today
    # Wednesday 2026-09-02 — a regular session
    b._now = lambda: ET.localize(datetime(2026, 9, 2, hh, mm))
    return b


def test_no_flatten_when_eod_mode_off(monkeypatch):
    assert bot_at(monkeypatch, 15, 50, eod_on=False).eod_close_due() is False


def test_flatten_after_1545_when_eod_mode_on(monkeypatch):
    assert bot_at(monkeypatch, 15, 50, eod_on=True).eod_close_due() is True


def test_flatten_only_once_per_session(monkeypatch):
    assert bot_at(monkeypatch, 15, 50, eod_on=True, closed_today=True).eod_close_due() is False


def test_no_flatten_before_1545(monkeypatch):
    assert bot_at(monkeypatch, 15, 40, eod_on=True).eod_close_due() is False
