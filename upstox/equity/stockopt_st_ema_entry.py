#!/usr/bin/env python3
"""Stock-option entries for the Supertrend-versus-EMA weekend list.

The liquid-universe scanner in stockopt_tech_entry.py stays paused
(TECHNICAL_PAUSED). This file is the only stock-option entry path that may
send, and only when STOCKOPT_ST_EMA_EXECUTE=1 and this process is not a
dry run. It buys at most the five names in monitor_scope.json
st_ema_category_watchlist. Off-list names are rejected. Nothing is substituted.

A first entry is either a fresh 1-hour trigger (baseline_signal: prior bar not
already aligned, MACD with the trade, shared ADX floor) or a later join of a
trend that is already aligned, with Supertrend strictly beyond EMA20. Calls
in an uptrend, puts in a downtrend. One lot, LIMIT, tag stockopt_tech, both
brokers checked before either send. A name already held or already entered
this week is not entered again and is not added to. One PSU bank per pass;
clearer Supertrend-versus-EMA separation wins, and the remaining names are
tried strongest first inside the distinct-underlying cap.

The index one-shot and the index Supertrend-versus-EMA 2-lot rule are not
used. Adds are not sent. A name with no chain, poor liquidity, or results due
is skipped and recorded. The position is marked for that Friday's 15:10 IST
flatten. Stock-option signals are always the completed 1-hour bar (never 3m
or 15m). When the current monthly stock-option expiry falls in the current
ISO week, a first entry is planned as a next-month debit spread (buy the
next-month ATM, sell two listed strikes further OTM) instead of the plain
near-month option. Live spread sends (SPREAD_SEND_ALLOWED) go through execute_debit_spread:
BUY the long, wait for a confirmed fill, SELL the short, retry the SELL
once, then alert; the long stays on its normal stop if the SELL still
fails. Overnight, a GTT SELL BELOW sits on the long and a companion GTT
BUY BELOW sits on the short, plus a 9:15/intraday reconcile of any lone
short. Results-day-before exits and expiry square-off close both legs
(short first); the expiry-week square-off must not flatten a later-month
spread.

Importing this module does not read a broker and does not place an order.
"""
import argparse
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import stockopt_st_ema as st
import stockopt_spread as ssp

SIGNAL_UNIT = ssp.SIGNAL_UNIT
SIGNAL_INTERVAL = ssp.SIGNAL_INTERVAL
SIGNAL_DAYS = ssp.SIGNAL_DAYS

IST = st.IST
TAG = st.TAG


def tick(price, size=0.05):
    """Same LIMIT rounding as stockopt_tech_entry.tick."""
    return round(max(round(float(price) / size) * size, size), 2)


def pick_atm(rows, side, close):
    """Nearest strike on the nearest unexpired expiry. Same choice as the paused scanner."""
    if not rows:
        return None
    expiry = min(row["expiry_date"] for row in rows)
    same = [row for row in rows if row["expiry_date"] == expiry and row.get("instrument_type") == side]
    if not same:
        return None
    return min(same, key=lambda row: abs(float(row.get("strike_price") or 0) - close))


def _position_brokers():
    try:
        import kite_disabled as _kd
        if _kd.active():
            return ("upstox",)
    except Exception:
        if os.path.exists(os.path.join(ROOT, "kite_disabled.json")):
            return ("upstox",)
    return ("upstox", "kite")


def _reserved_count(positions):
    """Distinct stock-option underlyings on the fuller broker, or None if unreadable."""
    brokers = _position_brokers()
    if not isinstance(positions, dict) or any(positions.get(b) is None for b in brokers):
        return None
    import stockopt_policy as pol
    counts = []
    for broker in brokers:
        names = set()
        for pos in positions[broker] or []:
            if not isinstance(pos, dict):
                return None
            try:
                qty = int(pos.get("quantity") or 0)
            except (TypeError, ValueError):
                return None
            sym = pos.get("trading_symbol") or pos.get("tradingsymbol") or ""
            if qty > 0 and pol.is_stock_option(sym):
                names.add(pol.underlying(sym))
        counts.append(len(names))
    return max(counts) if counts else None


