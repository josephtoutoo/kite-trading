#!/usr/bin/env python3
"""Automatic NIFTY/BANKNIFTY/SENSEX option entries (user standing permission from 4 Nov 2026, 2 lots/trade).
Run every 5 min by a routine. Acts only 09:20-14:45 IST Mon-Fri (risk_manager handles exits till 15:25).
Signal on last CLOSED 5-min candle (ind.py): FRESH cross only.
  CE: close > EMA20 and Supertrend green, previous candle not both; MACD hist > 0; ADX >= 18
  PE: close < EMA20 and Supertrend red,   previous candle not both; MACD hist < 0; ADX >= 18
  (MACD + ADX>=18 filters are there to avoid chop.)
Option: nearest weekly expiry, ATM strike, qty 2*lot_size on an ordinary trend (trade_lock may halve it).
STRONG trend (ind.strong: ADX >= 25 AND still rising, price on the Supertrend side of EMA20): qty doubles
to 4 lots (t295u). Measured on 93 fresh 5m signals: ADX>=25 rising = +0.080% avg forward 60-min move, 62% win,
14% of signals; the rest -0.016%. trade_lock caps still apply.
All rules via trade_lock.check_order. LIMIT BUY at ask (else LTP*1.005), product I (fallback D),
tag auto_entry; cancel if unfilled after 2 min.
Kite (4 Nov): same signal also buys 2 lots NRML LIMIT on Kite when the Kite token is valid (one decision for
the 3/day count); invalid Kite token -> Kite leg skipped + logged, Upstox unaffected. On fill: trade_lock.record_entry, risk_state.json
(entry, target +20%, stop -30%), seen_entries.json. One open position per index max.
After a fill the SL is placed and immediately re-verified (sl_confirm.py: re-reads the order book, retries once on a
tick-snapped price, else logs SL_CONFIRM_FAILED to sl_confirm_failures.json for the 10-min option monitor to alert on).
--dry-run: no orders, no state writes, ignores time window."""
import json,os,sys,gzip,time,datetime,urllib.request,urllib.error,urllib.parse
D=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,D)
import ind,trade_lock
import market_calendar
import broker_kite as bk
import sl_confirm as sc
DRY="--dry-run" in sys.argv
# User rule (4 Nov 2026, refined): the expiry-week intraday-only rule applies to STOCK options only.
# This script trades index options only (NIFTY/BANKNIFTY/SENSEX), so they keep normal carry (NRML) -
# in_expiry_week always returns False here. expiry_squareoff.py enforces the rule for stock options.
def in_expiry_week(exp_date):
    return False
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
now=datetime.datetime.now(IST)
LOTS=2; ADX_MIN=18; STRONG_LOTS=2*LOTS  # t295u: a STRONG trend doubles the index-option size
IDX={"NIFTY":{"key":"NSE_INDEX|Nifty 50","exch":"NSE","seg":"NSE_FO"},
     "BANKNIFTY":{"key":"NSE_INDEX|Nifty Bank","exch":"NSE","seg":"NSE_FO"},
     "SENSEX":{"key":"BSE_INDEX|SENSEX","exch":"BSE","seg":"BSE_FO"}}
SF=os.path.join(D,"risk_state.json"); SEEN=os.path.join(D,"seen_entries.json")
out={"now":now.strftime("%Y-%m-%d %H:%M IST"),"dry_run":DRY,"rules":f"fresh 5m cross EMA20+Supertrend, MACD hist in direction, ADX>={ADX_MIN} (anti-chop)","indices":{}}
def done(): print(json.dumps(out,indent=1,default=str)); sys.exit(0)
if not DRY and not market_calendar.is_trading_day(now.date()):
    out["skipped"]="exchange holiday / market closed ("+now.date().isoformat()+")"; done()
if not DRY and (now.weekday()>=5 or not (datetime.time(9,20)<=now.time()<=datetime.time(14,45))):
    out["skipped"]="outside 09:20-14:45 IST weekday entry window"; done()
