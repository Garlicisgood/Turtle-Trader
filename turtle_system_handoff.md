# Automated Turtle Trading System - Project Handoff

## Overview
Building an automated futures trading system on Interactive Brokers, based on the
classic Turtle Trading rules, adapted with a longer-term entry window inspired by
Jerry Parker's current approach (he still runs 100/120/150-day breakout systems
at Chesapeake Capital). Fully automated execution - no manual trade approval.

Account: IBKR paper trading (for now), Python via `ib_async`.

## Strategy Rules

| Rule | Value |
|---|---|
| Entry | 100-day breakout of the high (long) or low (short) |
| Exit | 50-day breakout of the opposite extreme |
| Initial stop | 2N from entry (N = volatility unit, see below) |
| Position sizing | Fixed 1% of account equity risked per unit |
| Pyramiding | Classic - add a unit every 0.5N in favor, up to 4 units total |
| Stop management | One shared stop across all pyramided units, trails up together |
| Execution | Fully automated (no manual "green light" step) |

**N calculation:** classic Turtle method - a 20-day smoothed (Wilder-style) average
of True Range. `N_today = (19 * N_yesterday + TR_today) / 20`. True Range is the
largest of: high-low, abs(high - prev_close), abs(low - prev_close).

## Market List (14 markets)

All are Micro futures contracts. **Watch the IBKR symbol quirks marked below** -
these caused silent failures during testing and took real debugging to track down.

| Sector | Display Name | IBKR Symbol | Exchange | Trading Class | Notes |
|---|---|---|---|---|---|
| Index | S&P 500 (Micro) | MES | CME | - | |
| Index | Nasdaq-100 (Micro) | MNQ | CME | - | |
| Index | Russell 2000 (Micro) | M2K | CME | - | |
| Index | Dow (Micro) | MYM | CBOT | - | |
| Metal | Gold (Micro) | MGC | COMEX | - | |
| Metal | Silver (Micro) | **SI** | COMEX | **SIL** | ⚠️ IBKR lists Micro Silver under root symbol "SI" (same as full-size), distinguished by tradingClass "SIL" - NOT under symbol "SIL" directly |
| Metal | Copper (Micro) | MHG | COMEX | - | |
| Energy | Crude Oil (Micro) | MCL | NYMEX | - | |
| Energy | Natural Gas (Micro) | **MHNG** | NYMEX | - | ⚠️ CME's Globex code is "MNG" but IBKR's internal symbol is "MHNG" (spelled out) - using "MNG" returns "no security definition found" |
| Grain | Corn (Micro) | MZC | CBOT | - | |
| Grain | Wheat (Micro) | MZW | CBOT | - | |
| Grain | Soybeans (Micro) | MZS | CBOT | - | |
| Grain | Soybean Meal (Micro) | MZM | CBOT | - | Correlated with MZS/MZL - treat as one group for portfolio heat |
| Grain | Soybean Oil (Micro) | MZL | CBOT | - | Correlated with MZS/MZM - treat as one group for portfolio heat |

## Technical Setup (already done)
- TWS installed, API enabled, Read-Only API disabled
- Market data: "US Securities Snapshot and Futures Value Bundle" subscribed ($10/mo,
  waived at $30+/mo commissions) - covers CME, CBOT, NYMEX, COMEX
- Market Data API Acknowledgement form signed
- Python 3.12 installed, `ib_async` and `pandas` installed
- Paper trading account connection confirmed working

## Current Code Status
Two scripts exist so far (both tested working):

1. **test_connection.py** - Minimal connection test. Confirms TWS connection,
   pulls account summary, confirms market data flows through the API.

2. **turtle_signals.py** - Pulls ~250 days of daily history for all 14 markets,
   calculates N, checks for 100-day entry / 50-day exit breakout signals, prints
   a summary. Handles the Silver and Natural Gas symbol quirks above. Falls back
   to finding the nearest dated contract via `reqContractDetails` when a
   continuous contract (`ContFuture`) isn't available for a given market.

**This script only detects and prints signals - it does not yet:**
- Size positions (the 1% risk / N-based sizing)
- Place any orders
- Track open positions or manage pyramiding
- Manage the shared trailing stop

## Next Steps (not yet built)
1. Position sizing: given account equity, N, and contract point value, calculate
   how many contracts = 1% risk per unit
2. Order placement: fully automated (`transmit=True`), initial stop at 2N
3. Pyramiding logic: track open positions, add units at 0.5N intervals up to 4,
   move the shared stop as units are added
4. Position/state tracking between runs (this needs to run daily, not just once -
   requires persisting what positions are open, entry prices, current stop level)
5. Backtest against historical data before going live
6. Extended paper trading before moving to the funded account

## Important Context
- Account size: $15k-$50k paper trading (funded account will match this range)
- User has some coding experience (self-described as "not very experienced but
  knowledgeable") - prefers things explained clearly, has been running commands
  via Windows Command Prompt
- A second, separate strategy (15-minute opening range breakout) is planned for
  later - not part of this build
