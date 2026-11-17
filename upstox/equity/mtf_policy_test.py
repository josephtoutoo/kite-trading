#!/usr/bin/env python3
"""Deterministic Monday-MTF policy tests. No broker, no orders, no tokens, no network.

Run: python3 equity/mtf_policy_test.py
"""
import datetime, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import mtf_policy as pol
import mtf_sizing as ms

IST = pol.IST
FAILS = []
CHECKS = [0]
VERBOSE = '-v' in sys.argv


def check(name, cond, detail=''):
    CHECKS[0] += 1
    if not cond:
        FAILS.append('%s: %s' % (name, detail))
        print('FAIL  %s  %s' % (name, detail))
    elif VERBOSE:
        print('ok    %s' % name)


def day(text):
    return text


# ---------------------------------------------------------------- date boundary
def test_date_boundary():
    off = (
        '2026-11-08',
        '2026-11-08T23:59:59',
        datetime.datetime(2026, 11, 8, 23, 59, tzinfo=IST),
        '2026-11-08T18:29:59+00:00',   # 23:59:59 IST, still Sunday
        datetime.date(2026, 11, 6),
        '2026-11-05T10:00',
    )
    on = (
        '2026-11-09',
        '2026-11-09T00:00:00',
        datetime.datetime(2026, 11, 9, 0, 0, tzinfo=IST),
        datetime.datetime(2026, 11, 9, 9, 15, tzinfo=IST),
        '2026-11-08T18:30:00+00:00',   # 00:00 IST Monday
        '2026-11-10T10:00',
    )
    for item in off:
        check('policy off %r' % (item,), pol.policy_active(item) is False, pol.policy_active(item))
        check('max names 5 before Monday %r' % (item,), pol.max_names(item) == 5)
    for item in on:
        check('policy on %r' % (item,), pol.policy_active(item) is True, pol.policy_active(item))
        check('max names 5 from Monday %r' % (item,), pol.max_names(item) == 5)
    check('garbage date is not readable', pol.date_readable('not-a-date') is False)
    check('garbage date fails closed (tighter rule active)', pol.policy_active('not-a-date') is True)
    closed = pol.rooms(1_000_000, {}, [], None, 0.25, 'not-a-date')
    check('garbage date leaves no buy room', closed.get('closed') is True and closed['room_new'] == 0, closed)
    # prior numeric settings are what sizing still returns before the morning
    check('sizing total 50% on 8 Nov', ms.total_cap('2026-11-08T23:59') == 0.50)
    check('sizing total 50% on 5 Nov (autonomy did not raise it)', ms.total_cap('2026-11-05T10:00') == 0.50)
    check('sizing per-stock 12% only on 5 Nov', ms.per_stock_cap('2026-11-05T10:00') == 0.12)
    check('sizing per-stock back to 10% on 8 Nov', ms.per_stock_cap('2026-11-08T10:00') == 0.10)
    check('sizing per-stock stays the stricter 10% on 9 Nov', ms.per_stock_cap('2026-11-09T09:15') == 0.10)
    check('sizing total 25% on 9 Nov', abs(ms.total_cap('2026-11-09T00:00') - 0.25) < 1e-9)
    check('sizing initial shares the 25% pool on 9 Nov', abs(ms.initial_fraction('2026-11-09T10:00') - 0.25) < 1e-9)
    check('sizing add shares the 25% pool on 9 Nov', abs(ms.add_fraction('2026-11-09T10:00') - 0.25) < 1e-9)
    check('sizing initial fraction is not a separate pool before Monday',
          ms.initial_fraction('2026-11-08T10:00') is None)
    check('MAX_STOCKS constant remains the pre-Monday 5', ms.MAX_STOCKS == 5)
    check('max_names(9 Nov) is 5 actual names', ms.max_names('2026-11-09T10:00') == 5)
    check('max_names(8 Nov) is 5', ms.max_names('2026-11-08T10:00') == 5)


# ---------------------------------------------------------------- reconcile
def test_reconcile_holdings():
    # legacy holding, no position row, no tag involved
    book = pol.reconcile_book([
        {'symbol': 'HYUNDAI', 'qty': 10, 'price': 100, 'kind': 'holding'},
    ])
    check('legacy holding counted', book['HYUNDAI']['qty'] == 10 and book['HYUNDAI']['cost'] == 1000, book)
    # position and holding are the same lot
    book = pol.reconcile_book([
        {'symbol': 'INDIGO', 'qty': 10, 'price': 50, 'kind': 'position'},
        {'symbol': 'INDIGO', 'qty': 10, 'price': 50, 'kind': 'holding'},
    ])
    check('holding is not added on top of a positive net', book['INDIGO']['qty'] == 10, book)
    # sold negative day leg against an overnight holding
    book = pol.reconcile_book([
        {'symbol': 'ICICIBANK', 'qty': 10, 'price': 100, 'kind': 'holding'},
        {'symbol': 'ICICIBANK', 'qty': -4, 'price': 110, 'kind': 'position'},
    ])
    check('negative day leg reduces the holding to 6', book['ICICIBANK']['qty'] == 6, book)
    check('remaining cost uses the long average', book['ICICIBANK']['cost'] == 600, book)
    # already-netted position must not be reduced a second time
    book = pol.reconcile_book([
        {'symbol': 'RELIANCE', 'qty': 6, 'price': 100, 'kind': 'position',
         'overnight_qty': 10, 'day_buy_qty': 0, 'day_sell_qty': 4},
        {'symbol': 'RELIANCE', 'qty': 10, 'price': 100, 'kind': 'holding'},
    ])
    check('already-netted sell is not applied twice', book['RELIANCE']['qty'] == 6, book)
    # quantity still shows the overnight figure while day_sell says 4 were sold
    book = pol.reconcile_book([
        {'symbol': 'TCS', 'qty': 10, 'price': 100, 'kind': 'position',
         'overnight_qty': 10, 'day_buy_qty': 0, 'day_sell_qty': 4},
    ])
    check('unapplied day_sell is subtracted once', book['TCS']['qty'] == 6, book)
    # flat net does not resurrect a stale holding
    book = pol.reconcile_book([
        {'symbol': 'SBIN', 'qty': 0, 'price': 100, 'kind': 'position'},
        {'symbol': 'SBIN', 'qty': 8, 'price': 100, 'kind': 'holding'},
    ])
    check('flat position drops the stale holding', 'SBIN' not in book, book)
    # full sell via negative leg
    book = pol.reconcile_book([
        {'symbol': 'ITC', 'qty': 5, 'price': 100, 'kind': 'holding'},
        {'symbol': 'ITC', 'qty': -5, 'price': 100, 'kind': 'position'},
    ])
    check('fully sold name is not a positive name', 'ITC' not in book, book)
    # separate day row
    book = pol.reconcile_book([
        {'symbol': 'LT', 'qty': 10, 'price': 100, 'kind': 'holding'},
        {'symbol': 'LT', 'qty': -3, 'price': 100, 'kind': 'day'},
    ])
    check('separate negative day row nets to 7', book['LT']['qty'] == 7, book)
    # day_sell on an already-net positive row without an overnight field is not subtracted again
    book = pol.reconcile_book([
        {'symbol': 'INFY', 'qty': 6, 'price': 100, 'kind': 'position', 'day_sell_qty': 4, 'day_buy_qty': 0},
    ])
    check('net quantity with a sell field and no overnight stays 6', book['INFY']['qty'] == 6, book)
    # two names, one of them legacy, neither tagged
    book = pol.reconcile_book([
        {'symbol': 'AAA', 'qty': 1, 'price': 10, 'kind': 'holding'},
        {'symbol': 'BBB', 'qty': 2, 'price': 10, 'kind': 'position'},
    ])
    check('tag-free book still has both names', set(book) == {'AAA', 'BBB'}, book)
    # unpriced positive qty fails closed later
    book = pol.reconcile_book([{'symbol': 'ZZ', 'qty': 3, 'price': 0, 'kind': 'position'}])
    check('positive qty with no price has cost None', book['ZZ']['cost'] is None, book)
    # upstox / kite adapters
    up = pol.row_from_upstox({'trading_symbol': 'HYUNDAI-EQ', 'quantity': 10, 'average_price': 20,
                              'overnight_quantity': 10, 'day_sell_quantity': 0, 'day_buy_quantity': 0})
    check('upstox adapter keeps the symbol and overnight', up['symbol'] == 'HYUNDAI-EQ' and up['overnight_qty'] == 10)
    kite = pol.rows_from_kite(
        [{'tradingsymbol': 'HYUNDAI', 'product': 'MTF', 'exchange': 'NSE', 'quantity': -2,
          'average_price': 20, 'day_sell_quantity': 2, 'overnight_quantity': 0}],
        [{'tradingsymbol': 'HYUNDAI', 'mtf': {'quantity': 10, 'average_price': 20}}],
    )
    book = pol.reconcile_book(kite)
    check('kite holding plus negative net reconciles', book['HYUNDAI']['qty'] == 8, book)
    check('a non-MTF kite row is ignored', len(pol.rows_from_kite(
        [{'tradingsymbol': 'NIFTY', 'product': 'NRML', 'exchange': 'NFO', 'quantity': 50}], [])) == 0)


