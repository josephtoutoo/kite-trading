#!/usr/bin/env python3
"""Upstox option risk manager (v2, 3 Nov). Manages ALL open long option positions (quantity>0),
including carry-forward (product D) ones. Keeps a SELL SL at a fixed % under entry (never lowered, floored at
seeded min_stop): 20% for positions opened from 16 Nov 2026 (t669u), 30% kept for positions already open then. Target roll-forward (3 Nov): at LTP>=target (start entry*1.20)
checks underlying trend (index opts 15m since 16 Nov 2026 14:37 IST, stock opts/futures 15m; CE/FUT need close>EMA20 & ST green, PE need
close<EMA20 & ST red). Intact -> target += entry*0.20, stop floor = target two steps back (1st roll: entry). Broken -> LIMIT sell
at LTP*0.995 for live qty. Long futures: 1.5% trail from high, target step +2%.
Only acts 09:15-15:25 IST Mon-Fri. Cancels leftover SELL SL orders on contracts with no long position.
MTF: not managed here any more (5 Nov) - equity/upstox_mtf_stops.py owns MTF stops (plain protective only, no trail).
Trend-stop policy (trend_stop_policy.py) is off. TREND_STOP_POLICY=1 only adds a log note.
It does not change the trigger, the quantity, or cancel the disaster stop.
Kite (4 Nov): also manages Kite long options/futures (Upstox LTP+trend, Kite SL/LIMIT orders); no-op if Kite token invalid.
Full auto (12 Nov 2026, user-authorized): full_auto_policy.flag("trend_exit") (env FULL_AUTO_TREND_EXIT=1 or
full_auto_flags.json) replaces the 15m index-option trend exit with a COMPLETED 1h Supertrend-flip exit for every
long option (index, stock, MCX), and lifts option stops on an upward-only ladder (breakeven at +15%, then the
target two steps back). Every exit SELL re-reads the broker quantity first and is capped to it.
16 Nov 2026 (t669u): OVERSIZE lines for option positions above the lot rule (index 1/2 lots, 3 on that index's
expiry day, 3 total with the one add; stock options 1 lot per broker), once per position per day, in "oversize".
Sent TREND_EXIT / STOP_BREACH_EXIT / TWO_CANDLE_EXIT / EMA50_EXIT orders are recorded in reentry_blocks.json (same-contract re-entry block).
16 Nov 2026 (t685u): index-option routes (tracked in index_route_positions.json, or inferred:
intraday->3m, monthly->1h, else 15m; existing BANKNIFTY calls read as 1h) also exit when two
consecutive completed route-timeframe FUTURES candles after entry each close against the previous
close (CE below, PE above). Checked on each 5-minute run; 3m signals use all completed post-entry
3m futures candles, and the sell is sent at that run.
16 Nov 2026 (t691u): stock-option debit spreads are closed as a pair (buy
the short back first, then sell the long) when the net-premium stop / ladder
fires, or when a long-leg exit applies. A lone leftover short is bought back
at the start of each run (9:15 reconcile). A lone SELL SL is never left on a
spread long.
--dry-run: read-only, prints intended actions, ignores time window, writes no state."""
import json,os,sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import json,urllib.request,urllib.error,datetime
import market_calendar
import api                     # pooled, proxied, timed client (5 Nov 2026)
try:
    import perf_rules as prules   # 16 Nov 2026 (t669u): 20% initial stop, OVERSIZE alerts, re-entry records
except Exception as _pe:
    prules=None
    print(f"risk_manager: perf_rules unavailable, 30% stop and no OVERSIZE check: {type(_pe).__name__}",file=sys.stderr)
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
def tick_size_for(key, default=0.05):
    """Contract tick from the daily Kite master (11 Nov 2026: NIFTY FUT 0.10, BANKNIFTY FUT 0.20,
    CRUDEOIL options 0.10). Upstox rejected an off-grid futures trigger ("invalid trigger price")
    and the stop stayed unplaced. Falls back to the old 0.05 grid when the contract is not found."""
    try:
        import broker_kite as _bk
        m = _bk.from_upstox(key)
        t = float((m or {}).get("tick_size") or 0)
        return t if t >= 0.05 else default
    except Exception:
        return default
def tick_on(p, ts):
    """Round p onto the contract grid ts. ts=0.05 (or unknown) gives exactly tick(p)."""
    if not ts or abs(ts - 0.05) < 1e-9: return tick(p)
    return round(round(p/ts)*ts, 2)
def isopt(sym): return len(sym)>2 and sym[-2:] in("CE","PE") and sym[-3].isdigit()  # BAJFINANCE ends in CE: require strike digit

# 16 Nov 2026 14:37 IST (t664u): index-option TREND_EXIT uses last CLOSED 15-minute bar
# (same EMA20 / Supertrend rule as before). Stock options stay on 15m. The 1h flag stays off.
INDEX_TREND_EXIT_MIN = 15
INDEX_TREND_EXIT_DAYS = 10
import position_life as pl
def _completed_fills(orders, sym=None, token=None):
    return pl.completed_fills(orders, sym=sym, token=token)
def current_open_gen(orders, sym=None, token=None, overnight=0):
    return pl.current_open_gen(orders, sym=sym, token=token, overnight=overnight)
def bind_position_peak(s, avg, ltp, qty, *, day_sell=0, overnight=0, orders=None, sym=None, token=None, step=0.20):
    """Ratchet the +15% peak from this position's own fill, not the contract's earlier high.

    Reset when the book went flat and was re-entered (observed live_qty==0, a new opening-fill id,
    or today's tape shows a flatten before the current opening). Adds keep the existing high.
    On reset the stop floor (s['stop'] and seeds[sym]) is cleared so the new entry gets a fresh
    initial stop, not the prior life's. Returns (reset, why, prev_high).
    """
    return pl.bind_position_peak(
        s, avg, ltp, qty, day_sell=day_sell, overnight=overnight, orders=orders,
        sym=sym, token=token, step=step, now=now,
        initial_stop_pct=(prules.INITIAL_STOP_PCT if prules is not None else None),
        seeds=seeds,
    )

def _gtt_clear(broker, sym, key=None):
    """Cancel this name's overnight GTT before a day order is placed on it.

    A GTT and a day SELL on the same position would both fire and sell the quantity twice.
    Returns (ok, detail). On False the caller must NOT place the day order: the GTT stays as cover.
    """
    if DRY:
        return True, "dry-run (GTT left alone)"
    try:
        sys.path.insert(0,"/workspace/equity")
        import gtt_overnight as _g
        return _g.cancel_for_day(broker, sym, key=key)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

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
    _mr0=req("https://api.upstox.com/v3/portfolio/mtf-positions")
    _mtfp=(_mr0.get("data") or [])
    if _mr0.get("status")!="success": _mtfp=[]
except Exception:
    _mtfp=[]
