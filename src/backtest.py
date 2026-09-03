"""
Backtest engine: replays historical 5-minute bars through the same signal
logic the live bot uses (indicators, _score, filters, fills with slippage,
R-based trailing, EOD stock liquidation) so exit/entry parameters can be
tuned on evidence instead of defaults.

Fidelity notes (kept deliberately honest):
- Signals are computed on completed bars only; entries fill at the NEXT
  bar's open plus slippage (the live bot fills at signal price — next-open
  is slightly more pessimistic and more realistic).
- Exits are checked intrabar against the bar's low/high. If both stop and
  target are touched in the same bar, the stop is assumed to hit first.
- Daily-timeframe context (EMA50 / ADX) is shifted one day so a session
  never sees its own daily close.
- A position opened at bar t is first exit-checked at bar t+1 (the live
  monitor would catch it within 120 s; one 5 m bar is the closest analog).
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

from . import indicators as ind
from .analyzer import _score
from .fees import sell_regulatory_fee
import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


# ── Data fetching (cached) ────────────────────────────────────────────────────

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(level=1, axis=1)
    return df


def _cache_path(ticker: str, days: int) -> Path:
    stamp = datetime.now(ET).strftime("%Y%m%d")
    return CACHE_DIR / f"{ticker}_{days}d_{stamp}.pkl"


def fetch_history(
    ticker: str, days: int, refresh: bool = False
) -> Optional[tuple[pd.DataFrame, pd.DataFrame]]:
    """(intraday 5m, daily 2y) for one ticker, disk-cached per day."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = _cache_path(ticker, days)
    if path.exists() and not refresh:
        try:
            intra, daily = pd.read_pickle(path)
            return intra, daily
        except Exception:
            path.unlink(missing_ok=True)

    try:
        intra = yf.download(
            ticker, period=f"{days}d", interval="5m",
            progress=False, auto_adjust=True, multi_level_index=False,
        )
        daily = yf.download(
            ticker, period="2y", interval="1d",
            progress=False, auto_adjust=True, multi_level_index=False,
        )
        intra = _normalize(intra).dropna(subset=["Close", "Volume"])
        daily = _normalize(daily).dropna(subset=["Close", "Volume"])
        if len(intra) < 100 or len(daily) < 60:
            return None
        pd.to_pickle((intra, daily), path)
        return intra, daily
    except Exception as e:
        logger.warning("History fetch failed %s: %s", ticker, e)
        return None


def fetch_universe(
    stocks: list[str], crypto: list[str], days: int,
    refresh: bool = False, max_workers: int = 8,
) -> dict[str, tuple[str, pd.DataFrame, pd.DataFrame]]:
    """ticker → (asset_type, intraday, daily) for every fetchable symbol."""
    tasks = [(t, "stock") for t in stocks] + [(t, "crypto") for t in crypto]
    out: dict[str, tuple[str, pd.DataFrame, pd.DataFrame]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch_history, t, days, refresh): (t, at) for t, at in tasks}
        for fut in as_completed(futs):
            t, at = futs[fut]
            res = fut.result()
            if res is not None:
                out[t] = (at, res[0], res[1])
    return out


# ── Signal frame ──────────────────────────────────────────────────────────────