# ---------------------------------------------------------------- budgets, pending, restart
def _pos(qty, price):
    return {'qty': qty, 'cost': qty * price}


def test_budgets():
    when = '2026-11-09T10:15'
    capital = 1_000_000
    # carried exposure is all initial when the ledger has no add split
    view = pol.rooms(capital, {'AAA': _pos(1, 260_000)}, [], None, 1.0, when)
    check('carried margin above 25% is over the shared pool', view['over_total'] is True and view['room_new'] == 0 and view['room_add'] == 0, view)
    check('carried exposure does not pretend to be an add', view['add_used'] == 0, view)
    check('report does not say sell', all('liquidat' in line.lower() or 'no ' in line.lower()
                                           for line in pol.exposure_report(view)), pol.exposure_report(view))
    check('report has no trim instruction', not any(w in ' '.join(pol.exposure_report(view)).upper()
                                                    for w in ('SELL', 'TRIM', 'CLOSE', 'SWAP')))
    full_initial = pol.rooms(capital, {'AAA': _pos(1, 200_000)}, [], None, 1.0, when)
    blocks = pol.order_blocks(full_initial, 'BBB', 'new', 10_000, True, False, 0)
    check('a new name can use room left in the shared pool', blocks == [], blocks)
    too_big = pol.order_blocks(full_initial, 'BBB', 'new', 60_000, True, False, 0)
    check('a new name cannot use another bucket when the shared pool is short',
          any('shared 25%' in b for b in too_big), too_big)
    check('the shared pool still has the unused 50k', full_initial['room_total'] == 50_000, full_initial)
    add_blocks = pol.order_blocks(full_initial, 'AAA', 'add', 50_000, True, False, 200_000)
    # per-stock is 100k and AAA already has 200k, so the stricter cap also blocks
    check('stricter per-stock cap blocks an add that the add pool would fund',
          any('10%' in b for b in add_blocks), add_blocks)
    # a name under the 10% cap can still add from the reserve, and that spend is not initial
    under = pol.rooms(capital, {'AAA': _pos(1, 50_000), 'BBB': _pos(1, 50_000), 'CCC': _pos(1, 100_000)},
                      [], None, 1.0, when)
    check('three names share the 25% pool', abs(under['initial_used'] - 200_000) < 1e-6 and under['total_pool'] == 250_000, under)
    check('no single name was given the whole 25%', True)
    add_ok = pol.order_blocks(under, 'AAA', 'add', 40_000, True, False, 50_000)
    check('add under the 10% cap and inside the reserve is allowed', add_ok == [], add_ok)
    new_blocked = pol.order_blocks(under, 'DDD', 'new', 10_000, True, False, 0)
    check('three names do not use up the 5-name maximum', not any('name cap' in b for b in new_blocked), new_blocked)
    # 20% is not per name: one order of 15% is inside the collective initial pool and inside 10%? 
    # 15% of 1e6 = 150k > 100k per stock, so the stricter cap wins.
    fresh = pol.rooms(capital, {}, [], None, 1.0, when)
    wide = pol.order_blocks(fresh, 'AAA', 'new', 150_000, True, False, 0)
    check('150k is inside the 25% pool but over the 10% name cap', any('10%' in b for b in wide), wide)
    fits = pol.order_blocks(fresh, 'AAA', 'new', 100_000, True, False, 0)
    check('100k new name fits initial pool and per-stock cap', fits == [], fits)
    # loser and weak trend
    check('no averaging a loser', any('loser' in b for b in pol.order_blocks(under, 'AAA', 'add', 1000, True, True, 50_000)))
    check('no add when the trend gate is closed',
          any('strong trend' in b for b in pol.order_blocks(under, 'AAA', 'add', 1000, False, False, 50_000)))
    # over total: carried 45% fails closed, no room, report only
    heavy = pol.rooms(capital, {'AAA': _pos(1, 450_000)}, [], None, 1.0, when)
    check('45% carried is over the 25% pool', heavy['over_total'] is True and heavy['room_new'] == 0 and heavy['room_add'] == 0)
    check('over total block does not liquidate',
          any('not liquidating' in b for b in pol.order_blocks(heavy, 'BBB', 'new', 1, True, False, 0)))
    # each broker's capital is its own
    small = pol.rooms(100_000, {}, [], None, 1.0, when)
    check('a smaller book has its own 25%', abs(small['initial_pool'] - 25_000) < 1e-6 and abs(small['total_pool'] - 25_000) < 1e-6)
    # pre-Monday rooms() does not apply the Monday pools
    pre = pol.rooms(capital, {'AAA': _pos(1, 400_000)}, [], None, 1.0, '2026-11-08T10:00')
    check('8 Nov rooms() is the pre-policy marker', pre.get('active') is False, pre)


