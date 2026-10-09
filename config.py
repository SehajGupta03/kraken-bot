PAIR = "BTC/USD"          # REST format, used once at startup to seed RSI history
WS_SYMBOL = "BTC/USD"    # WebSocket v2 format
INTERVAL_MINUTES = 15

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

STARTING_CASH = 100.0

# How much to spend on a BUY signal.
# "equity_fraction": TRADE_FRACTION * (cash + position value) -- scales with
#   account size instead of shrinking just because cash is currently tied up.
# "fixed_amount": always spend FIXED_TRADE_AMOUNT dollars (capped at available cash).
SIZING_MODE = "equity_fraction"
TRADE_FRACTION = 0.5
FIXED_TRADE_AMOUNT = 50.0

KRAKEN_TAKER_FEE = 0.004

STATE_FILE = "portfolio_state.json"
TRADE_LOG_FILE = "trades_log.csv"

# Live trading safety caps -- independent of SIZING_MODE, so a sizing bug
# can't exceed these. LIVE_MAX_LOSS_USD only blocks new buys once hit;
# selling an existing position is never blocked.
LIVE_MAX_TRADE_USD = 25.0
LIVE_MAX_LOSS_USD = 10.0