#!/usr/bin/env python3
"""Overnight protective stops via GTT (Good-Till-Triggered), 5 Nov 2026.

Why: every stop this desk places is a DAY order. They all die at the close, Kite cancels SLs
outright after 15:15, and nothing re-arms overnight - so from 3:30 PM to the first stop-trail run
the next morning every position is naked. GTT survives the close and the weekend, so it is the
only mechanism here that protects a position while nobody is watching.

Design (one stop per position, never two):
  * after the close this places a GTT SELL stop for each position this bot manages;
  * the morning stop scripts call `cancel SYM` for a name BEFORE they place its day SL, so the
    GTT and the day SL never coexist. If the cancel fails the day SL is not placed and the GTT
    stays live, so protection never lapses in the wrong direction;
  * levels follow the same rules as the day stops: 5% under LTP or the 1-hour Supertrend when
    nearer for MTF equity, 30% under entry for stock options, and never lower than a level
    already recorded (risk only narrows).

Scopes (user's decision, 4 Nov 2026 t325u): the Upstox MTF names this bot opened, the Upstox
option positions it manages, and the Kite MTF names it opened plus its Kite option/future longs.
The 14 Kite MTF names the user bought themselves are left on day stops and are never touched.

CLI: python3 gtt_overnight.py status | place [--dry-run] | cancel SYM [SYM...] | cancel-all
Exit codes: 0 ok, 1 something failed, 2 a book could not be read (nothing was changed).
Read this file's own notes before changing the trigger maths - the audit depends on it.
"""
import sys, os, json, datetime

sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, '/workspace/equity')
import api
import mtf_sizing as ms

D = os.path.dirname(os.path.abspath(__file__))
SF = os.path.join(D, 'gtt_state.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DRY = '--dry-run' in sys.argv
PCT = 5.0                 # MTF equity: stop under LTP
OPT_STOP = 0.30           # stock options: 30% under entry


def _k(broker, sym):
    """State key. One symbol can be held on both brokers, so never key by symbol alone."""
    return f'{broker}:{sym}'


def load():
    try:
        return json.load(open(SF))
    except Exception:
        return {}


def save(s):
    if DRY:
        return
    tmp = SF + '.tmp'
    json.dump(s, open(tmp, 'w'), indent=1, sort_keys=True)
    os.replace(tmp, SF)


def tick_for(p):
    return 0.10 if float(p) >= 1000 else 0.05


def snap(p, t=None):
    t = t or tick_for(p)
    return round(round(float(p) / t) * t, 2)


def st_1h(key):
    """(supertrend level, direction) on the last closed 1h candle, or (None, 0)."""
    try:
        import ind
        a = ind.analyze(key, unit='minutes', interval=60, days=20)
        return a.get('st'), a.get('st_dir')
    except Exception as e:
        print(f'gtt_overnight: 1h trend unavailable for {key}: {type(e).__name__}: {e}', file=sys.stderr)
        return None, 0


def quotes(keys):
    """instrument_key -> quote dict, in one batched call."""
    import urllib.parse
    out = {}
    keys = [k for k in keys if k]
    for i in range(0, len(keys), 25):
        part = keys[i:i + 25]
        q = urllib.parse.quote(','.join(part), safe='')
        r = api.call('https://api.upstox.com/v3/market-quote/quotes?instrument_key=' + q)
        for v in (r.get('data') or {}).values():
            key = v.get('instrument_token') or ''
            out[key] = v
    return out


# ---------------- targets ----------------
def upstox_targets():
    """MTF equity + every open option/future on Upstox that this bot manages."""
    mtf = api.get('/v3/portfolio/mtf-positions')
    pos = api.get('/v2/portfolio/short-term-positions')
    for r in (mtf, pos):
        if r.get('status') != 'success' or r.get('data') is None:
            raise RuntimeError(f'Upstox book unreadable: {str(r)[:120]}')
    mine = ms.mine_upstox()          # fail-closed: raises if the order book cannot be read
    rows = []
    for p in mtf.get('data') or []:
        q = int(p.get('quantity') or 0)
        s = p['trading_symbol'].replace('-EQ', '')
        if q > 0 and s in mine:
            rows.append(dict(broker='upstox', book='MTF', sym=s, key=p.get('instrument_token'),
                             qty=q, entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0), product='MTF', kind='eq'))
    for p in pos.get('data') or []:
        q = int(p.get('quantity') or 0)
        if q > 0:
            rows.append(dict(broker='upstox', book='Option', sym=p['trading_symbol'],
                             key=p.get('instrument_token'), qty=q,
                             entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0),
                             product=p.get('product') or 'D', kind='opt'))
    return rows


