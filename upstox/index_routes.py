"""Index-option entry routes (user t673u, confirmed 16 Nov 2026 20:55 IST).

NIFTY, BANKNIFTY and SENSEX, per broker. Three independent routes; each may hold
its own position on an index (up to 3 per index per broker):

  3m  : completed 3-minute futures candles, INTRADAY (Upstox I / Kite MIS),
        current-week (nearest listed) option. Squared off the same day by
        index_3m_squareoff.py, which closes 3m-route positions only.
  15m : completed 15-minute candles, delivery (Upstox D / Kite NRML),
        next-week option (NIFTY/SENSEX). BANKNIFTY lists monthlies only, so its
        15m route uses the 1h monthly rule (open question in the hand-off).
  1h  : completed 1-hour candles, delivery, monthly option; when the monthly
        expiry falls in the current ISO week, next month's contract.

Entry window 09:30-15:25 IST. Two tighter cut-offs stay inside it: the 3m
(intraday) route takes no new entry after 15:10 (broker MIS square-off starts
~15:15-15:20), and on a Friday no route enters after 15:05 (Friday 15:10 flatten).

Signal: the existing fresh EMA20/Supertrend cross on the last completed bar of
the route's own timeframe, with MACD histogram in direction and ADX >= 18.
Additionally, when the route has no open position on that index for a broker,
a "previous-day trend continuation" entry is allowed without a fresh cross: the
last completed bar of the previous session and the current completed bar (from
today) are on the same side (close vs EMA20 AND Supertrend), with MACD and ADX
still confirming. All existing guards stay downstream of this module.

Positions are tracked per route in index_route_positions.json. An open index
option with no record (opened before this change, or manually) is classified
read-only by inference and never rewritten: intraday product -> 3m, an expiry
that is a monthly (futures) date -> 1h, otherwise -> 15m. Nothing here reads a
broker or places an order.
"""
import datetime as dt
import json
import os
import tempfile

D = os.path.dirname(os.path.abspath(__file__))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEXES = ("NIFTY", "BANKNIFTY", "SENSEX")
WEEKLY = ("NIFTY", "SENSEX")
MONTHLY_ONLY = ("BANKNIFTY",)
ROUTES = ("3m", "15m", "1h")
SPEC = {
    "3m":  {"minutes": 3,  "upstox_product": "I", "kite_product": "MIS",  "intraday": True,
            "role": "current_week", "history_days": 5},
    "15m": {"minutes": 15, "upstox_product": "D", "kite_product": "NRML", "intraday": False,
            "role": "next_week", "history_days": 30},
    "1h":  {"minutes": 60, "upstox_product": "D", "kite_product": "NRML", "intraday": False,
            "role": "monthly", "history_days": 30},
}
ENTRY_START = dt.time(9, 30)
ENTRY_END = dt.time(15, 25)
INTRADAY_ENTRY_END = dt.time(15, 10)
FRIDAY_ENTRY_END = dt.time(15, 5)
SQUAREOFF_AT = dt.time(15, 12)
STATE = os.environ.get("INDEX_ROUTE_STATE") or os.path.join(D, "index_route_positions.json")
INTRADAY_PRODUCTS = ("I", "MIS", "INTRADAY")


def enabled_routes(env=None):
    """INDEX_ROUTES=3m,15m,1h (default all three). Unknown names are dropped, never guessed."""
    env = os.environ if env is None else env
    raw = (env.get("INDEX_ROUTES") or ",".join(ROUTES)).replace(" ", "")
    out = [r for r in raw.split(",") if r in ROUTES]
    return out or list(ROUTES)


def route(underlying, name):
    if underlying not in INDEXES:
        return {"ok": False, "reason": f"{underlying} is not an index this entry path trades", "minutes": None, "name": None}
    if name not in SPEC:
        return {"ok": False, "reason": f"route {name} is not 3m/15m/1h", "minutes": None, "name": None}
    return {"ok": True, "reason": None, "minutes": SPEC[name]["minutes"], "name": name}


def in_window(name, now):
    """(ok, reason). Shared 09:30-15:25 window plus the 3m and Friday cut-offs."""
    t = now.time()
    if now.weekday() >= 5:
        return False, "weekend"
    if not (ENTRY_START <= t <= ENTRY_END):
        return False, "outside 09:30-15:25 IST entry window"
    if now.weekday() == 4 and t > FRIDAY_ENTRY_END:
        return False, "Friday: no new entry after 15:05 IST (15:10 flatten)"
    if SPEC[name]["intraday"] and t > INTRADAY_ENTRY_END:
        return False, "3m intraday route: no new entry after 15:10 IST (same-day square-off)"
    return True, None


