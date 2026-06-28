"""
Pure-function technical indicators.
All inputs are pd.Series; all outputs are pd.Series.
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger_bands(
    close: pd.Series, period: int = 20, std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, mid, lower)."""
    mid = close.rolling(period).mean()
    dev = close.rolling(period).std()
    return mid + std * dev, mid, mid - std * dev


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift()
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Intraday VWAP — meaningful only on same-day data."""
    tp = (high + low + close) / 3
    return (tp * volume).cumsum() / volume.cumsum()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index (trend strength, 0–100)."""
    up = high.diff()
    down = -low.diff()
    pos_dm = np.where((up > down) & (up > 0), up, 0.0)
    neg_dm = np.where((down > up) & (down > 0), down, 0.0)

    pos_dm = pd.Series(pos_dm, index=high.index)
    neg_dm = pd.Series(neg_dm, index=high.index)

    tr_val = atr(high, low, close, period=1)
    s_tr = tr_val.ewm(com=period - 1, adjust=False).mean()
    pdi = 100 * pos_dm.ewm(com=period - 1, adjust=False).mean() / s_tr.replace(0, np.nan)
    ndi = 100 * neg_dm.ewm(com=period - 1, adjust=False).mean() / s_tr.replace(0, np.nan)

    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(com=period - 1, adjust=False).mean()


def volume_ratio(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Current volume bar vs. N-bar rolling average."""
    avg = volume.rolling(lookback).mean()
    return volume / avg.replace(0, np.nan)
