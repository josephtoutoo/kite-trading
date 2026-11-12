"""Index-option timeframe and listed expiry. Off unless INDEX_CONTRACT_POLICY=1.

When that flag is on, the default route is a completed 1-hour signal and the
listed next-week expiry for NIFTY and SENSEX. That route starts at 1 lot and
may record one +2 add, 3 lots maximum. The current-week route is completed
15-minute only, starts at 1 lot, and may record one +1 add, 2 lots maximum.
BANKNIFTY is completed 1-hour and the listed monthly expiry only, with the
same 1-then-+2 shape. Adds are eligibility records. They are not sent.

5-minute signals are not a fallback. A missing next-week contract is not
replaced by the current week, by the second listed date, or by session+7 days.
Monthly means "this date is a futures expiry in the broker master", not "the
second option expiry".

Candles are resampled from broker 5-minute bars. The session clock is
09:15-15:30 IST. A 15-minute or 1-hour bucket that is still forming, or that
is missing a 5-minute bar, is not used. No third-party series.

This module does not raise the per-index decision cap and does not authorise
a new instrument.
"""
import datetime as dt

import index_entry_logic as iel

IST = iel.IST
WEEKLY = ("NIFTY", "SENSEX")
MONTHLY = ("BANKNIFTY",)
OPEN = dt.time(9, 15)
CLOSE = dt.time(15, 30)


def contract_policy_on():
    import os
    return os.environ.get("INDEX_CONTRACT_POLICY", "").strip() == "1"


def route(underlying, timeframe=None):
    """Default 1h. 15m must be asked for. 5m is refused. BANKNIFTY is 1h only."""
    text = (timeframe or "1h").strip().lower()
    if text in ("1h", "60", "60m"):
        minutes, name = 60, "1h"
    elif text in ("15", "15m"):
        minutes, name = 15, "15m"
    else:
        return {"ok": False, "reason": "timeframe superseded; 5m is not a fallback", "minutes": None, "name": None}
    if underlying in MONTHLY and minutes != 60:
        return {"ok": False, "reason": "BANKNIFTY is 1h and monthly expiry only", "minutes": None, "name": None}
    if underlying not in WEEKLY and underlying not in MONTHLY:
        return {"ok": False, "reason": f"{underlying} is not an index this entry path trades", "minutes": None, "name": None}
    return {"ok": True, "reason": None, "minutes": minutes, "name": name}


def _iso(day):
    return day.isocalendar()[:2]


def _next_iso(day):
    thursday = dt.date.fromisocalendar(day.isocalendar()[0], day.isocalendar()[1], 4)
    return (thursday + dt.timedelta(days=7)).isocalendar()[:2]


def _tag(raw):
    """Return (date, family). family is weekly, monthly, or None when the row does not say."""
    if isinstance(raw, dict):
        day = raw.get("expiry")
        if isinstance(day, dt.datetime):
            day = day.date()
        if raw.get("family") in ("weekly", "monthly"):
            return day, raw["family"]
        if raw.get("weekly") is True:
            return day, "weekly"
        if raw.get("weekly") is False:
            return day, "monthly"
        return day, None
    day = raw.date() if isinstance(raw, dt.datetime) else raw
    return day, None


