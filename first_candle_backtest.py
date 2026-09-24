"""
First 5-Minute Candle Strategy - Backtest

Follows the rules of Zarattini & Aziz (2023), "Can Day Trading Really Be
Profitable?", which tested this on QQQ; here on the micro index futures.
All times US Eastern.

  - The first 5-minute candle of the day session (9:30-9:35) sets direction:
    closed up = buy, closed down = sell short, unchanged = no trade.
  - Enter at the open of the next candle (9:35).
  - Stop at the other end of the first candle (its low for a long, high for a short).
  - Size: 1% of equity lost if the stop is hit, capped at 4x leverage (as the paper).
  - Always flat by the close. Several exits are compared, including the
    paper's (a 10R target, else the close) and adding to winners.

Worst case is assumed whenever a 5-minute candle can't show the order of events
(a candle touching both the stop and a target counts as the stop).

Usage:
    python first_candle_backtest.py --download     # 5-minute bars from TWS, a few minutes
    python first_candle_backtest.py
"""

import argparse
import datetime as dt
import math
import os
import sys

import pandas as pd

import orb_backtest as orb
import turtle_config as cfg

# --- Settings ---------------------------------------------------------------
MARKETS = ['MES', 'MNQ', 'MYM']
RISK_PER_TRADE = 0.01
MAX_LEVERAGE = 4.0                   # contract value can't exceed this multiple of equity
START_EQUITY = orb.START_EQUITY
FIRST_CANDLE = dt.time(9, 30)
ENTRY_TIME = dt.time(9, 35)
LAST_BAR_START = dt.time(15, 55)     # flat at the close of this bar (4:00 PM)
DATA_DIR = 'data_5m'
RESULTS_DIR = 'first_candle_results'

EXIT_RULES = {
    'Paper: 10R target or close': {'target_r': 10.0},
    'Hold to close':              {},
    'Target 2R':                  {'target_r': 2.0},
    'Trailing 1R stop':           {'trail_r': 1.0},
    'Add at +1R, stop to entry':  {'add_at_r': 1.0},
}


def simulate_day(day, market, equity, target_r=None, trail_r=None, add_at_r=None):
    """
    day: one day's 5-minute bars (time, open, high, low, close) in order.
    Returns a trade dict with P&L after slippage and commission, or None.
    """
    rows = [b for b in day.itertuples() if b.time <= LAST_BAR_START]
    first = next((b for b in rows if b.time == FIRST_CANDLE), None)
    session = [b for b in rows if b.time >= ENTRY_TIME]
    if first is None or not session or session[0].time != ENTRY_TIME or first.close == first.open:
        return None

    d = 1 if first.close > first.open else -1
    tick, pv = market.tick, market.point_value
    slip = cfg.SLIPPAGE_TICKS * tick
    entry = session[0].open + d * slip
    stop = first.low if d > 0 else first.high
    risk = (entry - stop) * d
    if risk <= 0:                          # already through the stop at the open
        return None
    qty = min(math.floor(equity * RISK_PER_TRADE / ((risk + slip) * pv)),
              math.floor(equity * MAX_LEVERAGE / (entry * pv)))
    if qty < 1:
        return None

    units = [(entry, qty)]
    target = entry + d * target_r * risk if target_r else None
    add_price = entry + d * add_at_r * risk if add_at_r else None
    best, exit_, reason = entry, None, None

    for bar in session:                    # entry is at the first bar's open, so its whole range counts
        hit = bar.low <= stop if d > 0 else bar.high >= stop
        if hit:                            # stop first: worst case when a bar touches both
            gapped = bar.open < stop if d > 0 else bar.open > stop
            exit_, reason = (bar.open if gapped else stop) - d * slip, 'stop'
            break
        if target is not None and (bar.high >= target if d > 0 else bar.low <= target):
            exit_, reason = target, 'target'
            break
        if add_price is not None and (bar.high >= add_price if d > 0 else bar.low <= add_price):
            fill = (max(bar.open, add_price) if d > 0 else min(bar.open, add_price)) + d * slip
            units.append((fill, qty))
            stop, add_price = entry, None  # whole position's stop to the first entry
        if trail_r:
            best = max(best, bar.high) if d > 0 else min(best, bar.low)
            trailed = best - d * trail_r * risk
            stop = max(stop, trailed) if d > 0 else min(stop, trailed)
    if exit_ is None:
        exit_, reason = session[-1].close - d * slip, 'close'

    contracts = sum(q for _, q in units)
    pnl = sum((exit_ - p) * d * q for p, q in units) * pv - 2 * contracts * cfg.COMMISSION_PER_CONTRACT
    return {'direction': 'long' if d > 0 else 'short', 'qty': contracts, 'units': len(units),
            'entry': entry, 'stop': stop, 'exit': exit_, 'reason': reason,
            'r': pnl / (equity * RISK_PER_TRADE), 'pnl': round(pnl, 2)}