# ---------------- expiry ----------------
def _tag(raw):
    if isinstance(raw, dict):
        day = raw.get("expiry")
        if isinstance(day, dt.datetime):
            day = day.date()
        fam = raw.get("family")
        if fam not in ("weekly", "monthly"):
            fam = "weekly" if raw.get("weekly") is True else ("monthly" if raw.get("weekly") is False else None)
        return day, fam
    return (raw.date() if isinstance(raw, dt.datetime) else raw), None


def _wk(day):
    return day.isocalendar()[:2]


def _next_wk(day):
    return (day + dt.timedelta(days=7)).isocalendar()[:2]


def select_expiry(underlying, name, session, option_expiries, monthly_expiries):
    """Listed dates only; nothing is invented or substituted.

    3m : nearest listed expiry on/after the session (current week; BANKNIFTY: current monthly).
    15m: NIFTY/SENSEX the first listed expiry in the ISO week after the current-week expiry.
         BANKNIFTY (no weeklies) uses the 1h monthly rule.
    1h : first monthly (a futures-expiry date or a row tagged monthly) that is after the
         session and NOT in the session's ISO week; a monthly in the current week rolls to
         the next month.
    """
    rt = route(underlying, name)
    if not rt["ok"]:
        return {"ok": False, "reason": rt["reason"], "expiry": None, "role": None}
    if isinstance(session, dt.datetime):
        session = session.date()
    monthly = set(monthly_expiries or [])
    rows = sorted({_tag(r) for r in (option_expiries or []) if _tag(r)[0] is not None and _tag(r)[0] >= session},
                  key=lambda x: x[0])
    if not rows:
        return {"ok": False, "reason": "no listed option expiry on or after the session", "expiry": None, "role": None}
    days = sorted({d for d, _ in rows})
    fam = {}
    for d, f in rows:
        f2 = "monthly" if (f == "monthly" or (f is None and d in monthly)) else (f or "weekly")
        if fam.get(d) != "monthly":
            fam[d] = f2
    if underlying in MONTHLY_ONLY:
        days = [d for d in days if fam[d] == "monthly"]
        if not days:
            return {"ok": False, "reason": "BANKNIFTY monthly is not listed", "expiry": None, "role": None}
    if name == "3m":
        cur = days[0]
        return {"ok": True, "reason": None, "expiry": cur, "role": "current_week", "family": fam[cur],
                "route": name, "rule": "nearest listed expiry (current week)"}
    if name == "15m" and underlying in WEEKLY:
        cur = days[0]
        want = _next_wk(cur)
        nxt = [d for d in days if _wk(d) == want]
        if not nxt:
            return {"ok": False, "reason": "next-week expiry is not listed; nothing substituted",
                    "expiry": None, "role": None, "current_week": cur.isoformat()}
        return {"ok": True, "reason": None, "expiry": nxt[0], "role": "next_week", "family": fam[nxt[0]],
                "route": name, "current_week": cur.isoformat(), "rule": "week after the current-week expiry"}
    # 1h (all indices) and BANKNIFTY 15m: monthly, rolled out of the current ISO week
    mons = [d for d in days if fam[d] == "monthly" and d > session]
    skipped = [d.isoformat() for d in mons if _wk(d) == _wk(session)]
    mons = [d for d in mons if _wk(d) != _wk(session)]
    if not mons:
        return {"ok": False, "reason": "no listed monthly outside the current week; nothing substituted",
                "expiry": None, "role": None, "skipped_current_week": skipped}
    return {"ok": True, "reason": None, "expiry": mons[0], "role": "monthly", "family": "monthly", "route": name,
            "skipped_current_week": skipped,
            "rule": ("BANKNIFTY has no weekly; 15m uses the monthly rule" if name == "15m" else
                     "monthly; current-week monthly rolls to next month")}


# ---------------- signal ----------------
def side_of(bar):
    if bar is None:
        return None
    if bar["close"] > bar["ema20"] and bar["dir"] == 1:
        return "CE"
    if bar["close"] < bar["ema20"] and bar["dir"] == -1:
        return "PE"
    return None


def _day(ts):
    return dt.datetime.fromisoformat(str(ts)).date()


