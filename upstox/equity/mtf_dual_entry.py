#!/usr/bin/env python3
"""Mirrored MTF entry: buy the same stock on Upstox AND Kite as one decision (user, 4 Nov 2026 t305u).

"Tomorrow both upstox and kite should have the same trades." So every new MTF name goes on both
brokers, each sized by ITS OWN book (capital differs, so the share count differs) under the same
rules: the 10% per-stock cap, the 50% MTF budget and the half-room reserve, all measured on the MTF
margin the broker actually blocks, plus that broker's real free margin.

The pair is one decision: if either broker cannot take the trade at all, neither is placed. If one
broker fills and the other does not, the fill is reported loudly and the unfilled leg is cancelled -
a half-filled mirror is a bug, not a position.

Each leg also gets a protective SL (5% below the fill or the 1-hour Supertrend when nearer, never
above price), placed right after the fill and reported.

Aligning the two books (t305u/t306u, 5 Nov 2026): `mirror` compares the MTF names this bot holds
on each broker (mine only - a name the user bought themselves is never touched, and a name a broker
already holds is never bought again there) and reports or buys the missing leg on the broker that
lacks it, sized by that broker's own capital.

CLI:
  python3 mtf_dual_entry.py plan SYMBOL PRICE [--qty N] [--mode new|add]   # read only
  python3 mtf_dual_entry.py buy  SYMBOL PRICE [--qty N] [--mode new|add]   # places both legs
  python3 mtf_dual_entry.py mirror [--apply]                              # plan (default) or buy + arm stops
Plan/state go to mtf_mirror.json.
"""
import os, sys, json, time, datetime, urllib.request, urllib.error, urllib.parse

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
sys.path.insert(0, '/workspace/upstox')
import mtf_sizing as ms
import trade_lock
import ind

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
REC = os.path.join(D, 'mtf_mirror.json')
TOK = json.load(open('/workspace/upstox/token.json'))['access_token']
H = {'Authorization': 'Bearer ' + TOK, 'Accept': 'application/json', 'Content-Type': 'application/json',
     'User-Agent': 'Mozilla/5.0'}
_op = urllib.request.build_opener(urllib.request.ProxyHandler(
    {'https': os.environ['UPSTOX_PROXY_URL'], 'http': os.environ['UPSTOX_PROXY_URL']})) \
    if os.environ.get('UPSTOX_PROXY_URL') else urllib.request.build_opener()


def req(u, body=None, method=None):
    r = urllib.request.Request(u, data=json.dumps(body).encode() if body is not None else None,
                               headers=H, method=method)
    try:
        return json.load(_op.open(r, timeout=25))
    except urllib.error.HTTPError as e:
        return {'status': 'error', 'code': e.code, 'body': e.read().decode()[:300]}
    except Exception as e:
        return {'status': 'error', 'body': f'{type(e).__name__}: {e}'}


def tick(p, t=0.05):
    return round(max(round(p / t) * t, t), 2)


def quote(key):
    d = req('https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')).get('data') or {}
    return list(d.values())[0] if d else {}


def load():
    try:
        return json.load(open(REC))
    except Exception:
        return {}


def save(s):
    json.dump(s, open(REC, 'w'), indent=1)


def usym(sym):
    return sym if sym.endswith('-EQ') else sym + '-EQ'


def plan(symbol, price, mode='new', qty=None):
    """Size and gate both legs, no orders."""
    out = {'symbol': symbol, 'price': price, 'mode': mode, 'now': datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}
    legs = {}
    for b in ('upstox', 'kite'):
        try:
            sz = ms.size(b, symbol, price, mode)
            legs[b] = {'size': sz}
            q = qty if qty else sz['qty']
            if q and q != sz['qty']:
                legs[b]['check'] = ms.check(b, symbol, q, price, mode)
                legs[b]['requested'] = q
            elif q:
                legs[b]['requested'] = q
            else:
                legs[b]['blocked'] = sz.get('reason') or 'size 0'
        except Exception as e:
            legs[b] = {'error': f'{type(e).__name__}: {e}'}
    both = all(legs[b].get('requested') and legs[b]['size'].get('verdict') == 'OK'
               and (legs[b].get('check', {}).get('allowed', True)) for b in legs if 'error' not in legs[b])
    out['legs'] = legs
    out['both_ok'] = bool(both)
    if qty and not both:
        out['note'] = 'a fixed qty does not fit both books - each leg is sized by its own capital instead'
    return out


