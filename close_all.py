#!/usr/bin/env python3
"""
Wind down every open ledger trade at current prices (paper-sim fills,
reason "manual_close"). History and realized PnL are preserved.

Use before switching BROKER (e.g. paper -> alpaca): trades opened by the
simulator have no server-side orders at the new broker, so they can never
be closed by it — they must be wound down here.

    python close_all.py          # list and ask for confirmation
    python close_all.py --yes    # close without prompting

On the docker deployment:

    docker compose run --rm bot python close_all.py --yes
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import config
from src.broker import SimBroker
from src.data import get_current_prices
from src.ledger import Ledger


def wind_down(ledger: Ledger, prices: dict[str, float]) -> tuple[int, int]:
    """Close every open trade with a price; returns (closed, skipped)."""
    sim = SimBroker()
    closed = skipped = 0
    for t in ledger.get_open_trades():
        price = prices.get(t.ticker)
        if price is None:
            print(f"  SKIP   {t.ticker:<8} — no price available, still open")
            skipped += 1
            continue
        fill = sim.close(t.ticker, price, "manual_close")
        done = ledger.close_trade(t.id, fill.price, "manual_close")
        print(f"  CLOSED {t.ticker:<8} @ ${fill.price:>10.2f}  "
              f"PnL ${done.pnl:>+8.2f} ({done.pnl_pct:+.2f}%)")
        closed += 1
    return closed, skipped


def main():
    ap = argparse.ArgumentParser(description="Close all open ledger trades")
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    args = ap.parse_args()

    ledger = Ledger(config.DB_PATH)
    trades = ledger.get_open_trades()
    if not trades:
        print("No open trades — nothing to do.")
        return

    prices = get_current_prices([t.ticker for t in trades])
    print(f"{len(trades)} open trade(s) in {config.DB_PATH}:")
    for t in trades:
        cur = prices.get(t.ticker)
        upnl = (cur - t.entry_price) * t.quantity if cur is not None else None
        print(f"  {t.ticker:<8} qty {t.quantity:>9.3f} @ ${t.entry_price:>10.2f}"
              f"  now {f'${cur:.2f}' if cur is not None else 'n/a':>11}"
              f"  uPnL {f'${upnl:+.2f}' if upnl is not None else 'n/a'}")

    if not args.yes:
        resp = input("\nClose ALL of these at current prices? [y/N] ")
        if resp.strip().lower() not in ("y", "yes"):
            print("Aborted — nothing closed.")
            return

    print()
    closed, skipped = wind_down(ledger, prices)
    stats = ledger.get_stats()
    print(f"\n{closed} closed, {skipped} skipped (rerun for skipped ones).")
    print(f"Ledger now: {stats['total_trades']} closed trades, "
          f"realized PnL ${stats['total_pnl']:+,.2f}, 0 open"
          if not ledger.get_open_trades() else
          f"Ledger still has {len(ledger.get_open_trades())} open trade(s).")


if __name__ == "__main__":
    main()
