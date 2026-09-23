"""
Turtle Trading System - Core Rules

Pure strategy logic with no IBKR code in it: indicators, signals, position
sizing, pyramiding, the shared stop, and portfolio heat limits. Both the live
trader and the backtester call these same functions, so what gets backtested
is exactly what trades.
"""

import math
from dataclasses import dataclass, field, asdict

import pandas as pd

import turtle_config as cfg


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def flat_bars(df):
    """
    Bars whose open, high, low and close are all the same price. IBKR's older
    continuous-futures history is full of these (a closing price only, no real
    range). They make N far too small - which makes positions too big and stops
    too tight - so neither the trader nor the backtest may use them.
    """
    return df['high'] == df['low']


def trim_flat_history(df, window=20, max_flat=0.25):
    """
    Drops everything up to the last 20-bar stretch that is more than 25% flat bars.
    Returns (trimmed_df, date_of_last_bad_bar or None).
    """
    bad = flat_bars(df).astype(float).rolling(window, min_periods=1).mean() > max_flat
    if not bad.any():
        return df, None
    last_bad = bad[bad].index[-1]
    return df.loc[last_bad + 1:].reset_index(drop=True), df['date'][last_bad]


def calculate_true_range(df):
    """True Range = the largest of: high-low, abs(high-prev_close), abs(low-prev_close)"""
    prev_close = df['close'].shift(1)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - prev_close).abs()
    tr3 = (df['low'] - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def calculate_n(df, lookback=cfg.N_LOOKBACK):
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


def add_indicators(df):
    """
    Adds N and the breakout channels to a daily OHLC DataFrame.
    Channels are shifted one day so each row compares its close against the
    PRIOR days' range - today's own bar never counts itself.
    """
    df = df.copy()
    df['N'] = calculate_n(df)
    df['entry_high'] = df['high'].rolling(cfg.ENTRY_LOOKBACK).max().shift(1)
    df['entry_low'] = df['low'].rolling(cfg.ENTRY_LOOKBACK).min().shift(1)
    df['exit_high'] = df['high'].rolling(cfg.EXIT_LOOKBACK).max().shift(1)
    df['exit_low'] = df['low'].rolling(cfg.EXIT_LOOKBACK).min().shift(1)
    return df


# ---------------------------------------------------------------------------
# Signals and sizing
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """One market's latest daily bar plus indicators - everything a decision needs."""
    key: str
    date: str
    close: float
    n: float
    entry_high: float
    entry_low: float
    exit_high: float
    exit_low: float
    point_value: float

    @classmethod
    def from_row(cls, key, row, point_value):
        return cls(key=key, date=str(row['date']), close=float(row['close']), n=float(row['N']),
                   entry_high=float(row['entry_high']), entry_low=float(row['entry_low']),
                   exit_high=float(row['exit_high']), exit_low=float(row['exit_low']),
                   point_value=point_value)


def entry_signal(snap):
    """+1 for a long breakout, -1 for a short breakout, 0 for none."""
    if snap.close > snap.entry_high:
        return 1
    if snap.close < snap.entry_low:
        return -1
    return 0


def exit_signal(snap, direction):
    """True if the close broke the 50-day channel against an open position."""
    if direction > 0:
        return snap.close < snap.exit_low
    return snap.close > snap.exit_high


def unit_size(equity, n, point_value):
    """
    Contracts per unit so that a SIZING_N * N move against us loses
    RISK_PER_UNIT of equity. Always rounds DOWN - never risks more than
    the rule allows. Returns 0 when even one contract is too much risk.
    """
    risk_per_contract = cfg.SIZING_N * n * point_value
    if not risk_per_contract > 0 or not equity > 0:
        return 0
    return math.floor(equity * cfg.RISK_PER_UNIT / risk_per_contract)


def round_to_tick(price, tick):
    if not tick:
        return price
    return round(round(price / tick) * tick, 10)


# ---------------------------------------------------------------------------
# Positions, pyramiding and the shared stop
# ---------------------------------------------------------------------------

@dataclass
class Unit:
    qty: int
    price: float
    date: str


@dataclass
class Position:
    key: str
    direction: int              # +1 long, -1 short
    entry_n: float              # N when the first unit was entered; fixed for the whole trade
    stop: float                 # one shared stop for every unit
    units: list = field(default_factory=list)
    # Live-trading details (unused by the backtester)
    con_id: int | None = None
    local_symbol: str | None = None
    expiry: str | None = None
    stop_perm_id: int | None = None

    @property
    def qty(self):
        return sum(u.qty for u in self.units)

    @property
    def unit_count(self):
        return len(self.units)

    @property
    def last_price(self):
        return self.units[-1].price

    @property
    def next_add_price(self):
        """Price at which the next pyramid unit is due (0.5N beyond the last fill)."""
        return self.last_price + self.direction * cfg.PYRAMID_N * self.entry_n

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d['units'] = [Unit(**u) for u in d.get('units', [])]
        return cls(**d)


def open_position(key, direction, qty, fill_price, n, date, tick=None):
    stop = round_to_tick(fill_price - direction * cfg.STOP_N * n, tick)
    return Position(key=key, direction=direction, entry_n=n, stop=stop,
                    units=[Unit(qty=qty, price=fill_price, date=date)])


def add_unit(pos, qty, fill_price, date, tick=None):
    """
    Adds a pyramid unit and moves the shared stop to 2N from the newest fill.
    The stop only ever tightens - it never moves back toward the loss side.
    """
    pos.units.append(Unit(qty=qty, price=fill_price, date=date))
    new_stop = round_to_tick(fill_price - pos.direction * cfg.STOP_N * pos.entry_n, tick)
    if pos.direction > 0:
        pos.stop = max(pos.stop, new_stop)
    else:
        pos.stop = min(pos.stop, new_stop)
    return pos


def pyramid_due(pos, close):
    if pos.unit_count >= cfg.MAX_UNITS_PER_MARKET:
        return False
    if pos.direction > 0:
        return close >= pos.next_add_price
    return close <= pos.next_add_price


def stop_hit(pos, low, high):
    if pos.direction > 0:
        return low <= pos.stop
    return high >= pos.stop


# ---------------------------------------------------------------------------
# Daily decision - shared by live trading and the backtest
# ---------------------------------------------------------------------------

@dataclass
class Action:
    kind: str          # 'exit', 'add' or 'entry'
    key: str
    direction: int
    qty: int
    reason: str


class HeatTracker:
    """Counts units per correlation group and per direction as actions are planned."""

    def __init__(self, positions):
        self.group = {}
        self.side = {1: 0, -1: 0}
        for pos in positions.values():
            self.add(pos.key, pos.direction, pos.unit_count)

    def add(self, key, direction, units=1):
        g = cfg.MARKETS_BY_KEY[key].group
        self.group[g] = self.group.get(g, 0) + units
        self.side[direction] += units

    def blocked_by(self, key, direction):
        g = cfg.MARKETS_BY_KEY[key].group
        if self.group.get(g, 0) >= cfg.MAX_UNITS_PER_GROUP:
            return f"group '{g}' already at {cfg.MAX_UNITS_PER_GROUP} units"
        if self.side[direction] >= cfg.MAX_UNITS_PER_DIRECTION:
            side = 'long' if direction > 0 else 'short'
            return f"already {cfg.MAX_UNITS_PER_DIRECTION} {side} units across the portfolio"
        return None


def plan_actions(positions, snapshots, equity, skip=()):
    """
    Decides today's orders from today's closes.

    positions: {key: Position} currently open
    snapshots: {key: Snapshot} latest bar per market (markets missing here are ignored)
    equity:    account equity used for sizing
    skip:      market keys to leave alone this run (e.g. mismatched with the broker)

    Returns (actions, notes). Order of work: exits first (frees up heat),
    then pyramid adds on surviving positions, then new entries in market-list order.
    """
    actions, notes = [], []
    surviving = {}

    for key, pos in positions.items():
        snap = snapshots.get(key)
        if key in skip or snap is None:
            surviving[key] = pos
            continue
        if exit_signal(snap, pos.direction):
            actions.append(Action('exit', key, -pos.direction, pos.qty,
                                  f"{cfg.EXIT_LOOKBACK}-day breakout against position"))
        else:
            surviving[key] = pos

    heat = HeatTracker(surviving)

    for key, pos in surviving.items():
        snap = snapshots.get(key)
        if key in skip or snap is None or not pyramid_due(pos, snap.close):
            continue
        qty = unit_size(equity, pos.entry_n, snap.point_value)
        blocked = heat.blocked_by(key, pos.direction)
        if qty < 1:
            notes.append(f"{key}: pyramid add due but unit size rounds to 0 contracts")
        elif blocked:
            notes.append(f"{key}: pyramid add due but {blocked}")
        else:
            heat.add(key, pos.direction)
            actions.append(Action('add', key, pos.direction, qty,
                                  f"unit {pos.unit_count + 1}: close {snap.close:g} reached {pos.next_add_price:g}"))

    for m in cfg.MARKETS:
        key = m.key
        snap = snapshots.get(key)
        if key in positions or key in skip or snap is None:
            continue
        direction = entry_signal(snap)
        if direction == 0:
            continue
        side = 'LONG' if direction > 0 else 'SHORT'
        qty = unit_size(equity, snap.n, snap.point_value)
        blocked = heat.blocked_by(key, direction)
        if qty < 1:
            risk = cfg.SIZING_N * snap.n * snap.point_value
            notes.append(f"{key}: {side} entry signal but 1 contract risks ${risk:,.0f} "
                         f"vs ${equity * cfg.RISK_PER_UNIT:,.0f} allowed - skipped")
        elif blocked:
            notes.append(f"{key}: {side} entry signal but {blocked} - skipped")
        else:
            heat.add(key, direction)
            actions.append(Action('entry', key, direction, qty,
                                  f"{cfg.ENTRY_LOOKBACK}-day breakout {'up' if direction > 0 else 'down'}"))

    return actions, notes
