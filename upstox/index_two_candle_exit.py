"""Route-timeframe futures exits for index-option routes (t685u two-candle, t686u EMA50).

Close a NIFTY / BANKNIFTY / SENSEX option when TWO CONSECUTIVE completed candles
on that position's own route timeframe (3m / 15m / 1h), built from the underlying
index FUTURES (3m native, 15m and 1h resampled from 5m, same as entry), each close
against the previous candle's close:

  CE: close < previous close
  PE: close > previous close

The count starts from candles completed after the entry. A candle that closes
at or before the entry is ignored. A pre-entry candle is only used as the
comparison close for the first post-entry bar. Equal closes are not adverse.

The entry time is the current life's opening fill when today's order tape is
available (a re-entry or new opening fill resets it). The route record's
"opened" field is only a fallback, and is rewritten to the fill time so a
stale time from a previous life of the same contract cannot fire the exit.

Also (t686u): exit when a completed route-timeframe futures candle after entry
closes against EMA50 (CE: close < EMA50; PE: close > EMA50). EMA50 uses the same
ind.ema as the rest of the desk, warmed up on 30 days of 15m/1h bars and 10 days
of 3m bars (well over 50 periods). A series with fewer than 50 completed bars
does not arm the EMA50 exit. Whichever of the two-candle exit and the EMA50
break completes first closes the trade (a tie on the same candle is tagged
TWO_CANDLE_EXIT and the why notes both).

This is a pure decision helper plus a read-only futures fetch. It places no orders.
risk_manager.py calls evaluate() on each 5-minute run and, when the rule fires,
sends the same full-quantity LIMIT SELL used for TREND_EXIT. Because that routine
polls every 5 minutes, a 3m signal is checked on the next run against all
completed post-entry 3m futures candles, so a mid-interval signal is still
honoured even if a later 3m bar recovered. The sell is priced at the LTP of that
run, so a 3m exit can lag the completing candle by up to ~5 minutes.

Live go-live: candles that had already closed before RULE_EFFECTIVE (17 Nov 2026
09:15 IST, the next session) are not used to flatten positions that were open
when these exits were added. The backtest calls first_route_exit() with the
entry time so 12/13/16 Nov are scored as if the rules had been in force.
"""
import datetime as dt
import json
import os
import sys

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
import index_routes as iroutes

IST = iroutes.IST
# First session after this rule was written (market already closed on 16 Nov).
RULE_EFFECTIVE = dt.datetime(2026, 11, 17, 9, 15, tzinfo=IST)
LOG = os.environ.get("INDEX_TWO_CANDLE_LOG") or os.path.join(D, "index_two_candle_decisions.jsonl")
ACTION = "TWO_CANDLE_EXIT"
ACTION_EMA50 = "EMA50_EXIT"
EMA50_PERIOD = 50
# Warm-up: 30 calendar days of 15m/1h (and of the 5m source they are resampled from);
# 10 calendar days of native 3m (~8 sessions, well over 50 bars).
EMA50_HISTORY_DAYS = {3: 10, 15: 30, 60: 30}
MIN_EMA50_BARS = 50


def parse_ts(ts):
    if ts is None:
        return None
    if isinstance(ts, dt.datetime):
        d = ts
    else:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=IST)
    return d.astimezone(IST)


def bar_ts(bar):
    if isinstance(bar, dict):
        return parse_ts(bar.get("ts") or bar.get("time") or bar.get("timestamp") or bar.get(0))
    return parse_ts(bar[0])


def bar_close(bar):
    if isinstance(bar, dict):
        return float(bar["close"] if "close" in bar else bar.get("c"))
    return float(bar[4] if len(bar) > 4 else bar[1])


def completed_at(bar, minutes):
    return bar_ts(bar) + dt.timedelta(minutes=int(minutes))


def bar_completed_after(bar, minutes, start):
    """True when this candle's close is strictly after start (entry / floor).

    A candle that closes at or before entry is ignored by both the two-candle
    and EMA50 exits.
    """
    if start is None:
        return False
    try:
        return completed_at(bar, minutes) > start
    except Exception:
        return False


