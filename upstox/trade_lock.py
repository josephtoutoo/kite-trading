"""Account-wide trade lock + pre-trade rules (3 Nov 2026).
Entry scripts MUST call check_order(order) before placing any order (and can_enter() still works).
Lock file: trade_lock.json {date, reason, loss, next_day_reduced, next_day_date}
CLI: python3 trade_lock.py check '<json order>'   (order keys: trading_symbol, instrument_token,
     transaction_type, order_type, product, quantity, price, trigger_price; optional "_now":"YYYY-MM-DD HH:MM" for tests)
Read-only: places NO orders."""
import json,os,re,sys,datetime,urllib.request,urllib.parse
D=os.path.dirname(os.path.abspath(__file__))
LF=os.path.join(D,"trade_lock.json")
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
THRESHOLD=30000
INDEX_OPT_MAX=3; INDEX_OPT_LOSS=2000; WINDOW_MIN=30; EQ_STOP_CUTOFF=datetime.time(15,10)
MTF_STOCK_CAP=0.10; MTF_TOTAL_CAP=0.50
INDEX_KEYS={"NIFTY":"NSE_INDEX|Nifty 50","BANKNIFTY":"NSE_INDEX|Nifty Bank","FINNIFTY":"NSE_INDEX|Nifty Fin Service",
 "MIDCPNIFTY":"NSE_INDEX|NIFTY MID SELECT","NIFTYNXT50":"NSE_INDEX|Nifty Next 50","SENSEX":"BSE_INDEX|SENSEX","BANKEX":"BSE_INDEX|BANKEX"}
# ---------------- lock (unchanged API) ----------------
STOCK_OPT_MAX_UND=3
def today(): return datetime.datetime.now(IST).date()
def next_trading_day(d):
    d=d+datetime.timedelta(days=1)
    while d.weekday()>=5: d+=datetime.timedelta(days=1)
    return d
def load(): return json.load(open(LF)) if os.path.exists(LF) else {}
def save(s): json.dump(s,open(LF,"w"),indent=1)
def lock(loss,reason,dry=False):
    d=today(); s=load()
    if s.get("date")==d.isoformat(): return s
    s={"date":d.isoformat(),"reason":reason,"loss":round(loss,2),"next_day_reduced":True,
       "next_day_date":next_trading_day(d).isoformat(),"next_day_entries_used":0}
    if not dry: save(s)
    return s
def can_enter(size=1):
    """-> (allowed, max_qty_factor, reason)"""
    s=load(); d=today().isoformat()
    if s.get("date")==d: return (False,0.0,f"LOCKED today: {s.get('reason')}")
    if s.get("next_day_reduced") and s.get("next_day_date")==d:
        if s.get("next_day_entries_used",0)>=1: return (False,0.0,"reduced day: 1 entry already used")
        return (True,0.5,"reduced day after loss lock: 1 entry at half size")
    return (True,1.0,"ok")
def record_entry():
    """Call after a BUY entry is actually placed."""
    s=load()
    if s.get("next_day_date")==today().isoformat():
        s["next_day_entries_used"]=s.get("next_day_entries_used",0)+1; save(s)
# ---------------- live data ----------------
_cache={}
def _get(path):
    if path in _cache: return _cache[path]
    t=json.load(open(os.path.join(D,"token.json")))["access_token"]
    r=urllib.request.Request("https://api.upstox.com"+path,headers={"Authorization":"Bearer "+t,"Accept":"application/json","User-Agent":"Mozilla/5.0"})
    _cache[path]=json.load(urllib.request.urlopen(r,timeout=20)); return _cache[path]
def _data(path):
    try: return _get(path).get("data") or []
    except Exception as e: return []
def sym_of(x): return (x.get("trading_symbol") or x.get("tradingsymbol") or "").upper()
def is_opt(s):
    # 4 Nov: a stock named ...CE (e.g. BAJFINANCE) is not an option - require underlying + expiry digits first
    return bool(re.match(r"^[A-Z&\-]+\d.*(CE|PE)$",(s or "").upper()))
def underlying(s):
    s=re.sub(r"-EQ$","",s.upper()); m=re.match(r"^([A-Z&\-]+?)(\d{2}[A-Z0-9]{3}|\d{2}N\d{2}|$)",s)
    m2=re.match(r"^[A-Z&\-]+",s); u=m2.group(0) if m2 else s
    return u