@dataclass
class SignalData:
    """Per-ticker numpy view of everything the simulator needs."""
    ticker: str
    asset_type: str
    ts: np.ndarray          # bar start times (tz-aware, ET) as object array
    ts_pos: dict            # timestamp → row index
    date: np.ndarray        # ET date per bar
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    score: np.ndarray       # int, -1 where indicators unavailable
    rsi: np.ndarray
    vol_ratio: np.ndarray
    trend_ok: np.ndarray    # bool: trend in (bullish, neutral)
    regime_ok: np.ndarray   # bool: close above (shifted) daily EMA50
    stop_dist: np.ndarray   # max(1.5*ATR, close*STOP_LOSS_PCT)
    # Extra features exposed for research / custom entry rules
    bb_pct: np.ndarray = None      # 0 = lower band, 1 = upper band
    adx: np.ndarray = None         # daily ADX (shifted)
    above_vwap: np.ndarray = None  # bool
    macd_pos: np.ndarray = None    # bool: MACD histogram > 0
    ema_aligned: np.ndarray = None # bool: close > ema9 > ema21
    brk_ok: np.ndarray = None      # bool: close > prior CRYPTO_BREAKOUT_BARS high
    atr_ok: np.ndarray = None      # bool: ATR term > ATR_GATE_MULT x stop floor (NaN → False)
    # Swing (RSI2 pullback) features, stocks only. Daily values are from the
    # last COMPLETED session (shifted one day), mapped onto every bar.
    bar_idx: np.ndarray = None       # int: bar ordinal within its session
    day_idx: np.ndarray = None       # int: session ordinal within the frame
    is_last_bar: np.ndarray = None   # bool: last bar of its session
    swing_rsi2: np.ndarray = None    # daily RSI(2) at the prior close (NaN = warmup)
    swing_regime: np.ndarray = None  # bool: prior close > 200-day SMA (NaN → False)
    swing_exit_level: np.ndarray = None  # mean of the prior 4 daily closes (NaN = warmup)
    # When set, this boolean mask REPLACES the score/rsi/vol/trend/regime
    # entry filters in simulate() (structural rules like EOD cutoff and
    # same-session checks still apply). Lets research scripts test arbitrary
    # entry rules without touching the simulator.
    signal: Optional[np.ndarray] = None


