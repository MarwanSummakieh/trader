#!/usr/bin/env python3
"""
Day Trading Bot — entry point.

Usage:
    python main.py               # run the bot
    python main.py --scan-once   # one scan, print results, exit
    python main.py --stats       # print ledger statistics
    python main.py --trades      # print last 20 closed trades
    python main.py --capital 50000  # override starting capital

Set your eToro API key (optional, for portfolio context):
    export ETORO_API_KEY=<your key>
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console

console = Console()


def main():
    parser = argparse.ArgumentParser(description="Day Trading Bot")
    parser.add_argument("--scan-once", action="store_true",
                        help="Run one full scan, print top picks, exit")
    parser.add_argument("--stats", action="store_true",
                        help="Print ledger statistics and exit")
    parser.add_argument("--trades", action="store_true",
                        help="Print last 20 closed trades and exit")
    parser.add_argument("--capital", type=float, default=None,
                        help="Override starting capital (default 10 000)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show DEBUG log output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("bot.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Silence noisy third-party loggers
    # yfinance logs ERROR for delisted/unavailable tickers — suppress since we
    # handle missing data gracefully (get_intraday/get_daily return None).
    for lib in ("yfinance", "yfinance.base", "peewee", "urllib3", "requests", "asyncio"):
        logging.getLogger(lib).setLevel(logging.CRITICAL)

    import config
    if args.capital:
        config.STARTING_CAPITAL = args.capital

    # ── One-shot modes ────────────────────────────────────────────────────
    if args.stats:
        from src.ledger import Ledger
        s = Ledger().get_stats()
        console.print("\n[bold]Ledger Statistics[/bold]")
        console.print(f"  Total trades : {s['total_trades']}")
        console.print(f"  Win rate     : {s['win_rate']:.1f}%")
        console.print(f"  Total PnL    : ${s['total_pnl']:+,.2f}")
        console.print(f"  Avg PnL/trade: {s['avg_pnl_pct']:+.2f}%")
        console.print(f"  Best trade   : ${s['best_trade']:+,.2f}")
        console.print(f"  Worst trade  : ${s['worst_trade']:+,.2f}")
        return

    if args.trades:
        from src.ledger import Ledger
        trades = Ledger().get_recent_trades(20)
        if not trades:
            console.print("No closed trades yet.")
            return
        console.print("\n[bold]Last 20 Closed Trades[/bold]")
        for t in trades:
            pnl_str = f"${t.pnl:+.2f} ({t.pnl_pct:+.1f}%)" if t.pnl is not None else "open"
            color = "green" if (t.pnl or 0) >= 0 else "red"
            console.print(
                f"  {t.ticker:<8}  entered {t.entry_time[5:16]}  "
                f"[{color}]{pnl_str}[/{color}]  [{t.exit_reason}]"
            )
        return

    if args.scan_once:
        from src.scanner import scan_universe
        from src.universe import CRYPTO, STOCKS
        console.print("[cyan]Running single scan...[/cyan]")
        analyses = scan_universe(STOCKS, CRYPTO)
        console.print(f"\n[bold]Top 20 Opportunities[/bold] ({len(analyses)} analysed)\n")
        for i, a in enumerate(analyses[:20], 1):
            sc_color = "green" if a.score >= 65 else "yellow" if a.score >= 45 else "red"
            console.print(
                f"  {i:>2}. [bold]{a.ticker:<8}[/bold]  "
                f"score [{sc_color}]{a.score:>3}[/{sc_color}]  "
                f"${a.price:<10.2f}  "
                f"RSI {a.rsi:.0f}  vol {a.volume_ratio:.1f}x  "
                f"{a.trend:<8}  "
                f"SL ${a.stop_loss:.2f}  TP ${a.take_profit:.2f}"
            )
            if a.signals:
                console.print(f"       [dim]{' · '.join(a.signals)}[/dim]")
        return

    # ── Continuous mode ────────────────────────────────────────────────────
    from src.bot import TradingBot
    TradingBot().run()


if __name__ == "__main__":
    main()
