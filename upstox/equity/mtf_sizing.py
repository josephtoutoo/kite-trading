#!/usr/bin/env python3
"""MTF sizing + margin gate (4 Nov 2026). READ ONLY - places NO orders.

Rules it enforces, per broker, on every proposed MTF BUY:
  CAPS ARE ON MARGIN (4 Nov 2026): the 10% per stock and 50% total caps are measured on the
  MTF MARGIN the broker blocks for the order, not on price x qty. MTF margin runs ~24% of
  notional (4.1x leverage), so the same 10% cap now funds roughly 4x the notional it used to.
  R10a  stock cap      margin(symbol held) + margin(new) <= 10% of that broker's capital
  R10b  total cap      MTF margin used + margin(new) <= 50% of capital
  R10c  reserve        a NEW stock may use at most half of the remaining room;
                       the other half is kept for pullback adds into existing
                       holdings (mode="add" may draw on the reserve)
  R10d  margin         the broker's own margin API must actually cover the order
                       with the free margin available right now
Sizing: max_qty() walks the largest quantity that satisfies all of the above, so
the answer is the real order size, not a guess from price x qty.

Capital, matching the existing scripts:
  Upstox  available_margin + used_margin   (equity segment)
  Kite    margins.net + utilised.debits

CLI:  python3 mtf_sizing.py state [broker]
      python3 mtf_sizing.py check SYMBOL QTY PRICE [--broker upstox|kite] [--mode new|add]
      python3 mtf_sizing.py size  SYMBOL PRICE [--broker upstox|kite] [--mode new|add]
"""
import json, sys, os, glob, gzip, urllib.request

sys.path.insert(0, '/workspace/upstox')
D = '/workspace/upstox'
PER_STOCK = 0.10
TOTAL = 0.50
NEW_SHARE = 0.50          # new stocks may use half of the remaining room
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mtf_sizing_cache.json')


