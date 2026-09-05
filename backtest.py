#!/usr/bin/env python3
"""
Backtest / parameter-sweep CLI.

Usage:
    python backtest.py                       # single run, current config params
    python backtest.py --days 45             # shorter window
    python backtest.py --crypto-only         # skip stocks
    python backtest.py --refresh             # force re-download of history
    python backtest.py --sweep               # default parameter grid
    python backtest.py --sweep --trigger 1,1.5 --distance 1,1.5 \\
                       --tp 2,3,6 --adx 25,30,35
    python backtest.py --trigger 1.5 --distance 1.5   # single run, overrides
    python backtest.py --no-regime           # disable daily-EMA50 gate
    python backtest.py --eod-close           # old day-trading mode (flat at 15:45)
    python backtest.py --no-swing            # momentum entries only

Intraday 5m history is capped at ~60 days by yfinance, so that is the
maximum backtest window. Downloads are cached in cache/ (one day TTL).
"""

import argparse
import itertools
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()


def _floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",")]


def main():
    p = argparse.ArgumentParser(description="Strategy backtester")
    p.add_argument("--days", type=int, default=59,
                   help="Backtest window in days (max ~60 for 5m bars)")
    p.add_argument("--refresh", action="store_true", help="Force re-download")
    assets = p.add_mutually_exclusive_group()
    assets.add_argument("--stocks-only", action="store_true")
    assets.add_argument("--crypto-only", action="store_true")
    p.add_argument("--walk-forward", action="store_true", help="Select on earlier data and evaluate next blocks")
    p.add_argument("--folds", type=int, default=3)
    p.add_argument("--min-trades", type=int, default=20, help="Minimum train trades before selecting a candidate")
    p.add_argument("--max-drawdown", type=float, default=10.0, help="Maximum train drawdown percent")
    p.add_argument("--cache-date", help="Replay a trusted local YYYYMMDD snapshot without network")
    p.add_argument("--report", type=Path, help="Write a reproducible JSON validation report")
    p.add_argument("--sweep", action="store_true", help="Run a parameter grid")
    p.add_argument("--trigger", type=_floats, default=None,
                   help="Trail trigger R values, comma-separated")
    p.add_argument("--distance", type=_floats, default=None,
                   help="Trail distance R values, comma-separated")
    p.add_argument("--tp", type=_floats, default=None,
                   help="Take-profit R multiples, comma-separated")
    p.add_argument("--adx", type=_floats, default=None,
                   help="Entry ADX minimums, comma-separated")
    p.add_argument("--no-regime", action="store_true",
                   help="Disable the daily-EMA50 uptrend gate (A/B comparison)")
    p.add_argument("--eod-close", action="store_true",
                   help="Re-enable the 15:45 stock liquidation (day-trading mode)")
    p.add_argument("--no-swing", action="store_true",
                   help="Disable the RSI(2) swing entry family (momentum only)")
    p.add_argument("--top", type=int, default=15, help="Rows to show in sweep table")
    args = p.parse_args()
    if args.cache_date:
        try:
            datetime.strptime(args.cache_date, "%Y%m%d")
        except ValueError:
            p.error("--cache-date must be YYYYMMDD")
    if args.cache_date and args.refresh:
        p.error("--cache-date cannot be combined with --refresh")
    if args.report and not args.walk_forward:
        p.error("--report requires --walk-forward")
    if args.folds < 1 or args.min_trades < 1 or not 0 < args.max_drawdown < 100:
        p.error("Require positive folds/min-trades and max-drawdown in (0,100)")

    logging.basicConfig(level=logging.WARNING)
    for lib in ("yfinance", "urllib3", "requests"):
        logging.getLogger(lib).setLevel(logging.CRITICAL)

    import config
    from src.backtest import (
        SimParams, build_signal_frame, compute_metrics, fetch_universe,
        score_buckets, simulate,
    )
    from src.universe import CRYPTO, STOCKS

    stocks = STOCKS if args.stocks_only or (not args.crypto_only and config.ENABLE_STOCKS) else []
    crypto = CRYPTO if args.crypto_only or (not args.stocks_only and config.ENABLE_CRYPTO) else []

    console.print(f"[cyan]Fetching {len(stocks) + len(crypto)} symbols "
                  f"({args.days}d of 5m bars)…[/cyan]")
    if args.cache_date:
        import pandas as pd
        from src.backtest import CACHE_DIR
        raw = {}
        for ticker, asset in [(t, "stock") for t in stocks] + [(t, "crypto") for t in crypto]:
            path = CACHE_DIR / f"{ticker}_{args.days}d_{args.cache_date}.pkl"
            if path.exists():
                intra, daily = pd.read_pickle(path)
                raw[ticker] = (asset, intra, daily)
        console.print(f"[yellow]Offline snapshot: {args.cache_date}; "
                      f"missing symbols: {sorted(set(stocks + crypto) - set(raw))}[/yellow]")
    else:
        raw = fetch_universe(stocks, crypto, args.days, refresh=args.refresh)
    console.print(f"[cyan]{len(raw)} symbols with usable history — "
                  f"building signal frames…[/cyan]")

    frames = {}
    for tk, (at, intra, daily) in raw.items():
        sd = build_signal_frame(tk, at, intra, daily)
        if sd is not None:
            frames[tk] = sd
    console.print(f"[cyan]{len(frames)} signal frames ready.[/cyan]\n")
    if not frames:
        console.print("[red]No data — nothing to backtest.[/red]")
        return

    # ── Parameter grid ────────────────────────────────────────────────────
    if args.sweep or args.walk_forward:
        triggers = args.trigger or ([config.PROFIT_TRAIL_TRIGGER_R] if args.walk_forward else [1.0, 1.5, 99.0])
        distances = args.distance or ([config.PROFIT_TRAIL_DISTANCE_R] if args.walk_forward else [1.0, 1.5])
        tps = args.tp or [2.0, 3.0, 6.0]
        adxs = args.adx or ([config.ENTRY_ADX_MIN] if not stocks else [25.0, 30.0, 35.0])
    else:
        triggers = args.trigger or [config.PROFIT_TRAIL_TRIGGER_R]
        distances = args.distance or [config.PROFIT_TRAIL_DISTANCE_R]
        tps = args.tp or [config.TAKE_PROFIT_R_MULT]
        adxs = args.adx or [config.ENTRY_ADX_MIN]

    grid = list(itertools.product(adxs, tps, triggers, distances))
    baseline = SimParams(require_uptrend=config.REQUIRE_DAILY_UPTREND and not args.no_regime,
                         leverage=config.LEVERAGE, margin_call_loss=config.MARGIN_CALL_LOSS)
    if args.walk_forward:
        from src.validation import walk_forward, json_safe
        candidates = [replace(baseline, entry_adx_min=adx, take_profit_r_mult=tp,
                              trail_trigger_r=trig, trail_distance_r=dist)
                      for adx, tp, trig, dist in grid]
        try:
            report = walk_forward(frames, candidates, baseline, folds=args.folds,
                                  min_trades=args.min_trades, max_drawdown_pct=args.max_drawdown,
                                  progress=lambda message: console.print(f"[dim]{message}[/dim]"))
        except ValueError as exc:
            p.error(str(exc))
        report["data"] = {"cache_date": args.cache_date, "days_requested": args.days,
                          "symbols": sorted(frames),
                          "missing_symbols": sorted(set(stocks + crypto) - set(frames)),
                          "first_bar": str(min(t for f in frames.values() for t in f.ts)),
                          "last_bar": str(max(t for f in frames.values() for t in f.ts))}
        report["code_version"] = config.VERSION
        report["signal_config"] = {name: getattr(config, name) for name in (
            "STOP_LOSS_PCT", "CRYPTO_BREAKOUT_BARS", "RSI_PERIOD", "EMA_FAST", "EMA_MID",
            "EMA_SLOW", "ATR_PERIOD", "ADX_PERIOD", "MACD_FAST", "MACD_SLOW",
            "MACD_SIGNAL_PERIOD", "BB_PERIOD", "BB_STD", "VOLUME_LOOKBACK",
            "MIN_PRICE", "MIN_AVG_DAILY_VOLUME", "MAX_DATA_AGE_MINUTES")}
        report["data"]["coverage"] = {
            ticker: {"bars": len(frame.ts), "first_bar": str(frame.ts[0]), "last_bar": str(frame.ts[-1])}
            for ticker, frame in sorted(frames.items())
        }
        for name, metrics in report["out_of_sample"].items():
            console.print(f"{name}: {metrics['trades']} trades, return {metrics['return_pct']:+.2f}%, "
                          f"drawdown {metrics['max_dd_pct']:.2f}%, net R {metrics['avg_r']:+.3f}")
        for fold in report["folds"]:
            choice = "CASH" if fold["selected_params"] is None else "trained candidate"
            console.print(f"Fold {fold['fold']}: {fold['test_start']} to {fold['test_end_exclusive']} "
                          f"(end exclusive): {choice}")
        console.print("[yellow]Research only: results do not update bot settings. "
                      "Historical earnings filtering is not replayed.[/yellow]")
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(json_safe(report), indent=2, allow_nan=False), encoding="utf-8")
            console.print(f"Report: {args.report.resolve()}")
        return
    results = []
    for n, (adx, tp, trig, dist) in enumerate(grid, 1):
        params = replace(baseline, entry_adx_min=adx, take_profit_r_mult=tp,
                           trail_trigger_r=trig, trail_distance_r=dist,
                           require_uptrend=not args.no_regime,
                           eod_close_stocks=args.eod_close,
                           swing_enabled=not args.no_swing)
        if len(grid) > 1:
            console.print(f"[dim]  {n}/{len(grid)}  {escape(params.label())}[/dim]")
        res = simulate(frames, params)
        results.append((params, res, compute_metrics(res)))

    # ── Single-run report ─────────────────────────────────────────────────
    if len(results) == 1:
        params, res, m = results[0]
        _print_single(params, m, res, score_buckets(res.trades))
        return

    # ── Sweep report ──────────────────────────────────────────────────────
    results.sort(key=lambda r: r[2]["expectancy"], reverse=True)
    tbl = Table(title=f"Parameter sweep — top {min(args.top, len(results))} "
                      f"of {len(results)} by expectancy/trade",
                box=box.SIMPLE_HEAVY)
    for col in ("ADX>", "TP (R)", "Trig (R)", "Dist (R)", "Trades",
                "Win %", "PnL $", "Ret %", "Exp $/tr", "Avg R", "PF", "MaxDD %"):
        tbl.add_column(col, justify="right")
    for params, _res, m in results[:args.top]:
        pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "∞"
        tbl.add_row(
            f"{params.entry_adx_min:g}", f"{params.take_profit_r_mult:g}",
            f"{params.trail_trigger_r:g}", f"{params.trail_distance_r:g}",
            str(m["trades"]), f"{m['win_rate']:.0f}",
            f"{m['total_pnl']:+,.0f}", f"{m['return_pct']:+.1f}",
            f"{m['expectancy']:+.2f}", f"{m['avg_r']:+.2f}",
            pf, f"{m['max_dd_pct']:.1f}",
        )
    console.print(tbl)
    console.print(
        "\n[dim]Caveats: single historical window — prefer parameter regions "
        "where neighbours also perform well over a lone spike, and re-check "
        "the winner with --days on a shorter window before adopting it.[/dim]"
    )
    best = results[0][0]
    console.print(
        f"\nBest by expectancy: [bold]{escape(best.label())}[/bold]\n"
        f"Adopt via .env:  ENTRY_ADX_MIN={best.entry_adx_min:g}  "
        f"TAKE_PROFIT_R_MULT={best.take_profit_r_mult:g}  "
        f"PROFIT_TRAIL_TRIGGER_R={best.trail_trigger_r:g}  "
        f"PROFIT_TRAIL_DISTANCE_R={best.trail_distance_r:g}"
    )


