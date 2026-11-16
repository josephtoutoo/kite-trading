"""Daily loss cap: block NEW entries (never exits) for the rest of the IST day.

Trips on whichever comes first, across Upstox + Kite together:
  * max_losing_trades  losing CLOSED trades today        (default 2)
  * max_realised_loss_rs realised loss today, in rupees   (default 15000)

Pure logic plus one small latch file. Importing this module reads no broker and places no
order. Callers pass the day's position rows they already fetched:
  Upstox  /v2/portfolio/short-term-positions -> data[]   (fields quantity, realised, trading_symbol)
  Kite    kite.positions()["net"]                       (fields quantity, realised, tradingsymbol)
Pass None for a broker whose book could not be read.

Counting rules
  * A closed trade is a position row with quantity == 0 and realised != 0 (fully flat today).
    For a flat row whose "realised" is 0/missing, "pnl" is used (Kite fills pnl, not realised).
    A partly closed row (quantity != 0) adds its realised rupees but is not counted as a trade.
  * The same contract closed at a loss on BOTH brokers (the mirrored entry) counts as ONE losing
    trade by default (count_mirrors_once). Rupees always add up across both brokers.
  * Scope "derivatives" (default) only looks at option/future rows (trade_lock.is_opt pattern, or
    ...digits...FUT), so MTF / delivery / equity intraday rows do not trip it. Scope "all" counts everything.
  * Once tripped for a date the latch file keeps it tripped until the next IST date, even if a
    later book read shows fewer rows (Upstox drops carried rows, a token expires, ...).
  * Broker unreadable and not yet tripped: blocked if fail_closed (default False), else allowed
    with partial=True so the caller can log it.

Config (later wins): DEFAULTS < JSON file (DAILY_LOSS_CAP_CONFIG or daily_loss_cap.json next to
this file) < env DAILY_LOSS_CAP_MAX_LOSSES / DAILY_LOSS_CAP_MAX_RS / DAILY_LOSS_CAP_ENABLED /
DAILY_LOSS_CAP_SCOPE / DAILY_LOSS_CAP_FAIL_CLOSED.

16 Nov 2026 tally fix (user t669u "Improve code changes", after the performance review):
  * Today's realised rupees are recomputed per row instead of trusting the broker's field.
    Kite leaves realised=0 on a partly closed row (BANKNIFTY 300 open after 600 sold), so a
    +Rs41k booked gain was invisible, and both brokers value a carried position at its original
    cost, so yesterday's mark-to-market was counted as today's loss.
  * Method, best first: "fifo" = today's fills for that contract (callers pass the order books),
    matched FIFO, with the overnight quantity valued at the PREVIOUS CLOSE (close_price);
    "row" = the same FIFO on the position row's day_buy/day_sell averages when fills are missing
    or do not reconcile with the row; "broker" = the old field (realised, pnl on a flat row) when
    the row has no day fields at all.
  * A losing closed trade is still a flat row (quantity 0) whose recomputed realised is < 0.
  * Caps, the latch and the config are unchanged.
"""
import datetime as dt
import json
import os
import re
import tempfile

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "enabled": True,
    "max_losing_trades": 2,
    "max_realised_loss_rs": 15000.0,
    "scope": "derivatives",
    "count_mirrors_once": True,
    "fail_closed": False,
}
# same option test as trade_lock.is_opt (underlying, then expiry digits, then CE/PE) so a stock
# named ...CE (RELIANCE, BAJFINANCE) is not an option; futures carry FUT after digits.
_DERIV = re.compile(r"^[A-Z&\-]+\d.*(CE|PE|FUT)$")


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config(path=None, env=None):
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)
    path = path or env.get("DAILY_LOSS_CAP_CONFIG") or os.path.join(HERE, "daily_loss_cap.json")
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if env.get("DAILY_LOSS_CAP_MAX_LOSSES"):
        cfg["max_losing_trades"] = int(env["DAILY_LOSS_CAP_MAX_LOSSES"])
    if env.get("DAILY_LOSS_CAP_MAX_RS"):
        cfg["max_realised_loss_rs"] = float(env["DAILY_LOSS_CAP_MAX_RS"])
    if env.get("DAILY_LOSS_CAP_ENABLED"):
        cfg["enabled"] = _bool(env["DAILY_LOSS_CAP_ENABLED"])
    if env.get("DAILY_LOSS_CAP_SCOPE"):
        cfg["scope"] = env["DAILY_LOSS_CAP_SCOPE"].strip().lower()
    if env.get("DAILY_LOSS_CAP_FAIL_CLOSED"):
        cfg["fail_closed"] = _bool(env["DAILY_LOSS_CAP_FAIL_CLOSED"])
    cfg["max_losing_trades"] = int(cfg["max_losing_trades"])
    cfg["max_realised_loss_rs"] = abs(float(cfg["max_realised_loss_rs"]))
    return cfg