TOK=json.load(open(os.path.join(D,"token.json")))["access_token"]
H={"Authorization":"Bearer "+TOK,"Accept":"application/json","Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
op=urllib.request.build_opener(urllib.request.ProxyHandler({"https":os.environ.get("UPSTOX_PROXY_URL",""),"http":os.environ.get("UPSTOX_PROXY_URL","")}) if os.environ.get("UPSTOX_PROXY_URL") else urllib.request.ProxyHandler({}))
def req(u,body=None,method=None,proxy=False):
    r=urllib.request.Request(u,data=json.dumps(body).encode() if body is not None else None,headers=H,method=method)
    try: return json.load((op.open if proxy else urllib.request.urlopen)(r,timeout=20))
    except urllib.error.HTTPError as e: return {"status":"error","code":e.code,"body":e.read().decode()[:300]}
def tick(p,t=0.05): return round(max(round(p/t)*t,t),2)
def master(exch):
    f=os.path.join(D,f"instruments_{exch}_{now.date()}.json.gz")
    if not os.path.exists(f):
        for o in os.listdir(D):
            if o.startswith(f"instruments_{exch}_"): os.remove(os.path.join(D,o))
        urllib.request.urlretrieve(f"https://assets.upstox.com/market-quote/instruments/exchange/{exch}.json.gz",f)
    return json.load(gzip.open(f))
def signal(a):
    up=lambda c,e,d: c>e and d==1; dn=lambda c,e,d: c<e and d==-1
    cur_up=up(a["close"],a["ema20"],a["dir"]); prv_up=up(a["prev_close"],a["prev_ema"],a["prev_dir"])
    cur_dn=dn(a["close"],a["ema20"],a["dir"]); prv_dn=dn(a["prev_close"],a["prev_ema"],a["prev_dir"])
    side=None; why=[]
    if cur_up: side="CE"; why.append("fresh up-cross" if not prv_up else "up-trend but NOT fresh (prev candle already aligned)")
    elif cur_dn: side="PE"; why.append("fresh down-cross" if not prv_dn else "down-trend but NOT fresh (prev candle already aligned)")
    else: why.append("no alignment (close vs EMA20 / Supertrend disagree)")
    fresh=(side=="CE" and not prv_up) or (side=="PE" and not prv_dn)
    macd_ok=side and ((side=="CE" and a["hist"]>0) or (side=="PE" and a["hist"]<0))
    adx_ok=a["adx"]>=ADX_MIN
    if side and not macd_ok: why.append(f"MACD hist {a['hist']} against direction")
    if not adx_ok: why.append(f"ADX {a['adx']} < {ADX_MIN} (chop)")
    return (side if side and fresh and macd_ok and adx_ok else None), side, why
def pick(name,cfg,side,spot):
    ins=[x for x in master(cfg["exch"]) if x.get("segment")==cfg["seg"] and x.get("instrument_type")==side and x.get("underlying_symbol",x.get("name",""))==name]
    today_ms=int(datetime.datetime.combine(now.date(),datetime.time(0),IST).timestamp()*1000)
    exps=sorted({x["expiry"] for x in ins if x["expiry"]>=today_ms})
    exp=exps[0]; chain=[x for x in ins if x["expiry"]==exp]
    strikes=sorted({x["strike_price"] for x in chain})
    step=min(b-a for a,b in zip(strikes,strikes[1:]) if b>a) if len(strikes)>1 else 50
    std=50 if name=="NIFTY" else 100; step=max(step,std) if step<std else step
    atm=round(spot/std)*std
    best=min(chain,key=lambda x:(abs(x["strike_price"]-atm),x["strike_price"]))
    return best,datetime.datetime.fromtimestamp(exp/1000,IST).date(),std
def quote(k):
    d=req("https://api.upstox.com/v2/market-quote/quotes?instrument_key="+urllib.parse.quote(k,safe="")).get("data") or {}
    for v in d.values():
        ask=None
        try: ask=float(v["depth"]["sell"][0]["price"]) or None
        except Exception: pass
        return float(v.get("last_price") or 0),ask
    return 0,None
pos=req("https://api.upstox.com/v2/portfolio/short-term-positions").get("data") or []
for name,cfg in IDX.items():
    r=out["indices"][name]={}
    try:
        a=ind.analyze(cfg["key"],"minutes",5,5)
        r["candle"]=a["last_ts"][11:16]; r["5m"]={k:a[k] for k in("close","ema20","st","dir","prev_close","prev_ema","prev_dir","hist","adx")}
        sig,raw,why=signal(a); r["signal"]=sig or "NONE"; r["trend_side"]=raw; r["why"]=why
        best,exp,std=pick(name,cfg,raw or "CE",a["close"])
        strong,sside,sdet=ind.strong(a); r["strong"]={"on":strong,"side":sside,**sdet}
        lots=LOTS
        if strong and sig:
            if sside==raw: lots=STRONG_LOTS; r["size"]=f"STRONG trend -> {lots} lots (doubled)"
            else: r["size"]=f"strong {sside} but signal is {raw} - normal {lots} lots"
        elif sig: r["size"]=f"ordinary trend -> {lots} lots"
        qty=lots*int(best["lot_size"])
        r["option"]={"symbol":best["trading_symbol"],"key":best["instrument_key"],"expiry":str(exp),"strike":best["strike_price"],"lot_size":best["lot_size"],"qty":qty,"strike_step":std}
        openp=[p for p in pos if int(p.get("quantity") or 0)>0 and trade_lock.underlying(trade_lock.sym_of(p))==name and trade_lock.is_opt(trade_lock.sym_of(p))]
        if openp: r["open_position"]=[trade_lock.sym_of(p) for p in openp]
        if not sig: r["action"]="none: no fresh signal"; continue
        ltp,ask=quote(best["instrument_key"]); t=float(best.get("tick_size") or 0.05)
        if t>1: t=t/100  # some masters give paise
        px=tick(ask if ask else ltp*1.005,t); r["ltp"]=ltp; r["ask"]=ask; r["limit"]=px
        lot=int(best["lot_size"])
        ekw=in_expiry_week(exp)
        r["expiry_week"]=ekw
        r["carry"]="intraday only (expiry week)" if ekw else "positional"
        # ---- Kite leg plan (checked BEFORE any order so the pair is one decision; never blocks Upstox) ----
        kleg=None
        try:
            if not bk.valid(): r["kite"]={"action":"skipped: Kite token invalid ("+bk.why()[:80]+")"}; bk.log(f"auto_entry {name}: kite skipped, invalid token")
            else:
                m=bk.from_upstox(best["instrument_key"])
                kopen=[p for p in bk.positions() if p["quantity"]>0 and trade_lock.underlying(p["trading_symbol"])==name and trade_lock.is_opt(p["trading_symbol"])]
                if not m: r["kite"]={"action":"skipped: no Kite symbol for "+best["instrument_key"]}
                elif kopen: r["kite"]={"action":"skip: Kite position already open","open":[p["trading_symbol"] for p in kopen]}
                else:
                    kq=lots*m["lot_size"]
                    kprod="MIS" if ekw else "NRML"
                    ko={"trading_symbol":m["tradingsymbol"],"instrument_token":"KITE|%s|%s"%(m["exchange"],m["tradingsymbol"]),"transaction_type":"BUY","order_type":"LIMIT","product":kprod,"quantity":kq,"price":px}
                    kok,kre,kadj=trade_lock.check_order(ko,broker="kite"); kadj=(kadj//m["lot_size"])*m["lot_size"]
                    r["kite"]={"symbol":m["tradingsymbol"],"exchange":m["exchange"],"qty":kq,"check":{"allowed":kok,"reasons":kre,"adjusted_qty":kadj}}
                    if kok and kadj>0: kleg={**m,"qty":kadj}
                    else: r["kite"]["action"]="blocked by pre-trade rules"
        except Exception as e: r["kite"]={"action":f"skipped: error {type(e).__name__}: {e}"}; kleg=None
        # ---- Upstox leg ----
        uleg=None
        if openp: r["action"]="skip: position already open in "+name
        else:
            csym=f"{name}{exp:%y%m%d}{int(best['strike_price'])}{raw}"  # compact form trade_lock parses (master symbol has spaces)
            o={"trading_symbol":csym,"instrument_token":best["instrument_key"],"transaction_type":"BUY","order_type":"LIMIT","product":"I","quantity":qty,"price":px}
            ok,reasons,adj=trade_lock.check_order(o); adj=(adj//lot)*lot
            r["check"]={"allowed":ok,"reasons":reasons,"adjusted_qty":adj}
            if not ok or adj<=0: r["action"]="blocked by pre-trade rules"
            else: uleg=adj
        if DRY:
            if uleg: r["action"]=f"DRY-RUN: would BUY {uleg} {best['trading_symbol']} LIMIT {px}"
            if kleg: r["kite"]["action"]=f"DRY-RUN: would BUY {kleg['qty']} {kleg['tradingsymbol']} LIMIT {px} NRML on Kite"; bk.place(kleg["exchange"],kleg["tradingsymbol"],"BUY",kleg["qty"],px,dry=True)
            continue
        if not uleg and not kleg: continue
        oid=None; koid=None
        if uleg:
            body={"quantity":uleg,"product":"I","validity":"DAY","price":px,"tag":"auto_entry","instrument_token":best["instrument_key"],"order_type":"LIMIT","transaction_type":"BUY","disclosed_quantity":0,"trigger_price":0,"is_amo":False}
            pr=req("https://api-hft.upstox.com/v2/order/place",body,"POST",proxy=True)
            if pr.get("status")!="success" and "product" in json.dumps(pr).lower() and not ekw:
                body["product"]="D"; pr=req("https://api-hft.upstox.com/v2/order/place",body,"POST",proxy=True)
            oid=(pr.get("data") or {}).get("order_id")
            if not oid: r["action"]="order rejected"; r["resp"]={"code":pr.get("code"),"body":pr.get("body")}
            else: r["order_id"]=oid
        if kleg:
            koid=bk.place(kleg["exchange"],kleg["tradingsymbol"],"BUY",kleg["qty"],px,tag="auto_entry",product=kprod)
            r["kite"]["order_id"]=koid
            if not koid: r["kite"]["action"]="Kite order failed (see kite_broker.log)"
        st="open"; fill=0; avg=px; kst=None
        for _ in range(12):
            if not oid and not koid: break
            time.sleep(10)
            if oid and st not in("complete","rejected","cancelled"):
                d=req("https://api.upstox.com/v2/order/details?order_id="+oid).get("data") or {}
                st=d.get("status",""); fill=int(d.get("filled_quantity") or 0); avg=float(d.get("average_price") or px)
            if koid and (not kst or kst["status"] not in("complete","rejected","cancelled")): kst=bk.order_status(koid) or kst
            if (not oid or st in("complete","rejected","cancelled")) and (not koid or (kst and kst["status"] in("complete","rejected","cancelled"))): break
        if oid and st not in("complete","rejected","cancelled"):
            req("https://api-hft.upstox.com/v2/order/cancel?order_id="+oid,None,"DELETE",proxy=True); r["cancelled_unfilled"]=True
        if koid and not (kst and kst["status"] in("complete","rejected","cancelled")):
            bk.cancel(koid); r["kite"]["cancelled_unfilled"]=True
        kfill=(kst or {}).get("filled",0) if koid else 0
        if fill>0 or kfill>0: trade_lock.record_entry()  # one decision even if both legs fill
        s=json.load(open(SF)) if os.path.exists(SF) else {}; seen=json.load(open(SEEN)) if os.path.exists(SEEN) else {}
        if oid:
            r["status"]=st; r["filled"]=fill
            if fill>0:
                s[best["instrument_key"]]={"high":avg,"entry":avg,"target":round(avg*1.2,2),"sym":best["trading_symbol"],"source":"auto_entry","intraday":ekw}
                s.setdefault("_min_stop_by_symbol",{})[best["trading_symbol"]]=tick(avg*0.7)
                seen[oid]={"sym":best["trading_symbol"],"qty":fill,"avg":avg,"time":now.isoformat(),"source":"auto_entry"}
                r["action"]=f"ENTERED {fill} {best['trading_symbol']} @ {avg}; target {round(avg*1.2,2)} stop {tick(avg*0.7)}"
            else: r["action"]="not filled in 2 min - cancelled"
        if koid:
            ka=(kst or {}).get("avg") or px; r["kite"]["status"]=(kst or {}).get("status"); r["kite"]["filled"]=kfill
            if kfill>0:
                kk="KITE|%s|%s"%(kleg["exchange"],kleg["tradingsymbol"])
                s[kk]={"high":ka,"entry":ka,"target":round(ka*1.2,2),"sym":kleg["tradingsymbol"],"source":"auto_entry","broker":"kite"}
                s.setdefault("_min_stop_by_symbol",{})[kleg["tradingsymbol"]]=tick(ka*0.7)
                seen["kite:"+str(koid)]={"sym":kleg["tradingsymbol"],"qty":kfill,"avg":ka,"time":now.isoformat(),"source":"auto_entry","broker":"kite"}
                r["kite"]["action"]=f"ENTERED {kfill} {kleg['tradingsymbol']} @ {ka} on Kite; target {round(ka*1.2,2)} stop {tick(ka*0.7)}"
            else: r["kite"]["action"]="Kite not filled in 2 min - cancelled"
        json.dump(s,open(SF,"w"),indent=1); json.dump(seen,open(SEEN,"w"),indent=1)
        # ---- post-placement confirmation (4 Nov): SL must be LIVE before this counts as an entered trade ----
        if fill>0:
            try:
                c=sc.guard_upstox(best["trading_symbol"],best["instrument_key"],fill,avg,product=body.get("product","I"),
                                  target=s[best["instrument_key"]]["target"],tick=t,entry_order=oid)
                r["sl_check"]={k:c.get(k) for k in ("ok","sl","sl_trigger","sl_status","target_live","attempts","reasons")}
                if not c["ok"]: r["action"]+=" | SL_CONFIRM_FAILED (see sl_confirm_failures.json)"
            except Exception as e: r["sl_check"]={"ok":False,"error":f"{type(e).__name__}: {e}"}
        if kfill>0:
            try:
                c=sc.guard_kite(kleg["exchange"],kleg["tradingsymbol"],kfill,ka,product=kprod,
                                target=s[kk]["target"],tick=bk.tick_for(kleg["exchange"],kleg["tradingsymbol"]),entry_order=koid)
                r["kite"]["sl_check"]={k:c.get(k) for k in ("ok","sl","sl_trigger","sl_status","target_live","attempts","reasons")}
                if not c["ok"]: r["kite"]["action"]+=" | SL_CONFIRM_FAILED (see sl_confirm_failures.json)"
            except Exception as e: r["kite"]["sl_check"]={"ok":False,"error":f"{type(e).__name__}: {e}"}
    except Exception as e: r["error"]=f"{type(e).__name__}: {e}"
done()