def _held(positions, underlying):
    """True / False, or None when a required book cannot be read."""
    if not isinstance(positions, dict):
        return None
    brokers = _position_brokers()
    if any(positions.get(b) is None for b in brokers):
        return None
    import stockopt_policy as pol
    for broker in brokers:
        rows = positions[broker]
        for pos in rows:
            if not isinstance(pos, dict):
                return None
            try:
                qty = int(pos.get("quantity") or 0)
            except (TypeError, ValueError):
                return None
            sym = (pos.get("trading_symbol") or pos.get("tradingsymbol") or "")
            if qty > 0 and pol.is_stock_option(sym) and pol.underlying(sym) == underlying:
                return True
    return False


def _mirror_orders(decision, contract, price, flatten_by):
    lot = int(contract["lot_size"])
    kite_lot = int(contract.get("kite_lot_size") or lot)
    qty_u = st.stock_lots() * lot
    qty_k = st.stock_lots() * kite_lot
    stamp = flatten_by.isoformat() if hasattr(flatten_by, "isoformat") else flatten_by
    common = {
        "trading_symbol": contract.get("trading_symbol"),
        "instrument_token": contract.get("instrument_key"),
        "transaction_type": "BUY",
        "order_type": "LIMIT",
        "price": price,
        "tag": TAG,
        "_initial_entry": True,
        "_st_ema_week": True,
        "_flatten_by": stamp,
        "_paired": True,
    }
    upstox = dict(common, product="D", quantity=qty_u)
    kite = dict(common, product="NRML", quantity=qty_k,
                trading_symbol=contract.get("kite_symbol") or contract.get("trading_symbol"),
                kite_exchange=contract.get("kite_exchange") or "NFO")
    return upstox, kite



def _kite_writes_allowed():
    """False when the user has disabled Kite (t783u/t784u). Fail closed if the switch cannot be read."""
    try:
        import kite_disabled as _kd
        return not _kd.active()
    except Exception:
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return not os.path.exists(os.path.join(root, "kite_disabled.json"))


def _check_pair(check_order, upstox, kite):
    """Both brokers must clear the full 1-lot quantity. Anything else sends nothing."""
    if check_order is None:
        return False, "order check unverified"
    cleared = []
    legs = [("upstox", upstox)]
    if _kite_writes_allowed():
        legs.append(("kite", kite))
    for broker, order in legs:
        try:
            result = check_order(order, broker)
        except Exception as exc:
            return False, "order check unverified: %s" % type(exc).__name__
        if not isinstance(result, tuple) or len(result) != 3:
            return False, "order check unverified"
        ok, _why, adj = result
        try:
            adj = int(adj)
        except (TypeError, ValueError):
            return False, "order check quantity unverified"
        if ok is not True or adj != int(order["quantity"]) or adj <= 0:
            return False, "broker check did not clear 1 lot"
        cleared.append(order)
    return True, cleared


def _send_pair(place, orders):
    if place is None:
        return [], "sender unverified"
    sent = []
    legs = [("upstox", orders[0])]
    if len(orders) > 1 and _kite_writes_allowed():
        legs.append(("kite", orders[1]))
    for broker, order in legs:
        try:
            order_id = place(broker, order)
        except Exception as exc:
            return sent, "send failed: %s" % type(exc).__name__
        if not order_id:
            return sent, "broker rejected; the other leg was not sent" if not sent else "Kite rejected after Upstox"
        sent.append({"broker": broker, "order_id": order_id, "quantity": order["quantity"]})
    return sent, None


def _analyze_bar(market, name, contract):
    analyze = market.get("analyze")
    if analyze is None:
        return None
    key = None if not isinstance(contract, dict) else contract.get("underlying_key")
    if not key and isinstance(market.get("equity_keys"), dict):
        key = market["equity_keys"].get(name)
    try:
        return analyze(key or name)
    except Exception:
        return None


def _position_row(positions, broker, symbol):
    want = (symbol or "").upper()
    for pos in (positions or {}).get(broker) or []:
        sym = (pos.get("trading_symbol") or pos.get("tradingsymbol") or "").upper()
        if sym == want:
            return pos
    return {}


def _exit_limit(market, order, positions):
    """LIMIT under the last price. Missing price does not sell."""
    last = None
    for pos in (positions or {}).get(order.get("broker")) or []:
        sym = (pos.get("trading_symbol") or pos.get("tradingsymbol") or "").upper()
        if sym != (order.get("trading_symbol") or "").upper():
            continue
        try:
            last = float(pos.get("last_price") or pos.get("ltp") or 0)
        except (TypeError, ValueError):
            last = None
        break
    if not last or last <= 0:
        return None
    if (order.get("transaction_type") or "SELL").upper() == "BUY":
        return tick(last * 1.005, 0.05)
    return tick(last * 0.995, 0.05)


