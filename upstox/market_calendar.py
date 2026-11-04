#!/usr/bin/env python3
"""NSE/BSE trading-day calendar with automatic yearly refresh.

Data: holidays.json (flat {"YYYY-MM-DD": "reason"}) covering every year we have.
Every year the list is refreshed from NSE's holiday master:
  * ensure_year(y)  - called by is_trading_day(); if year y is missing it tries
                      to fetch once (rate-limited to one attempt per 6h, 12s timeout).
  * refresh(y=None) - force a fetch; y defaults to current year, and if NSE
                      already publishes next year it is picked up too.
  * coverage()      - which years are stored, and which are missing.
A failed fetch never blocks trading: the day is then judged on weekday only.
CLI: python3 market_calendar.py [next|check YYYY-MM-DD|refresh [YEAR]|coverage]"""
import json, os, datetime, urllib.request

D = os.path.dirname(os.path.abspath(__file__))
F = os.path.join(D, "holidays.json")
OLD_F = os.path.join(D, "holidays_2026.json")
STATE_F = os.path.join(D, "holidays_state.json")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NSE_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
ATTEMPT_GAP = 6 * 3600  # seconds between failed auto-refresh attempts

# Static fallback, used only if holidays.json is missing (NSE CM master).
_H2026 = {
 "2026-01-15":"Municipal Corporation Election - Maharashtra",
 "2026-01-26":"Republic Day","2026-02-15":"Mahashivratri","2026-03-03":"Holi",
 "2026-03-21":"Id-Ul-Fitr (Ramadan Eid)","2026-03-26":"Shri Ram Navami",
 "2026-03-31":"Shri Mahavir Jayanti","2026-04-03":"Good Friday",
 "2026-04-14":"Dr. Baba Saheb Ambedkar Jayanti","2026-05-01":"Maharashtra Day",
 "2026-05-28":"Bakri Id","2026-06-26":"Muharram","2026-08-15":"Independence Day",
 "2026-09-14":"Ganesh Chaturthi","2026-10-02":"Mahatma Gandhi Jayanti",
 "2026-10-20":"Dussehra","2026-11-08":"Diwali Laxmi Pujan",
 "2026-11-10":"Diwali-Balipratipada","2026-11-24":"Prakash Gurpurb Sri Guru Nanak Dev",
 "2026-12-25":"Christmas",
}
_FALLBACK = _H2026


def _load():
    for path in (F, OLD_F):
        if os.path.exists(path):
            try:
                d = json.load(open(path))
                if isinstance(d, dict) and d:
                    return dict(d)
            except Exception:
                pass
    return dict(_FALLBACK)


def _save(d):
    tmp = F + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1, sort_keys=True)
    os.replace(tmp, F)


def _state():
    try:
        return json.load(open(STATE_F))
    except Exception:
        return {}


def _set_state(**kw):
    s = _state(); s.update(kw)
    try:
        json.dump(s, open(STATE_F, "w"), indent=1)
    except Exception:
        pass


def holidays():
    return _load()


def _fetch(year, timeout=20):
    """Return {iso date: reason} for `year` from NSE, or None if unavailable."""
    url = NSE_URL if year is None else "%s&year=%d" % (NSE_URL, year)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    out = {}
    for x in d.get("CM", []):
        try:
            dt = datetime.datetime.strptime(x["tradingDate"], "%d-%b-%Y").date()
        except Exception:
            continue
        if year is None or dt.year == year:
            out[dt.isoformat()] = (x.get("description") or "").rstrip("*").strip()
    return out or None


def refresh(year=None, timeout=20):
    """Merge NSE's list for `year` (default: this year) into holidays.json.
    Also pulls next year when NSE already publishes it. Keeps old years intact.
    Returns a dict of {year: count} for the years actually written."""
    year = year or datetime.datetime.now(IST).year
    cur = _load()
    written, errors = {}, []
    for y in (year, year + 1):
        try:
            got = _fetch(y, timeout=timeout)
        except Exception as e:
            errors.append("%s: %s" % (y, type(e).__name__))
            got = None
        if not got:
            continue
        # replace that year's entries, keep every other year untouched
        cur = {k: v for k, v in cur.items() if not k.startswith("%d-" % y)}
        cur.update(got)
        written[y] = len(got)
    now = datetime.datetime.now(IST).isoformat(timespec="seconds")
    if written:
        _save(cur)
    _set_state(last_attempt=now, last_ok=(now if written else _state().get("last_ok")),
               last_written=written, last_errors=errors)
    return written


def coverage():
    h = _load()
    years = sorted({k[:4] for k in h})
    return years


STALE_DAYS = 60


def _age_days(stamp):
    try:
        return (datetime.datetime.now(IST) - datetime.datetime.fromisoformat(stamp)).total_seconds() / 86400
    except Exception:
        return 1e9


def ensure_year(year=None):
    """Make sure `year` is in the cache and fresh. Fetch at most once per ATTEMPT_GAP."""
    year = year or datetime.datetime.now(IST).year
    have = any(k.startswith("%d-" % year) for k in _load())
    if have and _age_days(_state().get("last_ok") or "") < STALE_DAYS:
        return True
    s = _state()
    last = s.get("last_attempt")
    if last:
        try:
            age = (datetime.datetime.now(IST) - datetime.datetime.fromisoformat(last)).total_seconds()
            if age < ATTEMPT_GAP:
                return False
        except Exception:
            pass
    try:
        return year in refresh(year, timeout=12) or have
    except Exception:
        return have


def is_trading_day(d=None):
    d = d or datetime.datetime.now(IST).date()
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    ensure_year(d.year)
    return d.weekday() < 5 and d.isoformat() not in _load()


def next_trading_day(d=None):
    d = d or datetime.datetime.now(IST).date()
    for _ in range(15):
        d = d + datetime.timedelta(days=1)
        if is_trading_day(d):
            return d
    return d


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "next":
        print(next_trading_day())
    elif len(a) > 1 and a[0] == "check":
        print(is_trading_day(a[1]), _load().get(a[1], ""))
    elif a and a[0] == "refresh":
        y = int(a[1]) if len(a) > 1 else None
        w = refresh(y)
        print("written", w, "| years in cache", coverage())
    elif a and a[0] == "coverage":
        print("years in cache:", coverage(), "| state:", json.dumps(_state()))
    else:
        d = datetime.datetime.now(IST).date()
        print("today", d, "trading" if is_trading_day(d) else "HOLIDAY/closed", _load().get(d.isoformat(), ""))
        print("next trading day", next_trading_day(d))
        print("years in cache", coverage())
