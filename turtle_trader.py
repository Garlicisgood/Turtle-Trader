"""
Turtle Trading System - Daily Trader

Run once a day, in the evening after the day session has closed (see README).
Each run:

  1. Connects to TWS and reads account equity, positions and open orders
  2. Pulls daily bars for all 14 markets and calculates N and the channels
  3. Reconciles its saved state (turtle_state.json) with what IBKR actually
     holds - e.g. notices a position that was stopped out overnight
  4. Exits positions on a 50-day breakout against them
  5. Adds pyramid units at +0.5N (up to 4) and moves the shared stop
  6. Enters new positions on 100-day breakouts, sized at 1% risk per unit,
     with a 2N stop, subject to the portfolio heat limits
  7. Saves state after every fill

Every stop is a real GTC stop order sitting at IBKR, so positions stay
protected between runs even if this computer is off.

Usage (from the project folder, with TWS open and logged into PAPER trading):
    python turtle_trader.py --dry-run     # show what it WOULD do, place nothing
    python turtle_trader.py               # actually trade

Install requirements first:
    python -m pip install -r requirements.txt
"""

import argparse
import datetime as dt
import logging
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, ContFuture, Contract, MarketOrder, StopOrder

import turtle_config as cfg
from turtle_core import (Snapshot, add_indicators, plan_actions, open_position,
                         add_unit, round_to_tick, unit_size)
from turtle_state import load_state, save_state, record_closed_trade

log = logging.getLogger('turtle')
CHICAGO = ZoneInfo('America/Chicago')
DAY_SESSION_OVER_CT = dt.time(16, 0)   # every market's day session has closed by 4pm Chicago time


