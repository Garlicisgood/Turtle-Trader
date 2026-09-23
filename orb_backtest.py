"""
Opening Range Breakout (ORB) - 15-minute Backtest

A separate day-trading strategy from the Turtle system, for the micro equity
index futures (MES, MNQ, MYM).

Rules being tested (all times US Eastern):
  - Anchor = the 2nd 15-minute candle of the day session (9:45-10:00)
  - Buy if price trades above the anchor high, sell short if it trades below the
    anchor low (stop orders one tick outside the anchor). Two entry windows are
    compared: the 3rd candle only (10:00-10:15), or any candle until noon.
    At most one trade per market per day.
  - Stop: the other side of the anchor
  - Size: 1% of equity lost if the stop is hit
  - Always flat by the close. Several exit rules are compared side by side.

Worst case is always assumed when 15-minute bars can't show the order of events:
if one candle touches both the stop and the target, the stop counts; if the
triggering candle breaks BOTH sides of the anchor, it's a full loss.

Usage:
    # 1. download 15-minute history from TWS (about 2 years; ~20 minutes
    #    because IBKR limits how fast historical data can be requested)
    python orb_backtest.py --download
    # 2. run the comparison (TWS not needed)
    python orb_backtest.py
"""

import argparse
import datetime as dt
import math
import os
import sys

import pandas as pd

import turtle_config as cfg

# --- Settings ---------------------------------------------------------------
MARKETS = ['MES', 'MNQ', 'MYM']      # keys from turtle_config.MARKETS
RISK_PER_TRADE = 0.01                # fraction of equity lost if the stop is hit
START_EQUITY = 75000
ANCHOR_START = dt.time(9, 45)        # the 2nd 15-minute candle
TRIGGER_START = dt.time(10, 0)       # the 3rd candle - first one allowed to trigger
LAST_BAR_START = dt.time(15, 45)     # flat at the close of this bar (4:00 PM)
MIN_ANCHOR_TICKS = 4                 # skip days with an anchor this small (stop too close)
TRAIL_R = 1.0                        # trailing-stop distance, in multiples of the initial risk
ROLL_DAYS = 8                        # switch to the next contract this many days before expiry
DATA_DIR = 'data_15m'
RESULTS_DIR = 'orb_results'

ENTRY_WINDOWS = {                    # start time of the LAST candle allowed to trigger
    '3rd candle only': dt.time(10, 0),
    'Until noon':      dt.time(11, 45),
}

EXIT_RULES = {
    'Hold to close':      {'target_r': None, 'trail': False},
    'Target 1R':          {'target_r': 1.0,  'trail': False},
    'Target 2R':          {'target_r': 2.0,  'trail': False},
    'Target 3R':          {'target_r': 3.0,  'trail': False},
    f'Trailing {TRAIL_R:g}R stop': {'target_r': None, 'trail': True},
}


# ---------------------------------------------------------------------------
# One day, one market
# ---------------------------------------------------------------------------

def simulate_day(day, tick, target_r=None, trail=False, trigger_end=TRIGGER_START):
    """
    day: that day's 15-minute bars (columns time, open, high, low, close), in order.
    trigger_end: start time of the last candle allowed to trigger an entry
                 (10:00 = 3rd candle only, 11:45 = any time until noon).
    Returns a dict describing the trade in price terms (before slippage/costs),
    or None if there was no trade.
    """
    rows = list(day.itertuples())
    anchor = next((b for b in rows if b.time == ANCHOR_START), None)
    if anchor is None or not any(b.time == TRIGGER_START for b in rows):
        return None
    if anchor.high - anchor.low < MIN_ANCHOR_TICKS * tick:
        return None

    buy_stop = anchor.high + tick
    sell_stop = anchor.low - tick
    session = [b for b in rows if TRIGGER_START <= b.time <= LAST_BAR_START]

    # 1. Find the entry: first candle in the window that breaks the anchor
    for i, bar in enumerate(session):
        if bar.time > trigger_end:
            return None
        up = bar.high >= buy_stop
        down = bar.low <= sell_stop
        if up and down:
            # Both sides broke inside one candle - order unknown, so assume the worst:
            # got in one way and stopped out the other.
            entry = max(bar.open, buy_stop)
            return {'direction': 1, 'entry': entry, 'stop': sell_stop, 'exit': sell_stop,
                    'reason': 'both sides broke (assumed loss)', 'entry_time': bar.time}
        if up or down:
            break
    else:
        return None

    direction = 1 if up else -1
    entry = max(bar.open, buy_stop) if up else min(bar.open, sell_stop)
    initial_stop = stop = sell_stop if up else buy_stop
    risk = abs(entry - stop)
    target = entry + direction * target_r * risk if target_r else None
    best = entry
    result = {'direction': direction, 'entry': entry, 'stop': initial_stop, 'entry_time': bar.time}

    # 2. Manage it: stop, target, trailing stop, else out at the close
    for j, bar in enumerate(session[i:]):
        if j > 0:
            # stop first - if a bar touches both stop and target, assume the stop
            hit = bar.low <= stop if direction > 0 else bar.high >= stop
            if hit:
                gapped = bar.open < stop if direction > 0 else bar.open > stop
                return {**result, 'exit': bar.open if gapped else stop,
                        'reason': 'stop' if stop == initial_stop else 'trailing stop'}
        if target is not None:
            reached = bar.high >= target if direction > 0 else bar.low <= target
            if reached:
                gapped = bar.open > target if direction > 0 else bar.open < target
                return {**result, 'exit': bar.open if (gapped and j > 0) else target, 'reason': 'target'}
        if trail:
            best = max(best, bar.high) if direction > 0 else min(best, bar.low)
            trailed = best - direction * TRAIL_R * risk
            stop = max(stop, trailed) if direction > 0 else min(stop, trailed)

    return {**result, 'exit': session[-1].close, 'reason': 'close'}