def _print_single(params, m, res, buckets):
    pf = "∞" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    console.print(f"[bold]Backtest — {escape(params.label())}[/bold]\n")
    console.print(
        f"  Trades        : {m['trades']}   (win rate {m['win_rate']:.1f}%)\n"
        f"  Total PnL     : ${m['total_pnl']:+,.2f}  ({m['return_pct']:+.2f}% "
        f"on ${params.starting_capital:,.0f})\n"
        f"  Expectancy    : ${m['expectancy']:+.2f}/trade   "
        f"(avg {m['avg_r']:+.2f}R)\n"
        f"  Profit factor : {pf}\n"
        f"  Max drawdown  : {m['max_dd_pct']:.1f}%\n"
        f"  Daily return  : mean {m['mean_daily_pct']:+.2f}%   "
        f"best {m['best_day_pct']:+.2f}%   worst {m['worst_day_pct']:+.2f}%\n"
        f"  Days >= +5%   : {m['days_ge_5pct']} of {m['n_days']}"
    )
    if m["exit_reasons"]:
        parts = [f"{k} {v}" for k, v in sorted(m["exit_reasons"].items())]
        console.print(f"  Exits         : {'  ·  '.join(parts)}")
    if len(m.get("by_strategy", {})) > 1:
        parts = [f"{k}: {v['trades']} trades, {v['avg_r']:+.2f}R, "
                 f"${v['total_pnl']:+,.0f}, win {v['win_rate']:.0f}%"
                 for k, v in m["by_strategy"].items()]
        console.print(f"  By strategy   : {'  ·  '.join(parts)}")

    if buckets:
        tbl = Table(title="Score vs outcome (entry-score buckets)",
                    box=box.SIMPLE_HEAVY)
        for col in ("Score", "Trades", "Win %", "Avg R", "PnL $"):
            tbl.add_column(col, justify="right")
        for b in buckets:
            tbl.add_row(b["bucket"], str(b["trades"]), f"{b['win_rate']:.0f}",
                        f"{b['avg_r']:+.2f}", f"{b['total_pnl']:+,.0f}")
        console.print()
        console.print(tbl)


if __name__ == "__main__":
    main()
