"""Equity MTF rotation proposer (PROPOSALS ONLY - places NO orders).
Rules: max 5 MTF stocks at 10% capital each = 50% total.
Strong trend: 1h close > EMA20 and Supertrend green, confirmed on daily (close>EMA20, ST green).
Score = ADX(1h) + 100*MACDhist/close(1h) * 10 + 2*%dist above EMA20(1h) + daily bonus.
Swap only if best candidate score >= 1.15 x weakest holding AND weakest is weaker/broken."""
import json, sys, time, gzip, urllib.request, datetime as dt
sys.path.insert(0, '/workspace/upstox'); import ind
TOK = json.load(open('/workspace/upstox/token.json'))['access_token']
MAXN, MARGIN = 5, 1.15
NIFTY100 = """ABB ADANIENSOL ADANIENT ADANIGREEN ADANIPORTS ADANIPOWER AMBUJACEM APOLLOHOSP ASIANPAINT DMART AXISBANK
BAJAJ-AUTO BAJFINANCE BAJAJFINSV BAJAJHLDNG BANKBARODA BEL BHARTIARTL BPCL BOSCHLTD BRITANNIA CANBK CHOLAFIN CIPLA COALINDIA
DABUR DIVISLAB DLF DRREDDY EICHERMOT GAIL GODREJCP GRASIM HAVELLS HCLTECH HDFCBANK HDFCLIFE HAL HEROMOTOCO HINDALCO
HINDUNILVR HINDZINC ICICIBANK ICICIGI INDHOTEL IOC IRFC INDUSINDBK NAUKRI INFY INDIGO ITC JINDALSTEL JSWENERGY JSWSTEEL
KOTAKBANK LT LICI LODHA LTIM M&M MARUTI NESTLEIND NTPC ONGC PIDILITIND PFC POWERGRID PNB RECLTD RELIANCE SBILIFE
MOTHERSON SHREECEM SHRIRAMFIN SIEMENS SBIN SUNPHARMA TATACONSUM TCS TATAMOTORS TATAPOWER TATASTEEL TECHM TITAN TORNTPHARM
TRENT TVSMOTOR ULTRACEMCO UNIONBANK UNITDSPR VBL VEDL WIPRO ZYDUSLIFE ETERNAL JIOFIN SWIGGY HYUNDAI""".split()
def get(p):
    r = urllib.request.Request('https://api.upstox.com'+p, headers={'Authorization':'Bearer '+TOK,'Accept':'application/json','User-Agent':'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r, timeout=20))
def keymap():
    d = json.load(gzip.open(f'/workspace/upstox/instruments_NSE_{dt.date.today()}.json.gz'))
    return {x['trading_symbol']: x['instrument_key'] for x in d if x.get('segment')=='NSE_EQ' and x.get('instrument_type')=='EQ'}
def score(key):
    h = ind.analyze(key, 'hours', 1, 30); d = ind.analyze(key, 'days', 1, 120)
    up1h = h['close'] > h['ema20'] and h['close'] > h['st'] and h['dir'] == 1
    upd = d['close'] > d['ema20'] and d['dir'] == 1
    dist = 100*(h['close']-h['ema20'])/h['ema20']
    s = h['adx'] + 1000*h['hist']/h['close'] + 2*dist + (15 if upd else -15)
    if not up1h: s -= 40
    return dict(close=h['close'], adx=h['adx'], hist=h['hist'], dist=round(dist,2), up1h=up1h, upd=upd,
                strong=up1h and upd, score=round(s,1))
def main():
    km = keymap()
    f = get('/v2/user/get-funds-and-margin')['data']['equity']
    cap = f['available_margin'] + f['used_margin']
    per_cap = min(0.5*cap/MAXN, 0.10*cap); tot_cap = per_cap*MAXN
    pos = get('/v3/portfolio/mtf-positions').get('data') or []
    hold = {}
    for p in pos:
        q = p.get('quantity') or p.get('net_quantity') or 0
        if q: hold[p['trading_symbol'].replace('-EQ','')] = dict(qty=q, avg=p.get('average_price'), ltp=p.get('last_price'), key=p.get('instrument_token'))
    for s in ['JPL']:  # user-requested watch name
        if s not in hold and s in km: NIFTY100.append(s)
    H = []
    for s, v in hold.items():
        try: sc = score(v['key'] or km[s])
        except Exception as e: print('ERR', s, e); continue
        v.update(sc, sym=s, value=round(v['qty']*(v['ltp'] or sc['close']))); H.append(v)
    H.sort(key=lambda x: -x['score'])
    C = []
    for s in NIFTY100:
        if s in hold or s not in km: continue
        try: C.append(dict(sym=s, **score(km[s])))
        except Exception as e: print('skip', s, e)
        time.sleep(0.15)
    C = sorted([c for c in C if c['strong']], key=lambda x: -x['score'])
    print(f"Capital {cap:,.0f} | per-stock cap {per_cap:,.0f} (min of 50%/3={0.5*cap/3:,.0f}, 10%={0.1*cap:,.0f}) | MTF max {tot_cap:,.0f}")
    print("Cap check: 5 x 10% = 50% MTF budget (no conflict).")
    print("\nHOLDINGS (ranked)"); [print(f" {h['sym']:<10} qty {h['qty']} val {h['value']:,} score {h['score']} adx {h['adx']} dist {h['dist']}% 1h {'UP' if h['up1h'] else 'DOWN'} D {'UP' if h['upd'] else 'DOWN'}") for h in H]
    print("\nTOP CANDIDATES"); [print(f" {c['sym']:<10} {c['close']} score {c['score']} adx {c['adx']} dist {c['dist']}%") for c in C[:8]]
    acts = []; keep = list(H)
    while len(keep) > MAXN:
        w = keep.pop(); acts.append(f"CLOSE {w['sym']} all {w['qty']} (over 5 holdings, weakest)")
    for h in keep:
        if h['value'] > per_cap*1.02:
            tq = int((h['value']-per_cap)/(h['ltp'] or h['close'])) + 1
            acts.append(f"TRIM {h['sym']} sell {tq} (value {h['value']:,} > cap {per_cap:,.0f})")
    ci = 0
    while keep and ci < len(C):
        w = keep[-1]; c = C[ci]
        if c['score'] >= MARGIN*max(w['score'],0.1) and (not w['strong'] or w['score'] < c['score']):
            acts.append(f"SWAP: CLOSE {w['sym']} ({w['qty']}, score {w['score']}) -> BUY {c['sym']} {int(per_cap//c['close'])} @~{c['close']} (score {c['score']})")
            keep.pop(); keep.insert(0, dict(c, qty=int(per_cap//c['close']))); keep.sort(key=lambda x:-x['score']); ci += 1
        else: break
    while len(keep) < MAXN and ci < len(C):
        c = C[ci]; ci += 1; acts.append(f"ADD {c['sym']} {int(per_cap//c['close'])} @~{c['close']} (~{per_cap:,.0f})"); keep.append(c)
    print("\nPROPOSED ACTIONS (not placed)"); [print(' ', a) for a in acts or ['none']]
    print("\nTARGET BOOK:", ', '.join(k['sym'] for k in keep), f"~{per_cap:,.0f} each")
if __name__ == '__main__': main()
