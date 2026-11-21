#!/usr/bin/env python3
"""Same-day square-off for the 3m (intraday) index-option route (t673u, 16 Nov 2026).

Closes ONLY positions that index_route_positions.json records as route "3m" AND that are
still on an intraday product (Upstox I / Kite MIS). 15m and 1h positions, untracked
positions (opened before t673u or manually) and anything else are never touched.

For each such position: cancel its own working SELL orders (SL / target) first, re-read the
live quantity (so a stop that filled meanwhile is not sold again), then one LIMIT SELL at the
bid (else LTP*0.98); if it is not complete in 60 s, cancel and re-price once at LTP*0.95.
The broker's own MIS auto square-off (~15:15-15:25) remains the backstop.

Sends orders only when started with --execute or INDEX_3M_SQUAREOFF_EXECUTE=1, on a trading
day between 15:12 and 15:29 IST. Default is a read-only plan (also with --dry-run).
Suggested routine: 15:12 IST weekdays (and 15:06 on Fridays, before the 15:10 flatten).
"""
import datetime, json, os, sys, time, urllib.parse

D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
import api, market_calendar, trade_lock
import index_routes as iroutes

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
now = datetime.datetime.now(IST)
EXECUTE = ("--execute" in sys.argv or os.environ.get("INDEX_3M_SQUAREOFF_EXECUTE", "").strip() == "1") and "--dry-run" not in sys.argv
out = {"now": now.strftime("%Y-%m-%d %H:%M IST"), "execute": EXECUTE, "positions": []}


def done():
    print(json.dumps(out, indent=1, default=str)); sys.exit(0)


def req(u, body=None, method=None, proxy=False):
    return api.call(u, body=body, method=method, proxy=True if proxy else False, timeout=20)


def tick(p, t=0.05):
    return round(max(round(p / t) * t, t), 2)


def uquote(key):
    d = req("https://api.upstox.com/v2/market-quote/quotes?instrument_key=" + urllib.parse.quote(key, safe="")).get("data") or {}
    for v in d.values():
        try: bid = float(v["depth"]["buy"][0]["price"]) or None
        except Exception: bid = None
        return float(v.get("last_price") or 0), bid
    return 0, None


if EXECUTE:
    if not market_calendar.is_trading_day(now.date()) or now.weekday() >= 5:
        out["skipped"] = "not a trading day"; done()
    if not (iroutes.SQUAREOFF_AT if now.weekday() != 4 else datetime.time(15, 6)) <= now.time() <= datetime.time(15, 29):
        out["skipped"] = "outside the 3m square-off window"; done()

state = iroutes.load()
recs = {k: v for k, v in (state.get("positions") or {}).items() if (v or {}).get("route") == "3m"}
out["tracked_3m"] = list(recs)
if not recs:
    out["action"] = "no tracked 3m positions"; done()

# ---- Upstox ----
pr = req("https://api.upstox.com/v2/portfolio/short-term-positions")
upos = pr.get("data") if pr.get("status") == "success" else None
uord = (req("https://api.upstox.com/v2/order/retrieve-all").get("data") or []) if upos is not None else []
if upos is None: out["upstox"] = "positions unreadable; nothing done on Upstox"

import broker_kite as bk
kpos = kord = None
try:
    import kite_disabled as _kd
    if _kd.active():
        out["kite"] = "kite disabled by user 21 Nov"; kpos = None
    elif bk.valid(): kpos = bk.positions(strict=True); kord = bk.orders(strict=True)
    else: out["kite"] = "token invalid; nothing done on Kite"
except Exception as e:
    out["kite"] = "book unreadable (%s); nothing done on Kite" % type(e).__name__; kpos = None


def _uqty(key):
    r2 = req("https://api.upstox.com/v2/portfolio/short-term-positions")
    for p in (r2.get("data") or []):
        if p.get("instrument_token") == key and (p.get("product") or "").upper() == "I": return int(p.get("quantity") or 0)
    return 0 if r2.get("status") == "success" else None


def _kqty(ex, sym):
    try:
        for p in bk.positions(strict=True):
            if p["exchange"] == ex and p["trading_symbol"] == sym and (p.get("product") or "").upper() == "MIS": return int(p["quantity"])
        return 0
    except Exception: return None


