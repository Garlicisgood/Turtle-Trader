"""
Turtle Trading System - Shared Configuration

Every tunable number and the market list live here, so the live trader
(turtle_trader.py) and the backtester (turtle_backtest.py) always run the
exact same rules.
"""

from dataclasses import dataclass


# --- Connection to TWS ---
IB_HOST = '127.0.0.1'
IB_PORT = 7497          # 7497 = TWS paper trading, 7496 = TWS live
IB_CLIENT_ID = 3        # keep this fixed: IBKR only lets the client ID that placed
                        # an order modify it later, and the trader edits its own
                        # stop orders on later runs

# --- Strategy rules (see turtle_system_handoff.md) ---
ENTRY_LOOKBACK = 100    # days for entry breakout
EXIT_LOOKBACK = 50      # days for exit breakout
N_LOOKBACK = 20         # days for N (volatility) calculation
HISTORY_DAYS = 250      # daily bars to pull - must comfortably exceed ENTRY_LOOKBACK

RISK_PER_UNIT = 0.01    # fraction of account equity risked per unit
STOP_N = 2.0            # initial / shared stop distance, in N
PYRAMID_N = 0.5         # add a unit every this many N in our favor
MAX_UNITS_PER_MARKET = 4

# Sizing: a unit risks RISK_PER_UNIT of equity if price travels all the way to
# the stop, i.e. contracts = equity * RISK_PER_UNIT / (STOP_N * N * point_value).
# (The original Turtles sized so that a 1N move = 1% of equity, which means a
# 2N stop risks 2%. To get that instead, set SIZING_N = 1.0.)
SIZING_N = STOP_N

# Paper accounts start at $1,000,000, which sizes nothing like a real $15k-$50k
# account. When set, the trader sizes paper trades as if the account started at
# this amount, plus/minus the actual P&L since the first run. Ignored on a live account.
PAPER_STARTING_EQUITY = 25000   # set to None to size off the real paper balance

# Portfolio heat limits (classic Turtle values), counted in units, not contracts
MAX_UNITS_PER_GROUP = 6        # closely correlated markets (the "group" column below)
MAX_UNITS_PER_DIRECTION = 12   # all longs together, or all shorts together

# --- Execution ---
FILL_TIMEOUT_SECONDS = 60      # how long to wait for a market order to fill
ROLL_WARNING_DAYS = 10         # warn when a held contract expires within this many days
ORDER_REF = 'turtle'           # tag on every order so they're easy to spot in TWS

STATE_FILE = 'turtle_state.json'
LOG_DIR = 'logs'

# --- Backtest costs ---
COMMISSION_PER_CONTRACT = 1.25   # dollars per contract per side (IBKR micro incl. fees, approx.)
SLIPPAGE_TICKS = 1               # ticks lost per fill


@dataclass(frozen=True)
class Market:
    key: str                 # our own unique id for this market (used in the state file)
    name: str
    symbol: str              # IBKR symbol
    exchange: str
    trading_class: str | None
    group: str               # correlation group for portfolio heat limits
    point_value: float       # expected $ per 1.0 move in IBKR's quoted price. The trader
                             # checks this against IBKR's contract details and refuses to
                             # trade the market if they disagree - a wrong point value
                             # silently mis-sizes every trade.
    tick: float              # approximate minimum tick; backtest slippage only (live
                             # orders use IBKR's real minTick)
    history_symbol: str      # full-size contract with the same price quote, used by
                             # the backtester for longer history (the micros are new)


# trading_class is normally None - it's only needed for Micro Silver, which IBKR
# lists under the root symbol "SI" (same as full-size silver) distinguished by
# tradingClass "SIL", rather than under symbol "SIL" itself.
# Micro Natural Gas is "MHNG" at IBKR, not CME's Globex code "MNG".
MARKETS = [
    Market('MES',  "S&P 500 (Micro)",      "MES",  "CME",   None,  'equity_index', 5.0,    0.25,   'ES'),
    Market('MNQ',  "Nasdaq-100 (Micro)",   "MNQ",  "CME",   None,  'equity_index', 2.0,    0.25,   'NQ'),
    Market('M2K',  "Russell 2000 (Micro)", "M2K",  "CME",   None,  'equity_index', 5.0,    0.1,    'RTY'),
    Market('MYM',  "Dow (Micro)",          "MYM",  "CBOT",  None,  'equity_index', 0.5,    1.0,    'YM'),
    Market('MGC',  "Gold (Micro)",         "MGC",  "COMEX", None,  'precious',     10.0,   0.1,    'GC'),
    Market('SIL',  "Silver (Micro)",       "SI",   "COMEX", "SIL", 'precious',     1000.0, 0.005,  'SI'),
    Market('MHG',  "Copper (Micro)",       "MHG",  "COMEX", None,  'copper',       2500.0, 0.0005, 'HG'),
    Market('MCL',  "Crude Oil (Micro)",    "MCL",  "NYMEX", None,  'energy',       100.0,  0.01,   'CL'),
    Market('MHNG', "Natural Gas (Micro)",  "MHNG", "NYMEX", None,  'energy',       1000.0, 0.001,  'NG'),
    Market('MZC',  "Corn (Micro)",         "MZC",  "CBOT",  None,  'grains',       5.0,    0.5,    'ZC'),
    Market('MZW',  "Wheat (Micro)",        "MZW",  "CBOT",  None,  'grains',       5.0,    0.5,    'ZW'),
    Market('MZS',  "Soybeans (Micro)",     "MZS",  "CBOT",  None,  'soy',          5.0,    0.5,    'ZS'),
    Market('MZM',  "Soybean Meal (Micro)", "MZM",  "CBOT",  None,  'soy',          10.0,   0.2,    'ZM'),
    Market('MZL',  "Soybean Oil (Micro)",  "MZL",  "CBOT",  None,  'soy',          60.0,   0.02,   'ZL'),
]

MARKETS_BY_KEY = {m.key: m for m in MARKETS}