def is_index(s): return underlying(s) in INDEX_KEYS
def _ts(o):
    try: return datetime.datetime.strptime(o.get("order_timestamp") or o.get("exchange_timestamp"),"%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except Exception: return None
def _ltp(order,positions):
    k=order.get("instrument_token")
    for p in positions:
        if (p.get("instrument_token")==k or sym_of(p)==sym_of(order)) and p.get("last_price"): return float(p["last_price"])
    if k:
        d=_data("/v2/market-quote/ltp?instrument_key="+urllib.parse.quote(k,safe=""))
        if isinstance(d,dict):
            for v in d.values(): return float(v.get("last_price") or 0) or None
    return None
def lot_size(key,sym=""):
    """lot size from Upstox instrument master (cached daily file)."""
    import gzip
    exch="BSE" if (key or "").startswith("BSE") or sym.startswith("SENSEX") or sym.startswith("BANKEX") else "NSE"
    f=os.path.join(D,f"instruments_{exch}_{datetime.datetime.now(IST).date()}.json.gz")
    try:
        if not os.path.exists(f):
            urllib.request.urlretrieve(f"https://assets.upstox.com/market-quote/instruments/exchange/{exch}.json.gz",f)
        if f not in _cache: _cache[f]={x["instrument_key"]:int(x.get("lot_size") or 0) for x in json.load(gzip.open(f)) if x.get("segment","").endswith("_FO")}
        return _cache[f].get(key) or None
    except Exception: return None
# ---------------- rules ----------------
def kite_data():
    """Kite positions/orders (normalized) or ([],[]) when Kite token invalid - never raises."""
    if "_kite" in _cache: return _cache["_kite"]
    try:
        import broker_kite as bk
        _cache["_kite"]=(bk.positions(),bk.orders()) if bk.valid() else ([],[])
    except Exception: _cache["_kite"]=([],[])
    return _cache["_kite"]
def _kite_holdings():
    """Kite holdings normalized to positions (MTF/CNC) so R8/R6 see settled shares, not just day positions."""
    if "_kiteh" in _cache: return _cache["_kiteh"]
    try:
        import broker_kite as bk
        _cache["_kiteh"]=bk.holdings() if bk.valid() and hasattr(bk,"holdings") else []
    except Exception: _cache["_kiteh"]=[]
    return _cache["_kiteh"]
def _decisions(filled,now):
    """Index-option BUY decisions today across brokers: an Upstox+Kite pair (same index, same CE/PE, within 5 min,
    different brokers) counts as ONE decision."""
    ev=sorted([(_ts(o),underlying(sym_of(o)),sym_of(o)[-2:],o.get("broker","upstox")) for o in filled
        if o["transaction_type"]=="BUY" and is_opt(sym_of(o)) and is_index(sym_of(o)) and _ts(o) and _ts(o).date()==now.date()])
    dec=[]
    for t,u,sd,b in ev:
        m=next((d for d in dec if d["u"]==u and d["sd"]==sd and b not in d["b"] and abs((t-d["t"]).total_seconds())<=300),None)
        if m: m["b"].add(b)
        else: dec.append({"t":t,"u":u,"sd":sd,"b":{b}})
    return dec
def check_order(order,broker="upstox"):
    """-> (allowed: bool, reasons: list[str], adjusted_qty: int)
    broker: 'upstox' | 'kite'. Daily limits (R1 count, R2 loss cutoffs, R3, R7) use BOTH accounts;
    quantity-held checks (R6, R8, R11, R10) use the given broker's positions."""
    now=datetime.datetime.strptime(order["_now"],"%Y-%m-%d %H:%M").replace(tzinfo=IST) if order.get("_now") else datetime.datetime.now(IST)
    sym=sym_of(order); side=(order.get("transaction_type") or "").upper(); ot=(order.get("order_type") or "").upper()
    prod=(order.get("product") or "").upper(); qty=int(order.get("quantity") or 0); k=order.get("instrument_token")
    und=underlying(sym); opt=is_opt(sym); idx_opt=opt and is_index(sym)
    orders=_data("/v2/order/retrieve-all"); pos=_data("/v2/portfolio/short-term-positions")
    mtf=_data("/v3/portfolio/mtf-positions")
    funds=_data("/v2/user/get-funds-and-margin") or {}
    eq=funds.get("equity",{}) if isinstance(funds,dict) else {}
    capital=float(eq.get("available_margin") or 0)+float(eq.get("used_margin") or 0)
    kpos,kord=kite_data()
    filled=[o for o in orders+kord if o.get("status")=="complete" and (_ts(o) or now)<=now]
    upos=pos
    if broker=="kite":
        pos=kpos+_kite_holdings(); mtf=[]
    block=[]; flags=[]; adj=qty
    allpos=pos+[m for m in mtf if m.get("instrument_token") not in {p.get("instrument_token") for p in pos}]
    held=[p for p in allpos if p.get("instrument_token")==k or (sym and sym_of(p)==sym)]
    held_q=sum(int(p.get("quantity") or 0) for p in held)
    ltp=_ltp(order,allpos)
    if side=="BUY":
        # rule 2b: account day loss breaker / reduced day
        day_pnl=sum(float(p.get("pnl") if p.get("pnl") is not None else (p.get("unrealised") or 0)+(p.get("realised") or 0)) for p in upos+kpos)
        if day_pnl< -THRESHOLD: block.append(f"R2: combined (Upstox+Kite) day P&L Rs {day_pnl:,.0f} < -30,000 - no new trades today")
        ok,fac,why=can_enter(1)
        if not ok: block.append("R2: "+why)
        elif fac<1:
            adj=max(int(adj*fac),0); flags.append(f"R2: {why}; qty cut to {adj} (round to lot size)")
        if idx_opt:
            # rule 1
            n=len(_decisions(filled,now))
            if n>=INDEX_OPT_MAX and not order.get("_paired"): block.append(f"R1: {n} index option trade decisions already today across Upstox+Kite (max {INDEX_OPT_MAX})")
            # rule 2a
            bl=sum(float(p.get("realised") or 0) for p in upos+kpos if is_opt(sym_of(p)) and is_index(sym_of(p)))
            if bl< -INDEX_OPT_LOSS: block.append(f"R2: combined index-option booked loss Rs {bl:,.0f} < -2,000 - no more index option trades today")
            # rule 3
            other="PE" if sym.endswith("CE") else "CE"
            for o in filled:
                t=_ts(o); s2=sym_of(o)
                if o["transaction_type"]=="BUY" and underlying(s2)==und and s2.endswith(other) and t and timedelta_min(now,t)<=WINDOW_MIN:
                    block.append(f"R3: {und} {other} bought {t:%H:%M} (<30 min ago) - no opposite side"); break
            # rule 4
            try:
                sys.path.insert(0,D); import ind
                a=ind.analyze(INDEX_KEYS[und],"minutes",5,5)
                above=a["close"]>a["ema20"] and a["dir"]==1; below=a["close"]<a["ema20"] and a["dir"]==-1
                trend=f"{und} 5m close {a['close']} ema20 {a['ema20']} ST {'green' if a['dir']==1 else 'red'} @ {a['last_ts'][11:16]}"
                if sym.endswith("PE") and not below: block.append("R4: PUT needs 5m close < 20 EMA and red Supertrend ("+trend+")")
                if sym.endswith("CE") and not above: block.append("R4: CALL needs 5m close > 20 EMA and green Supertrend ("+trend+")")
            except Exception as e: block.append(f"R4: trend check failed ({e}) - not allowed without trend confirmation")
        # rule 6
        if held_q>0:
            avg=float(held[0].get("average_price") or held[0].get("buy_price") or held[0].get("day_buy_price") or 0)
            if ltp and avg and ltp<avg: block.append(f"R6: averaging down (LTP {ltp} < avg {avg:.2f})")
            if ltp and avg and ltp>avg*1.20: block.append(f"R6: add-on after >20% run (LTP {ltp} > avg*1.2 {avg*1.2:.2f})")
            if qty>held_q: adj=min(adj,held_q); flags.append(f"R6: add-on qty capped at original {held_q}")
        # rule 7
        for o in filled:
            t=_ts(o)
            if o["transaction_type"]=="SELL" and underlying(sym_of(o))==und and t and timedelta_min(now,t)<=WINDOW_MIN:
                block.append(f"R7: {und} exit filled {t:%H:%M} (<30 min ago) - cooldown"); break
        # rule 11: stock options - max 2 underlyings, max 1 lot per underlying (incl. holdings)
        if opt and not idx_opt and ((k or "").startswith(("NSE_FO","BSE_FO")) or not k):
            longs={}
            # 4 Nov: per-broker count (user: Kite has no stock options, so its slots are free)
            for p in pos:
                sp=sym_of(p); q=int(p.get("quantity") or 0)
                if q>0 and is_opt(sp) and not is_index(sp):
                    longs.setdefault(underlying(sp),[]).append(p)
            others=[u for u in longs if u!=und]
            if und not in longs and len(others)>=STOCK_OPT_MAX_UND:
                block.append(f"R11: already long options in {len(others)} stocks ({', '.join(sorted(others))}) - max {STOCK_OPT_MAX_UND}")
            lot=lot_size(k,sym) if k else None
            if not lot: block.append("R11: lot size not found for "+(k or sym)+" - cannot verify 1-lot cap")
            else:
                have=sum(int(p.get("quantity") or 0) for p in longs.get(und,[]))
                room=max(lot-have,0)
                if room<=0: block.append(f"R11: {und} already holds {have} qty (= {have/lot:g} lot, lot {lot}) - cap 1 lot per stock"); adj=0
                elif adj>room: adj=room; flags.append(f"R11: qty capped to {room} (1 lot {lot} minus held {have})")
        # rule 10
        px=float(order.get("price") or 0) or ltp or 0
        if prod=="MTF":
            mtf_total=sum(float(m.get("average_price") or 0)*int(m.get("quantity") or 0) for m in mtf)
            this=sum(float(m.get("average_price") or 0)*int(m.get("quantity") or 0) for m in mtf if m.get("instrument_token")==k or sym_of(m)==und)
            new=px*qty
            if this+new>capital*MTF_STOCK_CAP: block.append(f"R10: {und} MTF Rs {this+new:,.0f} > 10% cap Rs {capital*MTF_STOCK_CAP:,.0f}")
            if mtf_total+new>capital*MTF_TOTAL_CAP: block.append(f"R10: total MTF Rs {mtf_total+new:,.0f} > 50% cap Rs {capital*MTF_TOTAL_CAP:,.0f}")
        elif opt or sym.endswith("FUT"):
            mtf_total=sum(float(m.get("average_price") or 0)*int(m.get("quantity") or 0) for m in mtf)
            fno_lim=min(capital*0.5,capital-mtf_total)
            fno_used=sum(float(p.get("average_price") or p.get("buy_price") or 0)*int(p.get("quantity") or 0) for p in pos if int(p.get("quantity") or 0)>0 and (is_opt(sym_of(p)) or sym_of(p).endswith("FUT")))
            if fno_used+px*qty>fno_lim: flags.append(f"R10 WARNING: F&O deployed Rs {fno_used+px*qty:,.0f} > F&O limit Rs {max(fno_lim,0):,.0f} (50% minus MTF overflow)")
    # rule 5
    if opt and ot in("MARKET","SL-M"): block.append(f"R5: {ot} not allowed on options - use LIMIT/SL")
    if side=="SELL":
        # rule 8
        live=sum(int(p.get("quantity") or 0) for p in held if (p.get("product") or "").upper()==prod)
        live=max(live,0)
        if live<=0: block.append(f"R8: no live long {sym} in product {prod} - SELL would open a short"); adj=0
        elif qty>live: adj=live; flags.append(f"R8: qty {qty} > live {live} ({prod}) - adjusted to {live}")
        # rule 9
        if not opt and not sym.endswith("FUT") and ot in("SL","SL-M") and now.time()>EQ_STOP_CUTOFF:
            block.append(f"R9: equity/MTF stop after 15:10 IST ({now:%H:%M}) - closing session rejects it")
    allowed=not block and adj>0
    return allowed, block+flags, adj
def timedelta_min(a,b): return (a-b).total_seconds()/60
if __name__=="__main__":
    if len(sys.argv)>=3 and sys.argv[1]=="check":
        a,r,q=check_order(json.loads(sys.argv[2]))
        print(json.dumps({"allowed":a,"adjusted_qty":q,"reasons":r},indent=1))
    else: print(can_enter())
