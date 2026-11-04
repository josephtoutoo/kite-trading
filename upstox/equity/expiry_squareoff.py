#!/usr/bin/env python3
"""Close expiry-week option positions the same day (user rule, 4 Nov 2026).

Options whose expiry is within 7 calendar days are intraday only - never carried as
delivery. This runs shortly before the close, finds any still-open option position
expiring in that window on either broker. Stock and MCX commodity options only - index options
are skipped, and --only equity / --only mcx split the NSE-BSE and MCX sessions., and squares it off with a marketable LIMIT
order (so we control the exit instead of leaving it to the broker's auto square-off).

CLI: python3 expiry_squareoff.py [--dry]
"""
import sys, json, datetime
sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, '/workspace/equity')

DRY = '--dry' in sys.argv
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


def main():
    import sys, urllib.parse
    import api
    now = datetime.datetime.now(IST)
    out = {'now': now.strftime('%Y-%m-%d %H:%M IST'), 'dry': DRY, 'only': ONLY, 'acts': []}

    def call(u, body=None, method=None):
        """Same contract as before (parsed JSON, or an error dict), on the shared client:
        keep-alive connection, User-Agent, 20 s timeout, no retry for an order POST.
        Routing is deliberately unchanged: this script went DIRECT for reads and for its
        square-off POST, so proxy=False keeps it direct (a routing change on an order
        endpoint is not something a performance refactor gets to make)."""
        try:
            return api.request(u, body=body, method=method, proxy=False, timeout=20)
        except Exception as e:
            return {'status': 'error', 'err': str(e)[:120]}

    def quote(key):
        d = call('https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')).get('data') or {}
        for v in d.values():
            return float(v.get('last_price') or 0), float(v.get('lower_circuit_limit') or 0)
        return 0, 0

    def tick(p, t=0.05):
        return round(max(round(p / t) * t, t), 4)

    # ---- Upstox ----
    pos = call('https://api.upstox.com/v2/portfolio/short-term-positions').get('data') or []
    for p in pos:
        q = int(p.get('quantity') or 0)
        if q <= 0:
            continue
        sym = p.get('trading_symbol') or ''
        if is_index_opt(sym):
            continue
        if ONLY == 'equity' and is_commodity_opt(sym):
            continue
        if ONLY == 'mcx' and not is_commodity_opt(sym):
            continue
        exp = expiry_for(sym)
        if not exp or not (0 <= (exp - now.date()).days <= 7):
            continue
        ltp, lcl = quote(p['instrument_token'])
        if not ltp:
            out['acts'].append({'broker': 'upstox', 'sym': sym, 'action': 'SKIP', 'why': 'no quote'})
            continue
        tsz = upstox_tick(p['instrument_token'])
        px = tick(ltp * 0.99, tsz)
        if lcl and px < lcl:
            px = tick(lcl, tsz)
        a = {'broker': 'upstox', 'sym': sym, 'expiry': str(exp), 'qty': q, 'ltp': ltp, 'limit': px}
        if DRY:
            a['action'] = 'WOULD_SELL'; out['acts'].append(a); continue
        r = call('https://api-hft.upstox.com/v2/order/place', {
            'quantity': q, 'product': p.get('product') or 'I', 'validity': 'DAY', 'price': px,
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
            for p in k.positions()['net']:
                q = int(p.get('quantity') or 0)
                if q <= 0:
                    continue
                sym = p['tradingsymbol']
                if is_index_opt(sym):
                    continue
                if ONLY == 'equity' and is_commodity_opt(sym):
                    continue
                if ONLY == 'mcx' and not is_commodity_opt(sym):
                    continue
                exp = expiry_for(sym)
                if not exp or not (0 <= (exp - now.date()).days <= 7):
                    continue
                ltp = float(p.get('last_price') or 0)
                if not ltp:
                    out['acts'].append({'broker': 'kite', 'sym': sym, 'action': 'SKIP', 'why': 'no quote'})
                    continue
                t = bk.tick_for(p['exchange'], sym) or 0.05
                px = bk.snap(ltp * 0.99, t)
                a = {'broker': 'kite', 'sym': sym, 'expiry': str(exp), 'qty': q, 'ltp': ltp, 'limit': px}
                if DRY:
                    a['action'] = 'WOULD_SELL'; out['acts'].append(a); continue
                oid = bk.place(p['exchange'], sym, 'SELL', q, px, tag='expiry_sqoff',
                               product=p.get('product') or 'MIS')
                a['action'] = 'SELL' if oid else 'FAIL'
                a['order'] = oid
                out['acts'].append(a)
    except Exception as e:
        out['kite_err'] = f'{type(e).__name__}: {e}'

    print(json.dumps(out, indent=1, default=str))


if __name__ == '__main__':
    main()
