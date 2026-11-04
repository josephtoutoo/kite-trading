#!/usr/bin/env python3
"""Crude pullback watch (user, 4 Nov 2026 t297u).

The 4 Nov crude trigger fired at 8,950 while the 9000 PE was Rs 360; by the time the order problem was
sorted the same put was asking Rs 554 and crude had already run ~250 points lower. The user asked to be
pinged instead if crude BOUNCES back into 8,820-8,850 with the put still cheap (~Rs 450) and the trend
still red - i.e. a pullback entry rather than chasing.

Alert condition (all must hold):
  1. CRUDEOIL futures last price inside [LO, HI] (the pullback zone),
  2. CRUDEOIL 9000 PE 17 NOV 26 ask <= PE_MAX (still near Rs 450),
  3. trend still down: 5m close < EMA20 and Supertrend red.
Silent otherwise. Alerts at most once per half hour and once per day, state in crude_pullback_watch.json.
No orders are placed - Upstox disables MCX orders over its API (UDAPI1161) and the commodity purse is
separate. The ping tells the user to place it in the Upstox app.

CLI: python3 crude_pullback_watch.py [--status] [--force]
"""
import os, sys, json, datetime, urllib.request, urllib.error, urllib.parse

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, 'crude_pullback_watch.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
FUT_KEY = 'MCX_FO|573422'   # CRUDEOIL FUT 19 NOV 26
PE_KEY = 'MCX_FO|583080'    # CRUDEOIL 9000 PE 17 NOV 26 (583080 = PE; 582930 is the CE)
LO, HI = 8820.0, 8850.0     # pullback zone the user picked
PE_MAX = 470.0              # "near Rs 450" - allow the ask up to here
EXPIRES = '2026-11-11'      # this watch is temporary; last day it may alert

sys.path.insert(0, '/workspace/upstox')
TOK = json.load(open('/workspace/upstox/token.json'))['access_token']
H = {'Authorization': 'Bearer ' + TOK, 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
_op = urllib.request.build_opener(urllib.request.ProxyHandler(
    {'https': os.environ['UPSTOX_PROXY_URL'], 'http': os.environ['UPSTOX_PROXY_URL']})) \
    if os.environ.get('UPSTOX_PROXY_URL') else urllib.request.build_opener()


def get(u, body=None, method=None):
    r = urllib.request.Request(u, data=json.dumps(body).encode() if body is not None else None,
                               headers=H, method=method)
    return json.load(_op.open(r, timeout=25))


def quote(key):
    u = 'https://api.upstox.com/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')
    d = (get(u).get('data') or {})
    return list(d.values())[0] if d else {}


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {'alerts': {}, 'last_alert': None}


def save(s):
    json.dump(s, open(STATE, 'w'), indent=1)


def trend():
    """5m crude trend from the shared indicator module: (is_down, detail)."""
    import ind
    a = ind.analyze(FUT_KEY, 'minutes', 5, 5)
    down = a['close'] < a['ema20'] and a['dir'] == -1
    return down, f"5m close {a['close']:.0f} vs EMA20 {a['ema20']:.0f}, Supertrend {'red' if a['dir']==-1 else 'green'}, ADX {a['adx']}"


def main():
    now = datetime.datetime.now(IST)
    out = {'now': now.strftime('%Y-%m-%d %H:%M IST')}
    st = load()
    if '--status' in sys.argv:
        out['state'] = st
        print(json.dumps(out, indent=1))
        return
    if now.date().isoformat() > EXPIRES:
        out['action'] = f'watch expired on {EXPIRES} - delete this routine'
        print(json.dumps(out, indent=1))
        return
    try:
        fut = quote(FUT_KEY); pe = quote(PE_KEY)
    except Exception as e:
        out['action'] = f'quote failed: {type(e).__name__}: {e}'
        print(json.dumps(out, indent=1))
        return
    ltp = float(fut.get('last_price') or 0)
    ask = float(((pe.get('depth') or {}).get('sell') or [{}])[0].get('price') or pe.get('last_price') or 0)
    out['crude'] = ltp
    out['pe_ask'] = ask
    out['pe_symbol'] = pe.get('symbol') or pe.get('trading_symbol')
    out['zone'] = [LO, HI]
    out['pe_max'] = PE_MAX
    if not ltp or not ask:
        out['action'] = 'no quote (MCX closed?)'
        print(json.dumps(out, indent=1))
        return
    day = now.date().isoformat()
    in_zone = LO <= ltp <= HI
    cheap = ask <= PE_MAX
    down, tdet = trend()
    out['in_zone'] = in_zone
    out['pe_ok'] = cheap
    out['trend_down'] = down
    out['trend'] = tdet
    if not (in_zone and cheap and down):
        out['action'] = 'no alert'
        # remember why, so a run is diagnosable
        st['last_check'] = {'ts': out['now'], 'crude': ltp, 'pe_ask': ask, 'in_zone': in_zone, 'pe_ok': cheap, 'trend_down': down}
        save(st)
        print(json.dumps(out, indent=1))
        return
    if st['alerts'].get(day) and '--force' not in sys.argv:
        out['action'] = 'already alerted today'
        print(json.dumps(out, indent=1))
        return
    if st.get('last_alert'):
        try:
            prev = datetime.datetime.fromisoformat(st['last_alert'])
            if (now - prev).total_seconds() < 1800 and '--force' not in sys.argv:
                out['action'] = 'alerted within the last 30 min'
                print(json.dumps(out, indent=1))
                return
        except Exception:
            pass
    out['action'] = 'ALERT'
    st['alerts'][day] = {'ts': out['now'], 'crude': ltp, 'pe_ask': ask}
    st['last_alert'] = now.isoformat()
    save(st)
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
