"""
Tests the live order flow in turtle_trader.py against a simulated IBKR,
so entries, pyramid adds, stop orders, exits and reconciliation can be checked
without TWS. Market orders fill instantly at a price you set.
"""

import types

import pytest
from ib_async import Contract, OrderStatus, Trade

import turtle_config as cfg
import turtle_trader as tt
from turtle_core import Action, Snapshot, open_position
from turtle_state import empty_state


class FakeIB:
    def __init__(self):
        self.trades = []
        self.holdings = {}           # conId -> signed qty
        self.next_perm = 1000
        self.fill_price = 100.0
        self.fill_market_orders = True

    def placeOrder(self, contract, order):
        for t in self.trades:        # modifying an existing order
            if t.order is order:
                return t
        order.permId = self.next_perm
        self.next_perm += 1
        trade = Trade(contract=contract, order=order, orderStatus=OrderStatus(status='Submitted'))
        self.trades.append(trade)
        if order.orderType == 'MKT' and self.fill_market_orders:
            sign = 1 if order.action == 'BUY' else -1
            self.holdings[contract.conId] = self.holdings.get(contract.conId, 0) + sign * order.totalQuantity
            trade.orderStatus = OrderStatus(status='Filled', filled=order.totalQuantity,
                                            avgFillPrice=self.fill_price)
        return trade

    def cancelOrder(self, order):
        for t in self.trades:
            if t.order is order:
                t.orderStatus = OrderStatus(status='Cancelled', filled=t.orderStatus.filled,
                                            avgFillPrice=t.orderStatus.avgFillPrice)

    def openTrades(self):
        return [t for t in self.trades if not t.isDone()]

    def stops(self):
        return [t for t in self.openTrades() if t.order.orderType == 'STP']

    def positions(self):
        return [types.SimpleNamespace(contract=Contract(conId=c, symbol='MZC'), position=q)
                for c, q in self.holdings.items() if q]

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqExecutions(self):
        return []

    def sleep(self, _):
        pass


class FakeMD:
    """Stands in for MarketData: front contract conId 7 (MZCZ6), tick 0.25, $5/pt."""
    def __init__(self, close=100.0, n=2.0):
        self.contract = Contract(conId=7, localSymbol='MZCZ6', lastTradeDateOrContractMonth='20261214',
                                 exchange='CBOT')
        self.min_tick = 0.25
        self.point_value = 5.0
        self.snapshot = Snapshot('MZC', '2026-09-23', close, n, 110, 90, 105, 95, 5.0)


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(tt.time, 'time', iter(range(0, 10**6, 1)).__next__)


def test_entry_places_market_order_then_protective_stop():
    ib, state = FakeIB(), empty_state()
    ib.fill_price = 100.1
    assert tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(n=2.0), dry_run=False)

    pos = state['positions']['MZC']
    assert pos.qty == 3 and pos.con_id == 7
    assert pos.stop == 96.0                         # 100.1 - 4.0 = 96.1 -> nearest 0.25 tick
    [stop] = ib.stops()
    assert (stop.order.action, stop.order.totalQuantity, stop.order.auxPrice) == ('SELL', 3, 96.0)
    assert stop.order.tif == 'GTC' and stop.order.outsideRth
    assert pos.stop_perm_id == stop.order.permId


def test_add_moves_the_one_shared_stop():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(n=2.0), dry_run=False)
    ib.fill_price = 101.0
    tt.execute(ib, state, Action('add', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)

    pos = state['positions']['MZC']
    assert pos.qty == 6 and pos.unit_count == 2
    [stop] = ib.stops()                             # still exactly one stop order
    assert (stop.order.totalQuantity, stop.order.auxPrice) == (6, 97.0)


def test_exit_cancels_stop_before_selling():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)
    ib.fill_price = 94.0
    tt.execute(ib, state, Action('exit', 'MZC', -1, 3, 'test'), FakeMD(), dry_run=False)

    assert state['positions'] == {}
    assert ib.stops() == []
    assert ib.holdings[7] == 0
    assert state['closed_trades'][0]['pnl'] == pytest.approx((94.0 - 100.0) * 3 * 5.0)


