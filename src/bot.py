"""
Main bot orchestration loop.
- Scans the full universe every SCAN_INTERVAL_SECS
- Monitors open positions every MONITOR_INTERVAL_SECS
- Force-closes all stock positions at EOD_CLOSE_TIME
- Crypto scanning + position monitoring runs 24/7
"""

import logging
import time
from datetime import datetime
from typing import Optional

import pytz
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .broker import make_broker
from .data import get_current_prices, EToroClient
from .ledger import Ledger, Trade
from .portfolio import Portfolio
from .scanner import get_buy_candidates, scan_universe
from .universe import CRYPTO, STOCKS
import config

logger = logging.getLogger(__name__)
console = Console()
ET = pytz.timezone("America/New_York")


class TradingBot:
    def __init__(self):
        self.ledger = Ledger(config.DB_PATH)
        self.broker = make_broker()
        self.portfolio = Portfolio(self.ledger, broker=self.broker)
        self.etoro: Optional[EToroClient] = (
            EToroClient(config.ETORO_PUBLIC_KEY, config.ETORO_PRIVATE_KEY)
            if config.ETORO_PUBLIC_KEY else None
        )
        self._last_scan: Optional[datetime] = None
        self._last_monitor: Optional[datetime] = None
        self._last_display: Optional[datetime] = None
        self._last_analyses: list = []
        self._eod_closed_today = False

    # ── Time helpers ───────────────────────────────────────────────────────

    def _now(self) -> datetime:
        return datetime.now(ET)

    def _market_open(self) -> bool:
        now = self._now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return config.MARKET_OPEN <= t <= config.MARKET_CLOSE

    def _past_eod(self) -> bool:
        return self._now().time() >= config.EOD_CLOSE_TIME

    def _due(self, last: Optional[datetime], interval: int) -> bool:
        if last is None:
            return True
        return (self._now() - last).total_seconds() >= interval

    # ── Actions ────────────────────────────────────────────────────────────

    def run_scan(self, stocks: list[str], crypto: list[str]):
        console.print("[cyan]Scanning universe...[/cyan]", end=" ")
        analyses = scan_universe(stocks, crypto)
        self._last_analyses = analyses
        self._last_scan = self._now()
        self.ledger.save_scan_results(analyses)
        console.print(f"[cyan]{len(analyses)} symbols analysed[/cyan]")

        candidates = get_buy_candidates(analyses, self.portfolio.open_tickers)
        if not candidates:
            console.print("[yellow]No actionable signals this scan.[/yellow]")
            return

        console.print(f"[green]{len(candidates)} candidate(s) → opening positions[/green]")
        for analysis in candidates:
            if not self.portfolio.can_open():
                break
            trade = self.portfolio.open_position(analysis)
            if trade:
                _print_open(analysis)

    def run_monitor(self):
        trades = self.portfolio.open_trades
        if not trades:
            self._last_monitor = self._now()
            return
        prices = get_current_prices([t.ticker for t in trades])
        closed = self.portfolio.check_exits(prices)
        for t in closed:
            _print_close(t)
        self._last_monitor = self._now()

    def run_eod_close(self):
        trades = [t for t in self.portfolio.open_trades if t.asset_type == "stock"]
        if not trades:
            self._eod_closed_today = True
            return
        console.print("[bold red]⏰ EOD — liquidating all stock positions[/bold red]")
        prices = get_current_prices([t.ticker for t in trades])
        closed = self.portfolio.eod_close_stocks(prices)
        for t in closed:
            _print_close(t)
        # Only mark done once every stock position actually closed — positions
        # skipped for missing prices are retried on the next loop iteration.
        still_open = [t for t in self.portfolio.open_trades if t.asset_type == "stock"]
        self._eod_closed_today = not still_open

    # ── Display ────────────────────────────────────────────────────────────

    def display(self):
        console.clear()
        now = self._now()
        market_tag = "[green]OPEN[/green]" if self._market_open() else "[dim]CLOSED[/dim]"
        console.print(Panel(
            f"[bold]Day Trading Bot[/bold]  {now.strftime('%Y-%m-%d %H:%M:%S')} ET  |  "
            f"Market: {market_tag}  |  Exec: {self.broker.name}",
            style="bold blue", expand=False,
        ))

        stats = self.ledger.get_stats()
        pnl_color = "green" if stats["total_pnl"] >= 0 else "red"
        console.print(
            f"Capital: [bold]${self.portfolio.capital:,.0f}[/bold]  "
            f"Deployed: [bold]${self.portfolio.capital_deployed:,.0f}[/bold]  "
            f"Positions: [bold]{self.portfolio.position_count}/{config.MAX_POSITIONS}[/bold]  "
            f"Realized PnL: [{pnl_color}]${stats['total_pnl']:+,.2f}[/{pnl_color}]  "
            f"Win rate: [bold]{stats['win_rate']:.0f}%[/bold] ({stats['total_trades']} trades)"
        )
        console.print()

        # Open positions
        if self.portfolio.open_trades:
            prices = get_current_prices([t.ticker for t in self.portfolio.open_trades])
            tbl = Table(title="Open Positions", box=box.SIMPLE_HEAVY, style="cyan")
            for col, just in [
                ("Ticker", "left"), ("Type", "left"), ("Entry $", "right"),
                ("Now $", "right"), ("Stop $", "right"), ("Target $", "right"),
                ("Qty", "right"), ("Unreal PnL", "right"), ("Since", "left"),
            ]:
                tbl.add_column(col, justify=just)
            for t in self.portfolio.open_trades:
                cur = prices.get(t.ticker, t.entry_price)
                upnl = (cur - t.entry_price) * t.quantity
                upnl_color = "green" if upnl >= 0 else "red"
                tbl.add_row(
                    t.ticker, t.asset_type,
                    f"${t.entry_price:.2f}", f"${cur:.2f}",
                    f"[red]${t.stop_loss:.2f}[/red]",
                    f"[green]${t.take_profit:.2f}[/green]",
                    f"{t.quantity:.2f}",
                    f"[{upnl_color}]${upnl:+.2f}[/{upnl_color}]",
                    t.entry_time[11:16],
                )
            console.print(tbl)
            console.print()

        # Top scan candidates
        if self._last_analyses:
            scan_ts = self._last_scan.strftime("%H:%M") if self._last_scan else "—"
            tbl2 = Table(
                title=f"Universe Scan — Top 20  (updated {scan_ts})",
                box=box.SIMPLE_HEAVY, style="yellow",
            )
            for col, just in [
                ("#", "right"), ("Ticker", "left"), ("Score", "right"),
                ("Price $", "right"), ("RSI", "right"), ("Vol x", "right"),
                ("Trend", "left"), ("Why", "left"),
            ]:
                tbl2.add_column(col, justify=just)
            for i, a in enumerate(self._last_analyses[:20], 1):
                sc = a.score
                sc_color = "green" if sc >= 65 else "yellow" if sc >= 45 else "red"
                tr_color = "green" if a.trend == "bullish" else "red" if a.trend == "bearish" else "yellow"
                tbl2.add_row(
                    str(i), a.ticker,
                    f"[{sc_color}]{sc}[/{sc_color}]",
                    f"${a.price:.2f}",
                    f"{a.rsi:.0f}",
                    f"{a.volume_ratio:.1f}",
                    f"[{tr_color}]{a.trend}[/{tr_color}]",
                    "  ".join(a.signals[:2]) or "—",
                )
            console.print(tbl2)
            console.print()

        # Recent closed trades
        recent = self.ledger.get_recent_trades(10)
        if recent:
            tbl3 = Table(title="Recent Closed Trades", box=box.SIMPLE_HEAVY)
            for col, just in [
                ("Ticker", "left"), ("Entry $", "right"), ("Exit $", "right"),
                ("PnL $", "right"), ("PnL %", "right"), ("Reason", "left"), ("Closed", "left"),
            ]:
                tbl3.add_column(col, justify=just)
            for t in recent:
                c = "green" if (t.pnl or 0) >= 0 else "red"
                tbl3.add_row(
                    t.ticker,
                    f"${t.entry_price:.2f}",
                    f"${t.exit_price:.2f}" if t.exit_price else "—",
                    f"[{c}]${t.pnl:+.2f}[/{c}]" if t.pnl is not None else "—",
                    f"[{c}]{t.pnl_pct:+.1f}%[/{c}]" if t.pnl_pct is not None else "—",
                    t.exit_reason or "—",
                    t.exit_time[11:16] if t.exit_time else "—",
                )
            console.print(tbl3)

        self._last_display = self._now()

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self):
        console.print("[bold green]Day Trading Bot — starting up[/bold green]")
        console.print(f"Execution: [bold]{self.broker.name}[/bold]")
        if hasattr(self.broker, "test_connection"):
            ok, msg = self.broker.test_connection()
            style = "green" if ok else "bold red"
            console.print(f"[{style}]{'✓' if ok else '✗'} {msg}[/{style}]")
            if not ok:
                console.print("[bold red]Broker unreachable — exiting.[/bold red]")
                return
        if self.etoro:
            ok, msg = self.etoro.test_connection()
            if ok:
                bal = self.etoro.get_account_balance()
                bal_str = f" — real balance: ${bal:,.2f}" if bal else ""
                console.print(f"[green]✓ {msg}{bal_str}[/green]")
            else:
                console.print(f"[yellow]⚠ {msg}[/yellow]")

        while True:
            try:
                now = self._now()

                # Reset EOD flag before the next session opens
                if now.time() < config.MARKET_OPEN:
                    self._eod_closed_today = False

                # EOD stock liquidation
                if self._market_open() and self._past_eod() and not self._eod_closed_today:
                    self.run_eod_close()

                # Determine which asset classes to scan this cycle
                crypto_universe = CRYPTO if config.ENABLE_CRYPTO else []
                if self._market_open() and not self._past_eod():
                    stocks_to_scan, crypto_to_scan = STOCKS, crypto_universe
                else:
                    stocks_to_scan, crypto_to_scan = [], crypto_universe  # crypto runs 24/7

                if self._due(self._last_scan, config.SCAN_INTERVAL_SECS):
                    self.run_scan(stocks_to_scan, crypto_to_scan)

                if self._due(self._last_monitor, config.MONITOR_INTERVAL_SECS):
                    self.run_monitor()

                if self._due(self._last_display, config.DISPLAY_INTERVAL_SECS):
                    self.display()

                time.sleep(10)

            except KeyboardInterrupt:
                console.print("\n[bold red]Shutting down.[/bold red]")
                break
            except Exception as exc:
                logger.error("Unhandled error in main loop: %s", exc, exc_info=True)
                time.sleep(30)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_open(a):
    console.print(Panel(
        f"[bold green]BUY  {a.ticker}[/bold green]  (score {a.score}/100)\n"
        f"Entry ${a.entry:.2f}  |  Stop [red]${a.stop_loss:.2f}[/red]  |  "
        f"Target [green]${a.take_profit:.2f}[/green]  |  R/R {a.risk_reward:.1f}x\n"
        f"[dim]{' · '.join(a.signals) or 'no signals'}[/dim]",
        style="green", expand=False,
    ))


def _print_close(t: Trade):
    pnl = t.pnl or 0
    color = "green" if pnl >= 0 else "red"
    console.print(Panel(
        f"[bold {color}]CLOSE {t.ticker}[/bold {color}]  [{t.exit_reason}]\n"
        f"${t.entry_price:.2f} → ${t.exit_price:.2f}  |  "
        f"PnL [{color}]${pnl:+.2f} ({t.pnl_pct:+.1f}%)[/{color}]",
        style=color, expand=False,
    ))