def kite_targets():
    """MTF equity this bot opened, plus its option/future longs."""
    import broker_kite as bk
    if not bk.valid():
        raise RuntimeError('Kite token invalid')
    mine = ms.mine_kite()            # fail-closed
    st = ms.state('kite')            # fail-closed on an unreadable book
    k = bk.client()
    held_mtf = {s: v for s, v in st['held'].items() if v > 0 and s in mine}
    rows = []
    net = k.positions().get('net')
    if net is None:
        raise RuntimeError('Kite positions returned no data')
    for p in net:
        q = int(p.get('quantity') or 0)
        if q <= 0:
            continue
        sym = p['tradingsymbol']
        if p.get('product') == 'MTF' and sym in held_mtf:
            rows.append(dict(broker='kite', book='MTF', sym=sym, key=sym, qty=q,
                             entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0), product='MTF', kind='eq'))
        elif sym[-2:] in ('CE', 'PE', 'FUT'):
            rows.append(dict(broker='kite', book='Option', sym=sym, key=sym, qty=q,
                             entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0),
                             product=p.get('product') or 'NRML', kind='opt'))
    return rows


# ---------------- existing GTTs ----------------
def upstox_gtts():
    r = api.get('https://api.upstox.com/v3/order/gtt')
    if r.get('status') != 'success' or r.get('data') is None:
        raise RuntimeError('Upstox GTT list unreadable')
    out = {}
    for g in r.get('data') or []:
        rules = g.get('rules') or []
        live = [x for x in rules if (x.get('status') or '').upper() in ('SCHEDULED', 'ACTIVE', 'OPEN')]
        if not live:
            continue
        key = g.get('instrument_token') or ''
        if not key:
            continue
        # keyed by instrument_token: the GTT list and the positions list spell option
        # symbols differently ("PAYTM 1720 CE 23 NOV 26" vs "PAYTM26NOV1720CE").
        out[key] = dict(id=g.get('gtt_order_id'), trigger=float(live[0].get('trigger_price') or 0),
                        qty=int(g.get('quantity') or 0),
                        sym=(g.get('trading_symbol') or '').replace('-EQ', ''))
    return out


def kite_gtts():
    import broker_kite as bk
    out = {}
    for g in bk.client().get_gtts() or []:
        if (g.get('status') or '') != 'active':
            continue
        cond = g.get('condition') or {}
        sym = cond.get('tradingsymbol') or ''
        if not sym:
            continue
        out[sym] = dict(id=g.get('id'), trigger=float((cond.get('trigger_values') or [0])[0] or 0),
                        qty=sum(int(o.get('quantity') or 0) for o in (g.get('orders') or [])))
    return out


# ---------------- level maths ----------------
def level_eq(row, recorded):
    """5% under LTP, or the 1h Supertrend when nearer and still below price. Never widens."""
    ltp = row['ltp']
    cand = ltp * (1 - PCT / 100.0)
    if row['broker'] == 'upstox':
        stx, d = st_1h(row['key'])
    else:
        km = ms.keymap()
        stx, d = st_1h(km.get(row['sym']) or '')
    if stx and d == 1 and cand < stx < ltp:
        cand = stx
    cand = min(cand, ltp * 0.99)          # never within 1% of price
    floor = max(float(recorded or 0), 0.0)
    if floor and floor >= ltp * 0.995:    # a recorded stop at/above price cannot be placed again
        floor = 0.0
    return max(cand, floor)


def level_opt(row, recorded):
    ltp, entry = row['ltp'], row['entry']
    cand = entry * (1 - OPT_STOP) if entry else ltp * 0.7
    floor = float(recorded or 0)
    if floor and floor >= ltp * 0.995:
        floor = 0.0
    return max(min(cand, ltp * 0.97), floor)


def recorded_level(broker, sym):
    """The level a previous run or the day scripts recorded, so a restart cannot widen a stop."""
    if broker == 'upstox':
        try:
            return float((json.load(open(os.path.join(D, 'mtf_stops_upstox.json')))
                          .get('stops') or {}).get(sym) or 0)
        except Exception:
            return 0.0
    try:
        rs = json.load(open('/workspace/upstox/risk_state.json'))
    except Exception:
        return 0.0
    for k, v in rs.items():
        if isinstance(v, dict) and v.get('sym') == sym:
            return float(v.get('stop') or 0)
    return 0.0


