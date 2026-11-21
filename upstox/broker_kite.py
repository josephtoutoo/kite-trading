#!/usr/bin/env python3
"""Kite (Zerodha) broker adapter for the Upstox automation (4 Nov 2026).
- Token: /workspace/kite/token.json {"access_token"} ; api key from env KITE_API_KEY, else the api_key query param in /workspace/kite/login_url.txt. Never printed.
- Kite API calls use UPSTOX_PROXY_URL when that env var is set. If token/key invalid -> valid()=False and
  every write/read helper no-ops (returns None / []), so callers never break.
- Orders: LIMIT (or SL for stops) only, NRML for options, tag auto_entry by default. MARKET/SL-M refused.
- Symbol map: Upstox instrument_key (e.g. NSE_FO|50978) <-> Kite tradingsymbol via exchange_token, using the
  public dump https://api.kite.trade/instruments/{NFO,BFO} (cached daily).
CLI: python3 broker_kite.py status | map NSE_FO|50978 | positions | orders"""
import os,sys,json,csv,io,datetime,urllib.request
D=os.path.dirname(os.path.abspath(__file__))
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
TOKF=os.environ.get("KITE_TOKEN_FILE","/workspace/kite/token.json")
SEG={"NSE_FO":"NFO","BSE_FO":"BFO","MCX_FO":"MCX"}; RSEG={v:k for k,v in SEG.items()}
_k=None; _valid=None; _why=""; _dump={}
# (5 Nov 2026) One run reads the same Kite book several times (trade_lock wants positions+orders,
# mtf_sizing wants the order book for my tags, the stop scripts want orders AND holdings), and each
# read is a proxied round trip. These reads are memoised for READ_TTL seconds and dropped by
# forget() immediately after any write, so nothing can act on a book from before an order.
READ_TTL=3.0
_reads={}          # ('orders'|'positions'|'holdings') -> (expires_at, value)
# (5 Nov 2026) Lookup indexes over the daily CSV masters. dump() already parsed each exchange
# once, but from_upstox/to_upstox/tick_for then walked the whole list (NFO is ~100k rows) on
# every single call - that is the hot path in option strikes and MTF stop placement. The
# indexes are keyed on (path, mtime, size) and keep the FIRST row per key, exactly the row the
# linear scan returned, so every mapping result is unchanged.
_idx={}
import kite_disabled as _kd
def log(msg):
    with open(os.path.join(D,"kite_broker.log"),"a") as f: f.write(f"{datetime.datetime.now(IST):%Y-%m-%d %H:%M:%S} {msg}\n")
def _blocked(what):
    """Logged no-op when the user has disabled Kite writes (t783u/t784u)."""
    if not _kd.active():
        return False
    log(_kd.skip_reason(what))
    return True
def api_key():
    """KITE_API_KEY, or the api_key param in login_url.txt. Populates the env var for this process only. Never logged."""
    k=(os.environ.get("KITE_API_KEY") or "").strip()
    if not k:
        try:
            from urllib.parse import urlparse, parse_qs
            q=parse_qs(urlparse(open(os.environ.get("KITE_LOGIN_URL_FILE","/workspace/kite/login_url.txt")).read().strip()).query)
            k=((q.get("api_key") or [""])[0] or "").strip()
        except Exception:
            k=""
        if k and not os.environ.get("KITE_API_KEY"):
            os.environ["KITE_API_KEY"]=k
    return k
def client():
    global _k
    if _k is None:
        from kiteconnect import KiteConnect
        key=api_key()
        if not key: raise RuntimeError("KITE_API_KEY not set")
        px=os.environ.get("UPSTOX_PROXY_URL")
        _k=KiteConnect(api_key=key,proxies={"https":px,"http":px} if px else None,timeout=20)
        _k.set_access_token(json.load(open(TOKF))["access_token"])
    return _kd.wrap_client(_k)
def valid():
    """True only if a live profile call succeeds. Cached per process."""
    global _valid,_why
    if _valid is None:
        try:
            if not api_key(): raise RuntimeError("KITE_API_KEY not set")
            client().profile(); _valid=True; _why="ok"
        except Exception as e:
            _valid=False; _why=f"{type(e).__name__}: {str(e)[:150]}"; log("kite invalid: "+_why)
    return _valid
