#!/usr/bin/env python3
"""Tests for autonomy.py and the rule hooks it drives (4 Nov 2026, t339u).

Run: python3 autonomy_test.py          (exit 0 = all pass)
     python3 autonomy_test.py -v       (print each check)

No orders are placed anywhere in this file, and no broker state is written. The rule tests
drive trade_lock.check_order with a forged `_now` (the parameter it already accepts for
exactly this) and stub the three broker reads it makes, so every assertion is about the RULE
and never about what the live book happened to hold at the second the test ran.

The property that matters most is expiry: on 2026-11-07 the user's own numbers must be back,
with no code change. Several checks below exist only to prove that.
"""
import datetime, json, os, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, '/workspace/upstox')
import autonomy as A
import mtf_sizing as ms
import trade_lock as tl

VERBOSE = '-v' in sys.argv
FAILS = []
CHECKS = [0]


def check(name, cond, detail=''):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(f'{name}: {detail}')
        print(f'FAIL  {name}  {detail}')
    elif VERBOSE:
        print(f'ok    {name}  {detail}')


# ================================================================ a) the date gate
def test_active_dates():
    table = {'2026-11-04': False, '2026-11-05': True, '2026-11-06': True,
             '2026-11-07': False, '2026-11-08': False, '2026-11-09': False}
    for d, want in table.items():
        for form in (d, d + 'T09:05', datetime.date.fromisoformat(d),
                     datetime.datetime.fromisoformat(d + 'T09:05')):
            got = A.active(form)
            check(f'active({form!r}) is {want}', got == want, f'got {got}')
    # a timestamp that is 5 Nov in UTC but already 6 Nov in IST belongs to 6 Nov
    check('UTC 5 Nov 20:00 -> IST 6 Nov, active',
          A.active('2026-11-05T20:00:00+00:00') is True, str(A.active('2026-11-05T20:00:00+00:00')))
    # ... and 6 Nov 19:00 UTC is 7 Nov 00:30 IST, so the user's rules are back
    check('UTC 6 Nov 19:00 -> IST 7 Nov, NOT active',
          A.active('2026-11-06T19:00:00+00:00') is False, str(A.active('2026-11-06T19:00:00+00:00')))
    # naive datetime is read as IST
    check('naive 2026-11-05 09:05 is active', A.active(datetime.datetime(2026, 11, 5, 9, 5)) is True)
    # a broken clock must not relax the rules
    check('garbage date -> not active (fails closed)', A.active('not-a-date') is False)


# ================================================================ b) the values
def test_override_values():
    want = {'INDEX_OPT_MAX': 6, 'INDEX_OPT_GAP_MIN': 20, 'INDEX_OPT_EXPIRY_CUTOFF': '13:00',
            'INDEX_OPT_LOSS': 6000, 'WINDOW_MIN': 15, 'STOCK_OPT_MAX_UND': 4,
            'STOCK_OPT_MAX_LOTS': 2, 'MTF_PER_STOCK': 0.12, 'MTF_TOTAL': 0.50}
    o = A.overrides('2026-11-05T09:05')
    for k, v in want.items():
        check(f'overrides()[{k}] == {v}', o.get(k) == v, f'got {o.get(k)!r}')
    check('overrides() carries a one-line why', isinstance(o.get('why'), str) and len(o['why']) > 40, o.get('why'))
    check('overrides() has no extras beyond the table + why',
          set(o) == set(want) | {'why'}, str(sorted(o)))
    check('overrides() is empty when inactive', A.overrides('2026-11-09T09:05') == {})
    check('overrides() returns a copy (caller cannot edit the table)',
          (A.overrides('2026-11-05')['INDEX_OPT_MAX'] == 6 and
           (lambda c: (c.__setitem__('INDEX_OPT_MAX', 99), A.overrides('2026-11-05')['INDEX_OPT_MAX'] == 6)[1])(A.overrides('2026-11-05'))))
    # effective() only moves a value it was asked about by name
    check('effective returns the default when inactive',
          A.effective('INDEX_OPT_MAX', 3, '2026-11-09') == 3)
    check('effective returns the override when active',
          A.effective('INDEX_OPT_MAX', 3, '2026-11-05') == 6)
    check('effective leaves an unlisted name alone even when active',
          A.effective('THRESHOLD', 30000, '2026-11-05') == 30000)
    # the two hard caps that must never be overridden
    check('THRESHOLD (Rs 30,000 breaker) is not in the override table', 'THRESHOLD' not in A.overrides('2026-11-05'))
    check('MTF_TOTAL is NOT raised', A.overrides('2026-11-05')['MTF_TOTAL'] == 0.50)
    # the module imports nothing that could reach a broker or an order
    src = open(os.path.join(HERE, 'autonomy.py')).read()
    for banned in ('order/place', 'import api', 'broker_kite', 'import requests', 'import urllib'):
        check(f'autonomy.py does not reference {banned!r}', banned not in src, banned)
    check('autonomy.py imports only datetime',
          [l.strip() for l in src.splitlines() if l.startswith('import ') or l.startswith('from ')]
          == ['import datetime'], str([l for l in src.splitlines() if l.startswith('import ')]))


