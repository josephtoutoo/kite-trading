#!/usr/bin/env python3
"""CRUDEOIL option entries and evening management on Kite (user-authorized 12 Nov 2026 06:16 IST).

  crude_auto.py entry [--dry-run]   needs full_auto_policy.flag("crude_entry") (CRUDE_EXECUTE=1);
                                 CRUDE_TF=15m uses completed 15m bars (default 1h)
  crude_auto.py manage [--dry-run]  stops/target/trend exit for Kite CRUDEOIL option longs, 15:25-23:30 IST
                                    (risk_manager.py owns them 09:15-15:25, so the two never overlap)

Entry signal: COMPLETED bar of the nearest CRUDEOIL future, Supertrend(10,3) + EMA20, fresh only
(CE: close>EMA20 and ST green with the previous bar not both; PE mirror). Timeframe is 1h by
default, or 15m when CRUDE_TF=15m (same signal rule). Contract: nearest monthly
CRUDEOIL option at least 8 days from expiry (the MCX square-off closes options inside 7 days), ATM
(50 step), two-sided quote with spread <= 3%. 1 lot, Kite only (Upstox disables MCX orders over its
API, UDAPI1161). One crude option position at a time, at most 2 crude entries a day shared across
both timeframes, window 10:00-22:30 IST Mon-Fri. Each timeframe tracks its own used bar so a 15m
and 1h signal cannot collide on the same timestamp. The ledger row is tagged with the timeframe.
LIMIT at the ask, cancelled after 2 minutes. On a fill: stop 20% under the
fill placed and re-read (sl_confirm.guard_kite), target +20% in risk_state.json like index options.
trade_lock.check_order still decides (R10 crude_auto: Kite, exactly 1 lot, verified capital).
Manage: the same rules as risk_manager for a Kite option: 20% floor on a new life (30% kept for
positions already open on 16 Nov 2026), stop floor reset on flatten/re-entry, +20% target roll, and with
FULL_AUTO_TREND_EXIT the 1h Supertrend-flip exit plus the upward-only ladder. Every exit SELL is
capped to the broker's quantity read just before it.
"""
import datetime, json, os, sys, time, urllib.parse, urllib.request

D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
import api, trade_lock
import full_auto_policy as fap
import full_auto_feed as faf
import position_life as pl
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import perf_rules as _prules
    _INIT_PCT = _prules.INITIAL_STOP_PCT
except Exception:
    _prules = None; _INIT_PCT = 0.30
DRY = "--dry-run" in sys.argv
MODE = next((a for a in sys.argv[1:] if a in ("entry", "manage")), "entry")
SF = os.path.join(D, "risk_state.json"); LEDGER = os.path.join(D, "crude_auto.json")
now = datetime.datetime.now(IST)
out = {"now": now.strftime("%Y-%m-%d %H:%M IST"), "mode": MODE, "dry_run": DRY,
       "crude_entry_flag": fap.flag("crude_entry"), "trend_exit_flag": fap.flag("trend_exit"),
       "ladder_flag": fap.flag("stop_ladder")}


def done():
    print(json.dumps(out, indent=1, default=str)); sys.exit(0)


def req(u, body=None, method=None):
    return api.call(u, body=body, method=method, proxy=True, timeout=20)


def quote(key):
    d = req("https://api.upstox.com/v2/market-quote/quotes?instrument_key=" + urllib.parse.quote(key, safe="")).get("data") or {}
    for v in d.values():
        dep = v.get("depth") or {}
        b = (dep.get("buy") or [{}])[0]; s = (dep.get("sell") or [{}])[0]
        return {"ltp": float(v.get("last_price") or 0), "bid": float(b.get("price") or 0), "ask": float(s.get("price") or 0),
                "bid_qty": int(b.get("quantity") or 0), "ask_qty": int(s.get("quantity") or 0), "oi": v.get("oi")}
    return {}


