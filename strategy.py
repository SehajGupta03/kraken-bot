def compute_rsi(closes, period=14):
    """Wilder's RSI. Returns None if there isn't enough history yet."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_rsi_series(closes, period=14):
    """Same math as compute_rsi, but returns the RSI at every index
    (None where there isn't enough history yet) so a backtest can
    replay history candle-by-candle instead of just seeing the latest value."""
    rsis = [None] * len(closes)
    if len(closes) < period + 1:
        return rsis

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsis[i + 1] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    return rsis


def generate_signal(rsi, has_position, oversold, overbought):
    """Simple RSI mean-reversion rule: buy when oversold and flat,
    sell when overbought and holding a position. Otherwise hold."""
    if rsi is None:
        return "HOLD"
    if rsi <= oversold and not has_position:
        return "BUY"
    if rsi >= overbought and has_position:
        return "SELL"
    return "HOLD"

# def generate_signal(has_position):
#     """Simple RSI mean-reversion rule: buy when oversold and flat,
#     sell when overbought and holding a position. Otherwise hold."""
#     if not has_position:
#         return "BUY"
#     if has_position:
#         return "SELL"
#     return "HOLD"