def test_unfilled_exit_keeps_position_protected():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)
    ib.fill_market_orders = False                   # e.g. market closed
    ok = tt.execute(ib, state, Action('exit', 'MZC', -1, 3, 'test'), FakeMD(), dry_run=False)

    assert ok is False                              # tells main() to retry these bars next run
    assert 'MZC' in state['positions']
    [stop] = ib.stops()
    assert stop.order.totalQuantity == 3


def test_dry_run_places_nothing():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=True)
    assert ib.trades == [] and state['positions'] == {}


def test_no_pyramiding_into_an_old_contract():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)
    state['positions']['MZC'].con_id = 6            # front month has since rolled
    ib.holdings = {6: 3}
    tt.execute(ib, state, Action('add', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)
    assert state['positions']['MZC'].qty == 3


def test_reconcile_detects_stop_out():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)
    ib.holdings[7] = 0                              # stop filled overnight
    ib.stops()[0].orderStatus = OrderStatus(status='Filled', filled=3, avgFillPrice=96.0)

    skip = tt.reconcile(ib, state, dry_run=False)
    assert skip == set()
    assert state['positions'] == {}
    assert state['closed_trades'][0]['reason'].startswith('stop')


def test_reconcile_refuses_mismatch_and_unknown_positions():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)
    ib.holdings[7] = 5                              # someone traded by hand
    assert tt.reconcile(ib, state, dry_run=False) == {'MZC'}

    ib2, state2 = FakeIB(), empty_state()
    ib2.holdings = {99: 2}                          # MZC held but not opened by the trader
    assert tt.reconcile(ib2, state2, dry_run=False) == {'MZC'}


def test_reconcile_replaces_missing_stop():
    ib, state = FakeIB(), empty_state()
    tt.execute(ib, state, Action('entry', 'MZC', 1, 3, 'test'), FakeMD(), dry_run=False)
    ib.cancelOrder(ib.stops()[0].order)             # stop deleted in TWS by accident
    tt.reconcile(ib, state, dry_run=False)
    [stop] = ib.stops()
    assert stop.order.totalQuantity == 3
    assert state['positions']['MZC'].stop_perm_id == stop.order.permId


def test_micro_silver_not_resolved_to_full_size(monkeypatch):
    """IBKR's continuous SI contract is full-size silver even when asked for tradingClass SIL."""
    import datetime as dt
    from ib_async import ContractDetails

    full = ContractDetails(contract=Contract(conId=1, symbol='SI', tradingClass='SI', multiplier='5000'),
                           minTick=0.005, priceMagnifier=1)
    micro = ContractDetails(contract=Contract(conId=2, symbol='SI', tradingClass='SIL', multiplier='1000',
                                              lastTradeDateOrContractMonth='20261229'),
                            minTick=0.005, priceMagnifier=1)
    day0 = dt.date(2025, 1, 1)
    bar_list = [types.SimpleNamespace(date=day0 + dt.timedelta(days=i), open=30, high=31, low=29, close=30)
                for i in range(200)]

    class StubIB:
        def qualifyContracts(self, c):
            return [Contract(conId=1, symbol='SI', tradingClass='SI')]
        def reqContractDetails(self, c):
            return [full] if c.conId == 1 else [micro]
        def reqHistoricalData(self, c, **kw):
            self.history_con_id = c.conId
            return bar_list

    ib = StubIB()
    md = tt.load_market(ib, cfg.MARKETS_BY_KEY['SIL'])
    assert md.contract.conId == 2 and md.point_value == 1000.0
    assert ib.history_con_id == 2
    assert tt.check_point_value(md)


def test_paper_sizing_equity_tracks_pnl(monkeypatch):
    monkeypatch.setattr(cfg, 'PAPER_STARTING_EQUITY', 25000)
    state = empty_state()
    assert tt.sizing_equity(state, 1_000_000, is_paper=True) == 25000       # first run sets baseline
    assert tt.sizing_equity(state, 1_001_500, is_paper=True) == 26500       # +$1,500 P&L
    assert tt.sizing_equity(state, 1_001_500, is_paper=False) == 1_001_500  # never on a live account
    monkeypatch.setattr(cfg, 'PAPER_STARTING_EQUITY', None)
    assert tt.sizing_equity(state, 1_001_500, is_paper=True) == 1_001_500
