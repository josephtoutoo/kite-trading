"""Stock-option 1h entries and next-month debit spreads (t687u, 16 Nov 2026).

Signal timeframe for every stock-option entry path is the completed 1-hour bar
(SIGNAL_UNIT/INTERVAL/DAYS). This module does not place orders by itself.

When the current monthly stock-option expiry falls in the current ISO week
(the contract's expiry week), a first entry is a next-month DEBIT SPREAD
instead of the plain near-month option:

  BUY  the next-month option chosen by the existing ATM (nearest-strike) rule
  SELL a further OTM next-month option of the same type, same quantity, same
       broker, default HEDGE_STEPS=2 listed strikes further OTM

Order safety: the BUY is placed first and the SELL is sent only after that buy
fill is confirmed. A failed SELL is alerted and logged; a SELL is never sent
without a confirmed long. Exits close both legs, buying back the short first
(safer for margin), then selling the long.

Overnight protection (t691u): a GTT SELL on the LONG only, trigger BELOW the
stop, plus a companion GTT BUY on the SHORT with trigger BELOW at a price
scaled to the same move (Upstox trigger_type=BELOW + transaction_type=BUY;
Kite single GTT BUY with the trigger below LTP). A SELL GTT is never placed
on the short. A 9:15 IST (and every day-session) reconcile buys back any
lone leftover short immediately. Day exits, Friday flatten, and expiry or
results-day exits close both legs, short first. SPREAD_SEND_ALLOWED is True
only while those paths are in place.

Places nothing on import.
"""
import datetime as dt
import json
import os

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Stock-option entry signal: completed 1-hour cash bar, 60 calendar days of history.
SIGNAL_UNIT = "hours"
SIGNAL_INTERVAL = 1
SIGNAL_DAYS = 60
SIGNAL_TIMEFRAME = "1h"

HEDGE_STEPS = 2  # listed strikes further OTM, not a rupee multiple
SPREAD_SEND_ALLOWED = True
SPREAD_SEND_BLOCK = (
    "live debit-spread send is blocked: pair day-exit, overnight GTT (long SELL BELOW + "
    "short BUY BELOW) plus 9:15/intraday naked-short reconcile, Friday flatten, or "
    "SELL retry is missing or untested."
)
LEDGER = os.environ.get("STOCKOPT_SPREAD_LEDGER") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "stockopt_spreads.json"
)


def parse_day(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def iso_week(day):
    d = parse_day(day)
    return None if d is None else d.isocalendar()[:2]


def in_expiry_week(exp, today):
    """True only when today is in the Monday-start ISO week that contains exp, and exp >= today."""
    exp, today = parse_day(exp), parse_day(today)
    if not exp or not today or exp < today:
        return False
    return iso_week(exp) == iso_week(today)


def _row_expiry(row):
    if not isinstance(row, dict):
        return None
    if row.get("expiry_date") is not None:
        return parse_day(row["expiry_date"])
    raw = row.get("expiry")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)) and raw > 10**11:
            return dt.datetime.fromtimestamp(raw / 1000, IST).date()
    except Exception:
        return None
    return parse_day(raw)


def monthly_expiries(chain, fut_dates=None):
    """Listed monthly dates: futures expiries if given, else unique chain dates.

    NSE stock options in the current master are monthlies (weekly=False) and
    share the futures expiry. A row tagged weekly=True is not treated as monthly.
    """
    futs = sorted({parse_day(x) for x in (fut_dates or []) if parse_day(x)})
    if futs:
        return futs
    dates = set()
    seen_weekly = False
    for row in chain or []:
        exp = _row_expiry(row)
        if not exp:
            continue
        if row.get("weekly") is True:
            seen_weekly = True
            continue
        dates.add(exp)
    if dates:
        return sorted(dates)
    if seen_weekly:
        return []
    return sorted({_row_expiry(r) for r in (chain or []) if _row_expiry(r)})


def current_monthly(monthlies, today):
    today = parse_day(today)
    later = [d for d in (monthlies or []) if d and today and d >= today]
    return later[0] if later else None


def next_monthly(monthlies, today, current=None):
    cur = current or current_monthly(monthlies, today)
    later = [d for d in (monthlies or []) if d and cur and d > cur]
    return later[0] if later else None


def in_monthly_expiry_week(today, monthlies=None, current=None):
    cur = current or current_monthly(monthlies or [], today)
    return bool(cur and in_expiry_week(cur, today))