def adverse_close(side, close, prev_close):
    """True when this candle's close is against a long CE or PE."""
    if close is None or prev_close is None:
        return False
    side = (side or "").upper()
    if side == "CE":
        return close < prev_close
    if side == "PE":
        return close > prev_close
    return False


def consecutive_adverse(bars, side, entry_ts, minutes, count_from=None):
    """Scan completed bars for two consecutive post-entry adverse closes.

    bars: chronological iterable of {ts, close} or raw [ts, o, h, l, c, ...]
          (open-time stamps; a bar is complete at ts + minutes).
    count_from: optional extra floor (live RULE_EFFECTIVE). The count still
          starts at the later of entry and count_from.

    Returns (fire: bool, detail: dict).
    """
    minutes = int(minutes)
    entry = parse_ts(entry_ts)
    floor = parse_ts(count_from) if count_from is not None else None
    start = entry
    if start is not None and floor is not None:
        start = max(start, floor)
    elif start is None:
        start = floor
    rows = list(bars or [])
    det = {"fire": False, "side": (side or "").upper(), "minutes": minutes,
           "entry_ts": entry.isoformat() if entry else None,
           "count_from": start.isoformat() if start else None, "why": None, "pair": None}
    if start is None:
        det["why"] = "no entry time; two-candle exit not evaluated"
        return False, det
    if det["side"] not in ("CE", "PE"):
        det["why"] = "side is not CE or PE"
        return False, det
    if len(rows) < 2:
        det["why"] = "need at least two completed futures candles"
        return False, det

    post = []  # indices of bars that completed after start
    for i, b in enumerate(rows):
        try:
            _ = bar_close(b)
        except Exception:
            continue
        if bar_completed_after(b, minutes, start):
            post.append(i)

    if len(post) < 2:
        det["why"] = "fewer than two futures candles completed after entry"
        det["post_entry"] = len(post)
        return False, det

    for a, b in zip(post, post[1:]):
        if b != a + 1:
            continue
        if a < 1:
            continue
        try:
            c_prev, c_a, c_b = bar_close(rows[a - 1]), bar_close(rows[a]), bar_close(rows[b])
            t_a, t_b = bar_ts(rows[a]), bar_ts(rows[b])
        except Exception:
            continue
        if adverse_close(side, c_a, c_prev) and adverse_close(side, c_b, c_a):
            det["fire"] = True
            det["why"] = (
                "%s two consecutive %sm futures closes against previous close "
                "(%s close %s %s prev %s; %s close %s %s prev %s)"
                % (det["side"], minutes,
                   t_a.strftime("%H:%M") if t_a else "?", c_a, "<" if det["side"] == "CE" else ">", c_prev,
                   t_b.strftime("%H:%M") if t_b else "?", c_b, "<" if det["side"] == "CE" else ">", c_a)
            )
            det["pair"] = {
                "first": {"ts": t_a.isoformat() if t_a else None, "close": c_a, "prev_close": c_prev,
                          "completed_at": completed_at(rows[a], minutes).isoformat()},
                "second": {"ts": t_b.isoformat() if t_b else None, "close": c_b, "prev_close": c_a,
                           "completed_at": completed_at(rows[b], minutes).isoformat()},
            }
            return True, det

    det["why"] = "no two consecutive post-entry %sm futures closes against the previous close" % minutes
    det["post_entry"] = len(post)
    return False, det



def adverse_ema50(side, close, ema):
    """True when this candle's close has broken EMA50 against a long CE or PE."""
    if close is None or ema is None:
        return False
    side = (side or "").upper()
    if side == "CE":
        return close < ema
    if side == "PE":
        return close > ema
    return False


def ema50_series(bars):
    """EMA50 for each bar, or None when there are fewer than MIN_EMA50_BARS closes."""
    import ind
    closes = []
    for b in bars or []:
        try:
            closes.append(bar_close(b))
        except Exception:
            return None
    if len(closes) < MIN_EMA50_BARS:
        return None
    return ind.ema(closes, EMA50_PERIOD)