orders=req("https://api.upstox.com/v2/order/retrieve-all").get("data") or []
def _old_pnl(p): return float(p.get("pnl") if p.get("pnl") is not None else (p.get("unrealised") or 0)+(p.get("realised") or 0))
# 16 Nov 2026 (t669u): option/future rows valued for TODAY (carry from the previous close, FIFO from today's fills)
try:
    import daily_loss_cap as _dlc
    _uf=_dlc.fills_from_orders(orders,"upstox")
    day_pnl=sum(_dlc.row_pnl(p,"upstox",_dlc._fills_for(p,"upstox",_uf))["day_pnl"] if (trade_lock.is_opt(trade_lock.sym_of(p)) or trade_lock.is_fut(trade_lock.sym_of(p))) else _old_pnl(p) for p in pos)+sum(_old_pnl(p) for p in _mtfp)
except Exception as _de:
    print(f"risk_manager: today-only P&L failed, broker figure used: {type(_de).__name__}",file=sys.stderr)
    day_pnl=sum(_old_pnl(p) for p in pos+_mtfp)
breaker={"day_pnl":round(day_pnl,2),"threshold":-trade_lock.THRESHOLD,"locked":False}
if day_pnl< -trade_lock.THRESHOLD:
    breaker["locked"]=True; breaker["lock"]=trade_lock.lock(-day_pnl,f"day loss Rs {-day_pnl:,.0f} > Rs {trade_lock.THRESHOLD:,}",dry=DRY)