# ================================================================ c) harness for check_order
FAKE_IND = types.ModuleType('ind')
FAKE_IND.analyze = lambda key, unit, interval, days: {
    'close': 100.0, 'ema20': 90.0, 'dir': 1, 'st': 80.0, 'adx': 30.0, 'hist': 1.0,
    'prev_close': 99.0, 'prev_ema': 90.0, 'prev_dir': 1, 'last_ts': '2026-11-05T10:25:00+05:30'}


def _pos(sym, qty=0, realised=0.0, pnl=0.0, key=None):
    return {'trading_symbol': sym, 'quantity': qty, 'realised': realised, 'pnl': pnl,
            'average_price': 100.0, 'last_price': 100.0, 'product': 'I',
            'instrument_token': key or ('K|' + sym)}


class Harness:
    """Stubs exactly what check_order reads, so a rule can be exercised on any date.

    orders/positions/mtf are returned verbatim; capital sets the funds reply; `fail` is a set of
    paths that raise ReadFailed (the unreadable-book case)."""

    def __init__(self, orders=None, positions=None, mtf=None, capital=1_000_000,
                 fail=(), ltp=None):
        self.orders = orders or []
        self.positions = positions or []
        self.mtf = mtf or []
        self.capital = capital
        self.fail = set(fail)
        self.ltp = ltp
        self._saved = None

    def __enter__(self):
        self._saved = (tl._data, tl.kite_data, tl._kite_err, tl._ltp, tl.lot_size, tl.LF,
                       sys.modules.get('ind'))
        h = self

        def _data(path, strict=False):
            base = path.split('?')[0]
            if base in h.fail:
                raise tl.ReadFailed(f'{base} returned no usable data (test)')
            if base == '/v2/order/retrieve-all':
                return list(h.orders)
            if base == '/v2/portfolio/short-term-positions':
                return list(h.positions)
            if base == '/v3/portfolio/mtf-positions':
                return list(h.mtf)
            if base == '/v2/user/get-funds-and-margin':
                return {'equity': {'available_margin': h.capital, 'used_margin': 0.0}}
            return []

        tl._data = _data
        tl.kite_data = lambda: ([], [])
        tl._kite_err[:] = []
        tl._ltp = lambda order, positions: h.ltp if h.ltp is not None else float(order.get('price') or 0) or None
        tl.lot_size = lambda key, sym='': h._lot(key, sym)
        # a lock file of our own, so a real trade_lock.json can never change a test result
        fd, self.lf = tempfile.mkstemp(suffix='.json'); os.close(fd)
        open(self.lf, 'w').write('{}')      # an empty file is not valid JSON; load() would raise
        tl.LF = self.lf
        sys.modules['ind'] = FAKE_IND
        return self

    def _lot(self, key, sym):
        # the real master, for the two index contracts and one stock option used here
        return {'NSE_FO|39961': 65, 'BSE_FO|1134330': 20}.get(key, 500)

    def __exit__(self, *a):
        (tl._data, tl.kite_data, tl._kite_err, tl._ltp, tl.lot_size, tl.LF, mod) = self._saved
        if mod is None:
            sys.modules.pop('ind', None)
        else:
            sys.modules['ind'] = mod
        try:
            os.unlink(self.lf)
        except OSError:
            pass
        return False