# ---------------------------------------------------------------------------
# Portfolio simulation
# ---------------------------------------------------------------------------

def run(frames, rule, equity=START_EQUITY):
    """frames: {key: DataFrame of 15-minute bars}. Returns (trades, daily_equity)."""
    days = {}
    for key, df in frames.items():
        for date, day in df.groupby('date'):
            days.setdefault(date, {})[key] = day

    trades, curve = [], []
    for date in sorted(days):
        start_equity = equity               # all markets sized off the morning's equity
        for key, day in sorted(days[date].items()):
            m = cfg.MARKETS_BY_KEY[key]
            t = simulate_day(day, m.tick, **rule)
            if t is None:
                continue
            slip = cfg.SLIPPAGE_TICKS * m.tick
            d = t['direction']
            entry = t['entry'] + d * slip                   # stop order - pays slippage
            exit_slip = 0 if t['reason'] == 'target' else slip   # targets are limit orders
            exit_ = t['exit'] - d * exit_slip
            risk_points = abs(entry - t['stop']) + slip
            qty = math.floor(start_equity * RISK_PER_TRADE / (risk_points * m.point_value))
            if qty < 1:
                continue
            pnl = (exit_ - entry) * d * qty * m.point_value - 2 * qty * cfg.COMMISSION_PER_CONTRACT
            equity += pnl
            trades.append({'date': date, 'time': t['entry_time'], 'key': key,
                           'direction': 'long' if d > 0 else 'short',
                           'qty': qty, 'entry': entry, 'exit': exit_, 'reason': t['reason'],
                           'r': (exit_ - entry) * d / risk_points, 'pnl': round(pnl, 2)})
        curve.append({'date': date, 'equity': equity})
    return pd.DataFrame(trades), pd.DataFrame(curve)


