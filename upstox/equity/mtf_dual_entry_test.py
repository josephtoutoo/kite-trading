#!/usr/bin/env python3
"""Unit tests for mtf_dual_entry same-day MTF sell guard (6 Nov 2026 post-market).

Run: python3 mtf_dual_entry_test.py
No orders placed; no live broker reads — feeds forged order lists into the pure helper.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, '/workspace/upstox')
import mtf_dual_entry as de

FAILS = []
CHECKS = [0]

def check(name, cond, detail=''):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(f'{name}: {detail}')
        print(f'FAIL  {name}  {detail}')

def test_no_sells():
    hits = de.mtf_sell_today('INDHOTEL', [], [])
    check('empty books -> no hits', hits == [], str(hits))
    hits = de.mtf_sell_today('INDHOTEL',
        [{'transaction_type': 'BUY', 'product': 'MTF', 'trading_symbol': 'INDHOTEL-EQ',
          'status': 'complete', 'filled_quantity': 77, 'order_id': 'u1'}],
        [{'transaction_type': 'BUY', 'product': 'MTF', 'tradingsymbol': 'INDHOTEL',
          'status': 'complete', 'filled_quantity': 100, 'order_id': 'k1'}])
    check('buys only -> no hits', hits == [], str(hits))

def test_kite_sell_blocks():
    hits = de.mtf_sell_today('INDHOTEL', [],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'INDHOTEL',
          'status': 'complete', 'filled_quantity': 2, 'order_id': '2118576876506505216',
          'tag': 'mtf_trim'}])
    check('kite mtf_trim sell detected', len(hits) == 1 and hits[0]['broker'] == 'kite', str(hits))
    check('kite order id preserved', hits[0]['order_id'] == '2118576876506505216', str(hits))

def test_upstox_sell_blocks():
    hits = de.mtf_sell_today('TITAN',
        [{'transaction_type': 'SELL', 'product': 'MTF', 'trading_symbol': 'TITAN-EQ',
          'status': 'complete', 'filled_quantity': 1, 'order_id': 'u-sell', 'tag': 'mtf_trim'}],
        [])
    check('upstox mtf_trim sell detected', len(hits) == 1 and hits[0]['broker'] == 'upstox', str(hits))

def test_symbol_normalization():
    hits = de.mtf_sell_today('INDHOTEL-EQ', [],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'INDHOTEL',
          'status': 'complete', 'filled_quantity': 2, 'order_id': 'k2'}])
    check('-EQ suffix normalized', len(hits) == 1, str(hits))

def test_ignores_rejected_and_other_product():
    hits = de.mtf_sell_today('INDHOTEL',
        [{'transaction_type': 'SELL', 'product': 'MTF', 'trading_symbol': 'INDHOTEL-EQ',
          'status': 'rejected', 'filled_quantity': 0, 'order_id': 'u-rej'},
         {'transaction_type': 'SELL', 'product': 'CNC', 'trading_symbol': 'INDHOTEL-EQ',
          'status': 'complete', 'filled_quantity': 10, 'order_id': 'u-cnc'}],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'OTHER',
          'status': 'complete', 'filled_quantity': 5, 'order_id': 'k-other'}])
    check('rejected/CNC/other-symbol ignored', hits == [], str(hits))

def test_both_brokers():
    hits = de.mtf_sell_today('TITAN',
        [{'transaction_type': 'SELL', 'product': 'MTF', 'trading_symbol': 'TITAN-EQ',
          'status': 'complete', 'filled_quantity': 1, 'order_id': 'u1'}],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'TITAN',
          'status': 'complete', 'filled_quantity': 1, 'order_id': 'k1'}])
    check('both brokers reported', len(hits) == 2, str(hits))
    check('brokers set', {h['broker'] for h in hits} == {'upstox', 'kite'}, str(hits))

if __name__ == '__main__':
    test_no_sells(); test_kite_sell_blocks(); test_upstox_sell_blocks()
    test_symbol_normalization(); test_ignores_rejected_and_other_product(); test_both_brokers()
    print(f'{CHECKS[0] - len(FAILS)}/{CHECKS[0]} passed')
    if FAILS:
        print('FAILURES:')
        for f in FAILS: print(' ', f)
        sys.exit(1)
    print('ALL OK')
