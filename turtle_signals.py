"""
Turtle Trading System - Signal Generation

Pulls historical daily data for all 14 markets, calculates N (the ATR-based
volatility unit used for position sizing and stops), and checks for entry/exit
breakout signals.

Rules:
- Entry: 100-day breakout of the high (long) or low (short)
- Exit:  50-day breakout of the opposite extreme
- N:     20-day exponential-style average of True Range (classic Turtle method)

This script only DETECTS signals and prints them - it does not size positions,
place orders, or manage stops yet. That's the next piece to build once this
is confirmed working correctly.

Run with TWS open and logged into your paper trading account.

Install requirements first:
    python -m pip install ib_async pandas
"""

from ib_async import IB, ContFuture, Contract
import pandas as pd
import sys

# --- Market definitions: (display name, IBKR symbol, exchange, trading_class) ---
# trading_class is normally None - it's only needed for Micro Silver, which IBKR
# lists under the root symbol "SI" (same as full-size silver) distinguished by
# tradingClass "SIL", rather than under symbol "SIL" itself.
MARKETS = [
    ("S&P 500 (Micro)",      "MES", "CME",   None),
    ("Nasdaq-100 (Micro)",   "MNQ", "CME",   None),
    ("Russell 2000 (Micro)", "M2K", "CME",   None),
    ("Dow (Micro)",          "MYM", "CBOT",  None),
    ("Gold (Micro)",         "MGC", "COMEX", None),
    ("Silver (Micro)",       "SI",  "COMEX", "SIL"),
    ("Copper (Micro)",       "MHG", "COMEX", None),
    ("Crude Oil (Micro)",    "MCL", "NYMEX", None),
    ("Natural Gas (Micro)",  "MHNG", "NYMEX", None),
    ("Corn (Micro)",         "MZC", "CBOT",  None),
    ("Wheat (Micro)",        "MZW", "CBOT",  None),
    ("Soybeans (Micro)",     "MZS", "CBOT",  None),
    ("Soybean Meal (Micro)", "MZM", "CBOT",  None),
    ("Soybean Oil (Micro)",  "MZL", "CBOT",  None),
]

ENTRY_LOOKBACK = 100   # days for entry breakout
EXIT_LOOKBACK = 50     # days for exit breakout
N_LOOKBACK = 20        # days for N (volatility) calculation
HISTORY_DAYS = 250     # how much history to pull - must comfortably exceed ENTRY_LOOKBACK


