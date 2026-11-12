#!/usr/bin/env python3
"""Same-day MTF sell guard plus the protective-stop source. No orders and no live broker reads."""
import inspect, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import mtf_dual_entry as de

FAILS = []
CHECKS = [0]


def check(name, cond, detail=''):
    CHECKS[0] += 1
    if not cond:
        FAILS.append('%s: %s' % (name, detail))
        print('FAIL ', name, detail)


def test_no_sells():
    check('empty books -> no hits', de.mtf_sell_today('INDHOTEL', [], []) == [])
    hits = de.mtf_sell_today('INDHOTEL',
        [{'transaction_type': 'BUY', 'product': 'MTF', 'trading_symbol': 'INDHOTEL-EQ',
          'status': 'complete', 'filled_quantity': 77, 'order_id': 'u1'}],
        [{'transaction_type': 'BUY', 'product': 'MTF', 'tradingsymbol': 'INDHOTEL',
          'status': 'complete', 'filled_quantity': 100, 'order_id': 'k1'}])
    check('buys only -> no hits', hits == [], str(hits))


def test_kite_and_upstox():
    hits = de.mtf_sell_today('INDHOTEL', [],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'INDHOTEL',
          'status': 'complete', 'filled_quantity': 2, 'order_id': '2118576876506505216',
          'tag': 'mtf_trim'}])
    check('kite mtf_trim sell detected', len(hits) == 1 and hits[0]['broker'] == 'kite', str(hits))
    check('kite order id preserved', hits[0]['order_id'] == '2118576876506505216')
    hits = de.mtf_sell_today('TITAN',
        [{'transaction_type': 'SELL', 'product': 'MTF', 'trading_symbol': 'TITAN-EQ',
          'status': 'complete', 'filled_quantity': 1, 'order_id': 'u-sell', 'tag': 'mtf_trim'}], [])
    check('upstox mtf_trim sell detected', len(hits) == 1 and hits[0]['broker'] == 'upstox', str(hits))
    hits = de.mtf_sell_today('INDHOTEL-EQ', [],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'INDHOTEL',
          'status': 'complete', 'filled_quantity': 2, 'order_id': 'k2'}])
    check('-EQ suffix normalized', len(hits) == 1, str(hits))
    hits = de.mtf_sell_today('TITAN',
        [{'transaction_type': 'SELL', 'product': 'MTF', 'trading_symbol': 'TITAN-EQ',
          'status': 'complete', 'filled_quantity': 1, 'order_id': 'u1'}],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'TITAN',
          'status': 'complete', 'filled_quantity': 1, 'order_id': 'k1'}])
    check('both brokers reported', len(hits) == 2 and {h['broker'] for h in hits} == {'upstox', 'kite'}, str(hits))


def test_ignores_noise():
    hits = de.mtf_sell_today('INDHOTEL',
        [{'transaction_type': 'SELL', 'product': 'MTF', 'trading_symbol': 'INDHOTEL-EQ',
          'status': 'rejected', 'filled_quantity': 0, 'order_id': 'u-rej'},
         {'transaction_type': 'SELL', 'product': 'CNC', 'trading_symbol': 'INDHOTEL-EQ',
          'status': 'complete', 'filled_quantity': 10, 'order_id': 'u-cnc'}],
        [{'transaction_type': 'SELL', 'product': 'MTF', 'tradingsymbol': 'OTHER',
          'status': 'complete', 'filled_quantity': 5, 'order_id': 'k-other'}])
    check('rejected, delivery, and other symbols are ignored', hits == [], str(hits))


def test_stop_path_unchanged():
    src = inspect.getsource(de.sl_level)
    check('protective stop still reads the 1-hour series', 'minutes' in src and '60' in src, src)


if __name__ == '__main__':
    test_no_sells()
    test_kite_and_upstox()
    test_ignores_noise()
    test_stop_path_unchanged()
    print('%s checks, %s failed' % (CHECKS[0], len(FAILS)))
    sys.exit(1 if FAILS else 0)
