#!/usr/bin/env python3
"""Upstox option risk manager (v2, 3 Nov). Manages ALL open long option positions (quantity>0),
including carry-forward (product D) ones. Keeps a SELL SL at 30% below the highest LTP seen
(never lowered, floored at seeded min_stop). Target roll-forward (3 Nov): at LTP>=target (start entry*1.20)
checks underlying trend (index opts 5m, stock opts/futures 15m; CE/FUT need close>EMA20 & ST green, PE need
close<EMA20 & ST red). Intact -> target += entry*0.20, stop floor = target two steps back (1st roll: entry). Broken -> LIMIT sell
at LTP*0.995 for live qty. Long futures: 1.5% trail from high, target step +2%.
Only acts 09:15-15:25 IST Mon-Fri. Cancels leftover SELL SL orders on contracts with no long position.
MTF: not managed here any more (5 Nov) - equity/upstox_mtf_stops.py owns MTF stops (plain protective only, no trail).
Kite (4 Nov): also manages Kite long options/futures (Upstox LTP+trend, Kite SL/LIMIT orders); no-op if Kite token invalid.
--dry-run: read-only, prints intended actions, ignores time window, writes no state."""
import json,os,sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import json,urllib.request,urllib.error,datetime
import market_calendar
import api                     # pooled, proxied, timed client (5 Nov 2026)
DRY="--dry-run" in sys.argv
D=os.path.dirname(os.path.abspath(__file__))
SF=os.path.join(D,"risk_state.json")
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
now=datetime.datetime.now(IST)
if not DRY and not market_calendar.is_trading_day(now.date()):
    print(json.dumps({"skipped":"exchange holiday / market closed","now":now.isoformat()})); sys.exit(0)
if not DRY and (now.weekday()>=5 or not (datetime.time(9,15)<=now.time()<=datetime.time(15,25))):
    print(json.dumps({"skipped":"outside 09:15-15:25 IST weekday window","now":now.isoformat()})); sys.exit(0)
def req(u,body=None,method=None):
    """Parsed JSON, or {'status':'error','code':..,'body':..} exactly as before. Shared keep-alive
    client, always through the static-IP proxy (this desk reads and writes order endpoints there),
    20 s timeout, and a write is never retried."""
    return api.call(u,body=body,method=method,proxy=True,timeout=20)
def write(u,body=None,method=None):
    if DRY: return {"status":"dry-run"}
    return req(u,body,method)
def tick(p): return round(round(p/0.05)*0.05,2)
def isopt(sym): return len(sym)>2 and sym[-2:] in("CE","PE") and sym[-3].isdigit()  # BAJFINANCE ends in CE: require strike digit
st=json.load(open(SF)) if os.path.exists(SF) else {}
seeds=st.setdefault("_min_stop_by_symbol",{})
for sym,v in {"INDHOTEL26NOV830CE":14.75,"SENSEX26N0576500CE":367.5,"PAYTM26NOV1720CE":44.25,"GLENMARK26NOV2400CE":56.3}.items():
    seeds[sym]=max(seeds.get(sym,0),v)
pr=req("https://api.upstox.com/v2/portfolio/short-term-positions")
if pr.get("status")!="success": print(json.dumps({"error":"positions fetch failed","resp":pr})); sys.exit(1)
pos=pr.get("data") or []
# --- account-wide loss circuit breaker (no auto-close; per-trade stops keep running) ---
import trade_lock
import market_calendar
import sl_confirm as sc  # clears SL_CONFIRM_FAILED once a live SL is seen
# (5 Nov) include Upstox MTF equity P&L: it is not in short-term-positions, so the breaker used to
# miss it. One bad MTF day could cross the Rs 30,000 cutoff without ever locking the account.
try:
    _mtfp=(req("https://api.upstox.com/v3/portfolio/mtf-positions").get("data") or [])
except Exception:
    _mtfp=[]