for k, rec in recs.items():
    r = {"key": k, "symbol": rec.get("symbol"), "broker": rec.get("broker")}; out["positions"].append(r)
    try:
        if k.startswith("upstox|"):
            if upos is None: r["action"] = "skip: Upstox unreadable"; continue
            key = k.split("|", 1)[1]
            row = next((p for p in upos if p.get("instrument_token") == key and (p.get("product") or "").upper() == "I"), None)
            q = int((row or {}).get("quantity") or 0)
            r["qty"] = q
            if q <= 0:
                r["action"] = "flat; route record dropped"; state["positions"].pop(k, None); continue
            if not trade_lock.is_opt(trade_lock.sym_of(row)) or trade_lock.underlying(trade_lock.sym_of(row)) not in iroutes.INDEXES:
                r["action"] = "skip: not an index option"; continue
            working = [o for o in uord if o.get("instrument_token") == key and o.get("transaction_type") == "SELL"
                       and (o.get("status") or "").lower() in ("open", "trigger pending") and (o.get("product") or "").upper() == "I"]
            ltp, bid = uquote(key); px = tick(bid if bid else ltp * 0.98)
            r["plan"] = {"cancel": [o.get("order_id") for o in working], "sell_qty": q, "limit": px, "product": "I"}
            if not EXECUTE: r["action"] = "PLAN (no orders)"; continue
            for o in working: req("https://api-hft.upstox.com/v2/order/cancel?order_id=" + o["order_id"], None, "DELETE", proxy=True)
            time.sleep(2); q2 = _uqty(key)
            if q2 is None: r["action"] = "position unreadable after cancel; not sold (broker MIS square-off is the backstop)"; continue
            if q2 <= 0: r["action"] = "flat after cancel (stop filled)"; state["positions"].pop(k, None); continue
            for attempt, pxa in ((1, px), (2, tick(ltp * 0.95))):
                res = req("https://api-hft.upstox.com/v2/order/place", {"quantity": q2, "product": "I", "validity": "DAY", "price": pxa, "tag": "sq3m",
                          "instrument_token": key, "order_type": "LIMIT", "transaction_type": "SELL", "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False}, "POST", proxy=True)
                oid = (res.get("data") or {}).get("order_id"); st = ""
                for _ in range(6):
                    if not oid: break
                    time.sleep(10); st = ((req("https://api.upstox.com/v2/order/details?order_id=" + oid).get("data") or {}).get("status") or "")
                    if st in ("complete", "rejected", "cancelled"): break
                if oid and st not in ("complete", "rejected", "cancelled"):
                    req("https://api-hft.upstox.com/v2/order/cancel?order_id=" + oid, None, "DELETE", proxy=True)
                q2 = _uqty(key)
                r["attempt%d" % attempt] = {"order_id": oid, "status": st, "limit": pxa, "left": q2}
                if q2 == 0: break
                if q2 is None: break
            r["action"] = "SQUARED OFF" if q2 == 0 else "NOT fully squared off (left %s) - broker MIS square-off is the backstop" % q2
            if q2 == 0: state["positions"].pop(k, None)
        else:
            if kpos is None: r["action"] = "skip: Kite unreadable"; continue
            _, ex, sym = k.split("|", 2)
            row = next((p for p in kpos if p["exchange"] == ex and p["trading_symbol"] == sym and (p.get("product") or "").upper() == "MIS"), None)
            q = int((row or {}).get("quantity") or 0); r["qty"] = q
            if q <= 0:
                r["action"] = "flat; route record dropped"; state["positions"].pop(k, None); continue
            working = [o for o in (kord or []) if o["trading_symbol"] == sym and o["transaction_type"] == "SELL"
                       and o["status"] in ("open", "trigger pending") and (o.get("kite_product") or "").upper() == "MIS"]
            ukey = bk.to_upstox(ex, sym)
            ltp, bid = uquote(ukey) if ukey else (float(row.get("last_price") or 0), None)
            px = bid if bid else ltp * 0.98
            r["plan"] = {"cancel": [o["order_id"] for o in working], "sell_qty": q, "limit": px, "product": "MIS"}
            if not EXECUTE: r["action"] = "PLAN (no orders)"; continue
            for o in working: bk.cancel(o["order_id"])
            time.sleep(2); q2 = _kqty(ex, sym)
            if q2 is None: r["action"] = "position unreadable after cancel; not sold (broker MIS square-off is the backstop)"; continue
            if q2 <= 0: r["action"] = "flat after cancel (stop filled)"; state["positions"].pop(k, None); continue
            for attempt, pxa in ((1, px), (2, ltp * 0.95)):
                oid = bk.place(ex, sym, "SELL", q2, pxa, tag="sq3m", product="MIS"); ks = None
                for _ in range(6):
                    if not oid: break
                    time.sleep(10); ks = bk.order_status(oid)
                    if ks and ks["status"] in ("complete", "rejected", "cancelled"): break
                if oid and not (ks and ks["status"] in ("complete", "rejected", "cancelled")): bk.cancel(oid)
                q2 = _kqty(ex, sym)
                r["attempt%d" % attempt] = {"order_id": oid, "status": (ks or {}).get("status"), "limit": pxa, "left": q2}
                if q2 == 0 or q2 is None: break
            r["action"] = "SQUARED OFF" if q2 == 0 else "NOT fully squared off (left %s) - broker MIS square-off is the backstop" % q2
            if q2 == 0: state["positions"].pop(k, None)
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, e)
if EXECUTE:
    try: iroutes.save(state)
    except Exception as e: out["state_error"] = "%s: %s" % (type(e).__name__, e)
done()
