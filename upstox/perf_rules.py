"""Performance rules, 16 Nov 2026 (user t669u "Improve code changes", after the 16 Nov review).

Pure helpers plus three small state files. Importing this module reads no broker and places
no order. Used by trade_lock.check_order (every bot BUY path), auto_entry.py and risk_manager.py.

  2) 15m trend entry filter: a CE BUY needs the underlying's last CLOSED 15m bar to have
     close > EMA20 AND Supertrend green; a PE needs close < EMA20 AND Supertrend red.
  3) Initial option stop 20% under entry for NEW positions (was 30%). Positions already open
     when this shipped keep 30% as their floor (their live stops are never widened or tightened
     by this change). The stop ladder still only moves up.
  4) Re-entry: after a stop-loss fill or a TREND_EXIT/STOP_BREACH_EXIT on a contract, no bot
     BUY on that contract for the rest of the IST day, and no bot BUY on the same underlying for
     30 minutes. Bot index entries never take a contract expiring within 1 trading day.
  5) Size: index option first entry max 2 lots (1 normal, 2 strong ST/EMA trend), 3 on that
     index's own expiry day; with the one add, max 3 lots in total. Stock options 1 lot per
     broker. Bot orders above that are refused; open positions above it raise one OVERSIZE line
     per position per day (again only if it grows further).
"""
import datetime as dt
import json
import os
import sys
import tempfile

D = os.path.dirname(os.path.abspath(__file__))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

INITIAL_STOP_PCT = 0.20      # new positions from 16 Nov 2026
LEGACY_STOP_PCT = 0.30       # positions already open when the change shipped
STOP_MIGRATION_KEY = "_stop_pct_v2"

TREND15_MIN = 15
TREND15_DAYS = 10

REENTRY_COOLDOWN_MIN = 30
STOP_EXIT_TAGS = ("trailsl", "target_book")

INDEX_BASE_LOTS = 1
INDEX_TREND_LOTS = 2
INDEX_EXPIRY_DAY_LOTS = 3
INDEX_MAX_TOTAL_LOTS = 3
STOCK_OPT_LOTS = 1

# PERF_RULES_STATE_DIR lets offline tests keep these files out of the live folder.
_SD = os.environ.get("PERF_RULES_STATE_DIR") or D
REFUSALS = os.path.join(_SD, "perf_rule_refusals.jsonl")
EXITS = os.path.join(_SD, "reentry_blocks.json")
OVERSIZE_STATE = os.path.join(_SD, "oversize_alerts.json")


def _dry():
    return "--dry-run" in sys.argv


def _now(now=None):
    now = now or dt.datetime.now(IST)
    return now if now.tzinfo else now.replace(tzinfo=IST)