def why(): valid(); return _why
# ---------- instruments ----------
def dump(exch):
    if exch in _dump: return _dump[exch]
    f=os.path.join(D,f"kite_instruments_{exch}_{datetime.datetime.now(IST).date()}.csv")
    if not os.path.exists(f):
        for o in os.listdir(D):
            if o.startswith(f"kite_instruments_{exch}_"): os.remove(os.path.join(D,o))
        raw=urllib.request.urlopen(f"https://api.kite.trade/instruments/{exch}",timeout=60).read().decode()
        open(f,"w").write(raw)
    _dump[exch]=list(csv.DictReader(open(f))); return _dump[exch]
def _index(exch):
    """-> (by_token, by_symbol) for one exchange's master, built once per (path, mtime, size)."""
    rows=dump(exch)
    f=os.path.join(D,f"kite_instruments_{exch}_{datetime.datetime.now(IST).date()}.csv")
    try:
        st=os.stat(f); stamp=(st.st_mtime_ns,st.st_size)
    except OSError:
        stamp=None
    hit=_idx.get(exch)
    if hit and hit[0]==stamp: return hit[1]
    by_tok={}; by_sym={}
    for r in rows:                                  # first row per key wins, as the scan did
        by_tok.setdefault(r["exchange_token"],r)
        by_sym.setdefault(r["tradingsymbol"],r)
    _idx[exch]=(stamp,(by_tok,by_sym))
    return by_tok,by_sym
def from_upstox(key):
    """NSE_FO|50978 -> {'exchange':'NFO','tradingsymbol':..,'lot_size':..} or None"""
    try:
        seg,tok=key.split("|"); exch=SEG[seg]
        r=_index(exch)[0].get(tok)
        if r is not None: return {"exchange":exch,"tradingsymbol":r["tradingsymbol"],"lot_size":int(r["lot_size"]),"tick_size":float(r["tick_size"])}
    except Exception as e: log(f"map fail {key}: {e}")
    return None
def to_upstox(exch,tsym):
    r=_index(exch)[1].get(tsym)
    if r is not None: return f"{RSEG[exch]}|{r['exchange_token']}"
    return None
# ---------- reads (normalized to Upstox-like dicts) ----------
def forget():
    """Drop the read memo - call after placing/cancelling/modifying an order."""
    _reads.clear()
def _memo(name,fn):
    if READ_TTL>0:
        hit=_reads.get(name)
        if hit and hit[0]>__import__("time").monotonic(): return hit[1]
    val=fn()
    if READ_TTL>0: _reads[name]=(__import__("time").monotonic()+READ_TTL,val)
    return val
def positions(strict=False):
    """Normalized Kite positions. strict=True RAISES instead of returning [] on a failed read:
    a silent [] makes a caller believe the book is empty, and risk_manager used to cancel every
    live Kite option stop as 'stray' when that happened (5 Nov 2026)."""
    if strict:
        if not valid(): raise RuntimeError('Kite token invalid')
        return _positions(True)
    if not valid(): return []
    return _memo("positions",_positions)
def _positions(strict=False):
    try: net=client().positions().get("net") or []
    except Exception as e:
        log(f"positions fail {e}")
        if strict: raise
        return []
    out=[]
    for p in net:
        row={"broker":"kite","trading_symbol":p["tradingsymbol"],"tradingsymbol":p["tradingsymbol"],"exchange":p["exchange"],
            "instrument_token":"KITE|%s|%s"%(p["exchange"],p["tradingsymbol"]),"quantity":int(p["quantity"]),
            "average_price":float(p.get("average_price") or 0),"buy_price":float(p.get("buy_price") or 0),"last_price":float(p.get("last_price") or 0),
            "pnl":float(p.get("pnl") or 0),"realised":float(p.get("realised") or 0),"unrealised":float(p.get("unrealised") or 0),
            "product":p.get("product"),"upstox_key":None}
        for src, dst in (("day_buy_quantity","day_buy_quantity"),("day_sell_quantity","day_sell_quantity"),
                         ("overnight_quantity","overnight_quantity")):
            if src in p and p.get(src) is not None: row[dst]=int(p.get(src) or 0)
        # 16 Nov 2026 (t669u loss-cap tally fix): prices daily_loss_cap needs to value a carried
        # position at the previous close and to book a partly closed row. Read-only pass-through.
        for src in ("close_price","day_buy_price","day_sell_price","multiplier"):
            if src in p and p.get(src) is not None:
                try: row[src]=float(p.get(src) or 0)
                except (TypeError,ValueError): pass
        out.append(row)
    return out
