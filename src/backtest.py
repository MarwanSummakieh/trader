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
- Bracket stops and targets are active on the entry bar, after its open.
- Historical earnings exclusions are not available in the OHLCV cache.
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
from .risk import position_quantity
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
    """(intraday 5m, daily 1y) for one ticker, disk-cached per day."""
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
            ticker, period="1y", interval="1d",
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
    atr_ok: np.ndarray = None      # bool: ATR term beats the stop floor (NaN → False)
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

    # Latest strictly earlier daily session, including dates missing from
    # the daily index (e.g. the current day is not downloaded yet).
    context_dates = pd.Series(
        df.index.tz_convert("UTC" if asset_type == "crypto" else ET).date,
        index=df.index,
    )
    daily = daily.sort_index()
    d_dates = np.array([d.date() for d in daily.index], dtype=object)
    prior = np.searchsorted(d_dates, context_dates.to_numpy(), side="left") - 1

    def prior_daily(values):
        if not len(values):
            return pd.Series(np.nan, index=df.index)
        return pd.Series(np.where(prior >= 0, values.to_numpy()[np.maximum(prior, 0)], np.nan),
                         index=df.index)

    ema50_col = prior_daily(ind.ema(daily["Close"], config.EMA_SLOW))
    adx_col = prior_daily(ind.adx(daily["High"], daily["Low"], daily["Close"], config.ADX_PERIOD))

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
        # Per-session liquidity from earlier daily bars, never the end of
        # the research window (which leaks future volume into past selection).
        liquid = prior_daily(daily["Volume"].rolling(20).mean()) >= config.MIN_AVG_DAILY_VOLUME
        score[~liquid.to_numpy()] = -1

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
        atr_ok=(atr * 1.5 > close * config.STOP_LOSS_PCT).to_numpy(),
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
    # Crypto entry rule (mirrors src.scanner._crypto_entry_ok)
    crypto_min_vol_ratio: float = config.CRYPTO_MIN_VOL_RATIO
    crypto_require_btc_uptrend: bool = config.CRYPTO_REQUIRE_BTC_UPTREND
    # Exits & sizing
    take_profit_r_mult: float = config.TAKE_PROFIT_R_MULT
    trail_trigger_r: float = config.PROFIT_TRAIL_TRIGGER_R
    trail_distance_r: float = config.PROFIT_TRAIL_DISTANCE_R
    position_size_pct: float = config.POSITION_SIZE_PCT
    max_positions: int = config.MAX_POSITIONS
    risk_per_trade_pct: float = config.RISK_PER_TRADE_PCT
    max_portfolio_risk_pct: float = config.MAX_PORTFOLIO_RISK_PCT
    crypto_max_capital_pct: float = config.CRYPTO_MAX_CAPITAL_PCT
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
        co = self.stock_entry_cutoff.strftime("%H:%M") if self.stock_entry_cutoff else "EOD"
        return (f"rsi{self.entry_rsi_min:g}-{self.entry_rsi_max:g} "
                f"adx>{self.entry_adx_min:g} cutoff={co} "
                f"tp={self.take_profit_r_mult}R "
                f"trig={self.trail_trigger_r}R dist={self.trail_distance_r}R "
                f"[{regime}, {gate}]")


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
        return self.pnl / (self.r * self.qty) if self.r > 0 and self.qty > 0 else 0.0


@dataclass
class SimResult:
    params: SimParams
    trades: list[BTTrade]
    equity: pd.Series             # equity marked at every bar timestamp
    busted: bool = False          # account equity hit zero (leverage wipeout)


EOD_T = config.EOD_CLOSE_TIME     # 15:45 ET


