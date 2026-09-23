"""
Turtle Trading System - Portfolio Backtest

Replays history day by day through the SAME decision code the live trader
uses (turtle_core.plan_actions), with one shared account across all markets
so sizing, pyramiding and the portfolio heat limits behave as they would live.

How a simulated day works (matches running the trader each evening):
  - orders decided at yesterday's close fill at today's open (+ slippage)
  - any stop inside today's range fills at the stop (or the open, if price
    gapped through it)
  - at today's close: mark to market, then decide tomorrow's orders

Price history: the micro contracts are too new for a meaningful test (micro
grains only launched in 2025), so by default history comes from the full-size
contract (ES, GC, ZC...), which quotes the same price. Sizing and P&L still use
the MICRO point values from turtle_config.

Usage:
    # 1. download history once from TWS (saved to data/<KEY>.csv)
    python turtle_backtest.py --download --years 15
    # 2. run the backtest from the saved files (TWS not needed)
    python turtle_backtest.py --equity 75000
    python turtle_backtest.py --equity 75000 --start 2015-01-01 --end 2024-12-31

Any CSV with date,open,high,low,close columns works too - put it in data/<KEY>.csv.
"""

import argparse
import os
import sys

import pandas as pd

import turtle_config as cfg
from turtle_core import (Snapshot, add_indicators, plan_actions, open_position,
                         add_unit, stop_hit)