def holdings():
    """Kite holdings normalized: MTF shares carry product MTF, delivery shares CNC. Used by trade_lock R8."""
    if not valid(): return []
    return _memo("holdings",_holdings)
def _holdings():
    try: hs=client().holdings() or []
    except Exception as e: log(f"holdings fail {e}"); return []
    out=[]
    for h in hs:
        sym=h["tradingsymbol"]; ex=h["exchange"]; tok="KITE|%s|%s"%(ex,sym)
        mtf=h.get("mtf") or {}
        mq=int(mtf.get("quantity") or 0); cq=int(h.get("quantity") or 0)+int(h.get("t1_quantity") or 0)
        base={"broker":"kite","trading_symbol":sym,"exchange":ex,"instrument_token":tok,"is_holding":True,
              "average_price":float(h.get("average_price") or 0),"buy_price":float(h.get("average_price") or 0),
              "last_price":float(h.get("last_price") or 0),"pnl":float(h.get("pnl") or 0),"realised":0.0,"unrealised":float(h.get("pnl") or 0),"upstox_key":None}
        if mq>0:
            d=dict(base); d.update({"quantity":mq,"product":"MTF","average_price":float(mtf.get("average_price") or base["average_price"])}); out.append(d)
        if cq>0:
            d=dict(base); d.update({"quantity":cq,"product":"CNC"}); out.append(d)
    return out
def orders(strict=False):
    """Normalized Kite orders. strict=True RAISES on a failed read - use it wherever an empty
    list would be read as 'nothing is live' (stop placement, stray-stop cleanup)."""
    if strict:
        if not valid(): raise RuntimeError('Kite token invalid')
        return _orders(True)
    if not valid(): return []
    return _memo("orders",_orders)
def _orders(strict=False):
    try: od=client().orders() or []
    except Exception as e:
        log(f"orders fail {e}")
        if strict: raise
        return []
    out=[]
    for o in od:
        ts=o.get("order_timestamp"); ts=ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts,"strftime") else ts
        out.append({"broker":"kite","order_id":o["order_id"],"trading_symbol":o["tradingsymbol"],"exchange":o["exchange"],
            "instrument_token":"KITE|%s|%s"%(o["exchange"],o["tradingsymbol"]),"transaction_type":o["transaction_type"],
            "status":(o.get("status") or "").lower(),"order_type":o.get("order_type"),"quantity":int(o.get("quantity") or 0),
            "filled_quantity":int(o.get("filled_quantity") or 0),"average_price":float(o.get("average_price") or 0),
            "trigger_price":float(o.get("trigger_price") or 0),"price":float(o.get("price") or 0),"order_timestamp":ts,"tag":o.get("tag"),
            "status_message":o.get("status_message") or "",
            # 16 Nov 2026 (t669u): product for the per-row FIFO tally, fill times for the re-entry block.
            # Stored as kite_product (not "product") so MTF/capital code that keys on product is unchanged.
            "kite_product":o.get("product"),
            "exchange_timestamp":(o.get("exchange_timestamp").strftime("%Y-%m-%d %H:%M:%S") if hasattr(o.get("exchange_timestamp"),"strftime") else o.get("exchange_timestamp")),
            "exchange_update_timestamp":(o.get("exchange_update_timestamp").strftime("%Y-%m-%d %H:%M:%S") if hasattr(o.get("exchange_update_timestamp"),"strftime") else o.get("exchange_update_timestamp"))})
    return out
def order_status(oid):
    if not valid(): return None
    try:
        h=client().order_history(oid)[-1]
        return {"status":(h.get("status") or "").lower(),"filled":int(h.get("filled_quantity") or 0),"avg":float(h.get("average_price") or 0)}
    except Exception as e: log(f"history fail {oid} {e}"); return None
# ---------- writes ----------
def tick_for(exch,tsym,default=0.05):
    """Tick size for a contract; Kite rejects prices off the grid (16283), so round every price to this."""
    r=_index(exch)[1].get(tsym)
    if r is not None:
        try: return float(r.get("tick_size") or default)
        except Exception: return default
    return default
def snap(p,ts):
    """Round p onto the tick grid ts."""
    if p is None or not ts: return p
    return round(round(float(p)/ts)*ts,4)
