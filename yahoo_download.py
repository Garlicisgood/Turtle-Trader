"""
Turtle Trading System - Yahoo Finance History Downloader

Free daily history for each market's full-size contract (ES, GC, ZC...) from
Yahoo Finance, going back 20+ years for most markets. No TWS or API key needed.

Saves data_yahoo/<KEY>.csv in the same date,open,high,low,close format the
backtest reads, plus data_yahoo/report.txt listing possible roll jumps and bad
bars for each market.

Know the limits before trusting a result:
  - Yahoo does NOT adjust for rolls. It joins each contract to the next one, so
    on every roll day the price jumps by the gap between the two months. The
    backtest can't tell that jump from a real move. Small for stock indexes and
    gold; 5-15% per roll is common for natural gas, crude and the grains.
  - It misses the cost of rolling, so long natural gas and crude look better
    than they would have traded.
  - Occasional bad prints and missing days. The report flags the obvious ones.
Good for a rough first look. Confirm with properly roll-adjusted data before
real money.

Usage:
    python yahoo_download.py
    python turtle_backtest.py --data data_yahoo --equity 75000
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

import pandas as pd

import turtle_config as cfg
from turtle_core import calculate_n, flat_bars, trim_flat_history

DATA_DIR = 'data_yahoo'
URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1=0&period2={end}&interval=1d'

GAP_N = 2.0      # flag an open-vs-previous-close gap bigger than this many N
MOVE_N = 4.0     # flag a close-to-close move bigger than this many N
MAX_LISTED = 15  # largest flagged days listed per market in the report


def fetch(ticker, retries=3):
    """Daily bars for one Yahoo ticker as a date,open,high,low,close DataFrame."""
    url = URL.format(ticker=ticker, end=int(time.time()) + 86400)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise RuntimeError(f"{ticker}: {e}") from e
            time.sleep(3 * (attempt + 1))

    result = (payload.get('chart') or {}).get('result')
    if not result:
        raise RuntimeError(f"{ticker}: {(payload.get('chart') or {}).get('error')}")
    result = result[0]
    quote = result['indicators']['quote'][0]
    tz = result['meta'].get('exchangeTimezoneName', 'America/New_York')
    dates = pd.to_datetime(result['timestamp'], unit='s', utc=True).tz_convert(tz)
    df = pd.DataFrame({'date': dates.tz_localize(None).normalize(),
                       'open': quote['open'], 'high': quote['high'],
                       'low': quote['low'], 'close': quote['close']})

    # Today's bar is still forming until the session closes - leave it out.
    today = pd.Timestamp.now(tz=tz).tz_localize(None).normalize()
    return df[df['date'] < today]


def clean(df):
    """Removes unusable rows. Returns (clean_df, list of problem descriptions)."""
    problems = []
    price_cols = ['open', 'high', 'low', 'close']

    dupes = df['date'].duplicated(keep='last')
    if dupes.any():
        problems.append(f"{dupes.sum()} duplicate dates (kept the last of each)")
    df = df[~dupes]

    missing = df[price_cols].isna().any(axis=1)
    if missing.any():
        problems.append(f"{missing.sum()} bars with missing prices (dropped)")
    df = df[~missing]

    nonpos = (df[price_cols] <= 0).any(axis=1)
    if nonpos.any():
        problems.append(f"{nonpos.sum()} bars with zero/negative prices (dropped): "
                        + _dates(df.loc[nonpos, 'date']))
    df = df[~nonpos].reset_index(drop=True)

    # Kept, but reported: the backtest would use these as-is.
    bad_range = ((df['high'] < df['low'])
                 | (df[['open', 'close']].max(axis=1) > df['high'] * 1.0001)
                 | (df[['open', 'close']].min(axis=1) < df['low'] * 0.9999))
    if bad_range.any():
        problems.append(f"{bad_range.sum()} bars with open/close outside high-low (kept): "
                        + _dates(df.loc[bad_range, 'date']))

    gaps = df['date'].diff().dt.days
    long_gaps = df.loc[gaps > 7, 'date']
    if len(long_gaps):
        problems.append(f"{len(long_gaps)} gaps of more than a week with no bars, ending: "
                        + _dates(long_gaps))
    return df, problems


def _dates(dates, limit=8):
    shown = ', '.join(str(d.date()) for d in dates.iloc[:limit])
    return shown + (f" ... (+{len(dates) - limit} more)" if len(dates) > limit else '')


def jump_report(df):
    """
    Days where price jumped far more than normal, measured in N (20-day average
    true range, from the PRIOR days). On unadjusted Yahoo data most of the
    big open gaps are roll days; some are real news (e.g. March 2020).
    """
    df = df.copy()
    df['N'] = calculate_n(df).shift(1)
    df['prev_close'] = df['close'].shift(1)
    df['gap_pct'] = 100 * (df['open'] - df['prev_close']) / df['prev_close']
    df['gap_N'] = (df['open'] - df['prev_close']) / df['N']
    df['move_N'] = (df['close'] - df['prev_close']) / df['N']
    df = df.iloc[cfg.N_LOOKBACK:]   # N needs 20 bars to settle
    flagged = df[(df['gap_N'].abs() > GAP_N) | (df['move_N'].abs() > MOVE_N)]
    return flagged.reindex(flagged['gap_pct'].abs().sort_values(ascending=False).index)


def main():
    parser = argparse.ArgumentParser(description="Download daily futures history from Yahoo Finance")
    parser.add_argument('--markets', help="comma-separated keys, e.g. MES,MGC (default: all)")
    args = parser.parse_args()

    keys = args.markets.split(',') if args.markets else [m.key for m in cfg.MARKETS]
    unknown = [k for k in keys if k not in cfg.MARKETS_BY_KEY]
    if unknown:
        raise SystemExit(f"Unknown market keys: {unknown}")

    os.makedirs(DATA_DIR, exist_ok=True)
    lines = ["Yahoo Finance download report - UNADJUSTED continuous contracts",
             f"Flagged: open gap > {GAP_N:g}N or close-to-close move > {MOVE_N:g}N "
             f"(N = prior 20-day average true range)",
             "Most big open gaps on unadjusted data are roll days. Check the % column: "
             "that is how far a roll can fake a breakout or trip a stop.", ""]

    for key in keys:
        m = cfg.MARKETS_BY_KEY[key]
        ticker = f"{m.history_symbol}=F"
        try:
            raw = fetch(ticker)
        except RuntimeError as e:
            print(f"  {key}: download failed - {e}")
            lines += [f"=== {key} ({ticker}) - DOWNLOAD FAILED: {e}", ""]
            continue

        df, problems = clean(raw)
        path = os.path.join(DATA_DIR, f"{key}.csv")
        df.to_csv(path, index=False, date_format='%Y-%m-%d')

        usable, bad_until = trim_flat_history(df)
        flat = int(flat_bars(df).sum())
        flagged = jump_report(usable) if len(usable) > cfg.N_LOOKBACK else pd.DataFrame()
        years = (usable['date'].iloc[-1] - usable['date'].iloc[0]).days / 365.25 if len(usable) else 0

        print(f"  {key}: {len(df)} bars of {ticker} ({df['date'].iloc[0].date()} to "
              f"{df['date'].iloc[-1].date()}), {len(flagged)} jumps flagged -> {path}")

        lines.append(f"=== {key} {m.name} - Yahoo {ticker}")
        lines.append(f"  {len(df)} bars, {df['date'].iloc[0].date()} to {df['date'].iloc[-1].date()}")
        if bad_until is not None:
            lines.append(f"  Early history has no real high/low until {bad_until.date()}; "
                         f"the backtest skips it and uses {len(usable)} bars ({years:.1f} years)")
        elif flat:
            lines.append(f"  {flat} scattered bars with high == low")
        lines += [f"  PROBLEM: {p}" for p in problems]
        if len(flagged):
            per_year = len(flagged) / years if years else 0
            lines.append(f"  {len(flagged)} flagged days (~{per_year:.1f} per year). Largest:")
            lines.append(f"    {'date':<12}{'prev close':>12}{'open':>12}{'close':>12}"
                         f"{'gap %':>9}{'gap N':>8}{'move N':>8}")
            for _, r in flagged.head(MAX_LISTED).iterrows():
                lines.append(f"    {str(r['date'].date()):<12}{r['prev_close']:>12.4g}"
                             f"{r['open']:>12.4g}{r['close']:>12.4g}{r['gap_pct']:>+9.2f}"
                             f"{r['gap_N']:>+8.1f}{r['move_N']:>+8.1f}")
        else:
            lines.append("  no jumps flagged")
        lines.append("")
        time.sleep(1)   # be polite to Yahoo

    report_path = os.path.join(DATA_DIR, 'report.txt')
    with open(report_path, 'w') as f:
        f.write('\n'.join(lines))
    print(f"\n  Report: {report_path}")
    print(f"  Backtest it with:  python turtle_backtest.py --data {DATA_DIR} --equity 75000")


if __name__ == '__main__':
    main()
