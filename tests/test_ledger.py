"""Ledger day-bucketed stats (feeds the dashboard's daily-gain metrics)."""

from datetime import datetime

import pytz

ET = pytz.timezone("America/New_York")


def _open_and_close(ledger, ticker, entry, exit_px):
    t = ledger.open_trade(ticker=ticker, asset_type="stock", entry_price=entry,
                          quantity=10.0, stop_loss=entry * 0.95,
                          take_profit=entry * 1.15, signals=[])
    return ledger.close_trade(t.id, exit_px, "take_profit")


def test_stats_for_day_buckets_by_et_date(ledger):
    _open_and_close(ledger, "WIN", 100.0, 110.0)     # +100
    _open_and_close(ledger, "LOSS", 100.0, 96.0)     # -40
    ledger.open_trade(ticker="OPEN", asset_type="stock", entry_price=100.0,
                      quantity=10.0, stop_loss=95.0, take_profit=115.0,
                      signals=[])                     # open — never counted

    today = datetime.now(ET).strftime("%Y-%m-%d")
    s = ledger.get_stats_for_day(today)
    assert s["total_trades"] == 2
    assert abs(s["total_pnl"] - 60.0) < 1e-9
    assert abs(s["win_rate"] - 50.0) < 1e-9

    # A day with no exits reports zeros, and other days' trades don't leak in
    s = ledger.get_stats_for_day("2000-01-01")
    assert s == {"total_pnl": 0, "total_trades": 0, "win_rate": 0.0}


def test_concurrent_access_single_connection(ledger):
    """The dashboard hits five endpoints in parallel from FastAPI's
    threadpool over one shared connection; unserialized access raises
    sqlite3.InterfaceError ('bad parameter or other API misuse')."""
    from concurrent.futures import ThreadPoolExecutor

    _open_and_close(ledger, "SEED", 100.0, 105.0)

    def hammer(i):
        for _ in range(25):
            ledger.get_stats()
            ledger.get_open_trades()
            ledger.get_recent_trades(10)
            ledger.get_stats_for_day("2026-01-01")
            if i == 0:
                _open_and_close(ledger, "W", 100.0, 101.0)

    with ThreadPoolExecutor(max_workers=5) as pool:
        for f in [pool.submit(hammer, i) for i in range(5)]:
            f.result()   # raises if any thread hit an sqlite error