def build_signal_frame(
    ticker: str, asset_type: str, intra: pd.DataFrame, daily: pd.DataFrame
) -> Optional[SignalData]:
    """Vectorised, causal indicator computation mirroring src.analyzer."""
    df = intra.copy()
    idx_et = df.index.tz_convert(ET)

    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]

    rsi = ind.rsi(close, config.RSI_PERIOD)
    _, _, macd_hist = ind.macd(close, config.MACD_FAST, config.MACD_SLOW,
                               config.MACD_SIGNAL_PERIOD)
    ema9 = ind.ema(close, config.EMA_FAST)
    ema21 = ind.ema(close, config.EMA_MID)
    bb_up, _, bb_low = ind.bollinger_bands(close, config.BB_PERIOD, config.BB_STD)
    atr = ind.atr(high, low, close, config.ATR_PERIOD)
    vol_ratio = ind.volume_ratio(vol, config.VOLUME_LOOKBACK)

    # Session VWAP: cumulative within each ET date (causal)
    dates = pd.Series(idx_et.date, index=df.index)
    tp = (high + low + close) / 3
    pv = tp * vol
    vwap = pv.groupby(dates.values).cumsum() / vol.groupby(dates.values).cumsum().replace(0, np.nan)

    # Daily context, shifted one day so a session never sees its own close
    d_ema50 = ind.ema(daily["Close"], config.EMA_SLOW).shift(1)
    d_adx = ind.adx(daily["High"], daily["Low"], daily["Close"], config.ADX_PERIOD).shift(1)
    d_dates = [d.date() if hasattr(d, "date") else d for d in daily.index]
    ema50_map = dict(zip(d_dates, d_ema50.values))
    adx_map = dict(zip(d_dates, d_adx.values))
    ema50_col = dates.map(ema50_map).ffill()
    adx_col = dates.map(adx_map).ffill()

    c = close.to_numpy(float)
    e9 = ema9.to_numpy(float)
    e21 = ema21.to_numpy(float)
    e50 = np.where(np.isnan(ema50_col.to_numpy(float)), c, ema50_col.to_numpy(float))
    adx_v = np.where(np.isnan(adx_col.to_numpy(float)), 15.0, adx_col.to_numpy(float))
    rsi_v = np.where(np.isnan(rsi.to_numpy(float)), 50.0, rsi.to_numpy(float))
    mh_v = np.where(np.isnan(macd_hist.to_numpy(float)), 0.0, macd_hist.to_numpy(float))
    vr_v = np.where(np.isnan(vol_ratio.to_numpy(float)), 1.0, vol_ratio.to_numpy(float))
    vw_v = np.where(np.isnan(vwap.to_numpy(float)), c, vwap.to_numpy(float))
    atr_v = np.where(np.isnan(atr.to_numpy(float)), c * 0.02, atr.to_numpy(float))
    bu = np.where(np.isnan(bb_up.to_numpy(float)), c * 1.02, bb_up.to_numpy(float))
    bl = np.where(np.isnan(bb_low.to_numpy(float)), c * 0.98, bb_low.to_numpy(float))

    bb_range = bu - bl
    bb_pct = np.where(bb_range > 0, (c - bl) / np.where(bb_range > 0, bb_range, 1.0), 0.5)

    bullish = (c > e9) & (e9 > e21) & (c > e50)
    bearish = (c < e9) & (e9 < e21) & (c < e50)
    trend = np.where(bullish, "bullish", np.where(bearish, "bearish", "neutral"))

    n = len(df)
    warmup = 50  # matches the live minimum-history requirement
    score = np.full(n, -1, dtype=int)
    for i in range(warmup, n):
        # Exact same scoring function the live analyzer uses
        s, _ = _score(
            rsi=rsi_v[i], macd_hist=mh_v[i], vol_ratio=vr_v[i], price=c[i],
            ema9=e9[i], ema21=e21[i], vwap=vw_v[i], adx=adx_v[i],
            bb_pct=bb_pct[i], trend=trend[i],
        )
        score[i] = s

    if asset_type == "stock":
        score[c < config.MIN_PRICE] = -1
        if float(daily["Volume"].tail(20).mean()) < config.MIN_AVG_DAILY_VOLUME:
            return None

    # Swing features from completed daily bars (shift(1): a session only
    # ever sees the previous close). NaN comparisons are False -> fail closed.
    d_close = daily["Close"]
    d_rsi2 = ind.rsi(d_close, 2).shift(1)
    d_reg = (d_close > d_close.rolling(200).mean()).shift(1)
    d_lvl = (d_close.rolling(4).mean()).shift(1)
    rsi2_col = dates.map(dict(zip(d_dates, d_rsi2.values))).ffill().to_numpy(float)
    reg_col = dates.map(dict(zip(d_dates, d_reg.values))).ffill()
    reg_col = np.where(reg_col.isna().to_numpy(), False, reg_col.to_numpy()).astype(bool)
    lvl_col = dates.map(dict(zip(d_dates, d_lvl.values))).ffill().to_numpy(float)
    date_arr = dates.to_numpy()
    new_day = np.r_[True, date_arr[1:] != date_arr[:-1]]
    day_idx = np.cumsum(new_day) - 1
    bar_idx = np.arange(len(date_arr)) - np.maximum.accumulate(np.where(new_day, np.arange(len(date_arr)), 0))
    is_last = np.r_[new_day[1:], True]

    ts_list = list(idx_et)
    return SignalData(
        ticker=ticker, asset_type=asset_type,
        ts=np.array(ts_list, dtype=object),
        ts_pos={t: i for i, t in enumerate(ts_list)},
        date=np.array([t.date() for t in ts_list], dtype=object),
        open=df["Open"].to_numpy(float), high=high.to_numpy(float),
        low=low.to_numpy(float), close=c,
        score=score, rsi=rsi_v, vol_ratio=vr_v,
        trend_ok=~bearish, regime_ok=c > e50,
        stop_dist=np.maximum(atr_v * 1.5, c * config.STOP_LOSS_PCT),
        bb_pct=bb_pct, adx=adx_v, above_vwap=c > vw_v,
        macd_pos=mh_v > 0, ema_aligned=(c > e9) & (e9 > e21),
        # NaN comparison is False — warmup bars can never be breakouts.
        brk_ok=(close > high.rolling(config.CRYPTO_BREAKOUT_BARS).max().shift(1)).to_numpy(),
        # Raw ATR series, NOT atr_v — its warmup fallback (2% of price)
        # would wrongly pass the gate. NaN comparison is False (fail closed).
        atr_ok=(atr * 1.5 > close * config.STOP_LOSS_PCT * config.ATR_GATE_MULT).to_numpy(),
        bar_idx=bar_idx, day_idx=day_idx, is_last_bar=is_last,
        swing_rsi2=rsi2_col, swing_regime=reg_col, swing_exit_level=lvl_col,
    )


