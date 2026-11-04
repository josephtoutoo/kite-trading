#!/usr/bin/env python3
"""Tests for book_sizing.py (4 Nov 2026, t336u). No network, no broker, no orders.

Run: python3 book_sizing_test.py          (exit 0 = all pass)
     python3 book_sizing_test.py -v       (print each check)

Every test states the property it protects. These are the properties that stop a sizing bug from
turning into a real, oversized order: never a negative size, never a size the capital cannot fund,
never a weight outside the clamp, never fewer lots for better conditions.
"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import book_sizing as bs

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


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def tmp_pnl(obj):
    f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
    if isinstance(obj, str):
        f.write(obj)
    else:
        json.dump(obj, f)
    f.close()
    return f.name


# ---------------------------------------------------------------- a) free capital guard
def test_free_capital():
    for cap in (0, -1, -250000):
        lots, note = bs.index_option_lots(150, 50, cap, 26, True, False, 1 / 3)
        check(f'zero/negative capital {cap} -> 0 lots', lots == 0, f'got {lots}')
        check(f'zero/negative capital {cap} -> note says so', 'free capital' in note.lower(), note)
    lots, note = bs.stock_option_lots(50, 500, 0, 1 / 3, when='2026-11-09T10:00')
    check('stock option zero capital -> 0 lots', lots == 0, f'got {lots}')
    q, note = bs.mtf_qty('X', 100.0, 'upstox', 0, 1 / 3)
    check('mtf zero capital -> 0 shares', q == 0, f'got {q}')
    # a missing ADX must not raise either
    lots, note = bs.index_option_lots(150, 50, 100000, None, True, False, 1 / 3)
    check('missing ADX -> 0 lots + a note (never raises)', lots == 0 and 'ADX' in note, f'{lots} {note}')
    lots, note = bs.index_option_lots(150, 50, 100000, 'n/a', True, False, 1 / 3)
    check('non-numeric ADX -> 0 lots + a note', lots == 0 and note, f'{lots} {note}')
    # a non-numeric capital must not raise - it must degrade
    lots, note = bs.index_option_lots(150, 50, 'n/a', 26, True, False, 1 / 3)
    check('non-numeric capital -> 0 lots + a note', lots == 0 and note, f'{lots} {note}')


# ---------------------------------------------------------------- b) premium too expensive
def test_unaffordable_lot():
    # 1 lot costs 100000 x 25 = 2,500,000 against a 30% premium cap of 0.3 x 100000 x 1.0 = 30,000
    lots, note = bs.index_option_lots(100000, 25, 100000, 30, True, False, 1 / 3)
    check('premium too expensive -> 0 lots', lots == 0, f'got {lots}')
    check('premium too expensive -> note says cannot afford 1 lot',
          'cannot afford 1 lot' in note, note)
    lots, note = bs.stock_option_lots(100000, 25, 100000, 1 / 3, when='2026-11-09T10:00')
    check('stock option premium too expensive -> 0 lots', lots == 0, f'got {lots}')
    check('stock option note names the premium cap', 'premium cap' in note, note)


# ---------------------------------------------------------------- c) 10x capital stays bounded
def test_capital_scaling():
    base = dict(premium=150.0, lot_size=50, adx=30, daily_aligned=True, expire_today=True, weight=1 / 3)
    lo, nlo = bs.index_option_lots(free_capital=50000, **base)
    hi, nhi = bs.index_option_lots(free_capital=500000, **base)
    check('10x capital does not exceed MAX_LOTS', hi <= bs.MAX_LOTS, f'got {hi}')
    check('10x capital does increase size (it is not a constant)', hi > lo, f'{lo} -> {hi}')
    # premium cap: outlay per lot x lots must stay inside 30% of free capital x weight scale
    outlay = hi * base['premium'] * base['lot_size']
    cap = 500000 * bs.PREMIUM_CAP * (base['weight'] / (1 / 3))
    check('premium outlay inside the 30% cap', outlay <= cap + 1e-6, f'{outlay} vs {cap}')
    # absurd weight / capital must not explode either
    lots, note = bs.index_option_lots(1.0, 1, 10_000_000, 40, True, True, 0.50)
    check('tiny premium + huge capital still <= MAX_LOTS', lots <= bs.MAX_LOTS, f'got {lots}')
    # exactly at MAX_LOTS it says it was capped
    if lots == bs.MAX_LOTS:
        check('MAX_LOTS note is present when capped', 'MAX_LOTS' in note, note)


# ---------------------------------------------------------------- d) thin / missing book_pnl
def test_weights_thin_and_missing():
    eq = 1.0 / 3

    w = bs.book_weights('/nonexistent/definitely/not/here.json')
    check('missing file -> equal weights',
          all(close(w[b], eq, 1e-4) for b in bs.BOOKS), str({b: w[b] for b in bs.BOOKS}))
    check('missing file -> note explains it', bool(w.get('_note')), w.get('_note'))

    p = tmp_pnl('{this is not json')
    w = bs.book_weights(p)
    check('unreadable json -> equal weights',
          all(close(w[b], eq, 1e-4) for b in bs.BOOKS), str({b: w[b] for b in bs.BOOKS}))
    os.unlink(p)

    # a spectacular 3-trade record must NOT take the budget
    p = tmp_pnl({'books': {
        'index_options': {'trades': 3, 'pnl': 900000, 'capital': 100000},
        'stock_options': {'trades': 2, 'pnl': -500, 'capital': 50000},
        'mtf': {'trades': 1, 'pnl': -100, 'capital': 20000}}})
    w = bs.book_weights(p)
    check('thin sample with a huge return is shrunk, not followed',
          w['index_options'] < 0.50, f"index_options {w['index_options']}")
    check('thin sample still inside the clamp',
          all(bs.W_MIN - 1e-9 <= w[b] <= bs.W_MAX + 1e-9 for b in bs.BOOKS), str(w))
    check('thin sample -> note names the thin book', 'shrunk' in (w.get('_note') or ''),
          w.get('_note'))
    os.unlink(p)

    # 10 trades are enough to be taken at face value: a 3x better book should out-weigh the rest
    p = tmp_pnl({'index_options': {'trades': 40, 'pnl': 60000, 'capital': 200000},
                 'stock_options': {'trades': 30, 'pnl': 5000, 'capital': 100000},
                 'mtf': {'trades': 30, 'pnl': 2000, 'capital': 100000}})
    w = bs.book_weights(p)
    check('a proven better book gets the larger weight',
          w['index_options'] > w['stock_options'] and w['index_options'] > w['mtf'], str(w))
    os.unlink(p)

    # all books losing -> equal, stated
    p = tmp_pnl({'books': {b: {'trades': 20, 'pnl': -1000, 'capital': 50000} for b in bs.BOOKS}})
    w = bs.book_weights(p)
    check('every book losing -> equal weights',
          all(close(w[b], eq, 1e-4) for b in bs.BOOKS), str(w))
    os.unlink(p)

    # empty object
    p = tmp_pnl({})
    w = bs.book_weights(p)
    check('empty json -> equal weights', all(close(w[b], eq, 1e-4) for b in bs.BOOKS), str(w))
    os.unlink(p)

    # the shape the sibling book-P&L report actually writes (4 Nov 2026): numbers nested under
    # 'window', 'realised_pnl', 'capital_deployed', and the MTF book named mtf_equity
    p = tmp_pnl({'as_of': '2026-11-04T23:27:45+05:30', 'read_only': True, 'books': {
        'index_options': {'window': {'trades': 44, 'realised_pnl': -26501.25,
                                     'capital_deployed': 3251907.0, 'return_on_capital_pct': -0.81},
                          'since_inception': {'trades': 139, 'realised_pnl': -65299.0,
                                              'capital_deployed': 8871955.5}},
        'stock_options': {'window': {'trades': 28, 'realised_pnl': 110177.5,
                                     'capital_deployed': 1396812.5, 'return_on_capital_pct': 7.89}},
        'mtf_equity': {'window': {'trades': 2, 'realised_pnl': -491.52}},
        'futures': {'window': {'trades': 1, 'realised_pnl': 53640.0}}}})
    w = bs.book_weights(p)
    check('nested book-report shape is read (mtf_equity maps to mtf)',
          w['_trades'].get('mtf') == 2, str(w['_trades']))
    check('the better-earning book gets the largest weight',
          w['stock_options'] > w['index_options'] and w['stock_options'] >= w['mtf'], str(w))
    check('a book with no capital figure contributes no evidence, keeps its equal share',
          w['mtf'] > w['index_options'], f"mtf {w['mtf']} vs index {w['index_options']}")
    check('nested shape still inside the clamp',
          all(bs.W_MIN - 1e-9 <= w[b] <= bs.W_MAX + 1e-9 for b in bs.BOOKS), str(w))
    os.unlink(p)

    # a shape we do not understand must not crash
    for bad in ([], {'books': 'nope'}, {'index_options': 'nope'}, {'index_options': [{'pnl': 'x'}]}):
        p = tmp_pnl(bad)
        try:
            w = bs.book_weights(p)
            ok = all(bs.W_MIN - 1e-9 <= w[b] <= bs.W_MAX + 1e-9 for b in bs.BOOKS)
            check(f'odd shape {bad!r} does not crash and stays in clamp', ok, str(w))
        except Exception as e:
            check(f'odd shape {bad!r} does not crash', False, f'{type(e).__name__}: {e}')
        os.unlink(p)


# ---------------------------------------------------------------- e) weight clamp
def test_weight_clamp():
    cases = [
        {'index_options': {'trades': 500, 'pnl': 10_000_000, 'capital': 1000},
         'stock_options': {'trades': 500, 'pnl': -10_000, 'capital': 1000},
         'mtf': {'trades': 500, 'pnl': -10_000, 'capital': 1000}},
        {'index_options': {'trades': 50, 'pnl': 1, 'capital': 100},
         'stock_options': {'trades': 50, 'pnl': 1, 'capital': 100},
         'mtf': {'trades': 50, 'pnl': 0, 'capital': 100}},
        {'index_options': {'trades': 400, 'pnl': -1, 'capital': 1},
         'stock_options': {'trades': 400, 'pnl': 1, 'capital': 1},
         'mtf': {'trades': 400, 'pnl': 1, 'capital': 1}},
        {'index_options': {'trades': 400, 'pnl': 0, 'capital': 1},
         'stock_options': {'trades': 0, 'pnl': 0, 'capital': 1},
         'mtf': {'trades': 0, 'pnl': 0, 'capital': 0}},
    ]
    for c in cases:
        p = tmp_pnl(c)
        w = bs.book_weights(p)
        ok = all(bs.W_MIN - 1e-9 <= w[b] <= bs.W_MAX + 1e-9 for b in bs.BOOKS)
        check('weights never outside 0.15-0.50', ok, str({b: w[b] for b in bs.BOOKS}))
        check('weights sum to 1', close(sum(w[b] for b in bs.BOOKS), 1.0, 1e-6), str(sum(w[b] for b in bs.BOOKS)))
        os.unlink(p)
    # and the clamp constants are what the brief asked for
    check('clamp constants are 0.15 / 0.50', (bs.W_MIN, bs.W_MAX) == (0.15, 0.50), str((bs.W_MIN, bs.W_MAX)))


# ---------------------------------------------------------------- f) ADX tiers monotonic
def test_adx_monotonic():
    tiers = [(17.0, False), (19.0, False), (22.0, False), (26.0, False), (26.0, True), (35.0, True)]
    prem, lot, cap, wgt = 150.0, 50, 400000, 1 / 3
    lots = [bs.index_option_lots(prem, lot, cap, a, d, False, wgt)[0] for a, d in tiers]
    check('ADX tiers monotonic (never fewer lots for a better trend)',
          all(b >= a for a, b in zip(lots, lots[1:])), f'{tiers} -> {lots}')
    # expiry day must never reduce size relative to the same non-expiry trade
    base = bs.index_option_lots(prem, lot, cap, 22.0, True, False, wgt)[0]
    exp_ = bs.index_option_lots(prem, lot, cap, 22.0, True, True, wgt)[0]
    check('expiry day never sizes down', exp_ >= base, f'{base} -> {exp_}')
    # daily alignment never sizes down at a high ADX
    al = bs.index_option_lots(prem, lot, cap, 26.0, True, False, wgt)[0]
    na = bs.index_option_lots(prem, lot, cap, 26.0, False, False, wgt)[0]
    check('daily alignment never sizes down', al >= na, f'{na} unaligned -> {al} aligned')
    # conviction tiers are strictly ordered by the multiplier
    mults = [bs._conviction(a, d)[0] for a, d in tiers]
    check('conviction multipliers non-decreasing', all(b >= a for a, b in zip(mults, mults[1:])), str(mults))
    # a weight below 1/3 must never size UP, and above 1/3 never DOWN
    lo = bs.index_option_lots(prem, lot, cap, 22.0, True, False, 0.15)[0]
    eq = bs.index_option_lots(prem, lot, cap, 22.0, True, False, 1 / 3)[0]
    hi = bs.index_option_lots(prem, lot, cap, 22.0, True, False, 0.50)[0]
    check('weight ordering: 0.15 <= 1/3 <= 0.50 in lots', lo <= eq <= hi, f'{lo} {eq} {hi}')


# ---------------------------------------------------------------- g) one-lot floor and R11
def test_single_lot_and_r11():
    # risk budget rounds to zero but one lot fits the premium cap -> 1 lot, not 0
    # 20000 x 1.5% = 300 budget; one lot risks 20 x 0.30 x 50 = 300 -> floor(300/300)=1. Make it smaller.
    lots, note = bs.index_option_lots(10.0, 50, 20000, 18.0, False, False, 1 / 3)
    check('small budget still returns at least 1 lot when affordable', lots == 1, f'{lots} {note}')
    check('the 1-lot floor is explained', '1 lot' in note, note)
    # stock options are hard-capped at 1 lot by R11 whatever the capital says.
    # The cap is date-aware since t339u (2 lots on 5-6 Nov 2026 only), so pin the date: these
    # assertions are about the USER's rule set, and must not change on the two override days.
    _user_day = '2026-11-09T10:00'
    lots, note = bs.stock_option_lots(10.0, 50, 10_000_000, 0.50, when=_user_day)
    check('stock option never exceeds 1 lot (R11)', lots == 1, f'{lots}')
    check('stock option note names R11', 'R11' in note, note)
    check('STOCK_OPT_MAX_LOTS is 1', bs.STOCK_OPT_MAX_LOTS == 1, str(bs.STOCK_OPT_MAX_LOTS))
    lots, _ = bs.stock_option_lots(10.0, 50, 10_000_000, 0.50, when='2026-11-05T10:00')
    check('R11 cap is 2 lots on the 5-6 Nov override days only', lots == 2, f'{lots}')


# ---------------------------------------------------------------- h) explain() + defaults
def test_explain():
    idx = {'NIFTY': dict(premium=146.6, lot_size=65, adx=26.08, daily_aligned=True, expire_today=False),
           'BANKNIFTY': dict(premium=756.0, lot_size=30, adx=20.38, daily_aligned=False, expire_today=False),
           'SENSEX': dict(premium=288.85, lot_size=20, adx=21.58, daily_aligned=True, expire_today=False)}
    d = bs.explain(354071, idx)
    check('explain returns all three books in weights', set(d['weights']) == set(bs.BOOKS), str(d['weights']))
    check('explain returns all three indexes', set(d['indexes']) == set(idx), str(list(d['indexes'])))
    for n in idx:
        r = d['indexes'][n]
        check(f'explain {n} has a note', bool(r['note']), str(r))
        check(f'explain {n} lots is an int >= 0', isinstance(r['lots'], int) and r['lots'] >= 0, str(r['lots']))
        if r['lots']:
            check(f'explain {n} outlay = lots x premium x lot',
                  close(r['premium_outlay'], r['lots'] * idx[n]['premium'] * idx[n]['lot_size'], 1),
                  str(r))
            check(f'explain {n} max loss at stop = 30% of outlay',
                  close(r['max_loss_at_stop'], r['premium_outlay'] * 0.30, 1), str(r))
    # an index with no quote must degrade to a note, not a crash
    d = bs.explain(100000, {'NIFTY': dict(premium=None, lot_size=65, adx=22, daily_aligned=True,
                                          expire_today=False)})
    check('missing premium -> 0 lots + note', d['indexes']['NIFTY']['lots'] == 0 and
          d['indexes']['NIFTY']['note'], str(d['indexes']['NIFTY']))
    # no indexes at all
    d = bs.explain(100000, {})
    check('explain with no indexes still returns weights', set(d['weights']) == set(bs.BOOKS), str(d))


# ---------------------------------------------------------------- i) never places anything
def test_no_side_effects():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'book_sizing.py')).read()
    for bad in ('/v2/order/place', 'api-hft', 'place(', 'order/place'):
        check(f'book_sizing.py contains no order call: {bad}', bad not in src.split('def _live')[0],
              'found in the library section')


if __name__ == '__main__':
    for fn in (test_free_capital, test_unaffordable_lot, test_capital_scaling,
               test_weights_thin_and_missing, test_weight_clamp, test_adx_monotonic,
               test_single_lot_and_r11, test_explain, test_no_side_effects):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILS.append(f'{fn.__name__} raised {type(e).__name__}: {e}')
    print(f'\n{CHECKS[0]} checks, {len(FAILS)} failure(s)')
    if FAILS:
        for f in FAILS:
            print(' - ' + f)
        sys.exit(1)
    print('ALL PASS')