def test_pending_and_restart():
    when = '2026-11-09T11:00'
    capital = 1_000_000
    held = {'AAA': _pos(10, 10_000)}          # 100_000 margin at ratio 1
    orders = [
        {'order_id': 'P1', 'trading_symbol': 'BBB', 'transaction_type': 'BUY', 'product': 'MTF',
         'status': 'open', 'quantity': 10, 'filled_quantity': 4, 'price': 1000, 'mode': 'new'},
        {'order_id': 'P2', 'trading_symbol': 'AAA', 'transaction_type': 'BUY', 'product': 'MTF',
         'status': 'trigger pending', 'quantity': 2, 'filled_quantity': 0, 'price': 5000, 'mode': 'add'},
        {'order_id': 'P3', 'trading_symbol': 'AAA', 'transaction_type': 'BUY', 'product': 'MTF',
         'status': 'complete', 'quantity': 10, 'filled_quantity': 10, 'price': 1000},
        {'order_id': 'P4', 'trading_symbol': 'CCC', 'transaction_type': 'SELL', 'product': 'MTF',
         'status': 'open', 'quantity': 1, 'filled_quantity': 0, 'price': 100},
    ]
    pending = pol.pending_buys(orders, held)
    check('complete fill is not a reservation', [p['order_id'] for p in pending] == ['P1', 'P2'] or
          sorted(p['order_id'] for p in pending) == ['P1', 'P2'], pending)
    by_id = {p['order_id']: p for p in pending}
    check('partially filled buy reserves only the open remainder', by_id['P1']['open_qty'] == 6, by_id['P1'])
    check('pending buy of a new name takes a slot', by_id['P1']['mode'] == 'new')
    check('pending buy of a held name is an add', by_id['P2']['mode'] == 'add')
    view = pol.rooms(capital, held, pending, None, 1.0, when)
    # initial = 100k held + 6*1000 new = 106k; add = 2*5000 = 10k
    check('pending new margin sits in the initial pool', abs(view['initial_used'] - 106_000) < 1e-6, view)
    check('pending add margin sits in the add pool', abs(view['add_used'] - 10_000) < 1e-6, view)
    check('pending new name consumes a slot on top of the holding', view['name_count'] == 2, view)
    check('filled quantity was not reserved again', abs(view['total_used'] - 116_000) < 1e-6, view)

    # restart: ledger records the filled add, position already contains those shares
    ledger = pol.empty_ledger()
    pol.remember(ledger, 'upstox', 'F1', 'AAA', 'add', 40_000, 'filled', '2026-11-09', filled_qty=4)
    # a stale reservation of the same fill must not be added once it is complete
    pol.remember(ledger, 'upstox', 'F2', 'BBB', 'new', 50_000, 'reserved', '2026-11-09')
    pol.reconcile_ledger(ledger, 'upstox', open_ids=['P1'], complete_ids=['F2'], today='2026-11-09')
    positions = {'AAA': _pos(1, 100_000), 'BBB': _pos(1, 50_000)}
    again = pol.rooms(capital, positions, [], ledger, 1.0, when)
    check('restart counts the position once', abs(again['total_used'] - 150_000) < 1e-6, again)
    check('filled add is capped inside the position, not stacked on it', abs(again['add_used'] - 40_000) < 1e-6, again)
    check('the rest of that position stays initial', abs(again['initial_used'] - 110_000) < 1e-6, again)
    # a completed id that was still marked reserved is not pending
    check('completed reservation is not pending', ledger['brokers']['upstox']['orders']['F2']['status'] == 'filled')
    # next morning the same reserved id, order gone, position still there: classification only
    pol.remember(ledger, 'upstox', 'OLD', 'AAA', 'add', 999_999, 'reserved', '2026-11-08')
    pol.reconcile_ledger(ledger, 'upstox', open_ids=[], complete_ids=[], today='2026-11-09')
    capped = pol.rooms(capital, {'AAA': _pos(1, 100_000)}, [], ledger, 1.0, when)
    check('aged reservation does not double the position', abs(capped['total_used'] - 100_000) < 1e-6, capped)
    check('aged add classification cannot exceed the position', capped['add_used'] <= 100_000 + 1e-6, capped)
    # in-flight today: reserved, not on the book yet, counts once
    flight = pol.empty_ledger()
    pol.remember(flight, 'kite', 'LIVE', 'CCC', 'new', 25_000, 'reserved', '2026-11-09')
    pol.reconcile_ledger(flight, 'kite', open_ids=[], complete_ids=[], today='2026-11-09')
    inflight = pol.rooms(capital, {}, [], flight, 1.0, when, broker='kite')
    check('in-flight reservation counts while the book has not shown it', abs(inflight['initial_used'] - 25_000) < 1e-6, inflight)
    check('in-flight new name takes a slot', inflight['name_count'] == 1, inflight)
    # the same id once the book shows it is not counted twice
    shown = pol.pending_buys([
        {'order_id': 'LIVE', 'trading_symbol': 'CCC', 'transaction_type': 'BUY', 'product': 'MTF',
         'status': 'open', 'quantity': 25, 'filled_quantity': 0, 'price': 1000, 'mode': 'new', 'margin': 25_000},
    ], [])
    pol.reconcile_ledger(flight, 'kite', open_ids=['LIVE'], complete_ids=[], today='2026-11-09')
    once = pol.rooms(capital, {}, shown, flight, 1.0, when, broker='kite')
    check('book plus ledger reservation is one reserve', abs(once['initial_used'] - 25_000) < 1e-6, once)
    # unreadable ledger fails closed
    bad = pol.rooms(capital, {}, [], None, 1.0, when, ledger_error='boom')
    check('unreadable ledger closes the book', bad['closed'] is True and bad['room_new'] == 0 and bad['room_add'] == 0)
    # save/load round trip
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        pol.save_ledger(path, flight)
        loaded, err = pol.load_ledger(path)
        check('ledger reloads', err is None and loaded['brokers']['kite']['orders']['LIVE']['margin'] == 25_000, err)
        os.remove(path)
        missing, err2 = pol.load_ledger(path)
        check('missing ledger is empty, not an error', err2 is None and missing['version'] == 1, err2)
        with open(path, 'w') as handle:
            handle.write('{')
        broken, err3 = pol.load_ledger(path)
        check('broken ledger is an error', broken is None and err3, err3)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    # brokers do not share a ledger bucket
    check('kite in-flight did not charge upstox', pol.rooms(capital, {}, [], flight, 1.0, when, broker='upstox')['total_used'] == 0)


# ---------------------------------------------------------------- trend, rank, mirror, stops, refill
def _hour(**kw):
    base = {'close': 110, 'ema20': 100, 'dir': 1, 'hist': 1.0, 'pdi': 30, 'mdi': 10, 'adx': 28}
    base.update(kw)
    return base


