"""
Turtle Trading System - Free Daily History from Yahoo Finance

Downloads 20+ years of daily bars for the full-size futures matching each
micro market and saves them to data_yahoo/<KEY>.csv for turtle_backtest.py.

KNOW THE LIMITS - this is for a rough first look, not a go-live decision:
  - Yahoo's continuous futures are NOT adjusted for rolls: the price jumps by
    the gap between contract months on every roll date, and the backtest sees
    that jump as a real move. Small for stock indexes and gold, big for
    natural gas, crude and grains.
  - The cost/gain of rolling itself is missing (e.g. natural gas longs lose
    money to rolling in many years), so those results come out too rosy.
  - Occasional bad or missing bars.

Usage (internet needed, TWS not needed):
    python -m pip install yfinance
    python turtle_download_yahoo.py
    python turtle_backtest.py --data-dir data_yahoo --equity 75000
"""

import argparse
import os

import pandas as pd

import turtle_config as cfg
from turtle_core import calculate_n

DATA_DIR = 'data_yahoo'
IBKR_DIR = 'data'       # IBKR files from turtle_backtest.py --download, used to check price units

YAHOO_SYMBOLS = {
    'MES': 'ES=F', 'MNQ': 'NQ=F', 'M2K': 'RTY=F', 'MYM': 'YM=F',
    'MGC': 'GC=F', 'SIL': 'SI=F', 'MHG': 'HG=F',
    'MCL': 'CL=F', 'MHNG': 'NG=F',
    'MZC': 'ZC=F', 'MZW': 'ZW=F', 'MZS': 'ZS=F', 'MZM': 'ZM=F', 'MZL': 'ZL=F',
}


def clean(df):
    """
    Drops unusable bars. Returns (clean_df, notes).
    Expects columns date, open, high, low, close.
    """
    notes = []
    before = len(df)
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    df = df[(df[['open', 'high', 'low', 'close']] > 0).all(axis=1)]
    if len(df) < before:
        notes.append(f"dropped {before - len(df)} empty/zero bars")
    # high/low must contain open and close
    bad = (df['high'] < df[['open', 'close']].max(axis=1)) | (df['low'] > df[['open', 'close']].min(axis=1))
    if bad.any():
        df = df.copy()
        df.loc[bad, 'high'] = df.loc[bad, ['open', 'high', 'close']].max(axis=1)
        df.loc[bad, 'low'] = df.loc[bad, ['open', 'low', 'close']].min(axis=1)
        notes.append(f"repaired {int(bad.sum())} bars whose high/low didn't contain the open/close")
    df = df.drop_duplicates('date').sort_values('date').reset_index(drop=True)
    return df, notes


def big_gaps(df, threshold_n=3.0):
    """Overnight gaps (open vs prior close) larger than threshold_n * N - roll jumps show up here."""
    n = calculate_n(df).shift(1)
    gap = (df['open'] - df['close'].shift(1)) / n
    out = df.assign(gap_n=gap)[gap.abs() > threshold_n]
    return out[['date', 'gap_n']]


def unit_check(key, df):
    """Compares recent Yahoo closes with the IBKR file, if there is one. Returns a note or None."""
    path = os.path.join(IBKR_DIR, f"{key}.csv")
    if not os.path.exists(path):
        return None
    ib = pd.read_csv(path, parse_dates=['date'])
    both = df.merge(ib, on='date', suffixes=('', '_ib')).tail(20)
    if both.empty:
        return None
    ratio = (both['close'] / both['close_ib']).median()
    if abs(ratio - 1) > 0.05:
        return (f"PRICE UNITS DIFFER from IBKR (Yahoo/IBKR = {ratio:.3g}) - sizing would be wrong. "
                f"Do NOT backtest this market until fixed.")
    return f"price units match IBKR (Yahoo/IBKR = {ratio:.3f})"


def download(start):
    import yfinance as yf
    os.makedirs(DATA_DIR, exist_ok=True)
    for m in cfg.MARKETS:
        sym = YAHOO_SYMBOLS[m.key]
        raw = yf.Ticker(sym).history(start=start, auto_adjust=False)
        if raw is None or raw.empty:
            print(f"  {m.key}: nothing returned for {sym}, skipping.")
            continue
        df = pd.DataFrame({'date': raw.index.tz_localize(None).normalize(),
                           'open': raw['Open'].values, 'high': raw['High'].values,
                           'low': raw['Low'].values, 'close': raw['Close'].values})
        df, notes = clean(df)
        path = os.path.join(DATA_DIR, f"{m.key}.csv")
        df.to_csv(path, index=False)

        gaps = big_gaps(df)
        flat = (df['high'] == df['low']).mean()
        print(f"  {m.key} ({sym}): {len(df)} bars, {df['date'].iloc[0].date()} to "
              f"{df['date'].iloc[-1].date()} -> {path}")
        for note in notes:
            print(f"      {note}")
        units = unit_check(m.key, df)
        if units:
            print(f"      {units}")
        print(f"      overnight gaps > 3N (roll jumps look like this): {len(gaps)};  "
              f"bars with no range: {flat:.1%}")


def main():
    parser = argparse.ArgumentParser(description="Download free daily futures history from Yahoo Finance")
    parser.add_argument('--start', default='2000-01-01', help="first date to download")
    args = parser.parse_args()
    download(args.start)
    print(f"\nNext: python turtle_backtest.py --data-dir {DATA_DIR} --equity 75000")


if __name__ == '__main__':
    main()
