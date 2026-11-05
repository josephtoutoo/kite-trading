#!/usr/bin/env python3
"""MTF sizing + margin gate (4 Nov 2026). READ ONLY - places NO orders.

Rules it enforces, per broker, on every proposed MTF BUY:
  CAPS ARE ON MARGIN (4 Nov 2026): the 10% per stock and 50% total caps are measured on the
  MTF MARGIN the broker blocks for the order, not on price x qty. MTF margin runs ~24% of
  notional (4.1x leverage), so the same 10% cap now funds roughly 4x the notional it used to.
  R10a  stock cap      margin(symbol held) + margin(new) <= 10% of that broker's capital
                       (12% on 5-6 Nov 2026 only - equity/autonomy.py, t339u)
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

Scoping (t304u, finished 5 Nov 2026): the stock COUNT cap and the MTF budget count only names
this bot opened (mine_upstox / mine_kite / mtf_managed.json). Names the user bought themselves are
excluded from both - they still block real margin, so free margin and the R10d coverage check keep
using every live holding.

CLI:  python3 mtf_sizing.py state [broker]
      python3 mtf_sizing.py check SYMBOL QTY PRICE [--broker upstox|kite] [--mode new|add]
      python3 mtf_sizing.py size  SYMBOL PRICE [--broker upstox|kite] [--mode new|add]
"""
import json, sys, os, glob, gzip, time

sys.path.insert(0, '/workspace/upstox')
import api                     # one pooled, timed, User-Agent-bearing client (5 Nov 2026)

D = '/workspace/upstox'
# (5 Nov 2026) In-process memo so one run never re-asks the broker for the same thing:
#   * keymap(): the gzipped NSE master used to be re-read and re-parsed on every call (~304 ms).
#     Cached on (path, mtime, size), so an instruments refresh invalidates it at once.
#   * _get(): portfolio / order / funds reads are reused for QUOTE_TTL seconds. The old code
#     fetched /v2/order/retrieve-all twice inside one size() and again in slots().
QUOTE_TTL = 3.0               # seconds; one decision run stays well inside this
_memo = {}                    # path -> (expires_at, payload)
_margin_memo = {}             # (broker, symbol, qty, price) -> (expires_at, quote)
_state_memo = {}              # broker -> (expires_at, state)
PER_STOCK = 0.10
TOTAL = 0.50
# t339u (4 Nov 2026): the per-stock cap is 12% on 5-6 Nov only, via equity/autonomy.py. Read it at
# decision time, never by rewriting PER_STOCK - a module global rewritten at import would outlive
# its own expiry and quietly raise this cap for ever. TOTAL is deliberately NOT overridden: it
# stays 50%, and the override table says so, so the total cannot drift.
# Loaded by path (a sibling file of THIS one), not by name: mtf_sizing is imported from several
# scripts, and one of them may not have this folder on sys.path - in which case a plain
# `import autonomy` would silently fall back to the user's caps ON an override day, which is
# the one failure this whole change exists to avoid.
_autonomy = None
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        'autonomy', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'autonomy.py'))
    _autonomy = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_autonomy)
    sys.modules.setdefault('autonomy', _autonomy)
except Exception as _ae:      # missing module -> the user's caps, which is the safe default
    _autonomy = None
    print(f'mtf_sizing: autonomy module unavailable, user caps apply: {type(_ae).__name__}: {_ae}', file=sys.stderr)


def per_stock_cap(when=None):
    """The per-stock MTF cap in force now: 0.10, or 0.12 on 5-6 Nov 2026."""
    if _autonomy is None:
        return PER_STOCK
    try:
        return float(_autonomy.effective('MTF_PER_STOCK', PER_STOCK, when))
    except Exception as e:
        print(f'mtf_sizing: override lookup failed, keeping the user cap: {type(e).__name__}: {e}',
              file=sys.stderr)
        return PER_STOCK


def total_cap(when=None):
    """The total MTF cap in force now. Never above the 0.50 ceiling, whatever any table says -
    this number is a hard guard, not only a preference (t339u did not raise it)."""
    if _autonomy is None:
        return min(TOTAL, 0.50)
    try:
        return min(float(_autonomy.effective('MTF_TOTAL', TOTAL, when)), 0.50)
    except Exception as e:
        print(f'mtf_sizing: override lookup failed, keeping the user total: {type(e).__name__}: {e}',
              file=sys.stderr)
        return min(TOTAL, 0.50)