def run(frames, rule, equity=START_EQUITY):
    days = {}
    for key, df in frames.items():
        for date, day in df.groupby('date'):
            days.setdefault(date, {})[key] = day
    trades, curve = [], []
    for date in sorted(days):
        morning = equity
        for key, day in sorted(days[date].items()):
            t = simulate_day(day, cfg.MARKETS_BY_KEY[key], morning, **rule)
            if t:
                equity += t['pnl']
                trades.append({'date': date, 'key': key, **t})
        curve.append({'date': date, 'equity': equity})
    return pd.DataFrame(trades), pd.DataFrame(curve)


def report(frames):
    rows, results = [], {}
    for name, rule in EXIT_RULES.items():
        trades, curve = run(frames, rule)
        results[name] = trades
        rows.append(orb.summarize(name, trades, curve))
    table = pd.DataFrame(rows).set_index('rule')

    first = min(df['datetime'].iloc[0] for df in frames.values()).date()
    last = max(df['datetime'].iloc[-1] for df in frames.values()).date()
    print("=" * 100)
    print(f"FIRST 5-MIN CANDLE  {first} to {last}   markets {', '.join(frames)}   "
          f"${START_EQUITY:,} start, {RISK_PER_TRADE:.0%} risk, max {MAX_LEVERAGE:g}x leverage")
    print("=" * 100)
    fmt = {'end equity': '${:,.0f}'.format, 'return': '{:+.1%}'.format, 'max DD': '{:.1%}'.format,
           'win %': '{:.0%}'.format, 'avg R': '{:+.2f}'.format, 'profit factor': '{:.2f}'.format}
    print(table.to_string(formatters=fmt))

    base = results['Paper: 10R target or close']
    if not base.empty:
        per_day = base.groupby('date')['direction'].agg(['count', 'nunique'])
        multi = per_day[per_day['count'] > 1]
        same = (multi['nunique'] == 1).mean() if len(multi) else float('nan')
        pnl = ', '.join(f"{k} ${v:,.0f}" for k, v in base.groupby('key')['pnl'].sum().items())
        print(f"\nPaper rules: when 2+ markets traded the same day they were the same direction "
              f"{same:.0%} of the time.  P&L by market: {pnl}")
        costs = (2 * base['qty'] * cfg.COMMISSION_PER_CONTRACT).sum()
        print(f"Commissions paid under the paper rules: ${costs:,.0f}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    for name, trades in results.items():
        fname = name.replace(':', '').replace(',', '').replace('+', 'plus').replace(' ', '_')
        trades.to_csv(os.path.join(RESULTS_DIR, f"trades_{fname}.csv"), index=False)
    print(f"\nTrade lists in {RESULTS_DIR}/")


def main():
    parser = argparse.ArgumentParser(description="First 5-minute candle backtest")
    parser.add_argument('--download', action='store_true', help="pull 5-minute history from TWS")
    parser.add_argument('--years', type=int, default=2)
    args = parser.parse_args()
    orb.MARKETS = MARKETS
    if args.download:
        orb.download(args.years, bar_size='5 mins', data_dir=DATA_DIR, chunks=('1 M', '1 W'))
        return
    frames = orb.load(MARKETS, data_dir=DATA_DIR)
    if not frames:
        sys.exit(f"No data in {DATA_DIR}/ - run with --download first.")
    report(frames)


if __name__ == '__main__':
    main()