breaker["can_enter"]=trade_lock.can_enter(1)
def pending_sl(k): return [o for o in orders if o["instrument_token"]==k and o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M")]
acts=[]; longs=set(); held_keys=set(); kite_book_ok=False
# 16 Nov 2026 (t669u): one-time marker - positions already open keep their 30% floor; new ones get 20%.
if prules is not None:
    try:
        _legacy=prules.migrate_stop_pct(st,[o["instrument_token"] for o in orders if o.get("transaction_type")=="SELL" and o.get("status") in("trigger pending","open") and o.get("order_type") in("SL","SL-M")],now)
        if _legacy: acts.append({"action":"STOP_PCT_LEGACY","why":"open before the 20% rule; floor stays 30%","keys":_legacy})
    except Exception as _me:
        print(f"risk_manager: stop_pct migration skipped: {type(_me).__name__}",file=sys.stderr)
OVERSIZE_ITEMS=[]
def _buys_today(book,sym=None,token=None,overnight=0):
    n=1 if int(overnight or 0)>0 else 0
    for o in book or []:
        osym=o.get("trading_symbol") or o.get("tradingsymbol") or ""
        if not ((sym and osym==sym) or (token and str(o.get("instrument_token") or "")==str(token))): continue
        if str(o.get("transaction_type") or "").upper()!="BUY": continue
        try:
            if int(float(o.get("filled_quantity") or 0))>0: n+=1
        except (TypeError,ValueError): pass
    return n
def _oversize(broker,sym,q,lot,overnight,book,token=None):
    """Queue an OVERSIZE item when this option position is above the lot rule (never sends anything)."""
    if prules is None or not lot or q<=0: return
    try:
        u=trade_lock.underlying(sym)
        if u=="CRUDEOIL" or str(token or "").startswith(("MCX","KITE|MCX")): return
        kind="index" if u in INDEX_KEYS else "stock"
        xd=bool(kind=="index" and trade_lock._expiry_today(u,now) is True)
        lots=q/float(lot)
        hit=prules.oversize(kind,lots,expiry_day=xd,buys_today=_buys_today(book,sym,token,overnight))
        if hit: OVERSIZE_ITEMS.append({"broker":broker,"sym":sym,"qty":int(q),"lots":round(lots,2),"allowed":hit[0],"rule":hit[1]})
    except Exception as e:
        print(f"risk_manager: oversize check failed for {sym}: {type(e).__name__}",file=sys.stderr)
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
    """Underlying trend on last CLOSED candle. Index opts: 15m; stock opts/futures: 15m.
    CE / long FUT: close>EMA20 & ST green. PE: close<EMA20 & ST red."""
    u=trade_lock.underlying(sym)
    if u in INDEX_KEYS: key,iv,days=INDEX_KEYS[u],INDEX_TREND_EXIT_MIN,INDEX_TREND_EXIT_DAYS
    else: key,iv,days=eq_keys().get(u),15,10
    if not key: return False,f"no underlying key for {u}"
    a=ind.analyze(key,"minutes",iv,days)
    up=a["close"]>a["ema20"] and a["dir"]==1; dn=a["close"]<a["ema20"] and a["dir"]==-1
    ok=dn if sym.endswith("PE") else up
    return ok,f"{u} {iv}m@{a['last_ts'][11:16]} close {a['close']} ema20 {a['ema20']} ST {'green' if a['dir']==1 else 'red'}"
def _note_trend_policy(info, sym, fut, avg, ltp, stop, q):
    """Annotation only. The stop already computed above is what gets sent."""
    if os.environ.get("TREND_STOP_POLICY", "") != "1":
        return
    try:
        import trend_stop_policy as tsp
        info["trend_policy"] = tsp.review(sym, fut=fut, entry=avg, ltp=ltp, existing_stop=stop, qty=q)
        info["trend_policy"]["orders_changed"] = False
        info["trend_policy"]["cancel_disaster_stop"] = False
    except Exception as e:
        info["trend_policy"] = {"orders_changed": False, "cancel_disaster_stop": False, "error": type(e).__name__}
def isfut(sym): return sym.endswith("FUT")
# ---- full auto (12 Nov 2026) ----
try:
    import full_auto_policy as fap
    FA_TREND=fap.flag("trend_exit")
    FA_LADDER=fap.flag("stop_ladder")
except Exception as _fe:
    fap=None; FA_TREND=False; FA_LADDER=False
    print(f"risk_manager: full_auto_policy unavailable, legacy exits only: {type(_fe).__name__}: {_fe}",file=sys.stderr)
_H1={}
def h1_bar(sym):
    """Completed 1h bar of the option's underlying (cached per underlying for this run)."""
    try:
        import full_auto_feed as faf
        u=faf.root(sym)
        if u not in _H1: _H1[u]=faf.underlying_1h(sym)
        return _H1[u]
    except Exception as e:
        return None,f"{type(e).__name__}: {e}"
def opt_side(sym): return "PE" if sym.endswith("PE") else ("CE" if sym.endswith("CE") else None)
def upstox_live_qty(k):
    """Fresh broker read of the open quantity for one instrument. None when the read fails."""
    r=req("https://api.upstox.com/v2/portfolio/short-term-positions")
    if r.get("status")!="success": return None
    return sum(int(x.get("quantity") or 0) for x in (r.get("data") or []) if x.get("instrument_token")==k)
def kite_live_qty(bk,sym):
    try:
        bk.forget(); return sum(int(x["quantity"]) for x in bk.positions(strict=True) if x["trading_symbol"]==sym)
    except Exception: return None
FA_NOTE={"trend_exit_1h":FA_TREND,"stop_ladder":FA_LADDER}
# t685u/t686u: two consecutive route-timeframe futures closes vs previous close, or a close through EMA50 (index option routes only); whichever completes first
try:
    import index_two_candle_exit as i2c
    _I2C=i2c.prepare(now=now)
except Exception as _i2e:
    i2c=None; _I2C=None
    print(f"risk_manager: index_two_candle_exit unavailable, two-candle/EMA50 exit off: {type(_i2e).__name__}",file=sys.stderr)
def _two_candle_check(sym,product,broker,instrument_key=None,exchange=None,orders=None):
    if i2c is None: return False,"route-timeframe exit module unavailable",None
    try:
        r=i2c.evaluate(sym,product=product,broker=broker,instrument_key=instrument_key,exchange=exchange,state=_I2C,now=now,orders=orders,sync_route=True)
        if isinstance(r,tuple) and len(r)>=3:
            return r[0],r[1],r[2]
        return (r[0],r[1],("TWO_CANDLE_EXIT" if r[0] else None))
    except Exception as e:
        return False,"route-timeframe exit check failed: %s"%type(e).__name__,None

def _stockopt_spread_ctx(pos_list, broker):
    """Ledger + inferred debit-spread pairs. Fail-closed: if the module cannot
    load, stock-option spread legs still do not get a lone SELL SL."""
    try:
        eq=os.path.join(D,"equity")
        if eq not in sys.path: sys.path.insert(0,eq)
        import stockopt_spread as _ssp
        legs=_ssp.legs_from_positions(pos_list, broker)
        return {"pairs": _ssp.infer_pairs(legs), "ledger": _ssp.load_ledger(), "mod": _ssp, "legs": legs}
    except Exception:
        return None

def _stockopt_spread_skip(sym, qty, ctx):
    if not ctx or not ctx.get("mod"): return None
    try:
        return ctx["mod"].gtt_skip_reason({"sym": sym, "qty": qty}, ledger=ctx.get("ledger"), pairs=ctx.get("pairs"))
    except Exception:
        return None

def _spread_find_raw(pos_list, sym, broker="upstox"):
    want=(sym or "").replace(" ","").replace("-","").upper()
    for r in pos_list or []:
        s=(r.get("trading_symbol") or r.get("tradingsymbol") or "")
        if s.replace(" ","").replace("-","").upper()==want:
            return r
    return None

def _spread_limit(ltp, side, ts=0.05):
    if not ltp: return None
    px=ltp*(1.005 if side=="BUY" else 0.995)
    return tick_on(px, ts or 0.05)

def _spread_cover_upstox(plan, pos_list):
    raw=_spread_find_raw(pos_list, plan.get("short_sym"))
    if not raw:
        return {"sym":plan.get("short_sym"),"action":"COVER_NAKED_SHORT_SKIP","why":"short not in the book"}
    qty=abs(int(raw.get("quantity") or plan.get("qty") or 0))
    ltp=float(raw.get("last_price") or 0)
    px=_spread_limit(ltp,"BUY",tick_size_for(raw.get("instrument_token")))
    if not qty or not px:
        return {"sym":raw.get("trading_symbol"),"action":"COVER_NAKED_SHORT_SKIP","why":"qty or price unverified"}
    r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":qty,"product":raw.get("product") or "D","validity":"DAY","price":px,"tag":"spread_cover","instrument_token":raw["instrument_token"],"order_type":"LIMIT","transaction_type":"BUY","disclosed_quantity":0,"trigger_price":0,"is_amo":False})
    oid=(r.get("data") or {}).get("order_id") if not DRY else "DRY"
    if DRY: r={"status":"dry-run"}
    return {"sym":raw.get("trading_symbol"),"action":"COVER_NAKED_SHORT" if (oid or DRY) else "COVER_NAKED_SHORT_FAIL","qty":qty,"limit":px,"resp":r.get("status"),"order":oid}

def _spread_close_upstox(long_raw, short_raw, why, action):
    ssp=__import__("stockopt_spread")
    alerts=[]
    def place(order):
        side=order["transaction_type"]
        raw=short_raw if side=="BUY" else long_raw
        ltp=float(raw.get("last_price") or 0)
        px=_spread_limit(ltp,side,tick_size_for(raw.get("instrument_token")))
        if not px: return None
        qty=int(order.get("quantity") or order.get("qty") or 0)
        r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":qty,"product":raw.get("product") or "D","validity":"DAY","price":px,"tag":"spread_exit","instrument_token":raw["instrument_token"],"order_type":"LIMIT","transaction_type":side,"disclosed_quantity":0,"trigger_price":0,"is_amo":False})
        if DRY: return "DRY-"+side
        return (r.get("data") or {}).get("order_id")
    def wait(oid, order):
        if DRY or not oid or str(oid).startswith("DRY"):
            return {"filled": True, "qty": int(order.get("quantity") or 0)}
        import time
        want=int(order.get("quantity") or 0)
        for _ in range(6):
            try:
                d=req("https://api.upstox.com/v2/order/details?order_id="+str(oid)).get("data") or {}
                status=str(d.get("status") or "").lower()
                filled=int(d.get("filled_quantity") or 0)
                if status in ("complete","completed","filled","traded") or (want and filled>=want):
                    return {"filled": True, "qty": filled or want, "avg": d.get("average_price")}
                if status in ("rejected","cancelled","canceled","failed"):
                    return {"filled": False, "qty": filled, "status": status}
            except Exception:
                pass
            time.sleep(0.5)
        return {"filled": False, "qty": 0, "reason": "cover fill unverified"}
    def live(order):
        raw=short_raw if order["transaction_type"]=="BUY" else long_raw
        # re-read the book if possible
        q=upstox_live_qty(raw.get("instrument_token"))
        if q is None:
            return abs(int(raw.get("quantity") or 0))
        return abs(int(q))
    # cancel leftover lone SLs on both legs
    for raw in (long_raw, short_raw):
        if not raw: continue
        for o in pending_sl(raw.get("instrument_token")):
            write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE")
        _gtt_clear("upstox", raw.get("trading_symbol"), key=raw.get("instrument_token"))
    result=ssp.execute_pair_exit(place, wait,
                                 {"transaction_type":"BUY","quantity":abs(int((short_raw or {}).get("quantity") or 0))},
                                 {"transaction_type":"SELL","quantity":abs(int((long_raw or {}).get("quantity") or 0))},
                                 live_qty=live, alert=lambda m,d: alerts.append(m))
    return {"sym": (long_raw or {}).get("trading_symbol"),"short":(short_raw or {}).get("trading_symbol"),
            "action": action if result.get("ok") or DRY else "SPREAD_EXIT_FAIL",
            "why": why, "result": result, "alert": alerts or None}

_spread_u=_stockopt_spread_ctx(pos,"upstox")
if _spread_u and _spread_u.get("mod"):
    for _plan in _spread_u["mod"].reconcile_naked_shorts(_spread_u.get("legs") or [], ledger=_spread_u.get("ledger"), pairs=_spread_u.get("pairs")):
        acts.append(_spread_cover_upstox(_plan, pos))
for p in pos:
    sym=p["trading_symbol"]; q=int(p.get("quantity") or 0)
    if not (isopt(sym) or isfut(sym)) or q<=0: continue
    if isopt(sym) and trade_lock.underlying(sym) not in INDEX_KEYS and _spread_u and _spread_u.get("mod"):
        _pair,_role=_spread_u["mod"].find_pair(sym, _spread_u.get("pairs"))
        if _pair and _role=="long":
            lg=_pair.get("long") or {}; sh=_pair.get("short") or {}
            long_raw=_spread_find_raw(pos, lg.get("sym") or sym) or p
            short_raw=_spread_find_raw(pos, sh.get("sym") or sh.get("trading_symbol"))
            # cancel a leftover lone SL on the long (it would naked the short)
            k=p["instrument_token"]; longs.add(k)
            for o in pending_sl(k):
                acts.append({"sym":sym,"action":"CANCEL_SPREAD_SL","order":o["order_id"],
                             "resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
            long_leg={"sym":sym,"qty":q,"avg":p.get("average_price") or p.get("buy_price"),"ltp":p.get("last_price")}
            short_leg={"sym":(short_raw or {}).get("trading_symbol") or sh.get("sym"),
                       "qty":int((short_raw or sh).get("quantity") or sh.get("qty") or 0),
                       "avg":(short_raw or sh).get("average_price"),
                       "ltp":(short_raw or sh).get("last_price") or sh.get("ltp")}
            skey="SPREAD|upstox|"+sym
            sstate=st.setdefault(skey,{})
            dec=_spread_u["mod"].spread_day_decision(long_leg, short_leg, sstate)
            if dec.get("fire") and short_raw is not None:
                acts.append(_spread_close_upstox(long_raw, short_raw, dec.get("why"), dec.get("action") or "SPREAD_EXIT"))
            elif dec.get("fire") and short_raw is None:
                acts.append({"sym":sym,"action":"SPREAD_EXIT_SKIPPED","why":"short leg missing from the book; long not sold alone"})
            else:
                acts.append({"sym":sym,"action":"SPREAD_WATCH","why":dec.get("why") or "net/long stop not hit","net_now":dec.get("net_now"),"net_entry":dec.get("net_entry"),"stop":dec.get("stop"),"method":dec.get("method")})
            continue
        if _pair and _role=="short":
            continue  # managed with the long, or by the naked-short reconcile
    fut=isfut(sym)
    k=p["instrument_token"]; longs.add(k); ltp=p["last_price"]
    _ts=tick_size_for(k); _tk=lambda x,_t=_ts: tick_on(x,_t)  # 11 Nov: contract tick grid
    avg=p.get("day_buy_price") if p.get("day_buy_quantity",0)>0 and p.get("overnight_quantity",0)<=0 else None
    avg=avg or p.get("average_price") or p.get("buy_price") or p.get("day_buy_price")
    if not avg or not ltp: acts.append({"sym":sym,"action":"SKIP_NO_PRICE"}); continue
    TRAIL=0.015 if fut else 0.30; STEP=0.02 if fut else 0.20
    s=st.setdefault(k,{"high":avg}); s["sym"]=sym
    _pr,_pw,_ph=bind_position_peak(s,avg,ltp,q,day_sell=int(p.get("day_sell_quantity") or 0),overnight=int(p.get("overnight_quantity") or 0),orders=orders,sym=sym,token=k,step=STEP)
    s.setdefault("target",round(avg*(1+STEP),2)); held_keys.add(k)
    if prules is not None and not fut:
        s.setdefault("stop_pct",prules.INITIAL_STOP_PCT); TRAIL=prules.stop_pct(s)   # 20% new, 30% legacy
        _oversize("upstox",sym,q,trade_lock.lot_size(k,sym),p.get("overnight_quantity"),orders,token=k)
    if _pr: acts.append({"sym":sym,"action":"PEAK_RESET","why":_pw,"entry":round(float(avg),2),"peak":s["high"],"prev_high":_ph,"open_gen":s.get("open_gen")})
    _ok,_why,_aq=trade_lock.check_order({"trading_symbol":sym,"instrument_token":k,"transaction_type":"SELL","order_type":"SL","product":p["product"],"quantity":q})
    if _aq<=0: acts.append({"sym":sym,"action":"SKIP_R8","why":_why}); continue
    q=min(q,_aq)  # rule 8
    sls=pending_sl(k)
    for o in sls[1:]:
        acts.append({"sym":sym,"action":"CANCEL_DUP_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
    sls=sls[:1]
    floor=0
    # ---- exit rule (4 Nov, t287u/t288u): INDEX OPTIONS ONLY get the trend exit ----
    # Losses are always bounded by a fixed 30% floor under entry. For index options, once this position's high
    # (from its own fill, reset on flatten/re-entry) is 15% above entry the exit also follows the underlying
    # 15m trend (CE sold when close<EMA20 with Supertrend red, PE the opposite), so winners run.
    # Stock options and futures keep the plain % stop.
    INDEX_OPT = trade_lock.underlying(sym) in INDEX_KEYS
    _fa_exit=None
    if FA_TREND and not fut:
        _b,_berr=h1_bar(sym)
        if _b is None: ok,why=True,"1h bar unavailable ("+str(_berr)+"); fixed stop kept"
        else:
            _fx,_fw=fap.trend_exit_1h(opt_side(sym),_b,s)
            ok,why=(not _fx),f"{_fw} (1h {_b['key']} @{str(_b['ts'])[11:16]} ST {'green' if _b['dir']==1 else 'red'})"
            if _fx: _fa_exit="TREND_EXIT_1H"
    else:
        ok,why=trend_ok(sym) if (not fut and INDEX_OPT) else (True,"not an index option")
        if INDEX_OPT and s["high"]>=avg*1.15 and not ok: _fa_exit="TREND_EXIT"
    if not _fa_exit and INDEX_OPT and not fut:
        _c2,_c2w,_c2a=_two_candle_check(sym,p.get("product"),"upstox",instrument_key=k,orders=orders)
        if _c2: _fa_exit=_c2a or "TWO_CANDLE_EXIT"; why=_c2w
    if _fa_exit:
        gok,gdet=_gtt_clear("upstox",sym,key=k)
        if not gok:
            acts.append({"sym":sym,"action":"GTT_FAIL","why":f"overnight GTT not cancelled, trend exit not sent: {gdet}"}); continue
        for o in sls: write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE")
        _live=upstox_live_qty(k)   # oversell guard: a stop may have filled while it was being cancelled
        qx=q if _live is None else (fap.capped_sell(q,_live) if fap else min(q,max(_live,0)))  # unreadable -> old R8-capped q, never naked
        if qx<=0:
            acts.append({"sym":sym,"action":_fa_exit+"_SKIPPED","why":f"broker quantity now {_live}; nothing left to sell","trend":why}); continue
        lim=_tk(ltp*0.995)
        r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":qx,"product":p["product"],"validity":"DAY","price":lim,"tag":"target_book","instrument_token":k,"order_type":"LIMIT","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":0,"is_amo":False})
        acts.append({"sym":sym,"action":_fa_exit,"qty":qx,"broker_qty":_live,"avg":round(avg,2),"ltp":ltp,"limit":lim,"peak":s["high"],"trend":why,"pnl_est":round((lim-avg)*qx),"resp":r.get("status"),"err":r.get("body")})
        if prules is not None and r.get("status")=="success": prules.record_exit(k,sym,trade_lock.underlying(sym),"upstox",_fa_exit,now)
        continue
    # 5 Nov (audit #8): the target roll used to raise the stop floor above entry, i.e. a trailing stop.
    # That contradicts the rule for options (t288u): a FIXED 30% stop under entry that never trails from
    # the peak, with the trend exit being the only thing that moves an index option out of a winner.
    # Futures keep the floor they were designed with; options get it only as a plain %-of-entry stop.
    while ltp>=s["target"]:
        prev=s["target"]; s["target"]=round(prev+avg*STEP,2)
        if fut: floor=max(floor,round(prev-avg*STEP,2),avg)
        acts.append({**{"sym":sym},"action":"ROLL_TARGET","from":prev,"to":s["target"],"trend":why,
                     "stop_floor_moved":bool(fut)})
    if True:
        # 30% fixed floor under entry (never lowered); floor rises only when a target rolls
        if FA_LADDER and not fut: floor=max(floor,fap.ladder_floor(s.get("entry",avg),s["high"]))  # upward-only ladder
        stop=_tk(max(avg*(1-TRAIL),seeds.get(sym,0),s.get("stop",0),floor))
        if FA_LADDER and not fut and stop>=ltp and stop>avg*(1-TRAIL)+1e-6:
            # the ladder stop is already through the market: sell instead of placing a looser stop
            gok,gdet=_gtt_clear("upstox",sym,key=k)
            if not gok:
                acts.append({"sym":sym,"action":"GTT_FAIL","why":f"overnight GTT not cancelled, stop-breach exit not sent: {gdet}"}); continue
            for o in sls: write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE")
            _live=upstox_live_qty(k); qx=q if _live is None else fap.capped_sell(q,_live)
            if qx<=0:
                acts.append({"sym":sym,"action":"STOP_BREACH_EXIT_SKIPPED","why":f"broker quantity now {_live}"}); continue
            lim=_tk(ltp*0.995)
            r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":qx,"product":p["product"],"validity":"DAY","price":lim,"tag":"target_book","instrument_token":k,"order_type":"LIMIT","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":0,"is_amo":False})
            acts.append({"sym":sym,"action":"STOP_BREACH_EXIT","qty":qx,"broker_qty":_live,"ladder_stop":stop,"ltp":ltp,"limit":lim,"resp":r.get("status"),"err":r.get("body")})
            if prules is not None and r.get("status")=="success": prules.record_exit(k,sym,trade_lock.underlying(sym),"upstox","STOP_BREACH_EXIT",now)
            continue
        if stop>=ltp: stop=_tk(ltp*(0.995 if fut else 0.97))
        else: s["stop"]=stop  # ratchet: never lowered
        lim=_tk(stop*(0.99 if fut else 0.95))
        info={"sym":sym,"qty":q,"avg":round(avg,2),"ltp":ltp,"high":s["high"],"target":s["target"],"trigger":stop,"limit":lim}
        _note_trend_policy(info, sym, fut, avg, ltp, stop, q)
        if not sls:
            gok,gdet=_gtt_clear("upstox",sym,key=k)
            if not gok:
                acts.append({**info,"action":"GTT_FAIL","why":f"overnight GTT not cancelled, day SL not placed: {gdet}"}); continue
            r=write("https://api-hft.upstox.com/v2/order/place",{"quantity":q,"product":p["product"],"validity":"DAY","price":lim,"tag":"trailsl","instrument_token":k,"order_type":"SL","transaction_type":"SELL","disclosed_quantity":0,"trigger_price":stop,"is_amo":False})
            acts.append({**info,"action":"PLACE_SL","resp":r.get("status"),"err":r.get("body")})
            if not DRY and r.get("status")=="success": sc.clear("upstox",sym)
        else:
            o=sls[0]
            if not DRY: sc.clear("upstox",sym)
            t2=pl.sl_modify_trigger(stop,o["trigger_price"],qty_changed=o["quantity"]!=q,reset=_pr)
            if t2 is not None:
                r=write("https://api-hft.upstox.com/v2/order/modify",{"order_id":o["order_id"],"quantity":q,"validity":"DAY","price":_tk(t2*(0.99 if fut else 0.95)),"order_type":"SL","trigger_price":t2,"disclosed_quantity":0},method="PUT")
                acts.append({**info,"action":"MODIFY_SL","from":o["trigger_price"],"to":t2,"resp":r.get("status"),"err":r.get("body")})
            else: acts.append({**info,"action":"SL_OK","sl_trigger":o["trigger_price"]})
# ---------------- Upstox MTF equity: NO stop management here (5 Nov 2026) ----------------
# This block used to ratchet a SELL SL to 30% below the highest LTP seen. That is the OPTION
# stop rule and it does not belong in the equity MTF book: per t263u, MTF positions get plain
# protective stops only, never a trail. equity/upstox_mtf_stops.py owns MTF stops (5% below LTP
# or the 1h Supertrend when nearer, never lowered, clamped inside the circuit band), and having
# two writers on the same orders made whichever ran last win. risk_manager keeps only the
# bookkeeping job: cancel a leftover MTF SELL SL on a name that is no longer held.
mtf_longs=set(); mtf_known=False
try:
    mr=req("https://api.upstox.com/v3/portfolio/mtf-positions")
    if mr.get("status")!="success":
        acts.append({"action":"MTF_SKIPPED","why":"mtf positions fetch failed","code":mr.get("code")})
    else:
        for p in mr.get("data") or []:
            k=p.get("instrument_token")
            if k and int(p.get("quantity") or 0)>0: mtf_longs.add(k)
        # 5 Nov: an empty/partial MTF list is NOT proof that every MTF SL is stray. It has happened
        # (a truncated payload, or a real book that reads [] for a moment) and this sweep then
        # cancelled live stops on every MTF holding. Only sweep when the book either shows something
        # or short-term positions independently agree the name is gone.
        st_keys={p.get("instrument_token") for p in pos if int(p.get("quantity") or 0)>0}
        mtf_known=bool(mtf_longs)
        if not mtf_known:
            acts.append({"action":"MTF_EMPTY","why":"MTF book read empty - stray MTF stop sweep skipped (an empty book is not proof a position closed)"})
        for o in orders:
            if not (o["transaction_type"]=="SELL" and (o.get("product") or "").upper()=="MTF"
                    and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M")):
                continue
            if o["instrument_token"] in mtf_longs: continue
            if not mtf_known and o["instrument_token"] in st_keys:
                acts.append({"sym":o.get("trading_symbol"),"action":"KEEP_SL","order":o["order_id"],
                             "why":"MTF book read empty but short-term positions still hold it"})
                continue
            if not mtf_known:
                acts.append({"sym":o.get("trading_symbol"),"action":"KEEP_SL","order":o["order_id"],
                             "why":"MTF book read empty - not cancelling a stop on an unconfirmed close"})
                continue
            acts.append({"sym":o.get("trading_symbol"),"action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
except Exception as e: acts.append({"action":"MTF_ERROR","why":f"{type(e).__name__}: {e}"})
# stray SELL SLs on option contracts with no long position -> cancel (prevents accidental shorts)
for o in orders:
    if o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M") and isopt(o.get("trading_symbol") or o.get("tradingsymbol","")) and o["instrument_token"] not in longs:
        acts.append({"sym":o.get("trading_symbol"),"action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":write("https://api-hft.upstox.com/v2/order/cancel?order_id="+o["order_id"],method="DELETE").get("status")})
# ---------------- Kite positions (4 Nov): Upstox prices/trend, Kite orders, same stop/roll rules ----------------
kacts=[]
try:
    import kite_disabled as _kd
    import broker_kite as bk
    if _kd.active():
        kacts.append({"action":"KITE_SKIPPED","why":"kite disabled by user 21 Nov"})
    elif not bk.valid(): kacts.append({"action":"KITE_SKIPPED","why":"Kite token invalid - no-op"}); bk.log("risk_manager: kite skipped, invalid token")
    else:
        # strict reads: a failed book read here used to come back as [], and the stray-SL sweep at
        # the end of this block then cancelled every live Kite option stop on the account. Refuse
        # the whole Kite section instead.
        kpos=bk.positions(strict=True); kord=bk.orders(strict=True); klongs=set()
        _spread_k=_stockopt_spread_ctx(kpos,"kite")
        def kpending(ts): return [o for o in kord if o["trading_symbol"]==ts and o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M")]
        def _spread_cover_kite(plan):
            raw=_spread_find_raw(kpos, plan.get("short_sym"), "kite")
            if not raw:
                return {"sym":plan.get("short_sym"),"broker":"kite","action":"COVER_NAKED_SHORT_SKIP","why":"short not in the book"}
            qty=abs(int(raw.get("quantity") or plan.get("qty") or 0))
            ltp=float(raw.get("last_price") or 0)
            px=tick(ltp*1.005) if ltp else None
            if not qty or not px:
                return {"sym":raw.get("trading_symbol"),"broker":"kite","action":"COVER_NAKED_SHORT_SKIP","why":"qty or price unverified"}
            oid=bk.place(raw.get("exchange") or "NFO", raw["trading_symbol"], "BUY", qty, px, tag="spread_cover", product=raw.get("product") or "NRML", dry=DRY)
            return {"sym":raw["trading_symbol"],"broker":"kite","action":"COVER_NAKED_SHORT" if (oid or DRY) else "COVER_NAKED_SHORT_FAIL","qty":qty,"limit":px,"order":oid}
        def _spread_close_kite(long_raw, short_raw, why, action):
            ssp=__import__("stockopt_spread")
            alerts=[]
            def place(order):
                side=order["transaction_type"]
                raw=short_raw if side=="BUY" else long_raw
                ltp=float(raw.get("last_price") or 0)
                px=tick(ltp*(1.005 if side=="BUY" else 0.995)) if ltp else None
                if not px: return None
                return bk.place(raw.get("exchange") or "NFO", raw["trading_symbol"], side, int(order.get("quantity") or 0), px, tag="spread_exit", product=raw.get("product") or "NRML", dry=DRY) or ("DRY-"+side if DRY else None)
            def wait(oid, order):
                if DRY or not oid or str(oid).startswith("DRY"):
                    return {"filled": True, "qty": int(order.get("quantity") or 0)}
                import time
                want=int(order.get("quantity") or 0)
                for _ in range(6):
                    try:
                        stt=bk.order_status(oid) or {}
                        status=str(stt.get("status") or "").lower()
                        filled=int(stt.get("filled") or 0)
                        if status in ("complete","completed","filled","traded") or (want and filled>=want):
                            return {"filled": True, "qty": filled or want, "avg": stt.get("avg")}
                        if status in ("rejected","cancelled","canceled","failed"):
                            return {"filled": False, "qty": filled, "status": status}
                    except Exception:
                        pass
                    time.sleep(0.5)
                return {"filled": False, "qty": 0, "reason": "cover fill unverified"}
            def live(order):
                raw=short_raw if order["transaction_type"]=="BUY" else long_raw
                q=kite_live_qty(bk, raw["trading_symbol"])
                if q is None:
                    return abs(int(raw.get("quantity") or 0))
                return abs(int(q))
            for raw in (long_raw, short_raw):
                if not raw: continue
                for o in kpending(raw["trading_symbol"]):
                    bk.cancel(o["order_id"], dry=DRY)
                _gtt_clear("kite", raw["trading_symbol"])
            result=ssp.execute_pair_exit(place, wait,
                                         {"transaction_type":"BUY","quantity":abs(int((short_raw or {}).get("quantity") or 0))},
                                         {"transaction_type":"SELL","quantity":abs(int((long_raw or {}).get("quantity") or 0))},
                                         live_qty=live, alert=lambda m,d: alerts.append(m))
            return {"sym":(long_raw or {}).get("trading_symbol"),"broker":"kite","short":(short_raw or {}).get("trading_symbol"),
                    "action": action if result.get("ok") or DRY else "SPREAD_EXIT_FAIL",
                    "why": why, "result": result, "alert": alerts or None}
        if _spread_k and _spread_k.get("mod"):
            for _plan in _spread_k["mod"].reconcile_naked_shorts(_spread_k.get("legs") or [], ledger=_spread_k.get("ledger"), pairs=_spread_k.get("pairs")):
                kacts.append(_spread_cover_kite(_plan))
        for p in kpos:
            sym=p["trading_symbol"]; q=p["quantity"]; ex=p["exchange"]
            if not (isopt(sym) or isfut(sym)) or q<=0: continue
            if isopt(sym) and trade_lock.underlying(sym) not in INDEX_KEYS and _spread_k and _spread_k.get("mod"):
                _pair,_role=_spread_k["mod"].find_pair(sym, _spread_k.get("pairs"))
                if _pair and _role=="long":
                    lg=_pair.get("long") or {}; sh=_pair.get("short") or {}
                    long_raw=_spread_find_raw(kpos, lg.get("sym") or sym, "kite") or p
                    short_raw=_spread_find_raw(kpos, sh.get("sym") or sh.get("trading_symbol"), "kite")
                    klongs.add(sym)
                    for o in kpending(sym):
                        kacts.append({"sym":sym,"broker":"kite","action":"CANCEL_SPREAD_SL","order":o["order_id"],
                                      "resp":bk.cancel(o["order_id"],dry=DRY)})
                    long_leg={"sym":sym,"qty":q,"avg":p.get("average_price") or p.get("buy_price"),"ltp":p.get("last_price")}
                    short_leg={"sym":(short_raw or {}).get("trading_symbol") or sh.get("sym"),
                               "qty":int((short_raw or sh).get("quantity") or sh.get("qty") or 0),
                               "avg":(short_raw or sh).get("average_price"),
                               "ltp":(short_raw or sh).get("last_price") or sh.get("ltp")}
                    skey="SPREAD|kite|"+sym
                    sstate=st.setdefault(skey,{})
                    dec=_spread_k["mod"].spread_day_decision(long_leg, short_leg, sstate)
                    if dec.get("fire") and short_raw is not None:
                        kacts.append(_spread_close_kite(long_raw, short_raw, dec.get("why"), dec.get("action") or "SPREAD_EXIT"))
                    elif dec.get("fire") and short_raw is None:
                        kacts.append({"sym":sym,"broker":"kite","action":"SPREAD_EXIT_SKIPPED","why":"short leg missing from the book; long not sold alone"})
                    else:
                        kacts.append({"sym":sym,"broker":"kite","action":"SPREAD_WATCH","why":dec.get("why") or "net/long stop not hit","net_now":dec.get("net_now"),"net_entry":dec.get("net_entry"),"stop":dec.get("stop"),"method":dec.get("method")})
                    continue
                if _pair and _role=="short":
                    continue
            fut=isfut(sym); klongs.add(sym); kk="KITE|%s|%s"%(ex,sym)
            ukey=bk.to_upstox(ex,sym); ltp=None
            if ukey:
                import urllib.parse
                d=req("https://api.upstox.com/v2/market-quote/ltp?instrument_key="+urllib.parse.quote(ukey,safe="")).get("data") or {}
                for v in d.values(): ltp=float(v.get("last_price") or 0) or None
            ltp=ltp or p["last_price"]; avg=p["average_price"] or p["buy_price"]
            if not avg or not ltp: kacts.append({"sym":sym,"broker":"kite","action":"SKIP_NO_PRICE"}); continue
            TRAIL=0.015 if fut else 0.30; STEP=0.02 if fut else 0.20
            s=st.setdefault(kk,{"high":avg}); s["sym"]=sym; s["broker"]="kite"
            _pr,_pw,_ph=bind_position_peak(s,avg,ltp,q,day_sell=int(p.get("day_sell_quantity") or 0),overnight=int(p.get("overnight_quantity") or 0),orders=kord,sym=sym,token=kk,step=STEP)
            s.setdefault("target",round(avg*(1+STEP),2)); held_keys.add(kk)
            if prules is not None and not fut:
                s.setdefault("stop_pct",prules.INITIAL_STOP_PCT); TRAIL=prules.stop_pct(s)   # 20% new, 30% legacy
                try: _klot=int(float((bk._index(ex)[1].get(sym) or {}).get("lot_size") or 0)) or None
                except Exception: _klot=None
                _oversize("kite",sym,q,_klot,p.get("overnight_quantity"),kord,token=None)
            if _pr: kacts.append({"sym":sym,"broker":"kite","action":"PEAK_RESET","why":_pw,"entry":round(float(avg),2),"peak":s["high"],"prev_high":_ph,"open_gen":s.get("open_gen")})
            _ok,_why,_aq=trade_lock.check_order({"trading_symbol":sym,"instrument_token":kk,"transaction_type":"SELL","order_type":"SL","product":p["product"],"quantity":q},broker="kite")
            if _aq<=0: kacts.append({"sym":sym,"broker":"kite","action":"SKIP_R8","why":_why}); continue
            q=min(q,_aq)
            sls=kpending(sym)
            for o in sls[1:]: kacts.append({"sym":sym,"broker":"kite","action":"CANCEL_DUP_SL","order":o["order_id"],"resp":bk.cancel(o["order_id"],dry=DRY)})
            sls=sls[:1]; floor=0
            INDEX_OPT = trade_lock.underlying(sym) in INDEX_KEYS
            _fa_exit=None
            if FA_TREND and not fut:
                _b,_berr=h1_bar(sym)
                if _b is None: ok,why=True,"1h bar unavailable ("+str(_berr)+"); fixed stop kept"
                else:
                    _fx,_fw=fap.trend_exit_1h(opt_side(sym),_b,s)
                    ok,why=(not _fx),f"{_fw} (1h {_b['key']} @{str(_b['ts'])[11:16]} ST {'green' if _b['dir']==1 else 'red'})"
                    if _fx: _fa_exit="TREND_EXIT_1H"
            else:
                ok,why=trend_ok(sym) if (not fut and INDEX_OPT) else (True,"not an index option")
                if INDEX_OPT and s["high"]>=avg*1.15 and not ok: _fa_exit="TREND_EXIT"
            if not _fa_exit and INDEX_OPT and not fut:
                _c2,_c2w,_c2a=_two_candle_check(sym,p.get("product"),"kite",exchange=ex,orders=kord)
                if _c2: _fa_exit=_c2a or "TWO_CANDLE_EXIT"; why=_c2w
            if _fa_exit:
                gok,gdet=_gtt_clear("kite",sym)
                if not gok:
                    kacts.append({"sym":sym,"broker":"kite","action":"GTT_FAIL","why":f"overnight GTT not cancelled, trend exit not sent: {gdet}"}); continue
                for o in sls: bk.cancel(o["order_id"],dry=DRY)
                _live=kite_live_qty(bk,sym)   # oversell guard
                qx=q if _live is None else (fap.capped_sell(q,_live) if fap else min(q,max(_live,0)))  # unreadable -> old R8-capped q, never naked
                if qx<=0:
                    kacts.append({"sym":sym,"broker":"kite","action":_fa_exit+"_SKIPPED","why":f"broker quantity now {_live}; nothing left to sell","trend":why}); continue
                lim=tick(ltp*0.995)
                r=bk.place(ex,sym,"SELL",qx,lim,tag="target_book",product=p["product"],dry=DRY)
                kacts.append({"sym":sym,"broker":"kite","action":_fa_exit,"qty":qx,"broker_qty":_live,"avg":round(avg,2),"ltp":ltp,"limit":lim,"peak":s["high"],"trend":why,"resp":r})
                if prules is not None and r and not DRY: prules.record_exit(kk,sym,trade_lock.underlying(sym),"kite",_fa_exit,now,to_upstox=bk.to_upstox)
                continue
            while ltp>=s["target"]:
                prev=s["target"]; s["target"]=round(prev+avg*STEP,2)
                if fut: floor=max(floor,round(prev-avg*STEP,2),avg)
                kacts.append({"sym":sym,"broker":"kite","action":"ROLL_TARGET","from":prev,"to":s["target"],
                              "trend":why,"stop_floor_moved":bool(fut)})
            if True:
                _kts=bk.tick_for(ex,sym)
                _ktick=lambda x,_t=_kts: tick_on(x,_t)  # contract grid (MCX crude options are 0.10, not 0.05)
                if FA_LADDER and not fut: floor=max(floor,fap.ladder_floor(s.get("entry",avg),s["high"]))  # upward-only ladder
                stop=_ktick(max(avg*(1-TRAIL),seeds.get(sym,0),s.get("stop",0),floor))
                if FA_LADDER and not fut and stop>=ltp and stop>avg*(1-TRAIL)+1e-6:
                    gok,gdet=_gtt_clear("kite",sym)
                    if not gok:
                        kacts.append({"sym":sym,"broker":"kite","action":"GTT_FAIL","why":f"overnight GTT not cancelled, stop-breach exit not sent: {gdet}"}); continue
                    for o in sls: bk.cancel(o["order_id"],dry=DRY)
                    _live=kite_live_qty(bk,sym); qx=q if _live is None else fap.capped_sell(q,_live)
                    if qx<=0:
                        kacts.append({"sym":sym,"broker":"kite","action":"STOP_BREACH_EXIT_SKIPPED","why":f"broker quantity now {_live}"}); continue
                    lim=tick(ltp*0.995)
                    _kr=bk.place(ex,sym,"SELL",qx,lim,tag="target_book",product=p["product"],dry=DRY)
                    kacts.append({"sym":sym,"broker":"kite","action":"STOP_BREACH_EXIT","qty":qx,"broker_qty":_live,"ladder_stop":stop,"ltp":ltp,"limit":lim,"resp":_kr})
                    if prules is not None and _kr and not DRY: prules.record_exit(kk,sym,trade_lock.underlying(sym),"kite","STOP_BREACH_EXIT",now,to_upstox=bk.to_upstox)
                    continue
                if stop>=ltp: stop=_ktick(ltp*(0.995 if fut else 0.97))
                lim=_ktick(stop*(0.99 if fut else 0.95))
                info={"sym":sym,"broker":"kite","qty":q,"avg":round(avg,2),"ltp":ltp,"high":s["high"],"target":s["target"],"trigger":stop,"limit":lim}
                _note_trend_policy(info, sym, fut, avg, ltp, stop, q)
                if not sls:
                    gok,gdet=_gtt_clear("kite",sym)
                    if not gok:
                        kacts.append({**info,"action":"GTT_FAIL","broker":"kite","why":f"overnight GTT not cancelled, day SL not placed: {gdet}"}); continue
                    _prsp=bk.place(ex,sym,"SELL",q,lim,order_type="SL",trigger=stop,tag="trailsl",product=p["product"],dry=DRY)
                    kacts.append({**info,"action":"PLACE_SL","resp":_prsp})
                    if _prsp and not DRY: s["stop"]=stop
                    if not DRY: sc.clear("kite",sym)
                else:
                    o=sls[0]
                    t2=pl.sl_modify_trigger(stop,o["trigger_price"],qty_changed=o["quantity"]!=q,reset=_pr)
                    if t2 is not None:
                        t2=_ktick(t2)
                        _lim=_ktick(t2*(0.99 if fut else 0.95))
                        info["trigger"]=t2; info["limit"]=_lim
                        _mr=bk.modify(o["order_id"],q,_lim,t2,dry=DRY)
                        if _mr:
                            if not DRY: s["stop"]=stop
                            kacts.append({**info,"action":"MODIFY_SL","from":o["trigger_price"],"to":t2,"resp":_mr})
                        else:
                            _live=o["trigger_price"]; _msg=""
                            try:
                                for _xo in (bk.orders() or []):
                                    if str(_xo.get("order_id"))==str(o["order_id"]):
                                        _live=_xo.get("trigger_price"); _msg=_xo.get("status_message") or ""
                                        break
                            except Exception:
                                pass
                            kacts.append({**info,"action":"MODIFY_SL_FAIL","from":o["trigger_price"],"to":t2,"live_trigger":_live,"err":_msg or "modify did not stick","resp":_mr})
                    else:
                        if not DRY: s["stop"]=stop
                        kacts.append({**info,"action":"SL_OK","sl_trigger":o["trigger_price"]})
        for o in kord:
            if o["transaction_type"]=="SELL" and o["status"] in("trigger pending","open") and o["order_type"] in("SL","SL-M") and isopt(o["trading_symbol"]) and o["trading_symbol"] not in klongs:
                kacts.append({"sym":o["trading_symbol"],"broker":"kite","action":"CANCEL_STRAY_SL","order":o["order_id"],"resp":bk.cancel(o["order_id"],dry=DRY)})
        kite_book_ok=True
except Exception as e: kacts.append({"action":"KITE_ERROR","why":f"{type(e).__name__}: {e}"})
# Flat names drop the peak so a later re-entry starts from its own fill. Only books we fully walked.
for _k,_s in st.items():
    if _k.startswith("_") or not isinstance(_s,dict):
        continue
    if (str(_k).startswith("KITE|") or str(_k).startswith("KITE:")) and not kite_book_ok:
        continue
    if _k not in held_keys:
        _s["live_qty"]=0
        _s["open_gen"]=None
OVERSIZE=[]
if prules is not None and OVERSIZE_ITEMS:
    try: OVERSIZE=prules.oversize_alerts(OVERSIZE_ITEMS,now,persist=not DRY)
    except Exception as _oe: print(f"risk_manager: oversize alerts failed: {type(_oe).__name__}",file=sys.stderr)
if not DRY: json.dump(st,open(SF,"w"))
print(json.dumps({"dry_run":DRY,"now":now.strftime("%Y-%m-%d %H:%M IST"),"oversize":OVERSIZE,"full_auto":FA_NOTE,"breaker":breaker,"actions":acts,"kite_actions":kacts},indent=1))