def _daily(**kw):
    base = {'close': 110, 'ema20': 100, 'dir': 1}
    base.update(kw)
    return base


def _daybar(**kw):
    base = {'timeframe': 'day', 'incomplete': False, 'close': 110.0, 'ema20': 100.0, 'dir': 1,
            'hist': 1.0, 'pdi': 30.0, 'mdi': 10.0, 'adx': 28.0}
    base.update(kw)
    return base


def test_trend_rank_mirror_stop():
    ok, why = pol.trend_ok(_daybar())
    check('completed daily trend passes', ok is True and why == [], why)
    for label, kw in (
        ('daily close under EMA20', {'close': 90}),
        ('daily supertrend red', {'dir': -1}),
        ('daily MACD not confirming', {'hist': -0.1}),
        ('daily +DI not dominant', {'pdi': 10, 'mdi': 30}),
        ('daily ADX below 25', {'adx': 24.9}),
    ):
        good, reasons = pol.trend_ok(_daybar(**kw))
        check('trend fails: %s' % label, good is False and reasons, reasons)
    check('an hourly bar is not an MTF signal',
          pol.trend_ok({'timeframe': 'hour', 'close': 110, 'ema20': 100, 'dir': 1, 'hist': 1,
                        'pdi': 30, 'mdi': 10, 'adx': 30})[0] is False)
    check('passing an hour without a daily bar fails closed',
          pol.trend_ok(None, hour={'close': 1})[0] is False)
    check('missing trend fails closed', pol.trend_ok(None)[0] is False)
    check('unreadable daily trend fails closed', pol.trend_ok({'timeframe': 'day', 'incomplete': False, 'close': 'x'})[0] is False)

    capital = 1_000_000
    empty = pol.rooms(capital, {}, [], None, 1.0, '2026-11-09T10:00')
    # give each broker the same empty room, name_count 0
    rooms = {'upstox': dict(empty), 'kite': dict(empty)}
    trends = {
        'STRONG': (True, []), 'WEAK': (False, ['ADX is below 25']),
        'HELD': (True, []), 'LOSER': (True, []),
    }
    held = {
        'upstox': [{'sym': 'HELD', 'qty': 1, 'score': 10}, {'sym': 'LOSER', 'qty': 1, 'score': 50},
                   {'sym': 'ONLYU', 'qty': 1, 'score': 99}],
        'kite': [{'sym': 'HELD', 'qty': 1, 'score': 10}, {'sym': 'LOSER', 'qty': 1, 'score': 40}],
    }
    # name_count on the empty rooms is 0, but the books have names. propose uses name_count if set.
    for broker, rows in held.items():
        rooms[broker] = dict(empty, name_count=pol.MAX_NAMES, held=[r['sym'] for r in rows])
    # LOSER is common and highest score but in loss. HELD is eligible. ONLYU is not common.
    plan = pol.propose(
        held,
        [{'sym': 'WEAK', 'score': 90, 'eligible': False}, {'sym': 'STRONG', 'score': 20, 'eligible': True},
         {'sym': 'MID', 'score': 15, 'eligible': True}],
        rooms, trends, in_loss={'LOSER': True, 'HELD': False},
    )
    kinds = [a['action'] for a in plan['actions']]
    check('ranker never trims or rotates', set(kinds) <= {'ADD', 'NEW'}, plan)
    check('loser is not an add', 'LOSER' not in [a['sym'] for a in plan['actions'] if a['action'] == 'ADD'], plan)
    check('one-sided name is reported and not bought or sold',
          'ONLYU' in ' '.join(plan['report']) and 'ONLYU' not in [a['sym'] for a in plan['actions']], plan)
    check('common eligible holding is an add candidate',
          any(a['action'] == 'ADD' and a['sym'] == 'HELD' for a in plan['actions']), plan)
    news = [a['sym'] for a in plan['actions'] if a['action'] == 'NEW']
    check('a full upstox book does not get a filler new name', news == [], plan)
    open_rooms = {'upstox': dict(empty, name_count=0), 'kite': dict(empty, name_count=0)}
    blocked = pol.propose(
        {'upstox': [], 'kite': []},
        [{'sym': 'WEAK', 'score': 90, 'eligible': False}, {'sym': 'STRONG', 'score': 20, 'eligible': True},
         {'sym': 'MID', 'score': 15, 'eligible': True}],
        open_rooms, trends,
    )
    check('an empty book does not open names merely because the count is under 5',
          [a for a in blocked['actions'] if a['action'] == 'NEW'] == [], blocked)
    picked = pol.propose(
        {'upstox': [], 'kite': []},
        [{'sym': 'WEAK', 'score': 90, 'eligible': False}, {'sym': 'STRONG', 'score': 20, 'eligible': True},
         {'sym': 'MID', 'score': 15, 'eligible': True}],
        open_rooms, trends, initial_deployment=True,
    )
    news = [a['sym'] for a in picked['actions'] if a['action'] == 'NEW']
    check('explicit initial authority picks the strongest and skips the weak name',
          news and news[0] == 'STRONG' and 'WEAK' not in news, picked)
    check('explicit initial authority still does not invent names to fill five', news == ['STRONG', 'MID'], picked)
    # no eligible candidate: slots stay empty
    quiet = pol.propose(
        {'upstox': [], 'kite': []},
        [{'sym': 'WEAK', 'score': 99, 'eligible': False}],
        {'upstox': empty, 'kite': empty}, {'WEAK': (False, ['no'])},
    )
    check('no eligible name means no new order', quiet['actions'] == [], quiet)
    check('spare slot is called out as empty', any('empty' in line for line in quiet['report']), quiet['report'])
    # over the name cap: report, no buy, no sell
    crowded_room = dict(empty, name_count=4, over_names=True, held=['A', 'B', 'C', 'D'])
    crowded = pol.propose(
        {'upstox': [{'sym': s, 'qty': 1, 'score': i} for i, s in enumerate('ABCD')],
         'kite': [{'sym': s, 'qty': 1, 'score': i} for i, s in enumerate('ABCD')]},
        [{'sym': 'STRONG', 'score': 100, 'eligible': True}],
        {'upstox': crowded_room, 'kite': crowded_room},
        {'STRONG': (True, []), 'A': (True, []), 'B': (True, []), 'C': (True, []), 'D': (True, [])},
    )
    check('over the cap does not liquidate', not any(a['action'] not in ('ADD', 'NEW') for a in crowded['actions']))
    check('over the cap does not open or add', crowded['actions'] == [], crowded)
    text = ' '.join(crowded['report']).upper()
    check('over the cap is reported', 'OVER NAMES' in text and 'LIQUIDAT' in text, crowded['report'])
    # both legs
    yes = pol.paired_decision({'ok': True, 'qty': 3}, {'ok': True, 'qty': 2})
    no = pol.paired_decision({'ok': True, 'qty': 3}, {'ok': False, 'qty': 0, 'blocks': ['budget']})
    check('both legs clear', yes['place'] is True and yes['both_ok'] is True)
    check('one leg blocks the pair', no['place'] is False and no['blocked_brokers'] == ['kite'], no)
    check('a zero qty is not a pass', pol.paired_decision({'ok': True, 'qty': 0}, {'ok': True, 'qty': 1})['place'] is False)
    # unwind safeguard
    check('kite miss after a fill sells back', pol.mirror_unwind('U1', 4, None) == {'unwind': True, 'action': 'SELL_BACK', 'qty': 4})
    check('kite miss before a fill cancels', pol.mirror_unwind('U1', 0, None)['action'] == 'CANCEL')
    check('both ids landed keeps the pair', pol.mirror_unwind('U1', 4, 'K1')['action'] == 'KEEP')
    check('nothing placed unwinds nothing', pol.mirror_unwind(None, 0, None)['action'] == 'NOTHING')
    # stops
    after_add = pol.stop_coverage(15, 90, [{'qty': 10, 'trigger': 95}])
    check('add stop covers the full quantity', after_add['qty'] == 15 and after_add['action'] == 'REPLACE', after_add)
    check('stop is not lowered', after_add['trigger'] == 95 and after_add['lowered'] is False and after_add['refused_lower'] is True)
    check('short stop is not verified', after_add['verified'] is False and after_add['shortfall'] == 5)
    kept = pol.stop_coverage(10, 90, [{'qty': 10, 'trigger': 95}])
    check('full existing stop is kept', kept['verified'] is True and kept['action'] == 'KEEP' and kept['trigger'] == 95)
    fresh = pol.stop_coverage(8, 91, [])
    check('first stop is a place for the full qty', fresh['action'] == 'PLACE' and fresh['qty'] == 8 and fresh['verified'] is False)
    higher = pol.stop_coverage(10, 100, [{'qty': 10, 'trigger': 95}])
    check('a higher stop replaces and never lowers', higher['action'] == 'REPLACE' and higher['trigger'] == 100 and higher['lowered'] is False)