NEW_SHARE = 0.50          # new stocks may use half of the remaining room
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mtf_sizing_cache.json')
# (t304u) The 5-stock MTF cap and the MTF budget count ONLY names this bot opened. Holdings the
# user brought in themselves (or traded only in the app) are excluded from the count. A name is
# "mine" when one of my own entry tags appears on a completed BUY for it, or when it is recorded
# in mtf_managed.json. Broker free margin (R10d) still uses every live holding - that is real money.
MY_TAGS = ('rotation', 'scanmtf', 'pullback', 'mtfentry', 'mtf_entry', 'refill', 'add', 'top5')
_km_cache = {}               # master path -> ((mtime_ns, size), map)
MANAGED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mtf_managed.json')


def _managed():
    try:
        return set((json.load(open(MANAGED)).get('managed') or []))
    except FileNotFoundError:
        return set()
    except Exception as e:
        # An unreadable managed list under-counts MY names, which moves the stock cap - say so.
        print(f'mtf_sizing: could not read {os.path.basename(MANAGED)} (my-name list): {type(e).__name__}: {e}',
              file=sys.stderr)
        return set()


class BookUnavailable(RuntimeError):
    """The broker order book could not be read. Never treat that as "I hold nothing": a failed
    read used to shrink the cap view to zero and free every slot, which lets a routine open new
    MTF names on top of positions it cannot see (5 Nov 2026)."""


def mine_upstox():
    """Names this bot opened on Upstox (order-book tags, or mtf_managed.json). Fail-closed:
    raises BookUnavailable instead of returning an empty set when the book cannot be read."""
    tags = {}
    try:
        book = _get('/v2/order/retrieve-all').get('data')
    except Exception as e:
        raise BookUnavailable(f'Upstox order book unreadable: {type(e).__name__}: {e}')
    if book is None:
        raise BookUnavailable('Upstox order book returned no data')
    for o in book:
        if (o.get('product') == 'MTF' and o.get('transaction_type') == 'BUY'
                and o.get('status') == 'complete' and (o.get('tag') or '') in MY_TAGS):
            tags[o['trading_symbol'].replace('-EQ', '')] = o.get('tag')
    return set(tags) | _managed()


def mine_kite():
    """Names this bot opened on Kite (same tag rule as Upstox, or mtf_managed.json). Fail-closed
    like mine_upstox: an unreadable book raises instead of silently freeing every slot."""
    out = set(_managed())
    try:
        import broker_kite as bk
        if not bk.valid():
            raise BookUnavailable('Kite token invalid')
        book = bk.client().orders()
    except BookUnavailable:
        raise
    except Exception as e:
        raise BookUnavailable(f'Kite order book unreadable: {type(e).__name__}: {e}')
    if book is None:
        raise BookUnavailable('Kite order book returned no data')
    for o in book:
        try:
            sym = o['tradingsymbol']; prod = o['product']
            txn = o['transaction_type']; status = o['status']; tag = o.get('tag') or ''
        except Exception:
            continue
        if prod == 'MTF' and txn == 'BUY' and status == 'COMPLETE' and tag in MY_TAGS:
            out.add(sym)
    return out


def _get(path):
    """GET api.upstox.com + path. Memoised for QUOTE_TTL seconds per process."""
    if QUOTE_TTL > 0:
        hit = _memo.get(path)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    d = api.get(path)
    if QUOTE_TTL > 0:
        _memo[path] = (time.monotonic() + QUOTE_TTL, d)
    return d


def forget(path=None):
    """Drop the read memo (all paths, or one) - used after an order so the next read is fresh.
    Also drops the memoised broker state and margin quotes, so nothing downstream can act on
    a book or a quote from before the order."""
    if path is None:
        _memo.clear(); _state_memo.clear(); _margin_memo.clear()
    else:
        _memo.pop(path, None)
        if path.startswith('/v3/portfolio') or path.startswith('/v2/user'):
            _state_memo.clear()


def _km(f):
    return {x['trading_symbol']: x['instrument_key'] for x in api.gzip_json(f)
            if x.get('segment') == 'NSE_EQ' and x.get('instrument_type') == 'EQ'}


