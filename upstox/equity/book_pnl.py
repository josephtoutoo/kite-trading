#!/usr/bin/env python3
"""Per-book realised P&L attribution across Upstox + Kite. READ ONLY: only GET/read calls.

Books: index_options, stock_options, mcx, mtf_equity, other_equity, equity_unsplit.
Sources (stated in output):
  Upstox  /v2/trade/profit-loss/data (FY P&L report, matched buy/sell, T+1: up to yesterday)
          + /v2/portfolio/short-term-positions and /v3/portfolio/mtf-positions for TODAY's closed legs
          (report has no product field, so Upstox equity history cannot be split MTF vs delivery
           -> book 'equity_unsplit').
  Kite    positions()/holdings() = today only. Kite history (Console) is not on the API.
Return on capital: options = P&L / premium paid (buy amount). MTF = P&L / margin blocked where
the broker states it; else 'unknown'. Nothing is estimated.
CLI: python3 book_pnl.py [--json] [--since YYYY-MM-DD]
"""
import sys, json, datetime, re, collections
sys.path.insert(0, '/workspace/upstox'); sys.path.insert(0, '/workspace/equity')
import api
from expiry_squareoff import underlying_of, INDEX_UND, COMMODITY_UND

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOW = datetime.datetime.now(IST); TODAY = NOW.date()
SINCE = datetime.date(2026, 10, 1)
if '--since' in sys.argv: SINCE = datetime.date.fromisoformat(sys.argv[sys.argv.index('--since') + 1])
JSON_ONLY = '--json' in sys.argv
UPX_INDEX_ALIAS = {'BSX': 'SENSEX', 'BKX': 'BANKEX', 'NIFTYBANK': 'BANKNIFTY'}
BOOKS = ['index_options', 'stock_options', 'futures', 'mcx', 'mtf_equity', 'other_equity', 'equity_unsplit']
notes = []; sources = collections.defaultdict(list)


def classify_opt(name, exch=''):
    root = underlying_of(name)
    root = UPX_INDEX_ALIAS.get(root, root)
    if exch == 'MCX' or root in COMMODITY_UND: return 'mcx'
    if root in INDEX_UND: return 'index_options'
    return 'stock_options'


def fy_codes(a, b):
    out = []
    y = a.year if a.month >= 4 else a.year - 1
    while datetime.date(y, 4, 1) <= b:
        out.append(f'{y % 100:02d}{(y + 1) % 100:02d}'); y += 1
    return out


def dmy(s): return datetime.datetime.strptime(s, '%d-%m-%Y').date()


trades = []   # dict(book, broker, sym, date, pnl, capital, cap_kind)

# ---------- Upstox history (P&L report) ----------
upx_hist_ok = {}
for seg in ['FO', 'COM', 'EQ']:
    rows = []
    try:
        for fy in fy_codes(datetime.date(2026, 4, 1), TODAY):
            p = 1
            while True:
                d = api.get(f'/v2/trade/profit-loss/data?segment={seg}&financial_year={fy}&page_number={p}&page_size=5000')['data'] or []
                rows += d
                if len(d) < 5000: break
                p += 1
        upx_hist_ok[seg] = True
    except Exception as e:
        upx_hist_ok[seg] = False; notes.append(f'Upstox P&L report {seg} unavailable: {str(e)[:150]}')
        continue
    # aggregate partial-fill rows into one trade per (scrip, buy_date, sell_date)
    agg = collections.OrderedDict()
    for r in rows:
        key = (r['scrip_name'], r['buy_date'], r['sell_date'])
        a = agg.setdefault(key, dict(buy=0.0, sell=0.0, tt=r['trade_type']))
        a['buy'] += float(r['buy_amount'] or 0); a['sell'] += float(r['sell_amount'] or 0)
    for (name, bd, sd), a in agg.items():
        if seg == 'EQ': book, ck = 'equity_unsplit', 'cost (MTF/delivery not separable)'
        elif a['tt'] == 'FUT': book, ck = ('mcx' if seg == 'COM' else 'other_equity'), 'unknown'
        else: book, ck = classify_opt(name, 'MCX' if seg == 'COM' else ''), 'premium'
        if a['tt'] == 'FUT' and seg == 'FO': book = 'futures'; ck = 'unknown (futures margin not in report)'
        trades.append(dict(book=book, broker='upstox', sym=name, date=dmy(sd).isoformat(), open_date=dmy(bd).isoformat(),
                           pnl=round(a['sell'] - a['buy'], 2), capital=(None if ck.startswith('unknown') else a['buy']), cap_kind=ck,
                           src='upstox P&L report (gross, before charges)'))
    sources['upstox'].append(f'/v2/trade/profit-loss/data segment={seg}: {len(rows)} rows')

