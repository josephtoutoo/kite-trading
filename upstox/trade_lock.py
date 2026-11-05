"""Account-wide trade lock + pre-trade rules (3 Nov 2026).
Two-day autonomy override (4 Nov 2026 t339u): on 5 and 6 Nov only, equity/autonomy.py swaps a
handful of limits for the bot's own. Every rule reads its value through _lim(), so the override is
applied where the rule is EVALUATED, not by rewriting these constants at import time - a global
rewritten at import is invisible to the next reader and would silently outlive its expiry.
On any other date _lim() returns the constant below untouched, so the user's rules return by
themselves. The Rs 30,000 account day-loss breaker (R2b), the fail-closed reads (R0), R8/R9 and
the 5-name MTF slot cap have no override entry and cannot move.
Entry scripts MUST call check_order(order) before placing any order (and can_enter() still works).
Lock file: trade_lock.json {date, reason, loss, next_day_reduced, next_day_date}
CLI: python3 trade_lock.py check '<json order>'   (order keys: trading_symbol, instrument_token,
     transaction_type, order_type, product, quantity, price, trigger_price; optional "_now":"YYYY-MM-DD HH:MM" for tests)
Read-only: places NO orders."""
import json,os,re,sys,datetime,urllib.parse
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import api                     # pooled + timed transport (5 Nov 2026)
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
STOCK_OPT_LOTS=1        # R11: lots per stock-option underlying (the 2-lot cap is 5-6 Nov only)
# ---------------- two-day rule override (t339u) ----------------
# The module is loaded by path and /workspace/equity is NOT left on sys.path: this folder's own
# modules must keep resolving to this folder first, whatever order scripts import things in.
_autonomy=None
try:
    import importlib.util as _ilu
    _af=os.path.join(os.path.dirname(D),"equity","autonomy.py")
    _spec=_ilu.spec_from_file_location("autonomy",_af)
    _autonomy=_ilu.module_from_spec(_spec); _spec.loader.exec_module(_autonomy)
    sys.modules.setdefault("autonomy",_autonomy)   # so a later plain `import autonomy` agrees
except Exception as _e:      # no module -> the user's own rules, which is the safe default
    _autonomy=None
    print(f"trade_lock: autonomy module unavailable, user's rules apply on every day: {type(_e).__name__}: {_e}",file=sys.stderr)
def _lim(name,default,when):
    """The value a rule should use at decision time `when`: DEFAULT unless the two-day override
    is active on that date, in which case autonomy's own number. DEFAULT is always what the
    user's rules say, so an inactive override is indistinguishable from having no module."""
    if _autonomy is None or when is None: return default
    try: return _autonomy.effective(name,default,when)
    except Exception as e:
        print(f"trade_lock: override lookup failed for {name}, keeping the user's rule: {type(e).__name__}: {e}",file=sys.stderr)
        return default
def today(): return datetime.datetime.now(IST).date()
def next_trading_day(d):
    try:
        import market_calendar
        return market_calendar.next_trading_day(d)
    except Exception as e:
        print(f"trade_lock: market_calendar unavailable, next day is judged on weekdays only: {type(e).__name__}: {e}",file=sys.stderr)
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
    """GET api.upstox.com + path, cached for the life of this process (one decision per run)."""
    if path in _cache: return _cache[path]
    _cache[path]=api.get(path); return _cache[path]
class ReadFailed(RuntimeError):
    """A broker read this decision depends on failed. Never treat that as 'nothing there'
    (5 Nov 2026): an empty positions/orders list silently zeroes the day-P&L breaker, the
    index-decision count and the MTF caps, so a BUY would pass every check it should fail."""


