"""Tests for the first 5-minute candle strategy - no TWS needed."""

import datetime as dt

import pandas as pd
import pytest

import first_candle_backtest as fc
import turtle_config as cfg

MES = cfg.MARKETS_BY_KEY['MES']          # $5/point, 0.25 tick
SLIP = 0.25
COMM = cfg.COMMISSION_PER_CONTRACT


def day(*bars, date=dt.date(2026, 3, 2)):
    rows = []
    for t, o, h, l, c in bars:
        clock = dt.datetime.strptime(t, '%H:%M').time()
        rows.append({'datetime': dt.datetime.combine(date, clock), 'date': date, 'time': clock,
                     'open': o, 'high': h, 'low': l, 'close': c})
    return pd.DataFrame(rows)


UP = ('09:30', 7000, 7012, 6996, 7010)   # first candle up, low 6996


def test_up_candle_buys_with_stop_at_candle_low_and_holds_to_close():
    t = fc.simulate_day(day(UP, ('09:35', 7010, 7015, 7005, 7014), ('15:55', 7030, 7032, 7028, 7030)),
                        MES, 75_000)
    entry = 7010 + SLIP
    risk = entry - 6996                                  # 14.25 points
    qty = min(int(750 // ((risk + SLIP) * 5)), int(75_000 * 4 // (entry * 5)))
    assert (t['direction'], t['qty'], t['reason']) == ('long', qty, 'close')
    assert t['stop'] == 6996
    assert t['pnl'] == pytest.approx((7030 - SLIP - entry) * qty * 5 - 2 * qty * COMM)


def test_down_candle_shorts_and_gets_stopped():
    t = fc.simulate_day(day(('09:30', 7000, 7004, 6990, 6992), ('09:35', 6992, 7006, 6991, 7005)),
                        MES, 75_000)
    assert (t['direction'], t['reason'], t['exit']) == ('short', 'stop', 7004 + SLIP)
    # 1% risk would allow 12 contracts, but the 4x leverage cap allows 8 -> loses less than 1%
    assert t['qty'] == 8
    assert t['r'] == pytest.approx(-((7004 + SLIP) - (6992 - SLIP)) * 8 * 5 / 750 - 2 * 8 * COMM / 750)


def test_unchanged_first_candle_means_no_trade():
    assert fc.simulate_day(day(('09:30', 7000, 7005, 6995, 7000), ('09:35', 7000, 7010, 6990, 7005)),
                           MES, 75_000) is None


def test_leverage_cap_limits_size_on_a_tiny_first_candle():
    t = fc.simulate_day(day(('09:30', 7000, 7001, 6999.5, 7001), ('09:35', 7001, 7003, 7000, 7002),
                            ('15:55', 7002, 7003, 7001, 7002)), MES, 75_000)
    assert t['qty'] == int(75_000 * 4 // ((7001 + SLIP) * 5))   # 8, not the ~43 that 1% risk allows


def test_stop_wins_a_tie_with_the_target():
    t = fc.simulate_day(day(UP, ('09:35', 7010, 7200, 6990, 7100)), MES, 75_000, target_r=2.0)
    assert t['reason'] == 'stop'


def test_adding_at_plus_1r_moves_stop_to_entry():
    # risk 14.25 -> add at 7024.5; later drop back to entry -> first unit ~flat, added unit loses ~1R
    t = fc.simulate_day(day(UP, ('09:35', 7010, 7026, 7009, 7025), ('09:40', 7025, 7026, 7000, 7001)),
                        MES, 75_000, add_at_r=1.0)
    assert t['units'] == 2 and t['reason'] == 'stop'
    assert t['exit'] == pytest.approx(7010 + SLIP - SLIP)   # stopped at the first entry, less slippage


def test_run_compounds_equity():
    frames = {'MES': day(UP, ('09:35', 7010, 7015, 7005, 7014), ('15:55', 7030, 7032, 7028, 7030))}
    trades, curve = fc.run(frames, {})
    assert curve['equity'].iloc[-1] == pytest.approx(fc.START_EQUITY + trades['pnl'].sum())
