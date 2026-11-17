#!/usr/bin/env python3
"""One add per winning index-option position, both brokers (user-authorized 12 Nov 2026 06:16 IST).

Runs only when full_auto_policy.flag("adds") is on (env FULL_AUTO_ADDS=1 or full_auto_flags.json).
Scope: long NIFTY / BANKNIFTY / SENSEX options. Stock options and crude are NOT added to
(stock options: R11 1-lot cap and the weekly list's adds-off rule; crude: 1-lot book).
Window 09:30-15:25 IST on trading days (t673u, 16 Nov 2026; was 09:20-14:45); no add to a 3m-route
(intraday) position after 15:05, and no add on a Friday after 15:05 (15:10 flatten). Rule: full_auto_policy.add_decision. Every order still
goes through trade_lock.check_order (R1-R7, R10 capital) and is a LIMIT at the ask.
After a fill the existing SELL stop is re-sized to the broker's new quantity at once
(trigger unchanged, so still >= breakeven); a failure is logged to sl_confirm_failures.json.
--dry-run: no orders, no state writes, ignores the window.
"""
import datetime, json, os, sys, time, urllib.parse

D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
import api, market_calendar, trade_lock
import full_auto_policy as fap
import full_auto_feed as faf
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DRY = "--dry-run" in sys.argv
LEDGER = os.path.join(D, "full_auto_adds.json")
INDEX = {"NIFTY", "BANKNIFTY", "SENSEX"}
now = datetime.datetime.now(IST)
out = {"now": now.strftime("%Y-%m-%d %H:%M IST"), "dry_run": DRY, "adds_flag": fap.flag("adds"), "positions": []}


def done():
    print(json.dumps(out, indent=1, default=str)); sys.exit(0)


def req(u, body=None, method=None):
    return api.call(u, body=body, method=method, proxy=True, timeout=20)


def tick(p, t=0.05):
    return round(max(round(p / t) * t, t), 2)


def ledger():
    try: return json.load(open(LEDGER))
    except Exception: return {}


def master_row(key):
    seg = key.split("|")[0]
    exch = {"NSE_FO": "NSE", "BSE_FO": "BSE"}.get(seg)
    if not exch: return None
    for r in faf._rows(exch):
        if r.get("instrument_key") == key: return r
    return None


def quote(key):
    d = req("https://api.upstox.com/v2/market-quote/quotes?instrument_key=" + urllib.parse.quote(key, safe="")).get("data") or {}
    for v in d.values():
        try: ask = float(v["depth"]["sell"][0]["price"]) or None
        except Exception: ask = None
        return float(v.get("last_price") or 0), ask
    return 0, None


def live_stop(orders, match):
    sls = [o for o in orders if match(o) and o.get("transaction_type") == "SELL"
           and (o.get("status") or "").lower() in ("trigger pending", "open") and o.get("order_type") in ("SL", "SL-M")]
    return max(sls, key=lambda o: float(o.get("trigger_price") or 0)) if sls else None


if not out["adds_flag"]:
    out["skipped"] = "adds flag is off"; done()
if not DRY and not market_calendar.is_trading_day(now.date()):
    out["skipped"] = "market closed"; done()
if not DRY and (now.weekday() >= 5 or not (datetime.time(9, 30) <= now.time() <= datetime.time(15, 25))):
    out["skipped"] = "outside 09:30-15:25 IST"; done()
import index_routes as iroutes   # t673u: per-route tracking; adds stay one per position (ledger key broker|symbol)
_RSTATE = iroutes.load()
LATE_ADD_CUTOFF = datetime.time(15, 5)

book = ledger()
rows = []
pr = req("https://api.upstox.com/v2/portfolio/short-term-positions")
if pr.get("status") == "success":
    uorders = req("https://api.upstox.com/v2/order/retrieve-all").get("data") or []
    for p in pr.get("data") or []:
        sym = p.get("trading_symbol") or ""
        if int(p.get("quantity") or 0) > 0 and trade_lock.is_opt(sym) and trade_lock.underlying(sym) in INDEX:
            rows.append(("upstox", p, uorders))
