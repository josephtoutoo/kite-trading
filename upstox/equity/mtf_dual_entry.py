#!/usr/bin/env python3
"""Mirrored MTF entry: buy the same stock on Upstox AND Kite as one decision (user, 4 Nov 2026 t305u).

"Tomorrow both upstox and kite should have the same trades." So every new MTF name goes on both
brokers, each sized by ITS OWN book (capital differs, so the share count differs) under the same
rules: the 10% per-stock cap, the 50% MTF budget and the half-room reserve, all measured on the MTF
margin the broker actually blocks, plus that broker's real free margin.

The pair is one decision: if either broker cannot take the trade at all, neither is placed. If one
broker fills and the other does not, the fill is reported loudly and the unfilled leg is cancelled -
a half-filled mirror is a bug, not a position.

Same-day MTF sell block (6 Nov 2026 post-market): if either broker already SOLD this name on MTF
today (mtf_trim / mtf_clear / trailsl / etc.), do not attempt a dual buy. Kite rejects the rebuy for
the rest of the day; placing the Upstox leg alone produced INDHOTEL and TITAN unwinds.

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
import os, sys, json, time, datetime, urllib.parse

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
sys.path.insert(0, '/workspace/upstox')
import mtf_sizing as ms
import trade_lock
import ind
import api                     # one pooled, proxied, timed client (5 Nov 2026)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
REC = os.path.join(D, 'mtf_mirror.json')


def req(u, body=None, method=None):
    """Same contract as before: parsed JSON, or {'status':'error','code':..,'body':..}.
    Runs over the shared keep-alive client, always via the static-IP proxy (this script's
    orders and quotes both need it), with a 25 s timeout and no retry for writes."""
    return api.call(u, body=body, method=method, proxy=True, timeout=25)


def tick(p, t=0.05):
    return round(max(round(p / t) * t, t), 2)


def quote(key):
    d = req('https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')).get('data') or {}
    return list(d.values())[0] if d else {}


LOCK = os.path.join(D, 'mtf_dual_entry.lock')
LIVE_STATUS = ('open', 'open pending', 'trigger pending', 'put order req received',
               'validation pending', 'modify pending', 'complete', 'amo req received')


class AlreadyPlaced(RuntimeError):
    """A BUY for this name is already live or complete today. Two runs racing, or a retry after a
    write whose answer was lost, must never buy the same MTF name twice on one broker."""


class _RunLock:
    """One mtf_dual_entry run at a time. Two concurrent runs (a scan and a mirror, or a routine
    overlapping a manual call) would each read the same pre-order book and each buy the same name."""

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(LOCK)
                    pid = int(open(LOCK).read().strip() or 0)
                except Exception:
                    age, pid = 999, 0
                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except Exception:
                        alive = False
                if age > 900 or not alive:      # stale lock from a killed run
                    try:
                        os.remove(LOCK)
                    except Exception:
                        pass
                    continue
                raise RuntimeError(f'another mtf_dual_entry run holds the lock (pid {pid}, {age:.0f}s old)')
        raise RuntimeError('could not take the entry lock')

    def __exit__(self, *a):
        try:
            os.remove(LOCK)
        except Exception:
            pass
        return False


def _orders_today():
    """(upstox, kite) order lists for today. Raises on a failed read: this feeds the
    "did my order actually go through?" question, where an empty list is the wrong answer."""
    u = req('https://api.upstox.com/v2/order/retrieve-all')
    if u.get('status') != 'success':
        raise RuntimeError(f"Upstox order book unreadable: {u.get('code')} {(u.get('body') or '')[:120]}")
    import broker_kite as bk
    if not bk.valid():
        raise RuntimeError('Kite token invalid')
    return (u.get('data') or []), bk.orders(strict=True)


def _already(symbol, broker=None):
    """An order of MINE still live or already filled today for this name, on one broker or both."""
    u, k = _orders_today()
    tag = 'rotation'
    hits = []
    if broker in (None, 'upstox'):
        want = symbol if symbol.endswith('-EQ') else symbol + '-EQ'
        for o in u:
            if (o.get('transaction_type') == 'BUY' and o.get('product') == 'MTF'
                    and (o.get('tag') or '') in ms.MY_TAGS
                    and o.get('trading_symbol', '').upper() == want.upper()
                    and (o.get('status') or '').lower() in LIVE_STATUS):
                hits.append({'broker': 'upstox', 'order_id': o.get('order_id'),
                             'status': o.get('status'), 'qty': o.get('quantity')})
    if broker in (None, 'kite'):
        for o in k:
            if ((o.get('transaction_type') or '').upper() == 'BUY' and (o.get('product') or '') == 'MTF'
                    and (o.get('tag') or '') in ms.MY_TAGS
                    and (o.get('tradingsymbol') or '').upper() == symbol.upper()
                    and (o.get('status') or '').lower() in LIVE_STATUS):
                hits.append({'broker': 'kite', 'order_id': o.get('order_id'),
                             'status': o.get('status'), 'qty': o.get('quantity')})
    return hits


def mtf_sell_today(symbol, u_orders, k_orders):
    """Brokers that already SOLD this name on MTF today (complete fills).

    Kite refuses a same-day MTF rebuy after any MTF holding sell in that stock.
    Upstox can still accept the buy, which leaves a half-filled mirror (INDHOTEL,
    TITAN on 6 Nov). Pure: callers pass order lists. From main 6d47f05.
    """
    base = (symbol or '').upper().removesuffix('-EQ')
    want_u = base + '-EQ'
    hits = []
    for o in u_orders or []:
        if ((o.get('transaction_type') or '').upper() == 'SELL'
                and (o.get('product') or '').upper() == 'MTF'
                and (o.get('trading_symbol') or '').upper() == want_u
                and (o.get('status') or '').lower() == 'complete'
                and int(o.get('filled_quantity') or o.get('quantity') or 0) > 0):
            hits.append({'broker': 'upstox', 'order_id': o.get('order_id'),
                         'qty': o.get('filled_quantity') or o.get('quantity'),
                         'tag': o.get('tag')})
    for o in k_orders or []:
        if ((o.get('transaction_type') or '').upper() == 'SELL'
                and (o.get('product') or '').upper() == 'MTF'
                and (o.get('tradingsymbol') or '').upper() == base
                and (o.get('status') or '').lower() == 'complete'
                and int(o.get('filled_quantity') or o.get('quantity') or 0) > 0):
            hits.append({'broker': 'kite', 'order_id': o.get('order_id'),
                         'qty': o.get('filled_quantity') or o.get('quantity'),
                         'tag': o.get('tag')})
    return hits


def _mtf_sell_blocked(symbol):
    """Fail closed: an unreadable book, or a sell on either broker, blocks the buy."""
    u, k = _orders_today()
    return mtf_sell_today(symbol, u, k)


def load():
    try:
        return json.load(open(REC))
    except Exception:
        return {}


def save(s):
    json.dump(s, open(REC, 'w'), indent=1)


def usym(sym):
    return sym if sym.endswith('-EQ') else sym + '-EQ'


def _ruleset():
    """t339u: which rule set this run is under (user's own, or the 5-6 Nov two-day override)."""
    try:
        import autonomy
        return autonomy.rule_set()
    except Exception as e:
        return {'rule_set': 'user rules', 'override': f'unavailable ({type(e).__name__})'}


def _symbol_trend(symbol):
    """Completed daily session only. A missing daily read is not eligible, and the
    1-hour series is not a substitute. Stops stay on their own intraday rule."""
    try:
        import mtf_policy as pol
        key = ms.keymap().get(symbol)
        if not key:
            return False, ['no instrument key']
        q = pol.qualify_symbol(key)
        return bool(q.get('eligible')), list(q.get('reasons') or [])
    except Exception as exc:
        return False, ['daily trend unreadable: %s; no intraday fallback' % type(exc).__name__]


def _in_loss(broker, symbol, price, when):
    try:
        st = ms.cap_state(broker, when)
        qty = int((st.get('qty') or {}).get(symbol) or 0)
        cost = float((st.get('held') or {}).get(symbol) or 0)
    except Exception:
        return True
    if qty <= 0 or cost <= 0:
        return False
    return float(price) < cost / qty


def plan(symbol, price, mode='new', qty=None, when=None):
    """Size and gate both legs, no orders. From 9 Nov both legs must clear the Monday
    rule (trend, name cap, initial/add pools) or neither is eligible."""
    try:
        import mtf_policy as pol
    except Exception as exc:
        pol = None
        pol_err = '%s: %s' % (type(exc).__name__, exc)
    else:
        pol_err = None
    active = bool(pol and (pol.policy_active(when) or not pol.date_readable(when)))
    if pol is None and when is None:
        # Without the policy module, a Monday clock must not fall through to the old caps.
        ist_now = datetime.datetime.now(IST).date()
        if ist_now >= datetime.date(2026, 11, 9):
            return {'symbol': symbol, 'price': price, 'mode': mode, 'both_ok': False,
                    'action': 'NOT PLACED: Monday policy module missing',
                    'legs': {'upstox': {'error': pol_err}, 'kite': {'error': pol_err}}}
    trend_ok, trend_why = (True, []) if not active else _symbol_trend(symbol)
    out = {'symbol': symbol, 'price': price, 'mode': mode, 'now': datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'),
           'rule_set': _ruleset(), 'monday': active, 'trend_ok': trend_ok, 'trend': trend_why}
    if active:
        out['policy'] = pol.POLICY_TEXT
    legs = {}
    for b in ('upstox', 'kite'):
        try:
            losing = _in_loss(b, symbol, price, when) if active else False
            sz = ms.size(b, symbol, price, mode, when=when, trend_ok=trend_ok, in_loss=losing)
            legs[b] = {'size': sz, 'in_loss': losing}
            q = qty if qty else sz['qty']
            if q and q != sz['qty']:
                legs[b]['check'] = ms.check(b, symbol, q, price, mode, when=when,
                                            trend_ok=trend_ok, in_loss=legs[b].get('in_loss'))
                legs[b]['requested'] = q
            elif q:
                legs[b]['requested'] = q
            else:
                legs[b]['blocked'] = sz.get('reason') or 'size 0'
        except Exception as e:
            legs[b] = {'error': f'{type(e).__name__}: {e}'}
    both = all(legs[b].get('requested') and legs[b]['size'].get('verdict') == 'OK'
               and (legs[b].get('check', {}).get('allowed', True)) for b in legs if 'error' not in legs[b])
    both = both and all('error' not in legs[b] for b in ('upstox', 'kite'))
    if active and pol is not None:
        gate = pol.paired_decision(
            {'ok': legs.get('upstox', {}).get('size', {}).get('verdict') == 'OK' and legs['upstox'].get('check', {}).get('allowed', True),
             'qty': legs.get('upstox', {}).get('requested') or 0,
             'blocks': (legs.get('upstox', {}).get('check') or {}).get('blocks'),
             'error': legs.get('upstox', {}).get('error')},
            {'ok': legs.get('kite', {}).get('size', {}).get('verdict') == 'OK' and legs['kite'].get('check', {}).get('allowed', True),
             'qty': legs.get('kite', {}).get('requested') or 0,
             'blocks': (legs.get('kite', {}).get('check') or {}).get('blocks'),
             'error': legs.get('kite', {}).get('error')})
        out['mirror_gate'] = gate
        both = bool(both and gate['place'] and trend_ok)
    out['legs'] = legs
    out['both_ok'] = bool(both)
    if qty and not both:
        out['note'] = 'a fixed qty does not fit both books - each leg is sized by its own capital instead'
    return out


def sl_level(key, ltp, entry, now=None):
    """Protective stop after a fill.

    Through 8 Nov 2026 IST: 5% below price, or the 1-hour Supertrend when that is
    tighter. From 9 Nov: the completed daily ratchet. A missing daily bar does not
    switch back to the 1-hour series."""
    try:
        import mtf_policy as pol
        if pol.stop_mode(now) == 'daily':
            plan = pol.position_stop(key, ltp, position_qty=1, allow_trail=True, now=now)
            if plan.get('place_stop') and plan.get('trigger'):
                return round(float(plan['trigger']), 2), plan
            if plan.get('exit'):
                return None, plan
            if plan.get('trigger'):
                return round(float(plan['trigger']), 2), plan
            return round(ltp * (1 - pol.EMERGENCY_FLOOR_PCT / 100.0), 2), plan
    except Exception as e:
        print('mtf_dual_entry: daily stop plan failed for %s, legacy 5%% floor kept: %s: %s' % (
            key, type(e).__name__, e), file=sys.stderr)
        return round(ltp * 0.95, 2), None
    try:
        a = ind.analyze(key, 'minutes', 60, 20)
        st = a['st']
        cand = max(ltp * 0.95, st) if st < ltp else ltp * 0.95
        return round(min(cand, ltp * 0.999), 2), a
    except Exception as e:
        # Falls back to the flat 5% stop, which is a DIFFERENT level than the rule picks - log it.
        print(f'mtf_dual_entry: 1h Supertrend unavailable for {key}, stop uses the flat 5%: {type(e).__name__}: {e}',
              file=sys.stderr)
        return round(ltp * 0.95, 2), None


def buy(symbol, price, mode='new', qty=None, _p=None):
    with _RunLock():
        return _buy(symbol, price, mode, qty)


def _placed_leg(resp, symbol, tag):
    """Did this write land? A timeout/connection reset gives no order id but the order may well be
    live on the broker, so ask the order book by tag+symbol before concluding nothing happened."""
    oid = (resp.get('data') or {}).get('order_id') if isinstance(resp, dict) else None
    if oid:
        return oid, 'placed'
    try:
        hits = _already(symbol)
    except Exception as e:
        return None, f'write answer unclear and the order book is unreadable ({type(e).__name__}: {e}) - CHECK THE BROKER before retrying'
    same = [h for h in hits if h['broker'] == 'upstox']
    if same:
        return same[0]['order_id'], 'recovered from the order book after an unclear write'
    return None, (resp.get('body') if isinstance(resp, dict) else str(resp)) or 'no order id'


def _buy(symbol, price, mode='new', qty=None):
    try:
        prior = _already(symbol)
    except Exception as e:
        # cannot prove the name is not already being bought, so do not buy it
        return {'symbol': symbol, 'action': f'NOT PLACED: could not read the order book to rule out a duplicate buy ({type(e).__name__}: {e})'}
    if prior:
        return {'symbol': symbol, 'action': 'NOT PLACED: a BUY of this name is already live/filled today - ' +
                ', '.join(f"{h['broker']} {h['order_id']} ({h['status']})" for h in prior), 'already': prior}
    try:
        sold = _mtf_sell_blocked(symbol)
    except Exception as e:
        return {'symbol': symbol, 'action': 'NOT PLACED: could not read the order book to rule out a same-day MTF sell (%s: %s)' % (type(e).__name__, e)}
    if sold:
        return {'symbol': symbol, 'action': 'NOT PLACED: same-day MTF sell on ' +
                ', '.join('%s %s' % (h['broker'], h['order_id']) for h in sold) +
                ' - Kite refuses rebuy today; mirror would be half-filled', 'mtf_sell_today': sold}
    p = plan(symbol, price, mode, qty)
    p.setdefault('rule_set', _ruleset())
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
                                               'product': 'MTF', 'quantity': uq, 'price': price,
                                               '_trend_ok': p.get('trend_ok') is True,
                                               '_in_loss': bool((p.get('legs') or {}).get('upstox', {}).get('in_loss'))})
    p['legs']['upstox']['trade_lock'] = {'allowed': ok, 'reasons': reasons, 'adjusted_qty': adj}
    if not ok or adj <= 0:
        p['action'] = 'NOT PLACED: Upstox trade_lock refused - ' + '; '.join(reasons)
        return p
    uq = adj
    body = {'quantity': int(uq), 'product': 'MTF', 'validity': 'DAY', 'price': price, 'tag': 'rotation',
            'instrument_token': key, 'order_type': 'LIMIT', 'transaction_type': 'BUY',
            'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}
    r = req('https://api-hft.upstox.com/v2/order/place', body, 'POST')
    oid, how = _placed_leg(r, symbol, 'rotation')
    ms.forget()          # the book has moved: never let the next read reuse a pre-order snapshot
    p['legs']['upstox']['order_id'] = oid
    p['legs']['upstox']['response'] = how if oid else {'code': r.get('code'), 'body': r.get('body')}
    # --- Kite leg ---
    if oid:
        import broker_kite as bk
        try:
            m = bk.from_upstox(key)
        except Exception as e:
            print(f'mtf_dual_entry: Kite symbol map failed for {key}: {type(e).__name__}: {e}', file=sys.stderr)
            m = None
        if not m:
            # Equity cash keys are not in the F&O map; resolve via NSE dump (same as _kite_eq).
            m = _kite_eq(symbol)
        if not m:
            p['legs']['kite']['order_id'] = None
            p['legs']['kite']['error'] = 'no Kite symbol for ' + key
        else:
            kok, kre, kadj = trade_lock.check_order({'trading_symbol': m['tradingsymbol'],
                                                     'instrument_token': 'KITE|%s|%s' % (m['exchange'], m['tradingsymbol']),
                                                     'transaction_type': 'BUY', 'order_type': 'LIMIT',
                                                     'product': 'MTF', 'quantity': kq, 'price': price,
                                                     '_trend_ok': p.get('trend_ok') is True,
                                                     '_in_loss': bool((p.get('legs') or {}).get('kite', {}).get('in_loss'))}, broker='kite')
            p['legs']['kite']['trade_lock'] = {'allowed': kok, 'reasons': kre, 'adjusted_qty': kadj}
            if kok and kadj > 0:
                kid = bk.place(m['exchange'], m['tradingsymbol'], 'BUY', kadj, price, tag='rotation', product='MTF')
                if not kid:
                    # Same rule as the Upstox write: a None here can hide an order that did land.
                    try:
                        hits = [h for h in _already(symbol, broker='kite')]
                    except Exception as e:
                        hits = []
                        p['legs']['kite']['error'] = f'Kite write answer lost and the book is unreadable ({e}) - CHECK KITE'
                    if hits:
                        kid = hits[0]['order_id']
                        p['legs']['kite']['recovered'] = 'order found in the Kite book after an unclear write'
                p['legs']['kite']['order_id'] = kid
                kq = kadj
            else:
                p['legs']['kite']['error'] = 'trade_lock refused: ' + '; '.join(kre)
    if not kid:
        # never leave a half mirror: undo the Upstox leg, by cancelling it if it is still live or by
        # selling back whatever already filled, and say exactly which happened
        if oid:
            ms.forget()
            d0 = (req('https://api.upstox.com/v2/order/details?order_id=' + oid).get('data') or {})
            st0 = (d0.get('status') or '').lower()
            fq = int(d0.get('filled_quantity') or 0)
            try:
                import mtf_policy as _unwind
                decision = _unwind.mirror_unwind(oid, fq, kid)
            except Exception:
                decision = {'action': 'SELL_BACK' if fq > 0 else 'CANCEL', 'qty': fq}
            if decision.get('action') == 'SELL_BACK':
                r3 = req('https://api-hft.upstox.com/v2/order/place', {
                    'quantity': fq, 'product': 'MTF', 'validity': 'DAY', 'price': price, 'tag': 'rotation',
                    'instrument_token': key, 'order_type': 'LIMIT', 'transaction_type': 'SELL',
                    'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}, 'POST')
                p['unwind_upstox'] = {'qty': fq, 'sell_order': (r3.get('data') or {}).get('order_id'),
                                      'resp': None if (r3.get('data') or {}).get('order_id') else r3}
                p['action'] = (f'Kite leg failed AFTER Upstox filled {fq}: sold {fq} back on Upstox to keep the '
                               f'mirror whole - check that the sell-back filled')
            elif decision.get('action') == 'CANCEL':
                req('https://api-hft.upstox.com/v2/order/cancel?order_id=' + oid, None, 'DELETE')
                p['action'] = f'Upstox order cancelled ({st0}): the Kite leg could not be placed'
        else:
            p['action'] = 'nothing placed'
        save({**load(), f"{symbol}:{datetime.datetime.now(IST).date().isoformat()}": p})
        return p
    p['action'] = f'PLACED both legs: Upstox {uq} @ {price}, Kite {kq} @ {price}'
    # --- fills + stops ---
    # Wait for the fills to settle before sizing the stop. Placing it on the first partial slice
    # (the old fixed 8 s sleep) left the rest of the order naked whenever the fill was slower than
    # that, which on a thin MTF name is common. Bounded so the run still reports promptly.
    p['fills'], d, ks, settled = _settle(oid, kid, price)
    ufill = int(d.get('filled_quantity') or 0); uavg = float(d.get('average_price') or price)
    kfill = int(ks.get('filled') or 0); kavg = float(ks.get('avg') or price)
    if not settled:
        p['partial'] = ('fill did not finish while this run watched: the stop covers what filled, and '
                        'the stop scripts pick up the rest on their next pass')
    st, _ = sl_level(key, uavg, uavg)
    prior_u = int(((p.get('legs') or {}).get('upstox') or {}).get('size', {}).get('position_qty') or 0)
    prior_k = int(((p.get('legs') or {}).get('kite') or {}).get('size', {}).get('position_qty') or 0)
    p['stop_plan'] = {'upstox': st, 'kite': st, 'upstox_qty': prior_u + ufill, 'kite_qty': prior_k + kfill}
    if ufill:
        p['stop_order_upstox'] = _arm_full_stop('upstox', symbol, key, prior_u + ufill, st, uavg)
    if kfill:
        p['stop_order_kite'] = _arm_full_stop('kite', symbol, key, prior_k + kfill, st, kavg)
    save({**load(), f"{symbol}:{datetime.datetime.now(IST).date().isoformat()}": p})
    return p


def _stop_rows(symbol, broker):
    """Live MTF SL sells for this name, or None when the book cannot be read.
    None is not an empty book: an empty book is a real 'no stop yet'."""
    try:
        u, k = _orders_today()
    except Exception:
        return None
    rows = []
    if broker == 'upstox':
        want = usym(symbol).upper()
        for o in u:
            if ((o.get('transaction_type') or '').upper() == 'SELL' and (o.get('product') or '').upper() == 'MTF'
                    and (o.get('trading_symbol') or '').upper() == want
                    and (o.get('status') or '').lower() in LIVE_STATUS
                    and (o.get('order_type') or '').upper() in ('SL', 'SL-M')):
                rows.append({'qty': int(o.get('quantity') or 0), 'trigger': o.get('trigger_price'),
                             'order_id': o.get('order_id')})
    else:
        for o in k:
            if ((o.get('transaction_type') or '').upper() == 'SELL' and (o.get('product') or '').upper() == 'MTF'
                    and (o.get('tradingsymbol') or '').upper() == symbol.upper()
                    and (o.get('status') or '').lower() in ('open', 'trigger pending', 'open pending')
                    and (o.get('order_type') or '').upper() in ('SL', 'SL-M')):
                rows.append({'qty': int(o.get('quantity') or 0), 'trigger': o.get('trigger_price'),
                             'order_id': o.get('order_id')})
    return rows


def _arm_full_stop(broker, symbol, key, full_qty, proposed, avg):
    """Place or keep one stop for the whole position. Never lowers a trigger.
    Does not stack a second stop on top of a short one — the short one is cancelled first."""
    try:
        import mtf_policy as pol
    except Exception as exc:
        return {'verified': False, 'error': 'policy module missing: %s' % exc}
    existing = _stop_rows(symbol, broker)
    if existing is None:
        return {'verified': False, 'qty': int(full_qty), 'action': 'UNVERIFIED',
                'reason': 'stop book unreadable — not placing a stop that might double-sell'}
    cov = pol.stop_coverage(full_qty, proposed, existing)
    if cov['action'] == 'KEEP':
        return dict(cov, verified=True, order_id=(existing[0].get('order_id') if existing else None))
    if cov['action'] not in ('PLACE', 'REPLACE') or not cov.get('trigger'):
        return dict(cov, verified=False)
    if cov['action'] == 'REPLACE':
        for row in existing:
            if broker == 'upstox':
                ans = req('https://api-hft.upstox.com/v2/order/cancel?order_id=' + str(row['order_id']), None, 'DELETE')
                if not (isinstance(ans, dict) and (ans.get('status') == 'success' or (ans.get('data') or {}).get('order_id'))):
                    return dict(cov, verified=False, error='could not cancel the short stop; no second stop placed',
                                cancel=ans)
            else:
                import broker_kite as bk
                if not bk.cancel(row['order_id']):
                    return dict(cov, verified=False, error='could not cancel the short Kite stop; no second stop placed')
    trigger = cov['trigger']
    if broker == 'upstox':
        ok2, why2, adj2 = trade_lock.check_order({'trading_symbol': usym(symbol), 'instrument_token': key,
                                                  'transaction_type': 'SELL', 'order_type': 'SL', 'product': 'MTF',
                                                  'quantity': int(cov['qty'])})
        if not ok2 or adj2 < int(cov['qty']):
            return dict(cov, verified=False, error='; '.join(why2) or 'trade_lock reduced the full-quantity stop')
        trig = tick(trigger, 0.10 if avg >= 1000 else 0.05)
        if trig < trigger:
            trig = tick(trigger + (0.10 if avg >= 1000 else 0.05), 0.10 if avg >= 1000 else 0.05)
        lim = tick(trig * 0.995, 0.10 if avg >= 1000 else 0.05)
        r2 = req('https://api-hft.upstox.com/v2/order/place', {
            'quantity': int(adj2), 'product': 'MTF', 'validity': 'DAY', 'price': lim, 'tag': 'trailsl',
            'instrument_token': key, 'order_type': 'SL', 'transaction_type': 'SELL',
            'disclosed_quantity': 0, 'trigger_price': trig, 'is_amo': False}, 'POST')
        oid = (r2.get('data') or {}).get('order_id')
        return dict(cov, verified=bool(oid) and int(adj2) >= int(cov['qty']), order_id=oid, trigger=trig, limit=lim,
                    response=None if oid else {'code': r2.get('code'), 'body': r2.get('body')})
    import broker_kite as bk
    m2 = bk.from_upstox(key) or _kite_eq(symbol)
    if not m2:
        return dict(cov, verified=False, error='no Kite symbol for stop on ' + symbol)
    ktick = bk.tick_for(m2['exchange'], m2['tradingsymbol']) or 0.05
    ktrig = round(round(float(trigger) / ktick) * ktick, 2)
    if ktrig + 1e-9 < float(trigger):
        ktrig = round(round((float(trigger) + ktick) / ktick) * ktick, 2)
    klim = round(round((ktrig - 5 * ktick) / ktick) * ktick, 2)
    ksid = bk.place(m2['exchange'], m2['tradingsymbol'], 'SELL', int(cov['qty']), klim, order_type='SL',
                    trigger=ktrig, tag='trailsl', product='MTF')
    return dict(cov, verified=bool(ksid), order_id=ksid, trigger=ktrig, limit=klim)


def _settle(oid, kid, price, budget=75.0, step=5.0):
    """Poll both orders until each is terminal or `budget` seconds pass. Returns
    (fills, upstox_details, kite_status, settled). Never raises."""
    end = time.time() + budget
    d = {}; ks = {}
    terminal = ('complete', 'cancelled', 'rejected', 'complete', 'trigger pending', 'open pending')
    while True:
        try:
            d = (req('https://api.upstox.com/v2/order/details?order_id=' + oid).get('data') or {}) or d
        except Exception:
            pass
        try:
            import broker_kite as bk
            ks = bk.order_status(kid) or ks
        except Exception:
            pass
        ust = (d.get('status') or '').lower()
        kst = (ks.get('status') or '').lower()
        u_done = ust in ('complete', 'cancelled', 'rejected', 'partially filled') and \
            int(d.get('filled_quantity') or 0) >= int(d.get('quantity') or 0)
        k_done = kst in ('complete', 'cancelled', 'rejected') and \
            int(ks.get('filled') or 0) >= int(ks.get('quantity') or ks.get('filled') or 0)
        if (ust in ('complete', 'cancelled', 'rejected') and kst in ('complete', 'cancelled', 'rejected')):
            return ({'upstox': {'status': d.get('status'), 'qty': int(d.get('filled_quantity') or 0),
                                'avg': float(d.get('average_price') or price)},
                     'kite': {'status': ks.get('status'), 'qty': int(ks.get('filled') or 0),
                              'avg': float(ks.get('avg') or price)}}, d, ks, True)
        if time.time() >= end:
            return ({'upstox': {'status': d.get('status'), 'qty': int(d.get('filled_quantity') or 0),
                                'avg': float(d.get('average_price') or price)},
                     'kite': {'status': ks.get('status'), 'qty': int(ks.get('filled') or 0),
                              'avg': float(ks.get('avg') or price)}}, d, ks, False)
        time.sleep(step)


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
    """(counted, all) MTF names with qty>0. Before 9 Nov the counted set is bot-tagged names.
    From 9 Nov every positive net name counts, tag or not."""
    st = ms.state(broker)
    mine = st.get('mine') or set()
    held = {s: v for s, v in st['held'].items() if v > 0}
    if ms._monday(None):
        return set(held), held
    return {s for s in held if s in mine}, held


def mirror(apply=False):
    """Make the two books hold the same MTF names, without touching the user's own names."""
    out = {'now': datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'), 'apply': apply,
           'rule_set': _ruleset()}
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
    with _RunLock():
        return _apply_plan(out)


def _apply_plan(out):
    placed = []
    for leg in out['plan']:
        # the plan was computed before the lock was taken, so re-check the book per name right before
        # buying: a leg that another run already filled must not be bought again
        try:
            dup = _already(leg['symbol'], broker=leg['broker'])
        except Exception as e:
            placed.append({'symbol': leg['symbol'], 'broker': leg['broker'], 'order_id': None,
                           'resp': f'skipped: could not rule out a duplicate buy ({type(e).__name__}: {e})'})
            continue
        if dup:
            placed.append({'symbol': leg['symbol'], 'broker': leg['broker'], 'order_id': None,
                           'resp': f"skipped: already {'/'.join(sorted({d['broker'] for d in dup}))} "
                                   f"{dup[0]['order_id']} ({dup[0]['status']})"})
            continue
        res = _place_mirror_leg(leg)
        placed.append(res)
    out['placed'] = placed
    ms.forget()          # same reason: the stop arming below must read the post-order book
    ok = [p for p in placed if p.get('order_id')]
    out['action'] = f'placed {len(ok)}/{len(placed)} missing leg(s)'
    # ---- arm protective stops on both books ----
    out['stops'] = _arm_stops()
    save({**load(), f"mirror:{datetime.datetime.now(IST).date().isoformat()}": out})
    return out


def _mirror_leg(sym, broker, books, count, committed):
    """Size + gate the leg that broker is missing. Returns the plan for it, never an order."""
    leg = {'symbol': sym, 'broker': broker}
    # hard guard 1: before 9 Nov, never touch a name the user owns themselves.
    # From 9 Nov that lot counts, and a missing leg may be mirrored if the caps allow.
    if not ms._monday(None):
        for b in ('upstox', 'kite'):
            st = ms.state(b)
            if sym in (st.get('held') or {}) and sym not in (st.get('mine') or set()):
                leg['skip'] = f'{sym} is the user\'s own holding on {b} - not touched'
                return leg
    # hard guard 2: never buy a name a broker already holds (either book, mine or the user's)
    if sym in books[broker]['held']:
        leg['skip'] = f'{broker} already holds {sym}'
        return leg
    try:
        sold = _mtf_sell_blocked(sym)
    except Exception as e:
        leg['skip'] = 'could not rule out same-day MTF sell (%s: %s)' % (type(e).__name__, e)
        return leg
    if sold:
        leg['skip'] = 'same-day MTF sell on ' + ', '.join('%s %s' % (h['broker'], h['order_id']) for h in sold)
        leg['mtf_sell_today'] = sold
        return leg

    # hard guard 3: the name cap. Before 9 Nov that is 5 names this bot opened.
    # From 9 Nov it is 5 actual positive names, pending buys included. Five is the
    # maximum, not a target. The same-day MTF sell guard already ran above.
    cap = books[broker]['slots'].get('cap') or ms.max_names()
    if count[broker] >= cap:
        leg['skip'] = f'{broker} is at the cap ({count[broker]}/{cap} of my MTF names) - no free slot'
        return leg
    key, px = _ltp(sym)
    if not px:
        leg['skip'] = 'no Upstox price'
        return leg
    leg['price'] = px
    leg['instrument_key'] = key
    trend_ok = None
    if ms._monday(None):
        trend_ok, why_t = _symbol_trend(sym)
        if not trend_ok:
            leg['skip'] = 'trend gate: ' + '; '.join(why_t)
            return leg
        trend_ok = True
    try:
        sz = ms.size(broker, sym, px, 'new', trend_ok=trend_ok)
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
        ck = ms.check(broker, sym, q, px, 'new', trend_ok=trend_ok)
    except Exception as e:
        leg['skip'] = f'check failed: {type(e).__name__}: {e}'
        return leg
    leg['check'] = {k: ck.get(k) for k in ('allowed', 'blocks', 'warns', 'margin', 'notional', 'max_qty')}
    if not ck.get('allowed'):
        leg['skip'] = '; '.join(ck.get('blocks') or []) or 'blocked by mtf_sizing'
        return leg
    # cumulative guard: legs planned earlier in THIS run already consume budget on that broker
    st = ms.cap_state(broker) if ms._monday(None) else ms.state_mine(broker)
    cap = float(st['capital'])
    ratio = float(sz['limits'].get('ratio') or (sz['margin'] / sz['amount'] if sz.get('amount') else 1.0))
    used_m = float(st['used']) * ratio
    if ms._monday(None):
        room_total = float((sz.get('limits') or {}).get('room_initial') or 0) - committed[broker]
        half = max(room_total, 0.0)
        leg_pool = 'initial 20%'
    else:
        room_total = max(0.5 * cap - used_m - committed[broker], 0.0)
        half = room_total * 0.5
        leg_pool = 'half of remaining'
    need = float(sz['margin'])
    leg['budget_after_prior_legs'] = {'committed_margin': round(committed[broker]),
                                      'room_total': round(room_total), 'need': round(need),
                                      'half_room': round(half), 'pool': leg_pool}
    if need > half:
        leg['skip'] = (f"after the legs already planned this run, only {half:,.0f} of the "
                       f"{leg_pool} is left on {broker} but this leg needs {need:,.0f} margin")
        return leg
    committed[broker] += need
    # trade_lock, on the right broker's book
    if broker == 'upstox':
        o = {'trading_symbol': usym(sym), 'instrument_token': key, 'transaction_type': 'BUY',
             'order_type': 'LIMIT', 'product': 'MTF', 'quantity': q, 'price': px,
             '_trend_ok': trend_ok is True}
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
             '_trend_ok': trend_ok is True,
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
        ms.forget()
        return {'symbol': sym, 'broker': broker, 'qty': q, 'order_id': oid,
                'resp': None if oid else {'code': r.get('code'), 'body': r.get('body')}}
    import broker_kite as bk
    m = leg.get('kite_symbol') or _kite_eq(sym)
    if not m:
        return {'symbol': sym, 'broker': broker, 'qty': q, 'order_id': None, 'resp': 'no Kite symbol'}
    oid = bk.place(m['exchange'], m['tradingsymbol'], 'BUY', q, px, tag='rotation', product='MTF')
    ms.forget()
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
