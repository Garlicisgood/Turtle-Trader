"""Tests for the 15-minute opening range breakout rules - no TWS needed."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import orb_backtest as orb

TICK = 0.25
NOON = dt.time(11, 45)


def day(*bars, date=dt.date(2026, 3, 2)):
    """bars: (HH:MM, open, high, low, close) tuples."""
    rows = []
    for t, o, h, l, c in bars:
        clock = dt.datetime.strptime(t, '%H:%M').time()
        rows.append({'datetime': dt.datetime.combine(date, clock), 'date': date, 'time': clock,
                     'open': o, 'high': h, 'low': l, 'close': c})
    return pd.DataFrame(rows)


# Anchor (9:45) runs 100-110 -> buy stop 110.25, sell stop 99.75
OPEN = [('09:30', 100, 105, 98, 104), ('09:45', 104, 110, 100, 106)]


def test_long_breakout_held_to_close():
    d = day(*OPEN, ('10:00', 106, 112, 105, 111), ('10:15', 111, 118, 110, 117), ('15:45', 117, 121, 116, 120))
    t = orb.simulate_day(d, TICK)
    assert (t['direction'], t['entry'], t['stop'], t['exit'], t['reason']) == (1, 110.25, 99.75, 120, 'close')


def test_short_stopped_out():
    d = day(*OPEN, ('10:00', 101, 102, 97, 98), ('10:15', 98, 111, 97, 110))
    t = orb.simulate_day(d, TICK)
    assert (t['direction'], t['entry'], t['exit'], t['reason']) == (-1, 99.75, 110.25, 'stop')


def test_no_break_in_third_candle_means_no_trade():
    d = day(*OPEN, ('10:00', 105, 109, 101, 106), ('10:15', 106, 120, 105, 119))
    assert orb.simulate_day(d, TICK) is None
    # ...but the noon window takes the 10:15 break
    t = orb.simulate_day(d, TICK, trigger_end=NOON)
    assert t['direction'] == 1 and t['entry_time'] == dt.time(10, 15)


def test_noon_window_ignores_breaks_after_noon():
    d = day(*OPEN, ('10:00', 105, 109, 101, 106), ('12:00', 106, 120, 105, 119))
    assert orb.simulate_day(d, TICK, trigger_end=NOON) is None


def test_both_sides_in_one_candle_counts_as_a_loss():
    d = day(*OPEN, ('10:00', 105, 112, 98, 106))
    t = orb.simulate_day(d, TICK)
    assert t['reason'].startswith('both sides') and t['exit'] == t['stop'] < t['entry']


def test_target_hit_and_stop_wins_a_tie():
    # risk = 110.25 - 99.75 = 10.5 -> 2R target = 131.25
    d = day(*OPEN, ('10:00', 106, 112, 105, 111), ('10:15', 111, 132, 110, 130))
    t = orb.simulate_day(d, TICK, target_r=2.0)
    assert (t['exit'], t['reason']) == (131.25, 'target')
    # a candle touching both the stop and the target -> stop
    d = day(*OPEN, ('10:00', 106, 112, 105, 111), ('10:15', 111, 132, 99, 100))
    assert orb.simulate_day(d, TICK, target_r=2.0)['reason'] == 'stop'


def test_gap_through_the_breakout_fills_at_the_open():
    d = day(*OPEN, ('10:00', 113, 115, 112, 114), ('15:45', 114, 116, 113, 115))
    assert orb.simulate_day(d, TICK)['entry'] == 113


def test_trailing_stop_locks_in_profit():
    # risk 10.5; high of 130 -> trailing stop 119.5; next candle drops to 115
    d = day(*OPEN, ('10:00', 106, 112, 105, 111), ('10:15', 111, 130, 110, 129),
            ('10:30', 129, 129, 115, 116), ('15:45', 116, 117, 115, 116))
    t = orb.simulate_day(d, TICK, trail=True)
    assert (t['exit'], t['reason']) == (119.5, 'trailing stop')


def test_tiny_anchor_is_skipped():
    d = day(('09:30', 100, 101, 99, 100), ('09:45', 100, 100.5, 100, 100.25), ('10:00', 100, 105, 99, 104))
    assert orb.simulate_day(d, TICK) is None


def test_run_sizes_at_one_percent_and_charges_costs():
    d = day(*OPEN, ('10:00', 106, 112, 105, 111), ('15:45', 111, 121, 110, 120))
    trades, curve = orb.run({'MES': d}, {'target_r': None, 'trail': False})
    [t] = trades.to_dict('records')
    # risk incl. 1 tick slippage each way = 10.75 points * $5 = $53.75/contract -> $750 / 53.75 = 13
    assert t['qty'] == 13
    expected = (119.75 - 110.5) * 13 * 5 - 2 * 13 * orb.cfg.COMMISSION_PER_CONTRACT
    assert t['pnl'] == pytest.approx(expected)
    assert curve['equity'].iloc[-1] == pytest.approx(orb.START_EQUITY + expected)


def test_quarterly_months():
    assert orb.quarterly_months(dt.date(2024, 8, 1), dt.date(2025, 6, 30)) == ['202409', '202412', '202503', '202506']