def _filled_index(sym, when, side='BUY', status='complete', broker='upstox'):
    """A completed index-option order at `when` (an IST datetime), as the order book stores it."""
    return {'trading_symbol': sym, 'transaction_type': side, 'status': status,
            'order_timestamp': when.strftime('%Y-%m-%d %H:%M:%S'), 'broker': broker,
            'instrument_token': 'K|' + sym, 'product': 'I', 'order_type': 'LIMIT',
            'quantity': 65, 'average_price': 100.0}


NIFTY = 'NIFTY26N0923950CE'
NIFTY_PE = 'NIFTY26N0923900PE'
SENSEX = 'SENSEX26N0577300CE'


def nifty_order(when, qty=65, side='BUY'):
    return {'trading_symbol': NIFTY, 'instrument_token': 'NSE_FO|39961',
            'transaction_type': side, 'order_type': 'LIMIT', 'product': 'I',
            'quantity': qty, 'price': 100.0, '_now': when.strftime('%Y-%m-%d %H:%M')}


def sensex_order(when, qty=20):
    return {'trading_symbol': SENSEX, 'instrument_token': 'BSE_FO|1134330',
            'transaction_type': 'BUY', 'order_type': 'LIMIT', 'product': 'I',
            'quantity': qty, 'price': 100.0, '_now': when.strftime('%Y-%m-%d %H:%M')}


def stock_order(sym, key, when, qty=500):
    return {'trading_symbol': sym, 'instrument_token': key, 'transaction_type': 'BUY',
            'order_type': 'LIMIT', 'product': 'I', 'quantity': qty, 'price': 100.0,
            '_now': when.strftime('%Y-%m-%d %H:%M')}


def _three_ce_decisions(day):
    """Three completed NIFTY CE decisions spaced 30/20 min apart, ending 30 min before 10:30."""
    base = datetime.datetime.fromisoformat(day + 'T09:00')
    return [_filled_index(NIFTY, base + datetime.timedelta(minutes=m)) for m in (0, 30, 60)]


# ================================================================ d) R1 count
def test_r1_count():
    for day, want_allowed, label in (('2026-11-04', False, 'user rules: 4th decision refused'),
                                     ('2026-11-05', True, 'override: 4th decision allowed'),
                                     ('2026-11-06', True, 'override day 2: 4th decision allowed'),
                                     ('2026-11-09', False, 'rules back: 4th decision refused')):
        when = datetime.datetime.fromisoformat(day + 'T10:30')
        with Harness(orders=_three_ce_decisions(day)) as h:
            ok, reasons, adj = tl.check_order(nifty_order(when))
        got = [r for r in reasons if r.startswith('R1')]
        check(label, ok == want_allowed, f'allowed={ok} reasons={got}')
        if not want_allowed:
            check(f'{label}: says R1', bool(got), str(reasons))
    # a 4th decision on a day with NO override never relies on the override being importable
    check('tl._lim falls back to the constant without the module',
          (lambda: (setattr(tl, '_autonomy', None),
                    tl._lim('INDEX_OPT_MAX', 3, datetime.datetime(2026, 11, 5, 10, 0)),
                    tl._lim('INDEX_OPT_LOSS', 2000, datetime.datetime(2026, 11, 5, 10, 0))))()[1:] == (3, 2000))
    tl._autonomy = A
    check('tl._autonomy restored', tl._autonomy is A)


