"""Shared open-generation and stop-floor reset for a position's current life.

risk_manager.py and crude_auto.py both keep a peak and a stop floor per contract.
Those must reset when the book goes flat and is bought again; otherwise a later
re-entry inherits the previous life's stop (16 Nov 2026: Kite CRUDEOIL26NOV8300CE
kept a 100.3 stop from a 125.40 entry after a new 120 entry).

This module has no broker I/O and is safe to import.
"""
from __future__ import annotations

import datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_FILL_DONE = {
    "complete", "completed", "filled", "traded",
    "partially filled", "partial complete", "partially complete",
}


def completed_fills(orders, sym=None, token=None):
    """Completed BUY/SELL fills for one contract, oldest first."""
    out = []
    tok = str(token or "")
    for o in orders or []:
        osym = o.get("trading_symbol") or o.get("tradingsymbol") or ""
        otok = str(o.get("instrument_token") or o.get("instrument_key") or "")
        if not ((sym and osym == sym) or (tok and otok == tok)):
            continue
        side = (o.get("transaction_type") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        st = (o.get("status") or "").lower().replace("_", " ")
        if st not in _FILL_DONE:
            continue
        fq = int(o.get("filled_quantity") or 0)
        if fq <= 0 and st in ("complete", "completed", "filled", "traded"):
            fq = int(o.get("quantity") or 0)
        if fq <= 0:
            continue
        ts = str(o.get("exchange_timestamp") or o.get("order_timestamp")
                 or o.get("order_time") or o.get("exchange_time") or "")
        oid = str(o.get("order_id") or o.get("orderId") or "")
        out.append((ts, side, fq, oid))
    out.sort()
    return out


def current_open_gen(orders, sym=None, token=None, overnight=0):
    """Id of the buy that opened the current long (after the last flatten).

    flats_before>0 means this is a re-entry.
    """
    run = int(overnight or 0)
    gen = ("overnight:%d" % run) if run > 0 else None
    saw_sell_flat = False
    flats_before = 0
    for ts, side, q, oid in completed_fills(orders, sym, token):
        if side == "BUY":
            if run <= 0:
                if saw_sell_flat:
                    flats_before += 1
                gen = "%s|%s" % (ts, oid)
            run += q
        else:
            run -= q
            if run <= 0:
                run = 0
                gen = None
                saw_sell_flat = True
    return gen, flats_before


def current_open_fill(orders, sym=None, token=None, overnight=0):
    """Timestamp and order id of the buy that opened the current long.

    Returns (fill_ts, order_id, flats_before). fill_ts and order_id are None
    when the book is flat, the position is an overnight carry with no day
    opening fill, or the tape does not identify the opening buy. An add after
    the open does not change the returned fill (same rule as current_open_gen).
    """
    gen, flats_before = current_open_gen(orders, sym=sym, token=token, overnight=overnight)
    if not gen or str(gen).startswith("overnight:"):
        return None, None, flats_before
    ts, _sep, oid = str(gen).partition("|")
    ts = ts.strip() or None
    oid = oid.strip() or None
    if not ts:
        return None, None, flats_before
    return ts, oid, flats_before


def bind_position_peak(s, avg, ltp, qty, *, day_sell=0, overnight=0, orders=None,
                       sym=None, token=None, step=0.20, now=None,
                       initial_stop_pct=None, seeds=None):
    """Ratchet the +15% peak from this position's own fill, not the contract's earlier high.

    Reset when the book went flat and was re-entered (observed live_qty==0, a new
    opening-fill id, or today's tape shows a flatten before the current opening).
    Adds keep the existing high. On reset the stop floor (s['stop'] and seeds[sym])
    is cleared so the new entry gets a fresh initial stop, not the prior life's.
    Returns (reset, why, prev_high).
    """
    gen, flats_before = current_open_gen(orders, sym=sym, token=token, overnight=overnight)
    prev_gen = s.get("open_gen")
    prev_qty = s.get("live_qty")
    prev_high = s.get("high")
    reset = False
    why = None
    if qty > 0 and prev_qty == 0:
        reset, why = True, "reentry after observed flat"
    elif qty > 0 and prev_gen and gen and prev_gen != gen:
        reset, why = True, "new opening fill after flatten"
    elif qty > 0 and prev_gen is None and gen and flats_before:
        reset, why = True, "legacy peak from an earlier life of this contract"
    if reset:
        s["high"] = max(float(avg or 0), float(ltp or 0))
        s["entry"] = avg
        s["target"] = round(float(avg) * (1 + step), 2)
        s.pop("stop", None)
        s["peak_reset"] = why
        if initial_stop_pct is not None:
            s["stop_pct"] = initial_stop_pct
        if seeds is not None and sym:
            seeds.pop(sym, None)
    else:
        s.setdefault("entry", avg)
        s["high"] = max(s.get("high", avg), ltp)
    if gen:
        s["open_gen"] = gen
    elif qty <= 0:
        s["open_gen"] = None
    s["live_qty"] = qty
    s["day_sell_qty"] = day_sell
    if now is None:
        now = datetime.datetime.now(IST)
    s["asof"] = now.date().isoformat() if hasattr(now, "date") else str(now)[:10]
    return reset, why, prev_high


def sl_modify_trigger(computed_stop, live_trigger, qty_changed=False, reset=False):
    """Trigger to write to a working day SL, or None if the live stop is already right.

    A re-entry (reset=True) may loosen to the new life's floor. Otherwise the stop
    only ratchets up, never down.
    """
    live = float(live_trigger or 0)
    stop = float(computed_stop or 0)
    if reset or stop > live * 1.001 or qty_changed:
        return stop if reset else max(stop, live)
    return None
