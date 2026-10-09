def calculate_spend(portfolio, price, config):
    """How many dollars to deploy on a BUY signal, per config.SIZING_MODE.
    Never exceeds the cash actually available."""
    if config.SIZING_MODE == "fixed_amount":
        amount = config.FIXED_TRADE_AMOUNT
    else:
        amount = portfolio.equity(price) * config.TRADE_FRACTION
    return max(0.0, min(amount, portfolio.cash))