def simulate(frames: dict[str, SignalData], params: SimParams) -> SimResult:
    all_ts = sorted({t for f in frames.values() for t in f.ts})
    tickers = sorted(frames)

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
        ) if pos.asset_type == "stock" else 0.0
        pos.reason = reason
        capital += pos.pnl
        closed.append(pos)
        del open_pos[pos.ticker]

    def check_exit(tk, ts, i, open_only=False):
        f = frames[tk]
        pos = open_pos[tk]
        o, h, l, c = f.open[i], f.high[i], f.low[i], f.close[i]
        if not open_only:
            pos.last_close = c

        if pos.asset_type == "stock" and (ts.time() >= EOD_T or ts.date() != pos.entry_ts.date()):
            close(pos, ts, o, "eod_close")
            return
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
            return
        if o >= pos.tp:
            close(pos, ts, o, "take_profit")
            return
        if open_only:
            return
        if l <= eff_stop:
            close(pos, ts, eff_stop, stop_reason)
            return
        if h >= pos.tp:
            close(pos, ts, pos.tp, "take_profit")
            return
        if params.max_hold_bars is not None and i - pos.entry_i >= params.max_hold_bars:
            close(pos, ts, c, "time_exit")
            return
        # Trailing update from this bar's close, effective next bar
        gain = c - pos.entry_px
        if pos.r > 0 and gain >= params.trail_trigger_r * pos.r:
            new_stop = c - params.trail_distance_r * pos.r
            if new_stop > pos.stop:
                pos.stop = new_stop


    for ts in all_ts:
        t_time = ts.time()
        exited = set()
        for tk in list(open_pos):
            i = frames[tk].ts_pos.get(ts)
            if i is not None:
                check_exit(tk, ts, i, open_only=True)
                if tk not in open_pos:
                    exited.add(tk)

        # ── Entries (signal from the previous completed bar) ──────────────
        candidates: list[tuple[int, str, int]] = []  # (score, ticker, row)
        for tk in tickers:
            if tk in open_pos or tk in exited:
                continue
            f = frames[tk]
            i = f.ts_pos.get(ts)
            if i is None or i == 0:
                continue
            j = i - 1  # signal bar
            if ts - f.ts[j] > pd.Timedelta(minutes=config.MAX_DATA_AGE_MINUTES):
                continue
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
                # Mirrors src.scanner._stock_entry_ok
                if f.score[j] < 0:                      # indicator warmup
                    continue
                if params.require_atr_stop and (
                    f.atr_ok is None or not f.atr_ok[j]
                ):
                    continue
                if not f.ema_aligned[j]:
                    continue
                if f.adx[j] <= params.entry_adx_min:
                    continue
                if not (params.entry_rsi_min <= f.rsi[j] < params.entry_rsi_max):
                    continue
                if params.require_uptrend and not f.regime_ok[j]:
                    continue
            if f.asset_type == "stock":
                cutoff = params.stock_entry_cutoff or EOD_T
                if t_time < config.MARKET_OPEN or t_time >= min(cutoff, EOD_T):         # no late entries
                    continue
                if f.date[j] != f.date[i]:   # signal must be same session
                    continue
            candidates.append((int(f.score[j]), tk, i))

        candidates.sort(reverse=True)
        for sc, tk, i in candidates:
            if len(open_pos) >= params.max_positions:
                break
            size = capital * params.position_size_pct
            avail = capital - deployed()
            if avail < size * 0.5:
                break
            f = frames[tk]
            j = i - 1
            entry_fill = f.open[i] * (1 + fee)
            stop = f.close[j] - f.stop_dist[j]         # anchored to signal bar
            tp = f.close[j] + f.stop_dist[j] * params.take_profit_r_mult
            if entry_fill <= stop:                     # gapped through the stop
                continue
            reserved_risk = sum(
                (p.r + max(0.0, p.entry_px - p.r) * fee) * p.qty
                + (sell_regulatory_fee(max(0.0, p.entry_px - p.r), p.qty,
                    params.sec_fee_rate, params.finra_taf_per_share, params.finra_taf_cap)
                   if p.asset_type == "stock" else 0.0)
                for p in open_pos.values()
            )
            qty = position_quantity(
                capital=capital, available=avail, entry=entry_fill, stop=stop, target=tp,
                leverage=params.leverage, position_pct=params.position_size_pct,
                risk_pct=params.risk_per_trade_pct,
                portfolio_risk_pct=params.max_portfolio_risk_pct,
                open_risk=reserved_risk, slippage=fee,
                sell_fee_per_share=(sell_regulatory_fee(stop, 1.0, params.sec_fee_rate,
                    params.finra_taf_per_share, params.finra_taf_cap)
                    if f.asset_type == "stock" else 0.0),
            )
            if qty <= 0:
                continue
            margin = qty * entry_fill / params.leverage
            if f.asset_type == "crypto" and margin + sum(
                p.margin for p in open_pos.values() if p.asset_type == "crypto"
            ) > capital * params.crypto_max_capital_pct:
                continue
            r = entry_fill - stop
            open_pos[tk] = BTTrade(
                ticker=tk, asset_type=f.asset_type, score=sc, entry_ts=ts,
                entry_px=entry_fill, qty=qty, stop=stop, tp=tp, r=r,
                entry_i=i, margin=margin, last_close=f.close[i],
            )

        # Intrabar exits occur AFTER every open fill. They cannot release cash
        # or a position slot retroactively for another entry at this bar's open.
        for tk in list(open_pos):
            i = frames[tk].ts_pos.get(ts)
            if i is not None:
                check_exit(tk, ts, i)

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
            eq_val[-1] = capital
            return SimResult(params=params, trades=closed,
                             equity=pd.Series(eq_val, index=pd.DatetimeIndex(eq_ts)),
                             busted=True)

    # Liquidate whatever is still open at the last known close
    for tk in list(open_pos):
        pos = open_pos[tk]
        close(pos, all_ts[-1], pos.last_close or pos.entry_px, "backtest_end")

    if eq_val:
        eq_val[-1] = capital  # final exit slippage/fees belong in the equity curve
    return SimResult(params=params, trades=closed,
                     equity=pd.Series(eq_val, index=pd.DatetimeIndex(eq_ts), dtype=float))


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
        if not mask.any():
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
            stop_dist=f.stop_dist[idx], bb_pct=f.bb_pct[idx] if f.bb_pct is not None else None,
            adx=f.adx[idx] if f.adx is not None else None, above_vwap=f.above_vwap[idx] if f.above_vwap is not None else None,
            macd_pos=f.macd_pos[idx] if f.macd_pos is not None else None, ema_aligned=f.ema_aligned[idx] if f.ema_aligned is not None else None,
            brk_ok=f.brk_ok[idx] if f.brk_ok is not None else None,
            atr_ok=f.atr_ok[idx] if f.atr_ok is not None else None,
            signal=f.signal[idx] if f.signal is not None else None,
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
    daily_ret = daily.pct_change() * 100
    if len(daily):
        daily_ret.iloc[0] = (daily.iloc[0] / res.params.starting_capital - 1) * 100
    roll_max = eq.cummax().clip(lower=res.params.starting_capital)
    dd = ((eq - roll_max) / roll_max * 100).min() if len(eq) else 0.0

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1

    total = sum(pnls)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "total_pnl": total,
        "return_pct": total / res.params.starting_capital * 100,
        "expectancy": total / len(pnls) if pnls else 0.0,
        "avg_r": float(np.mean(rs)) if rs else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else (math.inf if wins else 0.0),
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