def ema50_break(bars, side, entry_ts, minutes, count_from=None):
    """First post-entry completed bar whose close breaks EMA50.

    EMA50 is computed on the full series (pre-entry bars are warm-up only).
    Returns (fire: bool, detail: dict).
    """
    minutes = int(minutes)
    entry = parse_ts(entry_ts)
    floor = parse_ts(count_from) if count_from is not None else None
    start = entry
    if start is not None and floor is not None:
        start = max(start, floor)
    elif start is None:
        start = floor
    rows = list(bars or [])
    det = {"fire": False, "side": (side or "").upper(), "minutes": minutes, "action": None,
           "entry_ts": entry.isoformat() if entry else None,
           "count_from": start.isoformat() if start else None, "why": None, "bar": None}
    if start is None:
        det["why"] = "no entry time; EMA50 exit not evaluated"
        return False, det
    if det["side"] not in ("CE", "PE"):
        det["why"] = "side is not CE or PE"
        return False, det
    emas = ema50_series(rows)
    if emas is None:
        det["why"] = "EMA50 not armed (need %d completed futures candles for warm-up, have %d)" % (
            MIN_EMA50_BARS, len(rows))
        return False, det
    post = 0
    for i, b in enumerate(rows):
        try:
            close = bar_close(b)
            ema = emas[i]
            t = bar_ts(b)
            done = completed_at(b, minutes)
        except Exception:
            continue
        if not bar_completed_after(b, minutes, start):
            continue
        post += 1
        if adverse_ema50(side, close, ema):
            det["fire"] = True
            det["action"] = ACTION_EMA50
            op = "<" if det["side"] == "CE" else ">"
            det["why"] = (
                "%s %sm futures close %s %s EMA50 %s at %s (completed %s)"
                % (det["side"], minutes, close, op, round(ema, 2),
                   t.strftime("%H:%M") if t else "?",
                   done.strftime("%H:%M") if done else "?")
            )
            det["bar"] = {"ts": t.isoformat() if t else None, "close": close,
                          "ema50": round(ema, 4),
                          "completed_at": done.isoformat()}
            return True, det
    det["why"] = "no post-entry %sm futures close against EMA50" % minutes
    det["post_entry"] = post
    return False, det


def first_route_exit(bars, side, entry_ts, minutes, count_from=None):
    """Whichever of the two-candle exit and the EMA50 break completes first.

    On an exact tie (same completing candle), the action is TWO_CANDLE_EXIT and
    the why notes that EMA50 also broke. Returns (fire, detail) with detail['action'].
    """
    c2, d2 = consecutive_adverse(bars, side, entry_ts, minutes, count_from=count_from)
    e5, d5 = ema50_break(bars, side, entry_ts, minutes, count_from=count_from)
    t2 = parse_ts(((d2.get("pair") or {}).get("second") or {}).get("completed_at")) if c2 else None
    t5 = parse_ts((d5.get("bar") or {}).get("completed_at")) if e5 else None
    if c2 and e5:
        if t5 is not None and t2 is not None and t5 < t2:
            d5["two_candle"] = d2
            return True, d5
        d2["action"] = ACTION
        if t2 is not None and t5 is not None and t2 == t5:
            d2["why"] = d2.get("why") + "; also " + (d5.get("why") or "EMA50 broken on the same candle")
            d2["ema50"] = d5
        else:
            d2["ema50"] = d5
        return True, d2
    if e5:
        return True, d5
    if c2:
        d2["action"] = ACTION
        return True, d2
    why = "; ".join(x for x in (d2.get("why"), d5.get("why")) if x)
    return False, {"fire": False, "action": None, "why": why, "two_candle": d2, "ema50": d5}


def side_of(sym):
    s = (sym or "").upper()
    if s.endswith("PE"):
        return "PE"
    if s.endswith("CE"):
        return "CE"
    return None


def route_key(broker, instrument_key=None, exchange=None, sym=None):
    b = (broker or "upstox").lower()
    if b == "kite":
        return iroutes.kkey(exchange, sym)
    if not instrument_key:
        return None
    return iroutes.ukey(instrument_key)


def _monthlies(underlying, rows=None):
    """Futures expiry dates for infer_route. Empty set -> delivery defaults to 15m."""
    u = (underlying or "").upper()
    out = set()
    for r in rows or []:
        if r.get("instrument_type") != "FUT":
            continue
        if (r.get("underlying_symbol") or r.get("name") or "") != u:
            continue
        try:
            out.add(dt.datetime.fromtimestamp(r["expiry"] / 1000, IST).date())
        except Exception:
            continue
    return out