# ── Simulation ────────────────────────────────────────────────────────────────

@dataclass
class SimParams:
    # Entry rule (mirrors src.scanner.get_buy_candidates)
    entry_rsi_min: float = config.ENTRY_RSI_MIN
    entry_rsi_max: float = config.ENTRY_RSI_MAX
    entry_adx_min: float = config.ENTRY_ADX_MIN
    require_uptrend: bool = config.REQUIRE_DAILY_UPTREND
    require_atr_stop: bool = config.REQUIRE_ATR_STOP
    stock_entry_cutoff: Optional[dtime] = config.STOCK_ENTRY_CUTOFF
    # Force-flat stocks at EOD_CLOSE_TIME (day-trading mode). Off = positions
    # are held overnight and exit only on stop / target / trail.
    eod_close_stocks: bool = config.EOD_CLOSE_STOCKS
    # Stock swing entry (mirrors src.scanner._swing_entry_ok / portfolio)
    swing_enabled: bool = config.SWING_ENABLED
    swing_rsi2_max: float = config.SWING_RSI2_MAX
    swing_stop_pct: float = config.SWING_STOP_PCT
    swing_max_hold_days: int = config.SWING_MAX_HOLD_DAYS
    swing_entry_bars: int = config.SWING_ENTRY_BARS
    swing_position_size_pct: float = config.SWING_POSITION_SIZE_PCT
    swing_exit_mode: str = "eod"     # "eod": rule checked at the 15:50 close; "intraday": every bar
    # Crypto entry rule (mirrors src.scanner._crypto_entry_ok)
    crypto_min_vol_ratio: float = config.CRYPTO_MIN_VOL_RATIO
    crypto_require_btc_uptrend: bool = config.CRYPTO_REQUIRE_BTC_UPTREND
    # Exits & sizing
    take_profit_r_mult: float = config.TAKE_PROFIT_R_MULT
    trail_trigger_r: float = config.PROFIT_TRAIL_TRIGGER_R
    trail_distance_r: float = config.PROFIT_TRAIL_DISTANCE_R
    position_size_pct: float = config.POSITION_SIZE_PCT
    max_positions: int = config.MAX_POSITIONS
    fee_slippage_pct: float = config.FEE_SLIPPAGE_PCT
    # Alpaca sell-side regulatory fees (commission is $0); see src/fees.py.
    sec_fee_rate: float = config.ALPACA_SEC_FEE_RATE
    finra_taf_per_share: float = config.ALPACA_FINRA_TAF_PER_SHARE
    finra_taf_cap: float = config.ALPACA_FINRA_TAF_CAP
    starting_capital: float = config.STARTING_CAPITAL
    max_hold_bars: Optional[int] = None         # time-box: exit at close after N bars
    # CFD-style leverage: exposure = committed margin * leverage. The broker
    # force-closes a position when its loss reaches margin_call_loss of the
    # committed margin; the account is bust if total equity reaches zero.
    leverage: float = 1.0
    margin_call_loss: float = 0.9

    def label(self) -> str:
        regime = "regime ON" if self.require_uptrend else "regime OFF"
        gate = "atr-gate ON" if self.require_atr_stop else "atr-gate OFF"
        eod = "eod-flat ON" if self.eod_close_stocks else "eod-flat OFF"
        swing = f"swing rsi2<{self.swing_rsi2_max:g}" if self.swing_enabled else "swing OFF"
        co = self.stock_entry_cutoff.strftime("%H:%M") if self.stock_entry_cutoff else "none"
        return (f"rsi{self.entry_rsi_min:g}-{self.entry_rsi_max:g} "
                f"adx>{self.entry_adx_min:g} cutoff={co} "
                f"tp={self.take_profit_r_mult}R "
                f"trig={self.trail_trigger_r}R dist={self.trail_distance_r}R "
                f"[{regime}, {gate}, {eod}, {swing}]")