def continuation(bars, session, adx_min=18):
    """Previous-day trend still in force on this route's timeframe -> (side or None, detail)."""
    if not bars:
        return None, {"why": "no closed bars"}
    cur = bars[-1]
    if _day(cur["ts"]) != session:
        return None, {"why": "no completed bar from today yet"}
    prev = [b for b in bars if _day(b["ts"]) < session]
    if not prev:
        return None, {"why": "no previous-session bar in history"}
    p = prev[-1]
    ps, cs = side_of(p), side_of(cur)
    det = {"prev_day_bar": p["ts"], "prev_day_side": ps, "current_bar": cur["ts"], "current_side": cs,
           "hist": cur.get("hist"), "adx": cur.get("adx")}
    if ps is None or cs != ps:
        det["why"] = "previous-day trend not in force (prev %s, now %s)" % (ps, cs)
        return None, det
    macd = (ps == "CE" and cur["hist"] > 0) or (ps == "PE" and cur["hist"] < 0)
    if not macd:
        det["why"] = "previous-day %s trend continues but MACD hist %s against it" % (ps, cur["hist"])
        return None, det
    if cur["adx"] < adx_min:
        det["why"] = "previous-day %s trend continues but ADX %s < %s" % (ps, cur["adx"], adx_min)
        return None, det
    det["why"] = "previous-day %s trend continues (no fresh cross needed; route has no open position)" % ps
    return ps, det


# ---------------- per-route position state ----------------
def ukey(instrument_key):
    return "upstox|%s" % instrument_key


def kkey(exchange, tsym):
    return "kite|%s|%s" % (exchange, tsym)


def load(path=None):
    try:
        with open(path or STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(state, path=None):
    path = path or STATE
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".route_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=1, default=str)
    os.replace(tmp, path)


def record(state, key, *, route_name, index, symbol, product, qty, ts, broker, order_id=None):
    pos = state.setdefault("positions", {})
    pos[key] = {"route": route_name, "index": index, "symbol": symbol, "product": product, "qty": qty,
                "opened": ts, "broker": broker, "order_id": order_id}
    return state


def refresh_opened(state, key, ts, order_id=None, qty=None):
    """Reset a tracked route's entry time to the current life's opening fill.

    No-op when the key is not already tracked (inferred / untracked positions
    stay unwritten). Returns True when the record was changed.
    """
    rec = ((state or {}).get("positions") or {}).get(key)
    if not rec or rec.get("route") not in ROUTES:
        return False
    new_ts = ts.isoformat() if hasattr(ts, "isoformat") else ts
    changed = False
    if rec.get("opened") != new_ts:
        rec["opened"] = new_ts
        changed = True
    if order_id and rec.get("order_id") != order_id:
        rec["order_id"] = order_id
        changed = True
    if qty is not None and rec.get("qty") != qty:
        rec["qty"] = qty
        changed = True
    return changed


def infer_route(product, expiry, monthly_dates):
    """Read-only classification of an untracked position (never written back)."""
    if str(product or "").upper() in INTRADAY_PRODUCTS:
        return "3m"
    if expiry is not None and expiry in set(monthly_dates or []):
        return "1h"
    return "15m"


def route_of(state, key, product, expiry, monthly_dates):
    rec = ((state or {}).get("positions") or {}).get(key)
    if rec and rec.get("route") in ROUTES:
        return rec["route"], "tracked"
    return infer_route(product, expiry, monthly_dates), "inferred"


def dedupe_route(index, broker, name, occupied, decisions_today):
    """One position per index per route per broker, and one decision per route per broker a day.

    occupied: {route: [symbols]} for this index on this broker, or None if the book was
    unreadable (refuse). A decision row without "route" is an older 1h-path row and counts
    as 1h. A row with no "brokers" field blocks both brokers (conservative)."""
    if occupied is None:
        return {"ok": False, "reason": f"{broker} positions unreadable; {index} {name} entry refused on {broker}"}
    if occupied.get(name):
        return {"ok": False, "reason": f"{index} {name} route already holds {', '.join(occupied[name])} on {broker}; adds are full_auto_add's job"}
    for row in decisions_today or []:
        if row.get("index") != index:
            continue
        rr = row.get("route") or ("1h" if row.get("timeframe") in (None, "1h") else row.get("timeframe"))
        if rr != name:
            continue
        brokers = row.get("brokers")
        if brokers is None or broker in brokers:
            return {"ok": False, "reason": f"{index} {name} route already decided today for {broker}"}
    return {"ok": True, "reason": None}