def select_expiry(underlying, timeframe, session, option_expiries, monthly_expiries, trading_days=None):
    """Listed dates only. `monthly_expiries` are futures expiry dates from the master.

    A NIFTY or SENSEX date that is also a futures expiry is ambiguous unless the
    row says weekly or monthly. That date is not used as a weekly Friday-flatten
    contract or as a monthly hold. BANKNIFTY never takes a weekly row, and on its
    monthly expiry day it takes the NEXT listed monthly, never the one expiring today.
    """
    chosen = route(underlying, timeframe)
    if not chosen["ok"]:
        return {"ok": False, "reason": chosen["reason"], "expiry": None, "role": None}
    if isinstance(session, dt.datetime):
        session = session.date()
    monthly = set(monthly_expiries or [])
    rows = []
    for raw in option_expiries or []:
        day, family = _tag(raw)
        if day is None or day < session:
            continue
        if trading_days is not None and day not in trading_days:
            continue
        rows.append((day, family))
    rows.sort(key=lambda item: item[0])
    if underlying in MONTHLY:
        picked, skipped, same_day = [], [], []
        for day, family in rows:
            if family == "weekly":
                if day in monthly:
                    return {"ok": False, "reason": "BANKNIFTY row is both weekly and a futures expiry; family is ambiguous",
                            "expiry": None, "role": None}
                skipped.append(day.isoformat())
                continue
            if day in monthly or family == "monthly":
                # 12 Nov 2026: on the monthly expiry day itself the expiring contract is not
                # bought (it was, e.g. BANKNIFTY 27 Oct 2026); roll to the next listed monthly.
                if day <= session:
                    same_day.append(day.isoformat())
                    continue
                picked.append(day)
            else:
                skipped.append(day.isoformat())
        if not picked:
            return {"ok": False, "reason": "BANKNIFTY monthly expiry is not in the futures master; no weekly was invented"
                    + ("; the same-day monthly was not used" if same_day else ""),
                    "expiry": None, "role": None, "skipped_same_day": same_day}
        return {"ok": True, "reason": None, "expiry": picked[0], "role": "monthly",
                "timeframe": "1h", "family": "monthly", "skipped_non_monthly": skipped,
                "skipped_same_day": same_day}
    if not rows:
        return {"ok": False, "reason": "no listed option expiry on or after the session", "expiry": None, "role": None}
    # 12 Nov 2026 (t601u): NIFTY and SENSEX buy NEXT week's expiry. The exchange's monthly
    # contract IS that week's expiry in its last week (e.g. NIFTY 23 Nov 2026, the Monday before
    # the 24 Nov holiday), so a row the master explicitly tags monthly (weekly=False) is part of
    # the weekly sequence. A row with NO tag that falls on a futures date is still ambiguous.
    weeklies, ambiguous, fam = [], [], {}
    for day, family in rows:
        if family is None and day in monthly:
            ambiguous.append(day)
            continue
        weeklies.append(day)
        fam[day] = family or "weekly"
    if not weeklies:
        return {"ok": False, "reason": "no listed weekly expiry", "expiry": None, "role": None}
    current = weeklies[0]
    if any(day <= current for day in ambiguous):
        return {"ok": False, "reason": "expiry matches a futures monthly date and the row does not say weekly or monthly",
                "expiry": None, "role": None}
    if chosen["minutes"] == 15:
        return {"ok": True, "reason": None, "expiry": current, "role": "current_week",
                "timeframe": "15m", "family": fam[current], "coincides_monthly": fam[current] == "monthly"}
    want = _next_iso(current)
    if any(_iso(day) == want for day in ambiguous):
        return {"ok": False, "reason": "expiry matches a futures monthly date and the row does not say weekly or monthly",
                "expiry": None, "role": None}
    nxt = [d for d in weeklies if _iso(d) == want and d != current]
    if not nxt:
        return {"ok": False,
                "reason": "next-week expiry is not listed; current week and a later expiry were not substituted",
                "expiry": None, "role": None, "current_week": current.isoformat(), "wanted_iso_week": list(want)}
    return {"ok": True, "reason": None, "expiry": nxt[0], "role": "next_week", "timeframe": "1h",
            "family": fam[nxt[0]], "coincides_monthly": fam[nxt[0]] == "monthly", "current_week": current.isoformat()}


def _parse(ts):
    return iel.parse_ts(ts)