@dataclass
class BTTrade:
    ticker: str
    asset_type: str
    score: int
    entry_ts: object
    entry_px: float
    qty: float
    stop: float
    tp: float
    r: float                      # initial risk per share
    strategy: str = "momentum"    # "momentum" | "swing" | "crypto"
    entry_i: int = -1             # row index of the entry bar in its frame
    margin: float = 0.0           # cash committed (== exposure at leverage 1)
    last_close: float = 0.0
    exit_ts: object = None
    exit_px: float = 0.0
    reason: str = ""
    fees: float = 0.0             # sell-side regulatory fees (SEC + FINRA TAF)

    @property
    def pnl(self) -> float:
        return (self.exit_px - self.entry_px) * self.qty - self.fees

    @property
    def r_multiple(self) -> float:
        return (self.exit_px - self.entry_px) / self.r if self.r > 0 else 0.0


@dataclass
class SimResult:
    params: SimParams
    trades: list[BTTrade]
    equity: pd.Series             # equity marked at every bar timestamp
    busted: bool = False          # account equity hit zero (leverage wipeout)


EOD_T = config.EOD_CLOSE_TIME     # 15:45 ET


def simulate(frames: dict[str, SignalData], params: SimParams) -> SimResult:
    all_ts = sorted({t for f in frames.values() for t in f.ts})
    tickers = list(frames)

    # BTC regime lookup for the crypto entry gate. Fails closed: no BTC
    # frame (or no BTC bar at that timestamp) means no crypto entries.
    btc_f = next((f for f in frames.values() if f.ticker == "BTC-USD"), None)

    def btc_ok(sig_ts) -> bool:
        if btc_f is None:
            return False
        k = btc_f.ts_pos.get(sig_ts)
        return bool(btc_f.regime_ok[k]) if k is not None else False

    capital = params.starting_capital
    open_pos: dict[str, BTTrade] = {}
    closed: list[BTTrade] = []
    eq_ts: list = []
    eq_val: list[float] = []
    fee = params.fee_slippage_pct

    def deployed() -> float:
        return sum(p.margin for p in open_pos.values())

    def close(pos: BTTrade, ts, raw_px: float, reason: str):
        nonlocal capital
        pos.exit_ts = ts
        pos.exit_px = raw_px * (1 - fee)
        pos.fees = sell_regulatory_fee(
            pos.exit_px, pos.qty,
            params.sec_fee_rate, params.finra_taf_per_share, params.finra_taf_cap,
        )
        pos.reason = reason
        capital += pos.pnl
        closed.append(pos)
        del open_pos[pos.ticker]

    for ts in all_ts:
        t_time = ts.time()

        # ── Exits (positions opened before this bar) ──────────────────────
        for tk in list(open_pos):
            f = frames[tk]
            i = f.ts_pos.get(ts)
            pos = open_pos[tk]
            if i is None:
                continue
            if i <= pos.entry_i:
                continue  # entry bar itself — first check next bar
            o, h, l, c = f.open[i], f.high[i], f.low[i], f.close[i]
            pos.last_close = c

            if (params.eod_close_stocks and pos.asset_type == "stock"
                    and t_time >= EOD_T):
                close(pos, ts, o, "eod_close")
                continue
            if pos.strategy == "swing":
                # Rule exits decided on the previous bar's close, filled at
                # this bar's open (a market order). Sessions after entry only.
                held = f.day_idx[i] - f.day_idx[pos.entry_i]
                if held >= 1:
                    k = i - 1
                    lvl = f.swing_exit_level[k]
                    above = (not math.isnan(lvl)) and f.close[k] > lvl
                    check = f.is_last_bar[i] if params.swing_exit_mode == "eod" else True
                    if check and above and f.day_idx[k] == f.day_idx[i]:
                        close(pos, ts, o, "swing_exit")
                        continue
                    if held >= params.swing_max_hold_days and f.is_last_bar[i]:
                        close(pos, ts, o, "time_exit")
                        continue
            # Broker liquidation level: loss = margin_call_loss * margin.
            # Sits far below the stop at low leverage; above it when levered
            # enough that the margin runs out before the stop is reached.
            mc_price = pos.entry_px * (1 - params.margin_call_loss / params.leverage)
            eff_stop = max(pos.stop, mc_price)
            if pos.stop >= pos.entry_px:
                stop_reason = "trail_stop"
            elif mc_price > pos.stop:
                stop_reason = "margin_call"
            else:
                stop_reason = "stop_loss"
            # Stop first (pessimistic when both stop and target are touched)
            if o <= eff_stop:
                close(pos, ts, o, stop_reason)
                continue
            if l <= eff_stop:
                close(pos, ts, eff_stop, stop_reason)
                continue
            if o >= pos.tp:
                close(pos, ts, o, "take_profit")
                continue
            if h >= pos.tp:
                close(pos, ts, pos.tp, "take_profit")
                continue
            if params.max_hold_bars is not None and i - pos.entry_i >= params.max_hold_bars:
                close(pos, ts, c, "time_exit")
                continue
            # Trailing update from this bar's close, effective next bar
            gain = c - pos.entry_px
            if pos.r > 0 and gain >= params.trail_trigger_r * pos.r:
                new_stop = c - params.trail_distance_r * pos.r
                if new_stop > pos.stop:
                    pos.stop = new_stop

        # ── Entries (signal from the previous completed bar) ──────────────
        candidates: list[tuple[int, str, int, str]] = []  # (score, ticker, row, strategy)
        for tk in tickers:
            if tk in open_pos:
                continue
            f = frames[tk]
            i = f.ts_pos.get(ts)
            if i is None or i == 0:
                continue
            j = i - 1  # signal bar
            strat = "momentum" if f.asset_type == "stock" else "crypto"
            if f.signal is not None:
                if not f.signal[j]:
                    continue
            elif f.asset_type == "crypto":
                # Mirrors src.scanner._crypto_entry_ok (regime-gated breakout)
                if f.score[j] < 0:                      # indicator warmup
                    continue
                if f.brk_ok is None or not f.brk_ok[j]:
                    continue
                if not f.regime_ok[j]:
                    continue
                if f.vol_ratio[j] < params.crypto_min_vol_ratio:
                    continue
                if params.crypto_require_btc_uptrend and not btc_ok(f.ts[j]):
                    continue
            else:
                # Mirrors src.scanner.get_buy_candidates: the momentum rule
                # first, then the swing rule for names that failed it.
                if _momentum_signal(f, j, params):
                    pass
                elif _swing_signal(f, j, params):
                    strat = "swing"
                else:
                    continue
            if f.asset_type == "stock":
                # No late entries. With EOD liquidation on, nothing may open
                # at/after the flatten time; with it off the only cutoff is
                # the configured one (None = any bar of the session). Swing
                # entries have their own (morning) window instead.
                cutoff = params.stock_entry_cutoff
                if cutoff is None and params.eod_close_stocks:
                    cutoff = EOD_T
                if strat != "swing" and cutoff is not None and t_time >= cutoff:
                    continue
                if f.date[j] != f.date[i]:   # signal must be same session
                    continue
            candidates.append((int(f.score[j]), tk, i, strat))

        candidates.sort(reverse=True)
        for sc, tk, i, strat in candidates:
            if len(open_pos) >= params.max_positions:
                break
            size = capital * (params.swing_position_size_pct if strat == "swing"
                              else params.position_size_pct)
            avail = capital - deployed()
            if avail < size * 0.5:
                break
            f = frames[tk]
            j = i - 1
            entry_fill = f.open[i] * (1 + fee)
            dist = (f.close[j] * params.swing_stop_pct if strat == "swing"
                    else f.stop_dist[j])
            stop = f.close[j] - dist                   # anchored to signal bar
            tp = f.close[j] + dist * params.take_profit_r_mult
            if entry_fill <= stop:                     # gapped through the stop
                continue
            margin = min(size, avail * 0.95)
            qty = margin * params.leverage / entry_fill
            r = (tp - entry_fill) / params.take_profit_r_mult
            open_pos[tk] = BTTrade(
                ticker=tk, asset_type=f.asset_type, score=sc, entry_ts=ts,
                entry_px=entry_fill, qty=qty, stop=stop, tp=tp, r=r,
                strategy=strat, entry_i=i, margin=margin, last_close=f.close[i],
            )

        eq_ts.append(ts)
        equity = capital + sum(
            (p.last_close - p.entry_px) * p.qty for p in open_pos.values()
        )
        eq_val.append(max(equity, 0.0))
        if equity <= 0:
            # Account blown — liquidate everything at last marks and stop.
            for tk in list(open_pos):
                pos = open_pos[tk]
                close(pos, ts, pos.last_close or pos.entry_px, "account_blown")
            return SimResult(params=params, trades=closed,
                             equity=pd.Series(eq_val, index=pd.DatetimeIndex(eq_ts)),
                             busted=True)

    # Liquidate whatever is still open at the last known close
    for tk in list(open_pos):
        pos = open_pos[tk]
        close(pos, all_ts[-1], pos.last_close or pos.entry_px, "backtest_end")

    return SimResult(params=params, trades=closed,
                     equity=pd.Series(eq_val, index=pd.DatetimeIndex(eq_ts)))


