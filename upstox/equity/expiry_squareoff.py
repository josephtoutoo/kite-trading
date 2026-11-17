#!/usr/bin/env python3
"""Close expiry-week option positions the same day (user rule, 4 Nov 2026).

Stock and MCX commodity options are intraday-only in the calendar week the contract
actually expires (Monday-start ISO week that contains the expiry date). A rolling
"within 7 calendar days" window is NOT used: Mon 16 Nov is not expiry week for a
Mon 23 Nov contract. Index options are skipped. --only equity / --only mcx split
the NSE-BSE and MCX sessions. A marketable LIMIT is used so we control the exit
instead of leaving it to the broker's auto square-off.

The Friday 15:10 IST flatten of stock options is a separate rule and is not
implemented here; this script only fires in the contract's own expiry week.
Next-month debit-spread legs (t687u) are never squared off here: a December
contract is not in November's expiry week, and recorded spread legs are
skipped even if the expiry parse were wrong.

Open user LIMIT SELLs at or above market are left alone unless a genuine
expiry-week flatten still needs the quantity and the resting limit is not
marketable (too far above LTP to exit now).

CLI: python3 expiry_squareoff.py [--dry | --dry-run] [--only equity|mcx]
"""
import sys, json, datetime, os
sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, '/workspace/equity')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stockopt_spread as ssp

DRY = ('--dry' in sys.argv) or ('--dry-run' in sys.argv)
# MCX runs a separate, much later session (crude till ~23:30 IST), so commodity options are
# squared off by their own late run, not by the 15:10 one that handles NSE/BSE.
# --only equity  -> NSE/BSE legs only (the 15:10 run)
# --only mcx     -> MCX legs only (the ~23:10 run)
ONLY = None
if '--only' in sys.argv:
    i = sys.argv.index('--only')
    ONLY = sys.argv[i + 1].lower() if i + 1 < len(sys.argv) else None
# User rule (4 Nov 2026, refined t266u): the expiry-week intraday-only rule applies to
# STOCK options only. Index options (NIFTY/BANKNIFTY/SENSEX/FINNIFTY/MIDCPNIFTY/BANKEX/
# NIFTYNXT50) are excluded and may still be carried.
# MCX commodity options are also intraday-only (user, 4 Nov 2026 t268u).
INDEX_UND = {'NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX', 'NIFTYNXT50'}
COMMODITY_UND = {'CRUDEOIL', 'CRUDEOILM', 'GOLD', 'GOLDM', 'GOLDGUINEA', 'GOLDPETAL', 'SILVER',
                 'SILVERM', 'SILVERMIC', 'NATURALGAS', 'NATGASMINI', 'COPPER', 'ZINC', 'LEAD',
                 'NICKEL', 'ALUMINIUM', 'MENTHAOIL', 'COTTONCNDY', 'COTTON', 'CASTORSEED'}


def underlying_of(tsym):
    """Root symbol: leading letters (plus & and -) before the first digit, in both the
    Kite compact form (BANKNIFTY26NOV55700CE, CRUDEOIL26NOV8900PE) and the Upstox spaced
    form (NIFTY 23950 CE 09 NOV 26, CRUDEOIL 8900 PE 19 NOV 26)."""
    import re
    s = (tsym or '').upper().replace(' ', '').replace('-', '')
    m = re.match(r'[A-Z&]+', s)
    return m.group(0) if m else s


def _starts_with_any(tsym, roots):
    s = (tsym or '').upper().replace(' ', '')
    return any(s.startswith(r) for r in sorted(roots, key=len, reverse=True))


def is_index_opt(tsym):
    return _starts_with_any(tsym, INDEX_UND)


def is_commodity_opt(tsym):
    return _starts_with_any(tsym, COMMODITY_UND)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# A resting LIMIT SELL is close enough to market to leave in place when the
# price is at or only slightly above LTP (within a 1% band).
_MARKETABLE_BAND = 0.01
_WORKING = {
    'open', 'trigger pending', 'pending', 'open pending', 'validation pending',
    'put order req received', 'modified', 'after market order received',
}


