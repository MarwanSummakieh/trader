import os
from datetime import time
from dotenv import load_dotenv

load_dotenv()   # reads .env file (safe no-op if file doesn't exist)

# --- Capital & Risk ---
# Defaults below are the backtest-validated set (2026-07-02, 59d of 5m bars,
# train/holdout split): entries = EMA-aligned momentum, daily ADX > 30,
# RSI 55-70, above daily EMA50, stocks before 12:00 ET only.
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "10000"))
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.15"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))       # floor for the ATR stop
TAKE_PROFIT_R_MULT = float(os.getenv("TAKE_PROFIT_R_MULT", "3.0"))  # target = entry + R * this
# Validation showed 5 concurrent slots underperforms; >=7 is capital-limited anyway.
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "8"))

# Paper-fill realism: applied per side (entry fills higher, exit fills lower)
# to approximate spread + slippage + fees. 0.001 = 0.1% per side.
FEE_SLIPPAGE_PCT = float(os.getenv("FEE_SLIPPAGE_PCT", "0.001"))

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
MONITOR_INTERVAL_SECS = 120
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

# --- eToro ---
# Both keys are generated together in Settings → Trading → API Key Management
ETORO_PUBLIC_KEY  = os.getenv("ETORO_PUBLIC_KEY", "")   # x-api-key header
ETORO_PRIVATE_KEY = os.getenv("ETORO_PRIVATE_KEY", "")  # x-user-key header
