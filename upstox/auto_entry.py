#!/usr/bin/env python3
"""Automatic NIFTY/BANKNIFTY/SENSEX option entries (user standing permission from 4 Nov 2026; first entry 1 lot base / 2 ST-EMA / expiry max(3, trend); 14 Nov revert from the 2-lot base).
Run by a routine (every 3 min recommended for the 3m route). Acts only 09:30-15:25 IST Mon-Fri (t673u, 16 Nov 2026;
was 09:20-14:45); the 3m route stops new entries at 15:10 and every route at 15:05 on a Friday (index_routes.py).
t673u routes (INDEX_CONTRACT_POLICY=1): 3m intraday current-week, 15m delivery next-week, 1h delivery monthly
(current-week monthly rolls to next month). Each route holds its own position per index per broker and also takes a
previous-day trend continuation when it has no open position. Per-route tracking: index_route_positions.json.
Signal on last CLOSED 5-min candle (ind.py): FRESH cross only.
  CE: close > EMA20 and Supertrend green, previous candle not both; MACD hist > 0; ADX >= 18
  PE: close < EMA20 and Supertrend red,   previous candle not both; MACD hist < 0; ADX >= 18
  (MACD + ADX>=18 filters are there to avoid chop.)
Option: nearest weekly expiry, ATM strike, qty 1*lot_size on an ordinary trend (trade_lock may still cap it).
STRONG / Supertrend-versus-EMA (completed bar with ST strictly beyond EMA20): qty doubles to 2 lots. Measured on 93 fresh 5m signals: ADX>=25 rising = +0.080% avg forward 60-min move, 62% win,
14% of signals; the rest -0.016%. trade_lock caps still apply.
All rules via trade_lock.check_order. LIMIT BUY at ask (else LTP*1.005), product I (fallback D),
tag auto_entry; cancel if unfilled after 2 min.
Kite (4 Nov): same signal also buys the same sized lots NRML LIMIT on Kite when the Kite token is valid (one decision for
the 3/day count); invalid Kite token -> Kite leg skipped + logged, Upstox unaffected. On fill: trade_lock.record_entry, risk_state.json
(entry, target +20%, stop -20% since 16 Nov 2026 t669u; was -30%), seen_entries.json. One open position per index PER BROKER (t600u, 12 Nov 2026):
a broker already holding that index is skipped, the other still takes a fresh trigger.
After a fill the SL is placed and immediately re-verified (sl_confirm.py: re-reads the order book, retries once on a
tick-snapped price, else logs SL_CONFIRM_FAILED to sl_confirm_failures.json for the 10-min option monitor to alert on).
--dry-run: no orders, no state writes, ignores time window.
t336u (4 Nov 2026): lot size is no longer fixed. `lots` comes from equity/book_sizing.py, which sizes
from free capital, the premium actually quoted, the 5m ADX tier, whether the daily trend agrees, the
expiry-day multiplier and the index-option book's weight. The size_lots fallback (1 lot base, 2 on a strong bar, expiry max(3, trend))
is kept as the fallback for a missing input or a sizing error, and --fixed-size forces it outright.
The chosen lots, the reason, and the inputs used are written to r["size"], r["size_lots"] and
r["size_inputs"]. No cap in trade_lock.py is changed or bypassed - it still re-checks every order."""
import json,os,sys,gzip,time,datetime,urllib.parse
D=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,D)
import ind,trade_lock
import api                     # pooled, timed, proxy-aware client (5 Nov 2026)
import market_calendar
import broker_kite as bk
import sl_confirm as sc
import index_entry_logic as iel
import index_futures as ifut
import index_contract_policy as icp
import index_capital_check as icc
import index_size_policy as isp
import daily_loss_cap as dlc    # 14 Nov 2026: no NEW entry after 2 losing closed trades or Rs15k realised today (both brokers); exits untouched
import entry_filters as ef      # 12 Nov 2026: India VIX filter, SHADOW ONLY: logs would-skip, never blocks
import perf_rules as prules     # 16 Nov 2026 (t669u): 15m trend filter, expiry >1 trading day, 20% initial stop, lot cap
import index_routes as iroutes  # 16 Nov 2026 (t673u): 3m / 15m / 1h routes, per-route position tracking
DRY="--dry-run" in sys.argv
# Delayed confirmation of a fresh 5m cross (MACD/ADX may arrive on a later closed candle,
# and a cooldown must not erase the cross). Off unless the process is started with
# INDEX_REVERSAL_CONFIRM=1 or --reversal-confirm. A single-session replay is not enough
# to turn it on. Same-bar entries are unchanged while this is off.
REVERSAL_CONFIRM=os.environ.get("INDEX_REVERSAL_CONFIRM","")=="1" or "--reversal-confirm" in sys.argv
# Separate from the reversal flag. Default remains the cash index. Futures is used only
# when this process is started with INDEX_ENTRY_SOURCE=futures or --futures-signal.
# A bad value is refused; spot is not assumed. See index_futures.py.
if "--futures-signal" in sys.argv: os.environ["INDEX_ENTRY_SOURCE"]="futures"
ENTRY_SOURCE=ifut.entry_source()
FUTURES_SIGNAL=ENTRY_SOURCE=="futures"
# Off. When on, the default route is completed 1h. NIFTY/SENSEX 1h buys the
# listed next-week expiry; 15m is only used if INDEX_ENTRY_TIMEFRAME=15m and
# then buys the current listed week. BANKNIFTY stays 1h and the futures monthly.
# 5m is not a fallback. Delayed confirmation is a separate flag and stays off.
CONTRACT_POLICY=icp.contract_policy_on()
ENTRY_TF=os.environ.get("INDEX_ENTRY_TIMEFRAME","1h")
# t673u (16 Nov 2026): the contract-policy path evaluates every enabled route (INDEX_ROUTES, default 3m,15m,1h)
# for every index. INDEX_ENTRY_TIMEFRAME is superseded on that path.
ROUTES_ON=iroutes.enabled_routes() if CONTRACT_POLICY else []
EXECUTE=os.environ.get("INDEX_EXECUTE","").strip()=="1" or "--execute" in sys.argv
# Live 1h/15m indicators need enough history for MACD(12,26,9) to settle.
# 5 calendar days was too short. A single 30-day request works; 44 days failed (UDAPI1148).
# Used only on the contract-policy resample path. The 5m paths and the freshness-only
# gate stay at 5 days.
INDICATOR_HISTORY_DAYS=30
TRIGGERS=os.path.join(D,"index_entry_triggers.json")
DECISIONS=os.path.join(D,"index_entry_decisions.jsonl")
# t336u (4 Nov 2026): sizes come from capital + market conditions via equity/book_sizing.py.
# --fixed-size forces the size_lots fallback (1 / 2 / max(3, trend)); any sizing failure falls back to it too.
FIXED="--fixed-size" in sys.argv
# User rule (4 Nov 2026, refined): the expiry-week intraday-only rule applies to STOCK options only.
# This script trades index options only (NIFTY/BANKNIFTY/SENSEX), so they keep normal carry (NRML) -
# in_expiry_week always returns False here. expiry_squareoff.py enforces the rule for stock options.
def in_expiry_week(exp_date):
    return False
