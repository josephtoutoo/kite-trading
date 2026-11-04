"""MTF pullback-add scanner (READ ONLY - places NO orders).
For each long MTF stock (Upstox /v3/portfolio/mtf-positions + Kite product MTF, qty>0) that is in profit/flat:
 ADD  = daily trend up (close>EMA20, Supertrend(10,3) green, +DI>-DI) AND pullback AND turning up
 WAIT = daily up + pullback, no turn yet ; NO = daily trend broken (trim rules apply) ; HOLD = up, no pullback
Pullback: close 0-2% above daily EMA20 or 1h ST, or 3 lower 1h closes, or 1h RSI14<45 (dipped in last 3 bars).
Turn: last 1h close > prior 1h high, or 1h ST flipped green within last 3 bars.
Reserve rule: half of remaining MTF room is reserved for pullback adds; adds may use the full 50% budget and draw on the reserve first (reported as from_reserve).
Add size per broker: <= 10% of that broker's capital minus current value, and <= 50% total MTF budget minus MTF used.
Stop: nearer-below of 1h ST / daily EMA20. Target: roll-forward (entry*1.20, +20% steps past LTP).
Usage: python3 pullback_add.py [--json]"""
import json, sys, os, gzip, glob, urllib.request
sys.path.insert(0, '/workspace/upstox'); import ind
JS = '--json' in sys.argv
TOK = json.load(open('/workspace/upstox/token.json'))['access_token']
PER, TOT, STEP = 0.10, 0.50, 0.20
def get(p):
    r = urllib.request.Request('https://api.upstox.com'+p, headers={'Authorization':'Bearer '+TOK,'Accept':'application/json','User-Agent':'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r, timeout=20))
def keymap():
    f = sorted(glob.glob('/workspace/upstox/instruments_NSE_*.json.gz'))[-1]
    return {x['trading_symbol']: x['instrument_key'] for x in json.load(gzip.open(f)) if x.get('segment')=='NSE_EQ' and x.get('instrument_type')=='EQ'}
def rsi(c, n=14):
    g=[max(c[i]-c[i-1],0) for i in range(1,len(c))]; l=[max(c[i-1]-c[i],0) for i in range(1,len(c))]
    ag, al = ind.rma(g,n), ind.rma(l,n)
    return [100 if b==0 else 100-100/(1+a/b) for a,b in zip(ag,al)]
def series(key, unit, days):
    cs = ind.candles(key, unit, 1, days)
    import datetime as dt
    if unit=='hours' and cs and dt.datetime.fromisoformat(cs[-1][0])+dt.timedelta(hours=1)>dt.datetime.now().astimezone(): cs=cs[:-1]
    h=[x[2] for x in cs]; l=[x[3] for x in cs]; c=[x[4] for x in cs]
    st,d = ind.supertrend(h,l,c); e = ind.ema(c,20); pdi,mdi,adx = ind.dmi(h,l,c); m,s,hist = ind.macd(c)
    return dict(h=h,l=l,c=c,st=st,d=d,e=e,pdi=pdi,mdi=mdi,adx=adx,hist=hist,ts=cs[-1][0])
def analyze(key):
    D = series(key,'days',200); H = series(key,'hours',30)
    c=D['c'][-1]; hc=H['c']; r=rsi(hc)
    up = c>D['e'][-1] and D['d'][-1]==1 and D['pdi'][-1]>D['mdi'][-1]
    de = 100*(hc[-1]-D['e'][-1])/D['e'][-1]; ds = 100*(hc[-1]-H['st'][-1])/H['st'][-1]
    why=[]
    if 0<=de<=2: why.append(f'{de:.1f}% above D-EMA20')
    if H['d'][-1]==1 and 0<=ds<=2: why.append(f'{ds:.1f}% above 1h ST')
    if hc[-1]<hc[-2]<hc[-3]<hc[-4]: why.append('3 lower 1h closes')
    if min(r[-3:])<45: why.append(f'1h RSI dipped {min(r[-3:]):.0f}')
    if H['d'][-1]==-1: why.append('1h ST red (dip)')
    turn=[]
    if hc[-1]>H['h'][-2]: turn.append('1h close > prior high')
    if H['d'][-1]==1 and -1 in H['d'][-4:-1]: turn.append('1h ST reclaimed')
    last=hc[-1]
    stops=[x for x in (H['st'][-1] if H['d'][-1]==1 else None, D['e'][-1]) if x and x<last]
    return dict(last=round(last,2), d_up=up, d_adx=round(D['adx'][-1],1), d_ema20=round(D['e'][-1],2), d_st=round(D['st'][-1],2),
        h_st=round(H['st'][-1],2), h_dir=H['d'][-1], h_rsi=round(r[-1],1), h_macd_hist=round(H['hist'][-1],2),
        pullback=why, turn=turn, stop=round(max(stops),2) if stops else None, bar=H['ts'])
def upstox_side(km):
    out=[]; f=get('/v2/user/get-funds-and-margin')['data']['equity']; cap=f['available_margin']+f['used_margin']
    for p in get('/v3/portfolio/mtf-positions').get('data') or []:
        q=p.get('quantity') or p.get('net_quantity') or 0
        if q>0:
            s=p['trading_symbol'].replace('-EQ','')
            out.append(dict(broker='upstox',sym=s,qty=q,avg=float(p.get('average_price') or 0),key=p.get('instrument_token') or km.get(s)))
    return cap,out
def kite_side(km):
    import broker_kite as bk
    if not bk.valid(): return None,[],bk.why()
    k=bk.client(); m=k.margins('equity'); cap=float(m['net'])+float(m['utilised']['debits'])
    out=[]
    for p in (k.positions().get('net') or []):
        if p['product']=='MTF' and p['exchange'] in('NSE','BSE') and int(p['quantity'])>0:
            out.append(dict(broker='kite',sym=p['tradingsymbol'],qty=int(p['quantity']),avg=float(p['average_price']),key=km.get(p['tradingsymbol'])))
    try:
        for h in k.holdings():
            if int(h.get('mtf',{}).get('quantity') or 0)>0 and not any(o['sym']==h['tradingsymbol'] for o in out):
                out.append(dict(broker='kite',sym=h['tradingsymbol'],qty=int(h['mtf']['quantity']),avg=float(h['mtf'].get('average_price') or h['average_price']),key=km.get(h['tradingsymbol'])))
    except Exception: pass
    return cap,out,'ok'
def main():
    km=keymap(); notes=[]
    try: ucap,U=upstox_side(km)
    except Exception as e: ucap,U=None,[]; notes.append(f'Upstox read failed: {type(e).__name__}')
    try: kcap,K,kw=kite_side(km)
    except Exception as e: kcap,K,kw=None,[],f'{type(e).__name__}'
    if kw!='ok': notes.append('Kite unavailable: '+kw.split(':')[0])
    caps={'upstox':ucap,'kite':kcap}; rows=[]; cache={}
    used={b:0.0 for b in caps}
    for p in U+K: used[p['broker']]+=p['qty']*p['avg']
    for p in U+K:
        r=dict(broker=p['broker'],sym=p['sym'],qty=p['qty'],avg=round(p['avg'],2))
        if not p['key']: r.update(verdict='DATA N/A',note='no NSE instrument key'); rows.append(r); continue
        try: a=cache.get(p['key']) or analyze(p['key']); cache[p['key']]=a
        except Exception as e: r.update(verdict='DATA N/A',note=f'candles failed: {type(e).__name__}'); rows.append(r); continue
        r.update(a); last=a['last']; pnl=(last-p['avg'])/p['avg']*100 if p['avg'] else 0; r['pnl_pct']=round(pnl,2)
        tgt=round(p['avg']*(1+STEP),2)
        while tgt<=last: tgt=round(tgt+p['avg']*STEP,2)
        r['target']=tgt
        cap=caps[p['broker']]
        if cap:
            val=p['qty']*last; gap=min(PER*cap-val, TOT*cap-used[p['broker']])
            r['cap_10pct']=round(PER*cap); r['value']=round(val); r['add_qty']=max(int(gap//last),0)
            room=max(TOT*cap-used[p['broker']],0); r['reserve_room']=round(room/2); r['from_reserve']=round(min(r['add_qty']*last,room/2))
            # margin gate: shrink the add to what the broker's margin API covers right now
            if r['add_qty']>0:
                try:
                    import mtf_sizing as ms
                    g=ms.size(p['broker'],p['sym'],last,mode='add')
                    if g['qty']<r['add_qty']:
                        r['add_qty']=g['qty']; r['margin_limited']=True
                    r['margin_req']=g.get('margin'); r['leverage']=g.get('leverage')
                except Exception as e: r['margin_check']=f'failed: {type(e).__name__}'
        else: r['add_qty']=None
        if not a['d_up']: v,n='NO','daily trend broken - trim/exit rules apply'
        elif pnl< -0.5: v,n='NO','position in loss - no averaging down'
        elif not a['pullback']: v,n='HOLD','trend up, no pullback'
        elif not a['turn']: v,n='WAIT','pullback, no turn up yet'
        else: v,n='ADD','pullback + turn up'
        if v=='ADD' and r['add_qty']==0: n='at cap - no add'
        if v=='ADD' and r['add_qty'] is None: n+=' (capital unknown, size n/a)'
        r.update(verdict=v,note=n); rows.append(r)
    res=dict(read_only=True,capital=caps,mtf_used={b:round(x) for b,x in used.items()},rows=rows,notes=notes)
    if JS: print(json.dumps(res,default=str)); return
    print(f"READ ONLY. Capital upstox={ucap and round(ucap)} kite={kcap and round(kcap)} | MTF used {res['mtf_used']}")
    for b,c in caps.items():
        if c: room=max(TOT*c-used[b],0); print(f"{b}: MTF used {used[b]:,.0f} / budget {TOT*c:,.0f} | new-entry cap {room/2:,.0f} | pullback reserve {room/2:,.0f}")
    for n in notes: print('NOTE:',n)
    print(f"{'BRK':<6}{'SYM':<11}{'QTY':>5}{'AVG':>9}{'LAST':>9}{'P&L%':>7} {'D':<4}{'ADDQ':>5}{'STOP':>9}{'TGT':>9}  VERDICT")
    for r in rows:
        print(f"{r['broker']:<6}{r['sym']:<11}{r['qty']:>5}{r['avg']:>9}{str(r.get('last','-')):>9}{str(r.get('pnl_pct','-')):>7} {('UP' if r.get('d_up') else 'DN') if 'd_up' in r else '-':<4}{str(r.get('add_qty','-')):>5}{str(r.get('stop','-')):>9}{str(r.get('target','-')):>9}  {r['verdict']}")
    for r in rows: print(f" - {r['sym']} ({r['broker']}): {r['verdict']} - {r['note']}" + (f" | add from reserve {r['from_reserve']}" if r.get('add_qty') else '') + (f" | pullback: {', '.join(r['pullback']) or 'none'}; turn: {', '.join(r['turn']) or 'none'}; 1h RSI {r['h_rsi']}, D-ADX {r['d_adx']}" if 'pullback' in r else ''))
if __name__=='__main__': main()
