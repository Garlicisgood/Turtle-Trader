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
| Metal | Silver (Micro) | **SI** | COMEX | **SIL** | ⚠️ IBKR lists Micro Silver under root symbol "SI" (same as full-size), distinguished by tradingClass "SIL" - NOT under symbol "SIL" directly. Also, the continuous contract (ContFuture) IGNORES tradingClass and returns full-size SI (5000 oz), so the trader uses the dated SIL contract instead |
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
Steps 1-5 of the original plan are built. Nothing has been run against TWS yet: the
new code is tested only offline (`python -m pytest`, 23 tests, including a simulated
IBKR for the order flow).

| File | Status |
|---|---|
| `test_connection.py` | Original, tested working against TWS |
| `turtle_signals.py` | Original read-only scanner, tested working against TWS. Kept as-is. |
| `turtle_config.py` | All parameters and the market list (with the symbol quirks above) |
| `turtle_core.py` | Rules as pure functions: N, channels, sizing, pyramiding, shared stop, heat limits, and `plan_actions()`, the daily decision used by both the trader and the backtest |
| `turtle_state.py` | Persists open positions to `turtle_state.json` (atomic write + `.bak`) |
| `turtle_trader.py` | Daily runner: reconcile with IBKR -> exits -> pyramid adds -> entries. `--dry-run` flag. |
| `turtle_backtest.py` | Portfolio backtest using the same `plan_actions()`, with history from the full-size contracts |

### Decisions made while building (revisit if wanted)
- **Sizing:** 1% of equity lost if price reaches the 2N stop:
  `contracts = floor(equity * 1% / (2 * N * point_value))`. The original Turtles used
  1% per **1N**, which is twice the size. It's one setting (`SIZING_N`) in the config.
  At $15-50k many markets size to **0 contracts**. They are skipped and logged, never
  rounded up.
- **Signals on daily closes** (as in `turtle_signals.py`). Orders are market orders sent
  in the evening Globex session. The backtest fills them at the next day's open.
- **N is fixed at entry** for the whole position: 0.5N pyramid spacing, the 2N stop, and
  sizing for the added units. The shared stop moves to 2N from the newest fill, only
  ever tightens, and exists as one GTC stop order at IBKR (`outsideRth=True`).
- **Pyramiding:** at most one added unit per day.
- **Heat limits (classic):** 4 units per market, 6 per correlated group, 12 per
  direction. Groups: equity_index (MES/MNQ/M2K/MYM), precious (MGC/SIL), copper,
  energy (MCL/MHNG), grains (MZC/MZW), soy (MZS/MZM/MZL).
- **Safety:** refuses a non-paper account without `--live-account`, leaves alone any
  market where IBKR's position disagrees with saved state, and blocks a market whose
  IBKR multiplier disagrees with the config's point value.

- **Paper sizing:** the paper account holds $1M, so `PAPER_STARTING_EQUITY = 75000` makes
  the trader size as a $75k account plus the actual P&L since the first run.
- **Account size target: $75k** (chosen 2026-09-23). At today's N that trades 11 markets
  (everything except MGC, MNQ and SIL, which need roughly $160k+ for 1 contract).

## First dry run against TWS (2026-09-23)
- All 14 markets loaded. The point values for 13 markets matched IBKR, including the
  micro grains, which are quoted in cents.
- The Micro Silver full-size bug was caught by the point-value check and has been fixed.
- Sizing at $25k (1% = $250, 2N stop): only MZC (2 contracts), MZW, MZS, MZM, MZL and
  MHNG (1 each) can be traded. MES/MNQ/M2K/MYM/MGC/MHG/MCL/SIL all risk more than
  $250 per contract.

## Backtest findings (2026-09-23)
- **IBKR's continuous-futures daily history can't be used for backtesting before ~2023-2025.**
  It is back-adjusted for rolls (no roll gaps), but large stretches of older bars have
  open = high = low = close. That made N 2-6x too small, so positions were far too big and
  stops far too tight. The first backtest (-83% max drawdown) was invalid because of it.
  Both programs now reject these bars (`trim_flat_history` / `flat_bars` in turtle_core).
  Clean history is only ~1-4 years per market (MHNG from 2023-09, MZC from 2022-10,
  equity indexes only from 2025).