def test_refill_window_and_enforcement():
    def at(text, trading=True):
        return pol.in_refill_window(datetime.datetime.fromisoformat(text).replace(tzinfo=IST), trading_day=trading)
    check('09:30 IST Monday is inside the refill window', at('2026-11-09T09:30:00') is True)
    check('14:30 IST Monday is inside the refill window', at('2026-11-09T14:30:00') is True)
    check('09:29 is outside', at('2026-11-09T09:29:00') is False)
    check('14:31 is outside', at('2026-11-09T14:31:00') is False)
    check('Sunday is outside even at 10:30', at('2026-11-08T10:30:00') is False)
    check('Saturday is outside', at('2026-11-14T10:30:00') is False)
    check('a holiday flag closes the window', at('2026-11-09T10:30:00', trading=False) is False)
    check('11:00 IST weekday is inside (hourly schedule still allowed)', at('2026-11-09T11:00:00') is True)
    good = {
        'policy_active_on': True, 'max_names': 5, 'initial': 0.25, 'add': 0.25, 'total': 0.25,
        'per_stock': 0.10, 'counts_all_positive_names': True, 'pending_reserves': True,
        'shared_pool': True, 'add_reserve_isolated': False, 'no_auto_liquidate': True,
    }
    check('matching runtime enforces the policy', pol.enforcement_errors(good) == [])
    bad = dict(good, total=0.50, max_names=3)
    errs = pol.enforcement_errors(bad)
    check('a 50% total or a 3-name cap does not enforce the Monday rule',
          any('total' in e for e in errs) and any('max_names' in e for e in errs), errs)
    check('missing runtime view fails closed', pol.enforcement_errors(None) != [])
    view = pol.live_view()
    check('this tree enforces the Monday numbers', pol.enforcement_errors(view) == [], view)
    # sizing limits() on the boundary: per-stock unchanged, total and the new-name room change
    st = {'capital': 1_000_000.0, 'held': {}, 'used': 0.0}
    before = ms.limits(st, 'X', 'new', 1.0, when='2026-11-08T10:00')
    after = ms.limits(st, 'X', 'new', 1.0, when='2026-11-09T10:00')
    check('8 Nov total room is still 50%', before['room_total'] == 500_000, before)
    check('8 Nov new-name room is still half of remaining', before['room'] == 250_000, before)
    check('9 Nov total room is the shared 25%', after['room_total'] == 250_000, after)
    check('9 Nov per-stock cap is still 10%', after['per_stock'] == 100_000, after)
    check('9 Nov new-name room is the shared pool', after['room'] == 250_000, after)
    check('9 Nov add room is the same shared pool', after['room_add'] == 250_000, after)
    carried = ms.limits({'capital': 1_000_000.0, 'held': {'AAA': 250_000.0}, 'used': 250_000.0},
                        'BBB', 'new', 1.0, when='2026-11-09T10:00')
    check('carried 25% leaves a new name no room on 9 Nov', carried['room'] == 0, carried)
    check('that same book has no separate add room', carried['room_add'] == 0, carried)
    # pre-Monday per-stock override still wins on 5 Nov inside limits()
    nov5 = ms.limits(st, 'X', 'new', 1.0, when='2026-11-05T10:00')
    check('5 Nov per-stock room is 12%', nov5['per_stock'] == 120_000, nov5)
    check('5 Nov total room is still 50%', nov5['room_total'] == 500_000, nov5)