def place(exch,tsym,side,qty,price,order_type="LIMIT",trigger=None,tag="auto_entry",product="NRML",dry=False):
    """-> order_id | 'DRY' | None. LIMIT/SL only. Prices snapped to the contract tick."""
    if order_type not in("LIMIT","SL"): log(f"refused {order_type} {tsym}"); return None
    ts=tick_for(exch,tsym)
    price=snap(price,ts); trigger=snap(trigger,ts)
    body=dict(variety="regular",exchange=exch,tradingsymbol=tsym,transaction_type=side,quantity=int(qty),product=product,
              order_type=order_type,price=price,validity="DAY",tag=tag[:20])
    if order_type=="SL": body["trigger_price"]=trigger
    if _blocked(f"place {side} {qty} {tsym}"): return None
    if dry: log("DRY place "+json.dumps(body)); return "DRY"
    if not valid(): log("skip place (kite invalid) "+tsym); return None
    try:
        oid=client().place_order(**body); forget(); log(f"placed {oid} {json.dumps(body)}"); return oid
    except Exception as e: log(f"place fail {tsym}: {e}"); return None
def modify(oid,qty,price,trigger=None,order_type="SL",dry=False):
    """Modify a regular order. Prices are snapped to the contract tick.
    Returns the order id only if a post-modify readback shows the live trigger
    actually moved (or no trigger was sent); otherwise None. Exchange rejects
    are written to kite_broker.log."""
    if _blocked(f"modify {oid}"): return None
    if dry: return "DRY"
    if not valid(): return None
    try:
        exch=tsym=None
        for o in (orders() or []):
            if str(o.get("order_id"))==str(oid):
                exch=o.get("exchange"); tsym=o.get("trading_symbol") or o.get("tradingsymbol")
                break
        if not (exch and tsym):
            try:
                h=(client().order_history(str(oid)) or [{}])[-1]
                exch=exch or h.get("exchange"); tsym=tsym or h.get("tradingsymbol")
            except Exception as e:
                log(f"history fail {oid} {e}")
        ts=tick_for(exch,tsym) if (exch and tsym) else 0.05
        price=round(float(snap(price,ts)),2)
        trig=round(float(snap(trigger,ts)),2) if trigger is not None else None
        r=client().modify_order(variety="regular",order_id=oid,quantity=int(qty),price=price,trigger_price=trig,order_type=order_type)
        forget()
        import time
        h={}
        for _try in range(5):
            time.sleep(0.5)
            try:
                hist=client().order_history(str(oid)) or []
                h=hist[-1] if hist else {}
            except Exception as e:
                log(f"history fail {oid} {e}"); h={}
            st=(h.get("status") or "").upper().replace("-"," ")
            if st not in ("MODIFY PENDING","OPEN PENDING","PUT ORDER REQ RECEIVED","AMO REQ RECEIVED","VALIDATION PENDING"):
                break
        live=float(h.get("trigger_price") or 0)
        live_px=h.get("price")
        st=h.get("status") or ""
        msg=(h.get("status_message") or h.get("status_message_raw") or "") or ""
        st_u=st.upper()
        pending=st_u.replace("-"," ") in ("MODIFY PENDING","OPEN PENDING","PUT ORDER REQ RECEIVED","AMO REQ RECEIVED","VALIDATION PENDING")
        rejected=st_u in ("REJECTED","CANCELLED","CANCELED")
        moved=trig is None or abs(live-float(trig))<=max(float(ts),0.01)*0.51+1e-9
        if pending or rejected or (trig is not None and not moved):
            why=msg or ("still "+st if pending else ("trigger did not move" if not moved else st))
            log(f"modify reject {oid} {tsym or ''} wanted trig {trig} px {price} live trig {live} px {live_px} status {st}: {why}")
            return None
        log(f"modified {oid} {tsym or ''} trig {trig} px {price} live trig {live} px {live_px} status {st}")
        return r
    except Exception as e: log(f"modify fail {oid}: {e}"); return None
def cancel(oid,dry=False):
    if _blocked(f"cancel {oid}"): return None
    if dry: return "DRY"
    if not valid(): return None
    try:
        r=client().cancel_order(variety="regular",order_id=oid); forget(); return r
    except Exception as e: log(f"cancel fail {oid}: {e}"); return None
if __name__=="__main__":
    a=sys.argv[1:] or ["status"]
    if a[0]=="status": print(json.dumps({"valid":valid(),"why":why()}))
    elif a[0]=="map": print(json.dumps(from_upstox(a[1])))
    elif a[0]=="positions": print(json.dumps(positions(),indent=1))
    elif a[0]=="orders": print(json.dumps(orders(),indent=1,default=str))