IST=datetime.timezone(datetime.timedelta(hours=5,minutes=30))
now=datetime.datetime.now(IST)
# Fallback size_lots matches index_size_policy — 1 lot base, 2 on a
# strong/ST-EMA bar, expiry day is max(3, trend size). The live CONTRACT_POLICY path overwrites
# this with isp.first_entry_lots. --fixed-size still uses this fallback and skips book_sizing.
LOTS=isp.BASE_LOTS; EXPIRY_FLOOR=isp.EXPIRY_DAY_LOTS; ADX_MIN=18; STRONG_LOTS=isp.TREND_LOTS
IDX={"NIFTY":{"key":"NSE_INDEX|Nifty 50","exch":"NSE","seg":"NSE_FO","kite_exchange":"NFO"},
     "BANKNIFTY":{"key":"NSE_INDEX|Nifty Bank","exch":"NSE","seg":"NSE_FO","kite_exchange":"NFO"},
     "SENSEX":{"key":"BSE_INDEX|SENSEX","exch":"BSE","seg":"BSE_FO","kite_exchange":"BFO"}}
SF=os.path.join(D,"risk_state.json"); SEEN=os.path.join(D,"seen_entries.json")
out={"now":now.strftime("%Y-%m-%d %H:%M IST"),"dry_run":DRY,"reversal_confirm":REVERSAL_CONFIRM,
     "entry_source":ENTRY_SOURCE,"contract_policy":CONTRACT_POLICY,"execute":EXECUTE,"sender":bool(EXECUTE),
     "entry_timeframe":",".join(ROUTES_ON) if CONTRACT_POLICY else "5m","routes":ROUTES_ON,"rules":f"fresh 5m cross EMA20+Supertrend, MACD hist in direction, ADX>={ADX_MIN} (anti-chop)","indices":{}}
# t339u: say which rule set this run is under. On 5-6 Nov 2026 the limit checks in trade_lock
# move to the two-day values (equity/autonomy.py) and this block names them; from 7 Nov it is the
# user's own rules again and the block says so. Nothing else in this script changes.
def _ruleset():
    try:
        sys.path.insert(0,"/workspace/equity")
        import autonomy
        return autonomy.rule_set(now)
    except Exception as e:
        return {"rule_set":"user rules","override":"unavailable ("+type(e).__name__+")",
                "note":"trade_lock falls back to the user's constants"}
out["rule_set"]=_ruleset()
# the brief was explicit that the existing `rules` line should say which set it is on, so append
# it rather than making a reader join two fields. The signal logic text above is untouched.
_R=out["rule_set"]
out["rules"]+=(" | "+("OVERRIDE ACTIVE ("+_R["rule_set"]+", "+out["now"][:10]+")" if _R["rule_set"]!="user rules"
                       else "rule set: user's own"))
def done():
    # 19 Nov 2026 post-market review: keep the reason for every routed signal that was not sent
    # (audit only; no decision changes). Best effort, never blocks the run summary.
    try:
        if not DRY and isinstance(out.get("indices"), dict) and "iel" in globals():
            for _row in iroutes.outcome_records(out["indices"], datetime.datetime.now(IST).isoformat(), DRY):
                iel.append_jsonl(DECISIONS, _row)
    except Exception as e:
        print(f"auto_entry: outcome rows not logged: {type(e).__name__}: {e}", file=sys.stderr)
    print(json.dumps(out,indent=1,default=str)); sys.exit(0)
if ENTRY_SOURCE=="invalid":
    out["skipped"]="INDEX_ENTRY_SOURCE must be spot or futures; spot was not assumed"; done()
if not DRY and not market_calendar.is_trading_day(now.date()):
    out["skipped"]="exchange holiday / market closed ("+now.date().isoformat()+")"; done()
if not DRY and (now.weekday()>=5 or not (iroutes.ENTRY_START<=now.time()<=iroutes.ENTRY_END)):
    out["skipped"]="outside 09:30-15:25 IST weekday entry window"; done()
def req(u,body=None,method=None,proxy=False):
    """Parsed JSON, or {'status':'error','code':..,'body':..} exactly as before. One shared
    keep-alive client; proxy=True forces the static-IP proxy, False forces a direct read.
    A POST (order) is never retried."""
    return api.call(u,body=body,method=method,proxy=True if proxy else False,timeout=20)
def tick(p,t=0.05): return round(max(round(p/t)*t,t),2)
def master(exch):
    """Today's contract master for an exchange. Downloaded once when missing, then served from
    the per-process parse cache - pick() walked the same 4 MB list three times per run (5 Nov 2026)."""
    f=os.path.join(D,f"instruments_{exch}_{now.date()}.json.gz")
    if not os.path.exists(f):
        for o in os.listdir(D):
            if o.startswith(f"instruments_{exch}_"): os.remove(os.path.join(D,o))
        _download(f,f"https://assets.upstox.com/market-quote/instruments/exchange/{exch}.json.gz")
    return api.gzip_json(f)


def _download(dest,url):
    import urllib.request
    data=urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=60).read()
    tmp=dest+".part"
    open(tmp,"wb").write(data)
    os.replace(tmp,dest)
def signal(a):
    """Same-bar fresh cross + MACD + ADX. The delayed-confirmation path is index_entry_logic
    and is not consulted unless REVERSAL_CONFIRM is on."""
    return iel.baseline_signal(a, adx_min=ADX_MIN)
def _log_decision(record):
    if DRY: return
    try: iel.append_jsonl(DECISIONS, record)
    except Exception as e:
        print(f"auto_entry: decision log failed: {type(e).__name__}: {e}", file=sys.stderr)
def _save_trigger(name, trigger):
    if DRY or not REVERSAL_CONFIRM or not trigger: return
    if trigger.get("state") in ("consumed","submitting"): return
    try: iel.save_trigger(TRIGGERS, name, trigger)
    except Exception as e:
        print(f"auto_entry: trigger state not saved for {name}: {type(e).__name__}: {e}", file=sys.stderr)
def _remember_block(name, rev, r):
    """A pre-trade block must not consume the cross. The next run can still take it."""
    if not rev or not rev.get("trigger"): return
    tr=dict(rev["trigger"]); tr["last_blockers"]=iel.collect_blockers(r)
    _save_trigger(name, tr)
    rec=iel.public_record(rev, datetime.datetime.now(IST).isoformat(), DRY)
    rec["phase"]="pretrade"; rec["blockers"]=tr["last_blockers"]; rec["action"]="blocked"; rec["eligible"]=False
    r["entry_block"]=rec
    _log_decision(rec)
def size_lots(expire_today, strong_aligned, sig, raw=None, sside=None):
    """Index-option size in lots, and the note to show. Pure, so it can be tested off-market.

    1 lot normally, 2 on a strong/ST-EMA bar. On expiry day the size is
    max(3, the trend-based size), never stacked. This is the fallback used when the
    contract-policy path does not overwrite lots with isp.first_entry_lots.
    """
    trend = STRONG_LOTS if strong_aligned else LOTS
    if expire_today:
        lots = max(EXPIRY_FLOOR, trend)
        return lots, f"expiry-day -> {lots} lots (max of {EXPIRY_FLOOR} and trend size {trend})"
    if strong_aligned:
        return STRONG_LOTS, f"STRONG trend -> {STRONG_LOTS} lots"
    if sig and sside is not None and sside != raw:
        return LOTS, f"strong {sside} but signal is {raw} - normal {LOTS} lots"
    if sig:
        return LOTS, f"ordinary trend -> {LOTS} lots"
    return LOTS, None