def sl_level(key, ltp, entry):
    """5% below price, or the 1-hour Supertrend when that is nearer; never above price."""
    try:
        a = ind.analyze(key, 'minutes', 60, 20)
        st = a['st']
        cand = max(ltp * 0.95, st) if st < ltp else ltp * 0.95
        return round(min(cand, ltp * 0.999), 2), a
    except Exception:
        return round(ltp * 0.95, 2), None


def buy(symbol, price, mode='new', qty=None):
    p = plan(symbol, price, mode, qty)
    if not p['both_ok']:
        p['action'] = 'NOT PLACED: one leg cannot take it (mirror must be whole)'
        return p
    oid = kid = None
    u = p['legs']['upstox']; k = p['legs']['kite']
    uq = u['requested']; kq = k['requested']
    # --- Upstox leg ---
    km = ms.keymap()
    key = km.get(symbol)
    if not key:
        p['action'] = 'NOT PLACED: no Upstox instrument key'
        return p
    ok, reasons, adj = trade_lock.check_order({'trading_symbol': usym(symbol), 'instrument_token': key,
                                               'transaction_type': 'BUY', 'order_type': 'LIMIT',
                                               'product': 'MTF', 'quantity': uq, 'price': price})
    p['legs']['upstox']['trade_lock'] = {'allowed': ok, 'reasons': reasons, 'adjusted_qty': adj}
    if not ok or adj <= 0:
        p['action'] = 'NOT PLACED: Upstox trade_lock refused - ' + '; '.join(reasons)
        return p
    uq = adj
    body = {'quantity': int(uq), 'product': 'MTF', 'validity': 'DAY', 'price': price, 'tag': 'rotation',
            'instrument_token': key, 'order_type': 'LIMIT', 'transaction_type': 'BUY',
            'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}
    r = req('https://api-hft.upstox.com/v2/order/place', body, 'POST')
    oid = (r.get('data') or {}).get('order_id')
    p['legs']['upstox']['order_id'] = oid
    p['legs']['upstox']['response'] = {'code': r.get('code'), 'body': r.get('body')} if not oid else 'placed'
    # --- Kite leg ---
    if oid:
        import broker_kite as bk
        try:
            m = bk.from_upstox(key)
        except Exception:
            m = None
        if not m:
            p['legs']['kite']['order_id'] = None
            p['legs']['kite']['error'] = 'no Kite symbol for ' + key
        else:
            kok, kre, kadj = trade_lock.check_order({'trading_symbol': m['tradingsymbol'],
                                                     'instrument_token': 'KITE|%s|%s' % (m['exchange'], m['tradingsymbol']),
                                                     'transaction_type': 'BUY', 'order_type': 'LIMIT',
                                                     'product': 'MTF', 'quantity': kq, 'price': price}, broker='kite')
            p['legs']['kite']['trade_lock'] = {'allowed': kok, 'reasons': kre, 'adjusted_qty': kadj}
            if kok and kadj > 0:
                kid = bk.place(m['exchange'], m['tradingsymbol'], 'BUY', kadj, price, tag='rotation', product='MTF')
                p['legs']['kite']['order_id'] = kid
                kq = kadj
            else:
                p['legs']['kite']['error'] = 'trade_lock refused: ' + '; '.join(kre)
    if not kid:
        # never leave a half mirror: cancel the Upstox order and say so
        if oid:
            req('https://api-hft.upstox.com/v2/order/cancel?order_id=' + oid, None, 'DELETE')
            p['action'] = 'Upstox order cancelled: the Kite leg could not be placed'
        else:
            p['action'] = 'nothing placed'
        return p
    p['action'] = f'PLACED both legs: Upstox {uq} @ {price}, Kite {kq} @ {price}'
    # --- fills + stops ---
    time.sleep(8)
    p['fills'] = {}
    d = (req('https://api.upstox.com/v2/order/details?order_id=' + oid).get('data') or {})
    ufill = int(d.get('filled_quantity') or 0); uavg = float(d.get('average_price') or price)
    p['fills']['upstox'] = {'status': d.get('status'), 'qty': ufill, 'avg': uavg}
    import broker_kite as bk2
    ks = bk2.order_status(kid) or {}
    kfill = int(ks.get('filled') or 0); kavg = float(ks.get('avg') or price)
    p['fills']['kite'] = {'status': ks.get('status'), 'qty': kfill, 'avg': kavg}
    st, _ = sl_level(key, uavg, uavg)
    p['stop_plan'] = {'upstox': st, 'kite': st}
    if ufill:
        ok2, why2, adj2 = trade_lock.check_order({'trading_symbol': usym(symbol), 'instrument_token': key,
                                                  'transaction_type': 'SELL', 'order_type': 'SL', 'product': 'MTF',
                                                  'quantity': ufill})
        if ok2:
            trig = tick(st, 0.10 if uavg >= 1000 else 0.05)
            lim = tick(trig * 0.995, 0.10 if uavg >= 1000 else 0.05)
            r2 = req('https://api-hft.upstox.com/v2/order/place', {
                'quantity': int(adj2), 'product': 'MTF', 'validity': 'DAY', 'price': lim, 'tag': 'trailsl',
                'instrument_token': key, 'order_type': 'SL', 'transaction_type': 'SELL',
                'disclosed_quantity': 0, 'trigger_price': trig, 'is_amo': False}, 'POST')
            p['stop_order_upstox'] = {'order_id': (r2.get('data') or {}).get('order_id'), 'trigger': trig, 'limit': lim}
        else:
            p['stop_order_upstox'] = {'error': '; '.join(why2)}
    if kfill:
        try:
            m2 = bk2.from_upstox(key)
            ktick = bk2.tick_for(m2['exchange'], m2['tradingsymbol'])
            ktrig = round(round(st / ktick) * ktick, 2)
            ksid = bk2.place(m2['exchange'], m2['tradingsymbol'], 'SELL', kfill, ktrig, order_type='SL',
                             trigger=ktrig, tag='trailsl', product='MTF')
            p['stop_order_kite'] = {'order_id': ksid, 'trigger': ktrig}
        except Exception as e:
            p['stop_order_kite'] = {'error': f'{type(e).__name__}: {e}'}
    save({**load(), f"{symbol}:{datetime.datetime.now(IST).date().isoformat()}": p})
    return p


def _kite_eq(symbol):
    """broker_kite.from_upstox only maps F&O keys, so equity names need the NSE cash dump.
    Cash symbols are identical on both brokers (Upstox shows GAIL-EQ, Kite GAIL)."""
    import broker_kite as bk
    for r in bk.dump('NSE'):
        if r.get('tradingsymbol') == symbol and (r.get('instrument_type') or '').upper() == 'EQ':
            return {'exchange': r['exchange'], 'tradingsymbol': r['tradingsymbol'],
                    'tick_size': float(r.get('tick_size') or 0.05), 'lot_size': int(r.get('lot_size') or 1)}
    return None


def _ltp(symbol):
    km = ms.keymap()
    key = km.get(symbol)
    if not key:
        return None, None
    q = quote(key)
    return key, float(q.get('last_price') or 0) or None


def _holdings(broker):
    """(mine, all) MTF names with qty>0 on one broker. mine comes from mtf_sizing's tag/managed rule."""
    st = ms.state(broker)
    mine = st.get('mine') or set()
    held = {s: v for s, v in st['held'].items() if v > 0}
    return {s for s in held if s in mine}, held


def mirror(apply=False):
    """Make the two books hold the same MTF names, without touching the user's own names."""
    out = {'now': datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'), 'apply': apply}
    books = {}
    for b in ('upstox', 'kite'):
        try:
            mine, held = _holdings(b)
            sl = ms.slots(b)
            books[b] = {'mine': sorted(mine), 'held': sorted(held), 'slots': sl}
        except Exception as e:
            out['error'] = f'{b}: {type(e).__name__}: {e}'
            return out
    out['books'] = {b: {'mine': books[b]['mine'],
                        'mine_count': len(books[b]['mine']),
                        'cap': books[b]['slots'].get('cap'),
                        'free_slots': books[b]['slots'].get('free_slots'),
                        'not_mine': books[b]['slots'].get('not_mine')} for b in books}
    u_mine = set(books['upstox']['mine'])
    k_mine = set(books['kite']['mine'])
    out['missing_on_kite'] = sorted(u_mine - k_mine)
    out['missing_on_upstox'] = sorted(k_mine - u_mine)
    out['already_mirrored'] = sorted(u_mine & k_mine)

    # Running ledgers per broker, so planning several legs together respects the caps: the count of
    # MY names (the stock cap) and the MTF margin already committed by legs planned earlier in this
    # same run. Without the margin ledger each leg looks affordable on its own and the pair overshoots.
    count = {b: len(books[b]['mine']) for b in books}
    committed = {b: 0.0 for b in books}
    legs = []
    for sym in out['missing_on_kite']:
        legs.append(_mirror_leg(sym, 'kite', books, count, committed))
    for sym in out['missing_on_upstox']:
        legs.append(_mirror_leg(sym, 'upstox', books, count, committed))
    out['legs'] = legs
    out['plan'] = [l for l in legs if l.get('buy_qty')]
    out['blocked'] = [l for l in legs if not l.get('buy_qty')]
    out['aligned'] = not out['plan'] and not out['blocked']

    if not apply or not out['plan']:
        if out['aligned']:
            out['action'] = 'aligned - both books hold the same names, nothing to buy'
        elif out['plan'] and not apply:
            out['action'] = 'PLAN ONLY (no orders). Re-run with --apply to buy these legs'
        else:
            out['action'] = (f"NOT ALIGNED: {len(out['blocked'])} leg(s) would need buying but cannot be - "
                             f"see blocked[].skip; {len(out['already_mirrored'])} name(s) already match")
        return out

    # ---- buy the missing legs ----
    placed = []
    for leg in out['plan']:
        res = _place_mirror_leg(leg)
        placed.append(res)
    out['placed'] = placed
    ok = [p for p in placed if p.get('order_id')]
    out['action'] = f'placed {len(ok)}/{len(placed)} missing leg(s)'
    # ---- arm protective stops on both books ----
    out['stops'] = _arm_stops()
    save({**load(), f"mirror:{datetime.datetime.now(IST).date().isoformat()}": out})
    return out


def _mirror_leg(sym, broker, books, count, committed):
    """Size + gate the leg that broker is missing. Returns the plan for it, never an order."""
    leg = {'symbol': sym, 'broker': broker}
    # hard guard 1: never touch a name the user owns themselves on either book
    for b in ('upstox', 'kite'):
        st = ms.state(b)
        if sym in (st.get('held') or {}) and sym not in (st.get('mine') or set()):
            leg['skip'] = f'{sym} is the user\'s own holding on {b} - not touched'
            return leg
    # hard guard 2: never buy a name a broker already holds
    if sym in books[broker]['held']:
        leg['skip'] = f'{broker} already holds {sym}'
        return leg
    # hard guard 3: the 6-name cap, counted on MY names only
    cap = books[broker]['slots'].get('cap') or ms.MAX_STOCKS
    if count[broker] >= cap:
        leg['skip'] = f'{broker} is at the cap ({count[broker]}/{cap} of my MTF names) - no free slot'
        return leg
    key, px = _ltp(sym)
    if not px:
        leg['skip'] = 'no Upstox price'
        return leg
    leg['price'] = px
    leg['instrument_key'] = key
    try:
        sz = ms.size(broker, sym, px, 'new')
    except Exception as e:
        leg['skip'] = f'size failed: {type(e).__name__}: {e}'
        return leg
    leg['size'] = {k: sz.get(k) for k in ('qty', 'amount', 'margin', 'margin_pct_of_capital',
                                          'leverage', 'verdict', 'reason', 'limits')}
    q = int(sz.get('qty') or 0)
    if q <= 0:
        leg['skip'] = sz.get('reason') or 'size 0'
        return leg
    try:
        ck = ms.check(broker, sym, q, px, 'new')
    except Exception as e:
        leg['skip'] = f'check failed: {type(e).__name__}: {e}'
        return leg
    leg['check'] = {k: ck.get(k) for k in ('allowed', 'blocks', 'warns', 'margin', 'notional', 'max_qty')}
    if not ck.get('allowed'):
        leg['skip'] = '; '.join(ck.get('blocks') or []) or 'blocked by mtf_sizing'
        return leg
    # cumulative guard: legs planned earlier in THIS run already consume budget on that broker
    st = ms.state_mine(broker)
    cap = float(st['capital'])
    ratio = float(sz['limits'].get('ratio') or (sz['margin'] / sz['amount'] if sz.get('amount') else 1.0))
    used_m = float(st['used']) * ratio
    room_total = max(0.5 * cap - used_m - committed[broker], 0.0)
    need = float(sz['margin'])
    leg['budget_after_prior_legs'] = {'committed_margin': round(committed[broker]),
                                      'room_total': round(room_total), 'need': round(need),
                                      'half_room': round(room_total * 0.5)}
    if need > room_total * 0.5:
        leg['skip'] = (f"after the legs already planned this run, only {room_total * 0.5:,.0f} of the "
                       f"half-room is left on {broker} but this leg needs {need:,.0f} margin")
        return leg
    committed[broker] += need
    # trade_lock, on the right broker's book
    if broker == 'upstox':
        o = {'trading_symbol': usym(sym), 'instrument_token': key, 'transaction_type': 'BUY',
             'order_type': 'LIMIT', 'product': 'MTF', 'quantity': q, 'price': px}
        ok, reasons, adj = trade_lock.check_order(o)
    else:
        m = _kite_eq(sym)
        if not m:
            leg['skip'] = 'no Kite equity symbol for ' + sym
            return leg
        leg['kite_symbol'] = m
        ok, reasons, adj = trade_lock.check_order(
            {'trading_symbol': m['tradingsymbol'],
             'instrument_token': 'KITE|%s|%s' % (m['exchange'], m['tradingsymbol']),
             'transaction_type': 'BUY', 'order_type': 'LIMIT', 'product': 'MTF',
             'quantity': q, 'price': px}, broker='kite')
    leg['trade_lock'] = {'allowed': ok, 'reasons': reasons, 'adjusted_qty': adj}
    if not ok or adj <= 0:
        leg['skip'] = 'trade_lock refused: ' + ('; '.join(reasons) or 'qty 0')
        return leg
    leg['buy_qty'] = int(adj)
    count[broker] += 1
    return leg


def _place_mirror_leg(leg):
    sym, broker, q, px = leg['symbol'], leg['broker'], leg['buy_qty'], leg['price']
    if broker == 'upstox':
        body = {'quantity': int(q), 'product': 'MTF', 'validity': 'DAY', 'price': px, 'tag': 'rotation',
                'instrument_token': leg['instrument_key'], 'order_type': 'LIMIT', 'transaction_type': 'BUY',
                'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}
        r = req('https://api-hft.upstox.com/v2/order/place', body, 'POST')
        oid = (r.get('data') or {}).get('order_id')
        return {'symbol': sym, 'broker': broker, 'qty': q, 'order_id': oid,
                'resp': None if oid else {'code': r.get('code'), 'body': r.get('body')}}
    import broker_kite as bk
    m = leg.get('kite_symbol') or _kite_eq(sym)
    if not m:
        return {'symbol': sym, 'broker': broker, 'qty': q, 'order_id': None, 'resp': 'no Kite symbol'}
    oid = bk.place(m['exchange'], m['tradingsymbol'], 'BUY', q, px, tag='rotation', product='MTF')
    return {'symbol': sym, 'broker': broker, 'kite_symbol': m['tradingsymbol'], 'qty': q, 'order_id': oid}


def _arm_stops():
    """Run both protective-stop scripts (no --dry-run) and return what they did."""
    import subprocess
    out = {}
    for name, cmd in (('upstox', [sys.executable, os.path.join(D, 'upstox_mtf_stops.py')]),
                      ('kite', [sys.executable, os.path.join(D, 'kite_mtf_stops.py')])):
        try:
            pr = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            out[name] = {'rc': pr.returncode, 'out': (pr.stdout or '')[-4000:], 'err': (pr.stderr or '')[-1000:]}
        except Exception as e:
            out[name] = {'rc': None, 'err': f'{type(e).__name__}: {e}'}
    return out


if __name__ == '__main__':
    a = sys.argv[1:]
    if not a or a[0] not in ('plan', 'buy', 'slots', 'mirror'):
        print(__doc__)
        sys.exit(1)
    if a[0] == 'mirror':
        print(json.dumps(mirror('--apply' in a), indent=1, default=str))
        sys.exit(0)
    if a[0] == 'slots':
        for b in ('upstox', 'kite'):
            print(json.dumps(ms.slots(b), indent=1, default=str))
        sys.exit(0)
    if len(a) < 3:
        print(__doc__)
        sys.exit(1)
    cmd, symbol, price = a[0], a[1].upper(), float(a[2])
    mode = 'new'
    if '--mode' in a:
        mode = a[a.index('--mode') + 1]
    q = None
    if '--qty' in a:
        q = int(a[a.index('--qty') + 1])
    res = plan(symbol, price, mode, q) if cmd == 'plan' else buy(symbol, price, mode, q)
    print(json.dumps(res, indent=1, default=str))
