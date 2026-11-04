import json,os,sys,urllib.request,urllib.parse
sys.path.insert(0,'/workspace/upstox'); import trade_lock
T=json.load(open('/workspace/upstox/token.json'))['access_token']
H={"Authorization":"Bearer "+T,"Accept":"application/json","Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
op=urllib.request.build_opener(urllib.request.ProxyHandler({"https":os.environ["UPSTOX_PROXY_URL"]}))
def req(u,body=None):
    r=urllib.request.Request(u,data=json.dumps(body).encode() if body is not None else None,headers=H)
    try: return json.load(op.open(r,timeout=20))
    except urllib.error.HTTPError as e: return {"status":"error","code":e.code,"body":e.read().decode()[:300]}
def tick(p,t): return round(round(p/t)*t,2)
orders=req("https://api.upstox.com/v2/order/retrieve-all")['data']
for p in req("https://api.upstox.com/v3/portfolio/mtf-positions")['data']:
    k=p['instrument_token']; sym=p['trading_symbol']; q=int(p['quantity'])
    if q<=0: continue
    if any(o['instrument_token']==k and o['transaction_type']=='SELL' and o['order_type'] in('SL','SL-M') and o['status'] in('trigger pending','open') for o in orders): print(sym,'has SL'); continue
    qd=list(req("https://api.upstox.com/v2/market-quote/quotes?instrument_key="+urllib.parse.quote(k,safe=''))['data'].values())[0]
    ltp=qd['last_price']; lcl=float(qd.get('lower_circuit_limit') or 0); t=0.1 if ltp>=1000 else 0.05
    trig=tick(max(ltp*0.92,lcl*1.02),t); lim=tick(max(trig*0.995,lcl*1.005),t)
    ok,why,adj=trade_lock.check_order({"trading_symbol":sym,"instrument_token":k,"transaction_type":"SELL","order_type":"SL","product":"MTF","quantity":q})
    print(sym,q,'ltp',ltp,'lcl',lcl,'trig',trig,'lim',lim,ok,why)
    if not ok: continue
    print(' ->',req("https://api-hft.upstox.com/v2/order/place",{"quantity":adj,"product":"MTF","validity":"DAY","price":lim,"tag":"rotsl","instrument_token":k,"order_type":"SL","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":trig,"is_amo":False}))
