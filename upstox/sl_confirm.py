#!/usr/bin/env python3
"""Post-placement confirmation for option entries (4 Nov 2026).
An entry is not "in" until its stop is LIVE at the broker. After a new entry is placed we re-read the
broker's order book (2-3 s wait, one more try) and verify:
  (a) the entry filled or is open,
  (b) a SELL SL is live (open / trigger pending) for the full quantity with the expected trigger,
  (c) the target level exists (risk_state.json 'target' by default; a live order only when target_mode="order").
NOTE on targets: the desk keeps NO resting target order on purpose - risk_manager rolls the target forward
(+20% steps while the trend holds) and books it with a LIMIT 'target_book' exit when the trend breaks.
A resting LIMIT at entry*1.2 would fill on the first target and cap the roll-forward, so the default check
is the recorded level, not a resting order.
If the SL is missing or was rejected, the caller retries ONCE with prices snapped to the contract tick
(broker_kite.tick_for()/snap(); Kite error 16283 = "price is not a multiple of tick size").
If it still fails: log 'SL_CONFIRM_FAILED broker=... symbol=... order=... reason=...' and append it to
sl_confirm_failures.json. /workspace/kite/option_monitor.py (10-min routine) prints those as an alert and
risk_manager.py clears them as soon as it sees a live SL.
This module places orders ONLY through guard_*() (entry path). confirm() is read-only.
"""
import json,os,time,datetime,urllib.request,urllib.error
D=os.path.dirname(os.path.abspath(__file__))
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
FAILF=os.path.join(D,"sl_confirm_failures.json")
STATE=os.path.join(D,"risk_state.json")
TICK=0.05
LIVE=("open","trigger pending","open pending","modified")
def log(msg):
    try:
        with open(os.path.join(D,"sl_confirm.log"),"a") as f: f.write(f"{datetime.datetime.now(IST):%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception: pass
# ---------------- failure state file ----------------
def failures(include_resolved=False):
    try:
        d=json.load(open(FAILF))
        rows=d.get("failures") or []
    except Exception: return []
    return rows if include_resolved else [r for r in rows if not r.get("resolved")]
def _save(rows):
    try:
        json.dump({"updated":datetime.datetime.now(IST).isoformat(timespec="seconds"),"failures":rows[-50:]},open(FAILF,"w"),indent=1)
    except Exception as e: log(f"fail-write {e}")
def record_failure(broker,symbol,order,reason,detail=None,qty=None,trigger=None,target=None):
    """Log + persist a stop/target that did not reach a live state. One open record per broker+symbol."""
    ts=datetime.datetime.now(IST).isoformat(timespec="seconds")
    log(f"SL_CONFIRM_FAILED broker={broker} symbol={symbol} order={order} reason={reason}")
    rows=json.load(open(FAILF)).get("failures") if os.path.exists(FAILF) else []
    rows=rows or []
    rec={"ts":ts,"broker":broker,"symbol":symbol,"order":order,"reason":reason,"qty":qty,
         "trigger":trigger,"target":target,"detail":(detail or "")[:300],"resolved":False}
    rows=[r for r in rows if not (r.get("broker")==broker and r.get("symbol")==symbol and not r.get("resolved"))]+[rec]
    _save(rows); return rec
def clear(broker,symbol):
    """Mark records resolved (called once a live SL is seen, e.g. by risk_manager)."""
    if not os.path.exists(FAILF): return 0
    try: rows=json.load(open(FAILF)).get("failures") or []
    except Exception: return 0
    n=0
    for r in rows:
        if r.get("broker")==broker and r.get("symbol")==symbol and not r.get("resolved"):
            r["resolved"]=datetime.datetime.now(IST).isoformat(timespec="seconds"); n+=1
    if n: _save(rows); log(f"cleared broker={broker} symbol={symbol} ({n})")
    return n
# ---------------- prices ----------------
def snap(broker,exchange,symbol,price,default=TICK):
    """Round a price onto the contract tick grid (Kite 16283 guard)."""
    if price is None: return None
    if broker=="kite":
        try:
            import broker_kite as bk
            return bk.snap(price,bk.tick_for(exchange or "NFO",symbol,default))
        except Exception: pass
    t=float(default or TICK) or TICK
    return round(round(float(price)/t)*t,4)
# ---------------- order books ----------------
def _req(u,body=None,method=None):
    h={"Authorization":"Bearer "+json.load(open(os.path.join(D,"token.json")))["access_token"],
       "Accept":"application/json","Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
    r=urllib.request.Request(u,data=json.dumps(body).encode() if body is not None else None,headers=h,method=method)
    px=os.environ.get("UPSTOX_PROXY_URL")
    op=urllib.request.build_opener(urllib.request.ProxyHandler({"https":px,"http":px}))
    try: return json.load(op.open(r,timeout=20))
    except urllib.error.HTTPError as e: return {"status":"error","code":e.code,"body":e.read().decode()[:300]}
    except Exception as e: return {"status":"error","code":0,"body":f"{type(e).__name__}: {str(e)[:200]}"}
def get_orders(broker):
    """Normalized order list: order_id, trading_symbol, transaction_type, order_type, status (lower),
    quantity, filled_quantity, price, trigger_price, status_message, tag."""
    if broker=="kite":
        try:
            import broker_kite as bk
            return bk.orders() if bk.valid() else []
        except Exception as e: log(f"kite orders {e}"); return []
    d=(_req("https://api.upstox.com/v2/order/retrieve-all") or {}).get("data") or []
    out=[]
    for o in d:
        out.append({"order_id":o.get("order_id"),"trading_symbol":o.get("trading_symbol"),
            "transaction_type":o.get("transaction_type"),"order_type":o.get("order_type"),
            "status":(o.get("status") or "").lower(),"quantity":int(o.get("quantity") or 0),
            "filled_quantity":int(o.get("filled_quantity") or 0),"price":float(o.get("price") or 0),
            "trigger_price":float(o.get("trigger_price") or 0),"status_message":o.get("status_message") or "",
            "tag":o.get("tag")})
    return out
def position_qty(broker,symbol):
    try:
        if broker=="kite":
            import broker_kite as bk
            return sum(p["quantity"] for p in bk.positions() if p["trading_symbol"]==symbol)
        d=(_req("https://api.upstox.com/v2/portfolio/short-term-positions") or {}).get("data") or []
        return sum(int(p.get("quantity") or 0) for p in d if (p.get("trading_symbol") or "")==symbol)
    except Exception: return 0
def state_target(broker,exchange,symbol):
    """Target level recorded in risk_state.json for this contract (Kite keys are KITE|EX|SYM)."""
    try: s=json.load(open(STATE))
    except Exception: return None
    keys=[f"KITE|{exchange}|{symbol}"] if broker=="kite" else []
    keys+= [k for k,v in s.items() if isinstance(v,dict) and v.get("sym")==symbol]
    for k in keys:
        v=s.get(k) or {}
        if v.get("target") is not None: return {"key":k,"target":float(v["target"])}
    return None
# ---------------- confirmation ----------------
def confirm(broker,symbol,qty,trigger=None,target=None,exchange=None,entry_order=None,tick=None,
            target_mode="state",target_order=None,attempts=2,wait=2.5):
    """Re-read the order book and verify entry + SL (+ target level). Read-only.
    -> dict(ok, entry, sl, sl_trigger, sl_status, target, target_live, reasons[])"""
    tol=max(float(tick or TICK),0.01)+1e-6
    res={"ok":False,"broker":broker,"symbol":symbol,"qty":qty,"entry":None,"sl":None,"sl_trigger":None,
         "sl_status":None,"target":target,"target_live":None,"attempts":0,"reasons":[]}
    for i in range(max(1,attempts)):
        if wait: time.sleep(wait)
        res["attempts"]=i+1
        o=get_orders(broker); res["reasons"]=[]
        # (a) entry filled or open
        if entry_order:
            e=next((x for x in o if x["order_id"]==entry_order),None)
            if e:
                res["entry"]=e["status"]
                if e["status"] in("rejected","cancelled"): res["reasons"].append(f"entry {e['status']}: {e['status_message'] or 'no message'}")
        pq=position_qty(broker,symbol)
        if pq<=0: res["reasons"].append("entry not filled (no position)")
        elif qty and pq<qty: res["reasons"].append(f"entry partially filled ({pq} of {qty})")
        # (b) live SL for the full quantity at the right trigger
        sls=[x for x in o if x["trading_symbol"]==symbol and x["transaction_type"]=="SELL"
             and x["order_type"] in("SL","SL-M") and x["status"] in LIVE]
        cov=[x for x in sls if not qty or x["quantity"]>=qty] or sls
        # a stop ABOVE the expected trigger is a ratcheted (tighter) stop and is fine; only a LOWER one is a problem
        good=[x for x in cov if trigger is None or x["trigger_price"]>=float(trigger)-tol]
        if good:
            x=good[0]; res["sl"]=x["order_id"]; res["sl_trigger"]=x["trigger_price"]; res["sl_status"]=x["status"]
        elif cov:
            res["sl"]=cov[0]["order_id"]; res["sl_trigger"]=cov[0]["trigger_price"]; res["sl_status"]=cov[0]["status"]
            res["reasons"].append(f"SL trigger {cov[0]['trigger_price']} < expected {trigger} (stop too loose)")
        else:
            rej=[x for x in o if x["trading_symbol"]==symbol and x["transaction_type"]=="SELL"
                 and x["order_type"] in("SL","SL-M") and x["status"]=="rejected"]
            why=(rej[-1].get("status_message") or "no message") if rej else "no SL order in book"
            res["reasons"].append("SL not live: "+str(why))
            res["sl_rejected"]=rej[-1]["order_id"] if rej else None
        # (c) target level / order
        if target is None: res["target_live"]=None
        elif target_mode=="order":
            t=[x for x in o if x["trading_symbol"]==symbol and x["transaction_type"]=="SELL"
               and x["order_type"]=="LIMIT" and x["status"] in LIVE
               and (target_order is None or x["order_id"]==target_order)]
            tt=[x for x in t if abs(x["price"]-float(target))<=tol]
            res["target_live"]=bool(tt); res["target_order"]=tt[0]["order_id"] if tt else None
            if not tt: res["reasons"].append(f"target order not live at {target}")
        else:
            r=state_target(broker,exchange,symbol)
            res["target_live"]=bool(r and abs(r["target"]-float(target))<=max(0.02,tol))
            res["target_state"]=r
            if not r: res["reasons"].append("target level not in risk_state.json")
            elif abs(r["target"]-float(target))>max(0.02,tol): res["reasons"].append(f"target level {r['target']} != {target}")
        res["ok"]=not res["reasons"]
        if res["ok"]: break
    return res
# ---------------- placement (entry path only) ----------------
def live_sl(broker,symbol,qty=None,trigger=None,tick=None):
    tol=max(float(tick or TICK),0.01)+1e-6
    for x in get_orders(broker):
        if x["trading_symbol"]==symbol and x["transaction_type"]=="SELL" and x["order_type"] in("SL","SL-M") \
           and x["status"] in LIVE and (not qty or x["quantity"]>=qty) \
           and (trigger is None or x["trigger_price"]>=float(trigger)-tol): return x
    return None
def place_sl_upstox(instrument_key,symbol,qty,avg,product="I",stop=None,tick=None,dry=False):
    """SELL SL (SL-LIMIT) 30% below entry unless a stop is given. Prices tick-snapped. -> (order_id, stop, why)"""
    if stop is None: stop=snap("upstox",None,symbol,float(avg)*0.7,tick)
    stop=snap("upstox",None,symbol,stop,tick); lim=snap("upstox",None,symbol,stop*0.95,tick)
    try:
        import trade_lock
        ok,reasons,adj=trade_lock.check_order({"trading_symbol":symbol,"instrument_token":instrument_key,
            "transaction_type":"SELL","order_type":"SL","product":product,"quantity":qty})
    except Exception as e: ok,reasons,adj=False,[f"trade_lock error {e}"],0
    if not ok or adj<=0: return None,stop,"blocked by trade_lock: "+"; ".join(map(str,reasons))
    if dry: return "DRY",stop,"dry-run"
    r=_req("https://api-hft.upstox.com/v2/order/place",{"quantity":min(qty,adj),"product":product,"validity":"DAY",
        "price":lim,"tag":"trailsl","instrument_token":instrument_key,"order_type":"SL","transaction_type":"SELL",
        "disclosed_quantity":0,"trigger_price":stop,"is_amo":False},"POST")
    oid=(r.get("data") or {}).get("order_id")
    log(f"placed SL {oid} {symbol} trig {stop} lim {lim} qty {min(qty,adj)}")
    return oid,stop,(None if oid else f"{r.get('code')}: {str(r.get('body'))[:160]}")
def place_sl_kite(exchange,symbol,qty,avg,product="NRML",stop=None,tick=None,dry=False):
    if stop is None: stop=snap("kite",exchange,symbol,float(avg)*0.7,tick)
    stop=snap("kite",exchange,symbol,stop,tick); lim=snap("kite",exchange,symbol,stop*0.95,tick)
    try:
        import broker_kite as bk
        oid=bk.place(exchange,symbol,"SELL",int(qty),lim,order_type="SL",trigger=stop,tag="trailsl",product=product,dry=dry)
    except Exception as e: return None,stop,f"{type(e).__name__}: {e}"
    return oid,stop,(None if oid else "broker_kite.place returned no order id (see kite_broker.log)")
def guard_upstox(symbol,instrument_key,qty,avg,product="I",target=None,tick=None,entry_order=None,dry=False):
    """Place SL if missing, confirm entry+SL+target, retry once with snapped prices, else record failure."""
    stop=snap("upstox",None,symbol,float(avg)*0.7,tick)
    oid=None; why=""
    if not live_sl("upstox",symbol,qty,stop,tick):
        oid,stop,why=place_sl_upstox(instrument_key,symbol,qty,avg,product,stop,tick,dry)
    r=confirm("upstox",symbol,qty,trigger=stop,target=target,entry_order=entry_order,tick=tick,attempts=2)
    if not r["sl"] and not dry:
        stop=snap("upstox",None,symbol,float(avg)*0.7,tick)
        oid,stop,why=place_sl_upstox(instrument_key,symbol,qty,avg,product,stop,tick,dry)
        r=confirm("upstox",symbol,qty,trigger=stop,target=target,entry_order=entry_order,tick=tick,attempts=2)
    if r["ok"]: clear("upstox",symbol)
    elif not dry:
        record_failure("upstox",symbol,oid or entry_order,"; ".join(r["reasons"]),why,qty=qty,trigger=stop,target=target)
    return r
def guard_kite(exchange,symbol,qty,avg,product="NRML",target=None,tick=None,entry_order=None,dry=False):
    stop=snap("kite",exchange,symbol,float(avg)*0.7,tick)
    oid=None; why=""
    if not live_sl("kite",symbol,qty,stop,tick):
        oid,stop,why=place_sl_kite(exchange,symbol,qty,avg,product,stop,tick,dry)
    r=confirm("kite",symbol,qty,trigger=stop,target=target,exchange=exchange,entry_order=entry_order,tick=tick,attempts=2)
    if not r["sl"] and not dry:
        stop=snap("kite",exchange,symbol,float(avg)*0.7,tick)
        oid,stop,why=place_sl_kite(exchange,symbol,qty,avg,product,stop,tick,dry)
        r=confirm("kite",symbol,qty,trigger=stop,target=target,exchange=exchange,entry_order=entry_order,tick=tick,attempts=2)
    if r["ok"]: clear("kite",symbol)
    elif not dry:
        record_failure("kite",symbol,oid or entry_order,"; ".join(r["reasons"]),why,qty=qty,trigger=stop,target=target)
    return r
if __name__=="__main__":
    print(json.dumps({"failures":failures(),"all":failures(True)},indent=1))