def _instrument_rows():
    rows = []
    try:
        import full_auto_feed as faf
        for exch in ("NSE", "BSE"):
            rows.extend(faf._rows(exch) or [])
    except Exception:
        pass
    return rows


def _expiry_of(instrument_key, rows=None):
    if not instrument_key:
        return None
    for r in rows or []:
        if r.get("instrument_key") == instrument_key and r.get("expiry"):
            try:
                return dt.datetime.fromtimestamp(r["expiry"] / 1000, IST).date()
            except Exception:
                return None
    return None


def prepare(now=None, state=None, master_rows=None, orders=None, state_path=None):
    """Load route state and instrument masters once per risk_manager run.

    orders: today's completed (and working) orders for this broker, used to
    resolve the current life's opening fill. Not fetched here; the caller
    passes the book it already loaded. Tests stay offline by omitting it.
    """
    return {
        "now": now or dt.datetime.now(IST),
        "state": iroutes.load() if state is None else (state or {}),
        "rows": master_rows if master_rows is not None else _instrument_rows(),
        "orders": orders,
        "state_path": state_path,
    }


def fetch_completed_bars(index, minutes, now, fut_key=None):
    """Completed route-timeframe futures candles. 3m native; 15m/1h from 5m resample.

    Returns (bars, fut_key, error). bars is a list of raw Upstox candles, or [].
    """
    import ind
    import index_entry_logic as iel
    import index_contract_policy as icp
    try:
        import full_auto_feed as faf
    except Exception as e:
        return [], None, "full_auto_feed unavailable (%s)" % type(e).__name__
    minutes = int(minutes)
    key = fut_key or faf.fut_key(index, today=(now or dt.datetime.now(IST)).date())
    if not key:
        return [], None, "no futures key for %s" % index
    days = EMA50_HISTORY_DAYS.get(minutes, 30)
    try:
        if minutes == 3:
            raw = ind.candles(key, "minutes", 3, days)
            closed, _ = iel.drop_forming(raw, now, "minutes", 3)
            if not closed:
                return [], key, "no completed 3m futures candles"
            return closed, key, None
        raw = ind.candles(key, "minutes", 5, days)
        res = icp.resample(raw, minutes, now)
        if not res.get("ok"):
            return [], key, "resample failed: %s" % res.get("reason")
        return res["candles"] or [], key, None
    except Exception as e:
        return [], key, "futures candles unreadable (%s)" % type(e).__name__


def resolve_entry(rec, orders, sym, token=None):
    """Prefer the current life's opening fill over a stale route 'opened' time.

    Returns (entry_dt, source, fill_ts_str, fill_oid). source is 'opening_fill'
    or 'route_record', or (None, None, None, None) when neither is available.
    """
    fill_ts = fill_oid = None
    try:
        import position_life as pl
        fill_ts, fill_oid, _flats = pl.current_open_fill(orders, sym=sym, token=token)
    except Exception:
        fill_ts, fill_oid = None, None
    fill = parse_ts(fill_ts) if fill_ts else None
    opened = parse_ts((rec or {}).get("opened"))
    if fill is not None:
        return fill, "opening_fill", fill_ts, fill_oid
    if opened is not None:
        return opened, "route_record", None, None
    return None, None, None, None


def sync_route_opened(ctx, key, entry, source, fill_oid=None, persist=False):
    """Write the opening-fill time back onto a tracked route record."""
    if source != "opening_fill" or not key or entry is None:
        return False
    state = (ctx or {}).get("state")
    if not isinstance(state, dict):
        return False
    changed = iroutes.refresh_opened(state, key, entry, order_id=fill_oid)
    if changed and persist:
        try:
            iroutes.save(state, path=(ctx or {}).get("state_path"))
        except Exception:
            pass
    return changed


