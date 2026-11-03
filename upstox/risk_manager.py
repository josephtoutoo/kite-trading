#!/usr/bin/env python3
"""Upstox option risk manager (v2, 3 Nov). Manages ALL open long option positions (quantity>0),
including carry-forward (product D) ones. Keeps a SELL SL at 30% below the highest LTP seen
(never lowered, floored at seeded min_stop), and books profit with a market sell at entry*1.20.
Only acts 09:15-15:25 IST Mon-Fri. Cancels leftover SELL SL orders on contracts with no long position.
--dry-run: read-only, prints intended actions, ignores time window, writes no state."""
import json,os,sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import json,urllib.request,urllib.error,datetime
DRY="--dry-run" in sys.argv
D=os.path.dirname(os.path.abspath(__file__))
SF=os.path.join(D,"risk_state.json")
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
now=datetime.datetime.now(IST)
if not DRY and (now.weekday()>=5 or not (datetime.time(9,15)<=now.time()<=datetime.time(15,25))):
    print(json.dumps({"skipped":"outside 09:15-15:25 IST weekday window","now":now.isoformat()})); sys.exit(0)
t=json.load(open(os.path.join(D,"token.json")))["access_token"]
H={"Authorization":"Bearer "+t,"Accept":"application/json","Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
op=urllib.request.build_opener(urllib.request.ProxyHandler({"https":os.environ["UPSTOX_PROXY_URL"]}))
def req(u,body=None,method=None):
    r=urllib.request.Request(u,data=json.dumps(body).encode() if body is not None else None,headers=H,method=method)
    try: return json.load(op.open(r,timeout=20))
    except urllib.error.HTTPError as e: return {"status":"error","code":e.code,"body":e.read().decode()[:300]}
def write(u,body=None,method=None):
    if DRY: return {"status":"dry-run"}
    return req(u,body,method)
def tick(p): return round(round(p/0.05)*0.05,2)
def isopt(sym): return sym.endswith("CE") or sym.endswith("PE")
st=json.load(open(SF)) if os.path.exists(SF) else {}
seeds=st.setdefault("_min_stop_by_symbol",{})
for sym,v in {"INDHOTEL26NOV830CE":14.75,"SENSEX26N0576500CE":367.5,"PAYTM26NOV1720CE":44.25,"GLENMARK26NOV2400CE":56.3}.items():
    seeds[sym]=max(seeds.get(sym,0),v)
pr=req("https://api.upstox.com/v2/portfolio/short-term-positions")
if pr.get("status")!="success": print(json.dumps({"error":"positions fetch failed","resp":pr})); sys.exit(1)
pos=pr.get("data") or []
# --- account-wide loss circuit breaker (no auto-close; per-trade stops keep running) ---
import trade_lock
day_pnl=sum(float(p.get("pnl") if p.get("pnl") is not None else (p.get("unrealised") or 0)+(p.get("realised") or 0)) for p in pos)
breaker={"day_pnl":round(day_pnl,2),"threshold":-trade_lock.THRESHOLD,"locked":False}
if day_pnl< -trade_lock.THRESHOLD:
    breaker["locked"]=True; breaker["lock"]=trade_lock.lock(-day_pnl,f"day loss Rs {-day_pnl:,.0f} > Rs 30,000",dry=DRY)
breaker["can_enter"]=trade_lock.can_enter(1)
orders=req("https://api.upstox.com/v2/order/retrieve-all").get("data") or []
def pending_sl(k): return [o for o in orders if o["instrument_token"]==k and o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M")]
acts=[]; longs=set()
for p in pos:
    sym=p["trading_symbol"]; q=int(p.get("quantity") or 0)
    if not isopt(sym) or q<=0: continue
    k=p["instrument_token"]; longs.add(k); ltp=p["last_price"]
    avg=p.get("day_buy_price") if p.get("day_buy_quantity",0)>0 and p.get("overnight_quantity",0)<=0 else None
    avg=avg or p.get("average_price") or p.get("buy_price") or p.get("day_buy_price")
    if not avg or not ltp: acts.append({"sym":sym,"action":"SKIP_NO_PRICE"}); continue
    s=st.setdefault(k,{"high":avg}); s["sym"]=sym; s["high"]=max(s.get("high",avg),ltp)
    sls=pending_sl(k)
    for o in sls[1:]:  # never hold more than one SL per contract
        acts.append({"sym":sym,"action":"CANCEL_DUP_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
    sls=sls[:1]
    _ok,_why,_aq=trade_lock.check_order({"trading_symbol":sym,"instrument_token":k,"transaction_type":"SELL","order_type":"SL","product":p["product"],"quantity":q})
    if _aq<=0: acts.append({"sym":sym,"action":"SKIP_R8","why":_why}); continue
    q=min(q,_aq)  # rule 8: never sell more than live long qty in same product
    if ltp>=avg*1.2:
        for o in sls: write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE")
        r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":0,"tag":"target20","instrument_token":k,"order_type":"MARKET","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":0,"is_amo":False})
        acts.append({"sym":sym,"action":"BOOK_PROFIT","qty":q,"avg":avg,"ltp":ltp,"pnl_est":round((ltp-avg)*q),"resp":r.get("status")}); continue
    stop=tick(max(avg*0.7,s["high"]*0.7,seeds.get(sym,0),s.get("stop",0)))
    if stop>=ltp: stop=tick(ltp*0.97)  # can't place SL above market
    else: s["stop"]=stop  # ratchet: never lowered
    lim=tick(stop*0.95)
    if not sls:
        r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":lim,"tag":"trailsl","instrument_token":k,"order_type":"SL","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":stop,"is_amo":False})
        acts.append({"sym":sym,"action":"PLACE_SL","qty":q,"avg":round(avg,2),"ltp":ltp,"trigger":stop,"limit":lim,"resp":r.get("status"),"err":r.get("body")})
    else:
        o=sls[0]
        if stop>o["trigger_price"]*1.01 or o["quantity"]!=q:
            r=write("https://api-hft.upstox.com/v2/order/modify",{"order_id":o["order_id"],"quantity":q,"validity":"DAY","price":lim,"order_type":"SL","trigger_price":max(stop,o["trigger_price"]),"disclosed_quantity":0},method="PUT")
            acts.append({"sym":sym,"action":"MODIFY_SL","from":o["trigger_price"],"to":max(stop,o["trigger_price"]),"qty":q,"resp":r.get("status"),"err":r.get("body")})
        else: acts.append({"sym":sym,"action":"SL_OK","trigger":o["trigger_price"],"ltp":ltp})
# stray SELL SLs on option contracts with no long position -> cancel (prevents accidental shorts)
for o in orders:
    if o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M") and isopt(o.get("trading_symbol") or o.get("tradingsymbol","")) and o["instrument_token"] not in longs:
        acts.append({"sym":o.get("trading_symbol"),"action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
if not DRY: json.dump(st,open(SF,"w"))
print(json.dumps({"dry_run":DRY,"now":now.strftime("%Y-%m-%d %H:%M IST"),"breaker":breaker,"actions":acts},indent=1))
