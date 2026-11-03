"""Shared equity target-roll / trailing-stop helper (dry-run; places NO orders).
Rule: on target hit, if last closed 1h candle close > 1h EMA20 AND > 1h Supertrend(10,3, up)
and 1h EMA20 rising (continuing higher) -> ROLL: target += original distance,
stop = max(old stop, previous target, 1h Supertrend). Stops never lower.
Exit flag (BOOK/EXIT) only when 1h close < EMA20 or < Supertrend, or LTP <= stop."""
import json, os, sys, datetime as dt
sys.path.insert(0, '/workspace/upstox')
import ind
STATE = '/workspace/equity/trail_state.json'
DEF_TGT, DEF_SL = 0.10, 0.05   # default initial target +10%, stop -5% of avg if none set

def load():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}
def save(s):
    json.dump(s, open(STATE, 'w'), indent=1)

def trend(key):
    a = ind.analyze(key, 'hours', 1, 30)
    intact = a['close'] > a['ema20'] and a['close'] > a['st'] and a['dir'] == 1
    cont = a['ema20'] > a['prev_ema']  # 1h EMA20 still rising
    return a, intact, cont

def evaluate(sym, key, avg, ltp, st_all, persist=False):
    s = st_all.get(sym) or {'entry': avg, 'target': round(avg*(1+DEF_TGT), 2),
                             'dist': round(avg*DEF_TGT, 2), 'stop': round(avg*(1-DEF_SL), 2), 'rolls': 0}
    a, intact, cont = trend(key)
    act = 'HOLD'
    if ltp <= s['stop'] or not intact and ltp >= s['target']:
        act = 'EXIT_STOP' if ltp <= s['stop'] else 'BOOK_PROFIT (trend broken)'
    elif not intact and s['rolls'] > 0:
        act = 'BOOK_PROFIT (trend broken after roll)'
    else:
        while ltp >= s['target'] and intact and cont:
            prev = s['target']; s['target'] = round(prev + s['dist'], 2)
            s['stop'] = round(max(s['stop'], prev, a['st']), 2); s['rolls'] += 1; act = 'ROLLED'
        if ltp >= s['target'] and intact and not cont: act = 'AT_TARGET_WAIT (not continuing)'
    if intact: s['stop'] = round(max(s['stop'], a['st']), 2) if a['st'] < ltp else s['stop']
    st_all[sym] = s
    if persist: save(st_all)
    return dict(sym=sym, ltp=ltp, avg=avg, close1h=a['close'], ema20=a['ema20'], st1h=a['st'],
                intact=intact, cont=cont, target=s['target'], stop=s['stop'], rolls=s['rolls'], action=act)

def upstox_equity():
    out = []
    for h in ind.get('/v2/portfolio/long-term-holdings')['data']:
        if h['quantity']: out.append((h['trading_symbol'], h['instrument_token'], h['average_price'], h['last_price'], h['quantity'], 'CNC'))
    for p in ind.get('/v2/portfolio/short-term-positions')['data']:
        if p['quantity'] and p.get('instrument_token','').startswith(('NSE_EQ','BSE_EQ')):
            out.append((p['trading_symbol'], p['instrument_token'], p['average_price'], p['last_price'], p['quantity'], p['product']))
    return out

if __name__ == '__main__':
    persist = '--persist' in sys.argv
    st_all = load()
    for sym, key, avg, ltp, q, prod in upstox_equity():
        try: print(json.dumps({**evaluate(sym, key, avg, ltp, st_all, persist), 'qty': q, 'product': prod}))
        except Exception as e: print(sym, 'ERR', e)
    print('DRY RUN - no orders placed')
