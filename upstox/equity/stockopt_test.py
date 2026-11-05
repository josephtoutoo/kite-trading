#!/usr/bin/env python3
"""Offline unit smoke tests; imports broker modules but makes no network calls."""
import datetime as dt, importlib.util, json, os, sys, tempfile
D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
import stockopt_tech_entry as tech
import stockopt_results_exit as exits

def a(side='CE', adx=30):
    return dict(close=110 if side == 'CE' else 90, ema20=100, dir=1 if side == 'CE' else -1,
                adx=adx, prev_adx=adx-1, hist=1 if side == 'CE' else -1, pdi=30 if side == 'CE' else 10,
                mdi=10 if side == 'CE' else 30)
assert tech.signal(a())[0] == 'CE'
assert tech.signal(a('PE'))[0] == 'PE'
assert tech.signal(a(adx=24))[0] is None
weak = a(); weak['hist'] = -1
assert tech.signal(weak)[0] is None
row = {'expiry_date': dt.date(2026, 11, 26), 'instrument_type': 'CE', 'strike_price': 180}
assert tech.pick([row, {**row, 'strike_price': 190}], 'CE', 179, dt.date(2026, 11, 5))['strike_price'] == 180
assert exits.sources({'NSE_FO|1': {'sym': 'TATASTEEL26NOV185CE', 'source': 'results_entry'}}, {}, [], []) == {'TATASTEEL26NOV185CE'}
assert exits.sources({}, {}, [{'tag': 'results_entry', 'transaction_type': 'BUY', 'status': 'complete', 'trading_symbol': 'TATASTEEL26NOV185CE'}], []) == {'TATASTEEL26NOV185CE'}
import market_calendar
original = market_calendar.is_trading_day
market_calendar.is_trading_day = lambda d: d.weekday() < 5
try: assert exits.previous_trading_day(dt.date(2026, 11, 7)) == dt.date(2026, 11, 6)
finally: market_calendar.is_trading_day = original
with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
    json.dump({'TATASTEEL': {'expected_results_date': '2026-11-07'}}, f); path = f.name
try:
    original = market_calendar.is_trading_day
    market_calendar.is_trading_day = lambda d: d.weekday() < 5
    try: assert exits.calendar(path)['TATASTEEL']['exit_date'] == dt.date(2026, 11, 6)
    finally: market_calendar.is_trading_day = original
finally: os.unlink(path)
print('stockopt smoke tests passed (no orders, no network)')