def setup_logging():
    os.makedirs(cfg.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', '%Y-%m-%d %H:%M:%S')
    file_handler = logging.FileHandler(os.path.join(cfg.LOG_DIR, f"turtle_{dt.date.today()}.log"))
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(message)s'))
    log.setLevel(logging.INFO)
    log.addHandler(file_handler)
    log.addHandler(console)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

class MarketData:
    """Everything the trader knows about one market for this run."""

    def __init__(self, market, details, history_contract, df):
        self.market = market
        self.details = details
        self.contract = details.contract            # the actual dated contract to trade
        self.history_contract = history_contract
        self.min_tick = details.minTick
        self.point_value = float(details.contract.multiplier) / (details.priceMagnifier or 1)
        self.df = df
        self.snapshot = Snapshot.from_row(market.key, df.iloc[-1], self.point_value)


def get_front_month_details(ib, m):
    """
    Fallback for markets that don't have a continuous contract (ContFuture)
    defined in IBKR's system - usually newer or lower-volume products.
    Returns the contract details of the nearest (front month) dated contract.
    """
    kwargs = {'symbol': m.symbol, 'secType': 'FUT', 'exchange': m.exchange, 'currency': 'USD'}
    if m.trading_class:
        kwargs['tradingClass'] = m.trading_class
    try:
        details = ib.reqContractDetails(Contract(**kwargs))
    except Exception:
        return None
    if not details:
        return None
    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    return details[0]


def get_bars(ib, contract):
    bars = ib.reqHistoricalData(contract, endDateTime='', durationStr=f'{cfg.HISTORY_DAYS} D',
                                barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
    if not bars:
        return None
    return pd.DataFrame([{'date': b.date, 'open': b.open, 'high': b.high,
                          'low': b.low, 'close': b.close} for b in bars])


def load_market(ib, m):
    """Resolves the tradeable contract and pulls history. Returns MarketData or None."""
    kwargs = {'symbol': m.symbol, 'exchange': m.exchange, 'currency': 'USD'}
    if m.trading_class:
        kwargs['tradingClass'] = m.trading_class
    try:
        qualified = [c for c in ib.qualifyContracts(ContFuture(**kwargs)) if c is not None]
    except Exception:
        qualified = []

    if qualified:
        # A qualified ContFuture carries the conId of the current front contract -
        # look that up to get the real, orderable contract and its details.
        history_contract = qualified[0]
        found = ib.reqContractDetails(Contract(conId=history_contract.conId))
        details = found[0] if found else None
        # IBKR's continuous contract ignores tradingClass: asking for Micro Silver
        # (SI / SIL) returns FULL-SIZE silver (5000 oz). Use the dated contract instead.
        if details and m.trading_class and details.contract.tradingClass != m.trading_class:
            details = None
    if details is None:
        details = get_front_month_details(ib, m)
        history_contract = details.contract if details else None
    if details is None:
        log.warning(f"  {m.key}: could not qualify contract on {m.exchange}, skipping.")
        return None

    try:
        df = get_bars(ib, history_contract)
    except Exception as e:
        log.warning(f"  {m.key}: error pulling historical data ({e}), skipping.")
        return None
    if df is None or len(df) < cfg.ENTRY_LOOKBACK + cfg.N_LOOKBACK:
        log.warning(f"  {m.key}: not enough history ({0 if df is None else len(df)} bars), skipping.")
        return None

    # Never trade off a bar that is still forming.
    now_ct = dt.datetime.now(CHICAGO)
    if df['date'].iloc[-1] == now_ct.date() and now_ct.time() < DAY_SESSION_OVER_CT:
        log.info(f"  {m.key}: today's bar is still forming - using yesterday's close.")
        df = df.iloc[:-1]

    df = add_indicators(df).reset_index(drop=True)
    return MarketData(m, details, history_contract, df)


def check_point_value(md):
    """IBKR's multiplier must agree with the table in turtle_config - else sizing is wrong."""
    expected = md.market.point_value
    if abs(md.point_value - expected) > 0.01 * expected:
        log.error(f"  {md.market.key}: IBKR says ${md.point_value:g}/point but turtle_config expects "
                  f"${expected:g}/point. Not trading this market until the config is checked.")
        return False
    return True


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def wait_for(ib, condition, timeout):
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        ib.sleep(0.5)
    return condition()


def market_order(ib, contract, action, qty):
    """Sends a market order and waits for it. Returns (filled_qty, avg_price)."""
    order = MarketOrder(action, qty, tif='DAY', outsideRth=True, orderRef=cfg.ORDER_REF)
    trade = ib.placeOrder(contract, order)
    log.info(f"    -> {action} {qty} {contract.localSymbol} at market")
    if not wait_for(ib, trade.isDone, cfg.FILL_TIMEOUT_SECONDS):
        log.warning(f"    order not complete after {cfg.FILL_TIMEOUT_SECONDS}s "
                    f"(status {trade.orderStatus.status}) - cancelling the rest.")
        ib.cancelOrder(order)
        wait_for(ib, trade.isDone, 10)
    filled = int(trade.orderStatus.filled)
    price = trade.orderStatus.avgFillPrice
    if filled:
        log.info(f"    filled {filled} @ {price}")
    else:
        log.warning(f"    NOT filled (status {trade.orderStatus.status}). Is the market open?")
    return filled, price


def find_stop_trade(ib, pos):
    if not pos.stop_perm_id:
        return None
    for trade in ib.openTrades():
        if trade.order.permId == pos.stop_perm_id and not trade.isDone():
            return trade
    return None


def place_or_update_stop(ib, contract, pos):
    """Makes the single shared GTC stop order at IBKR match the position's qty and stop."""
    trade = find_stop_trade(ib, pos)
    if trade:
        trade.order.totalQuantity = pos.qty
        trade.order.auxPrice = pos.stop
        ib.placeOrder(contract, trade.order)
        log.info(f"    stop order updated: {pos.qty} contracts @ {pos.stop}")
        return
    action = 'SELL' if pos.direction > 0 else 'BUY'
    # outsideRth: without it IBKR only triggers the stop during the day session
    order = StopOrder(action, pos.qty, pos.stop, tif='GTC', outsideRth=True,
                      orderRef=f"{cfg.ORDER_REF}-stop-{pos.key}")
    trade = ib.placeOrder(contract, order)
    wait_for(ib, lambda: trade.order.permId != 0, 10)
    pos.stop_perm_id = trade.order.permId
    log.info(f"    stop order placed: {action} {pos.qty} @ {pos.stop} (permId {pos.stop_perm_id})")


def cancel_stop(ib, pos):
    trade = find_stop_trade(ib, pos)
    if trade is None:
        return True
    ib.cancelOrder(trade.order)
    return wait_for(ib, trade.isDone, 10)


def held_contract(ib, pos):
    """The dated contract a position is actually in (may differ from today's front month)."""
    contract = Contract(conId=pos.con_id, exchange=cfg.MARKETS_BY_KEY[pos.key].exchange)
    ib.qualifyContracts(contract)
    return contract


# ---------------------------------------------------------------------------
# Reconciliation with the broker
# ---------------------------------------------------------------------------

def reconcile(ib, state, dry_run):
    """
    Compares saved positions with what IBKR actually holds. Returns the set of
    market keys that must be left alone this run because the two disagree.
    """
    skip = set()
    broker = {}
    for p in ib.positions():
        broker[p.contract.conId] = broker.get(p.contract.conId, 0) + p.position
    today = str(dt.date.today())

    for key, pos in list(state['positions'].items()):
        held = broker.pop(pos.con_id, 0)
        m = cfg.MARKETS_BY_KEY[key]
        if held == 0:
            price = stop_fill_price(ib, pos)
            log.warning(f"  {key}: position is gone at IBKR - assuming the stop filled "
                        f"({'@ ' + str(price) if price else 'fill price unknown'}).")
            if not dry_run:
                cancel_stop(ib, pos)
            record_closed_trade(state, pos, price, today, 'stop (found flat at IBKR)', m.point_value)
        elif held != pos.direction * pos.qty:
            log.error(f"  {key}: IBKR holds {held:+g} but state says {pos.direction * pos.qty:+d}. "
                      f"Leaving this market alone - fix by hand, then edit {cfg.STATE_FILE}.")
            skip.add(key)
        else:
            if find_stop_trade(ib, pos) is None:
                log.warning(f"  {key}: no live stop order found at IBKR - re-placing it.")
                if not dry_run:
                    place_or_update_stop(ib, held_contract(ib, pos), pos)
            if pos.expiry:
                days = (dt.datetime.strptime(pos.expiry[:8], '%Y%m%d').date() - dt.date.today()).days
                if days <= cfg.ROLL_WARNING_DAYS:
                    log.warning(f"  {key}: held contract {pos.local_symbol} expires in {days} days - ROLL NEEDED.")

    # Anything IBKR holds in our markets that the trader didn't open.
    for p in ib.positions():
        if p.contract.conId not in broker or p.position == 0:
            continue
        for m in cfg.MARKETS:
            if p.contract.symbol == m.symbol and (not m.trading_class or p.contract.tradingClass == m.trading_class):
                log.error(f"  {m.key}: IBKR holds {p.position:+g} {p.contract.localSymbol} that the trader "
                          f"didn't open. Leaving this market alone.")
                skip.add(m.key)
    return skip


def stop_fill_price(ib, pos):
    """Looks for the stop order's execution in IBKR's recent fills (last ~24h)."""
    try:
        for fill in ib.reqExecutions():
            if pos.stop_perm_id and fill.execution.permId == pos.stop_perm_id:
                return fill.execution.avgPrice
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def execute(ib, state, action, md, dry_run):
    """Carries out one planned action. Returns False if an order didn't go through."""
    key = action.key
    side = 'BUY' if action.direction > 0 else 'SELL'
    today = md.snapshot.date
    log.info(f"  {action.kind.upper()} {key}: {side} {action.qty} ({action.reason})")
    if dry_run:
        return True

    if action.kind == 'exit':
        pos = state['positions'][key]
        contract = held_contract(ib, pos)
        if not cancel_stop(ib, pos):
            log.error(f"    could not cancel the stop order - not sending the exit (avoids a double fill).")
            return False
        filled, price = market_order(ib, contract, side, pos.qty)
        if filled == pos.qty:
            record_closed_trade(state, pos, price, today, action.reason, md.point_value)
        else:
            if filled:
                log.error(f"    PARTIAL exit ({filled}/{pos.qty}) - check TWS; state keeps the full position.")
            place_or_update_stop(ib, contract, pos)   # stay protected
            save_state(state, cfg.STATE_FILE)
            return False

    elif action.kind == 'add':
        pos = state['positions'][key]
        if pos.con_id != md.contract.conId:
            log.warning(f"    held contract {pos.local_symbol} is no longer the front month - "
                        f"not pyramiding into it. ROLL NEEDED.")
            return True
        filled, price = market_order(ib, md.contract, side, action.qty)
        if not filled:
            return False
        add_unit(pos, filled, price, today, md.min_tick)
        place_or_update_stop(ib, md.contract, pos)

    elif action.kind == 'entry':
        filled, price = market_order(ib, md.contract, side, action.qty)
        if not filled:
            return False
        pos = open_position(key, action.direction, filled, price, md.snapshot.n, today, md.min_tick)
        pos.con_id = md.contract.conId
        pos.local_symbol = md.contract.localSymbol
        pos.expiry = md.contract.lastTradeDateOrContractMonth
        state['positions'][key] = pos
        place_or_update_stop(ib, md.contract, pos)

    save_state(state, cfg.STATE_FILE)
    return True


def print_summary(state, data, equity):
    log.info("")
    log.info("=" * 78)
    log.info(f"OPEN POSITIONS   (equity ${equity:,.0f})")
    log.info("=" * 78)
    if not state['positions']:
        log.info("  none")
    for key, pos in state['positions'].items():
        md = data.get(key)
        close = md.snapshot.close if md else None
        avg = sum(u.price * u.qty for u in pos.units) / pos.qty
        pnl = (close - avg) * pos.direction * pos.qty * md.point_value if md else float('nan')
        log.info(f"  {key:5} {'LONG ' if pos.direction > 0 else 'SHORT'} {pos.unit_count} unit(s), "
                 f"{pos.qty} contracts  avg {avg:g}  stop {pos.stop:g}  last {close}  open P&L ${pnl:,.0f}")


def print_sizing_table(data, equity):
    log.info("")
    log.info(f"Unit size per market at ${equity:,.0f} equity "
             f"({cfg.RISK_PER_UNIT:.0%} risk, {cfg.SIZING_N:g}N):")
    for key, md in data.items():
        s = md.snapshot
        risk = cfg.SIZING_N * s.n * s.point_value
        log.info(f"  {key:5} close {s.close:>10g}  N {s.n:>9.4f}  ${s.point_value:g}/pt  "
                 f"risk/contract ${risk:>7,.0f}  -> {unit_size(equity, s.n, s.point_value)} contract(s)")


def get_equity(ib):
    for item in ib.accountSummary():
        if item.tag == 'NetLiquidation':
            return float(item.value)
    raise RuntimeError("NetLiquidation not found in account summary")


def sizing_equity(state, net_liq, is_paper):
    """
    Equity used for position sizing. On paper with PAPER_STARTING_EQUITY set, it's
    that amount plus the account's P&L since the first run (the first run's
    balance is remembered in the state file).
    """
    if not is_paper or cfg.PAPER_STARTING_EQUITY is None:
        return net_liq
    baseline = state.setdefault('paper_baseline_net_liq', net_liq)
    return cfg.PAPER_STARTING_EQUITY + (net_liq - baseline)


def main():
    parser = argparse.ArgumentParser(description="Daily Turtle trading run")
    parser.add_argument('--dry-run', action='store_true', help="show decisions, place no orders, save nothing")
    parser.add_argument('--force', action='store_true', help="run even if there are no new bars since last run")
    parser.add_argument('--live-account', action='store_true', help="required to trade a non-paper account")
    args = parser.parse_args()

    setup_logging()
    log.info(f"Turtle trader {'DRY RUN' if args.dry_run else 'LIVE ORDERS'} - {dt.datetime.now():%Y-%m-%d %H:%M}")

    ib = IB()
    try:
        ib.connect(host=cfg.IB_HOST, port=cfg.IB_PORT, clientId=cfg.IB_CLIENT_ID, timeout=10)
    except Exception as e:
        log.error(f"FAILED to connect: {e}")
        sys.exit(1)

    try:
        accounts = ib.managedAccounts()
        is_paper = all(a.startswith('D') for a in accounts)
        if not args.live_account and not is_paper:
            log.error(f"Account {accounts} does not look like a paper account (paper IDs start with 'D'). "
                      f"Pass --live-account if you really mean to trade it.")
            sys.exit(1)

        state = load_state(cfg.STATE_FILE)
        net_liq = get_equity(ib)
        equity = sizing_equity(state, net_liq, is_paper)
        if equity != net_liq:
            log.info(f"Paper account balance ${net_liq:,.0f} - sizing as a ${equity:,.0f} account "
                     f"(PAPER_STARTING_EQUITY in turtle_config.py)")
        ib.reqAllOpenOrders()

        log.info("\nLoading market data...")
        data = {}
        for m in cfg.MARKETS:
            try:
                md = load_market(ib, m)
            except Exception as e:
                log.warning(f"  {m.key}: unexpected error ({e}), skipping.")
                md = None
            if md and check_point_value(md):
                data[m.key] = md

        latest_bar = max((md.snapshot.date for md in data.values()), default=None)
        if not args.force and latest_bar and state.get('last_bar_date') == latest_bar:
            log.info(f"\nAlready processed bars up to {latest_bar} - nothing new. (--force to run anyway)")
            print_summary(state, data, equity)
            return

        log.info("\nReconciling with IBKR...")
        skip = reconcile(ib, state, args.dry_run)

        snapshots = {k: md.snapshot for k, md in data.items()}
        actions, notes = plan_actions(state['positions'], snapshots, equity, skip)

        log.info("\nToday's decisions:")
        for note in notes:
            log.info(f"  {note}")
        if not actions:
            log.info("  no orders to place.")
        all_done = True
        for action in actions:
            try:
                all_done &= execute(ib, state, action, data[action.key], args.dry_run)
            except Exception as e:
                log.exception(f"    {action.key}: error while executing {action.kind}: {e}")
                all_done = False

        if args.dry_run:
            print_sizing_table(data, equity)
        else:
            if all_done:
                state['last_bar_date'] = latest_bar
            else:
                # e.g. run on a Friday night with markets shut - the next run retries these bars
                log.warning("\nSome orders didn't go through - the next run will retry today's signals.")
            state['last_run'] = dt.datetime.now().isoformat(timespec='seconds')
            save_state(state, cfg.STATE_FILE)
        print_summary(state, data, equity)
    finally:
        ib.disconnect()


if __name__ == '__main__':
    main()
