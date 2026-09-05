import os
from datetime import time
from dotenv import load_dotenv

load_dotenv()   # reads .env file (safe no-op if file doesn't exist)


def _env(name: str, default: str) -> str:
    """Read an env var defensively. Docker compose's env_file passes values
    VERBATIM (unlike python-dotenv), so surrounding quotes, inline comments,
    CRLF line endings and stray whitespace all end up inside the value — and
    a `float()`/`int()` on it crash-loops both containers at startup.
    Normalize here so local runs and containers behave identically."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    # Every legitimate value here is pure ASCII (numbers, tickers, URLs,
    # API keys) — drop BOMs and keyboard dead-key artifacts like '¨'.
    val = raw.encode("ascii", "ignore").decode()
    val = val.split(" #", 1)[0].strip()     # inline comment, whitespace, \r
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1].strip()
    return val if val else default

# Deploy stamp — bump when cutting a release (shown at bot startup, in the
# dashboard header, and in /api/status) so "which code is running?" is
# always answerable at a glance.
VERSION = "2026-09-06"

# --- Capital & Risk ---
# Historical research below used the older simulator. Its entry-bar exit,
# liquidity lookahead, and terminal-cost errors were fixed on 2026-09-06.
# Those historical performance claims require revalidation with the new engine.
# Defaults below are the previously researched set (2026-07-02, 59d of 5m bars,
# train/holdout split): entries = EMA-aligned momentum, daily ADX > 30,
# RSI 55-70, above daily EMA50, stocks before 12:00 ET only.
#
# Re-validated 2026-07-04 (59d window, train/holdout + walk-forward thirds;
# baseline: 395 trades, +0.14R/trade, 95% CI [+0.03, +0.26], P(edge<=0)=0.8%).
# Challengers tested and REJECTED — do not re-try without new data:
#   - volume-ratio entry filter (>=1.0/1.2/1.5): monotonically worse
#   - session-VWAP filter, MACD>0 filter: no OOS improvement
#   - skip-first-30-min: strong in-sample, failed holdout (unstable)
#   - relative-strength vs SPY: tiny OOS gain, -20% trades — not worth it
#   - SPY daily-uptrend gate: never fired (no bear regime in window) — untested
#   - time-boxed exits (2h/3h/4h): halve expectancy
#   - opening-range breakout family: profitable but 3x weaker than baseline
#   - RSI<35 dip-buy family: negative edge (-0.10R), matches crypto finding
# Exit grid re-confirmed: tp=3R trig=1.5R dist=1.5R sits on a robust plateau
# (neighbours within noise; tp 2-6R all positive).
#
# Re-validated 2026-08-20 (59d window ending 2026-08-19, train/holdout at
# 2026-07-23 + walk-forward thirds). The momentum edge DECAYED across the
# window (+0.15R -> +0.02R -> -0.15R per third); in the holdout month no
# tested long rule earned a single take-profit or trail exit. Fresh
# challenger families, all NEGATIVE on train — do not re-try without new
# data: EMA21-pullback reclaim, VWAP reclaim, RSI-30 dip buy, lower-band
# reclaim, opening-range breakout (retest), prior-day-high break, 8h-high
# breakout (stock port of the crypto rule), BB-squeeze breakout, gap-and-go,
# gap-fade, VWAP-hold, 3-bar momentum burst. Softening the stop floor to 1%
# so quiet tape stays tradeable also loses (fees/slippage dominate small R).
# What DID hold up is standing aside when volatility is too low for the R
# geometry to work — see REQUIRE_ATR_STOP below.
STARTING_CAPITAL = float(_env("STARTING_CAPITAL", "10000"))
POSITION_SIZE_PCT = float(_env("POSITION_SIZE_PCT", "0.15"))
# Notional remains capped above; wide stops now receive smaller positions.
# Limits include estimated exit slippage and stock sell fees. Gaps can exceed
# them. These are risk budgets, not claims of optimal or validated returns.
RISK_PER_TRADE_PCT = float(_env("RISK_PER_TRADE_PCT", "0.005"))
MAX_PORTFOLIO_RISK_PCT = float(_env("MAX_PORTFOLIO_RISK_PCT", "0.025"))
STOP_LOSS_PCT = float(_env("STOP_LOSS_PCT", "0.02"))       # floor for the ATR stop
TAKE_PROFIT_R_MULT = float(_env("TAKE_PROFIT_R_MULT", "3.0"))  # target = entry + R * this
# Validation showed 5 concurrent slots underperforms; >=7 is capital-limited anyway.
MAX_POSITIONS = int(_env("MAX_POSITIONS", "8"))

# Bid/ask spread + slippage on market fills, applied per side (entry fills
# higher, exit fills lower). 0.001 = 0.1% per side. Alpaca US equities are
# COMMISSION-FREE, so this now models spread/slippage only — and 0.1% is
# conservative for the liquid large-caps in this universe (real spreads are
# usually tighter), so the backtested edge is if anything understated.
FEE_SLIPPAGE_PCT = float(_env("FEE_SLIPPAGE_PCT", "0.001"))

# Alpaca regulatory pass-through fees — charged on SELLS only, buys are free.
# Tiny (cents per trade) but modeled so backtest/paper PnL is honest net of
# every real cost. Rates change periodically; override via .env when they do.
#   SEC fee:   a % of sell proceeds (2025 rate ≈ $27.80 per $1M = 0.0000278)
#   FINRA TAF: per share sold, capped per trade
ALPACA_SEC_FEE_RATE        = float(_env("ALPACA_SEC_FEE_RATE", "0.0000278"))
ALPACA_FINRA_TAF_PER_SHARE = float(_env("ALPACA_FINRA_TAF_PER_SHARE", "0.000166"))
ALPACA_FINRA_TAF_CAP       = float(_env("ALPACA_FINRA_TAF_CAP", "8.30"))

# CFD-style leverage: exposure = committed margin * LEVERAGE. A position is
# force-closed ("margin_call") when its loss reaches MARGIN_CALL_LOSS of the
# committed margin. Backtested 2026-07: returns scale ~linearly but drawdowns
# scale faster (2x → -15% train DD). Half-Kelly on backtest stats ≈ 1.5x;
# do not raise above 1.0 until live paper results confirm the backtest edge.
LEVERAGE = float(_env("LEVERAGE", "1.0"))
MARGIN_CALL_LOSS = float(_env("MARGIN_CALL_LOSS", "0.9"))

# --- Entry Rule (backtest-validated momentum, see get_buy_candidates) ---
ENTRY_RSI_MIN = float(_env("ENTRY_RSI_MIN", "55"))
ENTRY_RSI_MAX = float(_env("ENTRY_RSI_MAX", "70"))
ENTRY_ADX_MIN = float(_env("ENTRY_ADX_MIN", "30"))
# No stock entries at/after this ET time — the intraday drift edge needs
# hours to play out before the 15:45 EOD liquidation.
_cutoff = _env("STOCK_ENTRY_CUTOFF", "12:00").split(":")
STOCK_ENTRY_CUTOFF = time(int(_cutoff[0]), int(_cutoff[1]))

# Volatility regime gate (validated 2026-08-20): only enter a stock when the
# ATR term actually sets the stop — 1.5*ATR(5m) > STOP_LOSS_PCT * price.
# When intraday vol is below that, the stop is pinned at the 2% floor, "R"
# stops being volatility-scaled, and the 3R target sits several days of
# range away from a name that can't move 1% — those entries churned to EOD
# scratches all July/August (live and backtest agree). Gate behaviour on the
# 59d window: train +0.28R/126tr at the loosest useful setting through
# +0.22R/35tr at this exact one (every threshold in between positive, so it
# is a plateau, not a tuned point), and ZERO trades in the entire holdout
# month — the intended behaviour: in tape where every tested rule lost, the
# bot now stands aside. Expect (correct) multi-day flat stretches while
# volatility is dead. Sample honesty: the gated subset alone is 35 trades,
# CI [-0.20R, +0.65R] — treat this as preserving the validated edge's
# operating conditions plus capital preservation, not as fresh proven alpha.
REQUIRE_ATR_STOP = _env("REQUIRE_ATR_STOP", "1").lower() in ("1", "true", "yes")

# The bot runs as per-asset-class instances (docker-compose starts one of
# each): the stock instance keeps the defaults below, the crypto instance
# sets ENABLE_STOCKS=0 ENABLE_CRYPTO=1 with its own DB_PATH and capital.
ENABLE_STOCKS = _env("ENABLE_STOCKS", "1").lower() in ("1", "true", "yes")

# Crypto is off by default in the stock instance; the dedicated crypto
# instance (docker-compose bot-crypto) turns it on.
ENABLE_CRYPTO = _env("ENABLE_CRYPTO", "0").lower() in ("1", "true", "yes")

# --- Crypto entry rule: regime-gated 8h breakout (researched 2026-07-22) ---
# Four long-only families were tested on 59d of 5m bars (train/holdout
# split) and 1y of daily bars, in a tape where every universe symbol fell
# 45-80% over the year:
#   - stock momentum rule applied to crypto: -0.23R over 121 train trades
#   - 5m mean reversion (band reclaim / RSI turn / dip-buy): all ≈0 or worse
#   - daily Donchian trend-following (10/20/30/55d): -0.8R and worse — bear
#     rallies make new daily highs and then collapse
#   - THIS RULE (5m close > prior 8h high, own daily-EMA50 regime, BTC
#     daily-EMA50 regime, volume >= 1.2x): train +0.26R (13 trades),
#     holdout -0.13R (11 trades). Edge UNPROVEN — samples are tiny by
#     construction, because the gates keep it flat most of the time.
# Adopted anyway: it is strictly safer than the stock rule the crypto
# instance previously traded (max backtest DD ~2% vs ~8% steady bleed),
# and its exposure is near zero unless the coin AND BTC are in daily
# uptrends — the only condition under which ANY tested long rule made
# money. Treat live results as data collection, not a validated edge.
CRYPTO_BREAKOUT_BARS = int(_env("CRYPTO_BREAKOUT_BARS", "96"))   # 96 x 5m = 8h
CRYPTO_MIN_VOL_RATIO = float(_env("CRYPTO_MIN_VOL_RATIO", "1.2"))
# BTC is the regime driver for the whole asset class: no crypto entries
# while BTC is below its daily EMA50. Fails CLOSED — if BTC data is missing
# from a scan, no crypto entries that cycle.
CRYPTO_REQUIRE_BTC_UPTREND = _env("CRYPTO_REQUIRE_BTC_UPTREND", "1").lower() in ("1", "true", "yes")

# When crypto IS enabled it scans 24/7 while stocks only trade 9:30–16:00 ET,
# so overnight crypto entries would otherwise consume all buying power before
# the stock session opens. Crypto's total committed margin is therefore capped
# at this fraction of capital; the rest stays reserved for stocks.
# At 15%/position this allows 2 concurrent crypto positions.
CRYPTO_MAX_CAPITAL_PCT = float(_env("CRYPTO_MAX_CAPITAL_PCT", "0.30"))

# Skip stock entries on earnings reaction days (announcement day + next
# session). Tested 2026-07: such entries won 18% at -0.41R average.
EARNINGS_FILTER = _env("EARNINGS_FILTER", "1").lower() in ("1", "true", "yes")

# Composite score is kept for the dashboard/scan display only — it showed no
# predictive power in backtests and is NOT part of the entry rule.
MIN_SCORE = int(_env("MIN_SCORE", "65"))

# Regime gate: only take longs while price is above its daily EMA50.
# Backtested 2026-07: improved expectancy on both train and holdout splits.
REQUIRE_DAILY_UPTREND = _env("REQUIRE_DAILY_UPTREND", "1").lower() in ("1", "true", "yes")

# --- Trailing stop (R-based; R = entry-to-initial-stop distance) ---
# Once unrealized gain reaches PROFIT_TRAIL_TRIGGER_R * R, the stop trails
# PROFIT_TRAIL_DISTANCE_R * R below the observed price. It only ever moves up.
PROFIT_TRAIL_TRIGGER_R  = float(_env("PROFIT_TRAIL_TRIGGER_R",  "1.5"))
PROFIT_TRAIL_DISTANCE_R = float(_env("PROFIT_TRAIL_DISTANCE_R", "1.5"))
MIN_VOLUME_RATIO = 1.5
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MIN_PRICE = 5.0                    # stocks only — crypto is exempt
MIN_AVG_DAILY_VOLUME = 500_000     # stocks only — 20-day average share volume
MAX_DATA_AGE_MINUTES = 15          # reject stale stock AND crypto signals

# --- Market Hours (US Eastern Time) ---
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EOD_CLOSE_TIME = time(15, 45)

# --- Loop Timing ---
SCAN_INTERVAL_SECS = 300
# 60s: the monitor only quotes open positions (<= MAX_POSITIONS tickers,
# behind data.py's 20s price cache), and the backtest assumes stops are
# caught within one 5m bar — slower polling than that gives up fills the
# validated numbers depend on.
MONITOR_INTERVAL_SECS = 60
DISPLAY_INTERVAL_SECS = 30

# --- Technical Indicator Periods ---
RSI_PERIOD = 14
EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
ATR_PERIOD = 14
ADX_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9
BB_PERIOD = 20
BB_STD = 2.0
VOLUME_LOOKBACK = 20

# --- Database ---
DB_PATH = _env("DB_PATH", "ledger.db")

# --- Crypto instance (read by the dashboard server only) ---
# The crypto bot container runs with DB_PATH=<CRYPTO_DB_PATH> and
# STARTING_CAPITAL=<CRYPTO_STARTING_CAPITAL>; the server needs both values
# under separate names so one process can serve / (stocks) and /crypto.
CRYPTO_DB_PATH = _env("CRYPTO_DB_PATH", "ledger-crypto.db")
CRYPTO_STARTING_CAPITAL = float(_env("CRYPTO_STARTING_CAPITAL", "1000"))

# --- Broker execution ---
# "paper"  = internal simulator (default): instant fills + slippage model.
# "alpaca" = Alpaca bracket orders — entry, stop and take-profit live at the
#            broker, so exits fire even if this process dies. Paper endpoint
#            by default; a live endpoint additionally needs ALPACA_ALLOW_LIVE=1.
# Note: US brokers enforce the Pattern Day Trader rule — real-money margin
# accounts under $25k get ~3 day trades per 5 sessions, which this strategy
# (~6-7 round trips/day) cannot operate under. Paper is exempt.
BROKER = _env("BROKER", "paper").lower()
ALPACA_API_KEY    = _env("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = _env("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = _env("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_ALLOW_LIVE = _env("ALPACA_ALLOW_LIVE", "0").lower() in ("1", "true", "yes")