# ================================================================ e) the 20-min spacing (new)
def test_spacing():
    when = datetime.datetime.fromisoformat('2026-11-05T10:05')
    # last decision 10 minutes ago -> too soon under the override
    orders = [_filled_index(NIFTY, datetime.datetime.fromisoformat('2026-11-05T09:55'))]
    with Harness(orders=orders):
        ok, reasons, _ = tl.check_order(nifty_order(when))
    check('5 Nov: a decision 10 min after the last is refused (<20 min gap)', not ok, str(reasons))
    check('5 Nov: and it names the gap', any('min' in r and r.startswith('R1') for r in reasons), str(reasons))
    # same shape, 25 minutes -> allowed
    orders = [_filled_index(NIFTY, datetime.datetime.fromisoformat('2026-11-05T09:40'))]
    with Harness(orders=orders):
        ok, reasons, _ = tl.check_order(nifty_order(when))
    check('5 Nov: a decision 25 min after the last is allowed', ok, str(reasons))
    # on a user-rules day there is no gap rule at all
    when4 = datetime.datetime.fromisoformat('2026-11-04T10:05')
    orders = [_filled_index(NIFTY, datetime.datetime.fromisoformat('2026-11-04T09:55'))]
    with Harness(orders=orders):
        ok, reasons, _ = tl.check_order(nifty_order(when4))
    check('4 Nov: no 20-min gap rule (user rules unchanged)', ok, str(reasons))


def test_pair_not_counted_twice():
    """A cross-broker mirror leg is ONE decision, so it must not trip the 20-min gap."""
    when = datetime.datetime.fromisoformat('2026-11-05T10:05')
    # the Upstox leg filled at 10:04; the Kite leg is being checked now, 1 minute later
    orders = [_filled_index(NIFTY, datetime.datetime.fromisoformat('2026-11-05T10:04'), broker='upstox')]
    with Harness(orders=orders):
        ok, reasons, _ = tl.check_order(nifty_order(when), broker='kite')
    check('5 Nov: the Kite leg 1 min after its Upstox partner is not blocked by the gap',
          ok, str(reasons))
    # ... but a genuine SECOND decision 1 minute later (same broker) still is
    with Harness(orders=orders):
        ok2, reasons2, _ = tl.check_order(nifty_order(when), broker='upstox')
    check('5 Nov: the same broker 1 min later IS blocked (that is a new decision)', not ok2, str(reasons2))


def test_r1_cap_six():
    """The override raises the count to 6 - and still refuses the 7th."""
    day = '2026-11-05'
    base = datetime.datetime.fromisoformat(day + 'T09:00')
    six = [_filled_index(NIFTY, base + datetime.timedelta(minutes=25 * i)) for i in range(6)]
    when = base + datetime.timedelta(minutes=25 * 6)
    with Harness(orders=six):
        ok, reasons, _ = tl.check_order(nifty_order(when))
    check('5 Nov: the 7th NIFTY decision is refused (cap 6)', not ok, str(reasons))
    check('5 Nov: the refusal states max 6', any('max 6' in r for r in reasons), str(reasons))
    five = six[:5]
    with Harness(orders=five):
        ok2, reasons2, _ = tl.check_order(nifty_order(when))
    check('5 Nov: the 6th NIFTY decision is allowed', ok2, str(reasons2))