# ---------------- t336u: capital- and conditions-driven sizing ----------------
_CAP={}
def free_capital():
    """Equity free margin (available_margin) from Upstox, read once per process. None on failure -
    the caller then keeps the fixed size rather than sizing on a number it could not read."""
    if "v" in _CAP: return _CAP["v"]
    v=None
    try:
        eq=(req("https://api.upstox.com/v2/user/get-funds-and-margin").get("data") or {}).get("equity") or {}
        avail=eq.get("available_margin")
        v=float(avail) if avail is not None else None
    except Exception as e:
        print(f"auto_entry: free capital unreadable, keeping the fixed size: {type(e).__name__}: {e}",file=sys.stderr)
        v=None
    _CAP["v"]=v
    return v


def book_weight(book="index_options"):
    """This book's weight from equity/book_sizing.py, or None (caller keeps the fixed size)."""
    if "w" in _CAP: return _CAP["w"].get(book)
    w={}
    try:
        sys.path.insert(0,"/workspace/equity")
        import book_sizing as bsz
        w=bsz.book_weights()
    except Exception as e:
        print(f"auto_entry: book weights unavailable, keeping the fixed size: {type(e).__name__}: {e}",file=sys.stderr)
        w={}
    _CAP["w"]=w
    return w.get(book)


def streak_mult(index_name):
    """t351u: this index book's day-streak multiplier from book_sizing (1.0 on any failure)."""
    try:
        sys.path.insert(0,"/workspace/equity")
        import book_sizing as bsz
        return float(bsz.streak_mult(index_name))
    except Exception as e:
        print(f"auto_entry: streak multiplier unavailable, using 1.0: {type(e).__name__}: {e}",file=sys.stderr)
        return 1.0


def sized_lots(premium, lot_size, adx, daily_aligned, expire_today, fallback_lots, fallback_note, index_name=None):
    """(lots, note) from book_sizing, or exactly (fallback_lots, fallback_note) when anything is
    missing or the module raises. A sizing bug must never stop the entry routine, and must never
    produce a size the caps do not allow - trade_lock still re-checks every order downstream."""
    if FIXED:
        return fallback_lots, (fallback_note or f"{fallback_lots} lots")+" | fixed size (--fixed-size)"
    try:
        cap=free_capital(); wgt=book_weight("index_options")
        if not premium or not lot_size or cap is None or wgt is None or adx is None:
            return fallback_lots, (fallback_note or f"{fallback_lots} lots")+" | fixed size (sizing input missing)"
        sys.path.insert(0,"/workspace/equity")
        import book_sizing as bsz
        sm=streak_mult(index_name) if index_name else 1.0
        lots,note=bsz.index_option_lots(premium,lot_size,cap,adx,daily_aligned,expire_today,wgt,sm)
        lots=int(lots)
        if lots<0 or lots>bsz.MAX_LOTS:
            return fallback_lots, (fallback_note or f"{fallback_lots} lots")+f" | fixed size (sized {lots} outside 0..{bsz.MAX_LOTS})"
        return lots, f"{lots} lot(s): {note}"
    except Exception as e:
        print(f"auto_entry: dynamic sizing failed, using the fixed size: {type(e).__name__}: {e}",file=sys.stderr)
        return fallback_lots, (fallback_note or f"{fallback_lots} lots")+f" | fixed size (sizing error {type(e).__name__})"


def daily_dir(key):
    """Daily trend direction (+1/-1), or None if it cannot be read."""
    try: return ind.analyze(key,"days",1,60).get("dir")
    except Exception as e:
        print(f"auto_entry: daily trend unavailable for {key}: {type(e).__name__}: {e}",file=sys.stderr)
        return None


def pick(name,cfg,side,spot,expiry=None):
    ins=[x for x in master(cfg["exch"]) if x.get("segment")==cfg["seg"] and x.get("instrument_type")==side and x.get("underlying_symbol",x.get("name",""))==name]
    today_ms=int(datetime.datetime.combine(now.date(),datetime.time(0),IST).timestamp()*1000)
    if expiry is not None:
        chain=[x for x in ins if datetime.datetime.fromtimestamp(x["expiry"]/1000,IST).date()==expiry]
        if not chain: return None
        exp=expiry
    else:
        exps=sorted({x["expiry"] for x in ins if x["expiry"]>=today_ms})
        exp=exps[0]; chain=[x for x in ins if x["expiry"]==exp]
        exp=datetime.datetime.fromtimestamp(exp/1000,IST).date()
    strikes=sorted({x["strike_price"] for x in chain})
    step=min(b-a for a,b in zip(strikes,strikes[1:]) if b>a) if len(strikes)>1 else 50
    std=50 if name=="NIFTY" else 100; step=max(step,std) if step<std else step
    atm=round(spot/std)*std
    best=min(chain,key=lambda x:(abs(x["strike_price"]-atm),x["strike_price"]))
    return best,exp,std
def quote(k):
    d=req("https://api.upstox.com/v2/market-quote/quotes?instrument_key="+urllib.parse.quote(k,safe="")).get("data") or {}
    for v in d.values():
        ask=None
        try: ask=float(v["depth"]["sell"][0]["price"]) or None
        except Exception as e:
            # No ask means the limit falls back to LTP*1.005 - a different entry price, so say it.
            print(f"auto_entry: no sell depth for {k}, pricing off LTP*1.005: {type(e).__name__}: {e}",file=sys.stderr)
        return float(v.get("last_price") or 0),ask
    return 0,None
_pos_reply=req("https://api.upstox.com/v2/portfolio/short-term-positions")
pos=_pos_reply.get("data") or []
def _kite_rows():
    try: return bk.positions(strict=True)
    except Exception: return None
_KPOS=_kite_rows()   # strict Kite read, once per run; None = unreadable/invalid token
def _open_opt_idx(rows):
    """Underlyings with a long option open in one broker's book; None when that book was unreadable."""
    if rows is None: return None
    return [trade_lock.underlying(trade_lock.sym_of(p)) for p in rows
            if int(p.get("quantity") or 0)>0 and trade_lock.is_opt(trade_lock.sym_of(p))]
# 12 Nov 2026 (t600u): the open-position guard is PER BROKER. A fresh trigger is taken by whichever
# broker has no open option on that index; a broker already holding it is skipped (adds are
# full_auto_add.py's job). Before this, an Upstox position blocked the Kite leg too.
U_OPEN=_open_opt_idx(pos if _pos_reply.get("status")=="success" else None)
K_OPEN=_open_opt_idx(_KPOS)
# ---- t673u (16 Nov 2026): per-route occupancy. A tracked position (index_route_positions.json) keeps its
# route; an untracked one (opened before this change, or manually) is classified read-only by inference
# (intraday product -> 3m, monthly expiry -> 1h, else 15m) and is never rewritten or touched.
_RSTATE=iroutes.load()
_MON_CACHE={}; _KEYROW={}; _OCC_CACHE={}; _FUT_CACHE={}
def _monthlies(name,exch):
    if name not in _MON_CACHE:
        _s=set()
        for row in master(exch):
            if row.get("instrument_type")=="FUT" and (row.get("underlying_symbol") or row.get("name"))==name:
                _s.add(datetime.datetime.fromtimestamp(row["expiry"]/1000,IST).date())
        _MON_CACHE[name]=_s
    return _MON_CACHE[name]
def _master_row(key):
    if not _KEYROW:
        for _ex in ("NSE","BSE"):
            for row in master(_ex):
                if row.get("segment") in ("NSE_FO","BSE_FO") and row.get("instrument_key"): _KEYROW[row["instrument_key"]]=row
    return _KEYROW.get(key)
