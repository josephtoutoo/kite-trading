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
    nearer for MTF equity, 20% under entry for new option positions (30% kept for ones
    already open on 16 Nov 2026), and never lower than a level already recorded (risk only
    narrows). A working DAY SL and a GTT must never both cover the same quantity: if a day
    SL is still live the GTT is deferred (NFO already dies at the cash close; MCX stays open
    into the evening, so this check matters there). --cover-open-session places the GTT
    anyway and leaves the day SL in place; crude_auto then cancels the GTT if the position
    goes flat, so they cannot both sell.

Scopes (user's decision, 4 Nov 2026 t325u): the Upstox MTF names this bot opened, the Upstox
option positions it manages, and the Kite MTF names it opened plus its Kite option/future longs.
The 14 Kite MTF names the user bought themselves are left on day stops and are never touched.

t691u: debit spreads get a paired overnight GTT: SELL BELOW on the long at the
net-debit stop, and a companion BUY BELOW on the short at the corresponding
price (same fractional move). A SELL GTT is never placed on the short. A
naked leftover short is bought back immediately (COVER_NAKED_SHORT) on
`reconcile` and at the start of `place` / `status` (status only reports).

CLI: python3 gtt_overnight.py status [SYM...] | place [--dry-run] [--cover-open-session] [SYM...] | reconcile [--dry-run] | cancel SYM [SYM...] | cancel-all
Exit codes: 0 ok, 1 something failed, 2 a book could not be read (nothing was changed).
Read this file's own notes before changing the trigger maths - the audit depends on it.
"""
import sys, os, json, datetime

sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, '/workspace/equity')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api
import mtf_sizing as ms
import stockopt_spread as ssp

D = os.path.dirname(os.path.abspath(__file__))
SF = os.path.join(D, 'gtt_state.json')
_SPREAD_LEGS = []  # filled while reading targets; used to infer debit-spread pairs
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DRY = '--dry-run' in sys.argv

def _kite_writes_allowed():
    try:
        import kite_disabled as _kd
        return not _kd.active()
    except Exception:
        return not os.path.exists('/workspace/upstox/kite_disabled.json') and not os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'kite_disabled.json'))

COVER_OPEN = '--cover-open-session' in sys.argv
PCT = 5.0                 # MTF equity: stop under LTP
OPT_STOP = 0.30           # fallback only if perf_rules / state are unreadable
# MCX F&O roots (Kite tradingsymbols). Used when the position left exchange blank.
MCX_PREFIXES = (
    'CRUDEOIL', 'NATURALGAS', 'NATGASMINI', 'GOLD', 'SILVER',
    'COPPER', 'ZINC', 'LEAD', 'NICKEL', 'ALUMINI',
    'MENTHA', 'COTTON', 'KAPAS', 'MCXBULLDEX', 'MCXMETLDEX', 'MCXENRGDEX',
)


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


def symbol_exchange(sym):
    """Guess the Kite exchange from a tradingsymbol when the position left it blank."""
    s = (sym or '').upper()
    if s.startswith(('SENSEX', 'BANKEX')):
        return 'BFO'
    if s.startswith(MCX_PREFIXES):
        return 'MCX'
    return None


def kite_exchange(row):
    """Exchange for a Kite GTT. The position's own exchange wins; if it is missing, derive
    MCX/BFO from the instrument (16 Nov 2026: CRUDEOIL* sent as NFO -> 'Instrument not found').
    The NFO default is only the last fallback."""
    if row.get('book') == 'MTF':
        return 'NSE'
    ex = (row.get('exchange') or '').upper()
    if ex in ('NFO', 'BFO', 'MCX', 'CDS', 'BCD', 'NSE', 'BSE'):
        return ex
    guessed = symbol_exchange(row.get('sym') or '')
    if guessed:
        return guessed
    try:
        import broker_kite as bk
        sym = row.get('sym') or ''
        if sym and bk._index('MCX')[1].get(sym):
            return 'MCX'
    except Exception:
        pass
    return 'NFO'


def contract_tick(row, trig):
    """Exchange tick for this contract from the daily Kite master; the old price-band guess
    (0.05 / 0.10) is the fallback. CRUDEOIL options tick 0.10 and BANKNIFTY futures 0.20."""
    fallback = tick_for(trig)
    if row.get('kind') == 'eq':
        return fallback
    try:
        import broker_kite as bk
        if row['broker'] == 'kite':
            t = float(bk.tick_for(kite_exchange(row), row['sym'], default=fallback) or fallback)
        else:
            t = float((bk.from_upstox(row.get('key') or '') or {}).get('tick_size') or fallback)
        return t if t >= 0.05 else fallback
    except Exception:
        return fallback


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
    # Day-stop book also gets overnight GTT cover (plain protective only). Tags age out of
    # today's order book, so mine alone drops names like GAIL/UNITDSPR that upstox_mtf_stops
    # still protects. Never add those to mtf_managed.json - that would turn trailing back on
    # for excluded_existing holdings (t264u). mtf_stops_upstox.json is the durable tracker.
    try:
        day = set((json.load(open(os.path.join(D, 'mtf_stops_upstox.json'))).get('stops') or {}))
    except Exception:
        day = set()
    cover = mine | day
    rows = []
    for p in mtf.get('data') or []:
        q = int(p.get('quantity') or 0)
        s = p['trading_symbol'].replace('-EQ', '')
        if q > 0 and s in cover:
            rows.append(dict(broker='upstox', book='MTF', sym=s, key=p.get('instrument_token'),
                             qty=q, entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0), product='MTF', kind='eq'))
    for p in pos.get('data') or []:
        q = int(p.get('quantity') or 0)
        if q != 0:
            _SPREAD_LEGS.extend(ssp.legs_from_positions([p], 'upstox'))
        if q > 0:
            rows.append(dict(broker='upstox', book='Option', sym=p['trading_symbol'],
                             key=p.get('instrument_token'), qty=q,
                             entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0),
                             product=p.get('product') or 'D', kind='opt'))
    return rows


def kite_targets():
    """MTF equity this bot opened, plus its option/future longs.

    Kite keeps MTF stock in holdings().mtf.quantity, not positions().net, so
    holdings are merged in. mine_kite() only sees today's tagged MTF BUYs, so
    a bot-opened name from a prior day would otherwise drop out of the
    overnight book; live held MTF is covered unless it is listed in
    mtf_managed.json excluded_existing (user names left untouched, t264u).
    """
    import broker_kite as bk
    if not bk.valid():
        raise RuntimeError('Kite token invalid')
    mine = ms.mine_kite()            # fail-closed
    st = ms.state('kite')            # fail-closed on an unreadable book
    k = bk.client()
    try:
        excl = set((json.load(open('/workspace/equity/mtf_managed.json')).get('excluded_existing') or []))
    except Exception:
        excl = set()
    held_mtf = {s: v for s, v in st['held'].items() if v > 0 and (s in mine or s not in excl)}
    rows = []
    seen_mtf = set()
    net = k.positions().get('net')
    if net is None:
        raise RuntimeError('Kite positions returned no data')
    for p in net:
        q = int(p.get('quantity') or 0)
        if q != 0:
            _SPREAD_LEGS.extend(ssp.legs_from_positions([p], 'kite', sym_field='tradingsymbol'))
        if q <= 0:
            continue
        sym = p['tradingsymbol']
        if p.get('product') == 'MTF' and sym in held_mtf:
            rows.append(dict(broker='kite', book='MTF', sym=sym, key=sym, qty=q,
                             entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0), product='MTF', kind='eq'))
            seen_mtf.add(sym)
        elif sym[-2:] in ('CE', 'PE', 'FUT'):
            rows.append(dict(broker='kite', book='Option', sym=sym, key=sym, qty=q,
                             entry=float(p.get('average_price') or 0),
                             ltp=float(p.get('last_price') or 0),
                             product=p.get('product') or 'NRML', kind='opt',
                             exchange=p.get('exchange') or ''))
    holdings = k.holdings()
    if holdings is None:
        raise RuntimeError('Kite holdings returned no data')
    for h in holdings:
        mtf = h.get('mtf') or {}
        q = int(mtf.get('quantity') or 0)
        if q <= 0:
            continue
        sym = h.get('tradingsymbol') or ''
        if not sym or sym in seen_mtf or sym not in held_mtf:
            continue
        rows.append(dict(broker='kite', book='MTF', sym=sym, key=sym, qty=q,
                         entry=float(mtf.get('average_price') or h.get('average_price') or 0),
                         ltp=float(h.get('last_price') or 0), product='MTF', kind='eq',
                         exchange=h.get('exchange') or 'NSE'))
        seen_mtf.add(sym)
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


def option_stop_frac(row):
    """20% for a new option life, 30% if risk_state already marked the position legacy."""
    try:
        import perf_rules as prules
        rec = _risk_row(row['broker'], row['sym']) or {}
        if rec.get('stop_pct') is not None:
            return prules.stop_pct(rec)
        return prules.INITIAL_STOP_PCT
    except Exception:
        return OPT_STOP


def level_opt(row, recorded):
    ltp, entry = row['ltp'], row['entry']
    frac = option_stop_frac(row)
    cand = entry * (1 - frac) if entry else ltp * (1 - frac)
    floor = float(recorded or 0)
    if floor and floor >= ltp * 0.995:
        floor = 0.0
    return max(min(cand, ltp * 0.97), floor)


def _risk_state():
    try:
        return json.load(open('/workspace/upstox/risk_state.json'))
    except Exception:
        return {}


def _risk_row(broker, sym):
    """The risk_state record for this broker+symbol, if any."""
    rs = _risk_state()
    if broker == 'kite':
        for k, v in rs.items():
            if isinstance(v, dict) and v.get('sym') == sym and (
                    k.startswith('KITE|') or k.startswith('KITE:') or v.get('broker') == 'kite'):
                return v
    for k, v in rs.items():
        if isinstance(v, dict) and v.get('sym') == sym:
            return v
    return None


def recorded_level(broker, sym):
    """The level a previous run or the day scripts recorded, so a restart cannot widen a stop."""
    if broker == 'upstox':
        try:
            eq = float((json.load(open(os.path.join(D, 'mtf_stops_upstox.json')))
                        .get('stops') or {}).get(sym) or 0)
        except Exception:
            eq = 0.0
        rec = _risk_row(broker, sym)
        opt = float((rec or {}).get('stop') or 0)
        return max(eq, opt)
    rec = _risk_row(broker, sym)
    return float((rec or {}).get('stop') or 0)


# ---------------- live day SLs (must not coexist with a GTT) ----------------
def kite_day_sls():
    """Working Kite DAY SELL SL/SL-M, keyed by tradingsymbol."""
    import broker_kite as bk
    out = {}
    for o in bk.orders(strict=True):
        if (o.get('transaction_type') or '').upper() != 'SELL':
            continue
        if (o.get('status') or '').lower() not in ('trigger pending', 'open'):
            continue
        if (o.get('order_type') or '') not in ('SL', 'SL-M'):
            continue
        sym = o.get('trading_symbol') or o.get('tradingsymbol') or ''
        if not sym:
            continue
        out[sym] = dict(oid=o.get('order_id'), trigger=float(o.get('trigger_price') or 0),
                        qty=int(o.get('quantity') or 0), exchange=(o.get('exchange') or ''))
    return out


def upstox_day_sls():
    """Working Upstox DAY SELL SL/SL-M, keyed by trading_symbol."""
    r = api.get('/v2/order/retrieve-all')
    out = {}
    for o in r.get('data') or []:
        if (o.get('transaction_type') or '').upper() != 'SELL':
            continue
        if (o.get('status') or '').lower() not in ('trigger pending', 'open'):
            continue
        if (o.get('order_type') or '') not in ('SL', 'SL-M'):
            continue
        sym = (o.get('trading_symbol') or o.get('tradingsymbol') or '').replace('-EQ', '')
        if not sym:
            continue
        out[sym] = dict(oid=o.get('order_id'), trigger=float(o.get('trigger_price') or 0),
                        qty=int(o.get('quantity') or 0))
    return out


# ---------------- actions ----------------
def place_one(row, trig, rec):
    """Place or raise one GTT. Returns an action dict."""
    t = contract_tick(row, trig)
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
    if not _kite_writes_allowed():
        return dict(sym=row['sym'], broker='kite', action='SKIP', why='kite disabled by user 21 Nov',
                    trig=trig, qty=row['qty'])
    import broker_kite as bk
    limit = snap(max(trig - max(t, trig * 0.005), 0.05), t)
    if DRY:
        return dict(sym=row['sym'], broker='kite', action='PLACE(dry)', trig=trig, qty=row['qty'])
    try:
        g = bk.client().place_gtt(
            trigger_type=bk.client().GTT_TYPE_SINGLE, tradingsymbol=row['sym'],
            exchange=kite_exchange(row),
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
    if not _kite_writes_allowed():
        return True, 'kite disabled by user 21 Nov - existing GTT left as-is'
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



def _leg_row(leg):
    """Normalise a spread leg into the GTT target shape."""
    if not isinstance(leg, dict):
        return None
    qty = abs(int(leg.get('qty') if leg.get('qty') is not None else leg.get('quantity') or 0))
    if qty <= 0:
        return None
    return dict(broker=leg.get('broker'), book='Option', kind='opt',
                sym=leg.get('sym') or leg.get('trading_symbol') or leg.get('tradingsymbol'),
                key=leg.get('key') or leg.get('instrument_token'),
                qty=qty,
                entry=float(leg.get('entry') or leg.get('average_price') or leg.get('avg') or 0),
                ltp=float(leg.get('ltp') or leg.get('last_price') or 0),
                product=leg.get('product') or ('D' if leg.get('broker') == 'upstox' else 'NRML'),
                exchange=leg.get('exchange') or '')


def place_gtt(row, trig, rec, *, side='SELL', trigger_type='BELOW', limit=None, role=None):
    """Place one single-instrument GTT (SELL or BUY). Never used to SELL a short spread leg."""
    if side == 'SELL' and role == 'cover_short':
        return dict(sym=row['sym'], broker=row['broker'], action='SKIP',
                    why='refusing a SELL GTT on a spread short')
    t = contract_tick(row, trig)
    trig = snap(trig, t)
    if trig is None or trig <= 0:
        return dict(sym=row['sym'], broker=row['broker'], action='SKIP', why='trigger unverified', role=role)
    if trigger_type == 'BELOW' and row.get('ltp') and trig >= row['ltp']:
        return dict(sym=row['sym'], broker=row['broker'], action='SKIP',
                    why='trigger at/above ltp', trig=trig, ltp=row['ltp'], role=role)
    if limit is None:
        limit = snap((trig - max(t, trig * 0.005)) if side == 'SELL' else (trig + max(t, trig * 0.005)), t)
    if row['broker'] == 'upstox':
        body = dict(type='SINGLE', quantity=int(row['qty']), product=row['product'],
                    rules=[dict(strategy='ENTRY', trigger_type=trigger_type, trigger_price=trig)],
                    instrument_token=row['key'], transaction_type=side)
        if DRY:
            return dict(sym=row['sym'], broker='upstox', action='PLACE(dry)', trig=trig,
                        qty=row['qty'], side=side, trigger_type=trigger_type, role=role, limit=limit)
        r = api.call('https://api.upstox.com/v3/order/gtt/place', body=body, proxy=True)
        gid = ((r.get('data') or {}).get('gtt_order_ids') or [None])[0]
        if not gid:
            return dict(sym=row['sym'], broker='upstox', action='FAIL', why=str(r.get('body') or r)[:200],
                        side=side, role=role)
        rec[_k('upstox', row['sym'])] = dict(broker='upstox', gtt_id=gid, trigger=trig, qty=row['qty'],
                               side=side, role=role, placed=datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M'))
        return dict(sym=row['sym'], broker='upstox', action='PLACE', gtt=gid, trig=trig,
                    qty=row['qty'], side=side, trigger_type=trigger_type, role=role)
    if not _kite_writes_allowed():
        return dict(sym=row['sym'], broker='kite', action='SKIP', why='kite disabled by user 21 Nov',
                    trig=trig, qty=row['qty'])
    import broker_kite as bk
    if DRY:
        return dict(sym=row['sym'], broker='kite', action='PLACE(dry)', trig=trig, qty=row['qty'],
                    side=side, trigger_type=trigger_type, role=role, limit=limit)
    try:
        g = bk.client().place_gtt(
            trigger_type=bk.client().GTT_TYPE_SINGLE, tradingsymbol=row['sym'],
            exchange=kite_exchange(row),
            trigger_values=[trig], last_price=row['ltp'],
            orders=[dict(transaction_type=side, quantity=int(row['qty']), product=row['product'],
                         order_type='LIMIT', price=limit)])
    except Exception as e:
        return dict(sym=row['sym'], broker='kite', action='FAIL', why=f'{type(e).__name__}: {str(e)[:180]}',
                    side=side, role=role)
    rec[_k('kite', row['sym'])] = dict(broker='kite', gtt_id=g.get('trigger_id'), trigger=trig, qty=row['qty'],
                           side=side, role=role, placed=datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M'))
    return dict(sym=row['sym'], broker='kite', action='PLACE', gtt=g.get('trigger_id'), trig=trig,
                qty=row['qty'], limit=limit, side=side, trigger_type=trigger_type, role=role)


def cover_short_now(order, dry=None):
    """Immediate LIMIT BUY to close a leftover spread short. Used by morning reconcile."""
    if dry is None:
        dry = DRY
    broker = order.get('broker')
    sym = order.get('sym') or order.get('trading_symbol') or order.get('tradingsymbol')
    qty = int(order.get('quantity') or order.get('qty') or 0)
    ltp = float(order.get('ltp') or order.get('last_price') or 0)
    if qty <= 0:
        return dict(sym=sym, broker=broker, action='SKIP', why='no short quantity')
    if not ltp:
        return dict(sym=sym, broker=broker, action='SKIP', why='no price to cover the short')
    t = tick_for(ltp)
    px = snap(ltp * 1.005, t)
    if dry:
        return dict(sym=sym, broker=broker, action='COVER_NAKED_SHORT(dry)', qty=qty, limit=px,
                    why=order.get('why') or 'spread long is gone; buy back the leftover short')
    if broker == 'upstox':
        token = order.get('key') or order.get('instrument_token')
        body = dict(quantity=qty, product=order.get('product') or 'D', validity='DAY', price=px,
                    tag='spread_cover', instrument_token=token, order_type='LIMIT',
                    transaction_type='BUY', disclosed_quantity=0, trigger_price=0, is_amo=False)
        r = api.call('https://api-hft.upstox.com/v2/order/place', body, 'POST', proxy=True)
        oid = (r.get('data') or {}).get('order_id')
        return dict(sym=sym, broker='upstox', action='COVER_NAKED_SHORT' if oid else 'FAIL',
                    qty=qty, limit=px, order=oid or r)
    if not _kite_writes_allowed():
        return dict(sym=sym, broker='kite', action='SKIP', why='kite disabled by user 21 Nov', qty=qty)
    import broker_kite as bk
    oid = bk.place(order.get('exchange') or 'NFO', sym, 'BUY', qty, px, tag='spread_cover',
                   product=order.get('product') or 'NRML')
    return dict(sym=sym, broker='kite', action='COVER_NAKED_SHORT' if oid else 'FAIL',
                qty=qty, limit=px, order=oid)


def process_spread_pairs(cmd, pairs, live, rec, day_sl):
    """Place or report the paired long-SELL + short-BUY GTTs. Never sells the short."""
    acts = []
    handled = set()
    for pair in pairs or []:
        lg, sh = pair.get('long') or {}, pair.get('short') or {}
        plan = ssp.gtt_spread_plan(lg, sh)
        long_row, short_row = _leg_row(lg), _leg_row(sh)
        if not plan.get('ok') or not long_row or not short_row:
            acts.append(dict(broker=pair.get('broker'), action='SKIP_SPREAD_GTT',
                             why=plan.get('why') or 'spread legs unverified',
                             long=(lg.get('sym') or lg.get('trading_symbol')),
                             short=(sh.get('sym') or sh.get('trading_symbol'))))
            continue
        long_row['broker'] = long_row.get('broker') or pair.get('broker')
        short_row['broker'] = short_row.get('broker') or pair.get('broker')
        handled.add((long_row['broker'], long_row['sym']))
        handled.add((short_row['broker'], short_row['sym']))
        for row, leg in ((long_row, plan['long']), (short_row, plan['short'])):
            key = row['key'] if row['broker'] == 'upstox' else row['sym']
            cur = (live.get(row['broker']) or {}).get(key)
            trig = leg['trigger']
            covered = bool(cur) and cur['trigger'] >= trig - tick_for(trig) - 1e-9
            if cmd == 'status':
                acts.append(dict(sym=row['sym'], broker=row['broker'],
                                 action='KEEP' if covered else 'WOULD_PLACE',
                                 role=leg['role'], side=leg['transaction_type'],
                                 trigger_type=leg['trigger_type'],
                                 gtt=(cur or {}).get('id'), trig=trig, ltp=row['ltp'], qty=row['qty']))
                continue
            sl = (day_sl.get(row['broker']) or {}).get(row['sym'])
            if sl and not COVER_OPEN and leg['transaction_type'] == 'SELL':
                acts.append(dict(sym=row['sym'], broker=row['broker'], action='DEFER',
                                 why='day SL still live - GTT deferred so they cannot both sell',
                                 role=leg['role'], trig=trig, ltp=row['ltp']))
                continue
            if covered:
                acts.append(dict(sym=row['sym'], broker=row['broker'], action='KEEP',
                                 gtt=cur['id'], trig=cur['trigger'], role=leg['role'],
                                 side=leg['transaction_type']))
                continue
            if cur:
                ok, detail = cancel_sym(row['broker'], row['sym'], rec, key=row.get('key'))
                if not ok:
                    acts.append(dict(sym=row['sym'], broker=row['broker'], action='REPLACE_FAIL',
                                     why='old GTT %s could not be cancelled: %s' % (cur['id'], detail),
                                     role=leg['role']))
                    save(rec)
                    continue
            acts.append(place_gtt(row, trig, rec, side=leg['transaction_type'],
                                  trigger_type=leg['trigger_type'], limit=leg.get('limit'),
                                  role=leg['role']))
            save(rec)
    return acts, handled


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
    wants = set(a[1:])
    try:
        _SPREAD_LEGS.clear()
        rows = upstox_targets() + kite_targets()
        spread_pairs = ssp.infer_pairs(_SPREAD_LEGS)
        spread_ledger = ssp.load_ledger()
    except Exception as e:
        print(json.dumps({'error': f'book unreadable - nothing placed: {type(e).__name__}: {e}'}))
        return 2
    if wants:
        rows = [r for r in rows if r['sym'] in wants]
        missing = sorted(wants - {r['sym'] for r in rows})
        if missing:
            print(json.dumps({'error': 'not in the overnight-GTT book: ' + ','.join(missing)}))
            return 2
    try:
        live_u, live_k = upstox_gtts(), kite_gtts()
    except Exception as e:
        print(json.dumps({'error': f'GTT list unreadable - nothing placed: {type(e).__name__}: {e}'}))
        return 2
    live = {'upstox': live_u, 'kite': live_k}
    day_sl = {'upstox': {}, 'kite': {}}
    if cmd == 'place':
        try:
            day_sl = {'upstox': upstox_day_sls(), 'kite': kite_day_sls()}
        except Exception as e:
            print(json.dumps({'error': f'day-SL book unreadable - nothing placed: {type(e).__name__}: {e}'}))
            return 2
    qs = quotes([r['key'] for r in rows if r['broker'] == 'upstox']) if any(r['broker'] == 'upstox' for r in rows) else {}
    acts = []
    # t691u: buy back a leftover spread short first (9:15 reconcile and every place/status).
    naked = ssp.reconcile_naked_shorts(_SPREAD_LEGS, ledger=spread_ledger, pairs=spread_pairs)
    for plan in naked:
        order = dict(plan.get('order') or {}, broker=plan.get('broker'))
        if cmd == 'status' or DRY:
            acts.append(dict(sym=plan.get('short_sym'), broker=plan.get('broker'),
                             action='WOULD_COVER_NAKED_SHORT' if (cmd == 'status' or DRY) else 'COVER_NAKED_SHORT(dry)',
                             qty=plan.get('qty'), why=(order.get('why'))))
            continue
        acts.append(cover_short_now(order, dry=DRY))
    spread_acts, spread_handled = process_spread_pairs(
        'status' if cmd == 'status' else ('status' if cmd == 'reconcile' else cmd),
        spread_pairs, live, rec, day_sl if cmd == 'place' else {'upstox': {}, 'kite': {}})
    # `reconcile` only covers leftover shorts; it does not place new GTTs.
    if cmd != 'reconcile':
        acts.extend(spread_acts)
    if cmd == 'reconcile':
        save(rec)
        print(json.dumps({'now': datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'),
                          'dry_run': DRY, 'command': 'reconcile', 'actions': acts}, indent=1, default=str))
        bad = [x for x in acts if x['action'] in ('FAIL', 'REPLACE_FAIL')]
        if bad:
            print('\nNEEDS ATTENTION:', json.dumps(bad, default=str))
        return 1 if bad else 0
    for row in rows:
        if (row.get('broker'), row.get('sym')) in spread_handled:
            continue
        if row['broker'] == 'upstox' and row['book'] == 'MTF':
            q = qs.get(row['key']) or {}
            row['ltp'] = float(q.get('last_price') or 0) or row['ltp']
        if not row['ltp']:
            acts.append(dict(sym=row['sym'], broker=row['broker'], action='SKIP', why='no price'))
            continue
        if row.get('kind') == 'opt':
            why_s = ssp.gtt_skip_reason(row, ledger=spread_ledger, pairs=spread_pairs)
            if why_s:
                cur_s = live[row['broker']].get(row['key'] if row['broker'] == 'upstox' else row['sym'])
                if cmd == 'status':
                    acts.append(dict(sym=row['sym'], broker=row['broker'],
                                     action='WOULD_CANCEL' if cur_s else 'SKIP_SPREAD',
                                     why=why_s, gtt=(cur_s or {}).get('id')))
                    continue
                if cur_s:
                    ok, detail = cancel_sym(row['broker'], row['sym'], rec, key=row.get('key'))
                    acts.append(dict(sym=row['sym'], broker=row['broker'],
                                     action='CANCEL_SPREAD' if ok else 'CANCEL_FAIL',
                                     why=why_s, detail=detail, gtt=cur_s.get('id')))
                    save(rec)
                    continue
                acts.append(dict(sym=row['sym'], broker=row['broker'], action='SKIP_SPREAD', why=why_s))
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
        sl = (day_sl.get(row['broker']) or {}).get(row['sym'])
        if sl and not COVER_OPEN:
            acts.append(dict(sym=row['sym'], broker=row['broker'], action='DEFER',
                             why='day SL still live - GTT deferred so they cannot both sell',
                             sl=sl.get('oid'), sl_trig=sl.get('trigger'), trig=round(lvl, 2),
                             ltp=row['ltp'], qty=row['qty']))
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
                      'dry_run': DRY, 'cover_open_session': COVER_OPEN, 'targets': len(rows),
                      'actions': acts}, indent=1, default=str))
    bad = [x for x in acts if x['action'] in ('FAIL', 'REPLACE_FAIL')]
    if bad:
        print('\nNEEDS ATTENTION:', json.dumps(bad, default=str))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
