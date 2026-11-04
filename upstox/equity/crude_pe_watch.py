#!/usr/bin/env python3
"""Watch crude oil and buy the CRUDEOIL 9000 PE 17 NOV 26 on Upstox when crude drops under 8,950.

User standing permission (4 Nov 2026, t274u): place this one order automatically when the trigger
hits. Upstox DISABLES all MCX orders through its API (error UDAPI1161 "MCX API orders are temporarily
disabled", applies to v2 and v3 alike), so an API order can never fill; the script reports that and
asks the user to place it in the Upstox app instead. It also checks that the commodity segment has
margin, because Upstox keeps equity and commodity purses separate.

Rules: 1 lot (=100 barrels, quantity 1 on Upstox), LIMIT at ask+2, SL -30%, target +20%.
Only fires once per calendar day (state in crude_pe_watch.json). Run only during MCX hours.

CLI: python3 crude_pe_watch.py [--dry] [--status]
"""
import os, sys, json, datetime, urllib.parse

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, 'crude_pe_watch.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TRIGGER = 8950.0
FUT_KEY = 'MCX_FO|573422'      # CRUDEOIL FUT 19 NOV 26
PE_KEY = 'MCX_FO|583080'       # CRUDEOIL 9000 PE 17 NOV 26 (was 582930 = the 9000 CE - wrong leg, fixed 4 Nov 2026 t296u)
PE_SYM = 'CRUDEOIL 9000 PE 17 NOV 26'
DRY = '--dry' in sys.argv
STATUS = '--status' in sys.argv

sys.path.insert(0, '/workspace/upstox')
import trade_lock as tl
import api                     # pooled, proxied, timed client (5 Nov 2026)


def quote(key):
    u = 'https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')
    d = api.request(u, method='GET', proxy=True, timeout=25).get('data') or {}
    return list(d.values())[0] if d else {}


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {'placed': {}, 'last_alert': None}


def save(s):
    json.dump(s, open(STATE, 'w'), indent=1)


def main():
    now = datetime.datetime.now(IST)
    out = {'now': now.strftime('%Y-%m-%d %H:%M IST'), 'dry': DRY}
    # t339u: which rule set this run is under. This book has no 5-6 Nov override of its own
    # (MCX is history only - Upstox blocks MCX API orders), but the line still states which
    # set trade_lock is applying, so a log never has to be guessed at.
    try:
        import autonomy
        out['rule_set'] = autonomy.rule_set(now)
    except Exception as e:
        out['rule_set'] = {'rule_set': 'user rules', 'override': f'unavailable ({type(e).__name__})'}
    st = load()
    fut = quote(FUT_KEY)
    pe = quote(PE_KEY)
    ltp = float(fut.get('last_price') or 0)
    ask = float(((pe.get('depth') or {}).get('sell') or [{}])[0].get('price') or pe.get('last_price') or 0)
    out['crude'] = ltp
    out['pe_ask'] = ask
    out['pe_symbol'] = pe.get('symbol') or pe.get('trading_symbol') or PE_SYM  # prove which leg was quoted
    out['commodity_margin'] = commodity_margin()
    out['trigger'] = TRIGGER
    if STATUS:
        out['state'] = st
        print(json.dumps(out, indent=1))
        return
    if not ltp or not ask:
        out['action'] = 'no quote'
        print(json.dumps(out, indent=1))
        return
    if ltp > TRIGGER:
        out['action'] = 'above trigger, nothing to do'
        print(json.dumps(out, indent=1))
        return
    if st['placed'].get(now.date().isoformat()):
        out['action'] = 'already acted today'
        print(json.dumps(out, indent=1))
        return
    cm = out.get('commodity_margin')
    if cm is not None and cm < ask * 100:
        out['action'] = (f'BLOCKED: commodity purse has Rs {cm:,.0f}, the 1-lot premium needs Rs {ask*100:,.0f}. '
                         'Move funds to the commodity segment in the Upstox app, then place the order there '
                         '(Upstox also refuses MCX orders from the API: UDAPI1161).')
        print(json.dumps(out, indent=1))
        return
    price = round(ask + 2, 1)   # crude options trade in 0.1 ticks
    order = {'trading_symbol': PE_SYM, 'instrument_token': PE_KEY, 'transaction_type': 'BUY',
             'order_type': 'LIMIT', 'product': 'D', 'quantity': 1, 'price': price}
    ok, reasons, adj = tl.check_order(order, broker='upstox')
    out['check'] = {'allowed': ok, 'reasons': reasons, 'qty': adj}
    if not ok or adj <= 0:
        out['action'] = 'blocked by pre-trade rules'
        print(json.dumps(out, indent=1))
        return
    if DRY:
        out['action'] = 'WOULD_BUY'
        out['price'] = price
        print(json.dumps(out, indent=1))
        return
    body = {'quantity': int(adj), 'product': 'D', 'validity': 'DAY', 'price': price, 'tag': 'crude_pe',
            'instrument_token': PE_KEY, 'order_type': 'LIMIT', 'transaction_type': 'BUY',
            'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}
    try:
        r = api.request('https://api-hft.upstox.com/v2/order/place', body, 'POST', proxy=True, timeout=25)
        oid = (r.get('data') or {}).get('order_id')
        out['action'] = 'BUY placed'
        out['order'] = oid
        st['placed'][now.date().isoformat()] = oid
        save(st)
    except Exception as e:
        try:
            msg = e.read().decode()[:300]
        except Exception:
            msg = str(e)[:200]
        out['action'] = 'order refused by broker'
        out['error'] = msg
    print(json.dumps(out, indent=1))


def commodity_margin():
    u = 'https://api.upstox.com/v2/user/get-funds-and-margin'
    try:
        d = api.request(u, method='GET', proxy=True, timeout=20).get('data') or {}
        return float((d.get('commodity') or {}).get('available_margin') or 0)
    except Exception as e:
        # None means "unknown", and the caller then skips the purse check - so it must be visible.
        print(f'crude_pe_watch: commodity margin unavailable, purse check skipped: {type(e).__name__}: {e}',
              file=sys.stderr)
        return None


if __name__ == '__main__':
    main()