def _route_occupancy(name,cfg,broker):
    """{"routes":{route:[symbols]},"positions":[...]} for one index on one broker; None = book unreadable."""
    ck=(name,broker)
    if ck in _OCC_CACHE: return _OCC_CACHE[ck]
    rows=(pos if _pos_reply.get("status")=="success" else None) if broker=="upstox" else _KPOS
    if rows is None: _OCC_CACHE[ck]=None; return None
    occ={rt:[] for rt in iroutes.ROUTES}; det=[]
    for p in rows:
        sym=trade_lock.sym_of(p)
        if int(p.get("quantity") or 0)<=0 or not trade_lock.is_opt(sym) or trade_lock.underlying(sym)!=name: continue
        if broker=="upstox":
            ik=p.get("instrument_token") or p.get("instrument_key"); key=iroutes.ukey(ik)
        else:
            key=iroutes.kkey(p.get("exchange"),sym)
            try: ik=bk.to_upstox(p.get("exchange"),sym)
            except Exception: ik=None
        mr=_master_row(ik) if ik else None
        expd=datetime.datetime.fromtimestamp(mr["expiry"]/1000,IST).date() if mr and mr.get("expiry") else None
        rt,how=iroutes.route_of(_RSTATE,key,p.get("product"),expd,_monthlies(name,cfg["exch"]))
        occ[rt].append(sym)
        det.append({"symbol":sym,"qty":int(p.get("quantity") or 0),"product":p.get("product"),"route":rt,"how":how,"key":key,"upstox_key":ik,"expiry":str(expd) if expd else None})
    _OCC_CACHE[ck]={"routes":occ,"positions":det}
    return _OCC_CACHE[ck]
def _prune_route_state():
    """Live runs only, both books readable: drop records of positions that are flat and were opened before today."""
    if DRY or not CONTRACT_POLICY or _pos_reply.get("status")!="success" or _KPOS is None: return
    try:
        live={iroutes.ukey(p.get("instrument_token")) for p in pos if int(p.get("quantity") or 0)>0}
        live|={iroutes.kkey(p.get("exchange"),trade_lock.sym_of(p)) for p in _KPOS if int(p.get("quantity") or 0)>0}
        P=_RSTATE.get("positions") or {}
        stale=[k for k,v in P.items() if k not in live and str((v or {}).get("opened",""))[:10]<now.date().isoformat()]
        for k in stale: P.pop(k,None)
        if stale: iroutes.save(_RSTATE)
    except Exception as e:
        print(f"auto_entry: route state prune skipped: {type(e).__name__}: {e}",file=sys.stderr)
_prune_route_state()
# 16 Nov 2026 (t669u): today's order books feed the FIFO loss-cap tally (partly closed rows, carried
# quantity valued at the previous close). An unreadable book only drops back to the row method.
def _orders_or_none():
    try:
        _r=req("https://api.upstox.com/v2/order/retrieve-all")
        return (_r.get("data") or []) if _r.get("status")=="success" else None
    except Exception: return None
def _kite_orders_or_none():
    try: return bk.orders(strict=True) if _KPOS is not None else None
    except Exception: return None
_UORD=_orders_or_none(); _KORD=_kite_orders_or_none()
LOSS_CAP=dlc.evaluate(pos if _pos_reply.get("status")=="success" else None,_KPOS,now,
                      state_path=dlc.default_state_path(),persist=not DRY,
                      upstox_orders=_UORD,kite_orders=_KORD)