# ---------------- actions ----------------
def place_one(row, trig, rec):
    """Place or raise one GTT. Returns an action dict."""
    t = tick_for(trig)
    trig = snap(trig, t)
    if trig >= row['ltp']:
        return dict(sym=row['sym'], broker=row['broker'], action='SKIP', why='trigger at/above ltp',
                    trig=trig, ltp=row['ltp'])
    if row['broker'] == 'upstox':
        body = dict(type='SINGLE', quantity=int(row['qty']), product=row['product'],
                    rules=[dict(strategy='ENTRY', trigger_type='BELOW', trigger_price=trig)],
                    instrument_token=row['key'], transaction_type='SELL')
        if DRY:
            return dict(sym=row['sym'], broker='upstox', action='PLACE(dry)', trig=trig, qty=row['qty'])
        r = api.call('https://api.upstox.com/v3/order/gtt/place', body=body, proxy=True)
        gid = ((r.get('data') or {}).get('gtt_order_ids') or [None])[0]
        if not gid:
            return dict(sym=row['sym'], broker='upstox', action='FAIL', why=str(r.get('body') or r)[:200])
        rec[_k('upstox', row['sym'])] = dict(broker='upstox', gtt_id=gid, trigger=trig, qty=row['qty'],
                               placed=datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M'))
        return dict(sym=row['sym'], broker='upstox', action='PLACE', gtt=gid, trig=trig, qty=row['qty'])
    import broker_kite as bk
    limit = snap(max(trig - max(t, trig * 0.005), 0.05), t)
    if DRY:
        return dict(sym=row['sym'], broker='kite', action='PLACE(dry)', trig=trig, qty=row['qty'])
    try:
        g = bk.client().place_gtt(
            trigger_type=bk.client().GTT_TYPE_SINGLE, tradingsymbol=row['sym'],
            exchange='NSE' if row['book'] == 'MTF' else ('NFO' if row['sym'].endswith('CE') or row['sym'].endswith('PE') else 'NFO'),
            trigger_values=[trig], last_price=row['ltp'],
            orders=[dict(transaction_type='SELL', quantity=int(row['qty']), product=row['product'],
                         order_type='LIMIT', price=limit)])
    except Exception as e:
        return dict(sym=row['sym'], broker='kite', action='FAIL', why=f'{type(e).__name__}: {str(e)[:180]}')
    rec[_k('kite', row['sym'])] = dict(broker='kite', gtt_id=g.get('trigger_id'), trigger=trig, qty=row['qty'],
                           placed=datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M'))
    return dict(sym=row['sym'], broker='kite', action='PLACE', gtt=g.get('trigger_id'), trig=trig,
                qty=row['qty'], limit=limit)


def cancel_sym(broker, sym, rec, key=None):
    """Cancel one symbol's GTT. -> (ok, detail). Failures are loud, never silent."""
    if broker == 'upstox':
        gid = (rec.get(_k('upstox', sym)) or {}).get('gtt_id')
        if not gid and key:
            gid = (upstox_gtts().get(key) or {}).get('id')
        if not gid:
            return True, 'no GTT'
        if DRY:
            return True, 'dry'
        r = api.call('https://api.upstox.com/v3/order/gtt/cancel', body={'gtt_order_id': gid},
                     method='DELETE', proxy=True)
        if r.get('status') != 'success':
            return False, str(r.get('body') or r)[:160]
        rec.pop(_k('upstox', sym), None)
        return True, gid
    import broker_kite as bk
    gid = (rec.get(_k('kite', sym)) or {}).get('gtt_id') or (kite_gtts().get(sym) or {}).get('id')
    if not gid:
        return True, 'no GTT'
    if DRY:
        return True, 'dry'
    try:
        bk.client().delete_gtt(gid)
    except Exception as e:
        return False, f'{type(e).__name__}: {str(e)[:140]}'
    rec.pop(_k('kite', sym), None)
    return True, gid


def cancel_for_day(broker, sym, key=None):
    """Hand-off from a day-stop script: kill this name's overnight GTT before a day SL is placed.

    Two live stops for one position would sell the quantity twice. Returns (ok, detail);
    the caller must NOT place its day stop when ok is False, so the GTT stays as cover.
    """
    rec = load()
    ok, detail = cancel_sym(broker, sym, rec, key=key)
    save(rec)
    return ok, detail


def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    cmd = a[0] if a else 'status'
    rec = load()
    if cmd == 'cancel-all' or cmd == 'cancel':
        wants = set(a[1:])
        acts = []
        try:
            ug, kg = upstox_gtts(), kite_gtts()
        except Exception as e:
            print(json.dumps({'error': f'GTT list unreadable - nothing cancelled: {e}'}))
            return 2
        for broker, live in (('upstox', ug), ('kite', kg)):
            items = ([(k, v.get('sym') or k, k) for k, v in live.items()] if broker == 'upstox'
                     else [(v.get('id'), k, None) for k, v in live.items()])
            for _id, sym, key in sorted(items, key=lambda x: str(x[1])):
                if wants and sym not in wants:
                    continue
                ok, detail = cancel_sym(broker, sym, rec, key=key)
                acts.append(dict(sym=sym, broker=broker, action='CANCEL' if ok else 'CANCEL_FAIL', detail=detail))
        save(rec)
        print(json.dumps(acts, indent=1, default=str))
        return 0 if all(x['action'] != 'CANCEL_FAIL' for x in acts) else 1
    # status / place
    try:
        rows = upstox_targets() + kite_targets()
    except Exception as e:
        print(json.dumps({'error': f'book unreadable - nothing placed: {type(e).__name__}: {e}'}))
        return 2
    try:
        live_u, live_k = upstox_gtts(), kite_gtts()
    except Exception as e:
        print(json.dumps({'error': f'GTT list unreadable - nothing placed: {type(e).__name__}: {e}'}))
        return 2
    live = {'upstox': live_u, 'kite': live_k}
    qs = quotes([r['key'] for r in rows if r['broker'] == 'upstox']) if any(r['broker'] == 'upstox' for r in rows) else {}
    acts = []
    for row in rows:
        if row['broker'] == 'upstox' and row['book'] == 'MTF':
            q = qs.get(row['key']) or {}
            row['ltp'] = float(q.get('last_price') or 0) or row['ltp']
        if not row['ltp']:
            acts.append(dict(sym=row['sym'], broker=row['broker'], action='SKIP', why='no price'))
            continue
        rec_level = recorded_level(row['broker'], row['sym'])
        lvl = level_eq(row, rec_level) if row['kind'] == 'eq' else level_opt(row, rec_level)
        cur = live[row['broker']].get(row['key'] if row['broker'] == 'upstox' else row['sym'])
        # one tick of slack: a stop a tick below the computed level is covered, not stale,
        # otherwise every run would replace a perfectly good GTT for a rounding difference.
        covered = bool(cur) and cur['trigger'] >= lvl - tick_for(lvl) - 1e-9
        # status is strictly read-only: it reports, it never places and never cancels.
        if cmd == 'status':
            acts.append(dict(sym=row['sym'], broker=row['broker'],
                             action='KEEP' if covered else 'WOULD_PLACE',
                             gtt=(cur or {}).get('id'), trig=round(lvl, 2), ltp=row['ltp'],
                             qty=row['qty'], old=None if not cur else cur['trigger']))
            continue
        if covered:
            acts.append(dict(sym=row['sym'], broker=row['broker'], action='KEEP', gtt=cur['id'],
                             trig=cur['trigger'], ltp=row['ltp']))
            continue
        if cur:      # our stop is stale or lower than the rules now allow: replace it, never widen
            ok, detail = cancel_sym(row['broker'], row['sym'], rec, key=row.get('key'))
            if not ok:
                acts.append(dict(sym=row['sym'], broker=row['broker'], action='REPLACE_FAIL',
                                 why=f'old GTT {cur["id"]} could not be cancelled: {detail}'))
                save(rec)
                continue
        acts.append(place_one(row, lvl, rec))
        save(rec)
    save(rec)
    print(json.dumps({'now': datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'),
                      'dry_run': DRY, 'targets': len(rows), 'actions': acts}, indent=1, default=str))
    bad = [x for x in acts if x['action'] in ('FAIL', 'REPLACE_FAIL')]
    if bad:
        print('\nNEEDS ATTENTION:', json.dumps(bad, default=str))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