else:
    out["upstox"] = "positions unreadable; no Upstox add"
import broker_kite as bk
kpos = None
try:
    if bk.valid():
        kpos = bk.positions(strict=True); kord = bk.orders(strict=True)
        for p in kpos:
            if p["quantity"] > 0 and p["exchange"] in ("NFO", "BFO") and trade_lock.is_opt(p["trading_symbol"]) and trade_lock.underlying(p["trading_symbol"]) in INDEX:
                rows.append(("kite", p, kord))
    else:
        out["kite"] = "token invalid; no Kite add"
except Exception as e:
    out["kite"] = "book unreadable (%s); no Kite add" % type(e).__name__
    kpos = None

# 12 Nov 2026: shared daily loss cap. An add is a new buy, so it is blocked once the cap trips
# (2 losing closed trades or Rs15k realised today across both brokers). This script has no exits.
import daily_loss_cap as dlc
# 16 Nov 2026 (t669u): today's order books feed the FIFO tally (same figure as auto_entry).
LOSS_CAP = dlc.evaluate(pr.get("data") if pr.get("status") == "success" else None, kpos, now,
                        state_path=dlc.default_state_path(), persist=not DRY,
                        upstox_orders=(uorders if pr.get("status") == "success" else None),
                        kite_orders=(kord if kpos is not None else None))
out["daily_loss_cap"] = LOSS_CAP
if not LOSS_CAP["ok"]:
    out["skipped"] = LOSS_CAP["reason"]; done()

