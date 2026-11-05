#!/usr/bin/env python3
"""Exit results_entry stock options on the trading day BEFORE expected results.

Calendar: ROOT/stockopt_calendar.json (copy the committed .example to deploy).
Only long stock options are considered. They must be marked results_entry in
risk_state.json (or risk_seeds.json) or have a completed BUY tagged results_entry.
At/after 15:10 IST on the due date, live sell stops are cancelled before a LIMIT
SELL near LTP is sent on the same broker. --dry-run never cancels or sells.

TATASTEEL's expected date is Sat 2026-11-07, so its due session is Fri 6 Nov.
Schedule every 5 minutes from 15:10 through 15:20 IST; repeat runs see flat books.
"""
import argparse, datetime as dt, json, os, sys, urllib.parse
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import api, broker_kite as bk, trade_lock as tl
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TAG = 'results_exit'


def previous_trading_day(day):
    import market_calendar
    day -= dt.timedelta(days=1)
    while not market_calendar.is_trading_day(day): day -= dt.timedelta(days=1)
    return day


def calendar(path):
    with open(path) as f: raw = json.load(f)
    return {u.upper(): {'results_date': dt.date.fromisoformat(v['expected_results_date']),
                        'exit_date': previous_trading_day(dt.date.fromisoformat(v['expected_results_date']))}
            for u, v in raw.items()}


def sources(state, seeds, orders, kite_orders):
    out = set()
    for data in (state, seeds):
        for k, v in data.items():
            if isinstance(v, dict) and v.get('source') == 'results_entry':
                out.add((v.get('sym') or v.get('trading_symbol') or '').upper())
    for o in list(orders) + list(kite_orders):
        if o.get('tag') == 'results_entry' and o.get('transaction_type') == 'BUY' and o.get('status') == 'complete':
            out.add(tl.sym_of(o))
    return out


def ltp_upstox(key, fallback=0):
    try:
        d = api.get('/v2/market-quote/ltp?instrument_key=' + urllib.parse.quote(key, safe='')).get('data') or {}
        return float(next(iter(d.values())).get('last_price') or fallback)
    except Exception: return float(fallback or 0)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--calendar', default=os.path.join(ROOT, 'stockopt_calendar.json'))
    ap.add_argument('--now', help='YYYY-MM-DD HH:MM, for smoke tests')
    args = ap.parse_args(argv)
    now = dt.datetime.strptime(args.now, '%Y-%m-%d %H:%M').replace(tzinfo=IST) if args.now else dt.datetime.now(IST)
    cal = calendar(args.calendar)
    def read(name):
        p = os.path.join(ROOT, name)
        try:
            with open(p) as f: return json.load(f)
        except (OSError, ValueError): return {}
    orders = api.get('/v2/order/retrieve-all').get('data') or []
    korders = bk.orders(strict=True)
    marked = sources(read('risk_state.json'), read('risk_seeds.json'), orders, korders)
    upos = api.get('/v2/portfolio/short-term-positions').get('data') or []
    kpos = bk.positions(strict=True)
    out = {'now': now.isoformat(), 'dry_run': args.dry_run,
           'calendar': {u: {k: d.isoformat() for k, d in v.items()} for u, v in cal.items()}, 'actions': []}
    for broker, positions, all_orders in (('upstox', upos, orders), ('kite', kpos, korders)):
        for p in positions:
            sym = tl.sym_of(p); qty = int(p.get('quantity') or 0); u = tl.underlying(sym)
            if qty <= 0 or u not in cal or not tl.is_opt(sym) or tl.is_index(sym): continue
            if sym not in marked:
                out['actions'].append({'broker': broker, 'symbol': sym, 'skip': 'not marked/tagged results_entry'}); continue
            due = cal[u]['exit_date']
            item = {'broker': broker, 'symbol': sym, 'qty': qty, 'results_date': cal[u]['results_date'].isoformat(),
                    'due_date': due.isoformat()}
            if now.date() != due or now.time() < dt.time(15, 10):
                item['skip'] = 'not yet 15:10 IST on the pre-results trading day'
                out['actions'].append(item); continue
            key = p.get('instrument_token') if broker == 'upstox' else bk.to_upstox(p['exchange'], sym)
            price = ltp_upstox(key, p.get('last_price')) if key else float(p.get('last_price') or 0)
            if not price:
                item['error'] = 'no LTP; no exit sent'; out['actions'].append(item); continue
            limit = round(price * 0.995, 2)
            stops = [o for o in all_orders if tl.sym_of(o) == sym and o.get('transaction_type') == 'SELL'
                     and o.get('status') in ('open', 'trigger pending') and o.get('order_type') in ('SL', 'SL-M')]
            if args.dry_run:
                item.update(action='DRY-RUN: cancel protective stops then SELL', stop_order_ids=[o['order_id'] for o in stops], limit=limit)
            else:
                item['cancelled_stops'] = []
                for o in stops:
                    if broker == 'upstox':
                        r = api.call('https://api-hft.upstox.com/v2/order/cancel?order_id=' + o['order_id'], None, 'DELETE', proxy=True)
                        item['cancelled_stops'].append({o['order_id']: r.get('status')})
                    else: item['cancelled_stops'].append({o['order_id']: bk.cancel(o['order_id'])})
                if broker == 'upstox':
                    body = {'quantity': qty, 'product': p['product'], 'validity': 'DAY', 'price': limit, 'tag': TAG,
                            'instrument_token': p['instrument_token'], 'order_type': 'LIMIT', 'transaction_type': 'SELL',
                            'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}
                    r = api.call('https://api-hft.upstox.com/v2/order/place', body, 'POST', proxy=True)
                    item['order'] = (r.get('data') or {}).get('order_id') or r
                else: item['order'] = bk.place(p['exchange'], sym, 'SELL', qty, limit, tag=TAG, product=p['product'])
            out['actions'].append(item)
    print(json.dumps(out, indent=2, default=str))
    return out


if __name__ == '__main__':
    main()