def classify(ctx, broker, instrument_key, exchange, sym, product):
    """(route, how, entry_ts, entry_source, key, index) or (None, why, ...)."""
    und = None
    try:
        import trade_lock
        und = trade_lock.underlying(sym)
    except Exception:
        # fallback: leading letters of the option symbol
        s = (sym or "").upper().replace(" ", "")
        for name in iroutes.INDEXES:
            if s.startswith(name):
                und = name
                break
    if und not in iroutes.INDEXES:
        return None, "not a NIFTY/BANKNIFTY/SENSEX option", None, None, None, und
    key = route_key(broker, instrument_key=instrument_key, exchange=exchange, sym=sym)
    state = (ctx or {}).get("state") or {}
    rows = (ctx or {}).get("rows") or []
    uk = instrument_key
    if not uk and broker == "kite":
        try:
            import broker_kite as bk
            uk = bk.to_upstox(exchange, sym)
        except Exception:
            uk = None
    expiry = _expiry_of(uk, rows)
    route, how = iroutes.route_of(state, key, product, expiry, _monthlies(und, rows))
    rec = ((state.get("positions") or {}).get(key) or {}) if key else {}
    orders = (ctx or {}).get("orders")
    token = instrument_key or uk
    entry, src, _fill_ts, _fill_oid = resolve_entry(rec, orders, sym, token=token)
    if entry is not None:
        return route, how, entry, src, key, und
    # Unknown entry (inferred / pre-t673u, no fill tape): do not scan days of
    # history and immediately flatten. Start from this session's open, then
    # RULE_EFFECTIVE still floors it in evaluate().
    now = (ctx or {}).get("now") or dt.datetime.now(IST)
    session = dt.datetime.combine(now.date(), dt.time(9, 15), IST)
    return route, how, session, "session_open_fallback", key, und


def _log(row):
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def evaluate(sym, product=None, broker="upstox", instrument_key=None, exchange=None,
             state=None, now=None, bars=None, apply_effective=True, log=True,
             orders=None, sync_route=False):
    """(fire, why, action). Fetches futures candles unless `bars` is supplied.

    action is TWO_CANDLE_EXIT, EMA50_EXIT, or None. apply_effective: live default
    True floors the count at RULE_EFFECTIVE so already-closed candles cannot
    flatten a carry-forward the first morning.

    orders: today's order tape for this broker; when present, the current
    life's opening fill is the entry time (a re-entry resets a stale route
    'opened'). sync_route=True (live risk_manager) writes that fill time back
    onto the tracked route record. Tests leave it False so they do not touch
    the live state file.
    """
    now = now or dt.datetime.now(IST)
    ctx = state if isinstance(state, dict) and "state" in (state or {}) else prepare(now=now, state=state)
    if orders is not None:
        ctx = dict(ctx)
        ctx["orders"] = orders
    route, how, entry, src, key, und = classify(ctx, broker, instrument_key, exchange, sym, product)
    if sync_route and src == "opening_fill":
        _oid = None
        try:
            import position_life as pl
            _ts, _oid, _fl = pl.current_open_fill(ctx.get("orders"), sym=sym,
                                                  token=instrument_key)
        except Exception:
            _oid = None
        sync_route_opened(ctx, key, entry, src, fill_oid=_oid, persist=True)
    if route is None:
        return False, how, None
    minutes = iroutes.SPEC[route]["minutes"]
    side = side_of(sym)
    floor = RULE_EFFECTIVE if apply_effective else None
    err = None
    fut_key = None
    if bars is None:
        bars, fut_key, err = fetch_completed_bars(und, minutes, now)
        if err and not bars:
            why = "route-timeframe exit not evaluated: %s" % err
            if log:
                _log({"ts": now.isoformat(), "sym": sym, "index": und, "route": route, "how": how,
                      "broker": broker, "fire": False, "why": why, "entry_source": src, "action": None})
            return False, why, None
    fire, det = first_route_exit(bars, side, entry, minutes, count_from=floor)
    det.update({"route": route, "how": how, "index": und, "symbol": sym, "broker": broker,
                "entry_source": src, "fut_key": fut_key, "route_key": key})
    action = det.get("action")
    if log:
        _log({"ts": now.isoformat(), "sym": sym, "index": und, "route": route, "how": how,
              "broker": broker, "fire": fire, "why": det.get("why"), "entry_source": src,
              "action": action, "pair": det.get("pair"), "bar": det.get("bar"), "minutes": minutes})
    return fire, det.get("why"), action
