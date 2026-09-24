# Turtle Trader

An automated Turtle-style trend-following system for 14 micro futures on
Interactive Brokers. Full rules and background: [turtle_system_handoff.md](turtle_system_handoff.md).

## Files

| File | What it does |
|---|---|
| `turtle_config.py` | **All settings in one place**: markets, lookbacks, risk %, heat limits, TWS port |
| `turtle_core.py` | The rules: N, breakouts, sizing, pyramiding, shared stop, heat limits. No IBKR code. |
| `turtle_trader.py` | **The daily runner.** Places real orders on your paper account. |
| `turtle_state.py` | Saves open positions to `turtle_state.json` between runs |
| `turtle_backtest.py` | Replays history through the same rules |
| `turtle_signals.py` | The original signal scanner (read-only, still works on its own) |
| `test_connection.py` | Checks the TWS connection |
| `orb_backtest.py` | **Second strategy:** 15-minute opening range breakout backtest (MES/MNQ/MYM) |
| `first_candle_backtest.py` | **Third strategy:** first 5-minute candle direction (Zarattini & Aziz 2023 rules) |
| `tests/` | Automated tests, no TWS needed: `python -m pytest` |

## Setup (Windows Command Prompt)

```
cd path\to\Turtle-Trader
python -m pip install -r requirements.txt
python -m pytest
```

## First runs - do these in order

1. **Download history and backtest** (TWS open):
   ```
   python turtle_backtest.py --download --years 15
   python turtle_backtest.py --equity 75000
   ```
   Pay attention to the "too small to size" line. See *Account size* below.

2. **Dry run the trader** (TWS open, paper account):
   ```
   python turtle_trader.py --dry-run
   ```
   Nothing gets placed or saved. Check two things:
   - No `IBKR says $X/point but turtle_config expects $Y/point` errors. If you see one,
     that market is blocked until the point value in `turtle_config.py` is corrected.
     This matters most for the micro grains, which may be quoted in cents.
   - The unit-size table at the end looks sensible.

3. **Trade for real on paper:**
   ```
   python turtle_trader.py
   ```

## When to run it

Run it **once each Sunday-Thursday evening, between about 8:30 and 10:00 PM US Eastern**
(Sunday evening handles Friday's bars):
- All the day sessions have closed, so the daily bars are final (if you run it earlier, it
  ignores today's unfinished bar and uses yesterday's).
- The overnight Globex session is open, so the market orders actually fill. (Friday night
  it isn't, so nothing fills. The trader notices and retries the same signals on Sunday.)

Running it twice on the same day is harmless. The second run sees there are no new bars and stops.
To automate it, create a Windows Task Scheduler task that runs `python turtle_trader.py`
in this folder Sunday-Thursday evenings, with TWS left logged in. Note that TWS logs
itself out once a day by default. In TWS, set Global Configuration > Lock and Exit > Auto restart.

Stops are real GTC stop orders at IBKR, so positions stay protected when the script
isn't running. If a stop fills overnight, the next run notices and records it.

## What the trader will NOT touch

It leaves a market alone and logs an ERROR when:
- IBKR's position doesn't match what the trader recorded, e.g. you traded it by hand
- IBKR holds a position in one of the 14 markets that the trader didn't open
- IBKR's contract multiplier disagrees with `turtle_config.py`

## Account size

At 1% risk per unit with a 2N stop, **a $15k-$50k account can't trade some of these
markets at all**, because one micro contract already risks more than 1%. For example,
with MES at N ≈ 60 points, one contract risks 2 × 60 × $5 = $600, while 1% of $25k is
$250. The trader never rounds up, so it skips those signals and logs why. The dry-run
table shows the size for every market, and the backtest counts the skipped signals.

The settings that change this are in `turtle_config.py`: `RISK_PER_UNIT`, and `SIZING_N`
(set it to `1.0` for the original Turtles' sizing, where 1% of equity = a 1N move,
so each unit is about twice as large and risks about 2% to the stop).

## Second strategy: 15-minute opening range breakout

Separate from the Turtle system. The 2nd 15-minute candle (9:45-10:00 ET) is the anchor;
a break above its high is a buy and a break below its low is a short, with the stop on the other side of the anchor.
It risks 1% per trade on MES, MNQ and MYM, and is always flat by the close. The backtest compares two entry
windows (3rd candle only, or until noon) against five exits (hold to close, 1R/2R/3R targets,
and a 1R trailing stop).

```
python orb_backtest.py --download     # ~20 min: IBKR limits how fast history can be requested
python orb_backtest.py
```
Settings are at the top of `orb_backtest.py`.

## Third strategy: first 5-minute candle

Follows Zarattini & Aziz (2023). If the 9:30-9:35 candle closes up, buy at 9:35; if it closes down, short.
The stop goes at the other end of that candle. Risk is 1% per trade with a 4x leverage cap.
The paper's exit is a 10R target or the close; four other exits are compared alongside it.

```
python first_candle_backtest.py --download
python first_candle_backtest.py
```
