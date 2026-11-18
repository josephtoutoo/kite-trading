"""Account-wide trade lock + pre-trade rules (3 Nov 2026).
Two-day autonomy override (4 Nov 2026 t339u): on 5 and 6 Nov only, equity/autonomy.py swaps a
handful of limits for the bot's own. Every rule reads its value through _lim(), so the override is
applied where the rule is EVALUATED, not by rewriting these constants at import time - a global
rewritten at import is invisible to the next reader and would silently outlive its expiry.
On any other date _lim() returns the constant below untouched, so the user's rules return by
themselves. The Rs 30,000 account day-loss breaker (R2b), the fail-closed reads (R0), R8/R9 and
the MTF name cap is 5 bot-opened names through 8 Nov 2026 and 5 actual positive names from 9 Nov (equity/mtf_policy.py). The 5-6 Nov override does not move it.
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
    # Historical box: trade_lock lives in /workspace/upstox and autonomy in /workspace/equity.
    # This repo: both live under the repo root, so equity/ is a child of D as well.
    _af=next(p for p in (os.path.join(os.path.dirname(D),"equity","autonomy.py"),
                         os.path.join(D,"equity","autonomy.py")) if os.path.exists(p))
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
# 16 Nov 2026 (user-approved 12:10 IST): Upstox trading symbols are spaced, e.g. "PAGEIND 31500 PE 23 NOV 26"
# and "PAYTM FUT 25 NOV 26". The compact Kite form (INDHOTEL26NOV890CE) keeps matching as before.
_SPACED_OPT=re.compile(r"^[A-Z0-9&\-]+ \d+(\.\d+)? (CE|PE) \d{1,2} [A-Z]{3} \d{2}$")
_SPACED_FUT=re.compile(r"^[A-Z0-9&\-]+ FUT \d{1,2} [A-Z]{3} \d{2}$")
def is_opt(s):
    # 4 Nov: a stock named ...CE (e.g. BAJFINANCE) is not an option - require underlying + expiry digits first
    t=(s or "").upper().strip()
    return bool(re.match(r"^[A-Z&\-]+\d.*(CE|PE)$",t) or _SPACED_OPT.match(t))
def opt_side(s):
    """'CE' / 'PE' for an option symbol in either format, '' otherwise."""
    t=(s or "").upper().strip(); m=_SPACED_OPT.match(t)
    if m: return m.group(2)
    return t[-2:] if is_opt(t) else ""
def is_fut(s):
    t=(s or "").upper().strip()
    return t.endswith("FUT") or bool(_SPACED_FUT.match(t))
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
        if ((k and p.get("instrument_token")==k) or (sym_of(order) and sym_of(p)==sym_of(order))) and p.get("last_price"): return float(p["last_price"])
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


# None: read the on-disk master. A list: that master, for a test. "missing": the master
# cannot be read. Production leaves this None. It is not a historical expiry calendar.
_expiry_rows=None
def _expiry_rows_of(und,when,rows):
    d=when.date().isoformat()
    for x in rows:
        if x.get("instrument_type") not in ("CE","PE"): continue
        if (x.get("underlying_symbol") or x.get("name"))!=und or not x.get("expiry"): continue
        try:
            if datetime.datetime.fromtimestamp(x["expiry"]/1000,IST).date().isoformat()==d: return True
        except Exception: continue
    return False
def _expiry_today(und,when,rows=None,master_dir=None):
    """Does any option on `und` expire on `when`'s date? True / False / None (unknown).

    Read from the newest instrument master already on disk, like _lotmap - no download is
    triggered from inside a rule check, because a rule must not depend on a network read it
    did not ask for. None means the master could not be read: the caller applies the tighter
    branch, since this check only ever ADDS a restriction. `rows` or `_expiry_rows` supplies
    a master without touching disk; that is a fixture, not the exchange's history."""
    if rows is None and _expiry_rows=="missing": return None
    if rows is None and isinstance(_expiry_rows,list): rows=_expiry_rows
    if rows is not None:
        try: return _expiry_rows_of(und,when,rows)
        except Exception as e:
            print(f"trade_lock: expiry calendar unreadable for {und}: {type(e).__name__}: {str(e)[:120]}",file=sys.stderr)
            return None
    exch="BSE" if und in ("SENSEX","BANKEX") else "NSE"
    try:
        import glob
        root=D if master_dir is None else master_dir
        files=sorted(glob.glob(os.path.join(root,f"instruments_{exch}_*.json.gz")))
        if not files: return None
        f=files[-1]
        d=when.date().isoformat()
        # the KEY carries the date: keyed on the file alone, an earlier day's answer would be
        # served for a later day and the 13:00 cutoff would fire on the wrong session.
        key=("expiry",f,d,und)
        hit=_cache.get(key)
        if hit is None:
            hit=_expiry_rows_of(und,when,api.gzip_json(f))
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
    """Kite MTF rows for R10. Positive nets only, after a sold day leg is reconciled
    against the holding. A legacy lot with no bot tag is still a row."""
    if "_kitemtf" in _cache: return _cache["_kitemtf"]
    rows=[]
    try:
        import broker_kite as bk
        if bk.valid():
            sys.path.insert(0, os.path.join(os.path.dirname(D), "equity"))
            # 12 Nov 2026: the deployed runtime keeps mtf_policy in <D>/equity, not ../equity; the
            # missing path made every BUY fail R0 "Kite MTF book unreadable". Both layouts are searched.
            if os.path.join(D, "equity") not in sys.path: sys.path.insert(0, os.path.join(D, "equity"))
            import mtf_policy as _pol
            rec=_pol.reconcile_book(_pol.rows_from_kite(bk.positions(), bk.holdings()))
            if any(v.get("cost") is None for v in rec.values()):
                _kite_err.append("Kite MTF book has a positive name with no price")
            for sym, row in rec.items():
                rows.append({"trading_symbol":sym,"quantity":row["qty"],"average_price":row["price"] or 0,
                             "product":"MTF"})
    except Exception as e:
        print(f"trade_lock: Kite MTF rows unavailable (R10 reads empty): {type(e).__name__}: {str(e)[:140]}",file=sys.stderr)
        _kite_err.append(f"Kite MTF book unreadable: {type(e).__name__}: {str(e)[:120]}")
        rows=[]
    _cache["_kitemtf"]=rows
    return rows