def _quote(market, contract):
    quote = market.get("quote")
    if quote is None or not isinstance(contract, dict):
        return None
    try:
        return quote(contract.get("instrument_key"))
    except Exception:
        return None



def _send_debit_spreads(place, wait_fill, buy_upstox, sell_upstox, buy_kite, sell_kite, alert=None, cancel=None):
    """One broker at a time: BUY long, wait for fill, SELL short (one retry)."""
    results = []
    legs = [("upstox", buy_upstox, sell_upstox)]
    if _kite_writes_allowed():
        legs.append(("kite", buy_kite, sell_kite))
    for broker, buy, sell in legs:
        def _place(order, _b=broker):
            return place(_b, order)
        def _wait(oid, order, _b=broker):
            if wait_fill is None:
                return {"filled": False, "reason": "fill check unverified"}
            return wait_fill(_b, oid, order)
        def _cancel(oid, order, _b=broker):
            if cancel is None:
                return None
            return cancel(_b, oid, order)
        r = ssp.execute_debit_spread(_place, _wait, buy, sell, cancel=_cancel if cancel else None,
                                     alert=alert, sell_retries=1)
        r["broker"] = broker
        results.append(r)
        # if this broker's buy did not fill, still try the other? No: continue so each
        # broker is independent, but report.
    return results