def in_expiry_week(exp, today=None):
    """True only when today is in the Monday-start calendar week that contains exp.

    Expired contracts (exp < today) are not expiry-week. Same ISO year+week is
    required, so the Friday before a Monday expiry does not fire.
    """
    if today is None:
        today = datetime.datetime.now(IST).date()
    if not exp:
        return False
    if isinstance(exp, str):
        try:
            exp = datetime.date.fromisoformat(exp[:10])
        except Exception:
            return False
    if exp < today:
        return False
    return exp.isocalendar()[:2] == today.isocalendar()[:2]


def user_limit_sell_cancel_needed(limit_price, ltp, flattening=False, marketable_band=_MARKETABLE_BAND):
    """Do not cancel a user LIMIT SELL at or above market unless needed.

    Needed only for a genuine expiry-week flatten when the resting limit is
    too far above LTP to exit the position now. A sell at or below LTP, or
    within the marketable band above LTP, is left in place.
    """
    try:
        limit_price = float(limit_price)
        ltp = float(ltp)
    except (TypeError, ValueError):
        return False
    if ltp <= 0 or limit_price <= 0:
        return False
    if not flattening:
        return False
    ceiling = ltp * (1 + marketable_band)
    # at or below market, or only slightly above: leave it
    if limit_price <= max(ceiling, ltp):
        return False
    return True


def _norm_sym(sym):
    return (sym or '').replace(' ', '').replace('-', '').upper()


def _order_sym(order):
    return order.get('trading_symbol') or order.get('tradingsymbol') or order.get('symbol') or ''


def _opt_type(sym):
    """CE/PE from compact (...880CE) or spaced (INDHOTEL880CE23NOV26) forms."""
    import re
    m = re.search(r'(CE|PE)', _norm_sym(sym))
    return m.group(1) if m else None


def _same_contract(a, b):
    """True when two symbols name the same contract, including compact vs spaced forms."""
    if _norm_sym(a) == _norm_sym(b) and _norm_sym(a):
        return True
    ra, rb = underlying_of(a), underlying_of(b)
    ta, tb = _opt_type(a), _opt_type(b)
    sa, sb = strike_of(a), strike_of(b)
    return bool(ra and ra == rb and ta and ta == tb and sa is not None and sa == sb)


def _order_working(order):
    st = (order.get('status') or '').lower().replace('_', ' ').strip()
    return st in _WORKING


def _order_qty(order):
    try:
        pending = order.get('pending_quantity')
        if pending is not None and str(pending) != '':
            q = int(float(pending))
            if q > 0:
                return q
        filled = int(float(order.get('filled_quantity') or 0))
        qty = int(float(order.get('quantity') or 0))
        return max(qty - filled, 0)
    except (TypeError, ValueError):
        return 0


def plan_open_sells(orders, sym, qty, ltp, flattening=False):
    """Decide which working SELL limits to leave, which to cancel, and residual qty.

    User LIMIT SELLs at or above market are not cancelled unless flattening
    is required and the limit is not marketable. Protective SL/SL-M sells
    that reserve quantity are cancelled only when a genuine flatten still
    needs that quantity.
    """
    leave, cancel = [], []
    remaining = int(qty or 0)
    for o in orders or []:
        if not _same_contract(_order_sym(o), sym):
            continue
        side = (o.get('transaction_type') or o.get('transactionType') or '').upper()
        if side != 'SELL' or not _order_working(o):
            continue
        oqty = _order_qty(o)
        if oqty <= 0:
            continue
        typ = (o.get('order_type') or o.get('orderType') or 'LIMIT').upper()
        typ_compact = typ.replace('_', '').replace(' ', '').replace('-', '')
        price = o.get('price') or o.get('limit_price') or 0
        rec = {'order_id': o.get('order_id') or o.get('orderId'), 'qty': oqty,
               'price': price, 'order_type': typ}
        if typ_compact in ('SL', 'SLM'):
            if flattening:
                cancel.append(dict(rec, why='stop reserves qty needed for expiry-week flatten'))
            else:
                leave.append(dict(rec, why='protective stop left in place'))
            continue
        if user_limit_sell_cancel_needed(price, ltp, flattening=flattening):
            cancel.append(dict(rec, why='limit too far above market to exit now'))
        else:
            leave.append(dict(rec, why='user limit sell at or above market left in place'))
            remaining -= oqty
    return {'leave': leave, 'cancel': cancel, 'sell_qty': max(remaining, 0)}




