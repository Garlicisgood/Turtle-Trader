"""
Tests for the Turtle rules - no TWS connection needed.
Run from the project folder:  python -m pytest
"""

import math

import numpy as np
import pandas as pd
import pytest

import turtle_config as cfg
from turtle_backtest import Backtest
from turtle_core import (Position, Snapshot, Unit, add_indicators, add_unit, calculate_n,
                         entry_signal, exit_signal, open_position, plan_actions, pyramid_due,
                         round_to_tick, stop_hit, trim_flat_history, unit_size)
from turtle_state import load_state, save_state, record_closed_trade


def snap(key='MZC', close=100.0, n=2.0, entry=(90.0, 110.0), exit_=(95.0, 105.0), pv=5.0):
    return Snapshot(key=key, date='2026-01-02', close=close, n=n,
                    entry_low=entry[0], entry_high=entry[1],
                    exit_low=exit_[0], exit_high=exit_[1], point_value=pv)


def bars(closes, spread=1.0):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({'date': pd.bdate_range('2020-01-01', periods=len(closes)),
                         'open': closes, 'high': closes + spread,
                         'low': closes - spread, 'close': closes})


# --- Indicators -------------------------------------------------------------

def test_n_matches_wilder_formula():
    df = bars(np.linspace(100, 130, 60), spread=1.0)   # TR = 2 every day except day 0
    n = calculate_n(df)
    assert n.iloc[19] == pytest.approx(2.0, abs=0.05)
    assert n.iloc[30] == pytest.approx((19 * n.iloc[29] + 2.0) / 20)


def test_channels_exclude_todays_bar():
    df = add_indicators(bars(list(range(1, 131))))
    last = df.iloc[-1]
    # prior 100 highs are bars 29..128 -> highest high = 129 + 1 spread... i.e. yesterday's high
    assert last['entry_high'] == df['high'].iloc[-2]
    assert last['close'] > last['entry_high'] - 1   # sanity: today's bar isn't in its own channel


# --- Signals & sizing -------------------------------------------------------

def test_entry_and_exit_signals():
    assert entry_signal(snap(close=111)) == 1
    assert entry_signal(snap(close=89)) == -1
    assert entry_signal(snap(close=100)) == 0
    assert exit_signal(snap(close=94), direction=1)
    assert not exit_signal(snap(close=96), direction=1)
    assert exit_signal(snap(close=106), direction=-1)


def test_unit_size_rounds_down_to_one_percent_risk():
    # $25,000 * 1% = $250 allowed; 1 contract risks 2 * 3 * $5 = $30 -> 8 contracts
    assert unit_size(25_000, 3.0, 5.0) == 8
    # 1 contract of MES with N=60 risks 2 * 60 * $5 = $600 > $250 -> can't trade
    assert unit_size(25_000, 60.0, 5.0) == 0
    assert unit_size(25_000, float('nan'), 5.0) == 0
    assert unit_size(-5, 3.0, 5.0) == 0


def test_round_to_tick():
    assert round_to_tick(100.13, 0.25) == 100.25
    assert round_to_tick(0.12345, 0.0005) == pytest.approx(0.1235)


# --- Pyramiding & the shared stop -------------------------------------------

def test_long_pyramid_and_shared_stop():
    pos = open_position('MZC', 1, 2, 100.0, 4.0, 'd1')
    assert pos.stop == 92.0                     # 100 - 2N
    assert pos.next_add_price == 102.0          # +0.5N
    assert not pyramid_due(pos, 101.9)
    assert pyramid_due(pos, 102.0)
    add_unit(pos, 2, 102.5, 'd2')
    assert pos.stop == 94.5                     # whole position's stop moved to 102.5 - 2N
    assert pos.qty == 4 and pos.unit_count == 2
    add_unit(pos, 2, 104.5, 'd3')
    add_unit(pos, 2, 106.5, 'd4')
    assert pos.unit_count == 4
    assert not pyramid_due(pos, 200)            # capped at 4 units


def test_stop_never_loosens():
    pos = open_position('MZC', 1, 1, 100.0, 4.0, 'd1')
    add_unit(pos, 1, 95.0, 'd2')                # a (weird) add below the first fill
    assert pos.stop == 92.0


def test_short_side_mirrors_long():
    pos = open_position('MZC', -1, 1, 100.0, 4.0, 'd1')
    assert pos.stop == 108.0
    assert pos.next_add_price == 98.0
    assert pyramid_due(pos, 97.0)
    add_unit(pos, 1, 97.0, 'd2')
    assert pos.stop == 105.0
    assert stop_hit(pos, low=90, high=105.0)
    assert not stop_hit(pos, low=90, high=104.9)


