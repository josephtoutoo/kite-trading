#!/usr/bin/env python3
"""Kite MTF protective stops (4 Nov 2026).

Kite's older MTF holdings had no stop-loss at all while the Upstox side is managed by
risk_manager.py. This fills that gap using the user's equity rule: stop 5% below the
last price, or the 1-hour Supertrend when that is nearer and still below price, never
above price, never lowered (an existing stop is only ever raised).

Prices come from Upstox quotes (Kite quotes/historicals throw PermissionException);
orders go through broker_kite.place as SL SELL with product MTF, tick-snapped, after
checking the exchange lower circuit so Kite's RMS does not reject the order.

CLI: python3 kite_mtf_stops.py [--dry] [--pct 5]
"""
import sys, json, os
sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, '/workspace/equity')

PCT = 5.0
DRY = '--dry' in sys.argv
if '--pct' in sys.argv:
    PCT = float(sys.argv[sys.argv.index('--pct') + 1])


def upstox_quotes(keys):
    """Batched LTP/band quotes from Upstox (Kite quotes need a permission this app lacks)."""
    import sys, urllib.parse
    import api
    out = {}
    keys = [k for k in keys if k]
    for i in range(0, len(keys), 20):
        chunk = keys[i:i + 20]
        q = urllib.parse.quote(','.join(chunk), safe='')
        try:
            # direct read, exactly as before (api.get only proxies the order host by default)
            d = api.get('/v2/market-quote/quotes?instrument_key=' + q).get('data') or {}
        except Exception as e:
            print(f'kite_mtf_stops: quote batch {i // 20} failed: {type(e).__name__}: {e}', file=sys.stderr)
            d = {}
        for v in d.values():
            out[v.get('instrument_token')] = v
    return out


def st_1h(key):
    try:
        import ind
        a = ind.analyze(key, 'hours', 1, 30)
        return a.get('st'), a.get('dir')
    except Exception as e:
        print(f'kite_mtf_stops: 1h Supertrend unavailable for {key}: {type(e).__name__}: {e}', file=sys.stderr)
        return None, None


def managed():
    # Only MTF names I opened myself may have their stop trailed (user rule, 4 Nov 2026).
    # Everything else gets a protective stop placed once and is then left alone.
    try:
        return set(json.load(open('/workspace/equity/mtf_managed.json')).get('managed') or [])
    except Exception:
        return set()


def past_cutoff():
    # Kite rejects SL order placement/modification after 15:15 IST ("Stop-loss orders not
    # allowed in the current session") and cancels the existing ones, so never touch MTF
    # stops past 15:14 - the next day's 9:05 run re-places them.
    import datetime
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    return now.time() > datetime.time(15, 14)


def main():
    import broker_kite as bk
    import mtf_sizing as ms
    if not bk.valid():
        print('kite invalid:', bk.why()); return
    k = bk.client()
    kh = k.holdings()       # one read for the whole loop: it was re-fetched per symbol
    st = ms.state('kite')
    held = {s: v for s, v in st['held'].items() if v > 0}
    stops, qty = {}, {}
    for o in k.orders():
        if (o['transaction_type'] == 'SELL' and o['product'] == 'MTF'
                and o['status'] in ('TRIGGER PENDING', 'OPEN') and o['order_type'] in ('SL', 'SL-M')):
            stops[o['tradingsymbol']] = o
    km = dict(ms.keymap())
    # BSE-listed names (e.g. NSE Ltd trades on BSE) are missing from the NSE map
    import glob
    import api
    for f in sorted(glob.glob('/workspace/upstox/instruments_BSE_*.json.gz'))[-1:]:
        for x in api.gzip_json(f):
            if x.get('segment') == 'BSE_EQ' and x.get('trading_symbol') not in km:
                km[x['trading_symbol']] = x['instrument_key']
    quotes = upstox_quotes([km.get(s) for s in held])
    frozen = past_cutoff() and not DRY
    mine = managed()
    acts = []
    for sym in sorted(held):
        key = km.get(sym)
        if not key:
            acts.append(dict(sym=sym, action='SKIP', why='no NSE instrument key')); continue
        q = quotes.get(key) or {}
        ltp = float(q.get('last_price') or 0)
        lcl = float(q.get('lower_circuit_limit') or 0)
        if not ltp:
            acts.append(dict(sym=sym, action='SKIP', why='no quote')); continue
        exch = 'BSE' if key.startswith('BSE_') else 'NSE'
        mtf = int((next((h.get('mtf') or {} for h in kh if h['tradingsymbol'] == sym), {}) or {}).get('quantity') or 0)
        want = mtf or int(round(held[sym] / max(ltp, 1e-9)))
        stx, d = st_1h(key)
        pct_stop = ltp * (1 - PCT / 100.0)
        cand = pct_stop
        if stx and d == 1 and stx < ltp and stx > pct_stop:
            cand = stx                      # Supertrend nearer, still below price
        if cand > ltp * 0.99:               # guard: never a stop within 1% of price
            cand = pct_stop
        tick = bk.tick_for(exch, sym) or 0.05
        trig = bk.snap(cand, tick)
        limit = bk.snap(trig - max(tick, trig * 0.005), tick)
        if lcl and limit < lcl:
            # the 5% stop sits outside today's band; use the tightest stop the exchange allows
            trig = bk.snap(lcl + 3 * tick, tick)
            limit = bk.snap(lcl, tick)
            if trig <= limit or trig >= ltp:
                acts.append(dict(sym=sym, action='SKIP', why=f'stop outside band (lcl {lcl}, ltp {ltp})'))
                continue
        cur = stops.get(sym)
        if frozen:
            acts.append(dict(sym=sym, action='KEEP' if cur else 'DEFER', stop=(cur or {}).get('trigger_price'),
                             want=trig, ltp=ltp,
                             why=None if cur else 'past 15:14 IST, SL not allowed in this session'))
            continue
        if cur and float(cur.get('trigger_price') or 0) >= trig:
            acts.append(dict(sym=sym, action='KEEP', stop=cur['trigger_price'], ltp=ltp)); continue
        if cur:
            if sym not in mine:
                acts.append(dict(sym=sym, action='KEEP', stop=cur['trigger_price'], ltp=ltp,
                                 why='existing holding: protective stop only, not trailed'))
                continue
            if DRY:
                acts.append(dict(sym=sym, action='RAISE(dry)', frm=cur['trigger_price'], to=trig, ltp=ltp)); continue
            bk.modify(cur['order_id'], want, limit, trig, order_type='SL')
            acts.append(dict(sym=sym, action='RAISE', frm=cur['trigger_price'], to=trig, ltp=ltp, order=cur['order_id']))
        else:
            if DRY:
                acts.append(dict(sym=sym, action='PLACE(dry)', qty=want, trigger=trig, limit=limit, ltp=ltp)); continue
            oid = bk.place(exch, sym, 'SELL', want, limit, order_type='SL', trigger=trig, tag='mtfsl', product='MTF')
            acts.append(dict(sym=sym, action='PLACE' if oid else 'FAIL', qty=want, trigger=trig, limit=limit, ltp=ltp, order=oid))
    print(json.dumps(acts, indent=1, default=str))
    bad = [a for a in acts if a['action'] in ('FAIL', 'SKIP')]
    if bad:
        print('\nNEEDS ATTENTION:', json.dumps(bad, default=str))


if __name__ == '__main__':
    main()