day_pnl=sum(float(p.get("pnl") if p.get("pnl") is not None else (p.get("unrealised") or 0)+(p.get("realised") or 0)) for p in pos+_mtfp)
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
_EQK=None
def eq_keys():
    """trading_symbol -> instrument_key for NSE cash. Parsed once per process; it used to
    re-read eq_keys.json (93 KB) on every trend_ok() call, i.e. once per open position."""
    global _EQK
    if _EQK is not None: return _EQK
    if not os.path.exists(EQF):
        raw=urllib.request.urlopen(urllib.request.Request("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",headers={"User-Agent":"Mozilla/5.0"}),timeout=60).read()
        d=json.loads(gzip.decompress(raw)); json.dump({x["trading_symbol"]:x["instrument_key"] for x in d if x.get("segment")=="NSE_EQ" and x.get("instrument_type")=="EQ"},open(EQF,"w"))
    _EQK=api.file_json(EQF)
    return _EQK
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
    # ---- exit rule (4 Nov, t287u/t288u): INDEX OPTIONS ONLY get the trend exit ----
    # Losses are always bounded by a fixed 30% floor under entry. For index options, once the contract's high
    # is 15% above entry the exit also follows the underlying 5m trend (CE sold when close<EMA20 with
    # Supertrend red, PE the opposite), so winners run. Stock options and futures keep the plain % stop.
    INDEX_OPT = trade_lock.underlying(sym) in INDEX_KEYS
    ok,why=trend_ok(sym) if (not fut and INDEX_OPT) else (True,"not an index option")
    if INDEX_OPT and s["high"]>=avg*1.15 and not ok:
        for o in sls: write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE")
        lim=tick(ltp*0.995)
        r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":lim,"tag":"target_book","instrument_token":k,"order_type":"LIMIT","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":0,"is_amo":False})
        acts.append({"sym":sym,"action":"TREND_EXIT","qty":q,"avg":round(avg,2),"ltp":ltp,"limit":lim,"peak":s["high"],"trend":why,"pnl_est":round((lim-avg)*q),"resp":r.get("status"),"err":r.get("body")})
        continue
    while ltp>=s["target"]:
        prev=s["target"]; s["target"]=round(prev+avg*STEP,2); floor=max(floor,round(prev-avg*STEP,2),avg)  # stop = target two steps back (1st roll: breakeven)
        acts.append({**{"sym":sym},"action":"ROLL_TARGET","from":prev,"to":s["target"],"trend":why})
    if True:
        # 30% fixed floor under entry (never lowered); floor rises only when a target rolls
        stop=tick(max(avg*(1-TRAIL),seeds.get(sym,0),s.get("stop",0),floor))
        if stop>=ltp: stop=tick(ltp*(0.995 if fut else 0.97))
        else: s["stop"]=stop  # ratchet: never lowered
        lim=tick(stop*(0.99 if fut else 0.95))
        info={"sym":sym,"qty":q,"avg":round(avg,2),"ltp":ltp,"high":s["high"],"target":s["target"],"trigger":stop,"limit":lim}
        if not sls:
            r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":lim,"tag":"trailsl","instrument_token":k,"order_type":"SL","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":stop,"is_amo":False})
            acts.append({**info,"action":"PLACE_SL","resp":r.get("status"),"err":r.get("body")})
            if not DRY and r.get("status")=="success": sc.clear("upstox",sym)
        else:
            o=sls[0]
            if not DRY: sc.clear("upstox",sym)
            if stop>o["trigger_price"]*1.001 or o["quantity"]!=q:
                t2=max(stop,o["trigger_price"])
                r=write("https://api-hft.upstox.com/v2/order/modify",{"order_id":o["order_id"],"quantity":q,"validity":"DAY","price":tick(t2*(0.99 if fut else 0.95)),"order_type":"SL","trigger_price":t2,"disclosed_quantity":0},method="PUT")
                acts.append({**info,"action":"MODIFY_SL","from":o["trigger_price"],"to":t2,"resp":r.get("status"),"err":r.get("body")})
            else: acts.append({**info,"action":"SL_OK","sl_trigger":o["trigger_price"]})