# --- Daily decisions ---------------------------------------------------------

def test_plan_exit_then_entry_order():
    positions = {'MZC': open_position('MZC', 1, 3, 100.0, 2.0, 'd1')}
    snaps = {'MZC': snap('MZC', close=94),               # exit long
             'MZW': snap('MZW', close=111)}              # new long entry
    actions, notes = plan_actions(positions, snaps, 25_000)
    assert [(a.kind, a.key, a.direction, a.qty) for a in actions] == [
        ('exit', 'MZC', -1, 3), ('entry', 'MZW', 1, 12)]


def test_plan_skips_untradeable_size():
    actions, notes = plan_actions({}, {'MES': snap('MES', close=7000, n=60, entry=(6000, 6900))}, 25_000)
    assert actions == []
    assert 'risks $600' in notes[0]


def test_group_heat_limit_blocks_entry():
    # 6 units already in the soy group -> a new soy entry is refused, corn is fine
    pos = open_position('MZS', 1, 1, 100.0, 2.0, 'd1')
    for i in range(3):
        add_unit(pos, 1, 101.0 + i, 'd')
    pos2 = open_position('MZM', 1, 1, 100.0, 2.0, 'd1')
    add_unit(pos2, 1, 101.0, 'd')
    positions = {'MZS': pos, 'MZM': pos2}
    snaps = {'MZL': snap('MZL', close=111, pv=60.0), 'MZC': snap('MZC', close=111)}
    actions, notes = plan_actions(positions, snaps, 25_000)
    assert [a.key for a in actions] == ['MZC']
    assert "group 'soy'" in notes[0]


def test_skip_leaves_market_alone():
    positions = {'MZC': open_position('MZC', 1, 3, 100.0, 2.0, 'd1')}
    actions, _ = plan_actions(positions, {'MZC': snap('MZC', close=50)}, 25_000, skip={'MZC'})
    assert actions == []


# --- State file -------------------------------------------------------------

def test_state_round_trip(tmp_path):
    path = str(tmp_path / 'state.json')
    state = load_state(path)
    pos = open_position('MGC', 1, 1, 2000.0, 20.0, '2026-01-02', tick=0.1)
    pos.con_id, pos.stop_perm_id = 123, 456
    state['positions']['MGC'] = pos
    save_state(state, path)
    save_state(state, path)                       # second save leaves a .bak
    loaded = load_state(path)
    assert loaded['positions']['MGC'] == pos
    assert (tmp_path / 'state.json.bak').exists()

    record_closed_trade(loaded, loaded['positions']['MGC'], 2050.0, '2026-02-01', 'exit', 10.0)
    assert loaded['positions'] == {}
    assert loaded['closed_trades'][0]['pnl'] == 500.0


# --- Backtest ---------------------------------------------------------------

def test_backtest_catches_a_trend():
    rng = np.random.default_rng(0)
    flat = 100 + rng.normal(0, 0.5, 150)
    up = flat[-1] + 1.5 * np.arange(1, 121)      # +1.5/day, faster than the +/-1 bar range
    down = up[-1] - 2.0 * np.arange(1, 61)
    frames = {'MZC': add_indicators(bars(np.concatenate([flat, up, down]))).set_index('date')}
    bt = Backtest(frames, 25_000).run()
    trades = pd.DataFrame(bt.trades)
    assert len(trades) >= 1
    first = trades.iloc[0]
    assert first['direction'] == 'long'
    assert first['units'] == cfg.MAX_UNITS_PER_MARKET
    assert first['pnl'] > 0
    assert bt.cash > 25_000


def test_trim_flat_history_drops_bars_without_range():
    df = bars(np.linspace(100, 110, 200))
    for col in ('open', 'high', 'low'):
        df.loc[:79, col] = df.loc[:79, 'close']       # first 80 bars flat
        df.loc[150, col] = df.loc[150, 'close']       # one stray flat bar later is fine
    trimmed, bad_until = trim_flat_history(df)
    # cut just past the flat stretch (a 20-bar window must be <= 25% flat), keeping the stray bar
    assert df['date'][79] <= bad_until < df['date'][99]
    assert trimmed['date'].iloc[0] > df['date'][79]
    assert df['date'][150] in set(trimmed['date'])
    assert trim_flat_history(bars(np.linspace(100, 110, 50)))[1] is None