def default_state_path(env=None):
    env = os.environ if env is None else env
    return env.get("DAILY_LOSS_CAP_STATE") or os.path.join(HERE, "daily_loss_cap_state.json")


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x else x


def _sym(row):
    return str(row.get("trading_symbol") or row.get("tradingsymbol") or row.get("symbol") or "").upper().replace(" ", "")


def _int(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _row_key(row, broker):
    """Contract key shared by a position row and its orders: Upstox instrument key, or KITE|EXCH|SYM."""
    tok = row.get("instrument_token") or row.get("instrument_key")
    if isinstance(tok, str) and "|" in tok:
        return tok
    if broker == "kite":
        return "KITE|%s|%s" % (row.get("exchange") or "", _sym(row))
    return str(tok or _sym(row))


def _fill_ts(o):
    return str(o.get("exchange_update_timestamp") or o.get("exchange_timestamp")
               or o.get("order_timestamp") or o.get("order_time") or "")


def fills_from_orders(orders, broker):
    """{(contract key, product): [(ts, side, qty, price)]} for every order with a filled quantity.
    Accepts the raw broker books or the normalized broker_kite rows. Never raises on odd rows."""
    out = {}
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        side = str(o.get("transaction_type") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        q = _int(o.get("filled_quantity"))
        px = _num(o.get("average_price"))
        if q <= 0 or px <= 0:
            continue
        key = (_row_key(o, broker), str(o.get("product") or o.get("kite_product") or "").upper())
        out.setdefault(key, []).append((_fill_ts(o), side, q, px))
    for v in out.values():
        v.sort()
    return out


def _fifo(carried, carried_px, fills):
    """-> (realised per unit-multiplier, open lots [[qty signed, px]]). Long lots positive, short negative."""
    lots = []
    if carried:
        lots.append([carried, carried_px])
    realised = 0.0
    for _ts, side, q, px in fills:
        sgn = 1 if side == "BUY" else -1
        rem = q
        while rem and lots and (lots[0][0] > 0) != (sgn > 0):
            m = min(rem, abs(lots[0][0]))
            realised += (px - lots[0][1]) * m if lots[0][0] > 0 else (lots[0][1] - px) * m
            lots[0][0] += m if lots[0][0] < 0 else -m
            rem -= m
            if lots[0][0] == 0:
                lots.pop(0)
        if rem:
            lots.append([sgn * rem, px])
    return realised, lots


def row_pnl(row, broker, fills=None):
    """Today's figures for one position row -> dict(realised, day_pnl, method).

    realised: rupees booked today, carried quantity valued at the previous close.
    day_pnl: realised plus the open quantity marked at last_price against the same FIFO lots.
    """
    qty = _int(row.get("quantity"))
    mult = _num(row.get("multiplier")) or 1.0
    ltp = _num(row.get("last_price"))
    carried = _int(row.get("overnight_quantity"))
    close = _num(row.get("close_price"))
    has_day = any(k in row for k in ("day_buy_quantity", "day_sell_quantity"))
    if carried and close <= 0:
        has_day = False          # a carried row with no previous close cannot be revalued
    # FIFO only when today's book actually shows this row's fills, or the row is carry-only (no day
    # activity). A row whose fills are missing from the book is never read as "nothing booked".
    no_day_activity = has_day and not _int(row.get("day_buy_quantity")) and not _int(row.get("day_sell_quantity"))
    if fills is not None and (fills or no_day_activity) and (not carried or close > 0):
        net = carried + sum(q if s == "BUY" else -q for _t, s, q, _p in fills)
        if net == qty:
            r, lots = _fifo(carried, close, fills)
            u = sum((ltp - px) * q for q, px in lots) if ltp else 0.0
            return {"realised": round(r * mult, 2), "day_pnl": round((r + u) * mult, 2), "method": "fifo"}
    if has_day:
        bq, sq = _int(row.get("day_buy_quantity")), _int(row.get("day_sell_quantity"))
        bp, sp = _num(row.get("day_buy_price")), _num(row.get("day_sell_price"))
        fl = []
        if bq:
            fl.append(("0", "BUY", bq, bp))
        if sq:
            # one averaged sell after one averaged buy: matches carry first (FIFO), then day buys
            fl.append(("1", "SELL", sq, sp))
        if carried + bq - sq == qty:
            r, lots = _fifo(carried, close, fl)
            u = sum((ltp - px) * q for q, px in lots) if ltp else 0.0
            return {"realised": round(r * mult, 2), "day_pnl": round((r + u) * mult, 2), "method": "row"}
    realised = _num(row.get("realised"))
    if qty == 0 and realised == 0:
        realised = _num(row.get("pnl"))
    pnl = row.get("pnl")
    day = _num(pnl) if pnl is not None else _num(row.get("unrealised")) + _num(row.get("realised"))
    return {"realised": round(realised, 2), "day_pnl": round(day, 2), "method": "broker"}


def _fills_for(row, broker, book):
    """Today's fills for this row's contract and product. [] = none today (carry-only row);
    None = no order book. Orders without a product field match on the contract alone."""
    if book is None:
        return None
    key = _row_key(row, broker)
    prod = str(row.get("product") or "").upper()
    if (key, prod) in book:
        return book[(key, prod)]
    return book.get((key, ""), [])


def rows_from(rows, broker, scope="derivatives", orders=None):
    """[(broker, symbol, quantity, realised, method)] for rows in scope. Never raises on odd rows.
    orders: that broker's order book for today (None = not available -> row/broker method)."""
    out = []
    book = fills_from_orders(orders, broker) if orders is not None else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = _sym(row)
        if scope != "all" and not _DERIV.match(sym):
            continue
        qty = _int(row.get("quantity"))
        try:
            res = row_pnl(row, broker, _fills_for(row, broker, book))
        except Exception:
            res = {"realised": _num(row.get("realised")) or (_num(row.get("pnl")) if qty == 0 else 0.0), "method": "broker"}
        out.append((broker, sym, qty, _num(res["realised"]), res["method"]))
    return out


def tally(upstox_rows, kite_rows, cfg, upstox_orders=None, kite_orders=None):
    rows = (rows_from(upstox_rows, "upstox", cfg["scope"], upstox_orders)
            + rows_from(kite_rows, "kite", cfg["scope"], kite_orders))
    realised = round(sum(r[3] for r in rows), 2)
    losers = [(b, s) for b, s, q, p, _m in rows if q == 0 and p < 0]
    if cfg.get("count_mirrors_once", True):
        losing = len({s for _, s in losers})
    else:
        losing = len(losers)
    return {"realised_rs": realised, "losing_trades": losing,
            "losing_symbols": sorted({s for _, s in losers}),
            "rows": [{"broker": b, "symbol": s, "qty": q, "realised": round(p, 2), "method": m} for b, s, q, p, m in rows]}


def _read_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path, data):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".dlc", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def evaluate(upstox_rows, kite_rows, now=None, cfg=None, state_path=None, persist=True,
             upstox_orders=None, kite_orders=None):
    """-> dict(ok, blocked, reason, losing_trades, realised_rs, partial, tripped_at, limits).

    ok False means: do not open a NEW position or add. Exits, stops and square-offs must
    never call this. upstox_rows / kite_rows None = that book was unreadable.
    upstox_orders / kite_orders: today's order books, used for the FIFO tally (optional).
    """
    cfg = cfg or load_config()
    now = now or dt.datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    day = now.astimezone(IST).date().isoformat()
    limits = {"max_losing_trades": cfg["max_losing_trades"], "max_realised_loss_rs": cfg["max_realised_loss_rs"]}
    res = {"ok": True, "blocked": False, "reason": None, "date": day, "limits": limits,
           "partial": upstox_rows is None or kite_rows is None, "losing_trades": 0, "realised_rs": 0.0,
           "tripped_at": None}
    if not cfg.get("enabled", True):
        res["reason"] = "daily loss cap disabled"
        return res
    t = tally(upstox_rows, kite_rows, cfg, upstox_orders, kite_orders)
    res.update(t)
    state = _read_state(state_path) if state_path else {}
    latch = (state.get("days") or {}).get(day) if isinstance(state.get("days"), dict) else None
    why = None
    if t["losing_trades"] >= cfg["max_losing_trades"] > 0:
        why = f"{t['losing_trades']} losing closed trades today (cap {cfg['max_losing_trades']})"
    elif t["realised_rs"] <= -cfg["max_realised_loss_rs"] < 0:
        why = f"realised loss Rs{-t['realised_rs']:,.0f} today (cap Rs{cfg['max_realised_loss_rs']:,.0f})"
    if why is None and isinstance(latch, dict) and latch.get("tripped"):
        why = "tripped earlier today: " + str(latch.get("reason"))
        res["tripped_at"] = latch.get("at")
    if why:
        res.update(ok=False, blocked=True, reason="daily loss cap: " + why + "; new entries blocked until tomorrow, exits unaffected")
        if state_path and persist and not (isinstance(latch, dict) and latch.get("tripped")):
            days = state.get("days") if isinstance(state.get("days"), dict) else {}
            days = {k: v for k, v in days.items() if k >= (now.date() - dt.timedelta(days=10)).isoformat()}
            days[day] = {"tripped": True, "reason": why, "at": now.isoformat(),
                         "losing_trades": t["losing_trades"], "realised_rs": t["realised_rs"]}
            _write_state(state_path, {"days": days})
            res["tripped_at"] = now.isoformat()
        return res
    if res["partial"] and cfg.get("fail_closed"):
        res.update(ok=False, blocked=True,
                   reason="daily loss cap: a broker book is unreadable and fail_closed is on; new entries blocked")
    return res