def run(scope, now, market=None, *, execute=False, dry=True, ledger_path=None,
        session_ok=None, results=None, scope_path=None, removal_log=None):
    """Decide the watchlist. Places only when execute is on, dry is off, and every check passed."""
    market = market or {}
    loaded = st.prepare_scope(scope)
    result = {
        "ok": loaded["ok"],
        "reason": loaded.get("reason"),
        "execute": bool(execute) and not dry,
        "execute_requested": bool(execute),
        "dry_run": bool(dry),
        "enable_env": st.ENABLE_ENV,
        "watchlist": list(loaded.get("names") or []),
        "dropped": list(loaded.get("dropped") or []),
        "flatten_by": loaded["flatten_by"].isoformat() if loaded.get("flatten_by") else None,
        "liquid_scanner_paused": True,
        "adds": False,
        "lots_per_broker": st.stock_lots(),
        "actions": [],
        "orders_placed": 0,
        "tag": TAG,
    }
    if not loaded["ok"]:
        result["orders_placed"] = 0
        return result
    # t689u: drop names whose completed 1h bar has broken the watched trend,
    # before any entry is evaluated. Dry-run reports only; it does not write
    # the scope or the removal log. Open positions are left untouched.
    bars = {}
    chains = market.get("chains")
    for name in loaded["names"]:
        chain = None if not isinstance(chains, dict) else chains.get(name)
        bars[name] = _analyze_bar(market, name, chain[0] if chain else None)
    pruned = st.prune_broken_watchlist(
        scope, bars, now, persist=not dry, scope_path=scope_path,
        log_path=removal_log)
    result["watchlist_prune"] = {
        "removed": pruned.get("removed") or [],
        "kept": [row.get("symbol") if isinstance(row, dict) else row for row in (pruned.get("kept") or [])],
        "unverified": pruned.get("unverified") or [],
        "persisted": bool(pruned.get("persisted")),
        "directions": pruned.get("directions") or {},
    }
    if pruned.get("removed") or pruned.get("unverified"):
        loaded = st.prepare_scope(scope)
        result["watchlist"] = list(loaded.get("names") or [])
        if not loaded["ok"]:
            result["ok"] = False
            result["reason"] = loaded.get("reason") or "watchlist empty after trend-break removals"
            result["orders_placed"] = 0
            return result
    flat = st.friday_flatten_plan(now, loaded["flatten_by"])
    result["flatten"] = flat
    positions = market.get("positions")
    if flat["due"]:
        book = st.flatten_orders(now, loaded["flatten_by"], st.read_ledger(ledger_path) if ledger_path else None,
                                 positions)
        result["flatten_orders"] = book
        placed = 0
        if execute and not dry and book.get("orders"):
            place = market.get("place")
            if place is None:
                result["flatten_orders"] = dict(book, send=False, reason="sender unverified")
            else:
                sent = []
                skipped = []
                blocked = set()  # (broker, underlying) whose short cover failed
                for order in sorted(book["orders"], key=lambda o: int(o.get("sequence") or 2)):
                    key = (order.get("broker"), order.get("underlying"))
                    if order.get("transaction_type") == "SELL" and key in blocked:
                        skipped.append(dict(order, reason="short cover failed or skipped; long not sold"))
                        continue
                    price = _exit_limit(market, order, positions if isinstance(positions, dict) else {})
                    row = _position_row(positions, order.get("broker"), order.get("trading_symbol"))
                    token = row.get("instrument_token") or row.get("instrument_key")
                    if price is None or not token:
                        skipped.append(dict(order, reason="exit price or instrument unverified"))
                        if (order.get("transaction_type") or "SELL") == "BUY":
                            blocked.add((order.get("broker"), order.get("underlying")))
                        continue
                    payload = dict(order, price=price, send=True, instrument_token=token,
                                   product=row.get("product") or "D",
                                   kite_exchange=row.get("exchange") or "NFO")
                    try:
                        order_id = place(order["broker"], payload)
                    except Exception:
                        order_id = None
                    if order_id:
                        sent.append(dict(order, price=price, order_id=order_id, send=True))
                        placed += 1
                    else:
                        skipped.append(dict(order, price=price, reason="flatten send failed"))
                        if (order.get("transaction_type") or "SELL") == "BUY":
                            blocked.add((order.get("broker"), order.get("underlying")))
                result["flatten_orders"] = dict(book, send=bool(sent), placed=sent, skipped=skipped,
                                                reason=book["reason"] if sent else "flatten send incomplete")
        for name in loaded["names"]:
            result["actions"].append({
                "underlying": name, "action": "skip", "lots": 0, "orders_placed": False, "add": False,
                "reason": "weekly list is flattening into Friday's close; no new entry",
            })
        result["orders_placed"] = placed
        return result
    window = st.entry_window(now)
    if session_ok is not True:
        window = {"ok": False, "reason": "trading session unverified" if session_ok is None else "not a trading session"}
    ledger = st.read_ledger(ledger_path) if ledger_path else None
    if ledger_path and ledger is None:
        ledger_state = "bad"
        entered = set()
    elif isinstance(ledger, dict):
        ledger_state = "ok"
        entered = {str(name).upper() for name, row in (ledger.get("names") or {}).items()
                   if isinstance(row, dict) and row.get("source") == st.SOURCE}
    else:
        ledger_state = "missing"
        entered = set()
    chains = market.get("chains")
    drafts = []
    prepared = {}
    for name in loaded["names"]:
        chain = None if not isinstance(chains, dict) else chains.get(name)
        chain_count = None if chains is None or chain is None else len(chain)
        held = _held(positions, name)
        bar = None
        contract = None
        quote = None
        plan = None
        hedge = None
        mapping_failed = False
        if chain_count:
            bar = _analyze_bar(market, name, chain[0] if chain else None)
            signal = st.entry_signal(bar) if bar else {"side": None}
            close = None if not isinstance(bar, dict) else bar.get("close")
            if signal.get("side") and close is not None:
                fut = None
                if isinstance(market.get("fut_dates"), dict):
                    fut = market["fut_dates"].get(name)
                plan = ssp.plan_entry(chain, signal["side"], close, now.date(), fut_dates=fut)
                if plan.get("mode") in ("plain", "spread") and isinstance(plan.get("long"), dict):
                    contract = plan["long"]
                    if plan.get("mode") == "spread" and isinstance(plan.get("short"), dict):
                        hedge = plan["short"]
                else:
                    contract = None
                def _map(row):
                    if not isinstance(row, dict) or not market.get("map_kite"):
                        return row, False
                    try:
                        mapped = market["map_kite"](row)
                    except Exception:
                        mapped = None
                    if not mapped:
                        return None, True
                    return dict(row, **mapped), False
                if isinstance(contract, dict):
                    contract, mapping_failed = _map(contract)
                    if mapping_failed:
                        contract = None
                if isinstance(hedge, dict) and not mapping_failed:
                    hedge, hedge_failed = _map(hedge)
                    if hedge_failed:
                        mapping_failed = True
                        hedge = None
                quote = _quote(market, contract) if contract else None
        decision = st.plan_name(
            name, loaded["names"], bar, chain_count=chain_count, quote=quote, results=results,
            contract=contract, held=held is True,
            entered_this_week=name in entered,
            books_readable=held is not None)
        if ledger_state == "bad":
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = "weekly entry ledger unverified"
        elif mapping_failed and decision.get("signal", {}).get("side"):
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = "Kite symbol mapping unavailable"
        decision["flatten_by"] = result["flatten_by"]
        if decision["action"] == "enter" and not window["ok"]:
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["reason"] = window["reason"]
        if isinstance(plan, dict):
            decision["entry_mode"] = plan.get("mode")
            decision["expiry_week"] = plan.get("expiry_week")
            decision["current_monthly"] = plan.get("current_monthly")
            decision["next_monthly"] = plan.get("next_monthly")
            decision["strike_step"] = plan.get("strike_step")
            decision["hedge_steps"] = plan.get("hedge_steps")
            decision["long_strike"] = plan.get("long_strike")
            decision["short_strike"] = plan.get("short_strike")
            decision["spread_reason"] = plan.get("reason")
            decision["send_allowed"] = plan.get("send_allowed")
            if plan.get("send_block"):
                decision["send_block"] = plan.get("send_block")
            if plan.get("mode") == "refuse" and decision.get("action") == "enter":
                decision["action"] = "skip"
                decision["lots"] = 0
                decision["quantity"] = 0
                decision["orders_placed"] = False
                decision["send"] = False
                decision["reason"] = plan.get("reason") or "debit-spread entry refused"
        prepared[name] = {"contract": contract, "quote": quote, "plan": plan, "hedge": hedge}
        drafts.append(decision)
    # Books were readable enough to draft. None here means the count itself could not be taken.
    books_closed = any(row.get("reason") == "broker book unverified" for row in drafts)
    open_count = None if books_closed else _reserved_count(positions)
    reserved = set(entered)
    if isinstance(positions, dict) and all(positions.get(b) is not None for b in _position_brokers()):
        import stockopt_policy as pol
        for broker in _position_brokers():
            rows = positions[broker]
            for pos in rows or []:
                if not isinstance(pos, dict):
                    continue
                try:
                    qty = int(pos.get("quantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                sym = pos.get("trading_symbol") or pos.get("tradingsymbol") or ""
                if qty > 0 and pol.is_stock_option(sym):
                    reserved.add(pol.underlying(sym))
    # 12 Nov 2026: shared daily loss cap (2 losing closed trades or Rs15k realised today, both
    # brokers). Blocks new entries only; the Friday flatten above and every exit are untouched.
    import daily_loss_cap as dlc
    _books = positions if isinstance(positions, dict) else {}
    loss_cap = dlc.evaluate(_books.get("upstox"), _books.get("kite"), now,
                            state_path=dlc.default_state_path(), persist=bool(execute and not dry))
    result["daily_loss_cap"] = loss_cap
    for decision in st.select_entries(drafts, open_count=open_count, reserved=reserved):
        name = decision["underlying"]
        if decision["action"] != "enter":
            decision["orders_placed"] = False
            decision["send"] = False
            result["actions"].append(decision)
            continue
        if not loss_cap["ok"]:
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = loss_cap["reason"]
            result["actions"].append(decision)
            continue
        plan_now = prepared.get(name, {}).get("plan") or {}
        if plan_now.get("mode") == "spread" and not plan_now.get("send_allowed"):
            decision["orders_placed"] = False
            decision["send"] = False
            decision["action"] = "skip" if (execute and not dry) else decision["action"]
            decision["reason"] = ("dry-run planned next-month debit spread; live send refused: " + (plan_now.get("send_block") or ssp.SPREAD_SEND_BLOCK)
                                  if dry else (plan_now.get("send_block") or ssp.SPREAD_SEND_BLOCK))
            if execute and not dry:
                decision["lots"] = 0
                decision["quantity"] = 0
            result["actions"].append(decision)
            continue
        if not execute or dry:
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = "dry-run; no order" if dry else "STOCKOPT_ST_EMA_EXECUTE is off; no order"
            result["actions"].append(decision)
            continue
        if not ledger_path or ledger is None:
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = "Friday ledger unverified"
            result["actions"].append(decision)
            continue
        contract = prepared.get(name, {}).get("contract")
        quote = prepared.get(name, {}).get("quote")
        hedge = prepared.get(name, {}).get("hedge")
        plan_live = prepared.get(name, {}).get("plan") or plan_now
        price = st.limit_price(quote, tick)
        if plan_live.get("mode") == "spread" and plan_live.get("send_allowed"):
            if price is None or not isinstance(contract, dict) or not isinstance(hedge, dict):
                decision["action"] = "skip"
                decision["lots"] = 0
                decision["quantity"] = 0
                decision["reason"] = "spread contracts or long limit unverified"
                decision["orders_placed"] = False
                result["actions"].append(decision)
                continue
            # hedge quote may be missing; size off the long price for the long, use a
            # conservative below-bid if the short has no quote (the SELL waits for the buy fill).
            short_quote = _quote(market, hedge)
            short_px = st.limit_price(short_quote, tick) if short_quote else None
            if short_px is None and short_quote and short_quote.get("ltp"):
                short_px = tick(float(short_quote["ltp"]) * 0.995, 0.05)
            if short_px is None:
                decision["action"] = "skip"
                decision["reason"] = "short-leg limit price unverified"
                decision["orders_placed"] = False
                result["actions"].append(decision)
                continue
            buy_u, buy_k = _mirror_orders(decision, contract, price, loaded["flatten_by"])
            sell_common = {"transaction_type": "SELL", "order_type": "LIMIT", "price": short_px, "tag": TAG,
                           "_paired": True, "_spread_short": True}
            sell_u = dict(sell_common, product="D", quantity=buy_u["quantity"],
                          trading_symbol=hedge.get("trading_symbol"),
                          instrument_token=hedge.get("instrument_key"))
            sell_k = dict(sell_common, product="NRML", quantity=buy_k["quantity"],
                          trading_symbol=hedge.get("kite_symbol") or hedge.get("trading_symbol"),
                          instrument_token=hedge.get("instrument_key"),
                          kite_exchange=hedge.get("kite_exchange") or "NFO")
            ok, cleared = _check_pair(market.get("check_order"), buy_u, buy_k)
            if not ok:
                decision["action"] = "skip"
                decision["reason"] = cleared
                decision["orders_placed"] = False
                result["actions"].append(decision)
                continue
            sent = _send_debit_spreads(market.get("place"), market.get("wait_fill"),
                                       buy_u, sell_u, buy_k, sell_k,
                                       cancel=market.get("cancel"))
            decision["spread_sends"] = sent
            decision["orders_placed"] = sum(1 for r in sent if r.get("buy_id") or r.get("sell_id"))
            decision["send"] = any(r.get("ok") or r.get("buy_filled") for r in sent)
            if any(r.get("alert") for r in sent):
                decision["reason"] = "; ".join(r["alert"] for r in sent if r.get("alert"))
            elif not all(r.get("ok") for r in sent):
                decision["reason"] = "; ".join(r.get("reason") or "spread send incomplete" for r in sent)
            else:
                decision["lots"] = st.stock_lots()
                st.remember_entry(ledger_path, name, loaded["flatten_by"])
                entered.add(name)
                # record the pair so GTT / exits can find it
                try:
                    led = ssp.load_ledger()
                    for r, broker, long_o, short_o in (
                            (sent[0], "upstox", buy_u, sell_u), (sent[1], "kite", buy_k, sell_k)):
                        if r.get("ok") or r.get("buy_filled"):
                            ssp.record_spread(led, broker=broker, underlying=name,
                                              expiry=plan_live.get("next_monthly"),
                                              side=decision.get("side") or (plan_live.get("side")),
                                              long_sym=long_o.get("trading_symbol"),
                                              short_sym=short_o.get("trading_symbol"),
                                              qty=long_o.get("quantity"), ts=str(now),
                                              strike_step=plan_live.get("strike_step"))
                    ssp.save_ledger(led)
                except Exception:
                    decision["reason"] = (decision.get("reason") or "opened") + "; spread ledger was not written"
            result["orders_placed"] += decision["orders_placed"]
            result["actions"].append(decision)
            continue
        if price is None or not isinstance(contract, dict):
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["reason"] = "limit price unverified"
            decision["orders_placed"] = False
            result["actions"].append(decision)
            continue
        upstox, kite = _mirror_orders(decision, contract, price, loaded["flatten_by"])
        ok, cleared = _check_pair(market.get("check_order"), upstox, kite)
        if not ok:
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = cleared
            result["actions"].append(decision)
            continue
        sent, err = _send_pair(market.get("place"), cleared)
        decision["orders"] = sent
        decision["orders_placed"] = len(sent)
        decision["send"] = bool(sent)
        if err or len(sent) != 2:
            decision["reason"] = err or "mirror incomplete"
            if sent and not st.remember_entry(ledger_path, name, loaded["flatten_by"]):
                decision["reason"] += "; Friday ledger was not written"
        else:
            if not st.remember_entry(ledger_path, name, loaded["flatten_by"]):
                decision["reason"] = "filled but Friday ledger was not written"
            else:
                entered.add(name)
            decision["lots"] = st.stock_lots()
        result["orders_placed"] += len(sent)
        result["actions"].append(decision)
    return result


def _live_market(names, now):
    """Broker adapters. Used only by the command line, never by the unit tests."""
    import api
    import broker_kite as bk
    import ind
    import trade_lock as tl
    import stockopt_tech_entry as tech

    rows = tech.master(now.date())
    chains = {name: [] for name in names}
    fut_dates = {name: set() for name in names}
    equity_keys = {}
    for row in rows or []:
        if row.get("segment") == "NSE_EQ" and row.get("instrument_type") == "EQ" and row.get("instrument_key"):
            sym = (row.get("trading_symbol") or "").upper().replace("-EQ", "")
            if sym in names:
                equity_keys[sym] = row["instrument_key"]
        underlying = (row.get("underlying_symbol") or row.get("name") or "").upper()
        if row.get("segment") == "NSE_FO" and row.get("instrument_type") == "FUT" and underlying in fut_dates and row.get("expiry"):
            try:
                fut_dates[underlying].add(dt.datetime.fromtimestamp(row["expiry"] / 1000, IST).date())
            except Exception:
                pass
        if row.get("segment") != "NSE_FO" or row.get("instrument_type") not in ("CE", "PE"):
            continue
        underlying = (row.get("underlying_symbol") or row.get("name") or "").upper()
        if underlying not in chains or not row.get("underlying_key") or not row.get("expiry"):
            continue
        expiry = dt.datetime.fromtimestamp(row["expiry"] / 1000, IST).date()
        if expiry < now.date():
            continue
        chains[underlying].append(dict(row, expiry_date=expiry))

    def analyze(key):
        return ind.analyze(key, SIGNAL_UNIT, SIGNAL_INTERVAL, SIGNAL_DAYS)

    def quote(key):
        import urllib.parse
        # /v2/market-quote/full returns HTTP 400 UDAPI100012. /quotes has the
        # same bid/ask/OI/LTP fields the liquidity check uses.
        payload = api.get("/v2/market-quote/quotes?instrument_key=" + urllib.parse.quote(key, safe=""))
        data = (payload or {}).get("data") or {}
        values = next(iter(data.values()), {}) if isinstance(data, dict) else {}
        depth = values.get("depth") or {}
        try:
            ask = float(depth["sell"][0]["price"])
        except Exception:
            ask = 0.0
        try:
            bid = float(depth["buy"][0]["price"])
        except Exception:
            bid = 0.0
        oi = values.get("oi")
        if oi is None:
            oi = values.get("open_interest")
        return {"ltp": float(values.get("last_price") or 0), "ask": ask, "bid": bid,
                "oi": oi, "tick": float(values.get("tick_size") or 0.05)}

    def positions():
        try:
            upstox = api.get("/v2/portfolio/short-term-positions").get("data")
        except Exception:
            upstox = None
        try:
            kite = bk.positions(strict=True)
        except Exception:
            kite = None
        return {"upstox": upstox, "kite": kite}

    def map_kite(contract):
        mapped = bk.from_upstox(contract.get("instrument_key"))
        if not mapped:
            return None
        return {"kite_symbol": mapped["tradingsymbol"], "kite_lot_size": int(mapped["lot_size"]),
                "kite_exchange": mapped["exchange"]}

    def check_order(order, broker):
        return tl.check_order(order, broker=broker)

    def place(broker, order):
        if order.get("transaction_type") == "SELL":
            price = order.get("price")
            if not price:
                return None
        if broker == "upstox":
            body = {"quantity": order["quantity"], "product": order.get("product") or "D", "validity": "DAY",
                    "price": order["price"], "tag": TAG, "instrument_token": order["instrument_token"],
                    "order_type": "LIMIT", "transaction_type": order["transaction_type"],
                    "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False}
            reply = api.call("https://api-hft.upstox.com/v2/order/place", body, "POST", proxy=True)
            return (reply.get("data") or {}).get("order_id")
        return bk.place(order.get("kite_exchange") or "NFO", order["trading_symbol"], order["transaction_type"],
                        order["quantity"], order["price"], tag=TAG, product=order.get("product") or "NRML")


    def wait_fill(broker, oid, order, tries=8, pause=0.5):
        import time
        if not oid or str(oid).startswith("DRY"):
            return {"filled": False, "reason": "dry or missing order id"}
        want = int(order.get("quantity") or 0)
        last = "fill not confirmed"
        for _ in range(max(1, int(tries))):
            try:
                if broker == "upstox":
                    d = (api.get("/v2/order/details?order_id=" + str(oid)) or {}).get("data") or {}
                    status = str(d.get("status") or "").lower()
                    filled = int(d.get("filled_quantity") or 0)
                    if status in ("complete", "completed", "filled", "traded") or (want and filled >= want):
                        return {"filled": True, "qty": filled or want, "avg": d.get("average_price"), "status": status}
                    if status in ("rejected", "cancelled", "canceled", "failed"):
                        return {"filled": False, "status": status, "reason": d.get("status_message") or status}
                    last = status or last
                else:
                    stt = bk.order_status(oid) or {}
                    status = str(stt.get("status") or "").lower()
                    filled = int(stt.get("filled") or 0)
                    if status in ("complete", "completed", "filled", "traded") or (want and filled >= want):
                        return {"filled": True, "qty": filled or want, "avg": stt.get("avg"), "status": status}
                    if status in ("rejected", "cancelled", "canceled", "failed"):
                        return {"filled": False, "status": status}
                    last = status or last
            except Exception as e:
                last = type(e).__name__
            time.sleep(pause)
        return {"filled": False, "reason": last}

    def cancel(broker, oid, order=None):
        if not oid or str(oid).startswith("DRY"):
            return None
        if broker == "upstox":
            return api.call("https://api-hft.upstox.com/v2/order/cancel?order_id=" + str(oid), None, "DELETE", proxy=True)
        return bk.cancel(oid)

    return {"chains": chains, "fut_dates": {k: sorted(v) for k, v in fut_dates.items()},
            "equity_keys": equity_keys,
            "analyze": analyze, "quote": quote, "positions": positions(),
            "map_kite": map_kite, "check_order": check_order, "place": place,
            "wait_fill": wait_fill, "cancel": cancel}


def _results_status(scope, calendar_path):
    """Names with results still due. An unreadable calendar fails closed."""
    names = set()
    try:
        if calendar_path and os.path.exists(calendar_path):
            import json
            raw = json.load(open(calendar_path))
            today = dt.datetime.now(IST).date()
            if isinstance(raw, dict):
                for sym, row in raw.items():
                    if not isinstance(row, dict) or not row.get("expected_results_date"):
                        continue
                    due = dt.date.fromisoformat(row["expected_results_date"])
                    week = {str(day)[:10] for day in (scope.get("week_trading_days") or [])}
                    if due.isoformat() in week or due >= today:
                        names.add(str(sym).upper())
        week = [str(day)[:10] for day in (scope.get("week_trading_days") or [])]
        if any(day in ("2026-11-11", "2026-11-12", "2026-11-13") for day in week):
            from weekly_cap_watchlists import RESULTS_1113
            names |= {str(sym).upper() for sym in RESULTS_1113}
    except Exception:
        return {"ok": False, "names": [], "reason": "results calendar unverified"}
    return {"ok": True, "names": sorted(names)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped Supertrend-versus-EMA stock-option entry")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scope", default=os.path.join(ROOT, "monitor_scope.json"))
    parser.add_argument("--calendar", default=os.path.join(ROOT, "stockopt_calendar.json"))
    parser.add_argument("--ledger", default=os.path.join(HERE, "logs", "stockopt_st_ema_ledger.json"))
    args = parser.parse_args(argv)
    import json
    now = dt.datetime.now(IST)
    try:
        with open(args.scope) as handle:
            scope = json.load(handle)
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": "monitor scope unreadable: %s" % type(exc).__name__}))
        return 1
    try:
        import market_calendar
        session_ok = bool(market_calendar.is_trading_day(now.date()))
    except Exception:
        session_ok = None
    results = _results_status(scope, args.calendar)
    prepared = st.prepare_scope(scope)
    if not prepared["ok"]:
        print(json.dumps({"ok": False, "reason": prepared["reason"], "orders_placed": 0}))
        return 1
    try:
        market = _live_market(prepared["names"], now)
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": "market unverified: %s" % type(exc).__name__,
                          "orders_placed": 0}))
        return 1
    result = run(scope, now, market, execute=st.execute_enabled(), dry=args.dry_run,
                 ledger_path=args.ledger, session_ok=session_ok, results=results,
                 scope_path=args.scope)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
