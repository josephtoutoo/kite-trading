#!/usr/bin/env python3
"""Upstox option risk manager (v2, 3 Nov). Manages ALL open long option positions (quantity>0),
including carry-forward (product D) ones. Keeps a SELL SL at 30% below the highest LTP seen
(never lowered, floored at seeded min_stop). Target roll-forward (3 Nov): at LTP>=target (start entry*1.20)
checks underlying trend (index opts 5m, stock opts/futures 15m; CE/FUT need close>EMA20 & ST green, PE need
close<EMA20 & ST red). Intact -> target += entry*0.20, stop floor = max(prev target, entry). Broken -> LIMIT sell
at LTP*0.995 for live qty. Long futures: 1.5% trail from high, target step +2%.
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
import ind,gzip
INDEX_KEYS=trade_lock.INDEX_KEYS
EQF=os.path.join(D,"eq_keys.json")
def eq_keys():
    if not os.path.exists(EQF):
        raw=urllib.request.urlopen(urllib.request.Request("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",headers={"User-Agent":"Mozilla/5.0"}),timeout=60).read()
        d=json.loads(gzip.decompress(raw)); json.dump({x["trading_symbol"]:x["instrument_key"] for x in d if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"},open(EQF,"w"))
    return json.load(open(EQF))
def trend_ok(sym):
    """Underlying trend on last CLOSED candle. Index opts: 5m; stock opts/futures: 15m.
    CE / long FUT: close>EMA20 & ST green. PE: close<EMA20 & ST red."""
    u=trade_lock.underlying(sym)
    if u in INDEX_KEYS: key,iv,days=INDEX_KEYS[u],5,5
    else: key,iv,days=eq_keys().get(u),15,10
    if not key: return False,f"no underlying key for {u}"
    a=ind.analyze(key,"minutes",iv,days)
    up=a["close"]>a["ema20"] and a["dir"]==1; dn=a["close"]<a["ema20"] and a["dir"]==-1
    ok=dn if sym.endswith("PE") else up
    return ok,f"{u} {iv}m@{a['last_ts'][11:16]} close {a['close']} ema20 {a['ema20']} ST {'green' if a['dir']==1 else 'red'}"
def isfut(sym): return sym.endswith("FUT")
for p in pos:
    sym=p["trading_symbol"]; q=int(p.get("quantity") or 0)
    if not (isopt(sym) or isfut(sym)) or q<=0: continue
    fut=isfut(sym)
    k=p["instrument_token"]; longs.add(k); ltp=p["last_price"]
    avg=p.get("day_buy_price") if p.get("day_buy_quantity",0)>0 and p.get("overnight_quantity",0)<=0 else None
    avg=avg or p.get("average_price") or p.get("buy_price") or p.get("day_buy_price")
    if not avg or not ltp: acts.append({"sym":sym,"action":"SKIP_NO_PRICE"}); continue
    TRAIL=0.015 if fut else 0.30; STEP=0.02 if fut else 0.20
    s=st.setdefault(k,{"high":avg}); s["sym"]=sym; s["high"]=max(s.get("high",avg),ltp)
    s.setdefault("entry",avg); s.setdefault("target",round(avg*(1+STEP),2))
    _ok,_why,_aq=trade_lock.check_order({"trading_symbol":sym,"instrument_token":k,"transaction_type":"SELL","order_type":"SL","product":p["product"],"quantity":q})
    if _aq<=0: acts.append({"sym":sym,"action":"SKIP_R8","why":_why}); continue
    q=min(q,_aq)  # rule 8
    sls=pending_sl(k)
    for o in sls[1:]:
        acts.append({"sym":sym,"action":"CANCEL_DUP_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
    sls=sls[:1]
    floor=0
    while ltp>=s["target"]:
        ok,why=trend_ok(sym)
        if not ok:
            for o in sls: write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE")
            lim=tick(ltp*0.995)
            r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":lim,"tag":"target_book","instrument_token":k,"order_type":"LIMIT","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":0,"is_amo":False})
            acts.append({"sym":sym,"action":"BOOK_PROFIT_TREND_BROKEN","qty":q,"avg":round(avg,2),"ltp":ltp,"limit":lim,"target":s["target"],"trend":why,"pnl_est":round((lim-avg)*q),"resp":r.get("status"),"err":r.get("body")})
            break
        prev=s["target"]; s["target"]=round(prev+avg*STEP,2); floor=max(floor,prev,avg)
        acts.append({"sym":sym,"action":"ROLL_TARGET","from":prev,"to":s["target"],"trend":why})
    else:
        stop=tick(max(avg*(1-TRAIL),s["high"]*(1-TRAIL),seeds.get(sym,0),s.get("stop",0),floor))
        if stop>=ltp: stop=tick(ltp*(0.995 if fut else 0.97))
        else: s["stop"]=stop  # ratchet: never lowered
        lim=tick(stop*(0.99 if fut else 0.95))
        info={"sym":sym,"qty":q,"avg":round(avg,2),"ltp":ltp,"high":s["high"],"target":s["target"],"trigger":stop,"limit":lim}
        if not sls:
            r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":lim,"tag":"trailsl","instrument_token":k,"order_type":"SL","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":stop,"is_amo":False})
            acts.append({**info,"action":"PLACE_SL","resp":r.get("status"),"err":r.get("body")})
        else:
            o=sls[0]
            if stop>o["trigger_price"]*1.001 or o["quantity"]!=q:
                t2=max(stop,o["trigger_price"])
                r=write("https://api-hft.upstox.com/v2/order/modify",{"order_id":o["order_id"],"quantity":q,"validity":"DAY","price":tick(t2*(0.99 if fut else 0.95)),"order_type":"SL","trigger_price":t2,"disclosed_quantity":0},method="PUT")
                acts.append({**info,"action":"MODIFY_SL","from":o["trigger_price"],"to":t2,"resp":r.get("status"),"err":r.get("body")})
            else: acts.append({**info,"action":"SL_OK","sl_trigger":o["trigger_price"]})
# stray SELL SLs on option contracts with no long position -> cancel (prevents accidental shorts)
for o in orders:
    if o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M") and isopt(o.get("trading_symbol") or o.get("tradingsymbol","")) and o["instrument_token"] not in longs:
        acts.append({"sym":o.get("trading_symbol"),"action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
if not DRY: json.dump(st,open(SF,"w"))
print(json.dumps({"dry_run":DRY,"now":now.strftime("%Y-%m-%d %H:%M IST"),"breaker":breaker,"actions":acts},indent=1))
