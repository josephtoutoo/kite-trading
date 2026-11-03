#!/usr/bin/env python3
"""Upstox option risk manager: for option positions opened today (day_buy_quantity>0, net long),
ensure a SELL SL order at 30% below avg (trailed to 30% below the highest LTP seen),
and book profit with a market sell when LTP >= avg*1.20. Prints a JSON summary of actions."""
import json,os,urllib.request,urllib.error,time
D=os.path.dirname(os.path.abspath(__file__))
SF=os.path.join(D,"risk_state.json")
t=json.load(open(os.path.join(D,"token.json")))["access_token"]
H={"Authorization":"Bearer "+t,"Accept":"application/json","Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
op=urllib.request.build_opener(urllib.request.ProxyHandler({"https":os.environ["UPSTOX_PROXY_URL"]}))
def req(u,body=None,method=None):
    r=urllib.request.Request(u,data=json.dumps(body).encode() if body is not None else None,headers=H,method=method)
    try: return json.load(op.open(r,timeout=20))
    except urllib.error.HTTPError as e: return {"status":"error","code":e.code,"body":e.read().decode()[:300]}
def tick(p): return round(round(p/0.05)*0.05,2)
st=json.load(open(SF)) if os.path.exists(SF) else {}
pos=req("https://api.upstox.com/v2/portfolio/short-term-positions").get("data",[])
orders=req("https://api.upstox.com/v2/order/retrieve-all").get("data",[])
acts=[]
for p in pos:
    sym=p["trading_symbol"]; q=p["quantity"]
    if not (sym.endswith("CE") or sym.endswith("PE")) or q<=0 or p.get("day_buy_quantity",0)<=0: continue
    k=p["instrument_token"]; avg=p.get("day_buy_price") or p["average_price"]; ltp=p["last_price"]
    s=st.setdefault(k,{"high":avg}); s["high"]=max(s["high"],ltp)
    sls=[o for o in orders if o["instrument_token"]==k and o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M")]
    if ltp>=avg*1.2:
        for o in sls: req("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE")
        r=req("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":0,"tag":"target20","instrument_token":k,"order_type":"MARKET","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":0,"is_amo":False})
        acts.append({"sym":sym,"action":"BOOKED_PROFIT","avg":avg,"ltp":ltp,"pnl_est":round((ltp-avg)*q),"resp":r}); continue
    stop=tick(max(avg*0.7,s["high"]*0.7))
    if stop>=ltp: stop=tick(ltp*0.97)
    lim=tick(stop*0.95)
    if not sls:
        r=req("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":lim,"tag":"trailsl","instrument_token":k,"order_type":"SL","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":stop,"is_amo":False})
        acts.append({"sym":sym,"action":"PLACED_SL","trigger":stop,"resp":r.get("status"),"err":r.get("body")})
    else:
        o=sls[0]
        if stop>o["trigger_price"]*1.01:
            r=req("https://api-hft.upstox.com/v2/order/modify",{"order_id":o["order_id"],"quantity":q,"validity":"DAY","price":lim,"order_type":"SL","trigger_price":stop,"disclosed_quantity":0},method="PUT")
            acts.append({"sym":sym,"action":"TRAILED_SL","from":o["trigger_price"],"to":stop,"resp":r.get("status"),"err":r.get("body")})
json.dump(st,open(SF,"w"))
print(json.dumps(acts))