# ================================================================ f) expiry-day 13:00 cutoff (new)
def test_expiry_cutoff():
    # SENSEX expires 2026-11-05 (BSE weekly Thursday); NIFTY's next is 9 Nov.
    check('_expiry_today(SENSEX, 5 Nov) is True', tl._expiry_today('SENSEX', datetime.datetime(2026, 11, 5, 9, 0)) is True)
    check('_expiry_today(NIFTY, 5 Nov) is False', tl._expiry_today('NIFTY', datetime.datetime(2026, 11, 5, 9, 0)) is False)
    check('_expiry_today(SENSEX, 6 Nov) is False', tl._expiry_today('SENSEX', datetime.datetime(2026, 11, 6, 9, 0)) is False)
    # after 13:00 on the SENSEX expiry day -> refused
    with Harness():
        ok, reasons, _ = tl.check_order(sensex_order(datetime.datetime(2026, 11, 5, 13, 30)))
    check('5 Nov 13:30 SENSEX (expiry day) refused', not ok, str(reasons))
    check('5 Nov 13:30 SENSEX refusal names the cutoff', any('13:00' in r for r in reasons), str(reasons))
    # before 13:00 on the same day -> allowed
    with Harness():
        ok, reasons, _ = tl.check_order(sensex_order(datetime.datetime(2026, 11, 5, 12, 0)))
    check('5 Nov 12:00 SENSEX (expiry day, before cutoff) allowed', ok, str(reasons))
    # after 13:00 on a day SENSEX is NOT expiring -> allowed
    with Harness():
        ok, reasons, _ = tl.check_order(sensex_order(datetime.datetime(2026, 11, 6, 13, 30)))
    check('6 Nov 13:30 SENSEX (not expiry) allowed', ok, str(reasons))
    # and the cutoff does not exist under the user's rules
    with Harness():
        ok, reasons, _ = tl.check_order(sensex_order(datetime.datetime(2026, 11, 4, 13, 30)))
    check('4 Nov 13:30 SENSEX allowed (no cutoff in the user rules)',
          not any('13:00' in r for r in reasons), str(reasons))


# ================================================================ g) R2a per-index booked loss
def test_booked_loss():
    for day, want_allowed in (('2026-11-04', False), ('2026-11-05', True), ('2026-11-06', True),
                              ('2026-11-09', False)):
        when = datetime.datetime.fromisoformat(day + 'T10:30')
        positions = [_pos('NIFTY26N0923950CE', qty=0, realised=-5000.0, key='NSE_FO|39961')]
        with Harness(positions=positions):
            ok, reasons, _ = tl.check_order(nifty_order(when))
        check(f'booked loss Rs 5,000: allowed={want_allowed} on {day}', ok == want_allowed,
              f'allowed={ok} {[r for r in reasons if r.startswith("R2")]}')
    # 7,000 is past BOTH cutoffs
    for day in ('2026-11-04', '2026-11-05'):
        when = datetime.datetime.fromisoformat(day + 'T10:30')
        positions = [_pos('NIFTY26N0923950CE', qty=0, realised=-7000.0, key='NSE_FO|39961')]
        with Harness(positions=positions):
            ok, reasons, _ = tl.check_order(nifty_order(when))
        check(f'booked loss Rs 7,000 blocked on {day}', not ok, str(reasons))
    # R2a is still per-index: a NIFTY loss must not block BANKNIFTY
    when = datetime.datetime.fromisoformat('2026-11-05T10:30')
    positions = [_pos('NIFTY26N0923950CE', qty=0, realised=-7000.0, key='NSE_FO|39961')]
    bn = {'trading_symbol': 'BANKNIFTY26N2355900CE', 'instrument_token': 'NSE_FO|62181',
          'transaction_type': 'BUY', 'order_type': 'LIMIT', 'product': 'I', 'quantity': 30,
          'price': 100.0, '_now': '2026-11-05 10:30'}
    with Harness(positions=positions):
        ok, reasons, _ = tl.check_order(bn)
    check('a NIFTY loss does not block BANKNIFTY under the override',
          not any('booked loss' in r for r in reasons), str(reasons))


# ================================================================ h) the Rs 30,000 breaker (never overridden)
def test_account_breaker():
    for day in ('2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09'):
        when = datetime.datetime.fromisoformat(day + 'T10:30')
        positions = [_pos('NIFTY26N0923950CE', qty=65, pnl=-35000.0, key='NSE_FO|39961')]
        with Harness(positions=positions):
            ok, reasons, _ = tl.check_order(nifty_order(when))
        check(f'Rs 35,000 day loss still blocks a BUY on {day}', not ok, str(reasons))
        check(f'{day}: and it is R2 that blocks', any(r.startswith('R2') for r in reasons), str(reasons))
    # exactly at the threshold: the rule is `< -THRESHOLD`, so -30,000 does not block, -30,001 does
    when = datetime.datetime.fromisoformat('2026-11-05T10:30')
    with Harness(positions=[_pos('NIFTY26N0923950CE', qty=65, pnl=-30001.0, key='NSE_FO|39961')]):
        ok1, r1, _ = tl.check_order(nifty_order(when))
    with Harness(positions=[_pos('NIFTY26N0923950CE', qty=65, pnl=-29999.0, key='NSE_FO|39961')]):
        ok2, r2, _ = tl.check_order(nifty_order(when))
    check('breaker sits exactly at Rs 30,000 under the override', (not ok1) and ok2, f'{r1} / {r2}')
    check('THRESHOLD is still 30000 in the module', tl.THRESHOLD == 30000, str(tl.THRESHOLD))