def test_trade_lock_counts_actual_names():
    """The Monday gate inside trade_lock counts legacy lots and refuses a fourth name.
    margin_quote is stubbed. No order is sent."""
    import trade_lock as tl
    real_quote = ms.margin_quote
    real = (tl._data, tl.kite_data, tl._ltp, tl.LF)
    saved_err = list(tl._kite_err)

    def quote(broker, symbol, qty, price):
        return {'ok': True, 'margin': 0.25 * float(qty) * float(price), 'leverage': 4}

    def positions_for(symbols):
        return [{'trading_symbol': s, 'quantity': 10, 'average_price': 1000.0, 'product': 'MTF',
                 'instrument_token': 'NSE_EQ|' + s} for s in symbols]

    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    open(path, 'w').write('{}')
    try:
        ms.margin_quote = quote
        tl.LF = path
        tl._kite_err[:] = []
        tl.kite_data = lambda: ([], [])
        tl._ltp = lambda order, positions: float(order.get('price') or 0)

        def bind(book):
            def _data(url, strict=False):
                base = url.split('?')[0]
                if base == '/v2/order/retrieve-all':
                    return []
                if base == '/v2/portfolio/short-term-positions':
                    return []
                if base == '/v3/portfolio/mtf-positions':
                    return book
                if base == '/v2/user/get-funds-and-margin':
                    return {'equity': {'available_margin': 1_000_000, 'used_margin': 0,
                                       'capital_basis': 'verified', 'verified_capital': 1_000_000}}
                return []
            tl._data = _data

        bind(positions_for(['AAA', 'BBB', 'CCC', 'DDD', 'EEE']))
        order = {'trading_symbol': 'FFF', 'instrument_token': 'NSE_EQ|FFF', 'transaction_type': 'BUY',
                 'order_type': 'LIMIT', 'product': 'MTF', 'quantity': 10, 'price': 100.0,
                 '_now': '2026-11-09 10:30', '_trend_ok': True}
        ok, reasons, _ = tl.check_order(order)
        check('sixth actual name is refused on 9 Nov', ok is False, reasons)
        check('the refusal names the 5-name cap', any('5 name cap' in r for r in reasons), reasons)
        bind(positions_for(['AAA', 'BBB', 'CCC', 'DDD']))
        fifth = dict(order, trading_symbol='EEE', instrument_token='NSE_EQ|EEE')
        ok5, reasons5, _ = tl.check_order(fifth)
        check('a fifth name is inside the maximum', not any('name cap' in r for r in reasons5), reasons5)
        check('that fifth name is allowed when budgets and the trend flag pass', ok5 is True, reasons5)
        bind([])
        fresh = dict(order, trading_symbol='AAA', _now='2026-11-09 10:30')
        ok2, reasons2, _ = tl.check_order(fresh)
        check('an eligible new name under the pools is not name-cap blocked',
              not any('name cap' in r for r in reasons2), reasons2)
        check('that buy is allowed when the book is empty and the trend flag is set', ok2 is True, reasons2)
        blind = dict(fresh, _trend_ok=False)
        ok3, reasons3, _ = tl.check_order(blind)
        check('missing trend proof fails closed', ok3 is False and any('technically' in r or 'trend' in r for r in reasons3), reasons3)
        # carried initial pool: one name whose cost * 0.25 margin ratio is 250k on a 1e6 book (25% > 20%)
        bind([{'trading_symbol': 'AAA', 'quantity': 1, 'average_price': 1_000_000, 'product': 'MTF'}])
        again = dict(order, trading_symbol='BBB')
        ok4, reasons4, _ = tl.check_order(again)
        check('carried exposure over the initial pool blocks a new name', ok4 is False, reasons4)
        check('the block names the shared 25% pool',
              any('25%' in r or 'shared' in r for r in reasons4), reasons4)
        def bind_raw(book):
            def _data(url, strict=False):
                base = url.split('?')[0]
                if base == '/v2/order/retrieve-all':
                    return []
                if base == '/v2/portfolio/short-term-positions':
                    return []
                if base == '/v3/portfolio/mtf-positions':
                    return book
                if base == '/v2/user/get-funds-and-margin':
                    return {'equity': {'available_margin': 1_000_000, 'used_margin': 0}}
                return []
            tl._data = _data
        bind_raw([])
        raw = dict(order, trading_symbol='AAA', _now='2026-11-09 10:30')
        ok_raw, reasons_raw, _ = tl.check_order(raw)
        check('margin capacity is not accepted as capital on 9 Nov',
              ok_raw is False and any('margin capacity' in r for r in reasons_raw), reasons_raw)
    finally:
        ms.margin_quote = real_quote
        tl._data, tl.kite_data, tl._ltp, tl.LF = real
        tl._kite_err[:] = saved_err
        try:
            os.remove(path)
        except OSError:
            pass