- On that short clean window (2022-10 to 2026-09, mostly 2024-26) every variant tested lost money:
  as built -37% (maxDD -49%, 6% winners, 64/68 exits by the 2N stop); no pyramiding -13%;
  no pyramiding + 3N stop -17% (maxDD -19%); no hard stop (50-day exit only) -18%.
  The window is far too short to judge a 100-day system, and pyramiding with the stop
  2N from the newest unit did the most damage.
- A hand-checked trade (MZL long 2025-04-03) matched the rules exactly.
- **Needed next:** 15-20+ years of proper back-adjusted daily futures data from a data
  vendor, loaded as data/<KEY>.csv (date,open,high,low,close).

## Next Steps
1. Re-run `--dry-run` to confirm the silver fix and the $25k sizing.
2. Run `turtle_backtest.py --download` and review the results, especially how many
   signals are skipped because they're too small to size at the actual account size.
3. **Contract rolls are not automated.** The trader warns "ROLL NEEDED" 10 days
   before a held contract expires and won't pyramid into an old contract, but the
   roll itself (close old, open new, move the stop) is still manual. This must be
   automated before the system can run unattended for months.
4. Schedule it (Windows Task Scheduler, Sun-Thu ~8:30-10pm ET) and paper trade.
5. Extended paper trading before moving to the funded account.

## Second Strategy: 15-Minute Opening Range Breakout (started 2026-09-23)
`orb_backtest.py` is a backtest only. Nothing trades live yet.
- Markets: MES, MNQ, MYM (user's choice). Risk 1% of equity per trade (user's choice).
- Anchor = 2nd 15-min RTH candle (9:45-10:00 ET). Buy stop 1 tick above the anchor high, sell
  stop 1 tick below its low. Initial stop = the other side of the anchor. One trade per market per day.
- Two entry windows compared (user asked for both): 3rd candle only / any candle until noon.
- Five exits compared: hold to close, 1R/2R/3R target, 1R trailing stop. Always flat by 4pm.
- 15-min bars can't show order of events, so the worst case is always assumed (stop wins
  ties with the target; a candle breaking both sides = full loss).
- Data: stitched from each quarterly contract while front month (roll 8 days before
  expiry). No back-adjustment is needed for day trades. IBKR keeps ~2 years of expired data.
- The three index micros are highly correlated, so the report shows how often they
  traded the same direction the same day (real risk ~3x one trade).
- Next: user runs `--download`, then the comparison; pick a rule; build a live ORB trader
  (separate client ID) if the results hold up.

## ORB results (2026-09-24, IBKR 15-min data Dec 2025 - Sep 2026)
IBKR only serves micro index contracts back to Z5, so there are ~9 months of data. All 10 variants lost
(-12% to -28%, max DD 19-47%). Average R was about 0.00 per trade before commissions, so there's no
edge and costs make it a loss. MES/MNQ/MYM traded the same direction 70% of the time. Not pursued.

## Third Strategy: First 5-Minute Candle (started 2026-09-24)
`first_candle_backtest.py`, following Zarattini & Aziz (2023). The 9:30-9:35 candle direction
sets long/short; entry at the 9:35 open; stop at the other end of the candle; 1% risk with a
4x leverage cap (the cap often binds on MES, so those trades risk less than 1%). Exits compared:
paper (10R target or close), hold to close, 2R, 1R trailing, add at +1R with stop to entry.
Same markets (MES/MNQ/MYM), same worst-case assumptions and costs as the ORB.

## Important Context
- Account size: target $75k (paper sizes as $75k; the funded account will match)
- User has some coding experience (self-described as "not very experienced but
  knowledgeable") - prefers things explained clearly, has been running commands
  via Windows Command Prompt
- A second, separate strategy (15-minute opening range breakout) is planned for
  later - not part of this build