# ================================================================ i) R11 stock options
def _fourth_stock_option(day, qty=2000):
    when = datetime.datetime.fromisoformat(day + 'T10:30')
    holds = [_pos('A26NOV100CE', qty=250, key='NSE_FO|1'), _pos('B26NOV100CE', qty=250, key='NSE_FO|2'),
             _pos('C26NOV100CE', qty=250, key='NSE_FO|3')]
    order = stock_order('RELIANCE26NOV1300CE', 'NSE_FO|125998', when, qty=qty)
    return holds, order


def test_r11():
    # 4th underlying
    for day, want_allowed in (('2026-11-04', False), ('2026-11-05', True), ('2026-11-06', True),
                              ('2026-11-09', False)):
        holds, order = _fourth_stock_option(day)
        with Harness(positions=holds):
            ok, reasons, adj = tl.check_order(order)
        check(f'R11 4th stock-option underlying allowed={want_allowed} on {day}', ok == want_allowed,
              f'allowed={ok} {[r for r in reasons if r.startswith("R11")]}')
    # 2 lots per underlying: lot 500, ask 2,000
    for day, want_adj in (('2026-11-04', 500), ('2026-11-05', 1000), ('2026-11-06', 1000),
                          ('2026-11-09', 500)):
        when = datetime.datetime.fromisoformat(day + 'T10:30')
        order = stock_order('RELIANCE26NOV1300CE', 'NSE_FO|125998', when, qty=2000)
        with Harness():
            ok, reasons, adj = tl.check_order(order)
        check(f'R11 {day}: 2,000 asked -> {want_adj} allowed', adj == want_adj,
              f'adj={adj} reasons={[r for r in reasons if r.startswith("R11")]}')
    # already holding 1 lot: a second lot only fits on the override days
    for day, want_adj in (('2026-11-04', 0), ('2026-11-05', 500), ('2026-11-09', 0)):
        when = datetime.datetime.fromisoformat(day + 'T10:30')
        holds = [_pos('RELIANCE26NOV1300CE', qty=500, key='NSE_FO|125998')]
        with Harness(positions=holds):
            ok, reasons, adj = tl.check_order(stock_order('RELIANCE26NOV1300CE', 'NSE_FO|125998', when, qty=500))
        check(f'R11 {day}: holding 1 lot -> {want_adj} more', adj == want_adj,
              f'adj={adj} {[r for r in reasons if r.startswith("R11")]}')


# ================================================================ j) fail-closed reads
def test_fail_closed():
    when = datetime.datetime.fromisoformat('2026-11-05T10:30')
    for path in ('/v2/order/retrieve-all', '/v2/portfolio/short-term-positions', '/v3/portfolio/mtf-positions'):
        with Harness(fail={path}):
            ok, reasons, adj = tl.check_order(nifty_order(when))
        check(f'unreadable {path}: BUY refused under the override', not ok, str(reasons))
        check(f'unreadable {path}: refusal is R0', any(r.startswith('R0') for r in reasons), str(reasons))
    # zero capital
    with Harness(capital=0):
        ok, reasons, adj = tl.check_order(nifty_order(when))
    check('zero capital: BUY refused under the override', not ok, str(reasons))
    # an unreadable Kite book
    with Harness() as h:
        tl._kite_err.append('Kite token invalid (test)')
        ok, reasons, adj = tl.check_order(nifty_order(when))
    check('unreadable Kite book: BUY refused under the override', not ok, str(reasons))
    # the same refusals hold on a user-rules day, proving the override did not touch R0
    when4 = datetime.datetime.fromisoformat('2026-11-04T10:30')
    with Harness(fail={'/v2/order/retrieve-all'}):
        ok4, reasons4, _ = tl.check_order(nifty_order(when4))
    check('unreadable book also refuses on 4 Nov (R0 unchanged)', not ok4, str(reasons4))


