"""Equity MTF rotation proposer (PROPOSALS ONLY - places NO orders).
Rules: max 5 MTF stocks at 10% capital each = 50% total.
Reserve rule: of the remaining MTF room (50% cap - deployed), only HALF may go to new rotation
entries; the other half is RESERVED for pullback adds (pullback_add.py only).
Strong trend: 1h close > EMA20 and Supertrend green, confirmed on daily (close>EMA20, ST green).
Score = ADX(1h) + 100*MACDhist/close(1h) * 10 + 2*%dist above EMA20(1h) + daily bonus.
Swap only if best candidate score >= 1.15 x weakest holding AND weakest is weaker/broken."""
import json, sys, time, glob
sys.path.insert(0, '/workspace/upstox'); import ind, api
TOK = None      # the shared client attaches the bearer token itself (5 Nov 2026)
MAXN, MARGIN = 5, 1.15
_KM = {}                      # master path -> ((mtime_ns, size), map)
NIFTY100 = """ABB ADANIENSOL ADANIENT ADANIGREEN ADANIPORTS ADANIPOWER AMBUJACEM APOLLOHOSP ASIANPAINT DMART AXISBANK
BAJAJ-AUTO BAJFINANCE BAJAJFINSV BAJAJHLDNG BANKBARODA BEL BHARTIARTL BPCL BOSCHLTD BRITANNIA CANBK CHOLAFIN CIPLA COALINDIA
DABUR DIVISLAB DLF DRREDDY EICHERMOT GAIL GODREJCP GRASIM HAVELLS HCLTECH HDFCBANK HDFCLIFE HAL HEROMOTOCO HINDALCO
HINDUNILVR HINDZINC ICICIBANK ICICIGI INDHOTEL IOC IRFC INDUSINDBK NAUKRI INFY INDIGO ITC JINDALSTEL JSWENERGY JSWSTEEL
KOTAKBANK LT LICI LODHA LTIM M&M MARUTI NESTLEIND NTPC ONGC PIDILITIND PFC POWERGRID PNB RECLTD RELIANCE SBILIFE
MOTHERSON SHREECEM SHRIRAMFIN SIEMENS SBIN SUNPHARMA TATACONSUM TCS TATAMOTORS TATAPOWER TATASTEEL TECHM TITAN TORNTPHARM
TRENT TVSMOTOR ULTRACEMCO UNIONBANK UNITDSPR VBL VEDL WIPRO ZYDUSLIFE ETERNAL JIOFIN SWIGGY HYUNDAI""".split()
def get(p):
    return api.get(p)


def keymap():
    """trading_symbol -> instrument_key from the newest NSE master, parsed once per process.

    It used to open today's dated file only, so on a day where the download had not landed yet
    the scan died outright. It now takes the newest master present (the same rule every other
    script uses) and caches the parse on (path, mtime, size) instead of re-reading it per call.
    """
    import os
    files = sorted(glob.glob(f'/workspace/upstox/instruments_NSE_*.json.gz'))
    if not files:
        raise RuntimeError('no NSE instruments master in /workspace/upstox')
    f = files[-1]
    st = os.stat(f)
    hit = _KM.get(f)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return dict(hit[1])
    km = {x['trading_symbol']: x['instrument_key'] for x in api.gzip_json(f)
          if x.get('segment')=='NSE_EQ' and x.get('instrument_type')=='EQ'}
    _KM.clear(); _KM[f] = ((st.st_mtime_ns, st.st_size), km)
    return dict(km)
def score(key):
    h = ind.analyze(key, 'hours', 1, 30); d = ind.analyze(key, 'days', 1, 120)
    up1h = h['close'] > h['ema20'] and h['close'] > h['st'] and h['dir'] == 1
    upd = d['close'] > d['ema20'] and d['dir'] == 1
    dist = 100*(h['close']-h['ema20'])/h['ema20']
    s = h['adx'] + 1000*h['hist']/h['close'] + 2*dist + (15 if upd else -15)
    if not up1h: s -= 40
    return dict(close=h['close'], adx=h['adx'], hist=h['hist'], dist=round(dist,2), up1h=up1h, upd=upd,
                strong=up1h and upd, score=round(s,1))
def scan(symbols=None, sleep=0.15):
    """Rank NIFTY100 names by the same technical score; returns strong candidates only.
    READ ONLY. Used by rotation.py and by the pre-market top-3 picker."""
    km = keymap()
    pos = get('/v3/portfolio/mtf-positions').get('data') or []
    held = {(p.get('trading_symbol') or '').replace('-EQ','') for p in pos if (p.get('quantity') or 0) > 0}
    watch = [s.replace('-EQ','') for s in (symbols or NIFTY100) if s.replace('-EQ','') not in held]
    out = []
    for s in watch:
        if s not in km: continue
        try: out.append(dict(sym=s, key=km[s], **score(km[s])))
        except Exception as e:
            print(f'rotation: score failed for {s}: {type(e).__name__}: {e}', file=sys.stderr)
        time.sleep(sleep)
    return sorted(out, key=lambda x: -x['score']), held


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
    deployed = sum(h['value'] for h in H)
    freed = sum(h['value'] for h in H if any(a.startswith('CLOSE '+h['sym']+' ') for a in acts))
    freed += sum(h['value']-per_cap for h in keep if h['value'] > per_cap*1.02)
    after = deployed - freed; room = max(0.0, 0.5*cap - after)
    new_room = room/2; reserve = room - new_room
    print(f"\nMTF deployed {deployed:,.0f} ({100*deployed/cap:.1f}%) | after trims {after:,.0f} | room {room:,.0f} -> new-entry cap {new_room:,.0f}, PULLBACK RESERVE {reserve:,.0f} (not for rotation)")
    def nq(c):
        nonlocal new_room
        amt = min(per_cap, new_room); q = int(amt//c['close']); new_room -= q*c['close']; return q
    ci = 0
    while keep and ci < len(C):
        w = keep[-1]; c = C[ci]
        if c['score'] >= MARGIN*max(w['score'],0.1) and (not w['strong'] or w['score'] < c['score']):
            new_room += w['value']/2; reserve += w['value']/2; q = nq(c)
            acts.append(f"SWAP: CLOSE {w['sym']} ({w['qty']}, score {w['score']}) -> BUY {c['sym']} {q} @~{c['close']} (score {c['score']})")
            keep.pop(); keep.insert(0, dict(c, qty=q)); keep.sort(key=lambda x:-x['score']); ci += 1
        else: break
    while len(keep) < MAXN and ci < len(C):
        c = C[ci]; ci += 1; q = nq(c)
        if q <= 0: acts.append(f"SKIP {c['sym']}: new-entry cap used (reserve kept for pullbacks)"); break
        acts.append(f"ADD {c['sym']} {q} @~{c['close']} (~{q*c['close']:,.0f})"); keep.append(c)
    print("\nPROPOSED ACTIONS (not placed)"); [print(' ', a) for a in acts or ['none']]
    print(f"New-entry room left {new_room:,.0f} | pullback reserve left {reserve:,.0f}")
    print("\nTARGET BOOK:", ', '.join(k['sym'] for k in keep), f"~{per_cap:,.0f} each")
if __name__ == '__main__': main()
