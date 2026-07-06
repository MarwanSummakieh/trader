import os
from datetime import time
from dotenv import load_dotenv

load_dotenv()   # reads .env file (safe no-op if file doesn't exist)

# Deploy stamp — bump when cutting a release (shown at bot startup, in the
# dashboard header, and in /api/status) so "which code is running?" is
# always answerable at a glance.
VERSION = "2026-07-05"

# --- Capital & Risk ---
# Defaults below are the backtest-validated set (2026-07-02, 59d of 5m bars,
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
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "10000"))
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.15"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))       # floor for the ATR stop
TAKE_PROFIT_R_MULT = float(os.getenv("TAKE_PROFIT_R_MULT", "3.0"))  # target = entry + R * this
# Validation showed 5 concurrent slots underperforms; >=7 is capital-limited anyway.
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "8"))

# Bid/ask spread + slippage on market fills, applied per side (entry fills
# higher, exit fills lower). 0.001 = 0.1% per side. Alpaca US equities are
# COMMISSION-FREE, so this now models spread/slippage only — and 0.1% is
# conservative for the liquid large-caps in this universe (real spreads are
# usually tighter), so the backtested edge is if anything understated.
FEE_SLIPPAGE_PCT = float(os.getenv("FEE_SLIPPAGE_PCT", "0.001"))

# Alpaca regulatory pass-through fees — charged on SELLS only, buys are free.
# Tiny (cents per trade) but modeled so backtest/paper PnL is honest net of
# every real cost. Rates change periodically; override via .env when they do.
#   SEC fee:   a % of sell proceeds (2025 rate ≈ $27.80 per $1M = 0.0000278)
#   FINRA TAF: per share sold, capped per trade
ALPACA_SEC_FEE_RATE        = float(os.getenv("ALPACA_SEC_FEE_RATE", "0.0000278"))
ALPACA_FINRA_TAF_PER_SHARE = float(os.getenv("ALPACA_FINRA_TAF_PER_SHARE", "0.000166"))
ALPACA_FINRA_TAF_CAP       = float(os.getenv("ALPACA_FINRA_TAF_CAP", "8.30"))

# CFD-style leverage: exposure = committed margin * LEVERAGE. A position is
# force-closed ("margin_call") when its loss reaches MARGIN_CALL_LOSS of the
# committed margin. Backtested 2026-07: returns scale ~linearly but drawdowns
# scale faster (2x → -15% train DD). Half-Kelly on backtest stats ≈ 1.5x;
# do not raise above 1.0 until live paper results confirm the backtest edge.
LEVERAGE = float(os.getenv("LEVERAGE", "1.0"))
MARGIN_CALL_LOSS = float(os.getenv("MARGIN_CALL_LOSS", "0.9"))

# --- Entry Rule (backtest-validated momentum, see get_buy_candidates) ---
ENTRY_RSI_MIN = float(os.getenv("ENTRY_RSI_MIN", "55"))
ENTRY_RSI_MAX = float(os.getenv("ENTRY_RSI_MAX", "70"))
ENTRY_ADX_MIN = float(os.getenv("ENTRY_ADX_MIN", "30"))
# No stock entries at/after this ET time — the intraday drift edge needs
# hours to play out before the 15:45 EOD liquidation.
_cutoff = os.getenv("STOCK_ENTRY_CUTOFF", "12:00").split(":")
STOCK_ENTRY_CUTOFF = time(int(_cutoff[0]), int(_cutoff[1]))

# Crypto has no validated entry rule (momentum tested negative-edge, and the
# oversold-bounce edge did not survive execution testing) — off by default.
ENABLE_CRYPTO = os.getenv("ENABLE_CRYPTO", "0").lower() in ("1", "true", "yes")

# Skip stock entries on earnings reaction days (announcement day + next
# session). Tested 2026-07: such entries won 18% at -0.41R average.
EARNINGS_FILTER = os.getenv("EARNINGS_FILTER", "1").lower() in ("1", "true", "yes")

# Composite score is kept for the dashboard/scan display only — it showed no
# predictive power in backtests and is NOT part of the entry rule.
MIN_SCORE = int(os.getenv("MIN_SCORE", "65"))

# Regime gate: only take longs while price is above its daily EMA50.
# Backtested 2026-07: improved expectancy on both train and holdout splits.
REQUIRE_DAILY_UPTREND = os.getenv("REQUIRE_DAILY_UPTREND", "1").lower() in ("1", "true", "yes")

# --- Trailing stop (R-based; R = entry-to-initial-stop distance) ---
# Once unrealized gain reaches PROFIT_TRAIL_TRIGGER_R * R, the stop trails
# PROFIT_TRAIL_DISTANCE_R * R below the observed price. It only ever moves up.
PROFIT_TRAIL_TRIGGER_R  = float(os.getenv("PROFIT_TRAIL_TRIGGER_R",  "1.5"))
PROFIT_TRAIL_DISTANCE_R = float(os.getenv("PROFIT_TRAIL_DISTANCE_R", "1.5"))
MIN_VOLUME_RATIO = 1.5
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MIN_PRICE = 5.0                    # stocks only — crypto is exempt
MIN_AVG_DAILY_VOLUME = 500_000     # stocks only — 20-day average share volume
MAX_DATA_AGE_MINUTES = 15          # reject stock signals from stale bars (holidays/halts)

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
DB_PATH = os.getenv("DB_PATH", "ledger.db")

# --- Broker execution ---
# "paper"  = internal simulator (default): instant fills + slippage model.
# "alpaca" = Alpaca bracket orders — entry, stop and take-profit live at the
#            broker, so exits fire even if this process dies. Paper endpoint
#            by default; a live endpoint additionally needs ALPACA_ALLOW_LIVE=1.
# Note: US brokers enforce the Pattern Day Trader rule — real-money margin
# accounts under $25k get ~3 day trades per 5 sessions, which this strategy
# (~6-7 round trips/day) cannot operate under. Paper is exempt.
BROKER = os.getenv("BROKER", "paper").lower()
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_ALLOW_LIVE = os.getenv("ALPACA_ALLOW_LIVE", "0").lower() in ("1", "true", "yes")