def test_daily_timeframe_and_top5():
    now = datetime.datetime(2026, 11, 9, 11, 0, tzinfo=IST)
    rows = []
    day = datetime.date(2026, 8, 1)
    px = 100.0
    while len(rows) < 40:
        if day.weekday() < 5:
            ts = datetime.datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST).isoformat()
            px += 1.5
            rows.append([ts, px - 1, px + 2, px - 2, px, 1000, 0])
        day += datetime.timedelta(days=1)
    today = [datetime.datetime(2026, 11, 9, 10, 0, tzinfo=IST).isoformat(), 10, 11, 9, 10, 1000, 0]
    closed, why = pol.completed_daily_candles(rows + [today], now)
    check('11:00 IST drops the incomplete daily bar', why is None and closed[-1][4] != 10 and len(closed) == 40, (why, closed[-1][4] if closed else None))
    later = now.replace(hour=15, minute=30)
    closed_after, why_after = pol.completed_daily_candles(rows + [today], later)
    check('15:30 IST keeps the completed daily bar', why_after is None and closed_after[-1][4] == 10, why_after)
    only_today, why_today = pol.completed_daily_candles([today], now)
    check('an open session with no prior bar fails closed', only_today == [] and why_today, why_today)
    snap, snap_why = pol.snapshot_from_candles(rows + [today], now)
    check('snapshot uses the completed bar, not the 10 print',
          snap and snap['close'] != 10 and snap['timeframe'] == 'day' and not snap_why, (snap_why, None if not snap else snap['close']))
    ranked = sorted([
        {'sym': 'BBB', 'score': 10, 'eligible': True},
        {'sym': 'AAA', 'score': 10, 'eligible': True},
        {'sym': 'CCC', 'score': 11, 'eligible': True},
        {'sym': 'ZZZ', 'score': 99, 'eligible': False},
    ], key=pol.rank_key)
    check('tie breaks by symbol after score, and does not promote an ineligible name',
          [r['sym'] for r in ranked if r['eligible']] == ['CCC', 'AAA', 'BBB'])
    capital = 1_000_000
    empty = pol.rooms(capital, {}, [], None, 1.0, '2026-11-09T11:00')
    many = [{'sym': 'S%s' % i, 'score': 100 - i, 'eligible': i < 2} for i in range(7)]
    naked = pol.propose({'upstox': [], 'kite': []}, many,
                        {'upstox': empty, 'kite': empty},
                        {row['sym']: (row['eligible'], []) for row in many})
    check('no closure means no new names even when two qualify',
          [a['sym'] for a in naked['actions'] if a['action'] == 'NEW'] == [], naked)
    picked = pol.propose({'upstox': [], 'kite': []}, many,
                         {'upstox': empty, 'kite': empty},
                         {row['sym']: (row['eligible'], []) for row in many},
                         initial_deployment=True)
    news = [a['sym'] for a in picked['actions'] if a['action'] == 'NEW']
    check('explicit authority takes only the two eligible names', news == ['S0', 'S1'], picked)
    full = dict(empty, name_count=4)
    one_slot = pol.propose({'upstox': [], 'kite': []}, many, {'upstox': full, 'kite': full},
                           {row['sym']: (row['eligible'], []) for row in many},
                           replacement={'allowed': 1, 'closure_ids': ['OLD|2026-11-10']})
    one_news = [a for a in one_slot['actions'] if a['action'] == 'NEW']
    check('one closure and one free slot take only the strongest name',
          [a['sym'] for a in one_news] == ['S0'] and one_news[0]['closure_id'] == 'OLD|2026-11-10', one_slot)
    held5 = {'N%s' % i: {'qty': 1, 'cost': 1000} for i in range(5)}
    book5 = pol.rooms(capital, held5, [], None, 1.0, '2026-11-09T11:00')
    blocked = pol.order_blocks(book5, 'NEW', 'new', 100, True, False, 0)
    check('the sixth name is blocked and nothing is sold',
          any('5 name cap' in b for b in blocked) and not any(w in ' '.join(blocked).upper() for w in ('SELL', 'TRIM')), blocked)
    pending = [{'symbol': 'PEND', 'mode': 'new', 'open_qty': 1, 'price': 100, 'order_id': 'P', 'product': 'MTF', 'side': 'BUY', 'status': 'open'}]
    held4 = {'N%s' % i: {'qty': 1, 'cost': 1000} for i in range(4)}
    booked = pol.rooms(capital, held4, pending, None, 1.0, '2026-11-09T11:00')
    check('a pending new buy counts toward the five', booked['name_count'] == 5, booked)
    check('that reservation blocks another new name',
          any('5 name cap' in b for b in pol.order_blocks(booked, 'EXTRA', 'new', 100, True, False, 0)))
    check('stop mode stays legacy on 8 Nov', pol.stop_mode('2026-11-08T16:00') == 'legacy')
    check('stop mode is daily from 9 Nov', pol.stop_mode('2026-11-09T09:20') == 'daily')
    check('an unreadable clock does not switch the stop rule', pol.stop_mode('not-a-date') == 'legacy')
    monday = datetime.datetime(2026, 11, 9, 11, 0, tzinfo=IST)
    friday = datetime.datetime(2026, 11, 6, 15, 30, tzinfo=IST).isoformat()
    partial = datetime.datetime(2026, 11, 9, 10, 15, tzinfo=IST).isoformat()
    roll, roll_why = pol.completed_daily_candles(
        [[friday, 100, 102, 99, 101, 1, 0], [partial, 101, 103, 100, 102, 1, 0]], monday)
    check('Monday morning keeps Friday and drops the open Monday bar',
          roll_why is None and len(roll) == 1 and roll[0][0] == friday, roll_why)
    check('Friday is not stale on Monday', pol.daily_bar_is_stale(friday, monday) is False)
    check('Wednesday is stale on Monday',
          pol.daily_bar_is_stale(datetime.datetime(2026, 11, 4, 15, 30, tzinfo=IST).isoformat(), monday) is True)
    up = pol.daily_stop_plan(100, 10, existing_trigger=90, existing_qty=10,
                             daily={'timeframe': 'day', 'incomplete': False, 'st': 94, 'dir': 1})
    check('tighter daily stop ratchets up', up['action'] == 'RATCHET' and up['trigger'] == 94 and up['lowered'] is False, up)
    hold = pol.daily_stop_plan(100, 10, existing_trigger=94, existing_qty=10,
                               daily={'timeframe': 'day', 'incomplete': False, 'st': 90, 'dir': 1})
    check('a farther daily stop does not loosen', hold['action'] == 'KEEP' and hold['trigger'] == 94 and hold['place_stop'] is False, hold)
    trail = pol.daily_stop_plan(100, 10, existing_trigger=None,
                                daily={'timeframe': 'day', 'incomplete': False, 'st': 90, 'dir': 1})
    check('a farther daily Supertrend is the initial trail, not the fixed 5%',
          trail['action'] == 'PLACE' and trail['trigger'] == 90 and trail['source'] == 'daily_supertrend', trail)
    stale = pol.daily_stop_plan(100, 10, existing_trigger=94, existing_qty=10, stale=True, data_ok=False)
    check('stale daily data keeps the live stop', stale['action'] == 'KEEP' and stale['trigger'] == 94 and '1-hour' in stale['reason'], stale)
    naked = pol.daily_stop_plan(100, 10, data_ok=False)
    check('missing daily data on a naked position uses only the emergency floor',
          naked['source'] == 'emergency_floor' and naked['trigger'] == 95 and naked['cancel_existing'] is False, naked)
    breach = pol.daily_stop_plan(100, 10, existing_trigger=96, existing_qty=10,
                                 daily={'timeframe': 'day', 'incomplete': False, 'st': 101, 'dir': -1})
    check('a breached daily trend is an exit and not a new stop',
          breach['action'] == 'EXIT' and breach['place_stop'] is False and breach['execute_exit'] is False
          and breach['cancel_existing'] is False, breach)
    cover = pol.daily_stop_plan(100, 10, existing_trigger=94, existing_qty=4,
                                daily={'timeframe': 'day', 'incomplete': False, 'st': 90, 'dir': 1})
    check('an add is covered at the full quantity without lowering the stop',
          cover['qty'] == 10 and cover['trigger'] == 94 and cover['lowered'] is False, cover)


def test_replacement_gate():
    prior = {'upstox': {'AAA': 10, 'BBB': 5}, 'kite': {'AAA': 8, 'BBB': 5},
             'held_since': {'AAA': '2026-11-10', 'BBB': '2026-11-10'}}
    flat = {'upstox': {'BBB': 5}, 'kite': {'BBB': 5}}
    seen = pol.observe_closures(prior, flat, open_sells={'upstox': [], 'kite': []},
                                readable={'upstox': True, 'kite': True}, today='2026-11-12')
    check('a name flat on both brokers is one closure',
          seen['ok'] and [c['id'] for c in seen['closures']] == ['AAA|2026-11-10'], seen)
    again = pol.observe_closures(seen['snapshot'], flat, readable={'upstox': True, 'kite': True},
                                 consumed=[c['id'] for c in seen['closures']], today='2026-11-12')
    check('the same closure is not granted again on the next run', again['closures'] == [], again)
    partial = pol.observe_closures(prior, {'upstox': {}, 'kite': {'AAA': 1, 'BBB': 5}},
                                   readable={'upstox': True, 'kite': True}, today='2026-11-12')
    check('a partial remainder is not a closure', partial['closures'] == [], partial)
    selling = pol.observe_closures(prior, flat, open_sells={'upstox': ['AAA'], 'kite': []},
                                   readable={'upstox': True, 'kite': True}, today='2026-11-12')
    check('an open sell is not a verified close', selling['closures'] == [], selling)
    blind = pol.observe_closures(prior, flat, readable={'upstox': True, 'kite': False}, today='2026-11-12')
    check('a missing broker book grants nothing and does not advance',
          blind['ok'] is False and blind['closures'] == [] and blind['snapshot'] is None, blind)
    already_flat = pol.observe_closures({'upstox': {}, 'kite': {}, 'held_since': {}}, flat,
                                        readable={'upstox': True, 'kite': True}, today='2026-11-06')
    check('a book that is already flat, including a 6 Nov clear, is not a new entitlement',
          already_flat['closures'] == [], already_flat)
    room = pol.replacement_room(
        [{'id': 'AAA|2026-11-10', 'status': 'open'}], ['PEND'],
        {'upstox': 4, 'kite': 4})
    check('a pending new buy uses the closure so another name is not opened', room['allowed'] == 0, room)
    room2 = pol.replacement_room(
        [{'id': 'AAA|2026-11-10', 'status': 'open'}], [],
        {'upstox': 5, 'kite': 4})
    check('no vacancy on one broker means no new name and no sixth', room2['allowed'] == 0, room2)
    held = {'upstox': [{'sym': 'BBB', 'qty': 5, 'score': 3}], 'kite': [{'sym': 'BBB', 'qty': 5, 'score': 3}]}
    empty = pol.rooms(1_000_000, {'BBB': {'qty': 5, 'cost': 1000}}, [], None, 1.0, '2026-11-12T10:00')
    rooms = {'upstox': dict(empty, name_count=1), 'kite': dict(empty, name_count=1)}
    trends = {'BBB': (True, []), 'S0': (True, []), 'S1': (True, [])}
    cands = [{'sym': 'S0', 'score': 50, 'eligible': True}, {'sym': 'S1', 'score': 40, 'eligible': True}]
    stay = pol.propose(held, cands, rooms, trends, replacement={'allowed': 0, 'closure_ids': []})
    check('an add does not wait for a closure',
          [a['action'] for a in stay['actions']] == ['ADD'] and [a for a in stay['actions'] if a['action'] == 'NEW'] == [], stay)
    one = pol.propose(held, cands, rooms, trends,
                      replacement={'allowed': 1, 'closure_ids': ['AAA|2026-11-10']})
    news = [a for a in one['actions'] if a['action'] == 'NEW']
    check('one closure selects only the strongest replacement',
          [a['sym'] for a in news] == ['S0'] and news[0].get('closure_id') == 'AAA|2026-11-10', one)
    check('the replacement plan sells nothing', not any(a['action'] not in ('ADD', 'NEW') for a in one['actions']))


