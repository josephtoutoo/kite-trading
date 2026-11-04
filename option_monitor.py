import sys,json,gzip,os,datetime as dt,urllib.parse
sys.path.insert(0,'/workspace/upstox')
import broker_kite as b, ind
SF='/workspace/kite/option_monitor_state.json'
st=json.load(open(SF)) if os.path.exists(SF) else {"peaks":{},"alerted":{}}
pos=[p for p in b.positions() if p['exchange'] in('NFO','BFO') and p['quantity']>0]
ords=b.orders()
import glob as _glob
_f=sorted(_glob.glob('/workspace/upstox/instruments_NSE_*.json.gz'))[-1]  # cwd-independent (routine runs from /workspace/kite)
nse=json.load(gzip.open(_f))
def eqkey(name):
    for r in nse:
        if r.get('segment')=='NSE_EQ' and r.get('trading_symbol')==name: return r['instrument_key']
out=[]
for p in pos:
    ts=p['trading_symbol']; ex=p['exchange']; uk=b.to_upstox(ex,ts)
    row=[r for r in b.dump(ex) if r['tradingsymbol']==ts][0]
    q=ind.get('/v2/market-quote/ltp?instrument_key='+urllib.parse.quote(uk))['data']; ltp=list(q.values())[0]['last_price']
    # high since entry from 1m candles
    entry_time=min(o['order_timestamp'] for o in ords if o['trading_symbol']==ts and o['transaction_type']=='BUY' and o['status']=='complete')
    try:
        cs=ind.get('/v3/historical-candle/intraday/'+urllib.parse.quote(uk,safe='')+'/minutes/1')['data']['candles']
        hi=max([c[2] for c in cs if c[0][:19].replace('T',' ')>=entry_time[:16]] or [ltp])
    except Exception as e: hi=ltp
    hi=max(hi,ltp,st['peaks'].get(ts,0)); st['peaks'][ts]=hi
    name=row['name']; under={'SENSEX':'BSE_INDEX|SENSEX','NIFTY':'NSE_INDEX|Nifty 50','BANKNIFTY':'NSE_INDEX|Nifty Bank','FINNIFTY':'NSE_INDEX|Nifty Fin Service','MIDCPNIFTY':'NSE_INDEX|NIFTY MID SELECT','BANKEX':'BSE_INDEX|BANKEX'}.get(name) or eqkey(name)
    a=ind.analyze(under,'minutes',15,10)
    sls=[o for o in ords if o['trading_symbol']==ts and o['transaction_type']=='SELL' and o['status'] in('trigger pending','open') and o['order_type']=='SL']
    out.append(dict(ts=ts,ex=ex,uk=uk,qty=p['quantity'],avg=p['average_price'],ltp=ltp,high=hi,expiry=row['expiry'],tick=row['tick_size'],
        sl=[(o['order_id'],o['quantity'],o['trigger_price'],o['price']) for o in sls],under=under,trend=a))
json.dump(st,open(SF,'w'),indent=1)
# --- SL confirmation failures raised by the entry path (sl_confirm.py) ---
# A new entry whose stop never reached 'open'/'trigger pending' lands in this file; alert until it is live.
fails=[]
try:
    fc=json.load(open('/workspace/upstox/sl_confirm_failures.json')).get('failures') or []
    fails=[f for f in fc if not f.get('resolved')]
except Exception: pass
# cross-check: is the SL actually live now? then mark it resolved instead of alerting
if fails:
    import sl_confirm as sc
    still=[]
    for f in fails:
        if sc.live_sl(f['broker'],f['symbol'],f.get('qty'),f.get('trigger')):
            sc.clear(f['broker'],f['symbol'])
        else: still.append(f)
    fails=still
alerts=[dict(type='SL_CONFIRM_FAILED',broker=f['broker'],symbol=f['symbol'],order=f.get('order'),
             reason=f.get('reason'),trigger=f.get('trigger'),qty=f.get('qty'),since=f.get('ts')) for f in fails]
# a position with no live SL at all is the same emergency, whatever the entry path reported
for o in out:
    if not o['sl']:
        alerts.append(dict(type='NO_LIVE_SL',broker='kite',symbol=o['ts'],qty=o['qty'],ltp=o['ltp'],
                           reason='long position with no open/trigger-pending SELL SL'))
print(json.dumps({'positions':out,'alerts':alerts},indent=1))