def _read(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".pr", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


# ---------------- 2) 15m trend filter ----------------
def trend15_gate(side, a):
    """(ok, why) for a completed 15m bar dict with close/ema20/dir. Unusable bar -> (False, why)."""
    if side not in ("CE", "PE"):
        return True, "not an option side"
    try:
        c, e, d = float(a["close"]), float(a["ema20"]), int(a["dir"])
    except Exception:
        return False, "15m bar unreadable - entry refused without trend confirmation"
    up = c > e and d == 1
    dn = c < e and d == -1
    txt = "15m@%s close %s ema20 %s ST %s" % (str(a.get("last_ts", ""))[11:16], c, e, "green" if d == 1 else "red")
    if side == "CE":
        return (True, "CE ok: " + txt) if up else (False, "CE needs 15m close > EMA20 and green Supertrend (" + txt + ")")
    return (True, "PE ok: " + txt) if dn else (False, "PE needs 15m close < EMA20 and red Supertrend (" + txt + ")")


def underlying_key(und, index_keys):
    """Upstox key for the underlying's 15m candles: index key, else NSE cash key. None if unknown."""
    if und in index_keys:
        return index_keys[und]
    try:
        with open(os.path.join(D, "eq_keys.json")) as f:
            return json.load(f).get(und)
    except (OSError, ValueError):
        return None


def trend15_check(und, side, index_keys, analyze):
    """-> dict(ok, why, key). analyze is ind.analyze (passed in so tests can stub it)."""
    key = underlying_key(und, index_keys)
    if not key:
        return {"ok": False, "why": "no 15m underlying key for %s - entry refused" % und, "key": None}
    try:
        a = analyze(key, "minutes", TREND15_MIN, TREND15_DAYS)
    except Exception as e:
        return {"ok": False, "why": "15m trend unreadable (%s) - entry refused" % type(e).__name__, "key": key}
    ok, why = trend15_gate(side, a)
    return {"ok": ok, "why": "%s %s" % (und, why), "key": key}


def log_refusal(rec):
    """Append one refusal row (skipped on --dry-run). Never raises."""
    if _dry():
        return
    try:
        rec = dict(rec)
        rec.setdefault("ts", dt.datetime.now(IST).isoformat())
        with open(REFUSALS, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        print("perf_rules: refusal log failed: %s" % type(e).__name__, file=sys.stderr)


# ---------------- 4) re-entry block ----------------
def _ts_of(o):
    for k in ("exchange_update_timestamp", "exchange_timestamp", "order_timestamp"):
        v = o.get(k)
        if not v:
            continue
        try:
            t = dt.datetime.strptime(str(v)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            return t.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def is_stop_exit(o):
    """A filled SELL that was a stop (SL/SL-M, any placer) or a stop-manager exit (tag)."""
    if str(o.get("transaction_type") or "").upper() != "SELL":
        return False
    try:
        if int(float(o.get("filled_quantity") or 0)) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    ot = str(o.get("order_type") or "").upper()
    return ot in ("SL", "SL-M") or str(o.get("tag") or "") in STOP_EXIT_TAGS


def contract_id(token, sym, to_upstox=None):
    """Upstox instrument key for either broker's contract. Kite 'KITE|EXCH|SYM' is mapped with
    broker_kite.to_upstox when given; unmapped stays as the Kite form."""
    tok = str(token or "")
    if tok.startswith("KITE|") and to_upstox is not None:
        try:
            _k, ex, ts = tok.split("|", 2)
            u = to_upstox(ex, ts)
            if u:
                return u
        except Exception:
            pass
    return tok or str(sym or "").upper()


def record_exit(token, sym, und, broker, why, now=None, to_upstox=None):
    """risk_manager calls this when it SENDS a TREND_EXIT/STOP_BREACH_EXIT (not on --dry-run)."""
    if _dry():
        return
    try:
        now = _now(now)
        day = now.date().isoformat()
        st = _read(EXITS)
        days = {k: v for k, v in (st.get("days") or {}).items() if k >= (now.date() - dt.timedelta(days=5)).isoformat()}
        rows = days.setdefault(day, [])
        rows.append({"contract": contract_id(token, sym, to_upstox), "sym": sym, "und": und, "broker": broker,
                     "why": why, "at": now.isoformat()})
        _write(EXITS, {"days": days})
    except Exception as e:
        print("perf_rules: exit record failed: %s" % type(e).__name__, file=sys.stderr)


def reentry_check(contract, und, now, orders, underlying, sym_of, to_upstox=None, exits_path=None):
    """(ok, why). orders: today's Upstox + Kite orders (normalized). Stop exits are found in the
    order books and in reentry_blocks.json. Same contract -> blocked rest of day; same underlying
    -> blocked for REENTRY_COOLDOWN_MIN after the exit. An exit whose time is unknown blocks the
    contract only."""
    now = _now(now)
    day = now.date()
    events = []
    for o in orders or []:
        if not isinstance(o, dict) or not is_stop_exit(o):
            continue
        t = _ts_of(o)
        if t is not None and t.date() != day:
            continue
        s = sym_of(o)
        events.append((contract_id(o.get("instrument_token"), s, to_upstox), underlying(s), t, s,
                       "SL" if str(o.get("order_type") or "").upper() in ("SL", "SL-M") else str(o.get("tag"))))
    for r in (_read(exits_path or EXITS).get("days") or {}).get(day.isoformat()) or []:
        try:
            t = dt.datetime.fromisoformat(r["at"])
        except Exception:
            t = None
        events.append((r.get("contract"), r.get("und"), t, r.get("sym"), r.get("why")))
    for c, u, t, s, why in events:
        if c and c == contract:
            return False, "R12: %s stop exit (%s)%s today - no re-entry on the same contract today" % (
                s, why, (" at %s" % t.strftime("%H:%M")) if t else "")
    for c, u, t, s, why in events:
        if u == und and t is not None and 0 <= (now - t).total_seconds() / 60 < REENTRY_COOLDOWN_MIN:
            return False, "R12: %s stop exit (%s) at %s - %s cooldown %d min" % (s, why, t.strftime("%H:%M"), und, REENTRY_COOLDOWN_MIN)
    return True, "no stop exit today on this contract or underlying"


def trading_days_to_expiry(expiry, today, next_trading_day):
    """0 = expires today, 1 = next trading day, ... (None if expiry before today)."""
    if expiry < today:
        return None
    n, d = 0, today
    while d < expiry and n < 60:
        d = next_trading_day(d)
        n += 1
    return n


def expiry_too_near(expiry, today, next_trading_day):
    n = trading_days_to_expiry(expiry, today, next_trading_day)
    return n is None or n <= 1


# ---------------- 5) size ----------------
def allowed_index_lots(expiry_day, adds_taken=False):
    """Largest lot count a bot index-option position may hold."""
    if adds_taken:
        return INDEX_MAX_TOTAL_LOTS
    return INDEX_EXPIRY_DAY_LOTS if expiry_day else INDEX_TREND_LOTS


def oversize(kind, lots, expiry_day=False, buys_today=1):
    """-> (allowed_lots, text) when the position is above the rule, else None.
    kind 'index' or 'stock'. buys_today >= 2 means an add may have been taken (cap 3 total)."""
    if kind == "stock":
        allowed, rule = STOCK_OPT_LOTS, "stock options 1 lot per broker"
    else:
        add = buys_today >= 2
        allowed = allowed_index_lots(expiry_day, add)
        rule = ("index max 3 lots incl. the one add" if add else
                ("index expiry day max 3 lots" if expiry_day else "index 1 lot normal / 2 strong trend"))
    if lots > allowed + 1e-9:
        return allowed, rule
    return None


def oversize_alerts(items, now=None, state_path=None, persist=True):
    """items: dicts broker, sym, qty, lots, allowed, rule. Returns the OVERSIZE lines to relay:
    once per (broker, symbol) per IST day, again only if the lot count grew since the last line."""
    now = _now(now)
    day = now.date().isoformat()
    path = state_path or OVERSIZE_STATE
    st = _read(path)
    seen = dict((st.get("days") or {}).get(day) or {})
    lines = []
    for it in items:
        k = "%s|%s" % (it["broker"], it["sym"])
        if k in seen and float(seen[k]) >= float(it["lots"]):
            continue
        seen[k] = it["lots"]
        lines.append("OVERSIZE: %s %s qty %d = %g lots, allowed %d (%s)" % (
            it["broker"].capitalize(), it["sym"], it["qty"], it["lots"], it["allowed"], it["rule"]))
    if lines and persist and not _dry():
        days = {k: v for k, v in (st.get("days") or {}).items() if k >= (now.date() - dt.timedelta(days=5)).isoformat()}
        days[day] = seen
        try:
            _write(path, {"days": days})
        except Exception as e:
            print("perf_rules: oversize state not saved: %s" % type(e).__name__, file=sys.stderr)
    return lines


# ---------------- 3) initial stop ----------------
def stop_pct(s, fut=False):
    """Stop distance under entry for one risk_state position dict."""
    if fut:
        return 0.015
    try:
        v = float(s.get("stop_pct"))
        if 0 < v < 1:
            return v
    except (TypeError, ValueError):
        pass
    return INITIAL_STOP_PCT


def migrate_stop_pct(st, pending_sl_keys=(), now=None):
    """One-time: positions already open when the 20% rule shipped keep 30% (live_qty>0 in the
    saved state, or a SELL stop already working for them). Returns the keys marked legacy."""
    if st.get(STOP_MIGRATION_KEY):
        return []
    marked = []
    pend = set(pending_sl_keys or ())
    for k, v in st.items():
        if k.startswith("_") or not isinstance(v, dict) or "stop_pct" in v:
            continue
        try:
            held = int(v.get("live_qty") or 0) > 0
        except (TypeError, ValueError):
            held = False
        if held or k in pend:
            v["stop_pct"] = LEGACY_STOP_PCT
            marked.append(k)
    st[STOP_MIGRATION_KEY] = _now(now).isoformat()
    return marked
