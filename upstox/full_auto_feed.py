"""Read-only market data helpers for the full-auto rules (12 Nov 2026). Places nothing.

fut_key(u)          nearest listed future (expiry >= today) from the newest local Upstox master.
bar_1h(key)         last COMPLETED 1h bar {ts, close, ema20, dir, st} via ind.analyze (forming bar dropped).
bar_15m(key)        last COMPLETED 15m bar, same shape as bar_1h (forming bar dropped).
underlying_1h(sym)  the 1h bar that governs an option: index -> its future (spot only if no future),
                    CRUDEOIL -> MCX future, stock -> NSE cash.
"""
import datetime, glob, os, sys

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
INDEX_EXCH = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "SENSEX": "BSE"}
SPOT = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank", "SENSEX": "BSE_INDEX|SENSEX"}
_master = {}


def _rows(exch):
    if exch in _master:
        return _master[exch]
    files = sorted(glob.glob(os.path.join(D, "instruments_%s_*.json.gz" % exch)))
    if not files:
        _master[exch] = []
        return []
    import api
    _master[exch] = api.gzip_json(files[-1])
    return _master[exch]


def root(sym):
    s = (sym or "").upper().replace(" ", "")
    i = 0
    while i < len(s) and (s[i].isalpha() or s[i] == "&"):
        i += 1
    return s[:i]


def fut_key(underlying, today=None, rows=None):
    u = underlying.upper()
    exch = "MCX" if u.startswith("CRUDEOIL") else INDEX_EXCH.get(u, "NSE")
    rows = _rows(exch) if rows is None else rows
    today = today or datetime.datetime.now(IST).date()
    best = None
    for r in rows:
        if r.get("instrument_type") != "FUT" or (r.get("underlying_symbol") or "") != u:
            continue
        exp = datetime.datetime.fromtimestamp(r["expiry"] / 1000, IST).date()
        if exp < today:
            continue
        if best is None or exp < best[0]:
            best = (exp, r["instrument_key"])
    return best[1] if best else None


def _completed_bar(key, unit, interval, days=20):
    import ind
    a = ind.analyze(key, unit, interval, days)
    return {"ts": a["last_ts"], "close": a["close"], "ema20": a["ema20"], "dir": a["dir"],
            "st": a["st"], "prev_close": a["prev_close"], "prev_ema": a["prev_ema"], "prev_dir": a["prev_dir"],
            "key": key}


def bar_1h(key):
    return _completed_bar(key, "hours", 1, 20)


def bar_15m(key):
    return _completed_bar(key, "minutes", 15, 20)


def underlying_key(sym):
    u = root(sym)
    if u in SPOT:
        return fut_key(u) or SPOT[u], u
    if u.startswith("CRUDEOIL"):
        return fut_key("CRUDEOIL"), "CRUDEOIL"
    try:
        import api
        k = api.file_json(os.path.join(D, "eq_keys.json")).get(u)
    except Exception:
        k = None
    return k, u


def underlying_1h(sym):
    key, u = underlying_key(sym)
    if not key:
        return None, "no underlying key for %s" % u
    try:
        return bar_1h(key), None
    except Exception as e:
        return None, "1h candles unreadable for %s (%s)" % (key, type(e).__name__)