def _decisions(filled,now):
    """Index-option BUY decisions today across brokers: an Upstox+Kite pair (same index, same CE/PE, within 5 min,
    different brokers) counts as ONE decision."""
    ev=sorted([(_ts(o),underlying(sym_of(o)),opt_side(sym_of(o)),o.get("broker","upstox")) for o in filled
        if o["transaction_type"]=="BUY" and is_opt(sym_of(o)) and is_index(sym_of(o)) and _ts(o) and _ts(o).date()==now.date()])
    dec=[]
    for t,u,sd,b in ev:
        m=next((d for d in dec if d["u"]==u and d["sd"]==sd and b not in d["b"] and abs((t-d["t"]).total_seconds())<=300),None)
        if m: m["b"].add(b)
        else: dec.append({"t":t,"u":u,"sd":sd,"b":{b}})
    return dec
# ---------------- 16 Nov 2026 performance rules (t669u): contract metadata ----------------
def _contract_meta(k,sym,broker="upstox"):
    """{'lot': int|None, 'expiry': date|None} for an option contract, from the masters already on
    disk (Upstox instruments_*.json.gz by key, Kite kite_instruments_*.csv by symbol). No download.
    Tests stub this function."""
    key=("meta",k,sym)
    if key in _cache: return _cache[key]
    out={"lot":None,"expiry":None}
    try:
        if (k or "").startswith("KITE|"):
            _k,ex,ts=k.split("|",2)
            import broker_kite as _bkm
            row=_bkm._index(ex)[1].get(ts)
            if row:
                out["lot"]=int(float(row.get("lot_size") or 0)) or None
                try: out["expiry"]=datetime.date.fromisoformat(str(row.get("expiry"))[:10])
                except Exception: out["expiry"]=None
        elif k:
            import glob
            exch="BSE" if k.startswith("BSE") else ("MCX" if k.startswith("MCX") else "NSE")
            files=sorted(glob.glob(os.path.join(D,f"instruments_{exch}_*.json.gz")))
            if files:
                mk=("expmap",files[-1])
                if mk not in _cache:
                    _cache[mk]={x["instrument_key"]:(int(x.get("lot_size") or 0),x.get("expiry")) for x in api.gzip_json(files[-1])
                                if str(x.get("segment","")).endswith("_FO")}
                hit=_cache[mk].get(k)
                if hit:
                    out["lot"]=hit[0] or None
                    if hit[1]: out["expiry"]=datetime.datetime.fromtimestamp(hit[1]/1000,IST).date()
    except Exception as e:
        print(f"trade_lock: contract master unreadable for {k or sym}: {type(e).__name__}: {str(e)[:120]}",file=sys.stderr)
    _cache[key]=out
    return out
def _perf_rules():
    try:
        if D not in sys.path: sys.path.insert(0,D)
        import perf_rules as _pr
        return _pr
    except Exception as e:
        print(f"trade_lock: perf_rules unavailable: {type(e).__name__}",file=sys.stderr)
        return None
def _r10_capital_msg(msg):
    """True when an R10 reason is a verified-capital or percentage-of-capital cap (not name/eligibility)."""
    m=(msg or "").lower()
    return any(s in m for s in (
        "verified capital denominator","margin capacity, not a verified","verified-capital",
        "shared 25% pool","per-stock margin would exceed","total mtf margin is already over",
        "stricter 10% cap","mtf margin rs ","% cap rs "))