def keymap():
    """trading_symbol -> instrument_key from the newest NSE master. Parsed once per process;
    the cache is keyed on (path, mtime, size) so a refreshed/rolled master is re-read."""
    f = sorted(glob.glob(os.path.join(D, 'instruments_NSE_*.json.gz')))[-1]
    hit = _km_cache.get(f)
    st = os.stat(f)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return dict(hit[1])          # fresh dict per call, as before - callers may mutate it
    km = _km(f)
    _km_cache.clear()
    _km_cache[f] = ((st.st_mtime_ns, st.st_size), km)
    return dict(km)


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
                used=sum(held.values()), held=held, mine=mine_upstox())


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
    try:
        net = k.positions().get('net')
    except Exception as e:
        raise BookUnavailable(f'Kite positions unreadable: {type(e).__name__}: {e}')
    if net is None:
        raise BookUnavailable('Kite positions returned no data')
    for p in net:
        if p['product'] == 'MTF' and p['quantity'] > 0 and p['exchange'] in ('NSE', 'BSE'):
            held[p['tradingsymbol']] = held.get(p['tradingsymbol'], 0.0) + p['quantity'] * float(p['average_price'])
    try:
        for h in k.holdings():
            q = int((h.get('mtf') or {}).get('quantity') or 0)
            if q > 0 and h['tradingsymbol'] not in held:
                held[h['tradingsymbol']] = q * float((h.get('mtf') or {}).get('average_price') or h.get('average_price') or 0)
    except Exception as e:
        # Fail closed. An incomplete `held` is dangerous: mtf_dual_entry's "never buy a name the
        # broker already holds" guard reads this dict, so a swallowed holdings error would let the
        # mirror buy an MTF holding it already owns.
        raise BookUnavailable(f'Kite holdings unreadable: {type(e).__name__}: {e}')
    return dict(broker='kite', capital=cap, free=free, used=sum(held.values()), held=held, mine=mine_kite())


def state(broker):
    """Broker capital / holdings / my-names. Memoised for QUOTE_TTL per process: one run asks
    this several times (size, then check, then the cumulative-budget guard in mtf_dual_entry),
    and each answer used to cost a funds read plus a portfolio read per call. Call forget()
    after anything that moves the book so the next reader still sees the trade."""
    if QUOTE_TTL > 0:
        hit = _state_memo.get(broker)
        if hit and hit[0] > time.monotonic():
            st = dict(hit[1])
            st['held'] = dict(st['held'])
            return st
    st = upstox_state() if broker == 'upstox' else kite_state()
    if QUOTE_TTL > 0:
        _state_memo[broker] = (time.monotonic() + QUOTE_TTL, st)
    return st


def state_mine(broker):
    """(t304u) The same state, but the MTF stock cap and the MTF budget count ONLY names this bot
    opened. Other MTF holdings still block real margin, so free / R10d stay untouched.
    Adds held_all (every MTF name) and not_mine (the user's own), both at cost."""
    st = state(broker)
    m = st.get('mine') or set()
    held = {s: v for s, v in st['held'].items() if s in m}
    st = dict(st)
    st['held_all'] = st['held']
    st['held'] = held
    st['used'] = sum(held.values())
    st['not_mine'] = {s: v for s, v in st['held_all'].items() if s not in m}
    return st


# ---------------- broker margin quotes ----------------
def margin_upstox(symbol, qty, price):
    km = keymap()
    key = km.get(symbol)
    if not key:
        return dict(ok=False, why=f'no Upstox instrument key for {symbol}')
    body = {'instruments': [{'instrument_key': key, 'quantity': int(qty), 'product': 'MTF',
                             'transaction_type': 'BUY'}]}
    try:
        d = api.request('https://api.upstox.com/v2/charges/margin', body, 'POST')['data']
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
    """The broker's margin for this exact order. size() asks at qty 1 and again at the final
    qty, check() asks twice more for the same pair, and mtf_dual_entry re-runs size() then
    check() - all the same question. Memoised for QUOTE_TTL so a run never quotes twice,
    and never acts on a quote older than QUOTE_TTL."""
    ck = (broker, symbol, int(qty), float(price))
    if QUOTE_TTL > 0:
        hit = _margin_memo.get(ck)
        if hit and hit[0] > time.monotonic():
            return dict(hit[1])
    q = margin_upstox(symbol, qty, price) if broker == 'upstox' else margin_kite(symbol, qty, price)
    if QUOTE_TTL > 0 and q.get('ok'):
        _margin_memo[ck] = (time.monotonic() + QUOTE_TTL, dict(q))
    return q


# ---------------- the gate ----------------
def limits(st, symbol, mode='new', ratio=1.0, when=None):
    """Margin limits for one order (4 Nov 2026: caps are on MTF margin, not on notional).
    ratio = margin / notional for this symbol, so existing holdings stored at cost can be
    converted to the margin they consume. `when` is the decision date for the two-day
    override (None = now); it only selects which cap applies, never widens one."""
    cap = st['capital']
    per = per_stock_cap(when) * cap        # 10%, or 12% on 5-6 Nov 2026 (t339u)
    already = st['held'].get(symbol, 0.0) * ratio          # cost -> margin it blocks
    used_m = st['used'] * ratio                            # portfolio at cost -> margin
    room_total = max(total_cap(when) * cap - used_m, 0.0)
    room = (room_total * NEW_SHARE if mode == 'new' and symbol not in st['held'] else room_total)
    by_stock = max(per - already, 0.0)
    return dict(per_stock=round(per), already=round(already), by_stock=round(by_stock),
                room=round(room), room_total=round(room_total), ratio=round(ratio, 4),
                reserve_after=round(room_total - min(room_total, room)))


