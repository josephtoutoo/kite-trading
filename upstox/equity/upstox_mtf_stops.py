#!/usr/bin/env python3
"""Upstox MTF protective stops (5 Nov 2026) - the Upstox counterpart of kite_mtf_stops.py.

For every Upstox MTF position with quantity > 0:
  stop = 5% below LTP, or the 1-hour Supertrend level when that is NEARER to price.
  Never above price; never lower than a stop already recorded for that name (risk is only
  ever narrowed, never widened); snapped to the Upstox tick grid (Rs 0.10 at/above Rs 1000,
  else Rs 0.05 - the API rejects anything else).
If a live SL SELL already covers the full quantity at the right level the name is left alone
(idempotent). Otherwise that name's stale live SL SELLs are cancelled and one fresh SL SELL is
placed at the computed level, tag 'trailsl', product MTF - after trade_lock.check_order.

Recorded stops live in mtf_stops_upstox.json ({"stops": {SYM: level}}); a live SL trigger is
also treated as a floor, so a crashed state file cannot widen a stop.

CLI: python3 upstox_mtf_stops.py [--dry-run] [--pct 5] [--at HH:MM]
  --dry-run  print every action, place/cancel nothing, write no state
  --at       evaluate trade_lock at HH:MM today instead of now (out-of-hours preview only)
"""
import sys, os, json, datetime, urllib.parse

sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, '/workspace/equity')
import api                     # pooled, proxied, timed client (5 Nov 2026)
D = os.path.dirname(os.path.abspath(__file__))
STOPFILE = os.path.join(D, 'mtf_stops_upstox.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

PCT = 5.0


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def is_dry():
    # read at call time, never at import time: a --dry-run guard must not depend on when the
    # module was first imported.
    return '--dry-run' in sys.argv or '--dry' in sys.argv

LIVE = ('trigger pending', 'open', 'open pending', 'modify pending')


def req(u, body=None, method=None):
    """Parsed JSON, or {'status':'error', ...} - the shape this script already handled.
    Shared keep-alive client, static-IP proxy, 25 s timeout, no retry on a write."""
    return api.call(u, body=body, method=method, proxy=True, timeout=25)


def tick_for(p):
    """Upstox rejects off-grid prices: Rs 0.10 at/above 1000, else Rs 0.05."""
    return 0.10 if float(p) >= 1000 else 0.05


def snap(p, t=None):
    t = t or tick_for(p)
    return round(round(float(p) / t) * t, 2)


def upstox_quotes(keys):
    out = {}
    keys = [k for k in keys if k]
    for i in range(0, len(keys), 20):
        q = urllib.parse.quote(','.join(keys[i:i + 20]), safe='')
        d = req('https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + q).get('data') or {}
        for v in d.values():
            out[v.get('instrument_token')] = v
    return out


def st_1h(key):
    try:
        import ind
        a = ind.analyze(key, 'hours', 1, 30)
        return a.get('st'), a.get('dir')
    except Exception as e:
        # Not fatal (the 5% stop still applies), but it must not be silent: a missing 1h series
        # is how a stop quietly ends up looser than the rule intends.
        print(f'upstox_mtf_stops: 1h Supertrend unavailable for {key}: {type(e).__name__}: {e}',
              file=sys.stderr)
        return None, None


def load_stops():
    try:
        return json.load(open(STOPFILE))
    except Exception:
        return {'stops': {}}


def save_stops(s, dry=None):
    if is_dry() if dry is None else dry:
        return
    json.dump(s, open(STOPFILE, 'w'), indent=1, sort_keys=True)


def main():
    import trade_lock
    import mtf_sizing as ms
    dry = is_dry()
    pct = float(arg('--pct')) if arg('--pct') else PCT
    at = arg('--at')
    now = datetime.datetime.now(IST)
    check_now = f'{now:%Y-%m-%d} {at}' if at else None
    mtf = ms._get('/v3/portfolio/mtf-positions').get('data') or []
    held = {}
    for p in mtf:
        q = int(p.get('quantity') or 0)
        if q > 0:
            s = p['trading_symbol'].replace('-EQ', '')
            held[s] = dict(qty=held.get(s, {}).get('qty', 0) + q, key=p.get('instrument_token'),
                           ltp=float(p.get('last_price') or 0), avg=float(p.get('average_price') or 0))
    km = ms.keymap()
    for s, v in held.items():
        v['key'] = v.get('key') or km.get(s)
    quotes = upstox_quotes([v['key'] for v in held.values()])

    # (5 Nov) a failed order-book read must never look like "no live stops": the script would then
    # place a second full-size SL on top of the live one, and a double stop sells twice the quantity.
    book = ms._get('/v2/order/retrieve-all')
    if book.get('status') != 'success' or book.get('data') is None:
        print(json.dumps({'error': 'order book unreadable - refusing to place stops that could duplicate',
                          'resp': str(book)[:200]}), file=sys.stderr)
        raise SystemExit(2)
    orders = book.get('data') or []
    live_sl = {}
    for o in orders:
        if ((o.get('transaction_type') or '').upper() == 'SELL'
                and (o.get('product') or '').upper() == 'MTF'
                and (o.get('status') or '').lower() in LIVE
                and (o.get('order_type') or '').upper() in ('SL', 'SL-M')):
            sym = (o.get('trading_symbol') or '').replace('-EQ', '')
            live_sl.setdefault(sym, []).append(o)

    rec = load_stops()
    stops = rec.setdefault('stops', {})
    acts = []
    for sym in sorted(held):
        h = held[sym]
        key = h['key']
        if not key:
            acts.append(dict(sym=sym, action='SKIP', why='no NSE instrument key')); continue
        q = quotes.get(key) or {}
        ltp = float(q.get('last_price') or 0) or h['ltp']
        lcl = float(q.get('lower_circuit_limit') or 0)
        if not ltp:
            acts.append(dict(sym=sym, action='SKIP', why='no quote')); continue
        qty = int(h['qty'])
        stx, d = st_1h(key)
        pct_stop = ltp * (1 - pct / 100.0)
        cand = pct_stop
        if stx and d == 1 and stx < ltp and stx > pct_stop:
            cand = stx                                  # Supertrend nearer, still below price
        cur = live_sl.get(sym) or []
        floor = max([float(stops.get(sym) or 0)] + [float(o.get('trigger_price') or 0) for o in cur])
        floor_note = None
        if floor >= ltp:
            # A recorded/live stop at or above price is unplaceable (it would have fired). Use the
            # tightest valid stop instead - that is the smallest possible move, never a real widening.
            floor_note = f'recorded/live stop {floor} is at or above ltp {ltp}; using the tightest valid stop'
            floor = ltp * 0.995
        if floor > cand:
            cand = floor                                # never widen: keep the better existing stop
        if cand >= ltp:
            cand = pct_stop                             # never above price
            if cand >= ltp:
                acts.append(dict(sym=sym, action='SKIP', why=f'stop {cand} at/above ltp {ltp}')); continue
        t = tick_for(ltp)
        trig = snap(cand, t)
        limit = snap(trig, t)                           # trigger and limit both at the computed level
        clamped = False
        if lcl and limit < lcl:
            # The whole 5%/Supertrend stop sits outside today's band and Upstox RMS rejects a limit
            # below it. Place the tightest stop the band allows instead (same fallback as
            # kite_mtf_stops.py). That is TIGHTER than the rule, so risk is never widened. The next
            # session re-runs this with a fresh band.
            t2 = tick_for(lcl)
            trig2 = snap(lcl + 3 * t2, t2)
            lim2 = snap(lcl, t2)
            if trig2 <= lim2 or trig2 >= ltp:
                acts.append(dict(sym=sym, action='SKIP', ltp=ltp, lower_circuit=lcl, trigger=trig,
                                 why='stop outside the band and no valid in-band level')); continue
            trig, limit, clamped = trig2, lim2, True
        info = dict(sym=sym, qty=qty, ltp=ltp, pct_stop=round(pct_stop, 2), st_1h=stx, st_dir=d,
                    recorded=stops.get(sym), floor=round(floor, 2) or None, trigger=trig, limit=limit)
        if floor_note:
            info['floor_note'] = floor_note
        if clamped:
            info['clamped_to_band'] = dict(lower_circuit=round(lcl, 2), rule_level=snap(cand, t),
                                           why='rule stop was below the lower circuit; using the tightest in-band stop')
        # idempotent: one live SL already covering the full qty at this level -> nothing to do
        exact = [o for o in cur if int(o.get('quantity') or 0) >= qty
                 and abs(float(o.get('trigger_price') or 0) - trig) < t / 2]
        if exact:
            acts.append({**info, 'action': 'KEEP', 'order': exact[0]['order_id'],
                         'why': 'live SL already covers the full qty at this level'})
            continue
        order = {'trading_symbol': sym + '-EQ', 'instrument_token': key, 'transaction_type': 'SELL',
                 'order_type': 'SL', 'product': 'MTF', 'quantity': qty, 'price': limit, 'trigger_price': trig}
        if check_now:
            order['_now'] = check_now
        ok, reasons, adj = trade_lock.check_order(order)
        info['trade_lock'] = {'allowed': ok, 'reasons': reasons, 'adjusted_qty': adj}
        if not ok or adj <= 0:
            acts.append({**info, 'action': 'BLOCKED', 'why': '; '.join(reasons) or 'adjusted qty 0'})
            continue
        qty2 = min(qty, adj)
        stale = [o for o in cur if o['order_id'] not in {x['order_id'] for x in exact}]
        if dry:
            acts.append({**info, 'action': 'PLACE(dry)', 'qty': qty2,
                         'cancel': [o['order_id'] for o in stale] or None,
                         'why': 'would cancel stale SL(s) and place fresh SL SELL' if stale else 'would place SL SELL'})
            continue
        # An overnight GTT and a day SL on the same position would both fire and sell twice.
        # The GTT goes first; if it cannot be cancelled, no day SL is placed and the GTT stays as cover.
        try:
            import gtt_overnight as gtt
            ok_g, det_g = gtt.cancel_for_day('upstox', sym, key=key)
        except Exception as e:
            ok_g, det_g = False, f'{type(e).__name__}: {e}'
        if not ok_g:
            acts.append({**info, 'action': 'GTT_FAIL', 'qty': qty2,
                         'why': f'overnight GTT could not be cancelled, day SL not placed: {det_g}'})
            continue
        cancelled = []
        for o in stale:
            r = req('https://api-hft.upstox.com/v2/order/cancel?order_id=' + o['order_id'], None, 'DELETE')
            cancelled.append({'order': o['order_id'], 'resp': r.get('status') or r})
        r = req('https://api-hft.upstox.com/v2/order/place', {
            'quantity': int(qty2), 'product': 'MTF', 'validity': 'DAY', 'price': limit, 'tag': 'trailsl',
            'instrument_token': key, 'order_type': 'SL', 'transaction_type': 'SELL',
            'disclosed_quantity': 0, 'trigger_price': trig, 'is_amo': False}, 'POST')
        oid = (r.get('data') or {}).get('order_id')
        if oid:
            stops[sym] = trig
            acts.append({**info, 'action': 'PLACE', 'qty': qty2, 'order': oid, 'cancelled': cancelled or None})
        else:
            acts.append({**info, 'action': 'FAIL', 'qty': qty2, 'cancelled': cancelled or None,
                         'why': str(r.get('body') or r)[:200]})
    save_stops(rec, dry)
    print(json.dumps({'now': now.strftime('%Y-%m-%d %H:%M IST'), 'dry_run': dry, 'pct': pct,
                      'positions': len(held), 'actions': acts}, indent=1, default=str))
    bad = [a for a in acts if a['action'] in ('FAIL', 'SKIP', 'BLOCKED')]
    if bad:
        print('\nNEEDS ATTENTION:', json.dumps(bad, default=str))


if __name__ == '__main__':
    main()
