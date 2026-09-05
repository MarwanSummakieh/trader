"""Expanding-window strategy selection; later bars never select earlier trades."""
from dataclasses import asdict, replace
from datetime import timedelta
import math

import numpy as np
import pandas as pd

from .backtest import SimResult, compute_metrics, simulate, slice_frames


def walk_forward(frames, candidates, baseline, *, folds=3, min_trades=20,
                 max_drawdown_pct=10.0, progress=None):
    """Use first half to train; test successive non-overlapping blocks thereafter.

    Pick the highest net-return train candidate with sufficient trades, positive
    expectancy and acceptable drawdown. Otherwise hold cash for the entire next
    block. Each decision is frozen before that block is inspected. This is a
    research comparison, never an automatic change to running bot settings.
    All paths liquidate at fold boundaries; costs are charged and cash compounds.
    """
    if (folds < 1 or min_trades < 1 or not math.isfinite(max_drawdown_pct)
            or not 0 < max_drawdown_pct < 100 or not candidates):
        raise ValueError("Require folds >= 1, min_trades >= 1, drawdown in (0,100), and candidates")
    dates = sorted({d for f in frames.values() for d in f.date})
    split = len(dates) // 2
    if split < 2 or len(dates) - split < folds:
        raise ValueError("Not enough dates for training and non-empty test folds")
    for candidate in candidates:
        if candidate.starting_capital != baseline.starting_capital:
            raise ValueError("Candidates must use the same starting capital")
    blocks = np.array_split(np.array(dates[split:], dtype=object), folds)
    names = ("selected", "baseline", "fixed_size")
    capital = {name: baseline.starting_capital for name in names}
    histories = {name: [] for name in names}
    trades = {name: [] for name in names}
    busted = {name: False for name in names}
    reports = []
    for number, block in enumerate(blocks, 1):
        start, end = block[0], block[-1] + timedelta(days=1)
        training = slice_frames(frames, end=start)
        ranked = []
        for index, params in enumerate(candidates):
            if progress:
                progress(f"Fold {number}/{folds}: train candidate {index + 1}/{len(candidates)}")
            result = simulate(training, params)
            metrics = compute_metrics(result)
            if (not result.busted and metrics["trades"] >= min_trades
                    and metrics["total_pnl"] > 0
                    and metrics["max_dd_pct"] >= -max_drawdown_pct):
                ranked.append((params, metrics))
        ranked.sort(key=lambda item: (item[1]["return_pct"], item[1]["max_dd_pct"]), reverse=True)
        selected, train_metrics = ranked[0] if ranked else (None, None)
        # Do not touch the test slice until selection is final.
        testing = slice_frames(frames, start=start, end=end)
        fold = {"fold": number, "train_start": str(dates[0]), "train_end_exclusive": str(start),
                "test_start": str(start), "test_end_exclusive": str(end),
                "eligible_candidates": len(ranked),
                "selected_params": asdict(selected) if selected else None,
                "selected_train_metrics": train_metrics}
        paths = {"selected": selected, "baseline": baseline,
                 "fixed_size": replace(baseline, risk_per_trade_pct=0.0,
                                       max_portfolio_risk_pct=0.0)}
        for name, params in paths.items():
            run_params = replace(params or baseline, starting_capital=max(capital[name], 1e-9))
            if params is None or busted[name]:
                ts = sorted({t for f in testing.values() for t in f.ts})
                result = SimResult(run_params, [], pd.Series(capital[name], index=pd.DatetimeIndex(ts)))
            else:
                result = simulate(testing, run_params)
            fold[name] = compute_metrics(result)
            if len(result.equity):
                capital[name] = float(result.equity.iloc[-1])
            histories[name].append(result.equity)
            trades[name].extend(result.trades)
            busted[name] |= result.busted
        reports.append(fold)
    totals = {name: compute_metrics(SimResult(baseline, trades[name], pd.concat(histories[name]), busted[name]))
              for name in names}
    return {"method": "expanding train / next-block test; flat at fold boundaries",
            "candidate_count": len(candidates), "min_train_trades": min_trades,
            "baseline_params": asdict(baseline),
            "candidate_params": [asdict(params) for params in candidates],
            "max_train_drawdown_pct": max_drawdown_pct,
            "folds": reports, "out_of_sample": totals,
            "notes": ["Selection maximizes train net return subject to the stated risk and sample limits.",
                      "A cash fold means insufficient positive training evidence, not a proven edge.",
                      "fixed_size uses the same corrected simulator with both risk caps disabled.",
                      "Static present-day universe; survivorship bias is not eliminated.",
                      "Historical earnings exclusions are not replayed from OHLCV caches.",
                      "Slippage is modeled; crypto venue fees, funding and market impact are not.",
                      "Short, previously researched histories do not prove future profitability."]}


def json_safe(value):
    """Portable JSON: preserve explicit infinity labels instead of invalid NaN tokens."""
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "infinity" if value == math.inf else None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