def strikes_on(chain, expiry, side):
    day, side = parse_day(expiry), (side or "").upper()
    out = []
    for row in chain or []:
        if (row.get("instrument_type") or "").upper() != side:
            continue
        if _row_expiry(row) != day:
            continue
        try:
            k = float(row.get("strike_price") or row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if k > 0:
            out.append(k)
    return sorted(set(out))


def strike_steps(strikes):
    """Adjacent listed-strike differences and the modal (most common) step."""
    s = sorted(set(float(x) for x in (strikes or []) if x is not None))
    diffs = [round(s[i + 1] - s[i], 6) for i in range(len(s) - 1) if s[i + 1] > s[i]]
    if not diffs:
        return {"steps": [], "modal": None, "count": len(s)}
    counts = {}
    for d in diffs:
        counts[d] = counts.get(d, 0) + 1
    modal = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    return {"steps": sorted(set(diffs)), "modal": modal, "counts": counts, "count": len(s)}


def pick_atm(chain, side, close, expiry):
    """Nearest strike on that expiry (existing ATM rule), or None."""
    day, side = parse_day(expiry), (side or "").upper()
    if not chain or close is None or not day or side not in ("CE", "PE"):
        return None
    same = [r for r in chain if (r.get("instrument_type") or "").upper() == side and _row_expiry(r) == day]
    if not same:
        return None
    try:
        close = float(close)
    except (TypeError, ValueError):
        return None
    return min(same, key=lambda r: (abs(float(r.get("strike_price") or r.get("strike") or 0) - close),
                                    float(r.get("strike_price") or r.get("strike") or 0)))


def pick_otm_hedge(chain, side, bought_strike, expiry, steps=HEDGE_STEPS):
    """`steps` listed strikes further OTM than the bought strike, same expiry and type."""
    side = (side or "").upper()
    try:
        bought = float(bought_strike)
        steps = int(steps)
    except (TypeError, ValueError):
        return None, "hedge strike or step unverified"
    if steps < 1:
        return None, "hedge step must be at least 1"
    listed = strikes_on(chain, expiry, side)
    if side == "CE":
        further = [k for k in listed if k > bought]
    elif side == "PE":
        further = list(reversed([k for k in listed if k < bought]))
    else:
        return None, "side is not CE or PE"
    if len(further) < steps:
        return None, "need %d listed strikes further OTM than %s; have %d" % (steps, bought, len(further))
    target = further[steps - 1]
    row = next((r for r in (chain or [])
                if (r.get("instrument_type") or "").upper() == side
                and _row_expiry(r) == parse_day(expiry)
                and abs(float(r.get("strike_price") or r.get("strike") or 0) - target) < 1e-9), None)
    if row is None:
        return None, "hedge contract missing from the chain at strike %s" % target
    return row, None


def plan_entry(chain, side, close, today, fut_dates=None, steps=HEDGE_STEPS):
    """Plan a plain ATM entry or a next-month debit spread.

    Returns a dict with mode 'plain', 'spread', or 'refuse' (no entry).
    send_allowed follows SPREAD_SEND_ALLOWED (True only when pair exits and overnight cover exist).
    """
    today = parse_day(today)
    side = (side or "").upper()
    monthlies = monthly_expiries(chain, fut_dates)
    current = current_monthly(monthlies, today)
    nxt = next_monthly(monthlies, today, current)
    expiry_week = bool(current and in_expiry_week(current, today))
    steps_now = strike_steps(strikes_on(chain, current, side)) if current else {"modal": None, "steps": []}
    base = {
        "ok": False, "mode": "refuse", "side": side, "today": today.isoformat() if today else None,
        "current_monthly": current.isoformat() if current else None,
        "next_monthly": nxt.isoformat() if nxt else None,
        "expiry_week": expiry_week, "hedge_steps": int(steps),
        "strike_step": None, "long": None, "short": None,
        "send_allowed": False, "reason": None,
    }
    if side not in ("CE", "PE"):
        base["reason"] = "side is not CE or PE"
        return base
    if not current:
        base["reason"] = "no current monthly stock-option expiry is listed"
        return base
    if not expiry_week:
        long = pick_atm(chain, side, close, current)
        if long is None:
            base["reason"] = "no ATM contract on the current monthly %s" % current
            return base
        st = strike_steps(strikes_on(chain, current, side))
        base.update(ok=True, mode="plain", long=long, send_allowed=True,
                    strike_step=st.get("modal"), strike_steps=st,
                    reason="current monthly %s is not in this week; plain ATM entry" % current)
        return base
    # Expiry week: next-month debit spread, never a same-week plain option.
    if not nxt:
        base["reason"] = (
            "current monthly %s is in expiry week and the next monthly is not listed; "
            "plain same-week entry is refused" % current)
        return base
    long = pick_atm(chain, side, close, nxt)
    if long is None:
        base["reason"] = "no ATM contract on the next monthly %s" % nxt
        return base
    bought = float(long.get("strike_price") or long.get("strike") or 0)
    short, why = pick_otm_hedge(chain, side, bought, nxt, steps=steps)
    if short is None:
        base["reason"] = "next-month debit spread not available: " + why
        return base
    hedge_k = float(short.get("strike_price") or short.get("strike") or 0)
    st = strike_steps(strikes_on(chain, nxt, side))
    base.update(ok=True, mode="spread", long=long, short=short,
                strike_step=st.get("modal"), strike_steps=st,
                long_strike=bought, short_strike=hedge_k,
                hedge_distance=round(abs(hedge_k - bought), 6),
                send_allowed=bool(SPREAD_SEND_ALLOWED),
                send_block=None if SPREAD_SEND_ALLOWED else SPREAD_SEND_BLOCK,
                reason=("next-month debit spread: buy %s %s, sell %s %s (%d strikes OTM, modal step %s)"
                        % (nxt, bought, nxt, hedge_k, int(steps), st.get("modal"))))
    return base


def execute_debit_spread(place, wait_fill, buy, sell, cancel=None, alert=None, sell_retries=1):
    """Place BUY, wait for a confirmed fill, then place SELL. Never sells first.

    place(order) -> order_id or None
    wait_fill(order_id, order) -> dict with filled (bool) and optional qty/avg
    cancel(order_id) optional
    alert(message, detail) optional; called when the SELL fails after a buy fill
    and the retry is exhausted. The long stays open and must be protected by
    its normal stop.

    sell_retries: extra SELL attempts after the first rejection (default 1).
    Returns a result dict; sell_placed is True only after a confirmed buy fill.
    """
    out = {"ok": False, "buy_id": None, "sell_id": None, "buy_filled": False,
           "sell_placed": False, "sell_filled": False, "alert": None, "reason": None}
    if place is None:
        out["reason"] = "sender unverified"
        return out
    try:
        buy_id = place(buy)
    except Exception as e:
        out["reason"] = "BUY send failed: %s" % type(e).__name__
        return out
    if not buy_id:
        out["reason"] = "BUY rejected; SELL was not sent"
        return out
    out["buy_id"] = buy_id
    fill = {"filled": False}
    if wait_fill is not None:
        try:
            fill = wait_fill(buy_id, buy) or fill
        except Exception as e:
            fill = {"filled": False, "error": type(e).__name__}
    if not fill.get("filled"):
        if cancel:
            try:
                cancel(buy_id, buy)
            except Exception:
                pass
        out["reason"] = "BUY did not fill; SELL was not sent"
        return out
    out["buy_filled"] = True
    out["buy_fill"] = fill
    try:
        retries = max(0, int(sell_retries))
    except (TypeError, ValueError):
        retries = 1
    attempts = 1 + retries
    sell_id = None
    last_err = None
    for attempt in range(1, attempts + 1):
        out["sell_attempts"] = attempt
        try:
            sell_id = place(sell)
            last_err = None
        except Exception as e:
            sell_id = None
            last_err = type(e).__name__
        if sell_id:
            break
        last_err = last_err or "rejected"
    if not sell_id:
        out["reason"] = ("BUY filled but SELL was rejected after %d attempt(s)%s; "
                         "long is unhedged and must stay on its normal stop"
                         % (attempts, (": " + last_err) if last_err else ""))
        out["alert"] = out["reason"]
        if alert:
            try:
                alert(out["reason"], {"buy_id": buy_id, "buy": buy, "sell": sell,
                                      "sell_attempts": attempts})
            except Exception:
                pass
        return out
    out["sell_id"] = sell_id
    out["sell_placed"] = True
    if wait_fill is not None:
        try:
            sf = wait_fill(sell_id, sell) or {}
            out["sell_filled"] = bool(sf.get("filled"))
            out["sell_fill"] = sf
        except Exception as e:
            out["sell_filled"] = False
            out["reason"] = "SELL status unverified: %s" % type(e).__name__
    if out.get("sell_filled"):
        out["ok"] = True
        out["reason"] = "debit spread opened (buy filled, sell filled)"
    else:
        out["reason"] = out.get("reason") or "BUY filled and SELL placed; SELL fill not yet confirmed"
    return out


def exit_plan(long_pos, short_pos, together=False):
    """Orders to flatten a debit spread.

    Default: buy back the short first (margin-safer), then sell the long.
    together=True emits both in one batch after the short cover is still listed
    first so a caller that can send atomically still has a defined order.
    """
    if not long_pos or not short_pos:
        return {"ok": False, "reason": "both legs are required", "legs": []}
    cover = dict(short_pos, transaction_type="BUY", role="cover_short", sequence=1)
    close_long = dict(long_pos, transaction_type="SELL", role="close_long", sequence=2)
    return {"ok": True, "together": bool(together), "short_first": True,
            "reason": "buy back the short first, then sell the long" if not together
                      else "cover the short and sell the long together (short listed first)",
            "legs": [cover, close_long]}


def _norm_sym(sym):
    return (sym or "").replace(" ", "").replace("-", "").upper()


def load_ledger(path=None):
    try:
        with open(path or LEDGER) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ledger(state, path=None):
    path = path or LEDGER
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, default=str)
    os.replace(tmp, path)