def _momentum_signal(f: SignalData, j: int, params: SimParams) -> bool:
    """Mirrors src.scanner._stock_entry_ok (minus the time-of-day cutoff)."""
    if f.score[j] < 0:                                  # indicator warmup
        return False
    if params.require_atr_stop and (f.atr_ok is None or not f.atr_ok[j]):
        return False
    if not f.ema_aligned[j]:
        return False
    if f.adx[j] <= params.entry_adx_min:
        return False
    if not (params.entry_rsi_min <= f.rsi[j] < params.entry_rsi_max):
        return False
    if params.require_uptrend and not f.regime_ok[j]:
        return False
    return True


def _swing_signal(f: SignalData, j: int, params: SimParams) -> bool:
    """Mirrors src.scanner._swing_entry_ok: prior-close RSI(2) below the
    threshold in a 200-day uptrend, acted on within the first
    swing_entry_bars bars of the session. NaN warmup fails closed."""
    if not params.swing_enabled or f.swing_rsi2 is None:
        return False
    if f.score[j] < 0 or f.bar_idx[j] >= params.swing_entry_bars:
        return False
    v = f.swing_rsi2[j]
    return (not math.isnan(v)) and v < params.swing_rsi2_max and bool(f.swing_regime[j])


def slice_frames(
    frames: dict[str, SignalData], start=None, end=None
) -> dict[str, SignalData]:
    """Restrict frames to bars with start <= ET date < end (train/test splits).
    Indicator values were computed on the full history, so a test slice still
    has warm indicators — only the tradeable window is narrowed."""
    out: dict[str, SignalData] = {}
    for tk, f in frames.items():
        mask = np.ones(len(f.ts), dtype=bool)
        if start is not None:
            mask &= f.date >= start
        if end is not None:
            mask &= f.date < end
        if mask.sum() < 10:
            continue
        idx = np.where(mask)[0]
        ts_list = list(f.ts[idx])
        out[tk] = SignalData(
            ticker=f.ticker, asset_type=f.asset_type,
            ts=f.ts[idx], ts_pos={t: i for i, t in enumerate(ts_list)},
            date=f.date[idx], open=f.open[idx], high=f.high[idx],
            low=f.low[idx], close=f.close[idx], score=f.score[idx],
            rsi=f.rsi[idx], vol_ratio=f.vol_ratio[idx],
            trend_ok=f.trend_ok[idx], regime_ok=f.regime_ok[idx],
            stop_dist=f.stop_dist[idx], bb_pct=f.bb_pct[idx],
            adx=f.adx[idx], above_vwap=f.above_vwap[idx],
            macd_pos=f.macd_pos[idx], ema_aligned=f.ema_aligned[idx],
            brk_ok=f.brk_ok[idx] if f.brk_ok is not None else None,
            atr_ok=f.atr_ok[idx] if f.atr_ok is not None else None,
            signal=f.signal[idx] if f.signal is not None else None,
            **{k: (getattr(f, k)[idx] if getattr(f, k) is not None else None)
               for k in ("bar_idx", "day_idx", "is_last_bar", "swing_rsi2",
                         "swing_regime", "swing_exit_level")},
        )
    return out


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(res: SimResult) -> dict:
    trades = res.trades
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rs = [t.r_multiple for t in trades]

    eq = res.equity
    daily = eq.groupby(eq.index.date).last()
    daily_ret = daily.pct_change().dropna() * 100
    roll_max = eq.cummax()
    dd = ((eq - roll_max) / roll_max * 100).min() if len(eq) else 0.0

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1

    total = sum(pnls)
    by_strat: dict[str, dict] = {}
    for s in sorted({t.strategy for t in trades}):
        ts_ = [t for t in trades if t.strategy == s]
        by_strat[s] = {
            "trades": len(ts_),
            "avg_r": float(np.mean([t.r_multiple for t in ts_])),
            "total_pnl": sum(t.pnl for t in ts_),
            "win_rate": sum(1 for t in ts_ if t.pnl > 0) / len(ts_) * 100,
        }
    return {
        "by_strategy": by_strat,
        "trades": len(trades),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "total_pnl": total,
        "return_pct": total / res.params.starting_capital * 100,
        "expectancy": total / len(pnls) if pnls else 0.0,
        "avg_r": float(np.mean(rs)) if rs else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else math.inf,
        "max_dd_pct": float(dd),
        "mean_daily_pct": float(daily_ret.mean()) if len(daily_ret) else 0.0,
        "best_day_pct": float(daily_ret.max()) if len(daily_ret) else 0.0,
        "worst_day_pct": float(daily_ret.min()) if len(daily_ret) else 0.0,
        "days_ge_5pct": int((daily_ret >= 5).sum()),
        "n_days": len(daily_ret),
        "exit_reasons": reasons,
    }


def score_buckets(trades: list[BTTrade], width: int = 5) -> list[dict]:
    """Score-vs-outcome table: does a higher score actually earn more R?"""
    buckets: dict[int, list[BTTrade]] = {}
    for t in trades:
        buckets.setdefault(t.score // width * width, []).append(t)
    out = []
    for lo in sorted(buckets):
        ts = buckets[lo]
        rs = [t.r_multiple for t in ts]
        out.append({
            "bucket": f"{lo}–{lo + width - 1}",
            "trades": len(ts),
            "win_rate": sum(1 for t in ts if t.pnl > 0) / len(ts) * 100,
            "avg_r": float(np.mean(rs)),
            "total_pnl": sum(t.pnl for t in ts),
        })
    return out