def summarize(name, trades, curve):
    if trades.empty:
        return {'rule': name, 'trades': 0}
    eq = curve['equity']
    wins, losses = trades[trades.pnl > 0], trades[trades.pnl <= 0]
    return {
        'rule': name,
        'end equity': eq.iloc[-1],
        'return': eq.iloc[-1] / START_EQUITY - 1,
        'max DD': (eq / eq.cummax() - 1).min(),
        'trades': len(trades),
        'win %': len(wins) / len(trades),
        'avg R': trades['r'].mean(),
        'profit factor': wins.pnl.sum() / -losses.pnl.sum() if losses.pnl.sum() < 0 else float('inf'),
    }


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def quarterly_months(first, last):
    """'YYYYMM' strings for every Mar/Jun/Sep/Dec between two dates."""
    months, year, month = [], first.year, first.month
    while (year, month) <= (last.year, last.month):
        if month in (3, 6, 9, 12):
            months.append(f"{year}{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def download(years, pacing_seconds=10):
    """
    Stitches 15-minute bars from each quarterly contract while it was the front
    month. (Day trades are flat every night, so no back-adjustment is needed.)
    IBKR keeps expired-contract data for about 2 years.
    """
    from ib_async import IB, Contract
    ib = IB()
    ib.connect(host=cfg.IB_HOST, port=cfg.IB_PORT, clientId=cfg.IB_CLIENT_ID + 20, timeout=10)
    os.makedirs(DATA_DIR, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=365 * years)
    try:
        for key in MARKETS:
            m = cfg.MARKETS_BY_KEY[key]
            # IBKR's contract list only includes recently expired contracts, so also
            # ask for each quarterly month (Mar/Jun/Sep/Dec) individually.
            query = dict(secType='FUT', symbol=m.symbol, exchange=m.exchange, currency='USD',
                         includeExpired=True)
            details = ib.reqContractDetails(Contract(**query))
            for month in quarterly_months(start - dt.timedelta(days=100), now + dt.timedelta(days=200)):
                details += ib.reqContractDetails(Contract(**query, lastTradeDateOrContractMonth=month))
            contracts = sorted({d.contract.conId: d.contract for d in details}.values(),
                               key=lambda c: c.lastTradeDateOrContractMonth)
            print(f"  {key}: contracts found: {', '.join(c.localSymbol for c in contracts)}")
            expiry = lambda c: dt.datetime.strptime(c.lastTradeDateOrContractMonth[:8], '%Y%m%d').replace(
                tzinfo=dt.timezone.utc)
            rows = []
            for prev, cur in zip(contracts, contracts[1:]):
                front_start = expiry(prev) - dt.timedelta(days=ROLL_DAYS)
                front_end = min(expiry(cur) - dt.timedelta(days=ROLL_DAYS), now)
                if front_end <= start or front_start >= now:
                    continue
                print(f"  {key}: {cur.localSymbol} {front_start.date()} to {front_end.date()}")
                end = front_end
                while end > max(front_start, start):
                    bars = ib.reqHistoricalData(cur, endDateTime=end, durationStr='1 M',
                                                barSizeSetting='15 mins', whatToShow='TRADES',
                                                useRTH=True, formatDate=2)
                    ib.sleep(pacing_seconds)
                    if not bars or bars[0].date >= end:
                        break
                    rows += [{'datetime': b.date, 'open': b.open, 'high': b.high, 'low': b.low,
                              'close': b.close, 'contract': cur.localSymbol}
                             for b in bars if front_start < b.date <= front_end]
                    end = bars[0].date
            if not rows:
                print(f"  {key}: no data returned.")
                continue
            df = pd.DataFrame(rows).drop_duplicates('datetime').sort_values('datetime')
            df['datetime'] = pd.to_datetime(df['datetime'], utc=True).dt.tz_convert('America/New_York')
            df['datetime'] = df['datetime'].dt.tz_localize(None)
            path = os.path.join(DATA_DIR, f"{key}.csv")
            df.to_csv(path, index=False)
            print(f"  {key}: {len(df)} bars, {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]} -> {path}")
    finally:
        ib.disconnect()


def load(keys):
    frames = {}
    for key in keys:
        path = os.path.join(DATA_DIR, f"{key}.csv")
        if not os.path.exists(path):
            print(f"  {key}: no {path}, leaving it out.")
            continue
        df = pd.read_csv(path, parse_dates=['datetime'])
        df['date'] = df['datetime'].dt.date
        df['time'] = df['datetime'].dt.time
        flat = (df['high'] == df['low']).mean()
        if flat > 0.05:
            print(f"  {key}: WARNING {flat:.0%} of bars have no range - data may be bad.")
        frames[key] = df
    return frames


def report(frames):
    rows, results = [], {}
    for window, trigger_end in ENTRY_WINDOWS.items():
        for exit_name, rule in EXIT_RULES.items():
            name = f"{window} | {exit_name}"
            trades, curve = run(frames, {**rule, 'trigger_end': trigger_end})
            results[name] = trades
            rows.append(summarize(name, trades, curve))
    table = pd.DataFrame(rows).set_index('rule')

    first = min(df['datetime'].iloc[0] for df in frames.values()).date()
    last = max(df['datetime'].iloc[-1] for df in frames.values()).date()
    print("=" * 104)
    print(f"ORB BACKTEST  {first} to {last}   markets {', '.join(frames)}   "
          f"${START_EQUITY:,} start, {RISK_PER_TRADE:.1%} risk per trade")
    print("=" * 104)
    fmt = {'end equity': '${:,.0f}'.format, 'return': '{:+.1%}'.format, 'max DD': '{:.1%}'.format,
           'win %': '{:.0%}'.format, 'avg R': '{:+.2f}'.format, 'profit factor': '{:.2f}'.format}
    print(table.to_string(formatters=fmt))

    for window in ENTRY_WINDOWS:
        base = results[f"{window} | Hold to close"]
        if base.empty:
            continue
        per_day = base.groupby('date')['direction'].agg(['count', 'nunique'])
        multi = per_day[per_day['count'] > 1]
        same = (multi['nunique'] == 1).mean() if len(multi) else float('nan')
        both = (base['reason'] == 'both sides broke (assumed loss)').sum()
        pnl = ', '.join(f"{k} ${v:,.0f}" for k, v in base.groupby('key')['pnl'].sum().items())
        print(f"\n{window}: trades on {len(per_day)} days; when 2+ markets traded the same day they were "
              f"the same direction {same:.0%} of the time; {both} both-sides candles counted as losses.")
        print(f"  Hold-to-close P&L by market: {pnl}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    for name, trades in results.items():
        fname = name.replace(' | ', '__').replace(' ', '_')
        trades.to_csv(os.path.join(RESULTS_DIR, f"trades_{fname}.csv"), index=False)
    print(f"\nTrade lists in {RESULTS_DIR}/")


def main():
    parser = argparse.ArgumentParser(description="15-minute opening range breakout backtest")
    parser.add_argument('--download', action='store_true', help="pull 15-minute history from TWS")
    parser.add_argument('--years', type=int, default=2, help="years to download (IBKR keeps ~2)")
    args = parser.parse_args()
    if args.download:
        download(args.years)
        return
    frames = load(MARKETS)
    if not frames:
        sys.exit(f"No data in {DATA_DIR}/ - run with --download first.")
    report(frames)


if __name__ == '__main__':
    main()
