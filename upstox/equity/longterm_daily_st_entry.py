#!/usr/bin/env python3
"""Live entry for the long-term daily Supertrend MTF sleeve (t697u / t698u).

Places nothing unless ALL of these are true:
  * LONGTERM_DST_EXECUTE=1
  * --execute is passed
  * the clock is inside 09:20-15:00 IST on an NSE trading day, from 17 Nov 2026

Default is --dry-run. Does not import auto_entry or risk_manager.

Flow, per approved name, per broker:
  1. Re-check the 16 Nov daily setup still holds.
  2. Skip if already held on that broker (no double-buy).
  3. Skip and alert if LTP has gapped more than 3% above the 16 Nov close.
  4. Recompute quantity from a live MTF margin quote so margin stays <= Rs 50,000.
  5. LIMIT a small buffer above LTP, product MTF, tag longterm_dst; poll for the fill.
  6. After a fill, place a protective SL at the daily Supertrend. Existing
     kite-mtf-stop-trail / upstox-mtf-stop-trail routines then trail that stop
     to the completed daily Supertrend and never loosen it.

These are delivery MTF positions, not intraday. They are excluded from same-day
square-off. Tag longterm_dst is exempt from the Monday 9 Nov MTF caps only;
this sleeve caps itself at 3 names and Rs 50k margin per name per broker.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EQ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, EQ)

import longterm_daily_st as lt  # noqa: E402

IST = ZoneInfo("Asia/Calcutta")
ALERT_LOG = os.path.join(lt.OUT_DIR, "alerts.jsonl")
RUN_DIR = os.path.join(lt.OUT_DIR, "runs")
BROKERS = ("upstox", "kite")


def _sanitize(msg):
    return lt._sanitize(msg)


def alert(kind, text, extra=None, wake=True):
    """Log an alert (gap skip, reject, missing stop). Print a clear ALERT line."""
    now = dt.datetime.now(IST)
    rec = {
        "ts": now.isoformat(timespec="seconds"),
        "kind": kind,
        "text": text,
        "wake": bool(wake),
    }
    if extra:
        rec["extra"] = extra
    os.makedirs(lt.OUT_DIR, exist_ok=True)
    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    print("ALERT: %s" % text)
    return rec


def load_approved(path=None):
    path = path or lt.APPROVED_PATH
    with open(path) as f:
        data = json.load(f)
    names = list(data.get("names") or [])
    if not names:
        raise RuntimeError("approved shortlist is empty: %s" % path)
    return data, names


def ltp_of(symbol, key=None):
    """Last traded price from an Upstox quote. None if unreadable."""
    import api
    import urllib.parse
    km = key
    if not km:
        try:
            import mtf_sizing as ms
            km = (ms.keymap() or {}).get(symbol)
        except Exception:
            km = None
    if not km:
        return None, None
    try:
        q = urllib.parse.quote(km, safe="")
        data = api.get("/v2/market-quote/quotes?instrument_key=" + q).get("data") or {}
        row = data.get(km) or (next(iter(data.values())) if data else {}) or {}
        ltp = float(row.get("last_price") or 0) or None
        return ltp, row
    except Exception as e:
        return None, {"error": "%s: %s" % (type(e).__name__, _sanitize(e))}


def held_on(book, broker, symbol):
    st = (book or {}).get(broker) or {}
    sym = (symbol or "").upper()
    if sym in {s.upper() for s in (st.get("held") or {})}:
        return True
    qty = st.get("qty") or {}
    if int(qty.get(symbol) or qty.get(sym) or 0) > 0:
        return True
    return False


def resize_from_quote(broker, symbol, price, probe_qty):
    """Recompute whole-share qty so live MTF margin stays <= Rs 50,000."""
    import mtf_sizing as ms
    probe = max(int(probe_qty or 1), 1)
    q = ms.margin_quote(broker, symbol, probe, price)
    if not q.get("ok"):
        return 0, None, "quote failed: %s" % _sanitize(q.get("why") or "no quote"), q
    margin = float(q["margin"])
    notional = float(q.get("notional") or (probe * price))
    rate = (margin / notional) if notional else None
    if not rate or rate <= 0:
        return 0, margin, "margin rate unreadable", q
    qty, est, _, _, note = lt.size_qty(lt.CAPITAL_PER_STOCK, price, rate)
    if qty < 1:
        return 0, est, note, q
    if qty != probe:
        q2 = ms.margin_quote(broker, symbol, qty, price)
        if q2.get("ok"):
            m2 = float(q2["margin"])
            # shrink if the fresh quote came in over the cap
            while qty > 1 and m2 > lt.CAPITAL_PER_STOCK + 1e-6:
                qty -= 1
                q2 = ms.margin_quote(broker, symbol, qty, price)
                if not q2.get("ok"):
                    break
                m2 = float(q2["margin"])
            if q2.get("ok") and m2 <= lt.CAPITAL_PER_STOCK + 1e-6:
                return qty, m2, "live quote ok", q2
            if q2.get("ok") and m2 > lt.CAPITAL_PER_STOCK + 1e-6:
                return 0, m2, "live margin still exceeds Rs 50,000 after shrink", q2
        return qty, est, "resized off live rate; confirming quote failed: %s" % _sanitize(q2.get("why")), q
    if margin > lt.CAPITAL_PER_STOCK + 1e-6:
        return 0, margin, "live margin exceeds Rs 50,000", q
    return qty, margin, "live quote ok", q


def _upstox_place(symbol, key, qty, price, dry):
    import api
    import trade_lock
    body = {
        "quantity": int(qty),
        "product": "MTF",
        "validity": "DAY",
        "price": float(price),
        "tag": lt.TAG,
        "instrument_token": key,
        "order_type": "LIMIT",
        "transaction_type": "BUY",
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }
    ok, reasons, adj = trade_lock.check_order({
        "trading_symbol": symbol + "-EQ",
        "instrument_token": key,
        "transaction_type": "BUY",
        "order_type": "LIMIT",
        "product": "MTF",
        "quantity": int(qty),
        "price": float(price),
        "tag": lt.TAG,
        "_trend_ok": True,
        "_in_loss": False,
    }, broker="upstox")
    if not ok or adj <= 0:
        return None, 0, "trade_lock refused: %s" % "; ".join(reasons), {"allowed": ok, "reasons": reasons}
    if dry:
        return None, int(adj), "dry-run", {"allowed": True, "reasons": reasons, "adjusted_qty": adj}
    resp = api.call("https://api-hft.upstox.com/v2/order/place", body, method="POST", proxy=True, timeout=25)
    oid = (resp.get("data") or {}).get("order_id") if isinstance(resp, dict) else None
    if not oid:
        # Recover from the book by tag+symbol so a timeout cannot be retried blindly.
        try:
            book = api.get("/v2/order/retrieve-all") or []
            if isinstance(book, dict):
                book = book.get("data") or []
            hits = [o for o in book
                    if (o.get("tag") or "") == lt.TAG
                    and (o.get("trading_symbol") or "").replace("-EQ", "") == symbol
                    and (o.get("transaction_type") or "").upper() == "BUY"
                    and (o.get("product") or "").upper() == "MTF"]
        except Exception as e:
            return None, int(adj), "write unclear and order book unreadable (%s) - CHECK THE BROKER before retrying" % _sanitize(e), resp
        if hits:
            return hits[0].get("order_id"), int(adj), "recovered from the order book after an unclear write", resp
        return None, int(adj), "no order id: %s" % _sanitize(str((resp or {}).get("body") or resp)[:200]), resp
    return oid, int(adj), "placed", resp


def _kite_place(symbol, qty, price, dry):
    import broker_kite as bk
    import trade_lock
    if not bk.valid():
        return None, 0, "kite invalid: %s" % _sanitize(bk.why()), None
    exch = "NSE"
    ok, reasons, adj = trade_lock.check_order({
        "trading_symbol": symbol,
        "tradingsymbol": symbol,
        # 17 Nov 2026: same token form as mtf_dual_entry / broker_kite holdings. Without it
        # trade_lock matched every tokenless Kite MTF row and capped the buy at SRF's 6 shares.
        "instrument_token": "KITE|%s|%s" % (exch, symbol),
        "transaction_type": "BUY",
        "order_type": "LIMIT",
        "product": "MTF",
        "quantity": int(qty),
        "price": float(price),
        "tag": lt.TAG,
        "_trend_ok": True,
        "_in_loss": False,
    }, broker="kite")
    if not ok or adj <= 0:
        return None, 0, "trade_lock refused: %s" % "; ".join(reasons), {"allowed": ok, "reasons": reasons}
    if dry:
        return None, int(adj), "dry-run", {"allowed": True, "reasons": reasons, "adjusted_qty": adj}
    oid = bk.place(exch, symbol, "BUY", int(adj), float(price), tag=lt.TAG, product="MTF")
    if not oid:
        return None, int(adj), "kite place returned no order id", None
    return oid, int(adj), "placed", {"order_id": oid}


def _poll_upstox(oid, budget=90.0, step=4.0):
    import api
    end = time.time() + budget
    last = {}
    while True:
        try:
            last = (api.get("/v2/order/details?order_id=" + oid).get("data") or {}) or last
        except Exception:
            pass
        st = (last.get("status") or "").lower()
        filled = int(last.get("filled_quantity") or 0)
        qty = int(last.get("quantity") or 0)
        avg = float(last.get("average_price") or 0)
        if st in ("complete", "cancelled", "rejected"):
            return {"status": st, "filled": filled, "qty": qty, "avg": avg, "raw": last, "settled": True}
        if time.time() >= end:
            return {"status": st or "timeout", "filled": filled, "qty": qty, "avg": avg, "raw": last, "settled": False}
        time.sleep(step)


def _poll_kite(oid, budget=90.0, step=4.0):
    import broker_kite as bk
    end = time.time() + budget
    last = {}
    while True:
        try:
            last = bk.order_status(oid) or last
        except Exception:
            pass
        st = (last.get("status") or "").lower()
        filled = int(last.get("filled") or last.get("filled_quantity") or 0)
        qty = int(last.get("quantity") or 0)
        avg = float(last.get("avg") or last.get("average_price") or 0)
        if st in ("complete", "cancelled", "rejected"):
            return {"status": st, "filled": filled, "qty": qty, "avg": avg, "raw": last, "settled": True}
        if time.time() >= end:
            return {"status": st or "timeout", "filled": filled, "qty": qty, "avg": avg, "raw": last, "settled": False}
        time.sleep(step)


def _cancel_open(broker, oid):
    try:
        if broker == "upstox":
            import api
            api.call("https://api-hft.upstox.com/v2/order/cancel?order_id=" + oid, None, method="DELETE", proxy=True, timeout=25)
        else:
            import broker_kite as bk
            k = bk.client()
            k.cancel_order(oid, variety=k.VARIETY_REGULAR)
        return True, "cancelled remainder"
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, _sanitize(e))


def _place_stop_upstox(symbol, key, qty, trigger, dry):
    import api
    import trade_lock
    t = lt.tick_for_price(trigger)
    trig = lt.snap_price(trigger, t)
    limit = lt.snap_price(trig * 0.99, t)
    if limit >= trig:
        limit = round(trig - t, 2)
    order = {
        "trading_symbol": symbol + "-EQ",
        "instrument_token": key,
        "transaction_type": "SELL",
        "order_type": "SL",
        "product": "MTF",
        "quantity": int(qty),
        "price": limit,
        "trigger_price": trig,
        "tag": "dstsl",
    }
    ok, reasons, adj = trade_lock.check_order(order, broker="upstox")
    if not ok or adj <= 0:
        return None, "trade_lock refused stop: %s" % "; ".join(reasons)
    if dry:
        return None, "dry-run would place SL SELL trigger %s limit %s qty %s" % (trig, limit, adj)
    # Retire any leftover overnight GTT so two stops cannot both fire.
    try:
        import gtt_overnight as gtt
        gtt.cancel_for_day("upstox", symbol, key=key)
    except Exception:
        pass
    resp = api.call("https://api-hft.upstox.com/v2/order/place", {
        "quantity": int(adj), "product": "MTF", "validity": "DAY", "price": limit, "tag": "dstsl",
        "instrument_token": key, "order_type": "SL", "transaction_type": "SELL",
        "disclosed_quantity": 0, "trigger_price": trig, "is_amo": False,
    }, method="POST", proxy=True, timeout=25)
    oid = (resp.get("data") or {}).get("order_id") if isinstance(resp, dict) else None
    if not oid:
        return None, "stop not placed: %s" % _sanitize(str((resp or {}).get("body") or resp)[:200])
    return oid, "placed SL trigger %s" % trig


def _place_stop_kite(symbol, qty, trigger, dry):
    import broker_kite as bk
    if not bk.valid():
        return None, "kite invalid: %s" % _sanitize(bk.why())
    tick = bk.tick_for("NSE", symbol) or lt.tick_for_price(trigger)
    trig = bk.snap(trigger, tick)
    limit = bk.snap(trig - max(tick, trig * 0.005), tick)
    if dry:
        return None, "dry-run would place SL SELL trigger %s limit %s qty %s" % (trig, limit, qty)
    try:
        import gtt_overnight as gtt
        gtt.cancel_for_day("kite", symbol)
    except Exception:
        pass
    oid = bk.place("NSE", symbol, "SELL", int(qty), limit, order_type="SL", trigger=trig, tag="dstsl", product="MTF")
    if not oid:
        return None, "kite stop not placed"
    return oid, "placed SL trigger %s" % trig


def _remember(broker, symbol, rec, dry):
    if dry:
        return
    os.makedirs(lt.OUT_DIR, exist_ok=True)
    try:
        data = json.load(open(lt.STATE_PATH))
    except Exception:
        data = {"sleeve": lt.TAG, "positions": {"upstox": {}, "kite": {}}}
    data.setdefault("positions", {}).setdefault(broker, {})
    data["positions"][broker][symbol] = rec
    data["sleeve"] = lt.TAG
    data["updated_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")
    tmp = lt.STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, lt.STATE_PATH)
    # Also mark the name as managed so the trail routines will ratchet it.
    for path in lt.MANAGED_PATHS:
        try:
            cur = json.load(open(path))
        except FileNotFoundError:
            if not os.path.isdir(os.path.dirname(path)):
                continue
            cur = {"managed": [], "note": "longterm_dst sleeve"}
        except Exception:
            continue
        managed = list(cur.get("managed") or [])
        if symbol not in managed:
            managed.append(symbol)
            cur["managed"] = managed
            try:
                with open(path, "w") as f:
                    json.dump(cur, f, indent=1)
            except Exception:
                pass


def plan_name(row, book, now, live_quotes=True):
    """Build a per-broker plan for one approved name. No orders."""
    symbol = row["symbol"]
    out = {
        "symbol": symbol,
        "ref_close": row.get("close"),
        "stop": row.get("stop") or row.get("st"),
        "condition": row.get("condition"),
        "setup_holds": lt.setup_holds(row),
        "legs": {},
        "skip": None,
    }
    if not out["setup_holds"]:
        out["skip"] = "16 Nov daily setup no longer holds"
        return out
    ltp, quote = ltp_of(symbol, row.get("key"))
    out["ltp"] = ltp
    if ltp is None:
        # After hours a missing LTP is not a gap skip; use the 16 Nov close for the dry plan.
        ltp = float(row.get("close") or 0) or None
        out["ltp"] = ltp
        out["ltp_note"] = "live LTP unreadable; using 16 Nov close for the plan"
        if quote and quote.get("error"):
            out["ltp_error"] = quote.get("error")
    if ltp:
        skip, why = lt.gap_skip(ltp, row.get("close"))
        out["gap_pct"] = lt.upside_gap_pct(ltp, row.get("close"))
        if skip:
            out["skip"] = why
            out["alert"] = True
            return out
        out["limit"] = lt.limit_buy_price(ltp)
    for broker in BROKERS:
        leg = {"broker": broker, "action": "plan"}
        if held_on(book, broker, symbol):
            leg["action"] = "SKIP"
            leg["why"] = "already held on %s — not double-buying" % broker
            out["legs"][broker] = leg
            continue
        scan_qty = int(row.get("%s_qty_scan" % broker) or 0)
        if not ltp:
            leg["action"] = "SKIP"
            leg["why"] = "no price to size or place a LIMIT"
            out["legs"][broker] = leg
            continue
        if live_quotes:
            qty, margin, note, raw = resize_from_quote(broker, symbol, ltp, scan_qty or 1)
            leg["qty"] = qty
            leg["margin"] = margin
            leg["size_note"] = note
            if raw and not raw.get("ok"):
                leg["quote_error"] = _sanitize(raw.get("why"))
        else:
            qty, margin, _, _, note = lt.size_qty(lt.CAPITAL_PER_STOCK, ltp, None)
            # no rate -> invalid; fall back to scan qty as a display only
            qty = scan_qty
            margin = row.get("%s_live_margin_scan" % broker)
            note = "scan sizes (live quotes skipped)"
            leg["qty"] = qty
            leg["margin"] = margin
            leg["size_note"] = note
        sleeve = lt.sleeve_symbols(broker)
        blocks = lt.longterm_dst_blocks(sleeve, symbol, leg.get("margin") or 0,
                                        already_held_name=False)
        if blocks:
            leg["action"] = "SKIP"
            leg["why"] = "; ".join(blocks)
        elif not leg.get("qty"):
            leg["action"] = "SKIP"
            leg["why"] = leg.get("size_note") or "qty is 0"
        else:
            leg["action"] = "BUY"
            leg["limit"] = out.get("limit")
            leg["stop"] = out["stop"]
            leg["tag"] = lt.TAG
            leg["product"] = "MTF"
        out["legs"][broker] = leg
    return out


def execute_plan(plan, key, dry, do_send):
    """Place, poll, and arm stops when do_send is True. Dry-run never sends."""
    symbol = plan["symbol"]
    results = {"symbol": symbol, "legs": {}}
    if plan.get("skip"):
        if plan.get("alert"):
            alert("gap_skip", "%s skipped: %s" % (symbol, plan["skip"]),
                  extra={"symbol": symbol, "ltp": plan.get("ltp"), "ref_close": plan.get("ref_close")})
        results["skip"] = plan["skip"]
        return results
    for broker in BROKERS:
        leg = dict(plan.get("legs") or {}).get(broker) or {}
        rec = dict(leg)
        if leg.get("action") != "BUY" or not do_send:
            if dry and leg.get("action") == "BUY":
                rec["sent"] = False
                rec["why"] = rec.get("why") or "dry-run; no order placed"
                rec["would_place"] = {
                    "product": "MTF", "tag": lt.TAG, "qty": leg.get("qty"),
                    "limit": leg.get("limit"), "stop": leg.get("stop"),
                }
            results["legs"][broker] = rec
            continue
        qty = int(leg["qty"])
        price = float(leg["limit"])
        try:
            if broker == "upstox":
                oid, adj, why, _raw = _upstox_place(symbol, key, qty, price, dry=False)
            else:
                oid, adj, why, _raw = _kite_place(symbol, qty, price, dry=False)
        except Exception as e:
            oid, adj, why = None, 0, "%s: %s" % (type(e).__name__, _sanitize(e))
        rec["order_id"] = oid
        rec["place_why"] = why
        rec["qty_sent"] = adj
        if not oid:
            rec["action"] = "REJECT"
            alert("reject", "%s %s BUY rejected: %s" % (broker, symbol, why),
                  extra={"broker": broker, "symbol": symbol})
            results["legs"][broker] = rec
            continue
        poll = _poll_upstox(oid) if broker == "upstox" else _poll_kite(oid)
        rec["fill"] = {k: poll[k] for k in ("status", "filled", "qty", "avg", "settled")}
        filled = int(poll.get("filled") or 0)
        if not poll.get("settled") and filled < int(poll.get("qty") or adj or 0):
            ok_c, why_c = _cancel_open(broker, oid)
            rec["cancel_remainder"] = {"ok": ok_c, "why": why_c}
            # re-read fill after cancel
            poll2 = _poll_upstox(oid, budget=20, step=2) if broker == "upstox" else _poll_kite(oid, budget=20, step=2)
            rec["fill"] = {k: poll2[k] for k in ("status", "filled", "qty", "avg", "settled")}
            filled = int(poll2.get("filled") or filled)
        if filled <= 0:
            rec["action"] = "UNFILLED"
            alert("unfilled", "%s %s BUY %s did not fill" % (broker, symbol, oid),
                  extra={"broker": broker, "symbol": symbol, "order_id": oid})
            results["legs"][broker] = rec
            continue
        rec["action"] = "FILLED"
        rec["filled_qty"] = filled
        rec["avg"] = poll.get("avg")
        stop_px = float(plan["stop"])
        try:
            if broker == "upstox":
                sid, swhy = _place_stop_upstox(symbol, key, filled, stop_px, dry=False)
            else:
                sid, swhy = _place_stop_kite(symbol, filled, stop_px, dry=False)
        except Exception as e:
            sid, swhy = None, "%s: %s" % (type(e).__name__, _sanitize(e))
        rec["stop_order"] = sid
        rec["stop_why"] = swhy
        rec["stop"] = stop_px
        if not sid:
            alert("stop_failed", "%s %s fill is live but the Supertrend stop was not placed: %s"
                  % (broker, symbol, swhy),
                  extra={"broker": broker, "symbol": symbol, "qty": filled, "stop": stop_px})
        _remember(broker, symbol, {
            "qty": filled,
            "avg": rec.get("avg"),
            "stop": stop_px,
            "entry_order": oid,
            "stop_order": sid,
            "scan_close": plan.get("ref_close"),
            "scan_date": "2026-11-16",
            "tag": lt.TAG,
        }, dry=False)
        results["legs"][broker] = rec
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Long-term daily ST MTF entry (default dry-run)")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--execute", action="store_true", default=False,
                    help="Place orders only when LONGTERM_DST_EXECUTE=1 and the window is open.")
    ap.add_argument("--no-quotes", action="store_true")
    ap.add_argument("--no-book", action="store_true")
    ap.add_argument("--approved", default=lt.APPROVED_PATH)
    ap.add_argument("--now", default=None, help="ISO timestamp override (tests / preview).")
    args = ap.parse_args(argv)

    if args.now:
        now = dt.datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        now = now.astimezone(IST)
    else:
        now = dt.datetime.now(IST)

    flag = lt.execute_flag_on()
    window_ok, window_why = lt.entry_window(now)
    # A send is allowed only when the env flag, --execute, and the session window all agree.
    do_send = bool(args.execute and flag and window_ok)
    dry = (not do_send)

    os.makedirs(RUN_DIR, exist_ok=True)
    print("Long-term daily Supertrend MTF entry")
    print("Time: %s Asia/Calcutta" % now.strftime("%Y-%m-%d %H:%M:%S"))
    print("Mode: %s" % ("LIVE SEND" if do_send else "DRY-RUN (no orders)"))
    print("  LONGTERM_DST_EXECUTE=%s" % ("1" if flag else "0"))
    print("  --execute=%s" % args.execute)
    print("  window: %s (%s)" % ("OPEN" if window_ok else "CLOSED", window_why))
    if args.execute and not flag:
        print("  --execute ignored: LONGTERM_DST_EXECUTE is not set.")
    if args.execute and flag and not window_ok:
        print("  --execute ignored: %s." % window_why)

    approved, names = load_approved(args.approved)
    print()
    print("Approved names (t697u/t698u, scan 16 Nov 2026): %s" % ", ".join(r["symbol"] for r in names))
    print("  Capital: Rs %s margin per name per broker, max %d names."
          % (f"{lt.CAPITAL_PER_STOCK:,}", lt.MAX_TRADES))
    print("  Product MTF, tag %s, LIMIT near LTP, stop at daily Supertrend." % lt.TAG)

    book = {"upstox": None, "kite": None, "errors": {}}
    if not args.no_book:
        print()
        print("Reading live MTF books (read only)...")
        try:
            book = lt.live_book()
        except Exception as e:
            book = {"upstox": None, "kite": None, "errors": {"both": _sanitize(e)}}
        for broker in BROKERS:
            st = book.get(broker)
            if not st:
                print("  %s: FAILED %s" % (broker, (book.get("errors") or {}).get(broker)))
                continue
            print("  %s: free %s, MTF names %s"
                  % (broker, lt.fmt_inr(st.get("free")), sorted((st.get("held") or {}).keys())))

    plans = []
    for row in names:
        print()
        p = plan_name(row, book, now, live_quotes=not args.no_quotes)
        plans.append(p)
        print("  %s  close %s  ST stop %s  LTP %s  gap %s  setup %s"
              % (p["symbol"], lt.fmt_inr(p.get("ref_close")), lt.fmt_inr(p.get("stop")),
                 lt.fmt_inr(p.get("ltp")),
                 ("—" if p.get("gap_pct") is None else "%.2f%%" % p["gap_pct"]),
                 "HOLDS" if p.get("setup_holds") else "BROKEN"))
        if p.get("skip"):
            print("    SKIP: %s" % p["skip"])
            if p.get("alert") and dry:
                alert("gap_skip", "%s would be skipped: %s" % (p["symbol"], p["skip"]),
                      extra={"symbol": p["symbol"], "ltp": p.get("ltp")}, wake=False)
            continue
        for broker in BROKERS:
            leg = p["legs"].get(broker) or {}
            print("    %s: %s  qty=%s  margin=%s  limit=%s  %s"
                  % (broker, leg.get("action"), leg.get("qty"),
                     lt.fmt_inr(leg.get("margin")), lt.fmt_inr(leg.get("limit")),
                     leg.get("why") or leg.get("size_note") or ""))

    results = []
    for p, row in zip(plans, names):
        results.append(execute_plan(p, row.get("key"), dry=dry, do_send=do_send))

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Calcutta",
        "dry_run": dry,
        "execute_requested": bool(args.execute),
        "execute_flag": flag,
        "window_ok": window_ok,
        "window_why": window_why,
        "sent": do_send,
        "approved": args.approved,
        "plans": plans,
        "results": results,
        "book_errors": book.get("errors") or {},
    }
    out_path = os.path.join(RUN_DIR, "entry_%s.json" % now.strftime("%Y%m%dT%H%M%S"))
    def conv(o):
        if isinstance(o, dt.date) and not isinstance(o, dt.datetime):
            return o.isoformat()
        if isinstance(o, dt.datetime):
            return o.isoformat(timespec="seconds")
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(type(o))
    with open(out_path, "w") as f:
        json.dump(payload, f, default=conv, indent=2)
    print()
    print("Wrote", out_path)
    print("No orders placed." if dry else "Live send attempted; see results above.")
    return 0 if dry or do_send else 0


if __name__ == "__main__":
    sys.exit(main())