DATA_DIR = 'data'
RESULTS_DIR = 'backtest_results'


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def download(years):
    """Pulls daily history for each market's full-size equivalent from TWS."""
    from ib_async import IB, ContFuture
    ib = IB()
    ib.connect(host=cfg.IB_HOST, port=cfg.IB_PORT, clientId=cfg.IB_CLIENT_ID + 10, timeout=10)
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        for m in cfg.MARKETS:
            qualified = [c for c in ib.qualifyContracts(
                ContFuture(symbol=m.history_symbol, exchange=m.exchange, currency='USD')) if c is not None]
            if not qualified:
                print(f"  {m.key}: could not qualify {m.history_symbol} on {m.exchange}, skipping.")
                continue
            bars = ib.reqHistoricalData(qualified[0], endDateTime='', durationStr=f'{years} Y',
                                        barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
            if not bars:
                print(f"  {m.key}: no history returned for {m.history_symbol}, skipping.")
                continue
            df = pd.DataFrame([{'date': b.date, 'open': b.open, 'high': b.high,
                                'low': b.low, 'close': b.close} for b in bars])
            path = os.path.join(DATA_DIR, f"{m.key}.csv")
            df.to_csv(path, index=False)
            print(f"  {m.key}: {len(df)} bars of {m.history_symbol} "
                  f"({df['date'].iloc[0]} to {df['date'].iloc[-1]}) -> {path}")
    finally:
        ib.disconnect()


def load_data(keys):
    frames = {}
    for key in keys:
        path = os.path.join(DATA_DIR, f"{key}.csv")
        if not os.path.exists(path):
            print(f"  {key}: no {path}, leaving it out.")
            continue
        df = pd.read_csv(path, parse_dates=['date'])
        df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)
        frames[key] = add_indicators(df).set_index('date')
    return frames


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class Backtest:
    def __init__(self, frames, equity, start=None, end=None):
        self.frames = frames
        self.cash = equity            # starting equity + realized P&L - costs
        self.positions = {}
        self.trades = []
        self.curve = []
        self.skipped = []             # signals not taken (size 0 / heat limit)
        dates = sorted(set().union(*[f.index for f in frames.values()]))
        self.dates = [d for d in dates
                      if (start is None or d >= start) and (end is None or d <= end)]

    def slip(self, key, price, side):
        """side +1 = buying (pay up), -1 = selling (receive less)."""
        return price + side * cfg.SLIPPAGE_TICKS * cfg.MARKETS_BY_KEY[key].tick

    def cost(self, qty):
        self.cash -= qty * cfg.COMMISSION_PER_CONTRACT

    def close_position(self, pos, price, date, reason):
        pv = cfg.MARKETS_BY_KEY[pos.key].point_value
        pnl = sum((price - u.price) * pos.direction * u.qty for u in pos.units) * pv
        self.cash += pnl
        self.cost(pos.qty)
        self.trades.append({'key': pos.key, 'direction': 'long' if pos.direction > 0 else 'short',
                            'entry_date': pos.units[0].date, 'exit_date': str(date.date()),
                            'units': pos.unit_count, 'qty': pos.qty,
                            'entry_price': pos.units[0].price, 'exit_price': price,
                            'reason': reason, 'pnl': round(pnl, 2)})
        del self.positions[pos.key]

    def open_pnl(self, date):
        total = 0.0
        for pos in self.positions.values():
            f = self.frames[pos.key]
            close = f.at[date, 'close'] if date in f.index else f.loc[:date, 'close'].iloc[-1]
            pv = cfg.MARKETS_BY_KEY[pos.key].point_value
            total += sum((close - u.price) * pos.direction * u.qty for u in pos.units) * pv
        return total

    def run(self):
        pending, pending_n = [], {}
        for date in self.dates:
            bars = {k: f.loc[date] for k, f in self.frames.items() if date in f.index}

            # 1. Yesterday's decisions fill at today's open
            for a in pending:
                if a.key not in bars:
                    continue
                bar = bars[a.key]
                fill = self.slip(a.key, bar['open'], a.direction)
                d = str(date.date())
                if a.kind == 'exit' and a.key in self.positions:
                    self.close_position(self.positions[a.key], fill, date, a.reason)
                elif a.kind == 'add' and a.key in self.positions:
                    add_unit(self.positions[a.key], a.qty, fill, d)
                    self.cost(a.qty)
                elif a.kind == 'entry' and a.key not in self.positions:
                    self.positions[a.key] = open_position(a.key, a.direction, a.qty, fill, pending_n[a.key], d)
                    self.cost(a.qty)

            # 2. Stops during the day
            for key in list(self.positions):
                if key not in bars:
                    continue
                pos, bar = self.positions[key], bars[key]
                if stop_hit(pos, bar['low'], bar['high']):
                    gapped = (bar['open'] < pos.stop) if pos.direction > 0 else (bar['open'] > pos.stop)
                    price = bar['open'] if gapped else pos.stop
                    self.close_position(pos, self.slip(key, price, -pos.direction), date, 'stop')

            # 3. Mark to market
            equity = self.cash + self.open_pnl(date)
            self.curve.append({'date': date, 'equity': equity,
                               'units': sum(p.unit_count for p in self.positions.values())})

            # 4. Decide tomorrow's orders from today's close
            snapshots = {}
            for key, bar in bars.items():
                if bar[['N', 'entry_high', 'entry_low', 'exit_high', 'exit_low']].isna().any():
                    continue
                row = dict(bar, date=date.date())
                snapshots[key] = Snapshot.from_row(key, row, cfg.MARKETS_BY_KEY[key].point_value)
            pending, notes = plan_actions(self.positions, snapshots, equity)
            pending_n = {k: s.n for k, s in snapshots.items()}
            self.skipped += [(date.date(), n) for n in notes]

        # Close anything still open at the last close so P&L is complete
        last = self.dates[-1]
        for key in list(self.positions):
            f = self.frames[key]
            self.close_position(self.positions[key], f.loc[:last, 'close'].iloc[-1], last, 'end of test')
        return self


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(bt, start_equity):
    curve = pd.DataFrame(bt.curve).set_index('date')
    trades = pd.DataFrame(bt.trades)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    curve.to_csv(os.path.join(RESULTS_DIR, 'equity_curve.csv'))
    trades.to_csv(os.path.join(RESULTS_DIR, 'trades.csv'), index=False)
    pd.DataFrame(bt.skipped, columns=['date', 'note']).to_csv(
        os.path.join(RESULTS_DIR, 'skipped_signals.csv'), index=False)

    final = bt.cash
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (final / start_equity) ** (1 / years) - 1 if years > 0 and final > 0 else float('nan')
    drawdown = curve['equity'] / curve['equity'].cummax() - 1
    worst = drawdown.idxmin()

    print("=" * 64)
    print(f"BACKTEST  {curve.index[0].date()} to {curve.index[-1].date()}  ({years:.1f} years)")
    print("=" * 64)
    print(f"  Start equity:      ${start_equity:,.0f}")
    print(f"  End equity:        ${final:,.0f}")
    print(f"  CAGR:              {cagr:.1%}")
    print(f"  Max drawdown:      {drawdown.min():.1%}  (bottom {worst.date()})")
    if trades.empty:
        print("  No trades taken.")
    else:
        wins = trades[trades['pnl'] > 0]
        losses = trades[trades['pnl'] <= 0]
        pf = wins['pnl'].sum() / -losses['pnl'].sum() if losses['pnl'].sum() < 0 else float('inf')
        print(f"  Trades:            {len(trades)}  ({len(wins)} winners, {len(wins) / len(trades):.0%})")
        print(f"  Profit factor:     {pf:.2f}")
        print(f"  Avg win / loss:    ${wins['pnl'].mean() if len(wins) else 0:,.0f} / "
              f"${losses['pnl'].mean() if len(losses) else 0:,.0f}")
        print("\n  P&L by market:")
        by_market = trades.groupby('key')['pnl'].agg(['count', 'sum'])
        for key, row in by_market.sort_values('sum', ascending=False).iterrows():
            print(f"    {key:5} {int(row['count']):4d} trades   ${row['sum']:>10,.0f}")
    size_zero = sum(1 for _, n in bt.skipped if 'rounds to 0' in n or 'risks $' in n)
    heat = len(bt.skipped) - size_zero
    print(f"\n  Skipped signals (counted per day, a signal repeats while it stays active):")
    print(f"    {size_zero} too small to size (<1 contract), {heat} blocked by heat limits")
    print(f"  Details in {RESULTS_DIR}/ (equity_curve.csv, trades.csv, skipped_signals.csv)")


def main():
    parser = argparse.ArgumentParser(description="Turtle portfolio backtest")
    parser.add_argument('--download', action='store_true', help="pull history from TWS into data/")
    parser.add_argument('--years', type=int, default=15, help="years of history to download")
    parser.add_argument('--equity', type=float, default=cfg.PAPER_STARTING_EQUITY or 75000,
                        help="starting account equity (default: PAPER_STARTING_EQUITY)")
    parser.add_argument('--start', type=pd.Timestamp, help="first date to trade (YYYY-MM-DD)")
    parser.add_argument('--end', type=pd.Timestamp, help="last date to trade (YYYY-MM-DD)")
    parser.add_argument('--markets', help="comma-separated keys to include, e.g. MES,MGC (default: all)")
    args = parser.parse_args()

    if args.download:
        download(args.years)
        return

    keys = args.markets.split(',') if args.markets else [m.key for m in cfg.MARKETS]
    unknown = [k for k in keys if k not in cfg.MARKETS_BY_KEY]
    if unknown:
        sys.exit(f"Unknown market keys: {unknown}")
    frames = load_data(keys)
    if not frames:
        sys.exit(f"No data found in {DATA_DIR}/ - run with --download first.")
    report(Backtest(frames, args.equity, args.start, args.end).run(), args.equity)


if __name__ == '__main__':
    main()