# ---------------- Upstox MTF equity: NO stop management here (5 Nov 2026) ----------------
# This block used to ratchet a SELL SL to 30% below the highest LTP seen. That is the OPTION
# stop rule and it does not belong in the equity MTF book: per t263u, MTF positions get plain
# protective stops only, never a trail. equity/upstox_mtf_stops.py owns MTF stops (5% below LTP
# or the 1h Supertrend when nearer, never lowered, clamped inside the circuit band), and having
# two writers on the same orders made whichever ran last win. risk_manager keeps only the
# bookkeeping job: cancel a leftover MTF SELL SL on a name that is no longer held.
mtf_longs=set()
try:
    mr=req("https://api.upstox.com/v3/portfolio/mtf-positions")
    if mr.get("status")!="success":
        acts.append({"action":"MTF_SKIPPED","why":"mtf positions fetch failed","code":mr.get("code")})
    else:
        for p in mr.get("data") or []:
            k=p.get("instrument_token")
            if k and int(p.get("quantity") or 0)>0: mtf_longs.add(k)
        for o in orders:
            if o["transaction_type"]=="SELL" and (o.get("product") or "").upper()=="MTF" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M") and o["instrument_token"] not in mtf_longs:
                acts.append({"sym":o.get("trading_symbol"),"action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
except Exception as e: acts.append({"action":"MTF_ERROR","why":f"{type(e).__name__}: {e}"})
# stray SELL SLs on option contracts with no long position -> cancel (prevents accidental shorts)
for o in orders:
    if o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M") and isopt(o.get("trading_symbol") or o.get("tradingsymbol","")) and o["instrument_token"] not in longs:
        acts.append({"sym":o.get("trading_symbol"),"action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
# ---------------- Kite positions (4 Nov): Upstox prices/trend, Kite orders, same stop/roll rules ----------------
kacts=[]
try:
    import broker_kite as bk
    if not bk.valid(): kacts.append({"action":"KITE_SKIPPED","why":"Kite token invalid - no-op"}); bk.log("risk_manager: kite skipped, invalid token")
    else:
        kpos=bk.positions(); kord=bk.orders(); klongs=set()
        def kpending(ts): return [o for o in kord if o["trading_symbol"]==ts and o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M")]
        for p in kpos:
            sym=p["trading_symbol"]; q=p["quantity"]; ex=p["exchange"]
            if not (isopt(sym) or isfut(sym)) or q<=0: continue
            fut=isfut(sym); klongs.add(sym); kk="KITE|%s|%s"%(ex,sym)
            ukey=bk.to_upstox(ex,sym); ltp=None
            if ukey:
                import urllib.parse
                d=req("https://api.upstox.com/v2/market-quote/ltp?instrument_key="+urllib.parse.quote(ukey,safe="")).get("data") or {}
                for v in d.values(): ltp=float(v.get("last_price") or 0) or None
            ltp=ltp or p["last_price"]; avg=p["average_price"] or p["buy_price"]
            if not avg or not ltp: kacts.append({"sym":sym,"broker":"kite","action":"SKIP_NO_PRICE"}); continue
            TRAIL=0.015 if fut else 0.30; STEP=0.02 if fut else 0.20
            s=st.setdefault(kk,{"high":avg}); s["sym"]=sym; s["broker"]="kite"; s["high"]=max(s.get("high",avg),ltp)
            s.setdefault("entry",avg); s.setdefault("target",round(avg*(1+STEP),2))
            _ok,_why,_aq=trade_lock.check_order({"trading_symbol":sym,"instrument_token":kk,"transaction_type":"SELL","order_type":"SL","product":p["product"],"quantity":q},broker="kite")
            if _aq<=0: kacts.append({"sym":sym,"broker":"kite","action":"SKIP_R8","why":_why}); continue
            q=min(q,_aq)
            sls=kpending(sym)
            for o in sls[1:]: kacts.append({"sym":sym,"broker":"kite","action":"CANCEL_DUP_SL","order":o["order_id"],"resp":bk.cancel(o["order_id"],dry=DRY)})
            sls=sls[:1]; floor=0
            INDEX_OPT = trade_lock.underlying(sym) in INDEX_KEYS
            ok,why=trend_ok(sym) if (not fut and INDEX_OPT) else (True,"not an index option")
            if INDEX_OPT and s["high"]>=avg*1.15 and not ok:
                for o in sls: bk.cancel(o["order_id"],dry=DRY)
                lim=tick(ltp*0.995)
                r=bk.place(ex,sym,"SELL",q,lim,tag="target_book",product=p["product"],dry=DRY)
                kacts.append({"sym":sym,"broker":"kite","action":"TREND_EXIT","qty":q,"avg":round(avg,2),"ltp":ltp,"limit":lim,"peak":s["high"],"trend":why,"resp":r})
                continue
            while ltp>=s["target"]:
                prev=s["target"]; s["target"]=round(prev+avg*STEP,2); floor=max(floor,round(prev-avg*STEP,2),avg)
                kacts.append({"sym":sym,"broker":"kite","action":"ROLL_TARGET","from":prev,"to":s["target"],"trend":why})
            if True:
                stop=tick(max(avg*(1-TRAIL),seeds.get(sym,0),s.get("stop",0),floor))
                if stop>=ltp: stop=tick(ltp*(0.995 if fut else 0.97))
                else: s["stop"]=stop
                lim=tick(stop*(0.99 if fut else 0.95))
                info={"sym":sym,"broker":"kite","qty":q,"avg":round(avg,2),"ltp":ltp,"high":s["high"],"target":s["target"],"trigger":stop,"limit":lim}
                if not sls:
                    kacts.append({**info,"action":"PLACE_SL","resp":bk.place(ex,sym,"SELL",q,lim,order_type="SL",trigger=stop,tag="trailsl",product=p["product"],dry=DRY)})
                    if not DRY: sc.clear("kite",sym)
                else:
                    o=sls[0]
                    if stop>o["trigger_price"]*1.001 or o["quantity"]!=q:
                        t2=max(stop,o["trigger_price"])
                        kacts.append({**info,"action":"MODIFY_SL","from":o["trigger_price"],"to":t2,"resp":bk.modify(o["order_id"],q,tick(t2*(0.99 if fut else 0.95)),t2,dry=DRY)})
                    else: kacts.append({**info,"action":"SL_OK","sl_trigger":o["trigger_price"]})
        for o in kord:
            if o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M") and isopt(o["trading_symbol"]) and o["trading_symbol"] not in klongs:
                kacts.append({"sym":o["trading_symbol"],"broker":"kite","action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":bk.cancel(o["order_id"],dry=DRY)})
except Exception as e: kacts.append({"action":"KITE_ERROR","why":f"{type(e).__name__}: {e}"})
if not DRY: json.dump(st,open(SF,"w"))
print(json.dumps({"dry_run":DRY,"now":now.strftime("%Y-%m-%d %H:%M IST"),"breaker":breaker,"actions":acts,"kite_actions":kacts},indent=1))
