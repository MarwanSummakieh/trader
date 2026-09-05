"""
Per-ticker deep analysis: computes all indicators, scores the setup 0–100,
generates human-readable signal text, and sets entry/stop/target levels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import pytz

from . import indicators as ind
import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class Analysis:
    ticker: str
    asset_type: str           # "stock" | "crypto"
    price: float

    # Score
    score: int                # 0–100 composite
    score_breakdown: dict     # component → pts

    # Signal text
    signals: list[str]

    # Levels
    entry: float
    stop_loss: float
    take_profit: float

    # Raw indicators (for display)
    rsi: float
    macd_hist: float
    volume_ratio: float
    atr: float
    ema9: float
    ema21: float
    vwap_val: float
    adx_val: float
    bb_pct: float             # 0 = at lower band, 1 = at upper band
    trend: str                # "bullish" | "bearish" | "neutral"
    regime_ok: bool = True    # price above daily EMA50 (long regime gate)
    # Crypto entry feature: last completed close above the prior
    # CRYPTO_BREAKOUT_BARS-bar high (False during warmup — fails closed).
    breakout_ok: bool = False
    # Stock volatility gate: True when the ATR term clears ATR_GATE_MULT of
    # the STOP_LOSS_PCT floor — i.e. intraday vol is high enough for the R
    # geometry to work. False during ATR warmup — fails closed.
    atr_binding: bool = False
    # Which entry family produced the levels below. The scanner rewrites a
    # momentum-failed stock Analysis into a "swing" one (wide stop) when the
    # swing rule fires; the ledger records it so exits can tell them apart.
    strategy: str = "momentum"
    # Swing (RSI(2) pullback) features from COMPLETED daily bars only:
    # swing_ok = prior-close RSI(2) < SWING_RSI2_MAX and close > 200d SMA;
    # swing_exit_level = mean of the prior 4 closes (price above it at the
    # session end == today's close would lift the 5-day SMA -> exit).
    swing_ok: bool = False
    swing_exit_level: float = float("nan")

    timestamp: datetime = field(default_factory=lambda: datetime.now(ET))

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def risk_reward(self) -> float:
        return round(self.reward / self.risk, 2) if self.risk > 0 else 0.0


def analyze(
    ticker: str,
    intraday_df: Optional[pd.DataFrame],
    daily_df: Optional[pd.DataFrame],
    asset_type: str = "stock",
) -> Optional[Analysis]:
    """Return a full Analysis or None if data is inadequate."""
    if intraday_df is None or daily_df is None:
        return None
    if len(intraday_df) < 30 or len(daily_df) < 50:
        return None

    intra = intraday_df.copy()
    daily = daily_df.copy()

    now = datetime.now(ET)

    # Drop the in-progress bar: indicators on a partial candle repaint, and its
    # volume is systematically understated. Evaluate the last *completed* bar.
    if len(intra) >= 2:
        idx = intra.index.tz_convert(ET)
        bar_interval = pd.Timedelta(minutes=5)  # scanner always requests 5m bars
        if now < idx[-1] + bar_interval:
            intra = intra.iloc[:-1]
    if len(intra) < 30:
        return None

    # Daily bars are labeled by their session date (UTC for crypto).
    # Exclude the signal day's daily candle even when replaying an older scan.
    context_tz = "UTC" if asset_type == "crypto" else ET
    signal_day = intra.index.tz_convert(context_tz)[-1].date()
    daily = daily[[d.date() < signal_day for d in daily.index]]
    if len(daily) < 50:
        return None

    price = float(intra["Close"].iloc[-1])

    if asset_type == "stock":
        # Penny-stock and liquidity filters (crypto is exempt — many majors
        # trade under $5 and volume units aren't comparable to shares).
        if price < config.MIN_PRICE:
            return None
        if float(daily["Volume"].tail(20).mean()) < config.MIN_AVG_DAILY_VOLUME:
            return None
    last_bar_age = now - intra.index.tz_convert(ET)[-1]
    if last_bar_age.total_seconds() > config.MAX_DATA_AGE_MINUTES * 60:
        return None

    # ── Intraday indicators ───────────────────────────────────────────────
    intra["rsi"] = ind.rsi(intra["Close"], config.RSI_PERIOD)
    intra["macd"], intra["macd_sig"], intra["macd_hist"] = ind.macd(
        intra["Close"], config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL_PERIOD
    )
    intra["ema9"] = ind.ema(intra["Close"], config.EMA_FAST)
    intra["ema21"] = ind.ema(intra["Close"], config.EMA_MID)
    intra["bb_up"], intra["bb_mid"], intra["bb_low"] = ind.bollinger_bands(intra["Close"])
    intra["atr"] = ind.atr(intra["High"], intra["Low"], intra["Close"], config.ATR_PERIOD)

    # VWAP from today's bars only (resets each session)
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    today_mask = intra.index.tz_convert(ET).strftime("%Y-%m-%d") == today_str
    if today_mask.sum() >= 5:
        today_bars = intra[today_mask].copy()
        today_bars["vwap"] = ind.vwap(
            today_bars["High"], today_bars["Low"], today_bars["Close"], today_bars["Volume"]
        )
        intra.loc[today_mask, "vwap"] = today_bars["vwap"]
    else:
        intra["vwap"] = ind.vwap(intra["High"], intra["Low"], intra["Close"], intra["Volume"])

    intra["vol_ratio"] = ind.volume_ratio(intra["Volume"], config.VOLUME_LOOKBACK)

    # Prior-N-bar high for the crypto breakout entry (see scanner). shift(1)
    # keeps the current bar out of its own breakout level.
    prior_high = intra["High"].rolling(config.CRYPTO_BREAKOUT_BARS).max().shift(1)

    # ── Daily indicators for trend context ───────────────────────────────
    daily["ema50"] = ind.ema(daily["Close"], config.EMA_SLOW)
    daily["adx"] = ind.adx(daily["High"], daily["Low"], daily["Close"], config.ADX_PERIOD)

    # ── Extract latest values (safe NaN fallbacks) ────────────────────────
    def _f(series: pd.Series, fallback: float) -> float:
        v = series.iloc[-1]
        return float(v) if pd.notna(v) else fallback

    rsi_val = _f(intra["rsi"], 50.0)
    macd_hist_val = _f(intra["macd_hist"], 0.0)
    ema9_val = _f(intra["ema9"], price)
    ema21_val = _f(intra["ema21"], price)
    vwap_val = _f(intra["vwap"], price) if "vwap" in intra else price
    atr_val = _f(intra["atr"], price * 0.02)
    vol_ratio_val = _f(intra["vol_ratio"], 1.0)
    ema50_val = _f(daily["ema50"], price)
    adx_val = _f(daily["adx"], 15.0)
    bb_up = _f(intra["bb_up"], price * 1.02)
    bb_low_val = _f(intra["bb_low"], price * 0.98)
    # NaN (warmup) falls back to +inf so an unknown breakout level blocks
    # entries — the safe direction.
    breakout_ok = price > _f(prior_high, float("inf"))

    bb_range = bb_up - bb_low_val
    bb_pct = (price - bb_low_val) / bb_range if bb_range > 0 else 0.5

    # ── Trend ─────────────────────────────────────────────────────────────
    if price > ema9_val > ema21_val and price > ema50_val:
        trend = "bullish"
    elif price < ema9_val < ema21_val and price < ema50_val:
        trend = "bearish"
    else:
        trend = "neutral"

    # ── Score ─────────────────────────────────────────────────────────────
    score, breakdown = _score(
        rsi=rsi_val, macd_hist=macd_hist_val, vol_ratio=vol_ratio_val,
        price=price, ema9=ema9_val, ema21=ema21_val, vwap=vwap_val,
        adx=adx_val, bb_pct=bb_pct, trend=trend,
    )

    # ── Signal text ───────────────────────────────────────────────────────
    signals = _signals(intra, rsi_val, macd_hist_val, vol_ratio_val,
                       price, ema9_val, ema21_val, vwap_val, adx_val)
    if asset_type == "crypto" and breakout_ok:
        signals.insert(0, f"{config.CRYPTO_BREAKOUT_BARS * 5 / 60:g}h-high breakout")

    # ── Levels: ATR-based stop, R-multiple target ─────────────────────────
    stop_loss = price - max(atr_val * 1.5, price * config.STOP_LOSS_PCT)
    take_profit = price + (price - stop_loss) * config.TAKE_PROFIT_R_MULT

    # ATR gate: computed from the RAW series, not atr_val — the NaN fallback
    # above (price * 2%) would otherwise make warmup bars pass the gate.
    atr_raw = intra["atr"].iloc[-1]
    atr_binding = bool(
        pd.notna(atr_raw)
        and float(atr_raw) * 1.5 > price * config.STOP_LOSS_PCT * config.ATR_GATE_MULT
    )
    if asset_type == "stock" and not atr_binding:
        signals.append("Low ATR — stop pinned at floor (no-entry vol)")

    # ── Swing features: last COMPLETED daily bars only ────────────────────
    # yfinance includes today's in-progress daily row during the session;
    # the rule is defined on the prior close, so drop anything dated today.
    swing_ok, swing_level = False, float("nan")
    if asset_type == "stock":
        d_dates = [d.date() if hasattr(d, "date") else d for d in daily.index]
        comp = daily["Close"][[d < now.date() for d in d_dates]]
        if len(comp) >= 200:
            rsi2 = ind.rsi(comp, 2).iloc[-1]
            sma200 = comp.rolling(200).mean().iloc[-1]
            swing_level = float(comp.tail(4).mean())
            swing_ok = bool(
                pd.notna(rsi2) and pd.notna(sma200)
                and float(rsi2) < config.SWING_RSI2_MAX
                and float(comp.iloc[-1]) > float(sma200)
            )
            if swing_ok:
                signals.append(f"RSI(2) {float(rsi2):.0f} pullback in 200d uptrend (swing)")

    return Analysis(
        ticker=ticker, asset_type=asset_type, price=price,
        score=score, score_breakdown=breakdown, signals=signals,
        entry=price, stop_loss=stop_loss, take_profit=take_profit,
        rsi=rsi_val, macd_hist=macd_hist_val, volume_ratio=vol_ratio_val,
        atr=atr_val, ema9=ema9_val, ema21=ema21_val,
        vwap_val=vwap_val, adx_val=adx_val, bb_pct=bb_pct, trend=trend,
        # NaN EMA50 falls back to price above, making this False — blocking
        # entries when the daily trend is unknown is the safe direction.
        regime_ok=price > ema50_val,
        breakout_ok=breakout_ok,
        atr_binding=atr_binding,
        swing_ok=swing_ok,
        swing_exit_level=swing_level,
    )


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(
    rsi, macd_hist, vol_ratio, price, ema9, ema21, vwap, adx, bb_pct, trend
) -> tuple[int, dict]:
    """
    100-point composite score for a long day-trade setup.

    Momentum   (30 pts): EMA alignment + trend direction
    Volume     (25 pts): surge above average
    Technical  (25 pts): RSI sweet spot + MACD
    Trend qual (20 pts): ADX strength + price vs VWAP
    """
    # Momentum (30)
    mom = 0
    if price > ema9:
        mom += 12
    if ema9 > ema21:
        mom += 10
    if trend == "bullish":
        mom += 8

    # Volume (25)
    if vol_ratio >= 3.0:
        vol = 25
    elif vol_ratio >= 2.0:
        vol = 20
    elif vol_ratio >= 1.5:
        vol = 13
    elif vol_ratio >= 1.0:
        vol = 6
    else:
        vol = 0

    # Technical (25)
    tech = 0
    if 45 <= rsi <= 65:
        tech += 15    # ideal: momentum but not extended
    elif 35 <= rsi < 45 or 65 < rsi <= 70:
        tech += 8
    if macd_hist > 0:
        tech += 10

    # Trend quality (20)
    tq = 0
    if adx > 30:
        tq += 10
    elif adx > 20:
        tq += 6
    if price > vwap:
        tq += 10

    breakdown = {
        "momentum": min(mom, 30),
        "volume": min(vol, 25),
        "technical": min(tech, 25),
        "trend_quality": min(tq, 20),
    }
    breakdown["total"] = sum(breakdown.values())
    return breakdown["total"], breakdown


def _signals(df, rsi, macd_hist, vol_ratio, price, ema9, ema21, vwap, adx) -> list[str]:
    out = []

    # MACD crossover (histogram just flipped positive)
    if len(df) >= 2 and "macd_hist" in df.columns:
        prev = df["macd_hist"].iloc[-2]
        if pd.notna(prev) and float(prev) < 0 < macd_hist:
            out.append("MACD bullish cross")

    # Volume
    if vol_ratio >= 2.5:
        out.append(f"Vol surge {vol_ratio:.1f}x avg")
    elif vol_ratio >= 1.5:
        out.append(f"Above-avg vol {vol_ratio:.1f}x")

    # EMA stack
    if price > ema9 > ema21:
        out.append("EMA 9 > 21 aligned")

    # VWAP
    if price > vwap:
        out.append("Above VWAP")

    # RSI
    if 50 <= rsi <= 65:
        out.append(f"RSI {rsi:.0f} (bullish zone)")
    elif rsi < config.RSI_OVERSOLD:
        out.append(f"RSI {rsi:.0f} (oversold bounce?)")

    # ADX
    if adx > 30:
        out.append(f"Strong trend (ADX {adx:.0f})")

    return out