out["daily_loss_cap"]=LOSS_CAP
_EF=ef.config()
if _EF["mode"]!="off": _EF["mode"]="shadow"   # user rule 12 Nov 2026: shadow only, enforce is not honoured here
_VIX=ef.vix_inputs(ind.candles,now) if (_EF["mode"]!="off" and _EF["vix_pct"] is not None) else (None,[])
# t673u: one pass per (index, route). Without the contract policy the legacy single 5m pass is unchanged.
_LOOP=[(name,cfg,rt) for name,cfg in IDX.items() for rt in (ROUTES_ON or [None])]
for name,cfg,ROUTE in _LOOP:
    if ROUTE is None: r=out["indices"][name]={}
    else: r=out["indices"].setdefault(name,{})[ROUTE]={}
    try:
        claimed_id=None
        signal_key=cfg["key"]
        forced_expiry=None
        tf_meta=None
        futures_5m=None
        _elig={"upstox":{"ok":True,"reason":None},"kite":{"ok":True,"reason":None}}
        _occ={}
        if ROUTE is not None:
            r["route"]=ROUTE
            r["product"]={"upstox":iroutes.SPEC[ROUTE]["upstox_product"],"kite":iroutes.SPEC[ROUTE]["kite_product"]}
            _wok,_wwhy=iroutes.in_window(ROUTE, now)
            if not _wok and not DRY:
                r["signal"]="NONE"; r["action"]="no entry: "+_wwhy; continue
            if not _wok: r["window_note"]=_wwhy+" (ignored on --dry-run)"
        if CONTRACT_POLICY:
            _route=iroutes.route(name, ROUTE)
            if not _route["ok"]:
                r["signal"]="NONE"; r["action"]="no entry: "+_route["reason"]; continue
            if not FUTURES_SIGNAL:
                r["signal"]="NONE"; r["action"]="no entry: 1h/15m requires futures candles; spot 5m was not used"; continue
        if FUTURES_SIGNAL:
            try:
                if name not in _FUT_CACHE:
                    krows,kerr=ifut.fetch_kite_master(cfg["kite_exchange"])
                    _res=ifut.resolve(name, master(cfg["exch"]), krows, now.date())
                    if krows is None: _res["audit"]["kite_master_error"]=kerr
                    _FUT_CACHE[name]=_res
                res=_FUT_CACHE[name]
            except Exception as e:
                r["signal"]="NONE"; r["action"]=f"no entry: futures master unreadable ({type(e).__name__}); spot was not used"
                continue
            r["futures"]=res["audit"]
            if not res["ok"]:
                r["signal"]="NONE"; r["action"]="no entry: "+res["reason"]; continue
            signal_key=res["instrument_key"]
            try:
                # Freshness gate itself is unchanged (assess_candles, last closed bar).
                # When the 1h/15m path also needs this series, fetch the 30-day window
                # once and reuse it below. The gate-only path stays at 5 days.
                _5m_days=INDICATOR_HISTORY_DAYS if CONTRACT_POLICY else 5
                futures_5m=ind.candles(signal_key,"minutes",5,_5m_days)
                gate=ifut.assess_candles(futures_5m, datetime.datetime.now(IST))
            except Exception as e:
                r["signal"]="NONE"; r["action"]=f"no entry: futures candles unreadable ({type(e).__name__}); spot was not used"
                continue
            r["futures_candles"]=gate
            if not gate["ok"] and DRY and "stale" in str(gate.get("reason")):
                # --dry-run only (never orders): evaluate the last completed session so routes/contracts can be checked after hours.
                r["dry_run_note"]="futures candles stale ("+gate["reason"]+"); evaluated anyway because --dry-run cannot order"
            elif not gate["ok"]:
                r["signal"]="NONE"; r["action"]="no entry: "+gate["reason"]+"; spot was not used"; continue
        if CONTRACT_POLICY:
            try:
                if _route["minutes"]==3:
                    # t673u 3m route: the broker's own 3-minute futures candles (5m cannot build 3m).
                    _c3=[c for c in ind.candles(signal_key,"minutes",3,iroutes.SPEC["3m"]["history_days"])
                         if icp.OPEN<=iel.parse_ts(c[0]).time()<icp.CLOSE]   # session bars only (09:15-15:30 IST)
                    built={"ok":bool(_c3),"reason":"no 3m futures candles","candles":_c3}
                else:
                    if futures_5m is None:
                        futures_5m=ind.candles(signal_key,"minutes",5,INDICATOR_HISTORY_DAYS)
                    built=icp.resample(futures_5m, _route["minutes"], datetime.datetime.now(IST))
            except Exception as e:
                r["signal"]="NONE"; r["action"]=f"no entry: futures resample failed ({type(e).__name__}); 5m was not used"; continue
            if not built["ok"]:
                r["signal"]="NONE"; r["action"]="no entry: "+built["reason"]+"; 5m was not used"; continue
            bars,forming=iel.bars_from_candles(built["candles"], datetime.datetime.now(IST), "minutes", _route["minutes"])
            if len(bars)<2:
                r["signal"]="NONE"; r["action"]="no entry: resampled series has no closed pair"; continue
            a=iel.analyze_shape(bars[-1], bars[-2]); a["last_ts"]=bars[-1]["ts"]; a["st"]=bars[-1].get("st"); a["prev_hist"]=bars[-2]["hist"]; a["prev_adx"]=bars[-2]["adx"]
            sig,raw,why=signal(a)
            r["signal_mode"]="fresh_cross" if sig else None
            if not sig:
                # t673u: previous-day trend continuation (no fresh cross) when the route has no open position;
                # a broker that already holds this route is refused by dedupe_route below.
                _cs,_cdet=iroutes.continuation(bars, now.date(), ADX_MIN)
                r["continuation"]=_cdet
                if _cs:
                    sig=raw=_cs; why=list(why)+[_cdet["why"]]; r["signal_mode"]="prev_day_continuation"
            monthlies=set()
            try:
                for row in master(cfg["exch"]):
                    if row.get("instrument_type")=="FUT" and (row.get("underlying_symbol") or row.get("name"))==name:
                        monthlies.add(datetime.datetime.fromtimestamp(row["expiry"]/1000,IST).date())
                opt_exp=set(); _wk={}
                for row in master(cfg["exch"]):
                    if row.get("instrument_type") in ("CE","PE") and (row.get("underlying_symbol") or row.get("name"))==name and row.get("expiry"):
                        _d=datetime.datetime.fromtimestamp(row["expiry"]/1000,IST).date()
                        opt_exp.add(_d); _wk.setdefault(_d,set()).add(row.get("weekly"))
                # 12 Nov 2026 (t601u): pass the master's own weekly/monthly tag so the monthly contract
                # can serve as NIFTY/SENSEX next-week expiry. A date with mixed or missing tags stays untagged.
                _rows=[{"expiry":_d,"weekly":next(iter(_f))} if len(_f)==1 and next(iter(_f)) in (True,False) else _d
                       for _d,_f in _wk.items()]
                chosen=iroutes.select_expiry(name, ROUTE, now.date(), _rows, monthlies)
                r["expiry_choice"]={k:(v.isoformat() if hasattr(v,"isoformat") else v) for k,v in chosen.items()}
            except Exception as e:
                r["signal"]="NONE"; r["action"]=f"no entry: expiry master unreadable ({type(e).__name__})"; continue
            if not chosen["ok"]:
                r["signal"]="NONE"; r["action"]="no entry: "+chosen["reason"]; continue
            forced_expiry=chosen["expiry"]
            tf_meta=icp.metadata(name, _route["name"], chosen["expiry"], chosen["role"], signal_key)
            tf_meta["_route"]=ROUTE
            r["contract"]=tf_meta
            held=iel.load_trigger(TRIGGERS, "_decisions") or {}
            today_dec=[row for row in (held.get(now.date().isoformat()) or [])]
            # t673u: one position per index PER ROUTE per broker (was per index).
            _occ={"upstox":_route_occupancy(name,cfg,"upstox"),"kite":_route_occupancy(name,cfg,"kite")}
            r["route_occupancy"]={b:(v["positions"] if v is not None else "unreadable") for b,v in _occ.items()}
            _elig={b:iroutes.dedupe_route(name, b, ROUTE, (_occ[b]["routes"] if _occ[b] is not None else None), today_dec)
                   for b in ("upstox","kite")}
            r["broker_eligibility"]=_elig
            if not _elig["upstox"]["ok"] and not _elig["kite"]["ok"]:
                r["signal"]="NONE"; r["action"]="no entry: upstox: "+_elig["upstox"]["reason"]+" | kite: "+_elig["kite"]["reason"]; continue
        else:
            a=ind.analyze(signal_key,"minutes",5,5)
            sig,raw,why=signal(a)
        r["candle"]=a["last_ts"][11:16]; r["5m"]={k:a[k] for k in("close","ema20","st","dir","prev_close","prev_ema","prev_dir","hist","adx") if k in a}
        r["signal"]=sig or "NONE"; r["trend_side"]=raw; r["why"]=why
        if CONTRACT_POLICY:
            # 12 Nov 2026 post-market review: the contract-policy (1h) path only logged pre-trade
            # blocks, so quiet days left no audit trail. Log one read-only signal row per index.
            try:
                _log_decision({"phase":"signal","index":name,"ts":datetime.datetime.now(IST).isoformat(),
                               "route":_route.get("name"),"candle":a.get("last_ts"),"signal":sig or "NONE",
                               "trend_side":raw,"why":why,"broker_eligibility":_elig,"dry_run":DRY})
            except Exception as e:
                print(f"auto_entry: decision row not built for {name}: {type(e).__name__}: {e}", file=sys.stderr)
        openp=[p for p in pos if int(p.get("quantity") or 0)>0 and trade_lock.underlying(trade_lock.sym_of(p))==name and trade_lock.is_opt(trade_lock.sym_of(p))]
        if ROUTE is not None:
            # t673u: only this route's own position blocks this route on Upstox.
            _uo=((_occ.get("upstox") or {}).get("routes") or {}).get(ROUTE) or []
            openp=[p for p in openp if trade_lock.sym_of(p) in _uo]
        if openp: r["open_position"]=[trade_lock.sym_of(p) for p in openp]
        rev=None
        if not CONTRACT_POLICY:
            try:
                wall=datetime.datetime.now(IST)
                cs=ind.candles(signal_key,"minutes",5,5)
                bars,forming=iel.bars_from_candles(cs, wall, "minutes", 5)
                stored=iel.load_trigger(TRIGGERS, name)
                rev=iel.decide(name, bars, stored, position_open=bool(openp) and (K_OPEN is None or name in K_OPEN), blockers=[], gate=REVERSAL_CONFIRM,
                               forming_dropped=forming, now=wall)
                latest=bars[-1]["ts"] if bars else None
                sig,raw,why,mode=iel.select_signal(sig, raw, why, rev, latest)
                r["signal"]=sig or "NONE"; r["trend_side"]=raw; r["why"]=why
                if mode=="reversal": r["reversal_entry"]=True
                if mode=="duplicate_suppressed": r["reversal_duplicate"]=True
                rec=iel.public_record(rev, wall.isoformat(), DRY)
                rec["phase"]="signal"; rec["select_mode"]=mode
                r["entry_decision"]=rec
                _log_decision(rec)
                if mode!="duplicate_suppressed": _save_trigger(name, rev.get("trigger"))
            except Exception as e:
                # The same-bar signal still stands. A logging failure must not create an order
                # and must not hide a baseline order either.
                print(f"auto_entry: reversal check failed for {name}, baseline signal kept: {type(e).__name__}: {e}", file=sys.stderr)
                r["reversal_error"]=f"{type(e).__name__}: {e}"
        strike_px=None if FUTURES_SIGNAL else a["close"]
        if FUTURES_SIGNAL:
            try:
                sa=ind.analyze(cfg["key"],"minutes",5,5)
                strike_px=sa["close"]
                r["strike_underlying"]={"source":"spot","key":cfg["key"],"close":sa["close"],"candle":sa["last_ts"],"used_for":"atm strike only"}
            except Exception as e:
                r["strike_underlying"]={"source":"spot","key":cfg["key"],"error":f"{type(e).__name__}: {e}"}
                if sig:
                    r["signal"]="NONE"; r["action"]=f"no entry: spot unavailable for strike selection ({type(e).__name__}); futures price was not used"
                    continue
        else:
            r["strike_underlying"]={"source":"spot","key":cfg["key"],"close":a["close"],"candle":a["last_ts"],
                                    "used_for":"atm strike and the entry signal"}
        r["signal_underlying"]={"source":ENTRY_SOURCE,"key":signal_key}
        if FUTURES_SIGNAL and strike_px is None:
            if sig:
                r["signal"]="NONE"; r["action"]="no entry: spot unavailable for strike selection; futures price was not used"
            else:
                r["action"]=r.get("action") or "none: no fresh signal"
            continue
        picked=pick(name,cfg,raw or "CE",strike_px,forced_expiry)
        if not picked:
            r["signal"]="NONE"; r["action"]="no entry: listed expiry "+str(forced_expiry)+" is not in the option master; nearest expiry was not used"; continue
        best,exp,std=picked
        if ROUTE is not None:
            # t673u: one contract is tracked by one route per broker (stops/exits are keyed by contract).
            for _b in ("upstox","kite"):
                _same=[d2 for d2 in ((_occ.get(_b) or {}).get("positions") or []) if d2.get("upstox_key")==best["instrument_key"]]
                if _same and _elig[_b]["ok"]:
                    _elig[_b]={"ok":False,"reason":f"{best['trading_symbol']} is already held on {_b} by the {_same[0]['route']} route; one contract per route"}
            r["broker_eligibility"]=_elig
        strong,sside,sdet=ind.strong(a); r["strong"]={"on":strong,"side":sside,**sdet}
        expire_today = (exp == now.date())
        lots, size_note = size_lots(expire_today, bool(strong and sig and sside == raw), sig, raw, sside)
        r["expiry_day"] = expire_today
        if size_note: r["size"] = size_note
        qty=lots*int(best["lot_size"])
        r["option"]={"symbol":best["trading_symbol"],"key":best["instrument_key"],"expiry":str(exp),"strike":best["strike_price"],"lot_size":best["lot_size"],"qty":qty,"strike_step":std}
        if ROUTE is not None:
            r["option"].update({"route":ROUTE,"expiry_role":(r.get("expiry_choice") or {}).get("role"),
                                "expiry_rule":(r.get("expiry_choice") or {}).get("rule"),
                                "product_upstox":iroutes.SPEC[ROUTE]["upstox_product"],"product_kite":iroutes.SPEC[ROUTE]["kite_product"]})
        if not sig:
            r["action"]="duplicate: reversal trigger already submitted" if r.get("reversal_duplicate") else "none: no fresh signal"
            continue
        if not LOSS_CAP["ok"]:
            r["signal"]="NONE"; r["action"]="no entry: "+LOSS_CAP["reason"]; continue
        # 16 Nov 2026 (t669u): never a contract expiring within 1 trading day (the expiry rule itself
        # stays next week for NIFTY/SENSEX and monthly for BANKNIFTY).
        if prules.expiry_too_near(exp, now.date(), market_calendar.next_trading_day):
            r["signal"]="NONE"; r["action"]=f"no entry: {best['trading_symbol']} expires {exp} (within 1 trading day); not taken"
            prules.log_refusal({"source":"auto_entry","index":name,"signal":sig,"rule":"expiry","symbol":best["trading_symbol"],"expiry":str(exp)})
            _log_decision({"phase":"pretrade","index":name,"ts":datetime.datetime.now(IST).isoformat(),"action":"blocked","blockers":["expiry within 1 trading day"],"dry_run":DRY})
            continue
        # 16 Nov 2026 (t669u): 15m trend filter on the underlying index (last CLOSED 15m bar).
        _t15=prules.trend15_check(name, sig, trade_lock.INDEX_KEYS, ind.analyze)
        r["trend_15m"]=_t15
        if not _t15["ok"]:
            r["signal"]="NONE"; r["action"]="no entry: 15m trend filter: "+_t15["why"]
            prules.log_refusal({"source":"auto_entry","index":name,"signal":sig,"rule":"trend15","why":_t15["why"]})
            _log_decision({"phase":"pretrade","index":name,"ts":datetime.datetime.now(IST).isoformat(),"action":"blocked","blockers":["15m trend: "+_t15["why"]],"dry_run":DRY})
            continue
        if _EF["mode"]=="shadow":
            _flt=ef.check(now,adx=a.get("adx"),vix_current=_VIX[0],vix_prior=_VIX[1],cfg=_EF)
            r["entry_filters"]=_flt
            ef.log_shadow({"ts":now.isoformat(),"source":"auto_entry","index":name,"signal":sig,"candle":a.get("last_ts"),
                           "would_skip":_flt["would_skip"],"inputs":_flt["inputs"],"dry_run":DRY,"blocked":False})
        # Fresh buys stay closed unless the user has turned the sender on and the
        # required route is live. Existing stops are not touched. The add and Friday
        # hedge senders stay unimplemented.
        missing=[]
        if not FUTURES_SIGNAL: missing.append("futures source")
        if not CONTRACT_POLICY: missing.append("timeframe/expiry metadata")
        if not EXECUTE: missing.append("sender not enabled")
        if missing:
            r["fresh_entry"]="fail_closed"
            r["sender"]=False
            r["action"]="no entry: fresh index entry is fail-closed until futures source, timeframe/expiry metadata, and a sender is explicitly enabled ("+", ".join(missing)+"); no order sent"
            continue
        r["fresh_entry"]="execute_on"
        r["sender"]=True
        ltp,ask=quote(best["instrument_key"]); t=float(best.get("tick_size") or 0.05)
        if t>1: t=t/100  # some masters give paise
        px=tick(ask if ask else ltp*1.005,t); r["ltp"]=ltp; r["ask"]=ask; r["limit"]=px
        lot=int(best["lot_size"])
        # t336u: re-size on the real premium just quoted. Until now `lots` is the old fixed answer,
        # which is also the fallback if the dynamic sizing cannot run.
        # A degenerate quote (no LTP and no ask -> px would be 5 paise) must NOT be sized: a 5-paise
        # premium would "afford" MAX_LOTS. Keep the fixed size and say why.
        quoted=bool(ltp or ask) and px>0
        dd=daily_dir(signal_key) if sig else None
        if FUTURES_SIGNAL and sig and dd is None: r["daily_trend"]="futures daily unavailable; spot daily was not used"
        d_aligned=bool(dd is not None and raw is not None and ((dd==1 and raw=="CE") or (dd==-1 and raw=="PE")))
        if quoted:
            lots,size_note=sized_lots(px,lot,a["adx"],d_aligned,expire_today,lots,r.get("size"),name)
        else:
            size_note=(r.get("size") or f"{lots} lots")+" | fixed size (no usable quote: ltp "+str(ltp)+", ask "+str(ask)+")"
        if CONTRACT_POLICY:
            # Capital and the order share one size. Supertrend-versus-EMA (2 lots when
            # the completed bar qualifies, otherwise 1) and the unused one-shot combine with max().
            # This loop is NIFTY/BANKNIFTY/SENSEX only. Adds are not sent.
            # 1-lot base; on this index's own expiry day the size is max(3, the trend size).
            exp_day=isp.is_expiry_day(name, now.date(), opt_exp)
            r["index_expiry_day"]=exp_day
            lots=isp.first_entry_lots(sig, a.get("st"), a.get("ema20"), underlying=name, expiry_day=exp_day)
            r["first_lots_override"]=bool(isp.pending_first_lots())
            signal_lots=isp.st_vs_ema_lots(sig, a.get("st"), a.get("ema20"))
            shot=isp.pending_first_lots() or isp.BASE_LOTS
            size_note=("contract policy: first entry is %s lot%s per broker; Supertrend-versus-EMA is %s, the one-shot is %s and the expiry-day size is %s, combined with max(); an add is not sent" % (
                lots, "" if lots==1 else "s", signal_lots, shot, isp.EXPIRY_DAY_LOTS if exp_day else isp.BASE_LOTS))
            u_cap=icc.check_index_option_buy({}, [], [], px*lots, now)
            k_cap=icc.check_index_option_buy({"broker":"kite"}, [], [], px*lots, now)
            gate=isp.dual_first_entry((tf_meta or {}).get("_expiry_role") or "next_week", 0, 0, u_cap, k_cap, side=sig, st=a.get("st"), ema=a.get("ema20"), underlying=name, expiry_day=exp_day)
            r["dual_entry"]=gate
            if gate.get("ok") and int(gate.get("lots_per_broker") or 0)!=int(lots):
                r["signal"]="NONE"; r["action"]="no entry: capital check size does not match the first-entry size"; continue
            if not gate["ok"]:
                reason=str(gate.get("reason") or "")
                unverified=("verified" in reason.lower() or "margin capacity" in reason.lower()
                            or "unverified" in reason.lower() or "not on this branch" in reason.lower()
                            or "rupee basis" in reason.lower() or "denominator" in reason.lower())
                if EXECUTE and unverified:
                    r["capital_note"]="capital denominator is unverified; INDEX_EXECUTE is on, so the %s-lot entry proceeds under trade_lock caps; verified capital was not invented" % lots
                else:
                    r["signal"]="NONE"; r["action"]="no entry: "+reason; continue
        r["size"]=size_note; r["size_lots"]=lots
        r["size_inputs"]={"premium":px,"lot_size":lot,"adx":a["adx"],"daily_aligned":d_aligned,
                          "expire_today":expire_today,"weight":book_weight("index_options"),
                          "free_capital":free_capital(),"fixed_size":FIXED,
                          "streak_mult":streak_mult(name)}
        qty=lots*lot; r["option"]["qty"]=qty
        ekw=in_expiry_week(exp)
        r["expiry_week"]=ekw
        r["carry"]="intraday only (expiry week)" if ekw else "positional"
        # t673u: product comes from the route (3m intraday I/MIS; 15m and 1h delivery D/NRML).
        _uprod=iroutes.SPEC[ROUTE]["upstox_product"] if ROUTE else "I"
        _kprod=iroutes.SPEC[ROUTE]["kite_product"] if ROUTE else ("MIS" if ekw else "NRML")
        if ROUTE: r["carry"]="intraday (3m route, same-day square-off)" if iroutes.SPEC[ROUTE]["intraday"] else "delivery (NRML/D)"
        # ---- Kite leg plan (checked BEFORE any order so the pair is one decision; never blocks Upstox) ----
        kleg=None
        try:
            if not bk.valid(): r["kite"]={"action":"skipped: Kite token invalid ("+bk.why()[:80]+")"}; bk.log(f"auto_entry {name}: kite skipped, invalid token")
            else:
                m=bk.from_upstox(best["instrument_key"])
                kopen=[p for p in bk.positions() if p["quantity"]>0 and trade_lock.underlying(p["trading_symbol"])==name and trade_lock.is_opt(p["trading_symbol"])]
                if ROUTE is not None:
                    _ko=((_occ.get("kite") or {}).get("routes") or {}).get(ROUTE) or []
                    kopen=[p for p in kopen if p["trading_symbol"] in _ko]
                if not _elig["kite"]["ok"]: r["kite"]={"action":"skip: "+_elig["kite"]["reason"]}
                elif not m: r["kite"]={"action":"skipped: no Kite symbol for "+best["instrument_key"]}
                elif kopen: r["kite"]={"action":"skip: Kite position already open","open":[p["trading_symbol"] for p in kopen]}
                else:
                    kq=lots*m["lot_size"]
                    kprod=_kprod
                    ko={"trading_symbol":m["tradingsymbol"],"instrument_token":"KITE|%s|%s"%(m["exchange"],m["tradingsymbol"]),"transaction_type":"BUY","order_type":"LIMIT","product":kprod,"quantity":kq,"price":px,"_signal_source":ENTRY_SOURCE,"_signal_instrument_key":signal_key,"_rule_lots":lots,"tag":"auto_entry",**({k2:v for k2,v in (tf_meta or {}).items() if k2.startswith("_")})}
                    kok,kre,kadj=trade_lock.check_order(ko,broker="kite"); kadj=(kadj//m["lot_size"])*m["lot_size"]
                    r["kite"]={"symbol":m["tradingsymbol"],"exchange":m["exchange"],"qty":kq,"check":{"allowed":kok,"reasons":kre,"adjusted_qty":kadj}}
                    if kok and kadj>0: kleg={**m,"qty":kadj}
                    else: r["kite"]["action"]="blocked by pre-trade rules"
        except Exception as e: r["kite"]={"action":f"skipped: error {type(e).__name__}: {e}"}; kleg=None
        # ---- Upstox leg ----
        uleg=None
        if openp: r["action"]="skip: position already open in "+name+" on Upstox (Kite decided separately)"
        elif not _elig["upstox"]["ok"]: r["action"]="skip: "+_elig["upstox"]["reason"]
        else:
            csym=f"{name}{exp:%y%m%d}{int(best['strike_price'])}{raw}"  # compact form trade_lock parses (master symbol has spaces)
            o={"trading_symbol":csym,"instrument_token":best["instrument_key"],"transaction_type":"BUY","order_type":"LIMIT","product":_uprod,"quantity":qty,"price":px,"_signal_source":ENTRY_SOURCE,"_signal_instrument_key":signal_key,"_rule_lots":lots,"tag":"auto_entry",**({k2:v for k2,v in (tf_meta or {}).items() if k2.startswith("_")})}
            ok,reasons,adj=trade_lock.check_order(o); adj=(adj//lot)*lot
            r["check"]={"allowed":ok,"reasons":reasons,"adjusted_qty":adj}
            if not ok or adj<=0: r["action"]="blocked by pre-trade rules"
            else: uleg=adj
        if DRY:
            if uleg: r["action"]=f"DRY-RUN: would BUY {uleg} {best['trading_symbol']} LIMIT {px} {_uprod} on Upstox"
            if kleg: r["kite"]["action"]=f"DRY-RUN: would BUY {kleg['qty']} {kleg['tradingsymbol']} LIMIT {px} {_kprod} on Kite"; bk.place(kleg["exchange"],kleg["tradingsymbol"],"BUY",kleg["qty"],px,dry=True)
            continue
        if not uleg and not kleg:
            _remember_block(name, rev, r)
            continue
        if REVERSAL_CONFIRM and rev and (rev.get("trigger") or {}).get("side")==raw:
            okc,ctr=iel.claim_trigger(TRIGGERS, name, rev["trigger"], datetime.datetime.now(IST).isoformat())
            if not okc:
                r["action"]="duplicate: reversal trigger already "+str((ctr or {}).get("state"))
                continue
            claimed_id=rev["trigger"]["id"]
        if CONTRACT_POLICY and not DRY:
            try:
                book=iel.load_trigger(TRIGGERS, "_decisions") or {}
                day=now.date().isoformat()
                rows=list(book.get(day) or [])
                rows.append({"index":name,"route":ROUTE,"timeframe":(tf_meta or {}).get("_timeframe"),"expiry":(tf_meta or {}).get("_option_expiry"),"side":raw,
                             "brokers":[b for b,leg in (("upstox",uleg),("kite",kleg)) if leg]})
                book[day]=rows
                iel.save_trigger(TRIGGERS, "_decisions", book)
                if ROUTE is not None:
                    # pre-record the route before any order, so a crash after a fill cannot orphan the position
                    for _b,_leg in (("upstox",uleg),("kite",kleg)):
                        if not _leg: continue
                        _k=iroutes.ukey(best["instrument_key"]) if _b=="upstox" else iroutes.kkey(kleg["exchange"],kleg["tradingsymbol"])
                        iroutes.record(_RSTATE,_k,route_name=ROUTE,index=name,symbol=best["trading_symbol"] if _b=="upstox" else kleg["tradingsymbol"],
                                       product=_uprod if _b=="upstox" else _kprod,qty=0,ts=now.isoformat(),broker=_b)
                    iroutes.save(_RSTATE)
            except Exception as e:
                r["action"]=f"no entry: timeframe decision was not recorded ({type(e).__name__}); order not sent"
                continue
        oid=None; koid=None
        if uleg:
            body={"quantity":uleg,"product":_uprod,"validity":"DAY","price":px,"tag":"auto_entry","instrument_token":best["instrument_key"],"order_type":"LIMIT","transaction_type":"BUY","disclosed_quantity":0,"trigger_price":0,"is_amo":False}
            pr=req("https://api-hft.upstox.com/v2/order/place",body,"POST",proxy=True)
            if pr.get("status")!="success" and "product" in json.dumps(pr).lower() and not ekw and ROUTE is None:
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
        if fill>0 or kfill>0:
            trade_lock.record_entry()  # one decision even if both legs fill
            if r.get("first_lots_override"):
                isp.consume_first_lots_override()
        s=json.load(open(SF)) if os.path.exists(SF) else {}; seen=json.load(open(SEEN)) if os.path.exists(SEEN) else {}
        if oid:
            r["status"]=st; r["filled"]=fill
            if fill>0:
                _sf=1-prules.INITIAL_STOP_PCT
                s[best["instrument_key"]]={"high":avg,"entry":avg,"target":round(avg*1.2,2),"sym":best["trading_symbol"],"source":"auto_entry","intraday":bool(ekw or (ROUTE and iroutes.SPEC[ROUTE]["intraday"])),"route":ROUTE,"stop_pct":prules.INITIAL_STOP_PCT}
                s.setdefault("_min_stop_by_symbol",{})[best["trading_symbol"]]=tick(avg*_sf)
                seen[oid]={"sym":best["trading_symbol"],"qty":fill,"avg":avg,"time":now.isoformat(),"source":"auto_entry"}
                r["action"]=f"ENTERED {fill} {best['trading_symbol']} @ {avg}; target {round(avg*1.2,2)} stop {tick(avg*_sf)}"
            else: r["action"]="not filled in 2 min - cancelled"
        if koid:
            ka=(kst or {}).get("avg") or px; r["kite"]["status"]=(kst or {}).get("status"); r["kite"]["filled"]=kfill
            if kfill>0:
                kk="KITE|%s|%s"%(kleg["exchange"],kleg["tradingsymbol"])
                _sf=1-prules.INITIAL_STOP_PCT
                s[kk]={"high":ka,"entry":ka,"target":round(ka*1.2,2),"sym":kleg["tradingsymbol"],"source":"auto_entry","broker":"kite","route":ROUTE,"intraday":bool(ROUTE and iroutes.SPEC[ROUTE]["intraday"]),"stop_pct":prules.INITIAL_STOP_PCT}
                s.setdefault("_min_stop_by_symbol",{})[kleg["tradingsymbol"]]=tick(ka*_sf)
                seen["kite:"+str(koid)]={"sym":kleg["tradingsymbol"],"qty":kfill,"avg":ka,"time":now.isoformat(),"source":"auto_entry","broker":"kite"}
                r["kite"]["action"]=f"ENTERED {kfill} {kleg['tradingsymbol']} @ {ka} on Kite; target {round(ka*1.2,2)} stop {tick(ka*_sf)}"
            else: r["kite"]["action"]="Kite not filled in 2 min - cancelled"
        json.dump(s,open(SF,"w"),indent=1); json.dump(seen,open(SEEN,"w"),indent=1)
        if ROUTE is not None:
            # t673u: confirm (or drop) the pre-recorded route rows with the real fills.
            try:
                _P=_RSTATE.setdefault("positions",{})
                _uk=iroutes.ukey(best["instrument_key"])
                if uleg:
                    if fill>0: iroutes.record(_RSTATE,_uk,route_name=ROUTE,index=name,symbol=best["trading_symbol"],product=body.get("product"),qty=fill,ts=now.isoformat(),broker="upstox",order_id=oid)
                    elif not [p for p in pos if p.get("instrument_token")==best["instrument_key"] and int(p.get("quantity") or 0)>0]: _P.pop(_uk,None)
                if kleg:
                    _kk=iroutes.kkey(kleg["exchange"],kleg["tradingsymbol"])
                    if kfill>0: iroutes.record(_RSTATE,_kk,route_name=ROUTE,index=name,symbol=kleg["tradingsymbol"],product=kprod,qty=kfill,ts=now.isoformat(),broker="kite",order_id=koid)
                    else: _P.pop(_kk,None)
                iroutes.save(_RSTATE)
            except Exception as e: r["route_state_error"]=f"{type(e).__name__}: {e}"
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
        if claimed_id:
            sub={"upstox":{"attempted":bool(uleg),"order_id":oid,"status":st if uleg else None,"filled":fill if uleg else 0},
                 "kite":{"attempted":bool(kleg),"order_id":koid,"status":(kst or {}).get("status") if kleg else None,"filled":kfill if kleg else 0}}
            iel.mark_consumed(TRIGGERS, name, claimed_id, sub, datetime.datetime.now(IST).isoformat())
            rec=iel.public_record(rev or {}, datetime.datetime.now(IST).isoformat(), DRY)
            rec["phase"]="submission"; rec["submission"]=iel.sanitize(sub); rec["action"]="submitted"
            r["submission"]=rec["submission"]
            _log_decision(rec)
            claimed_id=None
    except Exception as e:
        r["error"]=f"{type(e).__name__}: {e}"
        if claimed_id and REVERSAL_CONFIRM and not DRY:
            try: iel.mark_incomplete(TRIGGERS, name, claimed_id, datetime.datetime.now(IST).isoformat())
            except Exception as e2:
                print(f"auto_entry: could not close trigger {claimed_id}: {type(e2).__name__}: {e2}", file=sys.stderr)
done()