for broker, p, orders in rows:
    sym = p["trading_symbol"]; r = {"broker": broker, "sym": sym}; out["positions"].append(r)
    try:
        lk = "%s|%s" % (broker, sym)
        side = "PE" if sym.endswith("PE") else "CE"
        held = int(p["quantity"]); avg = float(p.get("average_price") or p.get("buy_price") or 0)
        if broker == "upstox":
            key = p["instrument_token"]; mr = master_row(key)
            lot = int((mr or {}).get("lot_size") or 0)
            exp = datetime.datetime.fromtimestamp(mr["expiry"] / 1000, IST).date() if mr else None
            ltp, ask = quote(key); prod = p.get("product") or "D"
            st = live_stop(orders, lambda o: o.get("instrument_token") == key)
        else:
            row = bk._index(p["exchange"])[1].get(sym) or {}
            lot = int(row.get("lot_size") or 0)
            exp = datetime.date.fromisoformat(row["expiry"]) if row.get("expiry") else None
            key = bk.to_upstox(p["exchange"], sym); ltp, ask = quote(key) if key else (p["last_price"], None)
            prod = p.get("product") or "NRML"
            st = live_stop(orders, lambda o: o.get("trading_symbol") == sym)
        if st and int(st.get("quantity") or 0) < held: st = None   # stop must cover the whole position
        _rk = iroutes.ukey(key) if broker == "upstox" else iroutes.kkey(p["exchange"], sym)
        _rec = (_RSTATE.get("positions") or {}).get(_rk) or {}
        r["route"] = _rec.get("route") or ("3m" if str(prod).upper() in iroutes.INTRADAY_PRODUCTS else "untracked")
        if not DRY and now.time() > LATE_ADD_CUTOFF and (r["route"] == "3m" or now.weekday() == 4):
            r["action"] = "no add: after 15:05 IST on a 3m intraday position or a Friday"; continue
        bar, berr = faf.underlying_1h(sym)
        dec = fap.add_decision(side=side, avg=avg, ltp=ltp, held_qty=held, lot_size=lot,
                               adds_used=1 if lk in book else 0, bar_1h=bar,
                               live_stop_trigger=float(st["trigger_price"]) if st else None,
                               days_to_expiry=(exp - now.date()).days if exp else None)
        r.update({"avg": avg, "ltp": ltp, "held": held, "lot": lot, "stop": st and st.get("trigger_price"),
                  "bar_1h": bar and {k: bar[k] for k in ("ts", "close", "ema20", "dir")}, "bar_err": berr, "decision": dec})
        if not dec["ok"]:
            continue
        px = tick(ask if ask else ltp * 1.005)
        fk = faf.fut_key(trade_lock.underlying(sym))
        order = {"trading_symbol": sym, "instrument_token": key if broker == "upstox" else "KITE|%s|%s" % (p["exchange"], sym),
                 "transaction_type": "BUY", "order_type": "LIMIT", "product": prod, "quantity": dec["qty"], "price": px,
                 "_add": True, "_signal_source": "futures", "_signal_instrument_key": fk}
        ok, why, adj = trade_lock.check_order(order, broker=broker)
        adj = (adj // lot) * lot
        r["check"] = {"allowed": ok, "reasons": why, "adjusted_qty": adj}
        if not ok or adj <= 0:
            r["action"] = "blocked by pre-trade rules"; continue
        if DRY:
            r["action"] = "DRY-RUN: would BUY %d %s LIMIT %s on %s" % (adj, sym, px, broker); continue
        book[lk] = {"ts": now.isoformat(), "qty": adj, "state": "submitting"}
        json.dump(book, open(LEDGER, "w"), indent=1)   # single-use even if the fill read fails
        oid = None; fill = 0
        if broker == "upstox":
            res = req("https://api-hft.upstox.com/v2/order/place", {"quantity": adj, "product": prod, "validity": "DAY", "price": px,
                      "tag": "fa_add", "instrument_token": key, "order_type": "LIMIT", "transaction_type": "BUY",
                      "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False}, "POST")
            oid = (res.get("data") or {}).get("order_id")
            for _ in range(12):
                if not oid: break
                time.sleep(10)
                d = req("https://api.upstox.com/v2/order/details?order_id=" + oid).get("data") or {}
                if d.get("status") in ("complete", "rejected", "cancelled"): break
            if oid and d.get("status") not in ("complete", "rejected", "cancelled"):
                req("https://api-hft.upstox.com/v2/order/cancel?order_id=" + oid, None, "DELETE")
            fill = int((d or {}).get("filled_quantity") or 0) if oid else 0
        else:
            oid = bk.place(p["exchange"], sym, "BUY", adj, px, tag="fa_add", product=prod)
            ks = None
            for _ in range(12):
                if not oid: break
                time.sleep(10); ks = bk.order_status(oid)
                if ks and ks["status"] in ("complete", "rejected", "cancelled"): break
            if oid and not (ks and ks["status"] in ("complete", "rejected", "cancelled")): bk.cancel(oid)
            fill = (ks or {}).get("filled", 0) if oid else 0
        book[lk].update({"order_id": oid, "filled": fill, "state": "filled" if fill else "unfilled"})
        json.dump(book, open(LEDGER, "w"), indent=1)
        r["order_id"] = oid; r["filled"] = fill
        if fill <= 0:
            r["action"] = "add not filled in 2 min - cancelled (add slot used)"; continue
        # Close the fill->stop gap now: resize the live stop to the broker's new quantity.
        import sl_confirm as sc
        newq = sc.position_qty(broker, sym)
        trig = float(st["trigger_price"])
        if broker == "upstox":
            mres = req("https://api-hft.upstox.com/v2/order/modify", {"order_id": st["order_id"], "quantity": newq, "validity": "DAY",
                       "price": tick(trig * 0.95), "order_type": "SL", "trigger_price": trig, "disclosed_quantity": 0}, "PUT")
            okm = mres.get("status") == "success"
        else:
            okm = bool(bk.modify(st["order_id"], newq, tick(trig * 0.95), trig))
        r["stop_resize"] = {"qty": newq, "trigger": trig, "ok": okm}
        if not okm:
            sc.record_failure(broker, sym, st["order_id"], "add filled but stop not resized to %s" % newq, qty=newq, trigger=trig)
        r["action"] = "ADDED %d %s on %s @ %s; stop %s resized to %s (%s)" % (fill, sym, broker, px, trig, newq, "ok" if okm else "FAILED")
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, e)
done()
