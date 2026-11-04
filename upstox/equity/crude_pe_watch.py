#!/usr/bin/env python3
"""Watch crude oil and buy the CRUDEOIL 9000 PE 17 NOV 26 on Upstox when crude drops under 8,950.

User standing permission (4 Nov 2026, t274u): place this one order automatically when the trigger
hits. Upstox currently disables MCX orders through its API, so when the order is refused the script
reports that and asks the user to place it in the app instead.

Rules: 1 lot (=100 barrels, quantity 1 on Upstox), LIMIT at ask+2, SL -30%, target +20%.
Only fires once per calendar day (state in crude_pe_watch.json). Run only during MCX hours.

CLI: python3 crude_pe_watch.py [--dry] [--status]
"""
import os, sys, json, datetime, urllib.request, urllib.parse

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, 'crude_pe_watch.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TRIGGER = 8950.0
FUT_KEY = 'MCX_FO|573422'      # CRUDEOIL FUT 19 NOV 26
PE_KEY = 'MCX_FO|582930'       # CRUDEOIL 9000 PE 17 NOV 26
PE_SYM = 'CRUDEOIL 9000 PE 17 NOV 26'
DRY = '--dry' in sys.argv
STATUS = '--status' in sys.argv

sys.path.insert(0, '/workspace/upstox')
import trade_lock as tl

TOK = json.load(open('/workspace/upstox/token.json'))['access_token']
H = {'Authorization': 'Bearer ' + TOK, 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
op = urllib.request.build_opener(urllib.request.ProxyHandler(
    {'https': os.environ['UPSTOX_PROXY_URL'], 'http': os.environ['UPSTOX_PROXY_URL']}))


def quote(key):
    u = 'https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')
    d = json.load(op.open(urllib.request.Request(u, headers=H), timeout=25)).get('data') or {}
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
    st = load()
    fut = quote(FUT_KEY)
    pe = quote(PE_KEY)
    ltp = float(fut.get('last_price') or 0)
    ask = float(((pe.get('depth') or {}).get('sell') or [{}])[0].get('price') or pe.get('last_price') or 0)
    out['crude'] = ltp
    out['pe_ask'] = ask
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
    H2 = dict(H); H2['Content-Type'] = 'application/json'
    try:
        r = json.load(op.open(urllib.request.Request('https://api-hft.upstox.com/v2/order/place',
                                                     data=json.dumps(body).encode(), headers=H2, method='POST'), timeout=25))
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


if __name__ == '__main__':
    main()