# ---------- Upstox today (positions) ----------
open_now = collections.defaultdict(lambda: dict(cost=0.0, margin=0.0, margin_known=True, n=0))
try:
    for x in api.get('/v2/portfolio/short-term-positions')['data'] or []:
        sym = x['trading_symbol']; exch = x['exchange']
        is_opt = sym.endswith(('CE', 'PE'))
        book = classify_opt(sym, exch) if (is_opt or exch == 'MCX') else 'other_equity'
        sq = int(x.get('day_sell_quantity') or 0) + int(x.get('overnight_sell_quantity') or 0)
        if sq > 0 and float(x.get('realised') or 0) != 0 or (int(x['quantity']) == 0 and sq > 0):
            cap = float(x.get('day_buy_value') or 0) + float(x.get('overnight_buy_amount') or 0)
            trades.append(dict(book=book, broker='upstox', sym=sym, date=TODAY.isoformat(), open_date=None,
                               pnl=round(float(x.get('realised') or 0), 2), capital=cap or None,
                               cap_kind='premium' if is_opt else 'unknown', src='upstox short-term-positions (today)'))
        if int(x['quantity']) != 0:
            o = open_now[book]; o['n'] += 1; o['cost'] += abs(int(x['quantity'])) * float(x['average_price'] or 0)
            if is_opt and int(x['quantity']) > 0: o['margin'] += int(x['quantity']) * float(x['average_price'] or 0)
            else: o['margin_known'] = False
    sources['upstox'].append('/v2/portfolio/short-term-positions (today closed + open)')
except Exception as e: notes.append(f'Upstox positions unavailable: {str(e)[:150]}')
try:
    for x in api.get('/v3/portfolio/mtf-positions')['data'] or []:
        if int(x.get('day_sell_quantity') or 0) > 0:
            trades.append(dict(book='mtf_equity', broker='upstox', sym=x['trading_symbol'], date=TODAY.isoformat(), open_date=None,
                               pnl=round(float(x.get('realised') or 0), 2), capital=None, cap_kind='unknown (margin not reported)',
                               src='upstox v3 mtf-positions (today)'))
        q = int(x.get('quantity') or 0)
        if q > 0:
            o = open_now['mtf_equity']; o['n'] += 1; o['cost'] += q * float(x.get('average_price') or 0); o['margin_known'] = False
    sources['upstox'].append('/v3/portfolio/mtf-positions (today closed + open; margin per position not reported)')
except Exception as e: notes.append(f'Upstox MTF positions unavailable: {str(e)[:150]}')
try:
    f = api.get('/v2/user/get-funds-and-margin')['data']['equity']
    upx_funds = dict(available=float(f['available_margin']), used=float(f['used_margin']))
except Exception as e: upx_funds = None; notes.append(f'Upstox funds unavailable: {e}')