def mcx_master():
    f = os.path.join(D, "instruments_MCX_%s.json.gz" % now.date())
    if not os.path.exists(f):
        data = urllib.request.urlopen(urllib.request.Request("https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz",
                                      headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read()
        open(f + ".part", "wb").write(data); os.replace(f + ".part", f)
    faf._master.pop("MCX", None)
    return api.gzip_json(f)


def load(path, default):
    try: return json.load(open(path))
    except Exception: return default


def gtt_clear(sym):
    if DRY: return True, "dry-run"
    try:
        sys.path.insert(0, "/workspace/equity"); import gtt_overnight as g
        return g.cancel_for_day("kite", sym)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def entry():
    if not out["crude_entry_flag"]:
        out["skipped"] = "crude entry flag is off"; done()
    if not DRY and (now.weekday() >= 5 or not (datetime.time(10, 0) <= now.time() <= datetime.time(22, 30))):
        out["skipped"] = "outside 10:00-22:30 IST MCX entry window"; done()
    import broker_kite as bk
    if not bk.valid():
        out["skipped"] = "Kite token invalid; no crude entry"; done()
    try:
        kpos = bk.positions(strict=True)
    except Exception as e:
        out["skipped"] = "Kite book unreadable (%s)" % type(e).__name__; done()
    # 12 Nov 2026: shared daily loss cap (2 losing closed trades or Rs15k realised today, both
    # brokers). Entry only - manage() (stops, target, trend exit) is never gated.
    import daily_loss_cap as dlc
    _up = req("https://api.upstox.com/v2/portfolio/short-term-positions")
    cap = dlc.evaluate(_up.get("data") if _up.get("status") == "success" else None, kpos, now,
                       state_path=dlc.default_state_path(), persist=not DRY)
    out["daily_loss_cap"] = cap
    if not cap["ok"]:
        out["skipped"] = cap["reason"]; done()
    held = [p["trading_symbol"] for p in kpos if p["quantity"] > 0 and p["exchange"] == "MCX" and faf.root(p["trading_symbol"]) == "CRUDEOIL" and trade_lock.is_opt(p["trading_symbol"])]
    if held:
        out["skipped"] = "crude option already open: " + ", ".join(held); done()
    book = load(LEDGER, {}); day = now.date().isoformat()
    today = [x for x in book.get(day, [])]
    if len(today) >= fap.CRUDE_MAX_ENTRIES_PER_DAY:
        out["skipped"] = "already %d crude entries today" % len(today); done()
    tf = fap.parse_crude_tf()
    if not tf:
        out["skipped"] = "invalid CRUDE_TF (use 15m or 1h)"; done()
    out["tf"] = tf
    rows = mcx_master()
    fk = faf.fut_key("CRUDEOIL", rows=rows)
    if not fk:
        out["skipped"] = "no CRUDEOIL future in the MCX master"; done()
    bar = faf.bar_15m(fk) if tf == "15m" else faf.bar_1h(fk)
    cur = {"close": bar["close"], "ema20": bar["ema20"], "dir": bar["dir"]}
    prev = {"close": bar["prev_close"], "ema20": bar["prev_ema"], "dir": bar["prev_dir"]}
    sig = fap.crude_fresh_signal(cur, prev)
    out["signal"] = {"tf": tf, "key": fk, "bar": bar["ts"], **cur, "prev": prev, "signal": sig}
    out["signal_15m" if tf == "15m" else "signal_1h"] = out["signal"]
    if not sig:
        out["action"] = "none: no fresh %s signal" % tf; done()
    if fap.crude_bar_used(today, bar["ts"], tf):
        out["action"] = "this %s bar was already used" % tf; done()
    fq = quote(fk); spot = fq.get("ltp")
    if not spot:
        out["skipped"] = "no CRUDEOIL future quote (MCX closed?)"; done()
    opts = [r for r in rows if r.get("underlying_symbol") == "CRUDEOIL" and r.get("instrument_type") == sig and r.get("expiry")]
    exps = {datetime.datetime.fromtimestamp(r["expiry"] / 1000, IST).date() for r in opts}
    exp = fap.pick_crude_expiry(exps, now.date())
    if not exp:
        out["skipped"] = "no CRUDEOIL option expiry 8+ days away"; done()
    strike = fap.atm(spot)
    chain = [r for r in opts if datetime.datetime.fromtimestamp(r["expiry"] / 1000, IST).date() == exp]
    best = min(chain, key=lambda r: (abs(r["strike_price"] - strike), r["strike_price"]))
    q = quote(best["instrument_key"])
    m = bk.from_upstox(best["instrument_key"])
    lot = (m or {}).get("lot_size")
    okl, whyl = fap.liquid(q.get("bid"), q.get("ask"), q.get("ltp"), q.get("bid_qty"), q.get("ask_qty"))
    out["option"] = {"upstox": best["trading_symbol"], "key": best["instrument_key"], "expiry": str(exp), "kite": m, "quote": q, "liquidity": whyl}
    if not m or not lot:
        out["skipped"] = "no Kite symbol for " + best["instrument_key"]; done()
    if not okl:
        out["skipped"] = "illiquid: " + whyl; done()
    px = bk.snap(q["ask"], m["tick_size"])
    order = {"trading_symbol": m["tradingsymbol"], "instrument_token": "KITE|MCX|" + m["tradingsymbol"], "transaction_type": "BUY",
             "order_type": "LIMIT", "product": "NRML", "quantity": lot, "price": px, "_book": "crude_auto"}
    ok, why, adj = trade_lock.check_order(order, broker="kite")
    out["check"] = {"allowed": ok, "reasons": why, "adjusted_qty": adj}
    if not ok or adj != lot:
        out["action"] = "blocked by pre-trade rules"; done()
    if DRY:
        out["action"] = "DRY-RUN: would BUY %d %s LIMIT %s NRML on Kite" % (lot, m["tradingsymbol"], px); done()
    rec = {"bar": bar["ts"], "side": sig, "sym": m["tradingsymbol"], "ts": now.isoformat(), "tf": tf}
    book.setdefault(day, []).append(rec); json.dump(book, open(LEDGER, "w"), indent=1)
    oid = bk.place("MCX", m["tradingsymbol"], "BUY", lot, px, tag="crude_auto", product="NRML")
    ks = None
    for _ in range(12):
        if not oid: break
        time.sleep(10); ks = bk.order_status(oid)
        if ks and ks["status"] in ("complete", "rejected", "cancelled"): break
    if oid and not (ks and ks["status"] in ("complete", "rejected", "cancelled")): bk.cancel(oid)
    fill = (ks or {}).get("filled", 0) if oid else 0
    rec.update({"order_id": oid, "filled": fill}); json.dump(book, open(LEDGER, "w"), indent=1)
    out["order_id"] = oid; out["filled"] = fill
    if fill <= 0:
        out["action"] = "crude entry not filled in 2 min - cancelled"; done()
    avg = (ks or {}).get("avg") or px
    st = load(SF, {}); kk = "KITE|MCX|" + m["tradingsymbol"]
    st[kk] = {"high": avg, "entry": avg, "target": round(avg * 1.2, 2), "sym": m["tradingsymbol"], "source": "crude_auto", "broker": "kite"}
    st[kk]["stop_pct"] = _INIT_PCT   # 16 Nov 2026 (t669u): new positions 20% under entry
    st.setdefault("_min_stop_by_symbol", {})[m["tradingsymbol"]] = bk.snap(avg * (1 - _INIT_PCT), m["tick_size"])
    json.dump(st, open(SF, "w"), indent=1)
    import sl_confirm as sc
    c = sc.guard_kite("MCX", m["tradingsymbol"], fill, avg, product="NRML", target=st[kk]["target"], tick=m["tick_size"], entry_order=oid)
    out["sl_check"] = {k: c.get(k) for k in ("ok", "sl", "sl_trigger", "sl_status", "reasons")}
    out["action"] = "ENTERED %d %s @ %s on Kite; target %s stop %s%s" % (fill, m["tradingsymbol"], avg, st[kk]["target"], c.get("sl_trigger"),
                                                                     "" if c.get("ok") else " | SL_CONFIRM_FAILED")
    done()


def manage():
    if not (out["crude_entry_flag"] or out["trend_exit_flag"] or out["ladder_flag"]):
        out["skipped"] = "crude entry and trend-exit flags are both off"; done()
    if not DRY and (now.weekday() >= 5 or not (datetime.time(15, 25) < now.time() <= datetime.time(23, 30))):
        out["skipped"] = "outside 15:25-23:30 IST (risk_manager owns the day session)"; done()
    import broker_kite as bk
    if not bk.valid():
        out["skipped"] = "Kite token invalid"; done()
    try:
        kpos = bk.positions(strict=True); kord = bk.orders(strict=True)
    except Exception as e:
        out["skipped"] = "Kite book unreadable (%s); nothing changed" % type(e).__name__; done()
    st = load(SF, {}); seeds = st.setdefault("_min_stop_by_symbol", {}); acts = out["actions"] = []
    for p in kpos:
        sym = p["trading_symbol"]; q = p["quantity"]
        if p["exchange"] != "MCX" or q <= 0 or faf.root(sym) != "CRUDEOIL" or not trade_lock.is_opt(sym):
            continue
        kk = "KITE|MCX|" + sym; ts = bk.tick_for("MCX", sym); snap = lambda x: bk.snap(x, ts)
        ukey = bk.to_upstox("MCX", sym); ltp = (quote(ukey).get("ltp") if ukey else None) or p["last_price"]
        avg = p["average_price"] or p["buy_price"]
        if not avg or not ltp:
            acts.append({"sym": sym, "action": "SKIP_NO_PRICE"}); continue
        s = st.setdefault(kk, {"high": avg}); s.update(sym=sym, broker="kite")
        _pr, _pw, _ph = pl.bind_position_peak(
            s, avg, ltp, q, day_sell=int(p.get("day_sell_quantity") or 0),
            overnight=int(p.get("overnight_quantity") or 0), orders=kord, sym=sym,
            token=p.get("instrument_token"), step=0.20,
            now=now, initial_stop_pct=_INIT_PCT, seeds=seeds)
        s.setdefault("target", round(avg * 1.2, 2))
        if _pr:
            acts.append({"sym": sym, "action": "PEAK_RESET", "why": _pw, "entry": round(float(avg), 2),
                         "peak": s["high"], "prev_high": _ph, "open_gen": s.get("open_gen")})
        sls = [o for o in kord if o["trading_symbol"] == sym and o["transaction_type"] == "SELL" and o["status"] in ("trigger pending", "open") and o["order_type"] in ("SL", "SL-M")]
        reason = None; why = ""
        if out["trend_exit_flag"]:
            bar, berr = faf.underlying_1h(sym)
            if bar:
                ex, why = fap.trend_exit_1h("PE" if sym.endswith("PE") else "CE", bar, s)
                if ex: reason = "TREND_EXIT_1H"
            else:
                why = "1h unavailable: %s" % berr
        while ltp >= s["target"]:
            prev = s["target"]; s["target"] = round(prev + avg * 0.2, 2)
            acts.append({"sym": sym, "action": "ROLL_TARGET", "from": prev, "to": s["target"]})
        floor = fap.ladder_floor(s["entry"], s["high"]) if out["ladder_flag"] else 0
        # 16 Nov 2026 (t669u): 20% floor for new positions, 30% kept for ones open before the change
        s.setdefault("stop_pct", _INIT_PCT); _sp = _prules.stop_pct(s) if _prules else 0.30
        stop = snap(max(avg * (1 - _sp), seeds.get(sym, 0), s.get("stop", 0), floor))
        if not reason and out["ladder_flag"] and stop >= ltp and stop > avg * (1 - _sp) + 1e-6:
            reason = "STOP_BREACH_EXIT"
        if reason:
            gok, gdet = gtt_clear(sym)
            if not gok:
                acts.append({"sym": sym, "action": "GTT_FAIL", "why": gdet}); continue
            for o in sls: bk.cancel(o["order_id"], dry=DRY)
            bk.forget()
            try: live = sum(x["quantity"] for x in bk.positions(strict=True) if x["trading_symbol"] == sym)
            except Exception: live = None
            qx = q if live is None else fap.capped_sell(q, live)
            if qx <= 0:
                acts.append({"sym": sym, "action": reason + "_SKIPPED", "why": "broker quantity now %s" % live}); continue
            lim = snap(ltp * 0.995)
            acts.append({"sym": sym, "action": reason, "qty": qx, "broker_qty": live, "limit": lim, "trend": why,
                         "resp": bk.place("MCX", sym, "SELL", qx, lim, tag="target_book", product=p["product"], dry=DRY)})
            continue
        if stop >= ltp: stop = snap(ltp * 0.97)
        else: s["stop"] = stop
        lim = snap(stop * 0.95)
        info = {"sym": sym, "qty": q, "avg": avg, "ltp": ltp, "high": s["high"], "target": s["target"], "trigger": stop, "trend": why}
        if not sls:
            gok, gdet = gtt_clear(sym)
            if not gok:
                acts.append({**info, "action": "GTT_FAIL", "why": gdet}); continue
            acts.append({**info, "action": "PLACE_SL", "resp": bk.place("MCX", sym, "SELL", q, lim, order_type="SL", trigger=stop, tag="trailsl", product=p["product"], dry=DRY)})
        else:
            o = sls[0]
            t2 = pl.sl_modify_trigger(stop, o["trigger_price"], qty_changed=o["quantity"] != q, reset=_pr)
            if t2 is not None:
                acts.append({**info, "action": "MODIFY_SL", "from": o["trigger_price"], "to": t2, "resp": bk.modify(o["order_id"], q, snap(t2 * 0.95), t2, dry=DRY)})
            else:
                acts.append({**info, "action": "SL_OK", "sl_trigger": o["trigger_price"]})
    # Any KITE|MCX crude with no live qty this run is flat: cancel its overnight GTT
    # so a GTT placed while a day SL was still live cannot sell a second time.
    live_syms = set()
    for p in kpos:
        if p.get("exchange") == "MCX" and int(p.get("quantity") or 0) > 0 and faf.root(p["trading_symbol"]) == "CRUDEOIL" and trade_lock.is_opt(p["trading_symbol"]):
            live_syms.add(p["trading_symbol"])
    for k, s in st.items():
        if not (isinstance(s, dict) and str(k).startswith("KITE|MCX|")):
            continue
        sym = s.get("sym") or str(k).split("|")[-1]
        if faf.root(sym) != "CRUDEOIL":
            continue
        if sym in live_syms:
            continue
        if int(s.get("live_qty") or 0) > 0 or s.get("open_gen"):
            gok, gdet = gtt_clear(sym)
            acts.append({"sym": sym, "action": "FLAT_CLEAR_GTT" if gok else "FLAT_CLEAR_GTT_FAIL",
                         "why": "position is flat - overnight GTT cancelled so it cannot sell twice",
                         "detail": gdet})
            s["live_qty"] = 0
            s["open_gen"] = None
    if not DRY: json.dump(st, open(SF, "w"))
    done()


if __name__ == "__main__":
    entry() if MODE == "entry" else manage()
