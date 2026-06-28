import os
from datetime import time
from dotenv import load_dotenv

load_dotenv()   # reads .env file (safe no-op if file doesn't exist)

# --- Capital & Risk ---
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "10000"))
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.15"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.04"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))

# --- Signal Thresholds ---
MIN_SCORE = int(os.getenv("MIN_SCORE", "65"))
MIN_VOLUME_RATIO = 1.5
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MIN_PRICE = 5.0
MIN_AVG_DAILY_VOLUME = 500_000

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

# --- eToro ---
ETORO_API_KEY = os.getenv("ETORO_API_KEY", "")
