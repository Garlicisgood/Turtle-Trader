"""
Test connection to Interactive Brokers via TWS API (paper trading).

What this does:
1. Connects to TWS running on your machine
2. Pulls your account summary (confirms paper trading balance is visible)
3. Requests a small chunk of historical data for one contract (MES)
   to confirm your CME market data subscription is actually working

Run this AFTER:
- TWS is open and logged into your PAPER TRADING account
- API is enabled in Global Configuration > API > Settings
- Read-Only API is UNCHECKED
- Socket port noted (default 7497 for paper trading)

Install the library first:
    pip install ib_async
"""

from ib_async import IB, ContFuture
import sys

def main():
    ib = IB()

    # --- Step 1: Connect ---
    # host: localhost since TWS is running on this same machine
    # port: 7497 is the default PAPER TRADING port in TWS
    # clientId: any number not already in use by another script/session
    try:
        ib.connect(host='127.0.0.1', port=7497, clientId=1, timeout=10)
        print("Connected to TWS successfully.\n")
    except Exception as e:
        print(f"FAILED to connect: {e}")
        print("\nCheck: Is TWS open? Are you logged into paper trading?")
        print("Is 'Enable ActiveX and Socket Clients' checked in API settings?")
        sys.exit(1)

    # --- Step 2: Pull account summary ---
    print("Account Summary:")
    account_values = ib.accountSummary()
    for item in account_values:
        if item.tag in ('NetLiquidation', 'TotalCashValue', 'BuyingPower'):
            print(f"  {item.tag}: {item.value} {item.currency}")

    # --- Step 3: Confirm market data works (MES = Micro E-mini S&P 500) ---
    print("\nRequesting sample market data for MES (Micro E-mini S&P 500)...")
    contract = ContFuture(symbol='MES', exchange='CME', currency='USD')

    # Qualify the contract (fills in exact expiry, conId, etc. from IBKR)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print("  Could not qualify MES contract. Check your futures data subscription.")
    else:
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime='',
            durationStr='5 D',
            barSizeSetting='1 day',
            whatToShow='TRADES',
            useRTH=True
        )
        if bars:
            print(f"  Got {len(bars)} daily bars. Most recent:")
            last = bars[-1]
            print(f"    Date: {last.date}, Close: {last.close}")
        else:
            print("  No bars returned. Market data subscription may not be active yet.")

    # --- Cleanup ---
    ib.disconnect()
    print("\nDone. If you saw account values and at least one bar above, everything is working.")

if __name__ == '__main__':
    main()
