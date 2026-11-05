"""One-shot: exit IOC MTF on Upstox to bring managed mine count from 6 to 5 (t383u, 5 Nov 2026).
Kite AMO already queued overnight (order 2118298180688207872). Run at/after 09:15 IST.
Cancels Upstox IOC GTT first, then LIMIT SELL full qty, tag mtf_trim.
"""
import json, os, sys, datetime
sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, '/workspace/equity')
import api, trade_lock, gtt_overnight as gtt

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
KEY = 'NSE_EQ|INE242A01010'
SYM = 'IOC'
TAG = 'mtf_trim'

def tick(p, t=0.05):
    return round(round(p / t) * t, 2)

def main():
    now = datetime.datetime.now(IST)
    pos = {((p.get('trading_symbol') or '').replace('-EQ', '')): p
           for p in (api.get('/v3/portfolio/mtf-positions').get('data') or [])}
    p = pos.get(SYM)
    if not p or int(p.get('quantity') or 0) <= 0:
        print(json.dumps({'ok': True, 'skipped': 'no Upstox IOC MTF left', 'now': now.isoformat()}))
        return 0
    qty = int(p['quantity'])
    ltp = float(p.get('last_price') or 0)
    if not ltp:
        q = api.get('/v2/market-quote/ltp?instrument_key=' + KEY)
        ltp = float(list(q['data'].values())[0]['last_price'])
    px = tick(ltp * 0.998)
    o = {'trading_symbol': SYM, 'instrument_token': KEY, 'transaction_type': 'SELL',
         'order_type': 'LIMIT', 'product': 'MTF', 'quantity': qty, 'price': px}
    ok, why, adj = trade_lock.check_order(o)
    out = {'now': now.isoformat(), 'broker': 'upstox', 'sym': SYM, 'qty': qty, 'ltp': ltp,
           'limit': px, 'check': {'ok': ok, 'why': why, 'adj': adj}}
    if not ok or adj <= 0:
        print(json.dumps(out)); return 1
    rec = gtt.load()
    cok, cdet = gtt.cancel_sym('upstox', SYM, rec, key=KEY)
    gtt.save(rec)
    out['gtt_cancel'] = {'ok': cok, 'detail': cdet}
    body = {'quantity': int(adj), 'product': 'MTF', 'validity': 'DAY', 'price': px, 'tag': TAG,
            'instrument_token': KEY, 'order_type': 'LIMIT', 'transaction_type': 'SELL',
            'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}
    r = api.call('https://api-hft.upstox.com/v2/order/place', body=body, method='POST', proxy=True)
    oid = (r.get('data') or {}).get('order_id')
    out['order_id'] = oid
    out['response'] = None if oid else r
    print(json.dumps(out, default=str))
    return 0 if oid else 1

if __name__ == '__main__':
    sys.exit(main())