def resample(candles, minutes, now):
    """Build closed session buckets from 5-minute broker bars. Gaps fail the series."""
    if minutes not in (15, 60):
        return {"ok": False, "reason": "resample only builds 15m or 1h", "candles": []}
    step = minutes // 5
    rows = []
    for row in candles or []:
        ts = _parse(row[0])
        if ts.time() < OPEN or ts.time() >= CLOSE:
            continue
        rows.append((ts, row))
    rows.sort(key=lambda x: x[0])
    if not rows:
        return {"ok": False, "reason": "no session 5m bars to resample", "candles": []}
    by_start = {}
    for ts, row in rows:
        if (ts - dt.datetime.combine(ts.date(), OPEN, IST)).total_seconds() % 300:
            return {"ok": False, "reason": "5m bar is not aligned to the 09:15 IST grid", "candles": []}
        origin = dt.datetime.combine(ts.date(), OPEN, IST)
        slot = int((ts - origin).total_seconds() // 60)
        if slot % minutes:
            bucket = origin + dt.timedelta(minutes=slot - (slot % minutes))
        else:
            bucket = origin + dt.timedelta(minutes=slot)
        by_start.setdefault(bucket, []).append((ts, row))
    if getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=IST)

    def _can_close(start):
        """A bucket that would run past the 15:30 IST close is the short last interval, not a gap."""
        return start + dt.timedelta(minutes=minutes) <= dt.datetime.combine(start.date(), CLOSE, IST)

    def _complete(start):
        got = by_start.get(start) or []
        if len(got) != step:
            return False
        stamps = [t for t, _ in got]
        return all(stamps[i] == start + dt.timedelta(minutes=5 * i) for i in range(step))

    # Only fully closed session buckets. The short 15:15-15:30 IST interval, a
    # weekend, or a holiday with leftover ticks is skipped rather than treated
    # as a hole that invalidates the whole series. A missing hour on a trading
    # day still fails below.
    closed_starts = sorted(
        s for s in by_start
        if s + dt.timedelta(minutes=minutes) <= now and _can_close(s) and _complete(s)
    )
    if len(closed_starts) < 2:
        return {"ok": False, "reason": "fewer than two closed buckets", "candles": []}
    out = []
    for i, expected in enumerate(closed_starts):
        if i:
            prev = closed_starts[i - 1]
            if expected.date() == prev.date() and expected != prev + dt.timedelta(minutes=minutes):
                return {"ok": False, "reason": f"incomplete or gapped bucket {(prev + dt.timedelta(minutes=minutes)).isoformat()}", "candles": []}
        got = by_start[expected]
        o = got[0][1][1]
        h = max(r[2] for _, r in got)
        l = min(r[3] for _, r in got)
        c = got[-1][1][4]
        out.append([expected.isoformat(), o, h, l, c, 0, 0])
    # A bucket that has started but not closed must not be the last row.
    forming = [s for s in by_start if s + dt.timedelta(minutes=minutes) > now]
    return {"ok": True, "reason": None, "candles": out, "forming_ignored": [s.isoformat() for s in sorted(forming)]}


def dedupe(index, open_indexes, decisions_today):
    """One index, one decision. A 15m fill blocks a later 1h entry and the reverse."""
    if index in set(open_indexes or []):
        return {"ok": False, "reason": f"{index} already has an open option; the other timeframe is not a second trade"}
    for row in decisions_today or []:
        if row.get("index") == index:
            return {"ok": False, "reason": f"{index} already decided today on {row.get('timeframe')}; mixed 15m and 1h is one trade"}
    return {"ok": True, "reason": None}


def dedupe_broker(index, broker, open_indexes, decisions_today):
    """Per-broker form of dedupe (12 Nov 2026, t600u). A broker that already holds an option on
    this index is skipped; the other broker may still take a FRESH trigger. A decision row that
    names the brokers it was sent to blocks only those brokers; an older row with no "brokers"
    field blocks both (conservative). open_indexes=None means the book was unreadable -> refuse."""
    if open_indexes is None:
        return {"ok": False, "reason": f"{broker} positions unreadable; {index} entry refused on {broker}"}
    if index in set(open_indexes):
        return {"ok": False, "reason": f"{index} already has an open option on {broker}; adds are full_auto_add's job"}
    for row in decisions_today or []:
        if row.get("index") != index:
            continue
        brokers = row.get("brokers")
        if brokers is None or broker in brokers:
            return {"ok": False, "reason": f"{index} already decided today on {row.get('timeframe')} for {broker}; mixed 15m and 1h is one trade"}
    return {"ok": True, "reason": None}


def metadata(underlying, timeframe_name, expiry, role, signal_key):
    """Shared fields for both broker orders. Quantity is not in here."""
    return {
        "_signal_source": "futures",
        "_signal_instrument_key": signal_key,
        "_timeframe": timeframe_name,
        "_option_expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else expiry,
        "_expiry_role": role,
        "_entry_index": underlying,
        "strike_source": "spot",
    }
