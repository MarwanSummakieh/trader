"""
Universe scanner: downloads data + runs analysis for every ticker in parallel,
then filters and ranks results.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from .analyzer import analyze, Analysis
from .data import get_intraday, get_daily
import config

logger = logging.getLogger(__name__)


def _scan_one(ticker: str, asset_type: str) -> Optional[Analysis]:
    try:
        intra = get_intraday(ticker)
        daily = get_daily(ticker)
        return analyze(ticker, intra, daily, asset_type=asset_type)
    except Exception as e:
        logger.debug("Scan failed %s: %s", ticker, e)
        return None


def scan_universe(
    stocks: list[str],
    crypto: list[str],
    max_workers: int = 10,
) -> list[Analysis]:
    """
    Scan all tickers concurrently.
    Returns results sorted by score descending (best opportunities first).
    """
    tasks = [(t, "stock") for t in stocks] + [(t, "crypto") for t in crypto]
    results: list[Analysis] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_scan_one, t, at): (t, at) for t, at in tasks}
        for fut in as_completed(futures):
            a = fut.result()
            if a is not None:
                results.append(a)

    results.sort(key=lambda a: a.score, reverse=True)
    return results


def get_buy_candidates(
    analyses: list[Analysis],
    existing_tickers: set[str],
) -> list[Analysis]:
    """
    Filter analyses down to actionable buy signals:
    - Score ≥ MIN_SCORE
    - RSI not overbought
    - Volume above threshold
    - Not already in a position
    - Long-only (bullish or neutral setup)
    """
    return [
        a for a in analyses
        if a.score >= config.MIN_SCORE
        and a.rsi < config.RSI_OVERBOUGHT
        and a.volume_ratio >= config.MIN_VOLUME_RATIO
        and a.trend in ("bullish", "neutral")
        and a.ticker not in existing_tickers
    ]