def calculate_true_range(df):
    """True Range = the largest of: high-low, abs(high-prev_close), abs(low-prev_close)"""
    prev_close = df['close'].shift(1)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - prev_close).abs()
    tr3 = (df['low'] - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def calculate_n(df, lookback=N_LOOKBACK):
    """
    Classic Turtle N: a smoothed (Wilder-style) average of True Range.
    N_today = (19 * N_yesterday + TR_today) / 20
    The very first N value is a plain average of the first 20 True Range values.
    """
    tr = calculate_true_range(df)
    n = tr.copy()
    n.iloc[:lookback] = tr.iloc[:lookback].expanding().mean()
    for i in range(lookback, len(tr)):
        n.iloc[i] = (n.iloc[i - 1] * (lookback - 1) + tr.iloc[i]) / lookback
    return n


def get_bars_as_dataframe(ib, contract, duration=f'{HISTORY_DAYS} D'):
    bars = ib.reqHistoricalData(
        contract,
        endDateTime='',
        durationStr=duration,
        barSizeSetting='1 day',
        whatToShow='TRADES',
        useRTH=True
    )
    if not bars:
        return None
    return pd.DataFrame([{
        'date': b.date, 'open': b.open, 'high': b.high,
        'low': b.low, 'close': b.close
    } for b in bars])


def get_front_month_future(ib, symbol, exchange, trading_class=None):
    """
    Fallback for markets that don't have a continuous contract (ContFuture)
    defined in IBKR's system - usually newer or lower-volume products.
    Finds every available dated contract and returns the nearest (front month).
    """
    kwargs = {'symbol': symbol, 'secType': 'FUT', 'exchange': exchange, 'currency': 'USD'}
    if trading_class:
        kwargs['tradingClass'] = trading_class
    contract = Contract(**kwargs)
    try:
        details = ib.reqContractDetails(contract)
    except Exception:
        return None
    if not details:
        return None
    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    return details[0].contract


def analyze_market(ib, name, symbol, exchange, trading_class=None):
    kwargs = {'symbol': symbol, 'exchange': exchange, 'currency': 'USD'}
    if trading_class:
        kwargs['tradingClass'] = trading_class
    contract = ContFuture(**kwargs)

    try:
        qualified = ib.qualifyContracts(contract)
    except Exception as e:
        print(f"  {name} ({symbol}): error qualifying contract ({e}), skipping.")
        return None

    # qualifyContracts can return None entries for contracts it couldn't resolve
    # rather than leaving them out of the list entirely - filter those out.
    qualified = [c for c in qualified if c is not None]
    if not qualified:
        # No continuous contract available for this market - fall back to
        # finding the nearest actual dated contract instead.
        fallback = get_front_month_future(ib, symbol, exchange, trading_class)
        if fallback is None:
            print(f"  {name} ({symbol}): could not qualify contract on {exchange}, skipping.")
            return None
        qualified = [fallback]

    try:
        df = get_bars_as_dataframe(ib, qualified[0])
    except Exception as e:
        print(f"  {name} ({symbol}): error pulling historical data ({e}), skipping.")
        return None
    if df is None or len(df) < ENTRY_LOOKBACK + N_LOOKBACK:
        got = 0 if df is None else len(df)
        print(f"  {name} ({symbol}): not enough historical data returned ({got} bars), skipping.")
        return None

    df['N'] = calculate_n(df)
    df['entry_high'] = df['high'].rolling(ENTRY_LOOKBACK).max()
    df['entry_low'] = df['low'].rolling(ENTRY_LOOKBACK).min()
    df['exit_high'] = df['high'].rolling(EXIT_LOOKBACK).max()
    df['exit_low'] = df['low'].rolling(EXIT_LOOKBACK).min()

    latest = df.iloc[-1]
    prev = df.iloc[-2]  # use the PRIOR day's channel so today's own bar doesn't count itself

    signal = "No signal"
    if latest['close'] > prev['entry_high']:
        signal = "LONG ENTRY signal (100-day breakout up)"
    elif latest['close'] < prev['entry_low']:
        signal = "SHORT ENTRY signal (100-day breakout down)"
    elif latest['close'] < prev['exit_low']:
        signal = "Exit signal for existing longs (50-day breakout down)"
    elif latest['close'] > prev['exit_high']:
        signal = "Exit signal for existing shorts (50-day breakout up)"

    print(f"  {name} ({symbol}):")
    print(f"    Latest close: {latest['close']:.4f}  |  N: {latest['N']:.4f}")
    print(f"    100-day range: {prev['entry_low']:.4f} - {prev['entry_high']:.4f}")
    print(f"    50-day range:  {prev['exit_low']:.4f} - {prev['exit_high']:.4f}")
    print(f"    --> {signal}")

    return {'name': name, 'symbol': symbol, 'close': latest['close'], 'N': latest['N'], 'signal': signal}


def main():
    ib = IB()
    try:
        ib.connect(host='127.0.0.1', port=7497, clientId=2, timeout=10)
        print("Connected to TWS.\n")
    except Exception as e:
        print(f"FAILED to connect: {e}")
        sys.exit(1)

    print("Analyzing all 14 markets (this may take a minute)...\n")
    results = []
    for name, symbol, exchange, trading_class in MARKETS:
        try:
            result = analyze_market(ib, name, symbol, exchange, trading_class)
        except Exception as e:
            print(f"  {name} ({symbol}): unexpected error ({e}), skipping.")
            result = None
        if result:
            results.append(result)
        print()

    print("=" * 60)
    print("SUMMARY - Markets with active signals today:")
    print("=" * 60)
    active = [r for r in results if r['signal'] != "No signal"]
    if not active:
        print("  No breakout signals today.")
    else:
        for r in active:
            print(f"  {r['name']} ({r['symbol']}): {r['signal']}")

    ib.disconnect()


if __name__ == '__main__':
    main()
