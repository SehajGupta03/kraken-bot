PAIR = "BTC/USD"          # REST format (AssetPairs lookup, WS token)
WS_SYMBOL = "BTC/USD"    # WebSocket v2 format

# Order book depth to subscribe to. Kraken only offers 10/25/100/500/1000;
# 10 is enough for the signals below and keeps per-update work minimal.
BOOK_DEPTH = 10

# ---------------------------------------------------------------- fees
# Kraken spot, lowest volume tier. A market maker only ever pays the maker
# fee because every quote is post-only. These dominate profitability: the
# strategy refuses to quote tighter than fees + MIN_EDGE_BPS.
MAKER_FEE = 0.0025
TAKER_FEE = 0.0040

# ---------------------------------------------------------------- signals
# Fair value = microprice, nudged by two short-horizon alphas:
#   book imbalance  -- resting size on the bid vs ask over the top levels
#   trade flow      -- EWMA of signed aggressor volume (buys +, sells -)
IMBALANCE_LEVELS = 5
FLOW_HALFLIFE_S = 5.0
ALPHA_IMBALANCE_BPS = 1.5    # fair value shift at imbalance = +/-1
ALPHA_FLOW_BPS = 1.5         # fair value shift at flow imbalance = +/-1

# Volatility: EWMA of squared log mid returns, sampled at most every VOL_SAMPLE_S.
VOL_SAMPLE_S = 1.0
VOL_HALFLIFE_S = 60.0

# ---------------------------------------------------------------- quoting
# half_spread_bps = max(MIN_HALF_SPREAD_BPS, fee + MIN_EDGE_BPS)
#                   + VOL_SPREAD_MULT * sigma_bps over QUOTE_HORIZON_S
MIN_EDGE_BPS = 2.0
MIN_HALF_SPREAD_BPS = 0.0
VOL_SPREAD_MULT = 1.0
QUOTE_HORIZON_S = 10.0

QUOTE_SIZE = 0.0001          # base units (BTC) per quote
MAX_POSITION = 0.0005        # base units; no more bids above this
TARGET_POSITION = 0.0        # inventory the skew pulls back towards
INVENTORY_SKEW_BPS = 10.0    # reservation price shift at |q - target| = MAX_POSITION

# Toxicity filter: pull the side that would get run over when the
# combined signal is strongly against it (adverse selection protection).
TOXIC_SIGNAL = 0.6

# Requote throttling. Every add/amend/cancel counts against Kraken's
# per-pair rate counter (cancelling an order younger than a few seconds is
# especially expensive), so only move a quote when it is meaningfully stale.
REQUOTE_THRESHOLD_BPS = 1.0
MIN_REQUOTE_INTERVAL_MS = 500
RATE_LIMIT_BACKOFF_S = 5.0   # live: pause quoting after a rate-limit rejection

# ---------------------------------------------------------------- risk
MAX_LOSS_USD = 10.0          # realized + unrealized; kill switch when hit
STALE_BOOK_S = 5.0           # pull quotes if the book stops updating
DEAD_MAN_TIMEOUT_S = 60      # live: Kraken cancels everything if we go silent

# ---------------------------------------------------------------- paper mode
STARTING_CASH = 100.0
# One-way order latency the paper exchange applies before an order becomes
# live / a cancel takes effect. Set it to what --live actually measures.
SIM_LATENCY_MS = 30

# ---------------------------------------------------------------- output
STATS_INTERVAL_S = 10
TRADE_LOG_FILE = "trades_log.csv"