def size(broker, symbol, price, mode='new', step=None, when=None):
    st = state_mine(broker)
    probe = margin_quote(broker, symbol, 1, price)     # margin for ONE share at the proposed price
    if not probe['ok']:
        return dict(broker=broker, symbol=symbol, mode=mode, qty=0, verdict='NO SIZE',
                    reason='margin quote failed: ' + probe.get('why', ''), state=st, quote=probe)
    per_share = probe['margin']
    ratio = per_share / price if price else 1.0
    lim = limits(st, symbol, mode, ratio, when)
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
        out['reason'] = f'no room under the {per_stock_cap(when) * 100:g}% stock cap / MTF room'
    return out


def check(broker, symbol, qty, price, mode='new', when=None):
    st = state_mine(broker)
    probe = margin_quote(broker, symbol, 1, price)
    ratio = (probe['margin'] / price) if probe.get('ok') and price else 1.0
    lim = limits(st, symbol, mode, ratio, when)
    q = margin_quote(broker, symbol, qty, price)
    m_new = q['margin'] if q.get('ok') else qty * price * ratio
    notional = qty * price
    blocks, warns = [], []
    if m_new > lim['by_stock']:
        blocks.append(f"R10a: {symbol} margin {m_new:,.0f} > {per_stock_cap(when) * 100:g}% stock cap room {lim['by_stock']:,.0f} (cap {lim['per_stock']:,.0f}, already {lim['already']:,.0f})")
    _tot = total_cap(when)
    if st['used'] * ratio + m_new > _tot * st['capital']:
        blocks.append(f"R10b: total MTF margin {st['used'] * ratio + m_new:,.0f} > {_tot * 100:g}% cap {_tot * st['capital']:,.0f}")
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
                max_qty=size(broker, symbol, price, mode, when=when)['qty'],
                state=dict(capital=round(st['capital']), free=round(st['free']), used=round(st['used'])),
                limits=lim)


# 5 (t383u, 5 Nov 2026): user cap is back to 5 managed MTF names per broker. Was briefly 6
# (t305u/t306u) so the two books could mirror while Upstox already held 6; now trim to 5 and stay there.
MAX_STOCKS = 5


def slots(broker):
    """How many new MTF stocks this broker may still open, and whether any
    today's pending buy will consume one. READ ONLY."""
    st = state_mine(broker)
    held = [s for s, v in st['held'].items() if v > 0]   # only names I opened count toward the stock cap
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
    except Exception as e:
        print(f'mtf_sizing: pending-order read failed in slots({broker}): {type(e).__name__}: {e}', file=sys.stderr)
    # a pending buy of a name I already hold is an add, not a new slot (and drop duplicate legs)
    pending = sorted({str(x).replace('-EQ', '') for x in pending if x} - set(held))
    free -= len(pending)
    return dict(broker=broker, held=held, count=len(held), cap=MAX_STOCKS, free_slots=max(free, 0),
                pending_buys=pending, not_mine=len(st.get('not_mine') or {}),
                capital=round(st['capital']), used=round(st['used']))


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
                print(f"{b}: {r['count']}/{r['cap']} MTF stocks of MINE (cap counts only my names), "
                      f"{r['free_slots']} free slot(s)"
                      f"{' - pending buys ' + ','.join(map(str, r['pending_buys'])) if r['pending_buys'] else ''}"
                      f" | not mine (excluded from cap/budget): {r['not_mine']}"
                      f" | capital {r['capital']:,.0f} | MTF used (mine) {r['used']:,.0f}")
                if r['held']:
                    print(f"    mine: {', '.join(r['held'])}")
            except Exception as e:
                print(b, 'ERR', type(e).__name__, str(e)[:120])
    elif a and a[0] == 'state':
        for b in ([brk] if brk else ['upstox', 'kite']):
            try:
                st = state_mine(b)
                allm = st['held_all']; nm = st['not_mine']
                print(f"{b}: capital {st['capital']:,.0f} | free margin {st['free']:,.0f} (ALL holdings)")
                print(f"    MINE    : {len(st['held'])}/{MAX_STOCKS} stock(s) (cap {MAX_STOCKS}), cost {st['used']:,.0f}"
                      f" -> 50% budget {0.5*st['capital']:,.0f}, room {0.5*st['capital']-st['used']:,.0f}")
                print(f"    not mine: {len(nm)} stock(s), cost {sum(nm.values()):,.0f}  "
                      f"(excluded from the cap and the MTF budget, still blocks margin)")
                print(f"    all MTF : {len(allm)} stock(s), cost {sum(allm.values()):,.0f}")
                for s2, v in sorted(allm.items()):
                    print(f"      {'MINE ' if s2 in st['held'] else 'user '} {s2:<12} cost {v:,.0f}")
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
