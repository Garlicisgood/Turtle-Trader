"""Tests for the Yahoo download helpers - no internet needed."""

import numpy as np
import pandas as pd

import turtle_download_yahoo as yd


def frame(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({'date': pd.bdate_range('2020-01-01', periods=len(closes)),
                         'open': closes, 'high': closes + 1, 'low': closes - 1, 'close': closes})


def test_clean_drops_empty_bars_and_repairs_high_low():
    df = frame([100, 101, 102, 103])
    df.loc[1, 'close'] = np.nan
    df.loc[2, 'high'] = 101.0          # below the close of 102
    out, notes = yd.clean(df)
    assert len(out) == 3
    assert out.loc[out['date'] == df['date'][2], 'high'].item() == 102
    assert len(notes) == 2


def test_big_gaps_finds_a_roll_jump():
    closes = list(np.linspace(100, 110, 60))
    df = frame(closes + [c + 20 for c in np.linspace(110, 112, 10)])   # sudden +20 jump
    gaps = yd.big_gaps(df)
    assert len(gaps) == 1 and gaps['gap_n'].iloc[0] > 3


def test_unit_check_flags_cents_vs_dollars(tmp_path, monkeypatch):
    monkeypatch.setattr(yd, 'IBKR_DIR', str(tmp_path))
    ib = frame([450] * 30)
    ib.to_csv(tmp_path / 'MZC.csv', index=False)
    assert 'match' in yd.unit_check('MZC', frame([450] * 30))
    assert 'DIFFER' in yd.unit_check('MZC', frame([4.5] * 30))
    assert yd.unit_check('MES', frame([450] * 30)) is None      # no IBKR file to compare