def _data(path, strict=False):
    try:
        r=_get(path)
        if strict and (not isinstance(r,dict) or r.get("status")!="success" or r.get("data") is None):
            raise ReadFailed(f"{path.split('?')[0]} returned no usable data")
        return r.get("data") or []
    except Exception as e:
        print(f"trade_lock: read failed for {path.split('?')[0]}: {type(e).__name__}: {str(e)[:160]}",file=sys.stderr)
        if strict: raise
        return []
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
    exch="BSE" if (key or "").startswith("BSE") or sym.startswith("SENSEX") or sym.startswith("BANKEX") else "NSE"
    f=os.path.join(D,f"instruments_{exch}_{datetime.datetime.now(IST).date()}.json.gz")
    try:
        if not os.path.exists(f):
            _download(f,f"https://assets.upstox.com/market-quote/instruments/exchange/{exch}.json.gz")
        lm=_lotmap(f)
        return lm.get(key) or None
    except Exception as e:
        print(f"trade_lock: lot size lookup failed for {key or sym} ({exch}): {type(e).__name__}: {str(e)[:120]}",file=sys.stderr)
        return None


def _download(dest,url):
    """Fetch a master file to `dest` (60 s timeout) - the one raw download left in this module."""
    import urllib.request
    data=urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=60).read()
    open(dest,"wb").write(data)


def _expiry_today(und,when):
    """Does any option on `und` expire on `when`'s date? True / False / None (unknown).

    Read from the newest instrument master already on disk, like _lotmap - no download is
    triggered from inside a rule check, because a rule must not depend on a network read it
    did not ask for. None means the master could not be read: the caller applies the tighter
    branch, since this check only ever ADDS a restriction."""
    exch="BSE" if und in ("SENSEX","BANKEX") else "NSE"
    try:
        import glob
        files=sorted(glob.glob(os.path.join(D,f"instruments_{exch}_*.json.gz")))
        if not files: return None
        f=files[-1]
        d=when.date().isoformat()
        # the KEY carries the date: keyed on the file alone, an earlier day's answer would be
        # served for a later day and the 13:00 cutoff would fire on the wrong session.
        key=("expiry",f,d,und)
        hit=_cache.get(key)
        if hit is None:
            for x in api.gzip_json(f):
                if x.get("instrument_type") in ("CE","PE") and (x.get("underlying_symbol") or x.get("name"))==und and x.get("expiry"):
                    try:
                        if datetime.datetime.fromtimestamp(x["expiry"]/1000,IST).date().isoformat()==d:
                            hit=True; break
                    except Exception: pass
            hit=bool(hit)
            _cache[key]=hit
        return hit
    except Exception as e:
        print(f"trade_lock: expiry calendar unreadable for {und}: {type(e).__name__}: {str(e)[:120]}",file=sys.stderr)
        return None


def _lotmap(f):
    """instrument_key -> lot_size for that exchange's F&O rows; parsed once per process per file."""
    hit=_cache.get(("lotmap",f))
    if hit: return hit
    lm={x["instrument_key"]:int(x.get("lot_size") or 0) for x in api.gzip_json(f) if x.get("segment","").endswith("_FO")}
    _cache[("lotmap",f)]=lm
    return lm
# ---------------- rules ----------------
_kite_err=[]   # why the Kite book is unusable this run ([] = a clean read). A BUY is refused on any entry.
def kite_data():
    """Kite positions/orders (normalized) or ([],[]) when Kite token invalid - never raises.
    Every failure is recorded in _kite_err so a BUY fails closed on it instead of seeing an empty book."""
    if "_kite" in _cache: return _cache["_kite"]
    try:
        import broker_kite as bk
        if not bk.valid():
            _kite_err.append(f"Kite token invalid ({getattr(bk,'why',lambda:'')() or 'no session'})")
            _cache["_kite"]=([],[])
        else:
            _cache["_kite"]=(bk.positions(),bk.orders())
    except Exception as e:
        print(f"trade_lock: Kite positions/orders unavailable: {type(e).__name__}: {str(e)[:140]}",file=sys.stderr)
        _kite_err.append(f"Kite positions/orders unreadable: {type(e).__name__}: {str(e)[:120]}")
        _cache["_kite"]=([],[])
    return _cache["_kite"]