def _get(path):
    tok = json.load(open(os.path.join(D, 'token.json')))['access_token']
    r = urllib.request.Request('https://api.upstox.com' + path,
                               headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/json',
                                        'User-Agent': 'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r, timeout=20))


def keymap():
    f = sorted(glob.glob(os.path.join(D, 'instruments_NSE_*.json.gz')))[-1]
    return {x['trading_symbol']: x['instrument_key'] for x in json.load(gzip.open(f))
            if x.get('segment') == 'NSE_EQ' and x.get('instrument_type') == 'EQ'}


# ---------------- per-broker state ----------------
def upstox_state():
    f = _get('/v2/user/get-funds-and-margin')['data']['equity']
    cap = float(f['available_margin']) + float(f['used_margin'])
    held = {}
    for p in _get('/v3/portfolio/mtf-positions').get('data') or []:
        q = int(p.get('quantity') or 0)
        if q > 0:
            s = p['trading_symbol'].replace('-EQ', '')
            held[s] = held.get(s, 0.0) + q * float(p.get('average_price') or 0)
    return dict(broker='upstox', capital=cap, free=float(f['available_margin']),
                used=sum(held.values()), held=held)


def kite_state():
    import broker_kite as bk
    if not bk.valid():
        raise RuntimeError('kite unavailable: ' + bk.why())
    k = bk.client()
    m = k.margins('equity')
    cap = float(m['net']) + float(m['utilised']['debits'])
    av = m.get('available') or {}
    free = min(float(m['net']), float(av.get('live_balance') or 0) + float(av.get('collateral') or 0))
    held = {}
    for p in k.positions().get('net') or []:
        if p['product'] == 'MTF' and p['quantity'] > 0 and p['exchange'] in ('NSE', 'BSE'):
            held[p['tradingsymbol']] = held.get(p['tradingsymbol'], 0.0) + p['quantity'] * float(p['average_price'])
    try:
        for h in k.holdings():
            q = int((h.get('mtf') or {}).get('quantity') or 0)
            if q > 0 and h['tradingsymbol'] not in held:
                held[h['tradingsymbol']] = q * float((h.get('mtf') or {}).get('average_price') or h.get('average_price') or 0)
    except Exception:
        pass
    return dict(broker='kite', capital=cap, free=free, used=sum(held.values()), held=held)


def state(broker):
    return upstox_state() if broker == 'upstox' else kite_state()


# ---------------- broker margin quotes ----------------
def margin_upstox(symbol, qty, price):
    km = keymap()
    key = km.get(symbol)
    if not key:
        return dict(ok=False, why=f'no Upstox instrument key for {symbol}')
    tok = json.load(open(os.path.join(D, 'token.json')))['access_token']
    body = json.dumps({'instruments': [{'instrument_key': key, 'quantity': int(qty), 'product': 'MTF',
                                        'transaction_type': 'BUY'}]}).encode()
    r = urllib.request.Request('https://api.upstox.com/v2/charges/margin', data=body,
                               headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/json',
                                        'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
    try:
        d = json.load(urllib.request.urlopen(r, timeout=20))['data']
        return dict(ok=True, margin=float(d['final_margin']), notional=qty * price)
    except Exception as e:
        return dict(ok=False, why=f'{type(e).__name__}: {str(e)[:120]}')


def margin_kite(symbol, qty, price, exchange='NSE'):
    import broker_kite as bk
    k = bk.client()
    for ex in ([exchange] + [x for x in ('NSE', 'BSE') if x != exchange]):
        try:
            r = k.order_margins([{'exchange': ex, 'tradingsymbol': symbol, 'transaction_type': 'BUY',
                                  'variety': 'regular', 'product': 'MTF', 'order_type': 'LIMIT',
                                  'quantity': int(qty), 'price': float(price)}])
            m = r[0]
            return dict(ok=True, margin=float(m.get('mtf') or m.get('total') or 0),
                        leverage=m.get('leverage'), notional=qty * price, exchange=ex)
        except Exception as e:
            last = f'{type(e).__name__}: {str(e)[:120]}'
    return dict(ok=False, why=last)


def margin_quote(broker, symbol, qty, price):
    return margin_upstox(symbol, qty, price) if broker == 'upstox' else margin_kite(symbol, qty, price)


# ---------------- the gate ----------------
def limits(st, symbol, mode='new', ratio=1.0):
    """Margin limits for one order (4 Nov 2026: caps are on MTF margin, not on notional).
    ratio = margin / notional for this symbol, so existing holdings stored at cost can be
    converted to the margin they consume."""
    cap = st['capital']
    per = PER_STOCK * cap
    already = st['held'].get(symbol, 0.0) * ratio          # cost -> margin it blocks
    used_m = st['used'] * ratio                            # portfolio at cost -> margin
    room_total = max(TOTAL * cap - used_m, 0.0)
    room = (room_total * NEW_SHARE if mode == 'new' and symbol not in st['held'] else room_total)
    by_stock = max(per - already, 0.0)
    return dict(per_stock=round(per), already=round(already), by_stock=round(by_stock),
                room=round(room), room_total=round(room_total), ratio=round(ratio, 4),
                reserve_after=round(room_total - min(room_total, room)))


def size(broker, symbol, price, mode='new', step=None):
    st = state(broker)
    probe = margin_quote(broker, symbol, 1, price)     # margin for ONE share at the proposed price
    if not probe['ok']:
        return dict(broker=broker, symbol=symbol, mode=mode, qty=0, verdict='NO SIZE',
                    reason='margin quote failed: ' + probe.get('why', ''), state=st, quote=probe)
    per_share = probe['margin']
    ratio = per_share / price if price else 1.0
    lim = limits(st, symbol, mode, ratio)
    amt_cap = min(lim['by_stock'], lim['room'])        # rupees of MARGIN the caps allow
    qty_by_margin = int(amt_cap // per_share) if per_share > 0 else 0
    q = qty_by_margin
    quote = None
    floor = step or 1
    while q >= floor:
        quote = margin_quote(broker, symbol, q, price)
        if quote['ok'] and quote['margin'] <= st['free']:
            break
        q -= floor
    else:
        return dict(broker=broker, symbol=symbol, mode=mode, qty=0, verdict='NO SIZE',
                    reason='free margin does not cover one lot', state=st, limits=lim, quote=quote)
    out = dict(broker=broker, symbol=symbol, price=price, mode=mode, qty=q,
               amount=round(q * price), margin=round(quote['margin']),
               margin_pct_of_capital=round(100 * quote['margin'] / st['capital'], 1),
               leverage=quote.get('leverage'), verdict='OK' if q else 'NO SIZE',
               state=dict(capital=round(st['capital']), free=round(st['free']), used=round(st['used'])),
               limits=lim)
    if q == 0:
        out['reason'] = 'no room under the 10% stock cap / MTF room'
    return out


def check(broker, symbol, qty, price, mode='new'):
    st = state(broker)
    probe = margin_quote(broker, symbol, 1, price)
    ratio = (probe['margin'] / price) if probe.get('ok') and price else 1.0
    lim = limits(st, symbol, mode, ratio)
    q = margin_quote(broker, symbol, qty, price)
    m_new = q['margin'] if q.get('ok') else qty * price * ratio
    notional = qty * price
    blocks, warns = [], []
    if m_new > lim['by_stock']:
        blocks.append(f"R10a: {symbol} margin {m_new:,.0f} > 10% stock cap room {lim['by_stock']:,.0f} (cap {lim['per_stock']:,.0f}, already {lim['already']:,.0f})")
    if st['used'] * ratio + m_new > TOTAL * st['capital']:
        blocks.append(f"R10b: total MTF margin {st['used'] * ratio + m_new:,.0f} > 50% cap {TOTAL * st['capital']:,.0f}")
    if mode == 'new' and symbol not in st['held'] and m_new > lim['room']:
        blocks.append(f"R10c: new entry margin {m_new:,.0f} > half of remaining room {lim['room']:,.0f} (reserve {lim['reserve_after']:,.0f} kept for pullback adds)")
    if not q['ok']:
        blocks.append('R10d: margin quote failed - ' + q['why'])
    elif q['margin'] > st['free']:
        blocks.append(f"R10d: margin needed {q['margin']:,.0f} > free margin {st['free']:,.0f}")
    if q.get('leverage') is not None and q['leverage'] < 1.5:
        warns.append(f"leverage only {q['leverage']}x")
    return dict(broker=broker, symbol=symbol, qty=qty, price=price, mode=mode, notional=round(notional),
                margin=round(m_new),
                allowed=not blocks, blocks=blocks, warns=warns, quote=q,
                max_qty=size(broker, symbol, price, mode)['qty'],
                state=dict(capital=round(st['capital']), free=round(st['free']), used=round(st['used'])),
                limits=lim)


MAX_STOCKS = 5


def slots(broker):
    """How many new MTF stocks this broker may still open, and whether any
    today's pending buy will consume one. READ ONLY."""
    st = state(broker)
    held = [s for s, v in st['held'].items() if v > 0]
    free = max(MAX_STOCKS - len(held), 0)
    pending = []
    try:
        if broker == 'upstox':
            for o in _get('/v2/order/retrieve-all').get('data') or []:
                if (o.get('product') == 'MTF' and o.get('transaction_type') == 'BUY'
                        and o.get('status') in ('open', 'trigger pending', 'open pending')):
                    pending.append(o.get('trading_symbol'))
        else:
            import broker_kite as bk
            for o in bk.client().orders():
                if o['product'] == 'MTF' and o['transaction_type'] == 'BUY' and o['status'] in ('OPEN', 'TRIGGER PENDING'):
                    pending.append(o['tradingsymbol'])
    except Exception:
        pass
    free -= len(pending)
    return dict(broker=broker, held=held, count=len(held), free_slots=max(free, 0),
                pending_buys=pending, capital=round(st['capital']), used=round(st['used']))


if __name__ == '__main__':
    a = sys.argv[1:]
    def opt(name, default):
        if name in a:
            i = a.index(name); v = a[i + 1]; a.pop(i); a.pop(i); return v
        return default
    mode = opt('--mode', 'new')
    brk = opt('--broker', None)
    if a and a[0] == 'slots':
        for b in ([brk] if brk else ['upstox', 'kite']):
            try:
                r = slots(b)
                print(f"{b}: {r['count']}/{MAX_STOCKS} MTF stocks, {r['free_slots']} free slot(s)"
                      f"{', pending buys ' + ','.join(map(str, r['pending_buys'])) if r['pending_buys'] else ''}"
                      f" | capital {r['capital']:,.0f} | MTF used {r['used']:,.0f}")
            except Exception as e:
                print(b, 'ERR', type(e).__name__, str(e)[:120])
    elif a and a[0] == 'state':
        for b in ([brk] if brk else ['upstox', 'kite']):
            try:
                st = state(b)
                print(f"{b}: capital {st['capital']:,.0f} | 50% budget {0.5*st['capital']:,.0f} | MTF used {st['used']:,.0f} "
                      f"| room {0.5*st['capital']-st['used']:,.0f} | free margin {st['free']:,.0f} | stocks {len(st['held'])}")
            except Exception as e:
                print(b, 'ERR', type(e).__name__, str(e)[:120])
    elif a and a[0] == 'check':
        brk = brk or 'upstox'
        r = check(brk, a[1], int(a[2]), float(a[3]), mode)
        print(json.dumps(r, indent=1, default=str))
    elif a and a[0] == 'size':
        brk = brk or 'upstox'
        print(json.dumps(size(brk, a[1], float(a[2]), mode), indent=1, default=str))
    else:
        print(__doc__)
