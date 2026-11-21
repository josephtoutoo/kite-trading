#!/usr/bin/env python3
"""Intraday strong-technical stock-option entries, mirrored on Upstox and Kite.

Places only when STOCKOPT_INTRADAY_TECH_EXECUTE=1 and this process is not a
dry run. The weekend Supertrend-versus-EMA watchlist path is separate.
The paused liquid scanner in stockopt_tech_entry.py is not used.

From the pinned shortlist (equity/stockopt_intraday_shortlist.json), a
completed 1-hour cash bar that still holds the strong setup (EMA20, EMA50,
Supertrend, MACD, ADX >= 20) with the daily bar agreeing can enter, at most
two names, ranked by strength and cleanliness. 1 lot, LIMIT, Upstox product
I and Kite product MIS, current monthly ATM (23 Nov 2026 this month). Both
brokers are checked before either send. Same-day square-off is 15:15 IST
(15:10 IST on Friday). New entries stop at 14:30 IST.

Expiry week refuses a new near-month entry here. Adds are off. Oversize,
the daily loss cap, the same-contract re-entry block, and the 3-name cap
are enforced through trade_lock.check_order and daily_loss_cap; a failed
check sends nothing. Total premium deployed per broker on this path
(open positions plus the new entry, lot size x limit price) is capped at
Rs 20,000; a name over the cap is skipped and the next-ranked still-valid
name is tried, still at most two names. Importing this module does not
read a broker and does not place an order.
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import stockopt_intraday_tech as it

IST = it.IST
TAG = it.TAG
DEFAULT_LEDGER = os.path.join(HERE, "logs", "stockopt_intraday_ledger.json")
DEFAULT_CALENDAR = os.path.join(ROOT, "stockopt_calendar.json")


def tick(price, size=0.05):
    return it.tick(price, size)



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
        ok, why, adj = result
        try:
            adj = int(adj)
        except (TypeError, ValueError):
            return False, "order check quantity unverified"
        if ok is not True or adj != int(order["quantity"]) or adj <= 0:
            return False, (why or "broker check did not clear 1 lot")
    return True, None


UPSTOX_PLACE_URL = "https://api-hft.upstox.com/v2/order/place"


def _upstox_place_body(order):
    """LIMIT body for the HFT place endpoint, same fields as ST/EMA and index entries."""
    return {
        "quantity": order["quantity"],
        "product": order.get("product") or it.UPSTOX_PRODUCT,
        "validity": "DAY",
        "price": order["price"],
        "tag": order.get("tag") or TAG,
        "instrument_token": order["instrument_token"],
        "order_type": order.get("order_type") or "LIMIT",
        "transaction_type": order["transaction_type"],
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }


def _place_upstox(api_mod, order):
    """Place through api.call. api.post does not exist and used to crash this path."""
    reply = api_mod.call(UPSTOX_PLACE_URL, _upstox_place_body(order), "POST", proxy=True)
    if not isinstance(reply, dict):
        return None
    data = reply.get("data")
    if not isinstance(data, dict):
        return None
    return data.get("order_id")


def _send_pair(place, orders):
    """Send both broker legs. A failure on one is logged and the other is still attempted."""
    if place is None:
        return [], "sender unverified"
    sent = []
    errors = []
    legs = [("upstox", orders[0])]
    if len(orders) > 1 and _kite_writes_allowed():
        legs.append(("kite", orders[1]))
    for broker, order in legs:
        try:
            order_id = place(broker, order)
        except Exception as exc:
            msg = "%s send failed: %s" % (broker, type(exc).__name__)
            errors.append(msg)
            print(msg, file=sys.stderr)
            continue
        if not order_id:
            msg = "%s rejected" % broker
            errors.append(msg)
            print(msg, file=sys.stderr)
            continue
        sent.append({"broker": broker, "order_id": order_id, "quantity": order["quantity"]})
    if not errors:
        return sent, None
    suffix = "; other leg still attempted" if len(errors) == 1 else "; both legs attempted"
    return sent, "; ".join(errors) + suffix


ENTRY_FILL_WAIT_S = 120.0     # same 2-minute rule as auto_entry / full_auto_add
_TERMINAL = ("complete", "rejected", "cancelled")


def settle_entries(sent, status_fn, cancel_fn, budget=ENTRY_FILL_WAIT_S, step=5.0, sleep=None, clock=None):
    """Wait up to `budget` seconds for each sent entry leg; cancel any leg still open.

    17 Nov 2026: a DAY limit left working can fill hours later, after the signal and the
    1h checks are stale (Kite SENSEX 82200 CE: an 11:09 limit filled 13:08 and was exited
    at a loss a minute later). Entry limits on this path are 1 lot and marketable, so an
    unfilled leg after 2 minutes is cancelled. A partly filled leg keeps its fill; only the
    remainder is cancelled. Never raises. Returns one row per leg.
    """
    import time as _t
    sleep = sleep or _t.sleep
    clock = clock or _t.time
    rows = []
    for leg in sent or []:
        if isinstance(leg, dict) and leg.get("order_id") and leg.get("broker"):
            rows.append({"broker": leg["broker"], "order_id": leg["order_id"], "status": None, "cancelled": False})
    end = clock() + float(budget)
    while True:
        open_rows = [r for r in rows if r["status"] not in _TERMINAL]
        if not open_rows:
            break
        for r in open_rows:
            try:
                st = status_fn(r["broker"], r["order_id"])
            except Exception:
                st = None
            if st:
                r["status"] = str(st).lower()
        if all(r["status"] in _TERMINAL for r in rows) or clock() >= end:
            break
        sleep(step)
    for r in rows:
        if r["status"] in _TERMINAL:
            continue
        try:
            res = cancel_fn(r["broker"], r["order_id"])
        except Exception as exc:
            res = None
            r["cancel_error"] = type(exc).__name__
        r["cancelled"] = bool(res)
        r["reason"] = "not filled in %ds - cancel %s" % (int(budget), "sent" if res else "FAILED, check the broker")
    return rows


def _position_row(positions, broker, symbol):
    want = (symbol or "").upper()
    for pos in (positions or {}).get(broker) or []:
        sym = (pos.get("trading_symbol") or pos.get("tradingsymbol") or "").upper()
        if sym == want:
            return pos
    return {}


def _exit_limit(market, order, positions):
    last = None
    row = _position_row(positions, order.get("broker"), order.get("trading_symbol"))
    try:
        last = float(row.get("last_price") or row.get("ltp") or 0)
    except (TypeError, ValueError):
        last = None
    if not last or last <= 0:
        quote = None
        if market.get("quote") and (order.get("instrument_token") or row.get("instrument_token")):
            try:
                quote = market["quote"](order.get("instrument_token") or row.get("instrument_token") or row.get("instrument_key"))
            except Exception:
                quote = None
        if quote:
            try:
                last = float(quote.get("ltp") or 0)
            except (TypeError, ValueError):
                last = None
    if not last or last <= 0:
        return None
    return tick(last * 0.995, 0.05)


def _quote(market, contract):
    quote = market.get("quote")
    if quote is None or not isinstance(contract, dict):
        return None
    try:
        return quote(contract.get("instrument_key"))
    except Exception:
        return None


def _bars_for(market, name, contract=None):
    """Prefer injected hour/daily bars; otherwise call analyze_pair."""
    hour = (market.get("hour_bars") or {}).get(name) if isinstance(market.get("hour_bars"), dict) else None
    daily = (market.get("daily_bars") or {}).get(name) if isinstance(market.get("daily_bars"), dict) else None
    if hour is not None or daily is not None:
        return hour, daily
    analyze = market.get("analyze_pair")
    if analyze is None:
        return None, None
    key = None
    if isinstance(contract, dict):
        key = contract.get("underlying_key")
    if not key and isinstance(market.get("equity_keys"), dict):
        key = market["equity_keys"].get(name)
    try:
        pair = analyze(key or name)
    except Exception:
        return None, None
    if isinstance(pair, tuple) and len(pair) == 2:
        return pair[0], pair[1]
    if isinstance(pair, dict):
        return pair.get("hour"), pair.get("daily")
    return None, None


def _map_contract(market, row):
    if not isinstance(row, dict) or not market.get("map_kite"):
        return row, False
    try:
        mapped = market["map_kite"](row)
    except Exception:
        mapped = None
    if not mapped:
        return None, True
    return dict(row, **mapped), False


def run(now, market=None, *, execute=False, dry=True, ledger_path=None,
        shortlist=None, report=None, session_ok=None, results=None,
        flatten_only=False):
    """Decide and (only when execute and not dry) place. Flatten is never blocked by the loss cap."""
    market = market or {}
    loaded = shortlist if isinstance(shortlist, dict) and "candidates" in (shortlist or {}) else it.load_shortlist()
    clock = it._as_ist(now)
    today = None if clock is None else clock.date()
    result = {
        "ok": bool(loaded.get("ok")),
        "reason": loaded.get("reason"),
        "execute": bool(execute) and not dry,
        "execute_requested": bool(execute),
        "dry_run": bool(dry),
        "enable_env": it.ENABLE_ENV,
        "shortlist": [c["symbol"] for c in (loaded.get("candidates") or [])],
        "max_names": loaded.get("max_names") or it.MAX_NAMES,
        "lots_per_broker": it.stock_lots(),
        "premium_cap_per_broker": it.PREMIUM_CAP_PER_BROKER,
        "product": {"upstox": it.UPSTOX_PRODUCT, "kite": it.KITE_PRODUCT},
        "tag": TAG,
        "source": it.SOURCE,
        "actions": [],
        "orders_placed": 0,
        "flatten_orders": [],
        "preview": [],
    }
    if not loaded.get("ok"):
        return result
    expiry = loaded.get("expiry")
    if report and isinstance(report, dict) and (report.get("expiry") or ((report.get("data") or {}).get("expiry") or {}).get("nov_monthly")):
        expiry = report.get("expiry") or ((report.get("data") or {}).get("expiry") or {}).get("nov_monthly") or expiry
    expiry_week = it.in_expiry_week(expiry, today) if (expiry and today) else None
    result["expiry"] = expiry
    result["expiry_week"] = expiry_week
    window = it.entry_window(now, session_ok=session_ok)
    result["window"] = window
    flat_plan = it.same_day_flatten_plan(now, session_ok=session_ok)
    result["flatten"] = flat_plan
    ledger = it.read_ledger(ledger_path) if ledger_path else {"names": {}, "days": {}}
    if ledger_path and ledger is None:
        result["ok"] = False
        result["reason"] = "intraday ledger unverified"
        return result
    positions = market.get("positions") if isinstance(market.get("positions"), dict) else {"upstox": [], "kite": []}
    # Flatten first, and always independently of the daily loss cap.
    if flat_plan.get("due") or flatten_only:
        book = it.flatten_orders(now, ledger, positions, session_ok=session_ok)
        result["flatten_orders"] = book
        if flatten_only and not book.get("due"):
            result["reason"] = book.get("reason") or flat_plan.get("reason")
            return result
        placed = 0
        if execute and not dry and book.get("orders"):
            place = market.get("place")
            if place is None:
                result["flatten_orders"] = dict(book, send=False, reason="sender unverified")
            else:
                sent, skipped = [], []
                for order in book["orders"]:
                    if (order.get("broker") or "").lower() == "kite" and not _kite_writes_allowed():
                        skipped.append(dict(order, reason="kite disabled by user 21 Nov"))
                        continue
                    price = _exit_limit(market, order, positions)
                    token = order.get("instrument_token") or _position_row(positions, order.get("broker"), order.get("trading_symbol")).get("instrument_token")
                    if price is None or not token:
                        skipped.append(dict(order, reason="exit price or instrument unverified"))
                        continue
                    payload = dict(order, price=price, instrument_token=token, send=True)
                    try:
                        order_id = place(order["broker"], payload)
                    except Exception as exc:
                        skipped.append(dict(order, reason="send failed: %s" % type(exc).__name__))
                        continue
                    if not order_id:
                        skipped.append(dict(order, reason="broker rejected flatten"))
                        continue
                    sent.append(dict(payload, order_id=order_id))
                    placed += 1
                result["flatten_orders"] = dict(book, send=True, sent=sent, skipped=skipped)
                result["orders_placed"] += placed
        elif book.get("orders"):
            result["flatten_orders"] = dict(book, reason=("dry-run; no flatten sent" if dry else "STOCKOPT_INTRADAY_TECH_EXECUTE is off; no flatten sent"))
        if flatten_only:
            return result

    report_rows = (report or {}).get("rows") if isinstance(report, dict) else {}
    drafts = []
    prepared = {}
    held_names = it.held_underlyings(positions)
    books_readable = held_names is not None
    already = it.entered_today(ledger, today) if today else set()
    reserved = set(already)
    if held_names:
        reserved |= set(held_names)
    chains = market.get("chains") if isinstance(market.get("chains"), dict) else {}

    for cand in loaded["candidates"]:
        name = cand["symbol"]
        chain = chains.get(name)
        chain_count = None if chain is None and "chains" not in market else (len(chain) if chain else 0)
        # A preview from the close report may omit live chains; treat as present
        # so ranking can still run. Live entry without a chain still skips later
        # when quantity/contract cannot be built.
        if chain is None and report_rows.get(name) and "chains" not in market:
            chain_count = 1
        hour, daily = _bars_for(market, name)
        if hour is None and report_rows.get(name):
            hour, daily, _meta = it.bars_from_report_row(report_rows[name])
            if _meta.get("results_in_window"):
                cand = dict(cand, results_in_window=True)
        close = it._num(hour, "close")
        contract = None
        mapping_failed = False
        if chain and close is not None and today is not None:
            want_exp = None
            if expiry:
                try:
                    want_exp = dt.date.fromisoformat(str(expiry)[:10])
                except (TypeError, ValueError):
                    want_exp = None
            contract = it.pick_monthly_atm(chain, cand["side"], close, today, expiry=want_exp)
            contract, mapping_failed = _map_contract(market, contract)
        quote = _quote(market, contract) if contract else None
        held = None if not books_readable else (name in held_names)
        decision = it.evaluate_name(
            cand, hour, daily, today=today, chain_count=chain_count, quote=quote,
            contract=contract, held=bool(held), entered_today=name in already,
            books_readable=books_readable, results=results, expiry_week=expiry_week)
        if mapping_failed and decision.get("action") == "enter":
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = "Kite symbol mapping unavailable"
        prepared[name] = {"contract": contract, "quote": quote}
        if decision.get("action") == "enter":
            it.attach_entry_premium(decision, quote=quote, contract=contract, tick_fn=tick)
        drafts.append(decision)

    deployed = it.path_premium_deployed(positions, ledger)
    if deployed is None and books_readable:
        # Positions are present but a path row has no usable average price.
        deployed = "unverified"
    result["premium_cap_per_broker"] = it.PREMIUM_CAP_PER_BROKER
    result["path_premium_open"] = deployed if deployed != "unverified" else None
    if deployed == "unverified":
        result["path_premium_open_unverified"] = True
    technically = [row for row in drafts if row.get("action") == "enter"]
    selected, chosen = it.select_entries(
        drafts, max_names=loaded.get("max_names") or it.MAX_NAMES, reserved=already,
        deployed=deployed, premium_cap=it.PREMIUM_CAP_PER_BROKER)
    if not window["ok"]:
        for decision in selected:
            if decision.get("action") == "enter":
                decision["action"] = "skip"
                decision["lots"] = 0
                decision["quantity"] = 0
                decision["reason"] = window["reason"]
                decision["would_enter"] = True
    # Preview is who would actually be taken after the name cap and the 20k cap.
    preview_rows = [row for row in selected if row.get("action") == "enter" or row.get("would_enter")]
    cap = loaded.get("max_names") or it.MAX_NAMES
    def _preview(row):
        item = {"underlying": row["underlying"], "side": row.get("side"), "strength": row.get("strength"),
                "cleanliness": row.get("cleanliness"), "adx": (row.get("hour") or {}).get("adx"),
                "contract": row.get("trading_symbol"), "reason": row.get("reason")}
        if row.get("premium") is not None:
            item["premium"] = row.get("premium")
        if row.get("limit_price") is not None:
            item["limit_price"] = row.get("limit_price")
        return item
    result["preview"] = [_preview(row) for row in preview_rows]
    result["still_valid"] = [
        {"underlying": row["underlying"], "side": row.get("side"), "strength": row.get("strength"),
         "cleanliness": row.get("cleanliness"), "adx": (row.get("hour") or {}).get("adx"),
         "premium": row.get("premium")}
        for row in sorted(technically, key=it.rank_key)
    ]
    # Preview should show who still qualifies even outside the window.
    if not result["preview"]:
        preview = it.preview_from_report(loaded, report or {"rows": report_rows}, today=today,
                                         results=results, expiry_week=False if expiry_week is False else expiry_week)
        if preview.get("ok"):
            result["preview"] = [
                {"underlying": row["underlying"], "side": row.get("side"), "strength": row.get("strength"),
                 "cleanliness": row.get("cleanliness"), "adx": (row.get("hour") or {}).get("adx"),
                 "contract": row.get("trading_symbol"), "reason": row.get("reason")}
                for row in preview.get("chosen") or []
            ]
            result["preview_note"] = "ranked from the close report; not an order"

    import daily_loss_cap as dlc
    _books = positions if isinstance(positions, dict) else {}
    try:
        loss_cap = dlc.evaluate(_books.get("upstox"), _books.get("kite"), now,
                                state_path=dlc.default_state_path(), persist=bool(execute and not dry))
    except Exception as exc:
        loss_cap = {"ok": False, "blocked": True, "reason": "daily loss cap unverified: %s" % type(exc).__name__}
    result["daily_loss_cap"] = loss_cap

    for decision in selected:
        name = decision["underlying"]
        if decision["action"] != "enter":
            decision["orders_placed"] = False
            decision["send"] = False
            result["actions"].append(decision)
            continue
        if not loss_cap.get("ok"):
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = loss_cap.get("reason") or "daily loss cap blocked"
            result["actions"].append(decision)
            continue
        if not execute or dry:
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = "dry-run; no order" if dry else "STOCKOPT_INTRADAY_TECH_EXECUTE is off; no order"
            result["actions"].append(decision)
            continue
        if not ledger_path or ledger is None:
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = "intraday ledger unverified"
            result["actions"].append(decision)
            continue
        contract = prepared.get(name, {}).get("contract")
        quote = prepared.get(name, {}).get("quote")
        price = it.limit_price(quote, tick)
        if price is None or not isinstance(contract, dict):
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["reason"] = "contract or limit unverified"
            decision["orders_placed"] = False
            result["actions"].append(decision)
            continue
        it.attach_entry_premium(decision, quote=quote, contract=contract, tick_fn=tick)
        if decision.get("action") != "enter":
            result["actions"].append(decision)
            continue
        live_open = it.path_premium_deployed(positions, ledger)
        if live_open is None:
            live_open = "unverified"
        why = it.premium_cap_reason(it.decision_premiums(decision), live_open, cap=it.PREMIUM_CAP_PER_BROKER)
        if why:
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = why
            result["actions"].append(decision)
            continue
        due_at = it.flatten_time(now)
        upstox, kite = it.mirror_orders(contract, price, due_at)
        if not upstox or not kite:
            decision["action"] = "skip"
            decision["reason"] = "mirror order unverified"
            decision["orders_placed"] = False
            result["actions"].append(decision)
            continue
        ok, why = _check_pair(market.get("check_order"), upstox, kite)
        if not ok:
            decision["action"] = "skip"
            decision["lots"] = 0
            decision["quantity"] = 0
            decision["orders_placed"] = False
            decision["send"] = False
            decision["reason"] = why or "broker check did not clear 1 lot"
            if why and "OVERSIZE" in str(why).upper():
                decision["alert"] = why
                result.setdefault("alerts", []).append(why)
            result["actions"].append(decision)
            continue
        sent, err = _send_pair(market.get("place"), (upstox, kite))
        if sent and callable(market.get("settle")):
            try:
                decision["settle"] = market["settle"](sent)
            except Exception as exc:
                decision["settle"] = {"error": type(exc).__name__}
        if err or len(sent) != 2:
            decision["action"] = "skip" if not sent else "partial"
            decision["orders_placed"] = bool(sent)
            decision["send"] = bool(sent)
            decision["reason"] = err or "send unverified"
            decision["sent"] = sent
            result["orders_placed"] += len(sent)
            result["actions"].append(decision)
            continue
        remembered = it.remember_entry(
            ledger_path, name, day=today, contract=contract,
            flatten_at=due_at.isoformat(timespec="minutes"), side=decision.get("side"),
            limit_price=price, premium=it.entry_premium(contract.get("lot_size"), price),
            lot_size=contract.get("lot_size"))
        decision["orders_placed"] = True
        decision["send"] = True
        decision["sent"] = sent
        decision["remembered"] = remembered
        decision["reason"] = "entered 1 lot intraday on both brokers"
        result["orders_placed"] += 2
        already.add(name)
        # Count this fill toward the 20k cap for any later name in the same pass.
        if isinstance(positions.get("upstox"), list):
            positions["upstox"].append({
                "trading_symbol": upstox.get("trading_symbol"), "quantity": upstox["quantity"],
                "product": upstox.get("product"), "average_price": price, "tag": TAG, "source": it.SOURCE,
            })
        if isinstance(positions.get("kite"), list):
            positions["kite"].append({
                "tradingsymbol": kite.get("trading_symbol"), "quantity": kite["quantity"],
                "product": kite.get("product"), "average_price": price, "tag": TAG, "source": it.SOURCE,
            })
        result["actions"].append(decision)
    return result


def _results_status(calendar_path, now):
    names = set()
    if not calendar_path:
        return {"ok": False, "names": [], "reason": "results calendar unverified"}
    try:
        if not os.path.exists(calendar_path):
            return {"ok": True, "names": []}
        raw = json.load(open(calendar_path))
    except Exception:
        return {"ok": False, "names": [], "reason": "results calendar unverified"}
    today = now.date() if isinstance(now, dt.datetime) else dt.datetime.now(IST).date()
    if isinstance(raw, dict):
        for sym, row in raw.items():
            if not isinstance(row, dict) or not row.get("expected_results_date"):
                continue
            try:
                due = dt.date.fromisoformat(str(row["expected_results_date"])[:10])
            except (TypeError, ValueError):
                return {"ok": False, "names": [], "reason": "results calendar unverified"}
            if due >= today:
                names.add(str(sym).strip().upper())
    return {"ok": True, "names": sorted(names)}


def _live_market(names, now):
    """Broker adapters. Used only by the command line, never by the unit tests."""
    import api
    import broker_kite as bk
    import ind
    import trade_lock as tl
    import stockopt_tech_entry as tech

    rows = tech.master(now.date())
    chains = {name: [] for name in names}
    equity_keys = {}
    for row in rows or []:
        if row.get("segment") == "NSE_EQ" and row.get("instrument_type") == "EQ" and row.get("instrument_key"):
            sym = (row.get("trading_symbol") or "").upper().replace("-EQ", "")
            if sym in names:
                equity_keys[sym] = row["instrument_key"]
        if row.get("segment") != "NSE_FO" or row.get("instrument_type") not in ("CE", "PE"):
            continue
        underlying = (row.get("underlying_symbol") or row.get("name") or "").upper()
        if underlying not in chains or not row.get("underlying_key") or not row.get("expiry"):
            continue
        expiry = dt.datetime.fromtimestamp(row["expiry"] / 1000, IST).date()
        if expiry < now.date():
            continue
        chains[underlying].append(dict(row, expiry_date=expiry))

    def analyze_pair(key):
        hour_cs = ind.candles(key, it.SIGNAL_UNIT, it.SIGNAL_INTERVAL, it.SIGNAL_DAYS)
        daily_cs = ind.candles(key, it.DAILY_UNIT, it.DAILY_INTERVAL, it.DAILY_DAYS)
        hour_cs = it.drop_incomplete(hour_cs, it.SIGNAL_UNIT, it.SIGNAL_INTERVAL, now)
        daily_cs = it.drop_incomplete(daily_cs, it.DAILY_UNIT, it.DAILY_INTERVAL, now)
        return it.pack_bars(hour_cs), it.pack_bars(daily_cs)

    def quote(key):
        import urllib.parse
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
        return {"kite_symbol": mapped.get("tradingsymbol") or mapped.get("trading_symbol"),
                "kite_exchange": mapped.get("exchange") or "NFO",
                "kite_lot_size": mapped.get("lot_size") or contract.get("lot_size")}

    def check_order(order, broker):
        return tl.check_order(order, broker=broker)

    def _order_status(broker, oid):
        if broker == "upstox":
            d = (api.get("/v2/order/details?order_id=" + str(oid)) or {}).get("data") or {}
            return d.get("status")
        st = bk.order_status(oid)
        return (st or {}).get("status")

    def _order_cancel(broker, oid):
        if broker == "upstox":
            r = api.call("https://api-hft.upstox.com/v2/order/cancel?order_id=" + str(oid), None, "DELETE", proxy=True)
            return isinstance(r, dict) and r.get("status") == "success"
        return bk.cancel(oid)

    def settle(sent):
        return settle_entries(sent, _order_status, _order_cancel)

    def place(broker, order):
        if broker == "upstox":
            return _place_upstox(api, order)
        return bk.place(order.get("kite_exchange") or "NFO", order["trading_symbol"], order["transaction_type"],
                        order["quantity"], order["price"], tag=TAG, product=order.get("product") or it.KITE_PRODUCT)

    return {"chains": chains, "equity_keys": equity_keys, "analyze_pair": analyze_pair,
            "quote": quote, "positions": positions(), "map_kite": map_kite,
            "check_order": check_order, "place": place, "settle": settle}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Intraday strong-technical stock-option entry")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from-report", nargs="?", const="auto", default=None,
                        help="Rank from a saved strong-technicals JSON; no broker calls")
    parser.add_argument("--shortlist", default=it.SHORTLIST_PATH)
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--flatten-only", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    now = dt.datetime.now(IST)
    loaded = it.load_shortlist(args.shortlist)
    if not loaded.get("ok"):
        print(json.dumps({"ok": False, "reason": loaded.get("reason"), "orders_placed": 0}))
        return 1
    report = None
    report_path = args.from_report
    if report_path:
        if report_path == "auto":
            report_path = it.latest_report_path()
        report = it.load_report(report_path)
    try:
        import market_calendar
        session_ok = bool(market_calendar.is_trading_day(now.date()))
    except Exception:
        session_ok = None
    results = _results_status(args.calendar, now)
    market = {}
    if not args.from_report:
        try:
            market = _live_market([c["symbol"] for c in loaded["candidates"]], now)
        except Exception as exc:
            print(json.dumps({"ok": False, "reason": "market unverified: %s" % type(exc).__name__,
                              "orders_placed": 0}))
            return 1
    elif report and report.get("ok"):
        # Provide report bars only; do not open a broker.
        hour_bars, daily_bars = {}, {}
        for name, row in (report.get("rows") or {}).items():
            hour, daily, _meta = it.bars_from_report_row(row)
            hour_bars[name] = hour
            daily_bars[name] = daily
        market = {"hour_bars": hour_bars, "daily_bars": daily_bars,
                  "positions": {"upstox": [], "kite": []}}
    result = run(now, market, execute=it.execute_enabled(), dry=args.dry_run or bool(args.from_report),
                 ledger_path=args.ledger, shortlist=loaded, report=report,
                 session_ok=session_ok, results=results, flatten_only=args.flatten_only)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