def _kite_holdings():
    """Kite holdings normalized to positions (MTF/CNC) so R8/R6 see settled shares, not just day positions."""
    if "_kiteh" in _cache: return _cache["_kiteh"]
    try:
        import broker_kite as bk
        _cache["_kiteh"]=bk.holdings() if bk.valid() and hasattr(bk,"holdings") else []
    except Exception as e:
        print(f"trade_lock: Kite holdings unavailable: {type(e).__name__}: {str(e)[:140]}",file=sys.stderr)
        _kite_err.append(f"Kite holdings unreadable: {type(e).__name__}: {str(e)[:120]}")
        _cache["_kiteh"]=[]
    return _cache["_kiteh"]
def _kite_mtf():
    """Kite MTF rows normalized for R10 (4 Nov: this used to be an empty list, so Kite's
    10%/50% MTF caps never fired). Uses holdings.mtf.quantity plus open MTF day positions."""
    if "_kitemtf" in _cache: return _cache["_kitemtf"]
    rows=[]
    try:
        import broker_kite as bk
        if bk.valid():
            seen={}
            for p in bk.positions():
                if p.get("product")=="MTF" and int(p.get("quantity") or 0)>0 and p.get("exchange") in ("NSE","BSE"):
                    seen[p["trading_symbol"]]=dict(p)
            for h in bk.holdings():
                m=h.get("mtf") or {}
                q=int(m.get("quantity") or 0)
                if q>0:
                    d=dict(h); d["quantity"]=q
                    d["average_price"]=float(m.get("average_price") or h.get("average_price") or 0)
                    d["product"]="MTF"; seen[h["trading_symbol"]]=d
            rows=list(seen.values())
    except Exception as e:
        print(f"trade_lock: Kite MTF rows unavailable (R10 reads empty): {type(e).__name__}: {str(e)[:140]}",file=sys.stderr)
        _kite_err.append(f"Kite MTF book unreadable: {type(e).__name__}: {str(e)[:120]}")
        rows=[]
    _cache["_kitemtf"]=rows
    return rows
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
    broker: 'upstox' | 'kite'. Daily index-option limits (R1 count, R2a loss cutoff, R3, R7) use BOTH
    accounts but ONLY the order's own underlying: NIFTY, SENSEX and BANKNIFTY are separate books (t329u).
    The account-wide day-loss breaker (R2b) stays account-wide. Quantity-held checks (R6, R8, R11, R10)
    use the given broker's positions."""
    now=datetime.datetime.strptime(order["_now"],"%Y-%m-%d %H:%M").replace(tzinfo=IST) if order.get("_now") else datetime.datetime.now(IST)
    sym=sym_of(order); side=(order.get("transaction_type") or "").upper(); ot=(order.get("order_type") or "").upper()
    prod=(order.get("product") or "").upper(); qty=int(order.get("quantity") or 0); k=order.get("instrument_token")
    und=underlying(sym); opt=is_opt(sym); idx_opt=opt and is_index(sym)
    # A BUY passes only on a complete picture of the book. If any read fails we cannot know the
    # day P&L, today's decisions or the MTF exposure, so the honest answer is "not allowed".
    # SELLs keep the old lenient behaviour: R8 already blocks an unprotected sell.
    strict = side=="BUY"
    try:
        orders=_data("/v2/order/retrieve-all",strict=strict); pos=_data("/v2/portfolio/short-term-positions",strict=strict)
        mtf=_data("/v3/portfolio/mtf-positions",strict=strict)
    except ReadFailed as e:
        return False,[f"R0: broker read failed ({e}) - a BUY cannot be cleared without the full book"],0
    funds=_data("/v2/user/get-funds-and-margin") or {}
    eq=funds.get("equity",{}) if isinstance(funds,dict) else {}
    capital=float(eq.get("available_margin") or 0)+float(eq.get("used_margin") or 0)
    if broker=="kite":
        try:
            import broker_kite as bk
            if bk.valid():
                m=bk.client().margins("equity")
                capital=float(m.get("net") or 0)+float(m.get("utilised",{}).get("debits") or 0)
        except Exception as e:
            print(f"trade_lock: Kite margins unavailable, using the Upstox capital figure: {type(e).__name__}: {str(e)[:140]}",file=sys.stderr)
    kpos,kord=kite_data()
    filled=[o for o in orders+kord if o.get("status")=="complete" and (_ts(o) or now)<=now]
    upos=pos
    umtf=mtf          # captured before a Kite book shadows `mtf` below
    if broker=="kite":
        pos=kpos+_kite_holdings(); mtf=_kite_mtf()
    block=[]; flags=[]; adj=qty
    if side=="BUY":
        # Every cap below is a fraction of capital, so a funds read that failed quietly sets capital
        # to 0 and the caps still "work" - but R2's day-loss breaker and the Kite half of it are then
        # blind. Refuse the BUY instead of clearing it on a partial picture.
        if capital<=0: block.append("R0: capital unreadable (funds call returned nothing) - a BUY cannot be sized")
        for e in _kite_err: block.append(f"R0: {e} - a BUY needs the Kite book readable")
    allpos=pos+[m for m in mtf if m.get("instrument_token") not in {p.get("instrument_token") for p in pos}]
    held=[p for p in allpos if p.get("instrument_token")==k or (sym and sym_of(p)==sym)]
    held_q=sum(int(p.get("quantity") or 0) for p in held)
    ltp=_ltp(order,allpos)
    if side=="BUY":
        # rule 2b: account day loss breaker / reduced day
        # (5 Nov) Upstox MTF equity lives in /v3/portfolio/mtf-positions, NOT in short-term-positions,
        # so its P&L was missing from this breaker: a six-name MTF book could lose money all day and
        # never trip the Rs 30,000 cutoff. Both brokers, all three books, now feed it.
        day_pnl=sum(float(p.get("pnl") if p.get("pnl") is not None else (p.get("unrealised") or 0)+(p.get("realised") or 0)) for p in upos+umtf+kpos)
        if day_pnl< -THRESHOLD: block.append(f"R2: combined (Upstox+Kite) day P&L Rs {day_pnl:,.0f} < -30,000 - no new trades today")
        ok,fac,why=can_enter(1)
        if not ok: block.append("R2: "+why)
        elif fac<1:
            adj=max(int(adj*fac),0); flags.append(f"R2: {why}; qty cut to {adj} (round to lot size)")
        if idx_opt:
            # t329u (4 Nov): NIFTY, SENSEX and BANKNIFTY are SEPARATE books. Every index-option rule below
            # counts only this order's own underlying - a NIFTY loss or a third NIFTY decision must never
            # block BANKNIFTY or SENSEX. (MCX crude is a separate script and never enters this path.)
            # rule 1
            # t339u (4 Nov): every limit below is read at decision time through _lim(), so the
            # two-day override moves it only on 5-6 Nov and the user's constant is back on 7 Nov.
            _max=_lim("INDEX_OPT_MAX",INDEX_OPT_MAX,now)
            _dec=[d for d in _decisions(filled,now) if d["u"]==und]
            n=len(_dec)
            if n>=_max and not order.get("_paired"): block.append(f"R1: {n} {und} option trade decisions already today across Upstox+Kite (max {_max} per index)")
            # t339u: two extra index checks, live ONLY while the override is on. Both can only
            # refuse a trade the count would have allowed, never allow one it refused.
            _gap=_lim("INDEX_OPT_GAP_MIN",None,now)
            if _gap and _dec:
                # The mirror leg of one decision must not count against its own partner: _decisions
                # already calls an Upstox+Kite pair of the same index and side within 5 min ONE
                # decision, so the gap is measured from the last decision that is not this order's
                # partner. Without this, a Kite leg retried a minute after its Upstox leg filled
                # would be refused by a rule that only exists to slow the bot down.
                _other="kite" if broker=="upstox" else "upstox"
                _prev=[d for d in _dec if not (d["b"]=={_other} and d["sd"]==sym[-2:] and timedelta_min(now,d["t"])<=5)]
                if _prev:
                    _last=max(d["t"] for d in _prev)
                    if timedelta_min(now,_last)<_gap:
                        block.append(f"R1: last {und} decision {_last:%H:%M} is {timedelta_min(now,_last):.0f} min ago (<{_gap} min) - too soon for another")
            _cut=_lim("INDEX_OPT_EXPIRY_CUTOFF",None,now)
            if _cut:
                try: _ch,_cm=str(_cut).split(":")
                except Exception: _ch,_cm="13","00"
                if now.time()>datetime.time(int(_ch),int(_cm)):
                    exp=_expiry_today(und,now)
                    if exp is not False:   # True, or unknown -> the tighter branch (this only restricts)
                        block.append(f"R1: {und} options expire today and it is {now:%H:%M} (>={_cut}) - no NEW index-option decision"
                                     + ("" if exp else " (expiry calendar unreadable, treating it as expiry day)"))
            # rule 2a
            _loss=_lim("INDEX_OPT_LOSS",INDEX_OPT_LOSS,now)
            bl=sum(float(p.get("realised") or 0) for p in upos+kpos
                   if is_opt(sym_of(p)) and is_index(sym_of(p)) and underlying(sym_of(p))==und)
            if bl< -_loss: block.append(f"R2: {und} option booked loss Rs {bl:,.0f} < -{_loss:,} - no more {und} option trades today")
            # rule 3
            _win=_lim("WINDOW_MIN",WINDOW_MIN,now)
            other="PE" if sym.endswith("CE") else "CE"
            for o in filled:
                t=_ts(o); s2=sym_of(o)
                if o["transaction_type"]=="BUY" and underlying(s2)==und and s2.endswith(other) and t and timedelta_min(now,t)<=_win:
                    block.append(f"R3: {und} {other} bought {t:%H:%M} (<{_win} min ago) - no opposite side"); break
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
        _win7=_lim("WINDOW_MIN",WINDOW_MIN,now)
        for o in filled:
            t=_ts(o)
            if o["transaction_type"]=="SELL" and underlying(sym_of(o))==und and t and timedelta_min(now,t)<=_win7:
                block.append(f"R7: {und} exit filled {t:%H:%M} (<{_win7} min ago) - cooldown"); break
        # rule 11: stock options - max 3 underlyings, max 1 lot per underlying (incl. holdings)
        # t339u: 4 x 2 lots on 5-6 Nov only (stock options are the one book with a positive
        # measured record), back to 3 x 1 on 7 Nov. Both values are read per decision.
        _und_max=_lim("STOCK_OPT_MAX_UND",STOCK_OPT_MAX_UND,now)
        _lot_max=_lim("STOCK_OPT_MAX_LOTS",STOCK_OPT_LOTS,now)
        if opt and not idx_opt and ((k or "").startswith(("NSE_FO","BSE_FO")) or not k):
            longs={}
            # 4 Nov: per-broker count (user: Kite has no stock options, so its slots are free)
            for p in pos:
                sp=sym_of(p); q=int(p.get("quantity") or 0)
                if q>0 and is_opt(sp) and not is_index(sp):
                    longs.setdefault(underlying(sp),[]).append(p)
            others=[u for u in longs if u!=und]
            if und not in longs and len(others)>=_und_max:
                block.append(f"R11: already long options in {len(others)} stocks ({', '.join(sorted(others))}) - max {_und_max}")
            lot=lot_size(k,sym) if k else None
            if not lot: block.append(f"R11: lot size not found for {k or sym} - cannot verify the {_lot_max}-lot cap")
            else:
                have=sum(int(p.get("quantity") or 0) for p in longs.get(und,[]))
                room=max(lot*_lot_max-have,0)
                if room<=0: block.append(f"R11: {und} already holds {have} qty (= {have/lot:g} lot, lot {lot}) - cap {_lot_max} lot{'s' if _lot_max!=1 else ''} per stock"); adj=0
                elif adj>room: adj=room; flags.append(f"R11: qty capped to {room} ({_lot_max} lot{'s' if _lot_max!=1 else ''} of {lot} minus held {have})")
        # rule 10
        px=float(order.get("price") or 0) or ltp or 0
        if prod=="MTF":
            # 4 Nov 2026 (t292u): R10 caps are measured on the MTF MARGIN the broker blocks, not on price x qty.
            # 5 Nov 2026 (t304u): only names THIS bot opened count toward the caps, and each holding is
            # converted to margin at its OWN rate. The single ratio taken from the new order used to be
            # applied to every holding, which over- or under-stated the book whenever the names differed
            # (a 4x-leverage stock and a 5x-leverage one do not share a margin rate).
            rows=mtf
            qty_by={}
            for m in rows:
                q0=int(m.get("quantity") or 0)
                if q0>0:
                    nm=sym_of(m).replace("-EQ","")
                    qty_by[nm]=qty_by.get(nm,0)+q0
            try:
                sys.path.insert(0,"/workspace/equity")
                import mtf_sizing as _ms
                mine=_ms.mine_upstox() if broker=="upstox" else _ms.mine_kite()
                q=_ms.margin_quote(broker,und,qty,px)
                if q.get("ok") and px and qty: ratio=float(q["margin"])/(px*qty)
                else:
                    ratio=1.0
                    block.append(f"R10: MTF margin quote failed ({q.get('why','no quote')}) - cannot size on margin")
                new_m=float(q["margin"]) if q.get("ok") else px*qty*ratio
                # margin already blocked by the same name (mine or the user's own: buying it makes it mine)
                held_here=[(nm,qq) for nm,qq in qty_by.items() if nm==und]
                avg_here=next((float(m.get("average_price") or 0) for m in rows
                               if sym_of(m).replace("-EQ","")==und and int(m.get("quantity") or 0)>0),0.0)
                this_m=0.0
                for nm,qq in held_here:
                    hq=_ms.margin_quote(broker,nm,qq,avg_here)
                    this_m+=float(hq["margin"]) if hq.get("ok") else qq*avg_here*ratio
                # everything else this bot owns, at each name's own margin
                total_m=0.0; unpriced=[]
                for nm,qq in qty_by.items():
                    if nm==und or nm not in mine: continue
                    av=next((float(m.get("average_price") or 0) for m in rows
                             if sym_of(m).replace("-EQ","")==nm and int(m.get("quantity") or 0)>0),0.0)
                    hq=_ms.margin_quote(broker,nm,qq,av)
                    if hq.get("ok"): total_m+=float(hq["margin"])
                    else: total_m+=qq*av*ratio; unpriced.append(nm)
                if unpriced: flags.append(f"R10: margin quote unavailable for {','.join(unpriced)} - counted at this order's rate")
            except Exception as e:
                ratio=1.0; new_m=px*qty; this_m=0.0; total_m=0.0
                block.append(f"R10: MTF margin sizing unavailable ({type(e).__name__}: {e})")
            # t339u: 10% -> 12% per stock on 5-6 Nov; the 50% total is NOT raised (the override
            # table states it at 0.50, and this line reads it, so the cap cannot drift by accident).
            _per=_lim("MTF_PER_STOCK",MTF_STOCK_CAP,now); _tot=_lim("MTF_TOTAL",MTF_TOTAL_CAP,now)
            if this_m+new_m>capital*_per: block.append(f"R10: {und} MTF margin Rs {this_m+new_m:,.0f} > {_per*100:g}% cap Rs {capital*_per:,.0f}")
            if total_m+new_m>capital*_tot: block.append(f"R10: total MTF margin (this bot's names) Rs {total_m+new_m:,.0f} > {_tot*100:g}% cap Rs {capital*_tot:,.0f}")
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