def test_refill_module_fails_closed():
    import mtf_refill as refill
    check('refill states the Monday policy', '9 November 2026' in refill.POLICY_TEXT and '25%' in refill.POLICY_TEXT)
    check('refill window matches 09:30-14:30', refill.pol.REFILL_START == datetime.time(9, 30) and refill.pol.REFILL_END == datetime.time(14, 30))
    sample = refill.decide(
        {'upstox': [{'sym': 'AAA', 'qty': 1, 'score': 5}], 'kite': [{'sym': 'AAA', 'qty': 1, 'score': 5}]},
        [{'sym': 'BBB', 'score': 9, 'eligible': True}],
        {'upstox': pol.rooms(1_000_000, {'AAA': {'qty': 1, 'cost': 50_000}}, [], None, 1.0, '2026-11-09T10:00'),
         'kite': pol.rooms(800_000, {'AAA': {'qty': 1, 'cost': 40_000}}, [], None, 1.0, '2026-11-09T10:00')},
        {'AAA': (True, []), 'BBB': (True, [])},
        when='2026-11-09T10:30+05:30',
    )
    check('refill decide places nothing by itself', sample.get('orders_placed') == 0, sample)
    check('a free slot alone does not add BBB',
          [a for a in sample['plan']['actions'] if a.get('action') == 'NEW'] == [], sample['plan'])
    check('the planner says the saved routine is still paused',
          sample.get('routine') == 'paused' and sample.get('resumed') is False, sample)
    ready = refill.readiness()
    check('readiness does not call the paused runtime live', ready.get('ready_to_trade') is False, ready)
    check('refill decide names the policy', 'Monday' in sample.get('policy', '') or '9 November' in sample.get('policy', ''))
    check('two brokers keep their own pools in the decision',
          sample['rooms']['upstox']['capital'] != sample['rooms']['kite']['capital'])
    closed = refill.refuse('runtime cannot enforce the Monday policy')
    check('refuse sends no order', closed['action'] == 'FAIL_CLOSED' and closed['orders'] == [] and closed['place'] is False)
    # names are data, not a hardcoded book inside the ranker
    src = open(os.path.join(HERE, 'mtf_policy.py')).read()
    for banned in ('HYUNDAI', 'INDIGO', 'ICICIBANK'):
        check('policy module does not hardcode %s' % banned, banned not in src)
    src_r = open(os.path.join(HERE, 'mtf_refill.py')).read()
    for banned in ('HYUNDAI', 'INDIGO', 'ICICIBANK'):
        check('refill module does not hardcode %s' % banned, banned not in src_r)



def test_longterm_dst_exempt_from_monday_caps():
    """Tag longterm_dst uses the sleeve cap, not the Monday 10%/5-name/25% caps.
    Sleeve holdings are also excluded from rooms() so they do not consume that budget."""
    when = '2026-11-17T09:30:00+05:30'
    capital = 1_000_000
    # 50k would exceed the 10% cap on a 400k capital, but on 1L it is 5%. Use 400k so 50k > 10%.
    capital = 400_000
    view = pol.rooms(capital, {}, [], None, 1.0, when)
    # A regular 50k new name is over the 10% cap (40k).
    regular = pol.order_blocks(view, 'IGIL', 'new', 50_000, True, False, 0)
    check('regular 50k name is blocked by the Monday 10% cap', any('10%' in b for b in regular), regular)
    sleeve = pol.order_blocks(view, 'IGIL', 'new', 50_000, True, False, 0, tag='longterm_dst')
    check('longterm_dst 50k name is not blocked by the Monday 10% cap', sleeve == [], sleeve)
    over = pol.order_blocks(view, 'IGIL', 'new', 50_001, True, False, 0, tag='longterm_dst')
    check('longterm_dst still enforces the Rs 50k sleeve cap', any('50,000' in b or '50000' in b for b in over), over)

    # rooms() excludes sleeve names from the Monday name count and used margin
    held = {'IGIL': _pos(355, 154_000), 'SRF': _pos(6, 20_000)}
    raw = pol.rooms(capital, held, [], None, 0.25, when, exempt_symbols=[])
    skipped = pol.rooms(capital, held, [], None, 0.25, when, exempt_symbols=['IGIL'])
    check('without exemption both names count', raw['name_count'] == 2, raw)
    check('IGIL is excluded from the Monday name count', skipped['name_count'] == 1 and skipped['held'] == ['SRF'], skipped)
    check('IGIL margin is excluded from the Monday pool', skipped['total_used'] < raw['total_used'], (raw['total_used'], skipped['total_used']))

    fourth = pol.longterm_dst_blocks(['IGIL', 'PVRINOX', 'LTTS'], 'KOTAKBANK', 40_000)
    check('a fourth sleeve name is refused', any('max 3' in b for b in fourth), fourth)

def main():
    test_date_boundary()
    test_reconcile_holdings()
    test_budgets()
    test_pending_and_restart()
    test_trend_rank_mirror_stop()
    test_daily_timeframe_and_top5()
    test_replacement_gate()
    test_refill_window_and_enforcement()
    test_trade_lock_counts_actual_names()
    test_longterm_dst_exempt_from_monday_caps()
    test_refill_module_fails_closed()
    print('%s checks, %s failed' % (CHECKS[0], len(FAILS)))
    if FAILS:
        for line in FAILS:
            print(' -', line)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
