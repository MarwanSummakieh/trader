import os
from datetime import time

# --- Capital & Risk ---
STARTING_CAPITAL = 10_000.0
POSITION_SIZE_PCT = 0.15       # 15% of capital per trade
STOP_LOSS_PCT = 0.02           # 2% hard stop
TAKE_PROFIT_PCT = 0.04         # 4% target (2:1 R/R)
MAX_POSITIONS = 5

# --- Signal Thresholds ---
MIN_SCORE = 65                  # 0-100 score needed to trigger buy
MIN_VOLUME_RATIO = 1.5          # Must be 1.5x average daily volume
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MIN_PRICE = 5.0                 # Skip penny stocks
MIN_AVG_DAILY_VOLUME = 500_000  # Skip illiquid names

# --- Market Hours (US Eastern Time) ---
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EOD_CLOSE_TIME = time(15, 45)  # Force-close all stock positions before this

# --- Loop Timing ---
SCAN_INTERVAL_SECS = 300       # Full universe scan every 5 min
MONITOR_INTERVAL_SECS = 120    # Position check every 2 min
DISPLAY_INTERVAL_SECS = 30     # Redraw status every 30 sec

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

# --- eToro ---
ETORO_API_KEY = os.getenv("ETORO_API_KEY", "")
