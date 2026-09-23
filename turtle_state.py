"""
Turtle Trading System - State Persistence

The trader runs once a day and exits, so everything it needs to remember
between runs (open positions, entry prices, unit count, the shared stop, the
IBKR stop order) is saved to a JSON file. The file is written atomically, so a
crash mid-write can never leave it half-written, and the previous version is
kept as a .bak copy.
"""

import json
import os
import shutil

from turtle_core import Position


def empty_state():
    return {'positions': {}, 'closed_trades': [], 'last_run': None}


def load_state(path):
    if not os.path.exists(path):
        return empty_state()
    with open(path) as f:
        raw = json.load(f)
    state = empty_state()
    state.update(raw)
    state['positions'] = {k: Position.from_dict(v) for k, v in raw.get('positions', {}).items()}
    return state


def save_state(state, path):
    out = dict(state)
    out['positions'] = {k: p.to_dict() for k, p in state['positions'].items()}
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    if os.path.exists(path):
        shutil.copyfile(path, path + '.bak')
    os.replace(tmp, path)


def record_closed_trade(state, pos, exit_price, date, reason, point_value):
    """Moves a position from open to the closed-trade history, with its P&L."""
    pnl = None
    if exit_price is not None:
        pnl = round(sum((exit_price - u.price) * pos.direction * u.qty for u in pos.units) * point_value, 2)
    state['closed_trades'].append({
        'key': pos.key,
        'direction': 'long' if pos.direction > 0 else 'short',
        'units': pos.unit_count,
        'qty': pos.qty,
        'entries': [[u.date, u.price, u.qty] for u in pos.units],
        'exit_date': date,
        'exit_price': exit_price,
        'reason': reason,
        'pnl': pnl,
    })
    state['positions'].pop(pos.key, None)
