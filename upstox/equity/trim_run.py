import json,os,sys,urllib.request,time
sys.path.insert(0,'/workspace/upstox'); import trade_lock
T=json.load(open('/workspace/upstox/token.json'))['access_token']
H={"Authorization":"Bearer "+T,"Accept":"application/json","Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
op=urllib.request.build_opener(urllib.request.ProxyHandler({"https":os.environ["UPSTOX_PROXY_URL"]}))
def req(u,body=None):
    r=urllib.request.Request(u,data=json.dumps(body).encode() if body is not None else None,headers=H)
    try: return json.load(op.open(r,timeout=20))
    except urllib.error.HTTPError as e: return {"status":"error","code":e.code,"body":e.read().decode()[:300]}
def tick(p,t=0.05): return round(round(p/t)*t,2)
orders=json.loads(sys.argv[1])
for sym,k,side,qty in orders:
    q=req("https://api.upstox.com/v2/market-quote/ltp?instrument_key="+k)
    ltp=list(q['data'].values())[0]['last_price']
    px=tick(ltp*(0.999 if side=="SELL" else 1.001), 0.1 if ltp>=1000 else 0.05)
    o={"trading_symbol":sym,"instrument_token":k,"transaction_type":side,"order_type":"LIMIT","product":"MTF","quantity":qty,"price":px}
    ok,why,adj=trade_lock.check_order(o)
    print(sym,side,qty,'ltp',ltp,'px',px,'check',ok,why,adj)
    if not ok: continue
    r=req("https://api-hft.upstox.com/v2/order/place",{"quantity":adj,"product":"MTF","validity":"DAY","price":px,"tag":"rotation","instrument_token":k,"order_type":"LIMIT","transaction_type":side,"disclosed_quantity":0,"trigger_price":0,"is_amo":False})
    print(' ->',r)