# ================================================================ k) MTF caps
def test_mtf_caps():
    st = {'capital': 1_000_000.0, 'held': {}, 'used': 0.0}
    for day, want in (('2026-11-04', 100000.0), ('2026-11-05', 120000.0),
                      ('2026-11-06', 120000.0), ('2026-11-09', 100000.0)):
        lim = ms.limits(st, 'X', 'new', 1.0, when=day + 'T10:00')
        check(f'MTF per-stock cap {want:,.0f} on {day}', lim['per_stock'] == want, str(lim['per_stock']))
        # the 50% total is the same number on every date
        check(f'MTF total room stays 50% on {day}',
              lim['room_total'] == 500000.0, str(lim['room_total']))
    # per_stock_cap() with no argument reads the wall clock, so assert what it must return for
    # whatever day this runs: 0.12 on an override day, 0.10 on every other day. Pinning it to a
    # fixed number would make this suite fail on 5-6 Nov for the wrong reason.
    _today = datetime.datetime.now(A.IST).date().isoformat()
    _want_today = 0.12 if _today in A.DAYS else 0.10
    check(f'mtf_sizing.per_stock_cap() with no argument is {_want_today} on {_today}',
          ms.per_stock_cap() == _want_today, str(ms.per_stock_cap()))
    check('mtf_sizing.per_stock_cap(5 Nov) is 0.12', ms.per_stock_cap('2026-11-05T10:00') == 0.12)
    check('mtf_sizing.total_cap never leaves 0.50',
          ms.total_cap('2026-11-05T10:00') == 0.50 and ms.total_cap('2026-11-09T10:00') == 0.50)
    check('MAX_STOCKS (5 per broker)', ms.MAX_STOCKS == 5, str(ms.MAX_STOCKS))
    # the cap really is the binding number: a 110,000 margin order fits 12% and not 10%
    lim5 = ms.limits(st, 'X', 'new', 1.0, when='2026-11-05T10:00')
    lim9 = ms.limits(st, 'X', 'new', 1.0, when='2026-11-09T10:00')
    check('Rs 1,10,000 of margin room fits on 5 Nov but not on 9 Nov',
          lim5['by_stock'] >= 110000.0 > lim9['by_stock'], f"{lim5['by_stock']} / {lim9['by_stock']}")


# ================================================================ l) the JSON block
def test_rule_set_block():
    on = A.rule_set('2026-11-05T09:05')
    off = A.rule_set('2026-11-09T09:05')
    check('rule_set names the autonomy set when active', 'autonomy' in on['rule_set'], on['rule_set'])
    check('rule_set names the user rules when inactive', on['rule_set'] != off['rule_set'], str(off))
    check('rule_set is JSON-serialisable', bool(json.dumps(on)) and bool(json.dumps(off)))
    check('active block lists what stays unchanged', 'unchanged' in on and len(on['unchanged']) > 20, str(on.get('unchanged')))
    check('inactive block has no override detail', 'unchanged' not in off, str(off))


def main():
    test_active_dates()
    test_override_values()
    test_r1_count()
    test_spacing()
    test_pair_not_counted_twice()
    test_r1_cap_six()
    test_expiry_cutoff()
    test_booked_loss()
    test_account_breaker()
    test_r11()
    test_fail_closed()
    test_mtf_caps()
    test_rule_set_block()
    print(f'\n{CHECKS[0]} checks, {len(FAILS)} failure(s)')
    if FAILS:
        print('ALL FAIL' if len(FAILS) > 5 else 'FAILURES')
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