# ---------- Kite (today only) ----------
kite_funds = None
try:
    import broker_kite as bk
    if not bk.valid(): raise RuntimeError('kite token invalid: ' + str(bk.why()))
    k = bk.client()
    for p in k.positions()['net']:
        sym, exch, prod = p['tradingsymbol'], p['exchange'], p['product']
        is_opt = exch in ('NFO', 'BFO', 'MCX') and sym.endswith(('CE', 'PE'))
        if is_opt or exch == 'MCX': book = classify_opt(sym, exch)
        elif prod == 'MTF': book = 'mtf_equity'
        else: book = 'other_equity'
        if p['quantity'] == 0 and (p['sell_quantity'] or p['buy_quantity']):
            cap = float(p['buy_value']) if is_opt else None
            trades.append(dict(book=book, broker='kite', sym=sym, date=TODAY.isoformat(), open_date=None,
                               pnl=round(float(p['pnl']), 2), capital=cap or None,
                               cap_kind='premium' if is_opt else 'unknown', src='kite positions (today)'))
        elif p['quantity'] != 0:
            o = open_now[book]; o['n'] += 1; o['cost'] += abs(p['quantity']) * float(p['average_price'])
            if is_opt and p['quantity'] > 0: o['margin'] += p['quantity'] * float(p['average_price'])
            else: o['margin_known'] = False
    for h in k.holdings():
        m = h.get('mtf') or {}
        if int(m.get('quantity') or 0) > 0:
            o = open_now['mtf_equity']; o['n'] += 1; o['cost'] += float(m.get('value') or 0); o['margin'] += float(m.get('initial_margin') or 0)
    mg = k.margins('equity'); kite_funds = dict(net=float(mg['net']), debits=float(mg['utilised']['debits']),
                                                option_premium_today=float(mg['utilised'].get('option_premium') or 0))
    sources['kite'].append('positions() today, holdings() mtf block (initial_margin), margins(); NO history before today on API')
except Exception as e: notes.append(f'Kite unavailable: {str(e)[:150]}')
notes.append('Kite: only today\'s (%s) closed positions are visible; Kite history before today is NOT included.' % TODAY)
notes.append('Upstox P&L report is T+1: history up to the previous trading day; today comes from live positions.')
notes.append('Upstox equity history (book equity_unsplit) has no product field, so MTF vs delivery cannot be separated; only today\'s Upstox MTF closes are tagged mtf_equity.')
notes.append('All P&L is gross (before brokerage/STT/charges). Open MTF margin on Upstox is not reported per position -> unknown.')
notes.append('A "trade" = one scrip with the same buy date and sell date (partial fills merged).')


def stats(ts):
    n = len(ts)
    if not n: return dict(trades=0)
    p = [t['pnl'] for t in ts]; w = sum(1 for x in p if x > 0); l = sum(1 for x in p if x < 0)
    caps = [t['capital'] for t in ts if t['capital']]
    capd = all(t['capital'] for t in ts)
    total = round(sum(p), 2)
    days = collections.defaultdict(float)
    for t in ts:
        if t['capital']: days[t['date']] += t['capital']
    best = max(ts, key=lambda t: t['pnl']); worst = min(ts, key=lambda t: t['pnl'])
    return dict(trades=n, wins=w, losses=l, flat=n - w - l, win_rate=f'{100 * w / n:.0f}% (n={n})',
                win_rate_pct=round(100 * w / n, 1), realised_pnl=total, avg_pnl=round(total / n, 2),
                best=dict(sym=best['sym'], date=best['date'], pnl=best['pnl']),
                worst=dict(sym=worst['sym'], date=worst['date'], pnl=worst['pnl']),
                capital_kind=sorted({t['cap_kind'] for t in ts}),
                capital_deployed=round(sum(caps), 2) if capd else 'unknown',
                return_on_capital_pct=round(100 * total / sum(caps), 2) if capd and caps else 'unknown',
                # when some trades lack a denominator: RoC over only the trades that have one
                capital_known_trades=f'{len(caps)}/{n}',
                roc_known_subset_pct=(round(100 * sum(t['pnl'] for t in ts if t['capital']) / sum(caps), 2) if caps else 'unknown'),
                unknown_capital_trades=[(t['sym'], t['date'], t['pnl']) for t in ts if not t['capital']],
                avg_capital_per_trade=round(sum(caps) / len(caps), 2) if caps else 'unknown',
                avg_capital_per_active_day=round(sum(days.values()) / len(days), 2) if capd and days else 'unknown',
                active_days=len({t['date'] for t in ts}),
                first=min(t['date'] for t in ts), last=max(t['date'] for t in ts),
                small_sample=n < 20)


