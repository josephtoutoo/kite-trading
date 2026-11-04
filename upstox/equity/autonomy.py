#!/usr/bin/env python3
"""Two-day rule override: 5 Nov 2026 and 6 Nov 2026 only (user, 4 Nov 2026 t339u).

"Atleast next 2 days don't take any of my rules, please do it on your own if possible."

That is a temporary, dated grant. This module is the whole of it: a date test and the
numbers for those two sessions. It places nothing, reads no broker book, touches no
order and no position - the only thing it imports is datetime, so there is no transport,
no token and no import-order surprise hiding in it.

AUTO-EXPIRY IS THE POINT. `active()` is False on every date except 2026-11-05 and
2026-11-06, so on Saturday 7 Nov the user's own rules come back on their own, with no
code change, no cron edit and nothing to remember. The dates are compared as IST
calendar dates, so a run at 00:30 IST already belongs to the new day.

Callers ask per rule, never by mutating a module global at import time:

    eff = autonomy.effective('WINDOW_MIN', trade_lock.WINDOW_MIN, now)

-> the constant unchanged when the override is not active (the default), else the
two-day value. Every constant keeps its own meaning, so a cap can only move where the
caller explicitly asked for it.

The two-day numbers, chosen off the measured per-book record in equity/book_pnl.json
(1 Oct - 4 Nov, realised, before charges):
  index options  -0.81% on Rs 32.5L over 44 trades  (since 30 Jun: -0.74%, 139 trades)
  stock options  +7.89% on Rs 13.9L over 28 trades  (the only positive book with a sample)
  MTF            no usable history (Upstox cannot split MTF from delivery; Kite has today only)
So the book with the evidence gets more room and the book without it gets wider per-trade
limits but a hard 13:00 stop on expiry days, rather than being switched off.
"""
import datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# The grant, exactly. Nothing else is an override day - not a weekend, not a holiday,
# not "the next two trading days" (the user said two days, and 5-6 Nov are both sessions).
DAYS = ("2026-11-05", "2026-11-06")

# Effective rule values for those two sessions. Keyed by the constant name each caller
# passes to effective(), so a reader can line the two up one for one.
_OVERRIDES = {
    # index options: more decisions, but spaced, and shut off into an expiry close
    "INDEX_OPT_MAX": 6,             # was 3 decisions per index per day
    "INDEX_OPT_GAP_MIN": 20,        # NEW check: minutes between two decisions on one index
    "INDEX_OPT_EXPIRY_CUTOFF": "13:00",  # NEW check: no new index-option decision after 13:00 on an expiry day
    "INDEX_OPT_LOSS": 6000,         # was: stop this index's options at Rs 2,000 booked loss
    # both windows: an opposite side or a re-entry may follow twice as fast
    "WINDOW_MIN": 15,               # was 30
    # stock options: the one book with a positive measured record, so it gets the room
    "STOCK_OPT_MAX_UND": 4,         # was 3 underlyings
    "STOCK_OPT_MAX_LOTS": 2,        # was 1 lot per underlying (R11)
    # MTF: a little more per name, the same total
    "MTF_PER_STOCK": 0.12,          # was 0.10 of that broker's capital
    "MTF_TOTAL": 0.50,              # NOT raised - stated here so the record shows it was considered
}

_WHY = ("5-6 Nov only, off the measured book P&L: index decisions 3->6 but 20 min apart and none "
        "after 13:00 on an expiry day, index booked-loss cutoff Rs 2k->6k, opposite-side and "
        "re-entry windows 30->15 min, stock options (the only positive book, +7.89%) 3x1 -> 4x2 "
        "underlyings/lots, MTF per stock 10%->12% with the 50% total unchanged")


def _ist_date(when=None):
    """`when` as an IST calendar date: None (now), a date, a datetime, or an ISO string.
    A naive datetime is read as IST, which is what every caller in this tree passes."""
    if when is None:
        return datetime.datetime.now(IST).date()
    if isinstance(when, str):
        when = datetime.datetime.fromisoformat(when) if "T" in when or " " in when else datetime.date.fromisoformat(when)
    if isinstance(when, datetime.datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        return when.astimezone(IST).date()
    return when


def active(when=None):
    """True only on 2026-11-05 or 2026-11-06 (IST). False on every other day, for ever."""
    try:
        return _ist_date(when).isoformat() in DAYS
    except Exception:
        # An unreadable clock is not a licence to relax the user's rules.
        return False


def overrides(when=None):
    """The two-day rule values (a copy), or {} when the override is not active."""
    if not active(when):
        return {}
    return dict(_OVERRIDES, why=_WHY)


def effective(name, default, when=None):
    """The value a rule should use: `default` normally, the two-day value while active.

    Absent from the override table -> `default` even when active, so a caller can never
    pick up a relaxation it did not ask for by name."""
    if not active(when):
        return default
    return _OVERRIDES.get(name, default)


def status(when=None):
    """One dict for a script's JSON output: which rule set this run is operating under."""
    d = _ist_date(when)
    on = d.isoformat() in DAYS
    return {"active": on,
            "rule_set": "autonomy 2026-11-05..06" if on else "user rules",
            "ist_date": d.isoformat(),
            "override_days": list(DAYS),
            "overrides": dict(_OVERRIDES) if on else {},
            "why": _WHY if on else "user's own rules (override not active)"}


def rule_set(when=None):
    """JSON-ready block for an entry script's output: which rule set this run is under.

    Every entry script prints this, so a log line always says whether the numbers in the rest
    of its output came from the user's rules or from the two-day ones. Pure; places nothing."""
    st = status(when)
    d = {"rule_set": st["rule_set"], "ist_date": st["ist_date"]}
    if st["active"]:
        o = st["overrides"]
        d["override"] = (f"2-day autonomy {st['override_days'][0]}..{st['override_days'][1]}: "
                         f"index decisions {o['INDEX_OPT_MAX']} (>= {o['INDEX_OPT_GAP_MIN']} min apart, "
                         f"none after {o['INDEX_OPT_EXPIRY_CUTOFF']} on an expiry day), "
                         f"index booked-loss cutoff Rs {o['INDEX_OPT_LOSS']:,}, "
                         f"side/re-entry windows {o['WINDOW_MIN']} min, "
                         f"stock options {o['STOCK_OPT_MAX_UND']} underlyings x {o['STOCK_OPT_MAX_LOTS']} lots, "
                         f"MTF per stock {o['MTF_PER_STOCK'] * 100:g}% (total {o['MTF_TOTAL'] * 100:g}% unchanged)")
        d["unchanged"] = ("Rs 30,000 account day-loss breaker; fail-closed reads; R8/R9; "
                          "mirrored MTF names; 6 MTF names per broker")
    else:
        d["override"] = "none - user's own rules apply"
    return d


if __name__ == "__main__":
    import json, sys
    print(json.dumps(status(sys.argv[1] if len(sys.argv) > 1 else None), indent=1))