def expiry_of(tsym):
    """Expiry from an option symbol. Handles Kite (BANKNIFTY26NOV55700CE,
    SENSEX26NOV2681105CE) and Upstox spaced forms (NIFTY 23950 CE 09 NOV 26)."""
    import re, calendar
    s = tsym.upper().replace('-', '').strip()
    months = {m: i for i, m in enumerate(
        ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'], 1)}
    spaced = re.search(r'(\d{1,2})\s+([A-Z]{3})\s+(\d{2,4})$', s)
    if spaced:
        d, mon, yy = spaced.group(1), spaced.group(2), spaced.group(3)
        y = int(yy) + (2000 if len(yy) == 2 else 0)
        try:
            return datetime.date(y, months[mon], int(d))
        except Exception:
            return None
    # compact: <underlying><DD><MON><strike><CE|PE>  (Kite drops the year on option symbols)
    m = re.search(r'([A-Z&]+?)(\d{1,2})([A-Z]{3})(\d+)(CE|PE)$', s.replace(' ', ''))
    if not m:
        return None
    d, mon = int(m.group(2)), m.group(3)
    if mon not in months or not (1 <= d <= 31):
        return None
    # no year in the symbol: assume the current one, roll forward if the date has passed
    y = datetime.date.today().year
    if datetime.date(y, months[mon], d) < datetime.date.today() - datetime.timedelta(days=30):
        y += 1
    try:
        datetime.date(y, months[mon], d)
    except Exception:
        return None
    return datetime.date(y, months[mon], d)


_TICK = None


def upstox_tick(instrument_key):
    """Tick size from the Upstox master (MCX uses 5 or 10, not 0.05 - orders get rejected otherwise)."""
    global _TICK
    if _TICK is None:
        import glob
        import api
        m = {}
        for f in glob.glob('/workspace/upstox/instruments_*_*.json.gz'):
            try:
                for x in api.gzip_json(f):
                    k = x.get('instrument_key')
                    if k:
                        try:
                            m[k] = float(x.get('tick_size') or 0.05)
                        except Exception:
                            m[k] = 0.05
            except Exception:
                continue
        _TICK = m
    t = _TICK.get(instrument_key) or 0.05
    return t / 100 if t > 1 else t


_EXP = None
_STRIKE = None


def _expiry_map():
    """trading_symbol -> expiry date, from whichever Upstox master covers the name."""
    global _EXP
    if _EXP is not None:
        return _EXP
    import glob, datetime as _dt, csv
    import api
    out = {}
    # Kite masters carry the authoritative expiry for Kite symbols (compact form)
    for f in glob.glob('/workspace/upstox/kite_instruments_*_*.csv'):
        try:
            for x in csv.DictReader(open(f)):
                if x.get('instrument_type') in ('CE', 'PE') and x.get('expiry'):
                    out[x['tradingsymbol'].replace(' ', '').upper()] = _dt.date.fromisoformat(x['expiry'])
        except Exception as e:
            print(f'expiry_squareoff: Kite master unreadable ({f.split("/")[-1]}), expiries from it missing: '
                  f'{type(e).__name__}: {e}', file=sys.stderr)
            continue
    for f in glob.glob('/workspace/upstox/instruments_*_*.json.gz'):
        try:
            for x in api.gzip_json(f):
                if x.get('instrument_type') in ('CE', 'PE') and x.get('expiry') and x.get('trading_symbol'):
                    out[x['trading_symbol'].replace(' ', '').upper()] = _dt.datetime.fromtimestamp(
                        x['expiry'] / 1000).date()
        except Exception:
            continue
    _EXP = out
    return out


def _strike_index():
    """(root, type, strike) -> sorted expiries, so a compact symbol still finds the right expiry
    when the master only carries the spaced form (MCX: CRUDEOIL 8900 PE 19 NOV 26)."""
    global _STRIKE
    if _STRIKE is not None:
        return _STRIKE
    import glob, datetime as _dt
    import api
    out = {}
    for f in glob.glob('/workspace/upstox/instruments_*_*.json.gz'):
        try:
            for x in api.gzip_json(f):
                if x.get('instrument_type') not in ('CE', 'PE') or not x.get('expiry'):
                    continue
                root = underlying_of(x.get('asset_symbol') or x.get('name') or x.get('trading_symbol') or '')
                try:
                    strike = float(x.get('strike_price') or 0)
                except Exception:
                    continue
                if not root or not strike:
                    continue
                exp = _dt.datetime.fromtimestamp(x['expiry'] / 1000).date()
                out.setdefault((root, x['instrument_type'], strike), set()).add(exp)
        except Exception:
            continue
    _STRIKE = {k: sorted(v) for k, v in out.items()}
    return _STRIKE


def strike_of(tsym):
    """Strike from an option symbol, both forms (CRUDEOIL 8900 PE 19 NOV 26 / CRUDEOIL26NOV8900PE)."""
    import re
    s = (tsym or '').upper().strip()
    m = re.search(r'\s(\d+(?:\.\d+)?)\s+(CE|PE)\s', s)
    if m:
        return float(m.group(1))
    m = re.search(r'([A-Z&]+?)\d{1,2}[A-Z]{3}(\d+(?:\.\d+)?)(CE|PE)$', s.replace(' ', ''))
    return float(m.group(2)) if m else None


def expiry_for(tsym):
    """Exact master lookup, then the strike index, then a symbol parse."""
    key = tsym.replace(' ', '').upper()
    m = _expiry_map().get(key)
    if m:
        return m
    root = underlying_of(tsym)
    typ = 'CE' if key.endswith('CE') else ('PE' if key.endswith('PE') else None)
    strike = strike_of(tsym)
    parsed = expiry_of(tsym)
    if typ and strike:
        exps = _strike_index().get((root, typ, strike))
        if exps:
            # trust the date written in the symbol when the master lists that same expiry;
            # otherwise fall back to the nearest upcoming expiry for that strike
            if parsed and parsed in exps:
                return parsed
            today = datetime.date.today()
            upcoming = [e for e in exps if e >= today]
            return upcoming[0] if upcoming else exps[-1]
    return parsed



def skip_next_month_spread(exp, today, sym=None, ledger=None):
    """True when this contract must not be flattened by the current-week square-off.

    Next-month debit-spread legs expire after this ISO week, so in_expiry_week
    already leaves them alone. The ledger check is a belt-and-suspenders guard
    if a symbol parse were wrong or a recorded spread leg appeared in-week.
    """
    # A next-month (later expiry) contract is left alone in this week's flatten.
    # A recorded spread that IS in its own expiry week must be closed as a pair
    # (short first) further down, not skipped here.
    if ssp.leave_next_month_spread(exp, today):
        return True, "next-month contract is not squared in this week's flatten"
    return False, None



def _opt_pos_row(p, broker):
    return {
        'broker': broker,
        'sym': p.get('trading_symbol') or p.get('tradingsymbol') or '',
        'trading_symbol': p.get('trading_symbol') or p.get('tradingsymbol') or '',
        'qty': int(p.get('quantity') or 0),
        'quantity': int(p.get('quantity') or 0),
        'product': p.get('product'),
        'instrument_token': p.get('instrument_token') or p.get('instrument_key'),
        'exchange': p.get('exchange') or 'NFO',
        'last_price': p.get('last_price'),
        'average_price': p.get('average_price'),
        'raw': p,
    }


def spread_expiry_closes(rows, today, ledger):
    """Ordered cover-then-sell plans for debit spreads whose contract expires this week."""
    legs = []
    for r in rows:
        if is_index_opt(r['sym']):
            continue
        parsed = ssp.parse_option_symbol(r['sym']) or {}
        r = dict(r, underlying=parsed.get('underlying') or underlying_of(r['sym']),
                 expiry=expiry_for(r['sym']) or parsed.get('expiry'),
                 side=parsed.get('side'), strike=parsed.get('strike') or strike_of(r['sym']))
        if not r.get('expiry') or not in_expiry_week(r['expiry'], today):
            continue
        skip, _ = skip_next_month_spread(r['expiry'], today, r['sym'], ledger)
        if skip:
            continue
        legs.append(r)
    plans = []
    seen = set()
    for pair in ssp.infer_pairs(legs) + []:
        # also pull ledger pairs whose both/any expiry-week legs are present
        sh, lg = pair.get('short') or {}, pair.get('long') or {}
        key = (pair.get('broker'), ssp._norm_sym(lg.get('sym') or lg.get('trading_symbol')),
               ssp._norm_sym(sh.get('sym') or sh.get('trading_symbol')))
        if key in seen:
            continue
        seen.add(key)
        plans.append(pair)
    # ledger-only shorts in expiry week whose long is also in the book or gone
    return plans, {ssp._norm_sym((p.get('long') or {}).get('sym') or (p.get('long') or {}).get('trading_symbol'))
                   for p in plans} | {ssp._norm_sym((p.get('short') or {}).get('sym') or (p.get('short') or {}).get('trading_symbol'))
                   for p in plans}

def main():
    import sys, urllib.parse
    import api
    now = datetime.datetime.now(IST)
    spread_ledger = ssp.load_ledger()
    out = {'now': now.strftime('%Y-%m-%d %H:%M IST'), 'dry': DRY, 'only': ONLY, 'acts': []}

    def call(u, body=None, method=None):
        """Same contract as before (parsed JSON, or an error dict), on the shared client:
        keep-alive connection, User-Agent, 20 s timeout, no retry for an order POST.
        Default routing: reads go direct; POSTs/DELETEs to the order host use the
        sanctioned static-IP proxy. proxy=False was why the first live place today
        hit the static-IP block; do not bypass the proxy on order endpoints."""
        try:
            return api.request(u, body=body, method=method, timeout=20)
        except Exception as e:
            return {'status': 'error', 'err': str(e)[:120]}

    def load_upstox_orders():
        d = call('https://api.upstox.com/v2/order/retrieve-all')
        return d.get('data') or []

    def cancel_upstox(oid):
        return call('https://api-hft.upstox.com/v2/order/cancel?order_id=' + str(oid), None, 'DELETE')

    upstox_orders = []
    try:
        upstox_orders = load_upstox_orders()
    except Exception as e:
        out['upstox_orders_err'] = f'{type(e).__name__}: {e}'

    def quote(key):
        d = call('https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')).get('data') or {}
        for v in d.values():
            return float(v.get('last_price') or 0), float(v.get('lower_circuit_limit') or 0)
        return 0, 0

    def tick(p, t=0.05):
        return round(max(round(p / t) * t, t), 4)

    # ---- Upstox ----
    pos = call('https://api.upstox.com/v2/portfolio/short-term-positions').get('data') or []
    u_pairs, u_paired = spread_expiry_closes([_opt_pos_row(p, 'upstox') for p in pos], now.date(), spread_ledger)
    by_sym = {(p.get('trading_symbol') or '').replace(' ', '').upper(): p for p in pos}

    def close_upstox_leg(raw, side, qty):
        sym = raw.get('trading_symbol') or ''
        ltp, lcl = quote(raw['instrument_token'])
        if not ltp:
            return {'broker': 'upstox', 'sym': sym, 'action': 'SKIP', 'why': 'no quote', 'side': side, 'qty': qty}
        tsz = upstox_tick(raw['instrument_token'])
        px = tick(ltp * (1.01 if side == 'BUY' else 0.99), tsz)
        a = {'broker': 'upstox', 'sym': sym, 'qty': qty, 'ltp': ltp, 'limit': px, 'side': side}
        if DRY:
            a['action'] = 'WOULD_COVER' if side == 'BUY' else 'WOULD_SELL'
            return a
        r = call('https://api-hft.upstox.com/v2/order/place', {
            'quantity': qty, 'product': raw.get('product') or 'D', 'validity': 'DAY', 'price': px,
            'tag': 'expiry_sqoff', 'instrument_token': raw['instrument_token'], 'order_type': 'LIMIT',
            'transaction_type': side, 'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}, 'POST')
        a['action'] = ('COVER' if side == 'BUY' else 'SELL') if (r.get('data') or {}).get('order_id') else 'FAIL'
        a['order'] = (r.get('data') or {}).get('order_id') or r
        return a

    for pair in u_pairs:
        sh, lg = pair.get('short') or {}, pair.get('long') or {}
        sh_raw = by_sym.get(ssp._norm_sym(sh.get('sym') or sh.get('trading_symbol')))
        lg_raw = by_sym.get(ssp._norm_sym(lg.get('sym') or lg.get('trading_symbol')))
        cover_ok = True
        if sh_raw and int(sh_raw.get('quantity') or 0) < 0:
            act = close_upstox_leg(sh_raw, 'BUY', abs(int(sh_raw['quantity'])))
            out['acts'].append(act)
            cover_ok = act.get('action') in ('WOULD_COVER', 'COVER')
        if lg_raw and int(lg_raw.get('quantity') or 0) > 0:
            if not cover_ok:
                out['acts'].append({'broker': 'upstox', 'sym': lg_raw.get('trading_symbol'),
                                    'action': 'SKIP', 'why': 'short cover failed; long not sold'})
            else:
                out['acts'].append(close_upstox_leg(lg_raw, 'SELL', int(lg_raw['quantity'])))

    for p in pos:
        q = int(p.get('quantity') or 0)
        if q <= 0:
            continue
        sym = p.get('trading_symbol') or ''
        if ssp._norm_sym(sym) in u_paired:
            continue
        if is_index_opt(sym):
            continue
        if ONLY == 'equity' and is_commodity_opt(sym):
            continue
        if ONLY == 'mcx' and not is_commodity_opt(sym):
            continue
        exp = expiry_for(sym)
        if not exp or not in_expiry_week(exp, now.date()):
            # t687u: next-month debit spreads expire after this week and are left alone
            continue
        skip, why = skip_next_month_spread(exp, now.date(), sym, spread_ledger)
        if skip:
            out['acts'].append({'broker': 'upstox', 'sym': sym, 'expiry': str(exp),
                                'action': 'SKIP_NEXT_MONTH_SPREAD', 'why': why})
            continue
        ltp, lcl = quote(p['instrument_token'])
        if not ltp:
            out['acts'].append({'broker': 'upstox', 'sym': sym, 'action': 'SKIP', 'why': 'no quote'})
            continue
        tsz = upstox_tick(p['instrument_token'])
        px = tick(ltp * 0.99, tsz)
        if lcl and px < lcl:
            px = tick(lcl, tsz)
        plan = plan_open_sells(upstox_orders, sym, q, ltp, flattening=True)
        a = {'broker': 'upstox', 'sym': sym, 'expiry': str(exp), 'qty': q, 'ltp': ltp, 'limit': px,
             'leave': plan['leave'], 'cancel_plan': plan['cancel'], 'sell_qty': plan['sell_qty']}
        if DRY:
            if plan['cancel']:
                a['would_cancel'] = [c['order_id'] for c in plan['cancel']]
            if plan['sell_qty'] <= 0:
                a['action'] = 'LEAVE_LIMIT'
            else:
                a['action'] = 'WOULD_SELL'
            out['acts'].append(a); continue
        a['cancelled'] = []
        for c in plan['cancel']:
            if not c.get('order_id'):
                continue
            r = cancel_upstox(c['order_id'])
            a['cancelled'].append({c['order_id']: (r.get('status') if isinstance(r, dict) else r)})
        if plan['sell_qty'] <= 0:
            a['action'] = 'LEAVE_LIMIT'
            out['acts'].append(a); continue
        r = call('https://api-hft.upstox.com/v2/order/place', {
            'quantity': plan['sell_qty'], 'product': p.get('product') or 'I', 'validity': 'DAY', 'price': px,
            'tag': 'expiry_sqoff', 'instrument_token': p['instrument_token'], 'order_type': 'LIMIT',
            'transaction_type': 'SELL', 'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}, 'POST')
        a['action'] = 'SELL' if (r.get('data') or {}).get('order_id') else 'FAIL'
        a['order'] = (r.get('data') or {}).get('order_id') or r
        out['acts'].append(a)

    # ---- Kite ----
    try:
        import broker_kite as bk
        if bk.valid():
            k = bk.client()
            try:
                kite_orders = bk.orders()
            except Exception as e:
                kite_orders = []
                out['kite_orders_err'] = f'{type(e).__name__}: {e}'
            knet = k.positions()['net']
            k_pairs, k_paired = spread_expiry_closes([_opt_pos_row(p, 'kite') for p in knet], now.date(), spread_ledger)
            k_by = {(p.get('tradingsymbol') or '').replace(' ', '').upper(): p for p in knet}

            def close_kite_leg(raw, side, qty):
                sym = raw['tradingsymbol']
                ltp = float(raw.get('last_price') or 0)
                if not ltp:
                    return {'broker': 'kite', 'sym': sym, 'action': 'SKIP', 'why': 'no quote', 'side': side, 'qty': qty}
                t = bk.tick_for(raw['exchange'], sym) or 0.05
                px = bk.snap(ltp * (1.01 if side == 'BUY' else 0.99), t)
                a = {'broker': 'kite', 'sym': sym, 'qty': qty, 'ltp': ltp, 'limit': px, 'side': side}
                if DRY:
                    a['action'] = 'WOULD_COVER' if side == 'BUY' else 'WOULD_SELL'
                    return a
                oid = bk.place(raw['exchange'], sym, side, qty, px, tag='expiry_sqoff',
                               product=raw.get('product') or 'NRML')
                a['action'] = ('COVER' if side == 'BUY' else 'SELL') if oid else 'FAIL'
                a['order'] = oid
                return a

            for pair in k_pairs:
                sh, lg = pair.get('short') or {}, pair.get('long') or {}
                sh_raw = k_by.get(ssp._norm_sym(sh.get('sym') or sh.get('trading_symbol')))
                lg_raw = k_by.get(ssp._norm_sym(lg.get('sym') or lg.get('trading_symbol')))
                cover_ok = True
                if sh_raw and int(sh_raw.get('quantity') or 0) < 0:
                    act = close_kite_leg(sh_raw, 'BUY', abs(int(sh_raw['quantity'])))
                    out['acts'].append(act)
                    cover_ok = act.get('action') in ('WOULD_COVER', 'COVER')
                if lg_raw and int(lg_raw.get('quantity') or 0) > 0:
                    if not cover_ok:
                        out['acts'].append({'broker': 'kite', 'sym': lg_raw.get('tradingsymbol'),
                                            'action': 'SKIP', 'why': 'short cover failed; long not sold'})
                    else:
                        out['acts'].append(close_kite_leg(lg_raw, 'SELL', int(lg_raw['quantity'])))

            for p in knet:
                q = int(p.get('quantity') or 0)
                if q <= 0:
                    continue
                sym = p['tradingsymbol']
                if ssp._norm_sym(sym) in k_paired:
                    continue
                if is_index_opt(sym):
                    continue
                if ONLY == 'equity' and is_commodity_opt(sym):
                    continue
                if ONLY == 'mcx' and not is_commodity_opt(sym):
                    continue
                exp = expiry_for(sym)
                if not exp or not in_expiry_week(exp, now.date()):
                    # t687u: next-month debit spreads expire after this week and are left alone
                    continue
                skip, why = skip_next_month_spread(exp, now.date(), sym, spread_ledger)
                if skip:
                    out['acts'].append({'broker': 'kite', 'sym': sym, 'expiry': str(exp),
                                        'action': 'SKIP_NEXT_MONTH_SPREAD', 'why': why})
                    continue
                ltp = float(p.get('last_price') or 0)
                if not ltp:
                    out['acts'].append({'broker': 'kite', 'sym': sym, 'action': 'SKIP', 'why': 'no quote'})
                    continue
                t = bk.tick_for(p['exchange'], sym) or 0.05
                px = bk.snap(ltp * 0.99, t)
                plan = plan_open_sells(kite_orders, sym, q, ltp, flattening=True)
                a = {'broker': 'kite', 'sym': sym, 'expiry': str(exp), 'qty': q, 'ltp': ltp, 'limit': px,
                     'leave': plan['leave'], 'cancel_plan': plan['cancel'], 'sell_qty': plan['sell_qty']}
                if DRY:
                    if plan['cancel']:
                        a['would_cancel'] = [c['order_id'] for c in plan['cancel']]
                    if plan['sell_qty'] <= 0:
                        a['action'] = 'LEAVE_LIMIT'
                    else:
                        a['action'] = 'WOULD_SELL'
                    out['acts'].append(a); continue
                a['cancelled'] = []
                for c in plan['cancel']:
                    if not c.get('order_id'):
                        continue
                    a['cancelled'].append({c['order_id']: bk.cancel(c['order_id'])})
                if plan['sell_qty'] <= 0:
                    a['action'] = 'LEAVE_LIMIT'
                    out['acts'].append(a); continue
                oid = bk.place(p['exchange'], sym, 'SELL', plan['sell_qty'], px, tag='expiry_sqoff',
                               product=p.get('product') or 'MIS')
                a['action'] = 'SELL' if oid else 'FAIL'
                a['order'] = oid
                out['acts'].append(a)
    except Exception as e:
        out['kite_err'] = f'{type(e).__name__}: {e}'

    print(json.dumps(out, indent=1, default=str))


if __name__ == '__main__':
    main()