def window(ts, a): return [t for t in ts if t['date'] >= a.isoformat()]


res = dict(as_of=NOW.isoformat(timespec='seconds'),
           window=dict(start=SINCE.isoformat(), end=TODAY.isoformat(),
                       note='Upstox: full window via P&L report; Kite: today only'),
           read_only=True, sources=dict(sources), notes=notes, funds=dict(upstox=upx_funds, kite=kite_funds), books={})
for b in BOOKS:
    bt = [t for t in trades if t['book'] == b]
    o = open_now.get(b)
    res['books'][b] = dict(
        window=stats(window(bt, SINCE)),
        since_inception=stats(bt),
        by_broker={br: stats([t for t in window(bt, SINCE) if t['broker'] == br]) for br in ('upstox', 'kite')},
        open_now=(dict(positions=o['n'], cost=round(o['cost'], 2),
                       capital_blocked=round(o['margin'], 2) if o['margin_known'] else
                       (f"partial: {round(o['margin'], 2)} known + unknown" if o['margin'] else 'unknown')) if o else dict(positions=0)))
res['since_inception_note'] = 'since_inception = earliest row the Upstox P&L report returns (FY2026-27 from 1 Apr 2026) + Kite today'
json.dump(res, open('/workspace/equity/book_pnl.json', 'w'), indent=1, default=str)

if JSON_ONLY:
    print(json.dumps(res, indent=1, default=str)); sys.exit()
print(f"Book P&L  as of {res['as_of']}  window {SINCE} .. {TODAY}  (gross, before charges)\n")
for lbl, key in (('WINDOW', 'window'), ('SINCE INCEPTION (data available)', 'since_inception')):
    print(f'== {lbl} ==')
    print(f"{'book':15}{'n':>5}{'W':>5}{'L':>5}{'win%':>7}{'P&L ₹':>12}{'avg ₹':>10}{'capital ₹':>13}{'RoC %':>9}  range")
    for b in BOOKS:
        s = res['books'][b][key]
        if not s['trades']: print(f'{b:15}{0:>5}   no closed trades'); continue
        print(f"{b:15}{s['trades']:>5}{s['wins']:>5}{s['losses']:>5}{s['win_rate_pct']:>7}{s['realised_pnl']:>12,.0f}{s['avg_pnl']:>10,.0f}"
              f"{str(s['capital_deployed']):>13}{str(s['return_on_capital_pct']):>9}  {s['first']}..{s['last']}{'  SMALL n' if s['small_sample'] else ''}")
    print()
print('== best / worst (window) ==')
for b in BOOKS:
    s = res['books'][b]['window']
    if s['trades']: print(f"{b:15} best {s['best']['sym']} {s['best']['pnl']:,.0f} ({s['best']['date']})   worst {s['worst']['sym']} {s['worst']['pnl']:,.0f} ({s['worst']['date']})   avg cap/trade {s['avg_capital_per_trade']}  avg cap/active day {s['avg_capital_per_active_day']}")
print('\n== trades without a capital denominator (RoC on the rest) ==')
for b in BOOKS:
    s = res['books'][b]['window']
    if s['trades'] and s['unknown_capital_trades']: print(f"{b:15} known {s['capital_known_trades']}, RoC on known {s['roc_known_subset_pct']}%  missing: {s['unknown_capital_trades']}")
print('\n== tied up now ==')
for b in BOOKS: print(f"{b:15} {res['books'][b]['open_now']}")
print('\nfunds:', res['funds'])
print('\nsources:'); [print(' ', k, v) for k, v in res['sources'].items()]
print('notes:'); [print(' -', n) for n in notes]
print('\nwrote /workspace/equity/book_pnl.json')