def held_rows(pos,mtf,k,sym,broker="upstox"):
    """-> (allpos, held). 17 Nov 2026: Kite MTF rows (_kite_mtf) carry no instrument_token and a
    Kite MTF order from longterm_daily_st had none either, so None==None matched every tokenless
    row: IGIL/PVRINOX/LTTS buys were "R6: add-on qty capped at original 6" (SRF's 6 shares).
    Match on a real token only. On Kite, an MTF row whose symbol is already in pos (holdings or
    day positions carry the same shares) is not added again; counting both doubled held_q."""
    _ptoks={p.get("instrument_token") for p in pos if p.get("instrument_token")}
    _psyms={sym_of(p) for p in pos if sym_of(p)} if broker=="kite" else set()
    allpos=pos+[m for m in mtf if not ((m.get("instrument_token") and m.get("instrument_token") in _ptoks)
                                       or (sym_of(m) and sym_of(m) in _psyms))]
    held=[p for p in allpos if (k and p.get("instrument_token")==k) or (sym and sym_of(p)==sym)]
    return allpos,held
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
    _kite_margin=None
    if broker=="kite":
        try:
            import broker_kite as bk
            if bk.valid():
                _kite_margin=bk.client().margins("equity")
                capital=float(_kite_margin.get("net") or 0)+float(_kite_margin.get("utilised",{}).get("debits") or 0)
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
        _cap_active=False; _cap_skip=False; _mtf_cap_skip=False; _mtf_r10_on=True
        try:
            sys.path.insert(0, os.path.join(D, "equity"))
            import capital_policy as _cp
            _cap_active=_cp.active(now)
        except Exception as _ce:
            _cp=None
            if now.date()>=datetime.date(2026,11,9):
                block.append(f"R10: capital policy missing ({type(_ce).__name__}) - buy refused")
        _gate_on=True
        try:
            if D not in sys.path: sys.path.insert(0, D)
            import full_auto_policy as _fap
            _gate_on=_fap.flag("capital_gate")
            _mtf_r10_on=_fap.flag("r10_mtf_capital")
        except Exception as _fe:
            print(f"trade_lock: capital_gate/r10_mtf_capital flag unreadable, R10 stays on: {type(_fe).__name__}",file=sys.stderr)
        # 17 Nov 2026 11:40 IST (t713u/t714u): r10_mtf_capital off skips R10 verified-capital
        # and percentage-of-capital caps for equity MTF stock buys only. Futures and options
        # are unchanged. No capital figure is invented. R0, R1-R9, R11, R13, the daily loss
        # cap, the Rs 50k per-stock MTF sleeve, the MTF name cap and the broker's own margin
        # checks still apply.
        if (not _mtf_r10_on) and prod=="MTF" and not opt and not is_fut(sym):
            _mtf_cap_skip=True
        if _cap_active and _cp is not None:
            _vo=_cp.verified_override(broker) if hasattr(_cp,"verified_override") else None
            _basis=_cp.basis_from_funds(_vo or (_kite_margin if broker=="kite" else eq))
            if not _basis["ok"]:
                # 12 Nov 2026 10:38 IST (t598u "Don't block"): with full_auto_flags capital_gate=false an
                # OPTION buy is not refused for a missing verified capital figure. No capital is invented:
                # the percentage buckets are skipped and size stays the caller's lot rule (1 lot, 2 on
                # ST/EMA, 3 on the index's own expiry day, by max). R0 funds, R1-R9, R11 and the daily
                # loss cap still apply. BEEs, futures and MCX keep the fail-closed refusal.
                # 17 Nov 2026 (t713u/t714u): equity MTF stock buys also skip this fail-closed
                # refusal when r10_mtf_capital is off.
                if not _gate_on and opt and prod!="MTF":
                    _cap_skip=True
                    flags.append(f"R10 skipped: capital_gate off ({_basis['reason']}); option sized by lots, not buckets")
                    print("trade_lock: R10 skipped: capital_gate off",file=sys.stderr)
                elif _mtf_cap_skip:
                    flags.append(f"R10 skipped: r10_mtf_capital off ({_basis['reason']}); MTF stock not sized on a verified-capital denominator")
                    print("trade_lock: R10 skipped: r10_mtf_capital off",file=sys.stderr)
                else:
                    block.append(f"R10: {_basis['reason']} - buy refused")
                    capital=0
            else:
                capital=_basis["capital"]
    allpos,held=held_rows(pos,mtf,k,sym,broker)
    held_q=sum(int(p.get("quantity") or 0) for p in held)
    ltp=_ltp(order,allpos)
    # R15 (18 Nov 2026 15:25 IST, t739u "Next one week don't trade in upstock"): while
    # state/upstox_entry_block.json is unexpired (until 25 Nov 2026 23:59 IST), an Upstox BUY is
    # refused unless it covers an existing short (held qty < 0). SELL exits/stops are untouched,
    # and R8 already refuses a SELL that would open a short. Kite is never affected.
    if broker=="upstox" and side=="BUY":
        try:
            import upstox_entry_block as _ueb
            _ebs=_ueb.status(now)
            if _ebs["active"] and held_q>=0: block.append(_ueb.message(_ebs))
        except Exception as _ebe:
            if os.path.exists(os.path.join(D,"state","upstox_entry_block.json")):
                block.append(f"R15: entry-block check failed ({type(_ebe).__name__}) - Upstox buy refused")
    if side=="BUY":
        # rule 2b: account day loss breaker / reduced day
        # (5 Nov) Upstox MTF equity lives in /v3/portfolio/mtf-positions, NOT in short-term-positions,
        # so its P&L was missing from this breaker: a six-name MTF book could lose money all day and
        # never trip the Rs 30,000 cutoff. Both brokers, all three books, now feed it.
        # 16 Nov 2026 (t669u): option/future rows are valued for TODAY only - carried quantity from
        # the previous close, partly closed rows booked FIFO from today's fills (daily_loss_cap.row_pnl).
        # MTF / equity rows keep the broker's own figure.
        try:
            import daily_loss_cap as _dlc
            _uf=_dlc.fills_from_orders(orders,"upstox"); _kf=_dlc.fills_from_orders(kord,"kite")
            def _dp(p,b,book):
                if is_opt(sym_of(p)) or is_fut(sym_of(p)):
                    return _dlc.row_pnl(p,b,_dlc._fills_for(p,b,book))["day_pnl"]
                return float(p.get("pnl") if p.get("pnl") is not None else (p.get("unrealised") or 0)+(p.get("realised") or 0))
            day_pnl=sum(_dp(p,"upstox",_uf) for p in upos)+sum(_dp(p,"upstox",None) for p in umtf)+sum(_dp(p,"kite",_kf) for p in kpos)
        except Exception as _de:
            print(f"trade_lock: today-only P&L failed, broker figure used: {type(_de).__name__}: {_de}",file=sys.stderr)
            day_pnl=sum(float(p.get("pnl") if p.get("pnl") is not None else (p.get("unrealised") or 0)+(p.get("realised") or 0)) for p in upos+umtf+kpos)
        if day_pnl< -THRESHOLD: block.append(f"R2: combined (Upstox+Kite) day P&L Rs {day_pnl:,.0f} < -{THRESHOLD:,} - no new trades today")
        ok,fac,why=can_enter(1)
        if not ok: block.append("R2: "+why)
        elif fac<1:
            adj=max(int(adj*fac),0); flags.append(f"R2: {why}; qty cut to {adj} (round to lot size)")
        # 17 Nov 2026 12:27 IST (t716u "Don't block today"): when index_r1_r7_off is on and
        # today's IST date is on or before index_r1_r7_off_until, skip R1 (daily decision cap,
        # including the expired 5-6 Nov gap/expiry-cutoff extras) and R7 (post-exit cooldown)
        # for index option entries only. R12, R2a, R3, R4, R4b, R5, R6, R11, R13, the loss cap
        # and the 09:30-15:25 window stay. Defaults off; expires by date even if the flag is left on.
        _skip_r1_r7=False
        if idx_opt:
            try:
                if D not in sys.path: sys.path.insert(0, D)
                import full_auto_policy as _fap17
                _skip_r1_r7=bool(_fap17.index_r1_r7_off(now))
                _r17_until=_fap17.index_r1_r7_until()
            except Exception as _se:
                _skip_r1_r7=False; _r17_until=None
                print(f"trade_lock: index_r1_r7_off unreadable, R1/R7 stay on: {type(_se).__name__}",file=sys.stderr)
            if _skip_r1_r7:
                _until_s=_r17_until.isoformat() if _r17_until else "today"
                flags.append(f"R1/R7 skipped: index_r1_r7_off until {_until_s}; index option decision cap and cooldown off today only")
                print("trade_lock: R1/R7 skipped: index_r1_r7_off",file=sys.stderr)
            # t329u (4 Nov): NIFTY, SENSEX and BANKNIFTY are SEPARATE books. Every index-option rule below
            # counts only this order's own underlying - a NIFTY loss or a third NIFTY decision must never
            # block BANKNIFTY or SENSEX. (MCX crude is a separate script and never enters this path.)
            # rule 1
            # t339u (4 Nov): every limit below is read at decision time through _lim(), so the
            # two-day override moves it only on 5-6 Nov and the user's constant is back on 7 Nov.
            if not _skip_r1_r7:
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
                    _prev=[d for d in _dec if not (d["b"]=={_other} and d["sd"]==opt_side(sym) and timedelta_min(now,d["t"])<=5)]
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
            # 16 Nov 2026 (t669u): booked today, FIFO from today's fills; Kite's realised=0 on a partly
            # closed row is no longer read as nothing booked.
            try:
                import daily_loss_cap as _dlc
                _uf2=_dlc.fills_from_orders(orders,"upstox"); _kf2=_dlc.fills_from_orders(kord,"kite")
                bl=sum(_dlc.row_pnl(p,b,_dlc._fills_for(p,b,bk_))["realised"] for b,bk_,rows_ in (("upstox",_uf2,upos),("kite",_kf2,kpos)) for p in rows_
                       if is_opt(sym_of(p)) and is_index(sym_of(p)) and underlying(sym_of(p))==und)
            except Exception as _be:
                print(f"trade_lock: R2a FIFO tally failed, broker realised used: {type(_be).__name__}",file=sys.stderr)
                bl=sum(float(p.get("realised") or 0) for p in upos+kpos
                       if is_opt(sym_of(p)) and is_index(sym_of(p)) and underlying(sym_of(p))==und)
            if bl< -_loss: block.append(f"R2: {und} option booked loss Rs {bl:,.0f} < -{_loss:,} - no more {und} option trades today")
            # rule 3
            _win=_lim("WINDOW_MIN",WINDOW_MIN,now)
            other="PE" if opt_side(sym)=="CE" else "CE"
            for o in filled:
                t=_ts(o); s2=sym_of(o)
                if o["transaction_type"]=="BUY" and underlying(s2)==und and opt_side(s2)==other and t and timedelta_min(now,t)<=_win:
                    block.append(f"R3: {und} {other} bought {t:%H:%M} (<{_win} min ago) - no opposite side"); break
            # rule 4. Spot index, unless this process was started with INDEX_ENTRY_SOURCE=futures.
            # Then the order must carry the futures key the entry already chose. A missing or spot
            # key is a block: this rule does not read the cash index as a substitute.
            try:
                sys.path.insert(0,D); import ind, index_futures as _ifut
                _src=_ifut.entry_source()
                _key,_why=_ifut.signal_key_for_gate(order, und, INDEX_KEYS, _src)
                if not _key:
                    block.append("R4: "+_why)
                else:
                    a=ind.analyze(_key,"minutes",5,5)
                    above=a["close"]>a["ema20"] and a["dir"]==1; below=a["close"]<a["ema20"] and a["dir"]==-1
                    trend=f"{und} 5m {_src} {_key} close {a['close']} ema20 {a['ema20']} ST {'green' if a['dir']==1 else 'red'} @ {a['last_ts'][11:16]}"
                    if opt_side(sym)=="PE" and not below: block.append("R4: PUT needs 5m close < 20 EMA and red Supertrend ("+trend+")")
                    if opt_side(sym)=="CE" and not above: block.append("R4: CALL needs 5m close > 20 EMA and green Supertrend ("+trend+")")
            except Exception as e: block.append(f"R4: trend check failed ({e}) - not allowed without trend confirmation")
        # rule 6
        if held_q>0:
            avg=float(held[0].get("average_price") or held[0].get("buy_price") or held[0].get("day_buy_price") or 0)
            if ltp and avg and ltp<avg: block.append(f"R6: averaging down (LTP {ltp} < avg {avg:.2f})")
            if ltp and avg and ltp>avg*1.20: block.append(f"R6: add-on after >20% run (LTP {ltp} > avg*1.2 {avg*1.2:.2f})")
            if qty>held_q: adj=min(adj,held_q); flags.append(f"R6: add-on qty capped at original {held_q}")
        # rule 7
        if not (idx_opt and _skip_r1_r7):
            _win7=_lim("WINDOW_MIN",WINDOW_MIN,now)
            for o in filled:
                t=_ts(o)
                if o["transaction_type"]=="SELL" and underlying(sym_of(o))==und and t and timedelta_min(now,t)<=_win7:
                    block.append(f"R7: {und} exit filled {t:%H:%M} (<{_win7} min ago) - cooldown"); break
        # ---- 16 Nov 2026 performance rules (user t669u "Improve code changes"), every bot option BUY ----
        # R4b 15m trend filter, R12 re-entry block, R13 lot cap, R14 index expiry >1 trading day.
        # MCX (crude, manage-only) keeps its own 1-lot crude_auto rule and is not covered here.
        _mcx=(k or "").startswith(("MCX","KITE|MCX")) or und=="CRUDEOIL"
        # Effective from 16 Nov 2026; a dated test fixture ("_now") before that keeps the old rule set.
        if opt and not _mcx and now.date()>=datetime.date(2026,11,16):
            _pr=_perf_rules()
            if _pr is None:
                block.append("R12: perf_rules module missing - option buy refused")
            else:
                _pblock=[]
                try:
                    sys.path.insert(0,D); import ind as _ind
                    _t15=_pr.trend15_check(und,opt_side(sym),INDEX_KEYS,_ind.analyze)
                except Exception as _te:
                    _t15={"ok":False,"why":f"15m trend check failed ({type(_te).__name__})"}
                if not _t15["ok"]: _pblock.append("R4b: "+_t15["why"])
                try:
                    import broker_kite as _bkr
                    _tu=_bkr.to_upstox
                except Exception:
                    _tu=None
                _cid=_pr.contract_id(k,sym,_tu)
                _rok,_rwhy=_pr.reentry_check(_cid,und,now,list(orders)+list(kord),underlying,sym_of,_tu)
                if not _rok: _pblock.append(_rwhy)
                _meta=_contract_meta(k,sym,broker)
                _lot=_meta.get("lot")
                if not _lot:
                    _pblock.append(f"R13: lot size unknown for {k or sym} - cannot verify the lot cap")
                else:
                    _tot=(max(held_q,0)+qty)/_lot
                    if idx_opt:
                        _xd=_expiry_today(und,now) is True
                        _allow=_pr.allowed_index_lots(_xd, adds_taken=held_q>0)
                        if held_q<=0 and order.get("_rule_lots") is not None:
                            try: _allow=min(_allow,int(order["_rule_lots"]))
                            except (TypeError,ValueError): pass
                        _rule="max 3 lots incl. the one add" if held_q>0 else ("expiry-day max 3 lots" if _xd else "1 lot normal / 2 strong trend")
                    else:
                        # the user's 1 lot; only the dated 5-6 Nov override (_lim) ever said 2
                        _allow=int(_lim("STOCK_OPT_MAX_LOTS",_pr.STOCK_OPT_LOTS,now)); _rule=f"stock options {_allow} lot per broker"
                    if _tot>_allow+1e-9:
                        _pblock.append(f"R13: {sym} would hold {_tot:g} lots (held {held_q} + {qty}, lot {_lot}) > allowed {_allow} ({_rule})")
                if idx_opt:
                    _exp=_meta.get("expiry")
                    if _exp is None:
                        _pblock.append(f"R14: expiry unknown for {k or sym} - index buy refused")
                    elif _pr.expiry_too_near(_exp,now.date(),next_trading_day):
                        _pblock.append(f"R14: {sym} expires {_exp} (within 1 trading day) - bot index buys use a later expiry")
                if _pblock:
                    block.extend(_pblock)
                    if not order.get("_now"):   # "_now" = an offline test fixture; keep it out of the live log
                        _pr.log_refusal({"broker":broker,"symbol":sym,"side":opt_side(sym),"qty":qty,"reasons":_pblock,"source":order.get("tag") or order.get("_source")})
        # rule 11: stock options, before the order is sent. t460u: three distinct
        # underlyings per broker, pending and reserved names included. The old
        # 4-underlying figure is not applied. Lots stay 1, or 2 only on the
        # dated override, and are never raised above that. A new underlying
        # needs an explicit initial-entry flag or a verified full closure.
        # Count under 3 is not a reason to open one. Technical, results, and
        # manual buys use the same check.
        _und_max=_lim("STOCK_OPT_MAX_UND",STOCK_OPT_MAX_UND,now)
        _lot_max=_lim("STOCK_OPT_MAX_LOTS",STOCK_OPT_LOTS,now)
        if opt and not idx_opt and ((k or "").startswith(("NSE_FO","BSE_FO")) or not k):
            _sp=None
            try:
                sys.path.insert(0, os.path.join(D, "equity"))
                import stockopt_policy as _sp
            except Exception as _se:
                block.append(f"R11: stock-option policy missing ({type(_se).__name__}) - buy refused")
            if _sp is not None:
                _und_max=min(int(_und_max), _sp.MAX_UNDERLYINGS)
                _lot_max=min(int(_lot_max), _sp.LOT_CEILING)
                _this_orders=orders if broker=="upstox" else kord
                _view=_sp.book_view(pos, _this_orders)
                _other=_sp.book_view(kpos, kord) if broker=="upstox" else _sp.book_view(upos, orders)
                if broker=="upstox" and _kite_err:
                    _other=None
                if _view is None:
                    block.append("R11: this broker's stock-option book is unreadable - buy refused")
                else:
                    _held_syms=sorted({sym_of(p) for p in pos if int(p.get("quantity") or 0)>0
                                       and _sp.is_stock_option(sym_of(p)) and _sp.underlying(sym_of(p))==und})
                    _new=und not in _view["held"] and und not in _view["pending"] and und not in _view["uncertain"]
                    _names=sorted(_view["names"])
                    if _new and _view["count"]>=_und_max:
                        block.append(f"R11: already reserved options in {_view['count']} stocks ({', '.join(_names)}) - max {_und_max} including pending")
                    elif _new and _other is None:
                        block.append("R11: other broker book unreadable - new stock-option name refused")
                    elif _new and _other is not None and und not in _other["names"] and _other["count"]>=_und_max:
                        block.append(f"R11: other broker has no vacancy ({_other['count']} of {_und_max}, pending included) - mirrored entry needs both")
                    elif _new:
                        _pend=len(_view["pending"])
                        if _other is not None:
                            _pend=max(_pend, len(_other["pending"]))
                        _admit=_sp.admit_new(_view["count"], None if _other is None else _other["count"],
                                             order.get("_replacement") if isinstance(order.get("_replacement"), dict) else None,
                                             order.get("_initial_entry") is True, _pend)
                        if not _admit["ok"]:
                            block.append("R11: "+_admit["reason"])
                    elif len(_held_syms)!=1 or _held_syms[0]!=sym:
                        block.append("R11: add only the single open contract; a different strike or expiry is not a substitute")
                    elif int(_view["pending_qty"].get(und) or 0)>0 or und in _view["uncertain"]:
                        block.append(f"R11: an add in {und} is already pending")
                    else:
                        _guard=_sp.guard_add(order.get("_add_inputs"), sym, broker,
                                             _this_orders, [] if _other is None else (kord if broker=="upstox" else orders))
                        if not _guard["ok"]:
                            block.append("R11: "+_guard["reasons"][0])
                    lot=lot_size(k,sym) if k else None
                    if not lot: block.append(f"R11: lot size not found for {k or sym} - cannot verify the {_lot_max}-lot cap")
                    else:
                        have=int(_view["held_qty"].get(und) or 0)+int(_view["pending_qty"].get(und) or 0)
                        if und in _view["uncertain"]:
                            block.append(f"R11: {und} has a stock-option buy of unknown size - not adding")
                            adj=0
                        else:
                            room=max(lot*_lot_max-have,0)
                            if room<=0: block.append(f"R11: {und} already holds {have} qty (= {have/lot:g} lot, lot {lot}) - cap {_lot_max} lot{'s' if _lot_max!=1 else ''} per stock"); adj=0
                            elif adj>room: adj=room; flags.append(f"R11: qty capped to {room} ({_lot_max} lot{'s' if _lot_max!=1 else ''} of {lot} minus held {have})")
        # rule 10
        px=float(order.get("price") or 0) or ltp or 0
        if prod=="MTF":
            # Monday 9 Nov 2026: 5 actual names, daily qualification, one shared 25% MTF pool.
            # The 10% per-stock cap stays. A missing policy module refuses the buy.
            _mon=False
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(D), "equity"))
                import mtf_policy as _mp
                _mon=_mp.policy_active(now) or not _mp.date_readable(now)
            except Exception as _pe:
                _mp=None
                if now.date()>=datetime.date(2026,11,9):
                    block.append(f"R10: Monday policy module missing ({type(_pe).__name__}) - no MTF buy")
            if _mon and _mp is not None:
                _rows=[]
                for m in mtf:
                    q0=int(m.get("quantity") or 0)
                    if q0==0 and not m.get("day_sell_quantity"): continue
                    _rows.append(_mp.row_from_upstox(m) if "trading_symbol" in m or "tradingsymbol" in m else m)
                if broker=="kite":
                    _rec=_mp.reconcile_book(_rows) if _rows else {}
                else:
                    _rec=_mp.reconcile_book([_mp.row_from_upstox(m) for m in mtf])
                _pos={}
                for _s,_r in _rec.items():
                    if _r.get("cost") is None:
                        block.append(f"R10: {_s} has no average price - MTF buy refused")
                    else:
                        _pos[_s]={"qty":_r["qty"],"cost":_r["cost"]}
                _pend=_mp.pending_buys(orders if broker=="upstox" else kord, _pos)
                _view=_mp.rooms(capital, _pos, _pend, None, 1.0, now, broker=broker,
                                unpriced=[s for s,r in _rec.items() if r.get("cost") is None])
                try:
                    sys.path.insert(0,"/workspace/equity")
                    import mtf_sizing as _ms
                    _q=_ms.margin_quote(broker, und, qty, px) if px and qty else {"ok":False,"why":"no price"}
                except Exception as _qe:
                    _q={"ok":False,"why":str(_qe)}
                if not _q.get("ok"):
                    block.append(f"R10: MTF margin quote failed ({_q.get('why','no quote')}) - cannot size on margin")
                    _mnew=px*qty if px else 0
                else:
                    _mnew=float(_q["margin"])
                _already=float((_pos.get(und) or {}).get("cost") or 0)
                if _q.get("ok") and px and qty and _already:
                    _already=_already*float(_q["margin"])/(px*qty)
                if _q.get("ok") and px and qty:
                    _view=_mp.rooms(capital, _pos, _pend, None, float(_q["margin"])/(px*qty), now, broker=broker,
                                    unpriced=[s for s,r in _rec.items() if r.get("cost") is None])
                # t697u/t698u: tag longterm_dst is a separate sleeve (max 3 names, Rs 50k
                # margin per name). It is exempt from the Monday 5-name / 10% / 25% caps
                # only. rooms() already excludes sleeve holdings from those Monday budgets
                # so a regular MTF buy is not crowded out by this sleeve.
                _tag=(order.get("tag") or "")
                if _tag == getattr(_mp, "LONGTERM_DST_TAG", "longterm_dst"):
                    if not _q.get("ok"):
                        pass  # quote failure already appended above
                    else:
                        block.extend(_mp.longterm_dst_blocks(_mp.longterm_dst_symbols(broker), und, _mnew,
                                                            already_held=und in _pos))
                else:
                    _ob=_mp.order_blocks(_view, und, "add" if und in _pos else "new", _mnew,
                                         order.get("_trend_ok") is True, order.get("_in_loss") is True, _already)
                    # t713u/t714u: when r10_mtf_capital is off, drop percentage-of-capital /
                    # verified-denominator pool blocks only. Name cap, eligibility, add rules stay.
                    if _mtf_cap_skip:
                        _ob=[b for b in _ob if not _r10_capital_msg(b)]
                    block.extend(_ob)
            if not _mon:
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
              if not _mtf_cap_skip:
                  if this_m+new_m>capital*_per: block.append(f"R10: {und} MTF margin Rs {this_m+new_m:,.0f} > {_per*100:g}% cap Rs {capital*_per:,.0f}")
                  if total_m+new_m>capital*_tot: block.append(f"R10: total MTF margin (this bot's names) Rs {total_m+new_m:,.0f} > {_tot*100:g}% cap Rs {capital*_tot:,.0f}")
        elif opt or is_fut(sym) or (sym.replace("-EQ","") in (getattr(_cp, "BEE_SYMBOLS", ()) if _cp is not None else ())):
            if _cap_active and _cp is not None:
                _kind=_cp.classify(sym, prod, k or "")
                if _kind=="unassigned" and order.get("_book")==getattr(_cp,"CRUDE_AUTO_BOOK",None) and opt and und=="CRUDEOIL":
                    # 12 Nov 2026: user-authorized crude option entries. No bucket; Kite only; exactly 1 lot.
                    _cl=None
                    try:
                        import broker_kite as _bk2
                        _row=_bk2._index("MCX")[1].get(sym)
                        _cl=int(_row["lot_size"]) if _row else None
                    except Exception: _cl=None
                    if broker!="kite": block.append("R10: crude_auto buys go to Kite only (Upstox MCX orders are API-disabled)")
                    elif not _cl: block.append(f"R10: Kite lot size unreadable for {sym} - crude buy refused")
                    elif held_q>0: block.append(f"R10: crude_auto already holds {sym}; adds are not part of the crude book")
                    elif qty!=_cl*getattr(_cp,"CRUDE_AUTO_MAX_LOTS",1): block.append(f"R10: crude_auto is {getattr(_cp,'CRUDE_AUTO_MAX_LOTS',1)} lot ({_cl}); order qty {qty} refused")
                elif _kind=="unassigned":
                    block.append("R10: index futures and MCX have no bucket; other books are not used to fund them. Existing exposure is not closed")
                elif _kind in ("stock_options","index_options") and _cap_skip:
                    # capital_gate off (t598u): no verified denominator, so no bucket fit; lot caps above still apply.
                    # The broker's own free funds must still cover the premium (no margin is treated as capital).
                    try:
                        _free=float((_kite_margin or {}).get("net") or 0) if broker=="kite" else float(eq.get("available_margin") or 0)
                    except Exception: _free=0.0
                    _amt=px*(adj if adj else qty)
                    if _amt<=0: block.append(f"R10: {_kind} premium is unreadable - buy refused")
                    elif _free<=0 or _amt>_free: block.append(f"R10 funds: premium Rs {_amt:,.0f} > {broker} free funds Rs {max(_free,0):,.0f} - buy refused")
                elif _kind in ("stock_options","index_options","index_bees"):
                    def _same(row):
                        s2=sym_of(row); return _cp.classify(s2, row.get("product") or "", row.get("instrument_token") or "")==_kind
                    _book_orders=orders if broker=="upstox" else kord
                    _used=_cp.premium_exposure([p for p in pos if _same(p)], [o for o in _book_orders if _same(o)])
                    if _used is None:
                        block.append(f"R10: {_kind} premium is unreadable - buy refused")
                    else:
                        _fit=_cp.fit(_kind, capital if capital else None, _used, px*(adj if adj else qty))
                        if not _fit["ok"]:
                            block.append("R10: "+_fit["reason"])
            else:
                # Pre-9 Nov warning only. This combined line is not a stock-option budget.
                mtf_total=sum(float(m.get("average_price") or 0)*int(m.get("quantity") or 0) for m in mtf)
                fno_lim=min(capital*0.5,capital-mtf_total)
                fno_used=sum(float(p.get("average_price") or p.get("buy_price") or 0)*int(p.get("quantity") or 0) for p in pos if int(p.get("quantity") or 0)>0 and (is_opt(sym_of(p)) or is_fut(sym_of(p))))
                if fno_used+px*qty>fno_lim: flags.append(f"R10 WARNING: F&O deployed Rs {fno_used+px*qty:,.0f} > legacy combined F&O line Rs {max(fno_lim,0):,.0f} (not a stock-option budget)")
    # rule 5
    if opt and ot in("MARKET","SL-M"): block.append(f"R5: {ot} not allowed on options - use LIMIT/SL")
    if side=="SELL":
        # rule 8
        live=sum(int(p.get("quantity") or 0) for p in held if (p.get("product") or "").upper()==prod)
        live=max(live,0)
        if live<=0: block.append(f"R8: no live long {sym} in product {prod} - SELL would open a short"); adj=0
        elif qty>live: adj=live; flags.append(f"R8: qty {qty} > live {live} ({prod}) - adjusted to {live}")
        # rule 9
        if not opt and not is_fut(sym) and ot in("SL","SL-M") and now.time()>EQ_STOP_CUTOFF:
            block.append(f"R9: equity/MTF stop after 15:10 IST ({now:%H:%M}) - closing session rejects it")
    allowed=not block and adj>0
    return allowed, block+flags, adj
def timedelta_min(a,b): return (a-b).total_seconds()/60
if __name__=="__main__":
    if len(sys.argv)>=3 and sys.argv[1]=="check":
        a,r,q=check_order(json.loads(sys.argv[2]))
        print(json.dumps({"allowed":a,"adjusted_qty":q,"reasons":r},indent=1))
    else: print(can_enter())