def record_spread(state, *, broker, underlying, expiry, side, long_sym, short_sym, qty, ts, strike_step=None):
    pos = state.setdefault("positions", [])
    pos.append({"broker": broker, "underlying": underlying, "expiry": str(expiry),
                "side": side, "long": long_sym, "short": short_sym, "qty": qty,
                "opened": ts, "strike_step": strike_step})
    return state


def is_recorded_spread_leg(sym, ledger, role=None):
    """role is 'long', 'short', or None (either)."""
    want = _norm_sym(sym)
    for row in (ledger or {}).get("positions") or []:
        if role in (None, "long") and _norm_sym(row.get("long")) == want:
            return True
        if role in (None, "short") and _norm_sym(row.get("short")) == want:
            return True
    return False


def infer_pairs(positions):
    """Group same-broker stock-option longs with a further-OTM short of the same type and expiry.

    positions: iterable of dicts with broker, symbol, qty, side, expiry, strike, underlying.
    """
    by = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        try:
            qty = int(p.get("qty") if p.get("qty") is not None else p.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        key = (p.get("broker"), (p.get("underlying") or "").upper(),
               expiry_key(p.get("expiry")) or str(parse_day(p.get("expiry")) or ""),
               (p.get("side") or "").upper())
        by.setdefault(key, {"longs": [], "shorts": []})
        if qty > 0:
            by[key]["longs"].append(p)
        else:
            by[key]["shorts"].append(p)
    pairs = []
    for key, g in by.items():
        for lg in g["longs"]:
            try:
                ls = float(lg.get("strike") or 0)
            except (TypeError, ValueError):
                continue
            side = key[3]
            for sh in g["shorts"]:
                try:
                    ss = float(sh.get("strike") or 0)
                except (TypeError, ValueError):
                    continue
                otm = (side == "CE" and ss > ls) or (side == "PE" and ss < ls)
                if otm:
                    pairs.append({"broker": key[0], "underlying": key[1], "expiry": key[2],
                                  "side": side, "long": lg, "short": sh})
    return pairs


def gtt_skip_reason(row, ledger=None, pairs=None):
    """Why a GTT must not be placed on this option row, or None if a long plain option is fine.

    Shorts are never given a SELL GTT. Spread longs are never given a lone SELL GTT.
    """
    try:
        qty = int(row.get("qty") if row.get("qty") is not None else row.get("quantity") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty < 0:
        return "short option leg; a SELL GTT would increase the short (naked or worse)"
    if qty <= 0:
        return "not a long option"
    sym = row.get("sym") or row.get("trading_symbol") or row.get("tradingsymbol")
    if is_recorded_spread_leg(sym, ledger, role="long"):
        return "debit-spread long; a lone SELL GTT would leave the short naked"
    if is_recorded_spread_leg(sym, ledger, role="short"):
        return "debit-spread short; a SELL GTT would increase the short"
    want = _norm_sym(sym)
    for p in pairs or []:
        if _norm_sym((p.get("long") or {}).get("sym") or (p.get("long") or {}).get("trading_symbol")) == want:
            return "inferred debit-spread long; a lone SELL GTT would leave the short naked"
        if _norm_sym((p.get("short") or {}).get("sym") or (p.get("short") or {}).get("trading_symbol")) == want:
            return "inferred debit-spread short; a SELL GTT would increase the short"
    return None


def leave_next_month_spread(exp, today):
    """True when this contract expires after this week's expiry-week flatten window.

    Next-month debit-spread legs must not be squared off in the current monthly's
    expiry week. (Their own expiry week later is a separate decision.)
    """
    exp, today = parse_day(exp), parse_day(today)
    if not exp or not today or exp <= today:
        return False
    return not in_expiry_week(exp, today)


def parse_option_symbol(sym):
    """Best-effort parse of compact or spaced option symbols.

    Compact forms often have the year and month but not the day
    (RELIANCE26DEC1260CE); expiry is then the first of that month and
    pairing uses the year-month only.
    """
    import re
    raw = (sym or "").strip()
    s = raw.replace(" ", "").replace("-", "").upper()
    months = {m: i for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
    spaced = re.search(r"([A-Z&]+?)\s+(\d+(?:\.\d+)?)\s+(CE|PE)\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{2,4})$", raw.upper())
    if spaced:
        yy = spaced.group(6)
        y = int(yy) + (2000 if len(yy) == 2 else 0)
        mon = months.get(spaced.group(5))
        if not mon:
            return None
        try:
            exp = dt.date(y, mon, int(spaced.group(4)))
        except Exception:
            return None
        return {"underlying": spaced.group(1), "expiry": exp, "strike": float(spaced.group(2)),
                "side": spaced.group(3), "expiry_precise": True}
    compact = re.search(r"([A-Z&]+?)(\d{2})([A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$", s)
    if not compact:
        return None
    mon = months.get(compact.group(3))
    if not mon:
        return None
    try:
        exp = dt.date(2000 + int(compact.group(2)), mon, 1)
    except Exception:
        return None
    return {"underlying": compact.group(1), "expiry": exp, "strike": float(compact.group(4)),
            "side": compact.group(5), "expiry_precise": False}


def expiry_key(exp):
    d = parse_day(exp)
    return None if d is None else (d.year, d.month)


def legs_from_positions(positions, broker, sym_field=None):
    """Normalise broker position rows for infer_pairs / GTT / risk-manager skips."""
    out = []
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sym = (p.get(sym_field) if sym_field else None) or p.get("trading_symbol") or p.get("tradingsymbol") or p.get("sym") or ""
        parsed = parse_option_symbol(sym)
        if parsed is None and (p.get("side") or p.get("instrument_type")) in ("CE", "PE"):
            parsed = {"underlying": (p.get("underlying") or "").upper(),
                      "expiry": parse_day(p.get("expiry")),
                      "strike": p.get("strike") or p.get("strike_price"),
                      "side": (p.get("side") or p.get("instrument_type") or "").upper(),
                      "expiry_precise": True}
        if parsed is None:
            continue
        try:
            qty = int(p.get("qty") if p.get("qty") is not None else p.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        out.append({
            "broker": broker or p.get("broker"),
            "underlying": parsed.get("underlying") or p.get("underlying"),
            "expiry": parsed.get("expiry") or parse_day(p.get("expiry")),
            "side": parsed.get("side"),
            "strike": parsed.get("strike") or p.get("strike") or p.get("strike_price"),
            "qty": qty,
            "quantity": qty,
            "sym": sym,
            "trading_symbol": sym,
            "ltp": p.get("ltp") or p.get("last_price"),
            "last_price": p.get("last_price") or p.get("ltp"),
            "avg": p.get("avg") or p.get("average_price") or p.get("entry"),
            "average_price": p.get("average_price") or p.get("avg") or p.get("entry"),
            "entry": p.get("entry") or p.get("average_price") or p.get("avg"),
            "key": p.get("key") or p.get("instrument_token") or p.get("instrument_key"),
            "instrument_token": p.get("instrument_token") or p.get("instrument_key") or p.get("key"),
            "product": p.get("product"),
            "exchange": p.get("exchange") or p.get("kite_exchange"),
        })
    return out


def signal_analyze_args():
    """Keyword arguments for ind.analyze on a stock-option underlying."""
    return {"unit": SIGNAL_UNIT, "interval": SIGNAL_INTERVAL, "days": SIGNAL_DAYS}


# ----- t691u: net-premium stop, paired exits, overnight GTT, naked-short reconcile -----

INITIAL_STOP_PCT = 0.20          # 20% of the net debit
LADDER_ARM = 1.15                # breakeven when net marks +15%
LADDER_STEP = 0.20


def _px(*vals):
    for v in vals:
        if v is None:
            continue
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if n == n:
            return n
    return None


def net_premium(long_px, short_px):
    """Debit (positive) of a long minus a short of the same type."""
    lo, sh = _px(long_px), _px(short_px)
    if lo is None or sh is None:
        return None
    return lo - sh


def net_stop_level(net_entry, stop_pct=INITIAL_STOP_PCT):
    """Close the spread when net value falls to or below this (80% of the debit)."""
    net = _px(net_entry)
    if net is None or net <= 0:
        return None
    return net * (1.0 - float(stop_pct))


def net_ladder_floor(net_entry, net_high, arm=LADDER_ARM, step=LADDER_STEP):
    """Upward-only floor on net value. Breakeven at +15%, then the previous +20% step."""
    entry, high = _px(net_entry), _px(net_high)
    if entry is None or high is None or entry <= 0 or high <= 0:
        return 0.0
    floor = 0.0
    if high >= entry * arm:
        floor = entry
    # same roll count as full_auto_policy.ladder_floor
    import math
    rolls = int(math.floor(((high / entry) - 1.0) / step + 1e-9))
    if rolls >= 1:
        floor = max(floor, entry * (1.0 + step * (rolls - 1)))
    return floor


def _leg_px(leg, *keys):
    if not isinstance(leg, dict):
        return None
    vals = []
    for k in keys or ("ltp", "last_price", "avg", "average_price", "entry"):
        vals.append(leg.get(k))
    return _px(*vals)


def spread_day_decision(long_leg, short_leg, state=None, *, extra_fire=None, extra_why=None,
                        stop_pct=INITIAL_STOP_PCT):
    """Whether a debit spread must be closed now.

    Prefers a net-premium stop (20% of the net debit) plus the existing ladder
    on net value (breakeven at +15%, then +20% steps). If net prices are
    missing, falls back to a 20% stop on the long leg. In either case a fire
    means close BOTH legs, short first. extra_fire covers a trend / two-candle
    / EMA50 exit that already applied to the long.
    """
    state = dict(state or {})
    out = {"fire": False, "action": None, "why": None, "method": None,
           "net_entry": None, "net_now": None, "net_high": None, "stop": None,
           "short_first": True, "state": state}
    if extra_fire:
        out.update(fire=True, action=extra_fire if isinstance(extra_fire, str) else "SPREAD_EXIT",
                   why=extra_why or "long-leg exit applies; close both legs, short first",
                   method="long_exit")
        return out
    long_avg = _leg_px(long_leg, "avg", "average_price", "entry", "buy_price")
    short_avg = _leg_px(short_leg, "avg", "average_price", "entry", "sell_price")
    long_ltp = _leg_px(long_leg, "ltp", "last_price")
    short_ltp = _leg_px(short_leg, "ltp", "last_price")
    net_entry = _px(state.get("net_entry"), net_premium(long_avg, short_avg))
    net_now = net_premium(long_ltp, short_ltp)
    out["net_entry"] = net_entry
    out["net_now"] = net_now
    if net_entry and net_entry > 0 and net_now is not None:
        net_high = max(_px(state.get("net_high")) or net_now, net_now, net_entry)
        state["net_high"] = net_high
        state["net_entry"] = net_entry
        floor = net_ladder_floor(net_entry, net_high)
        stop = max(net_stop_level(net_entry, stop_pct), floor)
        out.update(method="net_premium", net_high=net_high, stop=stop, state=state)
        if net_now <= stop + 1e-9:
            why = ("net premium %.2f at or below stop %.2f (entry debit %.2f, 20%% + ladder)"
                   % (net_now, stop, net_entry))
            action = "SPREAD_LADDER_STOP" if floor and net_now <= floor + 1e-9 and floor > net_stop_level(net_entry, stop_pct) + 1e-9 else "SPREAD_NET_STOP"
            out.update(fire=True, action=action, why=why)
        return out
    # fallback: 20% on the long, still close both
    if long_avg and long_avg > 0 and long_ltp is not None:
        stop = long_avg * (1.0 - float(stop_pct))
        out.update(method="long_leg", stop=stop)
        if long_ltp <= stop + 1e-9:
            out.update(fire=True, action="SPREAD_LONG_STOP",
                       why="long LTP %.2f at or below 20%% stop %.2f; close both legs, short first"
                           % (long_ltp, stop))
        return out
    out["why"] = "spread marks unverified; no stop fired"
    return out


def long_stop_from_net(long_avg, short_avg, stop_pct=INITIAL_STOP_PCT):
    """Long-leg GTT trigger that realises a 20% net-debit loss if the short is unchanged."""
    net = net_premium(long_avg, short_avg)
    long_avg = _px(long_avg)
    if long_avg is None or long_avg <= 0:
        return None
    if net and net > 0:
        return max(0.05, long_avg - float(stop_pct) * net)
    return long_avg * (1.0 - float(stop_pct))


def corresponding_short_trigger(long_avg, long_stop, short_avg, short_ltp=None):
    """BUY-BELOW trigger on the short, scaled to the same fractional move as the long stop."""
    long_avg, long_stop, short_avg = _px(long_avg), _px(long_stop), _px(short_avg)
    short_px = _px(short_ltp, short_avg)
    if not long_avg or long_avg <= 0 or not long_stop or not short_px or short_px <= 0:
        return None
    ratio = long_stop / long_avg
    if ratio <= 0 or ratio >= 1:
        ratio = 0.80
    return max(0.05, short_px * ratio)


def gtt_spread_plan(long_leg, short_leg, stop_pct=INITIAL_STOP_PCT, tick=0.05):
    """Paired overnight GTT plan: SELL BELOW on the long, BUY BELOW on the short.

    Upstox: type=SINGLE, trigger_type=BELOW, transaction_type SELL (long) / BUY (short).
    Kite: GTT_TYPE_SINGLE, SELL on the long, BUY on the short, trigger below LTP.
    A SELL GTT is never produced for the short. Returns None triggers when unverified.
    """
    def snap(p, t=tick):
        t = t or 0.05
        try:
            p = float(p)
        except (TypeError, ValueError):
            return None
        return round(max(round(p / t) * t, t), 4)

    long_avg = _leg_px(long_leg, "avg", "average_price", "entry")
    short_avg = _leg_px(short_leg, "avg", "average_price", "entry")
    long_ltp = _leg_px(long_leg, "ltp", "last_price")
    short_ltp = _leg_px(short_leg, "ltp", "last_price")
    long_qty = abs(int(long_leg.get("qty") if long_leg.get("qty") is not None else long_leg.get("quantity") or 0))
    short_qty = abs(int(short_leg.get("qty") if short_leg.get("qty") is not None else short_leg.get("quantity") or 0))
    long_stop = long_stop_from_net(long_avg, short_avg, stop_pct)
    if long_stop is None and long_ltp:
        long_stop = long_stop_from_net(long_ltp, short_ltp or short_avg, stop_pct)
    short_trig = corresponding_short_trigger(long_avg or long_ltp, long_stop, short_avg or short_ltp, short_ltp)
    long_stop = snap(long_stop, tick)
    short_trig = snap(short_trig, tick)
    # triggers must sit below live prices so they do not fire immediately
    if long_stop and long_ltp and long_stop >= long_ltp:
        long_stop = snap(min(long_stop, long_ltp - (tick or 0.05)), tick)
    if short_trig and short_ltp and short_trig >= short_ltp:
        short_trig = snap(min(short_trig, short_ltp - (tick or 0.05)), tick)
    long_limit = snap((long_stop or 0) * 0.95, tick) if long_stop else None
    short_limit = snap((short_trig or 0) * 1.05, tick) if short_trig else None  # buy a bit above trigger to fill
    ok = bool(long_stop and short_trig and long_qty > 0 and short_qty > 0)
    return {
        "ok": ok,
        "why": None if ok else "spread GTT levels unverified",
        "long": {"sym": long_leg.get("sym") or long_leg.get("trading_symbol") or long_leg.get("tradingsymbol"),
                 "transaction_type": "SELL", "trigger_type": "BELOW", "trigger": long_stop,
                 "limit": long_limit, "qty": long_qty, "ltp": long_ltp, "role": "stop_long",
                 "key": long_leg.get("key") or long_leg.get("instrument_token")},
        "short": {"sym": short_leg.get("sym") or short_leg.get("trading_symbol") or short_leg.get("tradingsymbol"),
                  "transaction_type": "BUY", "trigger_type": "BELOW", "trigger": short_trig,
                  "limit": short_limit, "qty": short_qty, "ltp": short_ltp, "role": "cover_short",
                  "key": short_leg.get("key") or short_leg.get("instrument_token")},
        "net_entry": net_premium(long_avg, short_avg),
        "never_sell_short": True,
    }


def find_pair(sym, pairs):
    want = _norm_sym(sym)
    for p in pairs or []:
        lg = p.get("long") or {}
        sh = p.get("short") or {}
        if _norm_sym(lg.get("sym") or lg.get("trading_symbol") or lg.get("tradingsymbol")) == want:
            return p, "long"
        if _norm_sym(sh.get("sym") or sh.get("trading_symbol") or sh.get("tradingsymbol")) == want:
            return p, "short"
    return None, None


def _qty_of(pos):
    try:
        return int(pos.get("qty") if pos.get("qty") is not None else pos.get("quantity") or 0)
    except (TypeError, ValueError):
        return 0


def reconcile_naked_shorts(positions, ledger=None, pairs=None):
    """Buy-back plans for a spread short whose long is gone (qty<=0).

    Used at 9:15 IST and on every day-session risk_manager / gtt run. A lone
    short is never left without a cover instruction.
    """
    pairs = list(pairs or [])
    if not pairs:
        pairs = infer_pairs(positions)
    # also walk the ledger so a recorded short is found even if inference missed it
    by_sym = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sym = p.get("sym") or p.get("trading_symbol") or p.get("tradingsymbol")
        if sym:
            by_sym[_norm_sym(sym)] = p
    plans = []
    seen = set()
    candidates = list(pairs)
    for row in (ledger or {}).get("positions") or []:
        candidates.append({"broker": row.get("broker"), "underlying": row.get("underlying"),
                           "expiry": row.get("expiry"), "side": row.get("side"),
                           "long": {"sym": row.get("long"), "qty": 0},
                           "short": {"sym": row.get("short"), "qty": -abs(int(row.get("qty") or 0) or 0)}})
    for p in candidates:
        sh = dict(p.get("short") or {})
        lg = dict(p.get("long") or {})
        short_sym = sh.get("sym") or sh.get("trading_symbol") or sh.get("tradingsymbol")
        long_sym = lg.get("sym") or lg.get("trading_symbol") or lg.get("tradingsymbol")
        if not short_sym:
            continue
        live_short = by_sym.get(_norm_sym(short_sym), sh)
        live_long = by_sym.get(_norm_sym(long_sym), lg) if long_sym else lg
        sq = _qty_of(live_short)
        lq = _qty_of(live_long) if live_long else 0
        if sq >= 0:
            continue
        if lq > 0:
            continue
        key = (p.get("broker"), _norm_sym(short_sym))
        if key in seen:
            continue
        seen.add(key)
        cover = dict(live_short, transaction_type="BUY", role="cover_naked_short",
                     quantity=abs(sq), qty=abs(sq),
                     why="spread long is gone; buy back the leftover short immediately")
        plans.append({"broker": p.get("broker") or live_short.get("broker"),
                      "short_sym": short_sym, "long_sym": long_sym,
                      "qty": abs(sq), "order": cover, "action": "COVER_NAKED_SHORT"})
    return plans


def execute_pair_exit(place, wait_fill, cover, close_long, *, live_qty=None, cancel=None, alert=None, retries=1):
    """Buy back the short, then sell the long. Re-reads qty, retries partials once.

    If the cover fails or a short remainder is still open, the long is NOT sold
    (the hedge stays). If the cover fills and the long sell fails, the leftover
    is a lone long — alert, and its normal stop still applies.
    """
    out = {"ok": False, "cover_id": None, "long_id": None, "cover_filled": False,
           "long_sold": False, "alert": None, "reason": None, "short_first": True}
    if place is None:
        out["reason"] = "sender unverified"
        return out
    cover = dict(cover or {}, transaction_type="BUY")
    close_long = dict(close_long or {}, transaction_type="SELL")
    if live_qty:
        try:
            sq = live_qty(cover)
            if sq is not None:
                cover["quantity"] = abs(int(sq))
                cover["qty"] = abs(int(sq))
        except Exception:
            pass
        try:
            lq = live_qty(close_long)
            if lq is not None:
                close_long["quantity"] = abs(int(lq))
                close_long["qty"] = abs(int(lq))
        except Exception:
            pass
    if int(cover.get("quantity") or cover.get("qty") or 0) <= 0:
        # no short left: sell the long only
        pass
    else:
        remaining = int(cover.get("quantity") or cover.get("qty"))
        attempts = 1 + max(0, int(retries))
        filled = 0
        for attempt in range(1, attempts + 1):
            leg = dict(cover, quantity=remaining, qty=remaining)
            try:
                oid = place(leg)
            except Exception as e:
                out["reason"] = "short cover send failed: %s" % type(e).__name__
                oid = None
            if not oid:
                out["reason"] = out.get("reason") or "short cover rejected"
                if attempt < attempts:
                    continue
                out["alert"] = out["reason"] + "; long was not sold so the hedge stays"
                if alert:
                    try:
                        alert(out["alert"], {"cover": cover, "remaining": remaining})
                    except Exception:
                        pass
                return out
            out["cover_id"] = oid
            fill = {"filled": True, "qty": remaining}
            if wait_fill is not None:
                try:
                    fill = wait_fill(oid, leg) or fill
                except Exception as e:
                    fill = {"filled": False, "error": type(e).__name__}
            got = int(fill.get("qty") or fill.get("filled_qty") or (remaining if fill.get("filled") else 0))
            if fill.get("filled") and got <= 0:
                got = remaining
            filled += max(got, 0)
            remaining = max(remaining - max(got, 0), 0)
            if remaining <= 0:
                break
        out["cover_filled_qty"] = filled
        if remaining > 0:
            out["reason"] = "short cover partial (%d left); long was not sold so the remainder stays hedged" % remaining
            out["alert"] = out["reason"]
            if alert:
                try:
                    alert(out["reason"], {"cover": cover, "remaining": remaining, "filled": filled})
                except Exception:
                    pass
            return out
        out["cover_filled"] = True

    long_qty = int(close_long.get("quantity") or close_long.get("qty") or 0)
    if live_qty:
        try:
            lq = live_qty(close_long)
            if lq is not None:
                long_qty = abs(int(lq))
        except Exception:
            pass
    if long_qty <= 0:
        out.update(ok=True, reason="short covered; no long left to sell")
        return out
    close_long = dict(close_long, quantity=long_qty, qty=long_qty)
    try:
        lid = place(close_long)
    except Exception as e:
        lid = None
        out["reason"] = "short covered but long SELL failed: %s" % type(e).__name__
    if not lid:
        out["reason"] = out.get("reason") or "short covered but long SELL was rejected; leftover is a lone long"
        out["alert"] = out["reason"]
        if alert:
            try:
                alert(out["reason"], {"cover_id": out.get("cover_id"), "long": close_long})
            except Exception:
                pass
        return out
    out["long_id"] = lid
    fill = {"filled": True}
    if wait_fill is not None:
        try:
            fill = wait_fill(lid, close_long) or fill
        except Exception as e:
            fill = {"filled": False, "error": type(e).__name__}
    out["long_sold"] = bool(fill.get("filled"))
    if out["long_sold"]:
        out.update(ok=True, reason="spread closed (short bought back, long sold)")
    else:
        out["reason"] = "short covered; long SELL placed but fill not confirmed"
        out["alert"] = out["reason"]
    return out


def flatten_leg_order(positions, wanted, ledger=None, pairs=None):
    """Ordered flatten legs for wanted underlyings: cover shorts first, then sell longs.

    `wanted` is a set of underlying names (upper). Plain longs with no short
    still get a SELL. Returns a list of order dicts.
    """
    wanted = {str(x).upper() for x in (wanted or [])}
    legs = []
    if not pairs:
        pairs = infer_pairs(positions)
    paired_syms = set()
    for p in pairs or []:
        lg, sh = p.get("long") or {}, p.get("short") or {}
        und = (p.get("underlying") or lg.get("underlying") or sh.get("underlying") or "").upper()
        if und not in wanted:
            continue
        short_qty = _qty_of(sh)
        long_qty = _qty_of(lg)
        if short_qty < 0:
            legs.append(dict(sh, broker=p.get("broker") or sh.get("broker"),
                             trading_symbol=sh.get("sym") or sh.get("trading_symbol") or sh.get("tradingsymbol"),
                             underlying=und, quantity=abs(short_qty), qty=abs(short_qty),
                             transaction_type="BUY", role="cover_short", sequence=1,
                             reason="close both legs by Friday / expiry, short first"))
            paired_syms.add(_norm_sym(sh.get("sym") or sh.get("trading_symbol") or sh.get("tradingsymbol")))
        if long_qty > 0:
            legs.append(dict(lg, broker=p.get("broker") or lg.get("broker"),
                             trading_symbol=lg.get("sym") or lg.get("trading_symbol") or lg.get("tradingsymbol"),
                             underlying=und, quantity=long_qty, qty=long_qty,
                             transaction_type="SELL", role="close_long", sequence=2,
                             reason="close both legs by Friday / expiry, short first"))
            paired_syms.add(_norm_sym(lg.get("sym") or lg.get("trading_symbol") or lg.get("tradingsymbol")))
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sym = p.get("sym") or p.get("trading_symbol") or p.get("tradingsymbol") or ""
        if _norm_sym(sym) in paired_syms:
            continue
        und = (p.get("underlying") or "").upper()
        if und not in wanted:
            continue
        qty = _qty_of(p)
        if qty > 0:
            legs.append(dict(p, trading_symbol=sym, underlying=und, quantity=qty, qty=qty,
                             transaction_type="SELL", role="close_long", sequence=2,
                             reason="close by Friday's close"))
        elif qty < 0 and is_recorded_spread_leg(sym, ledger, role="short"):
            legs.append(dict(p, trading_symbol=sym, underlying=und, quantity=abs(qty), qty=abs(qty),
                             transaction_type="BUY", role="cover_short", sequence=1,
                             reason="buy back leftover spread short"))
    legs.sort(key=lambda r: (r.get("broker") or "", int(r.get("sequence") or 2), r.get("trading_symbol") or ""))
    return legs
