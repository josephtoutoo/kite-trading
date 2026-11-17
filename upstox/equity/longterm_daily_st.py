#!/usr/bin/env python3
"""Long-term daily Supertrend MTF scanner (t692u / t693u, 16 Nov 2026 IST).

READ ONLY by default. Places NO orders. Does not import auto_entry or risk_manager.

Defaults (also printed in every report):
  * Daily timeframe, completed session only. A bar dated today is used only after
    15:30 Asia/Calcutta; before that the previous completed daily bar is the last one.
  * Supertrend(10, 3) — the repository default in ind.py.
  * Bullish: last close is strictly above Supertrend (ST is green).
  * 'above_both': ST value is strictly above both SMA50 and SMA150.
  * 'between': ST value sits between SMA50 and SMA150, inclusive of the edges
    (and is not already classified as above_both).
  * Rank: above_both first, then between; within a tier, higher ADX, then less
    extended ((close-ST)/ST), then symbol A-Z.
  * These are MTF delivery longs on BOTH brokers (Upstox and Kite).
  * Capital is Rs 50,000 per stock per broker (t693u), not Rs 50,000 total.
    At most 3 concurrent long-term MTF names, so up to Rs 1,50,000 margin
    per broker. Quantity is the largest whole-share size whose estimated (or
    live-quoted) MTF margin is at most Rs 50,000. If one share needs more than
    Rs 50,000, the name is flagged and not auto-sized.
  * Suggested initial stop: the daily Supertrend value. Existing MTF stop-trail
    routines would manage the stop after an approved entry.
  * Universe: Nifty 500 (universe.csv) intersect Upstox-MTF intersect Kite-MTF.
  * Exclude names with a results date in the next 7 calendar days when a
    calendar file is present, and names with 20-session average traded value
    below Rs 2 crore.
  * The scanner itself never places orders. Live entry is equity/longterm_daily_st_entry.py,
    armed only when LONGTERM_DST_EXECUTE=1 and --execute is passed, and only inside the
    09:20-15:00 IST window on an NSE trading day from Tue 17 Nov 2026. A live wrapper
    (equity/longterm_daily_st_live.sh) sets the env flag for that process only.
  * After a fill, a protective SL/SL-M (or GTT if a day SL cannot be placed) is set at the
    daily Supertrend. kite-mtf-stop-trail and upstox-mtf-stop-trail then trail that stop
    to the completed daily Supertrend and never loosen it. These are delivery MTF positions,
    not intraday, and must be skipped by any same-day square-off.
  * Tag longterm_dst is exempt from the Monday 9 Nov MTF caps (max 5 names, 10% per stock,
    25% pool). This sleeve's own cap is max 3 names and Rs 50,000 margin per name per broker.

This is a separate sleeve from the Monday 9 Nov 2026 MTF policy in mtf_policy.py
(max 5 names, 25% shared pool, 10% per stock). Tag longterm_dst is exempt from
those caps only; the sleeve enforces its own 3-name / Rs 50k-per-name limits.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.parse
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EQ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, EQ)

import api  # noqa: E402
import ind  # noqa: E402

IST = ZoneInfo("Asia/Calcutta")
EXECUTE_ENABLED = False  # scanner never places; live path is longterm_daily_st_entry.py
TAG = "longterm_dst"
EXECUTE_ENV = "LONGTERM_DST_EXECUTE"
GAP_MAX_PCT = 3.0
FIRST_ENTRY_DATE = dt.date(2026, 11, 17)
ENTRY_START = dt.time(9, 20)
ENTRY_END = dt.time(15, 0)
LIMIT_BUFFER_PCT = 0.0015
LIMIT_MIN_TICKS = 4

ST_PERIOD = 10
ST_MULT = 3
SMA_FAST = 50
SMA_SLOW = 150
MIN_DAILY_BARS = 200
ADV_DAYS = 20
ADV_MIN = 20_000_000  # Rs 2 crore
CAPITAL_PER_STOCK = 50_000
MAX_TRADES = 3
RESULTS_WINDOW_DAYS = 7
SESSION_CLOSE = dt.time(15, 30)

UNIVERSE_CSV = "/workspace/universe.csv"
UPSTOX_MTF_DEFAULT = os.path.join(ROOT, "mtf_instruments_2026-11-04.json.gz")
KITE_MTF_URL = "https://public.zrd.sh/crux/approved-mtf-securities.json"
KITE_MTF_CACHE = os.path.join(EQ, "logs", "kite_mtf_securities_2026-11-16.json")
RESULTS_CSV = "/workspace/results_universe.csv"
STOCKOPT_CAL = os.path.join(ROOT, "stockopt_calendar.json")
CACHE_DIR = os.path.join(EQ, "logs", "longterm_daily_st_cache")
OUT_DIR = os.path.join(EQ, "logs", "longterm_daily_st")
STATE_PATH = os.path.join(OUT_DIR, "positions.json")
APPROVED_PATH = os.path.join(OUT_DIR, "approved_2026-11-16.json")
MANAGED_PATHS = (
    os.path.join(EQ, "mtf_managed.json"),
    "/workspace/equity/mtf_managed.json",
)


# --------------- pure helpers (unit-tested) ---------------
def sma(values, n):
    """Simple moving average series; leading n-1 entries are None."""
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    total = sum(values[:n])
    out[n - 1] = total / n
    for i in range(n, len(values)):
        total += values[i] - values[i - n]
        out[i] = total / n
    return out


def classify_st(close, st, sma50, sma150):
    """Return condition name or None if the bar is not a long-term candidate."""
    if close is None or st is None or sma50 is None or sma150 is None:
        return None
    if close <= st:
        return None
    lo, hi = (sma50, sma150) if sma50 <= sma150 else (sma150, sma50)
    if st > hi:
        return "above_both"
    if lo <= st <= hi:
        return "between"
    return None


def rank_key(row):
    """Sort key: above_both first, then higher ADX, then less extended, then symbol."""
    tier = 0 if row.get("condition") == "above_both" else 1
    adx = row.get("adx")
    adx_sort = -(float(adx) if adx is not None else -1e9)
    ext = row.get("extension_pct")
    ext_sort = float(ext) if ext is not None else 1e9
    return (tier, adx_sort, ext_sort, row.get("symbol") or "")


def parse_results_date(text):
    """Parse a results calendar date. Returns a date or None."""
    if not text:
        return None
    s = str(text).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def results_exclusions(rows, today, window_days=RESULTS_WINDOW_DAYS):
    """symbol -> results date for names whose results fall in [today, today+window]."""
    end = today + dt.timedelta(days=int(window_days))
    out = {}
    for symbol, raw in rows:
        day = parse_results_date(raw)
        if day is None:
            continue
        if today <= day <= end:
            out[symbol] = day
    return out


def size_qty(capital, price, margin_rate):
    """Largest whole-share qty whose margin (price * qty * rate) is <= capital.

    Returns (qty, est_margin, est_notional, leverage, note). qty is 0 when
    one share already exceeds capital or inputs are invalid.
    """
    try:
        capital = float(capital)
        price = float(price)
        margin_rate = float(margin_rate)
    except (TypeError, ValueError):
        return 0, None, None, None, "invalid inputs"
    if capital <= 0 or price <= 0 or margin_rate <= 0:
        return 0, None, None, None, "invalid inputs"
    margin_one = price * margin_rate
    leverage = 1.0 / margin_rate
    if margin_one > capital + 1e-9:
        return 0, margin_one, price, leverage, "one share margin exceeds Rs 50k capital"
    qty = int(capital // margin_one)
    if qty < 1:
        return 0, margin_one, price, leverage, "one share margin exceeds Rs 50k capital"
    est_margin = qty * margin_one
    est_notional = qty * price
    return qty, est_margin, est_notional, leverage, "ok"




def execute_flag_on(env=None):
    """True only when LONGTERM_DST_EXECUTE is an explicit on-value."""
    env = os.environ if env is None else env
    return str((env or {}).get(EXECUTE_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def is_longterm_dst_squareoff_excluded(tag=None, product=None, symbol=None):
    """Delivery MTF in this sleeve must never be flattened by an intraday square-off."""
    if (tag or "").strip() == TAG:
        return True
    if symbol and symbol_in_sleeve(symbol):
        return True
    return False


def tick_for_price(price):
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.05
    return 0.10 if p >= 1000 else 0.05


def snap_price(price, tick=None):
    t = tick or tick_for_price(price)
    return round(round(float(price) / t) * t, 2)


def limit_buy_price(ltp, buffer_pct=LIMIT_BUFFER_PCT, min_ticks=LIMIT_MIN_TICKS):
    """Marketable LIMIT a small buffer above LTP, snapped to the cash tick."""
    t = tick_for_price(ltp)
    buf = max(float(ltp) * float(buffer_pct), int(min_ticks) * t)
    return snap_price(float(ltp) + buf, t)


def entry_window(now, trading_day=None):
    """(ok, reason) for the 17 Nov+ 09:20-15:00 IST trading-day window."""
    now = now or dt.datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    day = now.date()
    if day < FIRST_ENTRY_DATE:
        return False, "first entry is Tue 17 Nov 2026 after 09:20 IST"
    if trading_day is None:
        try:
            import market_calendar as mc
            trading_day = mc.is_trading_day(day)
        except Exception:
            trading_day = day.weekday() < 5
    if not trading_day:
        return False, "not an NSE trading day"
    t = now.time()
    if t < ENTRY_START:
        return False, "before 09:20 IST entry window"
    if t >= ENTRY_END:
        return False, "after 15:00 IST entry window (leaves time to place Supertrend stops)"
    return True, "in window"


def upside_gap_pct(ltp, ref_close):
    try:
        px, ref = float(ltp), float(ref_close)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or px <= 0:
        return None
    return (px - ref) / ref * 100.0


def gap_skip(ltp, ref_close, max_pct=GAP_MAX_PCT):
    """Skip when LTP is more than max_pct above the reference (16 Nov) close."""
    g = upside_gap_pct(ltp, ref_close)
    if g is None:
        return True, "LTP or reference close unreadable"
    if g > float(max_pct) + 1e-12:
        return True, "gapped %.2f%% above the 16 Nov close (limit %.1f%%)" % (g, float(max_pct))
    return False, None


def setup_holds(row):
    """True when the completed daily bar still meets the long-term ST rule."""
    return classify_st(row.get("close"), row.get("st") or row.get("stop"),
                       row.get("sma50"), row.get("sma150")) in ("above_both", "between")


def already_held(held_symbols, symbol):
    return (symbol or "").upper() in {str(s).upper() for s in (held_symbols or [])}


def sleeve_name_count(held_sleeve, symbol, already=False):
    names = {str(s).upper() for s in (held_sleeve or [])}
    if symbol and not already:
        names.add(str(symbol).upper())
    return len(names)


def longterm_dst_blocks(held_sleeve, symbol, margin, already_held_name=False,
                        capital=CAPITAL_PER_STOCK, max_names=MAX_TRADES):
    """This sleeve's own cap: max 3 names, Rs 50k margin, no double-buy."""
    blocks = []
    if already_held_name:
        blocks.append("longterm_dst: already held — not double-buying %s" % symbol)
    try:
        margin = float(margin)
    except (TypeError, ValueError):
        blocks.append("longterm_dst: live MTF margin unreadable")
        margin = None
    if margin is not None and margin > float(capital) + 1e-6:
        blocks.append("longterm_dst: live MTF margin Rs %.2f exceeds Rs %s cap"
                      % (margin, f"{int(capital):,}"))
    if not already_held_name:
        n = sleeve_name_count(held_sleeve, symbol, already=False)
        if n > int(max_names):
            blocks.append("longterm_dst: max %d names in this sleeve" % int(max_names))
    return blocks


def longterm_dst_trail_plan(ltp, daily_st, existing_trigger=None, floor_stop=None):
    """Trail a long MTF stop up to the daily Supertrend; never loosen; never at/above LTP."""
    try:
        price = float(ltp)
    except (TypeError, ValueError):
        return {"action": "HOLD", "place_stop": False, "trigger": existing_trigger,
                "source": "none", "reason": "price unreadable; existing stop is not cancelled"}
    st = None
    if daily_st not in (None, ""):
        try:
            st = float(daily_st)
        except (TypeError, ValueError):
            st = None
    have = None
    for v in (existing_trigger, floor_stop):
        if v in (None, ""):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        have = fv if have is None else max(have, fv)
    if st is None or st >= price:
        if have is not None and have < price:
            return {"action": "KEEP", "place_stop": False, "trigger": have,
                    "source": "existing",
                    "reason": "daily Supertrend unusable; existing stop kept; not loosened"}
        return {"action": "HOLD", "place_stop": False, "trigger": have,
                "source": "daily_supertrend",
                "reason": "daily Supertrend is not below price; no invalid stop placed"}
    candidate = st
    if have is not None and have < price:
        if candidate <= have + 1e-9:
            return {"action": "KEEP", "place_stop": False, "trigger": have,
                    "source": "existing",
                    "reason": "daily Supertrend would loosen the stop; not moved"}
        return {"action": "RATCHET", "place_stop": True, "trigger": candidate,
                "source": "daily_supertrend",
                "reason": "trail up to daily Supertrend"}
    return {"action": "PLACE", "place_stop": True, "trigger": candidate,
            "source": "daily_supertrend",
            "reason": "initial protective stop at daily Supertrend"}


def load_sleeve_state(path=None):
    path = path or STATE_PATH
    try:
        data = json.load(open(path))
    except FileNotFoundError:
        return {"positions": {"upstox": {}, "kite": {}}}
    except Exception:
        return {"positions": {"upstox": {}, "kite": {}}}
    pos = data.get("positions") or {}
    return {"positions": {
        "upstox": dict(pos.get("upstox") or {}),
        "kite": dict(pos.get("kite") or {}),
    }, "raw": data}


def sleeve_symbols(broker=None, path=None):
    st = load_sleeve_state(path)
    if broker:
        return {str(s).upper() for s in (st["positions"].get(broker) or {})}
    out = set()
    for b in ("upstox", "kite"):
        out.update(str(s).upper() for s in (st["positions"].get(b) or {}))
    return out


def symbol_in_sleeve(symbol, path=None):
    return (symbol or "").upper() in sleeve_symbols(path=path)


def session_cut(now=None):
    """Last daily bar timestamp that is complete as of `now` (IST)."""
    now = now or dt.datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    today = now.date()
    close_at = dt.datetime.combine(today, SESSION_CLOSE, tzinfo=IST)
    if now >= close_at:
        return dt.datetime.combine(today, dt.time(15, 30), tzinfo=IST)
    return dt.datetime.combine(today - dt.timedelta(days=1), dt.time(15, 30), tzinfo=IST)


def avg_traded_value(candles, n=ADV_DAYS):
    """Mean of close*volume over the last n bars. None if not enough bars."""
    if not candles or len(candles) < n:
        return None
    vals = []
    for row in candles[-n:]:
        close = float(row[4])
        vol = float(row[5] or 0)
        vals.append(close * vol)
    return sum(vals) / len(vals)


# --------------- IO helpers ---------------
def _sanitize(msg):
    import re
    s = str(msg)
    s = re.sub(r"https?://[^\s'\"]+", "<url>", s)
    s = re.sub(r"(?i)(token|secret|password|api[_-]?key|authorization|bearer|proxy)[=: ]+\S+",
               r"\1=<redacted>", s)
    s = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", s)
    return s


def load_nifty500(path=UNIVERSE_CSV):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            sym = (r.get("Symbol") or "").strip().upper()
            if not sym:
                continue
            rows.append({
                "symbol": sym,
                "name": (r.get("Company Name") or "").strip(),
                "industry": (r.get("Industry") or "").strip(),
                "isin": (r.get("ISIN Code") or "").strip(),
                "idx": (r.get("idx") or "").strip(),
            })
    return rows


def load_upstox_mtf(path=UPSTOX_MTF_DEFAULT):
    with gzip.open(path, "rt") as f:
        data = json.load(f)
    out = {}
    for row in data:
        if not row.get("mtf_enabled"):
            continue
        if row.get("segment") and row.get("segment") != "NSE_EQ":
            continue
        sym = (row.get("trading_symbol") or "").replace("-EQ", "").upper()
        if not sym:
            continue
        bracket = row.get("mtf_bracket")
        try:
            rate = float(bracket) / 100.0 if bracket is not None else None
        except (TypeError, ValueError):
            rate = None
        out[sym] = {
            "instrument_key": row.get("instrument_key"),
            "mtf_bracket": bracket,
            "margin_rate": rate,
            "leverage": (1.0 / rate) if rate else None,
            "isin": row.get("isin"),
            "source": os.path.basename(path),
        }
    return out


def load_kite_mtf(path=None):
    path = path or KITE_MTF_CACHE
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("data") or data.get("securities") or []
    out = {}
    for row in data:
        sym = (row.get("tradingsymbol") or row.get("trading_symbol") or "").upper()
        if not sym:
            continue
        margin = row.get("margin")
        try:
            rate = float(margin) / 100.0 if margin is not None else None
        except (TypeError, ValueError):
            rate = None
        lev = row.get("leverage")
        try:
            lev = float(lev) if lev is not None else ((1.0 / rate) if rate else None)
        except (TypeError, ValueError):
            lev = (1.0 / rate) if rate else None
        out[sym] = {
            "margin_pct": margin,
            "margin_rate": rate,
            "leverage": lev,
            "isin": row.get("isin"),
            "category": row.get("category"),
            "source": os.path.basename(path),
        }
    return out


def refresh_kite_mtf(dest=KITE_MTF_CACHE, timeout=30):
    """Refresh the public Zerodha MTF list. Never logs the URL with credentials."""
    import ssl
    import urllib.request
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    ctx = ssl.create_default_context()
    req = urllib.request.Request(KITE_MTF_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) < 100:
        raise RuntimeError("unexpected Kite MTF list shape")
    with open(dest, "wb") as f:
        f.write(raw)
    return dest, len(data)


def load_results_rows():
    rows = []
    if os.path.exists(RESULTS_CSV):
        with open(RESULTS_CSV, newline="") as f:
            for r in csv.DictReader(f):
                sym = (r.get("symbol") or r.get("Symbol") or "").strip().upper()
                raw = r.get("date") or r.get("results_date") or ""
                if sym:
                    rows.append((sym, raw))
    if os.path.exists(STOCKOPT_CAL):
        try:
            cal = json.load(open(STOCKOPT_CAL))
        except (OSError, json.JSONDecodeError):
            cal = {}
        if isinstance(cal, dict):
            for sym, rec in cal.items():
                if isinstance(rec, dict):
                    rows.append((str(sym).upper(), rec.get("expected_results_date") or rec.get("results_date")))
    return rows


def keymap():
    """trading_symbol -> instrument_key from the newest NSE equity master."""
    import glob
    files = sorted(glob.glob(os.path.join(ROOT, "instruments_NSE_*.json.gz")))
    if not files:
        raise RuntimeError("no NSE instrument master under %s" % ROOT)
    path = files[-1]
    with gzip.open(path, "rt") as f:
        data = json.load(f)
    km = {}
    for row in data:
        if row.get("segment") != "NSE_EQ":
            continue
        if row.get("instrument_type") not in (None, "EQ"):
            continue
        sym = (row.get("trading_symbol") or "").replace("-EQ", "").upper()
        key = row.get("instrument_key")
        if sym and key:
            km[sym] = key
    return km, os.path.basename(path)


def _bar_date(c):
    ts = dt.datetime.fromisoformat(c[0])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    else:
        ts = ts.astimezone(IST)
    return ts


def _completed_daily(cs, now):
    """Keep completed daily bars only; today's bar is kept after 15:30 IST."""
    cut = session_cut(now)
    by = {}
    for c in cs or []:
        ts = _bar_date(c)
        if ts.date() <= cut.date():
            by[ts.date()] = c
    if now < dt.datetime.combine(now.date(), SESSION_CLOSE, tzinfo=IST):
        by.pop(now.date(), None)
    return [by[d] for d in sorted(by)]


def fetch_intraday_daily(key):
    """Today's daily bar lives on the intraday endpoint until the master rolls."""
    k = urllib.parse.quote(key, safe="")
    last_err = None
    for attempt in range(4):
        try:
            r = api.get(f"/v3/historical-candle/intraday/{k}/days/1")
            return r.get("data", {}).get("candles") or []
        except Exception as e:
            last_err = e
            time.sleep(0.4 * (2 ** attempt))
    raise last_err


def fetch_daily_candles(key, days=400, now=None, cache_dir=CACHE_DIR):
    """Completed daily candles for `key`, including today's bar after the close.

    Upstox /v3/historical-candle/.../days/1/{to} does not include the current
    session even after 15:30; that bar is on the intraday daily endpoint and is
    merged here (same pattern as ind.candles).
    """
    now = now or dt.datetime.now(IST)
    cut = session_cut(now)
    to = cut.date().isoformat()
    fr = (cut.date() - dt.timedelta(days=int(days))).isoformat()
    safe = key.replace("|", "_").replace("/", "_")
    os.makedirs(cache_dir, exist_ok=True)
    # v2 cache includes the merged today bar
    cache_path = os.path.join(cache_dir, f"{safe}_{to}_v2.json")
    hist_cache = os.path.join(cache_dir, f"{safe}_{to}.json")
    if os.path.exists(cache_path):
        try:
            cs = json.load(open(cache_path))
            if isinstance(cs, list) and cs:
                return _completed_daily(cs, now)
        except (OSError, json.JSONDecodeError):
            pass
    hist = None
    if os.path.exists(hist_cache):
        try:
            hist = json.load(open(hist_cache))
        except (OSError, json.JSONDecodeError):
            hist = None
    k = urllib.parse.quote(key, safe="")
    last_err = None
    if not hist:
        for attempt in range(4):
            try:
                r = api.get(f"/v3/historical-candle/{k}/days/1/{to}/{fr}")
                hist = r.get("data", {}).get("candles") or []
                break
            except Exception as e:
                last_err = e
                time.sleep(0.4 * (2 ** attempt))
        else:
            raise last_err
    intra = []
    try:
        intra = fetch_intraday_daily(key)
    except Exception as e:
        # After the close a missing today bar is a real gap; surface it.
        if now >= dt.datetime.combine(now.date(), SESSION_CLOSE, tzinfo=IST):
            raise
        intra = []
    kept = _completed_daily(list(hist) + list(intra), now)
    with open(cache_path, "w") as f:
        json.dump(kept, f)
    return kept


def analyze_daily(candles):
    """Indicators on completed daily bars. Returns None if the series is too short."""
    if not candles or len(candles) < MIN_DAILY_BARS:
        return {"ok": False, "reason": "fewer than %d daily bars" % MIN_DAILY_BARS,
                "n_bars": len(candles) if candles else 0}
    h = [float(x[2]) for x in candles]
    l = [float(x[3]) for x in candles]
    c = [float(x[4]) for x in candles]
    st_series, d_series = ind.supertrend(h, l, c, ST_PERIOD, ST_MULT)
    s50 = sma(c, SMA_FAST)
    s150 = sma(c, SMA_SLOW)
    pdi, mdi, adx_series = ind.dmi(h, l, c)
    close = c[-1]
    st = st_series[-1]
    sma50 = s50[-1]
    sma150 = s150[-1]
    adx = adx_series[-1] if adx_series else None
    condition = classify_st(close, st, sma50, sma150)
    ext = ((close - st) / st * 100.0) if st else None
    last_ts = candles[-1][0]
    adv = avg_traded_value(candles, ADV_DAYS)
    return {
        "ok": True,
        "n_bars": len(candles),
        "last_ts": last_ts,
        "close": close,
        "st": st,
        "st_dir": d_series[-1] if d_series else None,
        "sma50": sma50,
        "sma150": sma150,
        "adx": adx,
        "pdi": pdi[-1] if pdi else None,
        "mdi": mdi[-1] if mdi else None,
        "condition": condition,
        "extension_pct": ext,
        "adv_20": adv,
        "liquid": (adv is not None and adv >= ADV_MIN),
    }


def live_book():
    """Current MTF holdings and free margin on both brokers. Read only."""
    import mtf_sizing as ms
    out = {"upstox": None, "kite": None, "errors": {}}
    try:
        st = ms.upstox_state()
        out["upstox"] = {
            "capital": st["capital"],
            "capital_basis": st.get("capital_basis"),
            "free": st["free"],
            "used_cost": st["used"],
            "held": dict(st["held"]),
            "qty": dict(st.get("qty") or {}),
            "mine": sorted(st.get("mine") or []),
        }
    except Exception as e:
        out["errors"]["upstox"] = "%s: %s" % (type(e).__name__, _sanitize(e))
    try:
        st = ms.kite_state()
        out["kite"] = {
            "capital": st["capital"],
            "capital_basis": st.get("capital_basis"),
            "free": st["free"],
            "used_cost": st["used"],
            "held": dict(st["held"]),
            "qty": dict(st.get("qty") or {}),
            "mine": sorted(st.get("mine") or []),
        }
    except Exception as e:
        out["errors"]["kite"] = "%s: %s" % (type(e).__name__, _sanitize(e))
    return out


def margin_quotes(symbol, qty, price):
    """Live MTF margin quotes. Read only — does not place an order."""
    import mtf_sizing as ms
    out = {}
    for broker in ("upstox", "kite"):
        try:
            q = ms.margin_quote(broker, symbol, qty, price)
            if q.get("ok"):
                margin = float(q["margin"])
                notional = float(q.get("notional") or (qty * price))
                rate = (margin / notional) if notional else None
                out[broker] = {
                    "ok": True,
                    "margin": margin,
                    "notional": notional,
                    "margin_rate": rate,
                    "leverage": (1.0 / rate) if rate else q.get("leverage"),
                    "quoted_leverage": q.get("leverage"),
                }
            else:
                out[broker] = {"ok": False, "why": _sanitize(q.get("why") or "no quote")}
        except Exception as e:
            out[broker] = {"ok": False, "why": "%s: %s" % (type(e).__name__, _sanitize(e))}
    return out


def r2(x, n=2):
    if x is None:
        return None
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def fmt_inr(x):
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 100:
        return "%s₹%s" % (sign, f"{v:,.0f}")
    return "%s₹%s" % (sign, f"{v:,.2f}")


# --------------- scan ---------------
def build_universe(upstox_mtf, kite_mtf, nifty):
    both = []
    up_only = []
    kite_only = []
    neither = []
    for row in nifty:
        sym = row["symbol"]
        u = sym in upstox_mtf
        k = sym in kite_mtf
        item = dict(row)
        if u:
            item["upstox_mtf"] = upstox_mtf[sym]
        if k:
            item["kite_mtf"] = kite_mtf[sym]
        if u and k:
            both.append(item)
        elif u:
            up_only.append(sym)
        elif k:
            kite_only.append(sym)
        else:
            neither.append(sym)
    return both, up_only, kite_only, neither


def scan_symbol(row, km, now, exclusions):
    sym = row["symbol"]
    if sym in exclusions:
        return {"symbol": sym, "ok": False, "skip": "results %s" % exclusions[sym].isoformat()}
    key = (row.get("upstox_mtf") or {}).get("instrument_key") or km.get(sym)
    if not key:
        return {"symbol": sym, "ok": False, "skip": "no instrument key"}
    try:
        candles = fetch_daily_candles(key, days=400, now=now)
    except Exception as e:
        return {"symbol": sym, "ok": False, "skip": "candles: %s: %s" % (type(e).__name__, _sanitize(e))}
    snap = analyze_daily(candles)
    out = {"symbol": sym, "name": row.get("name"), "industry": row.get("industry"),
           "isin": row.get("isin"), "idx": row.get("idx"), "key": key}
    out.update(snap)
    if not snap.get("ok"):
        out["skip"] = snap.get("reason") or "short series"
        return out
    last_ts = snap.get("last_ts")
    if last_ts:
        try:
            last_day = dt.datetime.fromisoformat(last_ts).date()
        except ValueError:
            last_day = None
        if last_day and (now.date() - last_day).days > 4:
            out["ok"] = False
            out["skip"] = "stale daily bar %s" % last_day.isoformat()
            return out
    if not snap.get("liquid"):
        out["ok"] = False
        out["skip"] = "illiquid ADV20 %s" % (fmt_inr(snap.get("adv_20")))
        return out
    if not snap.get("condition"):
        out["ok"] = False
        out["skip"] = "ST condition not met (close<=ST or ST below both MAs)"
        return out
    out["upstox_eligible"] = True
    out["kite_eligible"] = True
    out["upstox_margin_rate"] = (row.get("upstox_mtf") or {}).get("margin_rate")
    out["upstox_leverage"] = (row.get("upstox_mtf") or {}).get("leverage")
    out["kite_margin_rate"] = (row.get("kite_mtf") or {}).get("margin_rate")
    out["kite_leverage"] = (row.get("kite_mtf") or {}).get("leverage")
    return out


def attach_sizing(row, quotes=False):
    """Attach per-broker qty for Rs 50k capital. Optional live margin quotes."""
    price = row.get("close")
    for broker, rate_key, lev_key in (
        ("upstox", "upstox_margin_rate", "upstox_leverage"),
        ("kite", "kite_margin_rate", "kite_leverage"),
    ):
        qty, margin, notional, lev, note = size_qty(CAPITAL_PER_STOCK, price, row.get(rate_key))
        row["%s_qty" % broker] = qty
        row["%s_est_margin" % broker] = margin
        row["%s_est_notional" % broker] = notional
        row["%s_size_note" % broker] = note
        if lev and not row.get(lev_key):
            row[lev_key] = lev
    row["stop"] = row.get("st")
    if quotes and price:
        # quote at 1 share first if sized qty is 0, else at sized qty for each broker
        for broker in ("upstox", "kite"):
            qtry = row.get("%s_qty" % broker) or 1
            # filled below after both list rates; we do a combined quote using max qty later
        # one combined quote per broker at that broker's qty (or 1)
        pass
    return row


def attach_live_quotes(rows):
    for row in rows:
        price = row.get("close")
        if not price:
            continue
        row["quotes"] = {}
        for broker in ("upstox", "kite"):
            qty = int(row.get("%s_qty" % broker) or 0)
            probe = qty if qty >= 1 else 1
            q = margin_quotes(row["symbol"], probe, price).get(broker) or {}
            row["quotes"][broker] = q
            if q.get("ok") and q.get("margin_rate"):
                # resize off the live rate
                qty2, margin2, notional2, lev2, note2 = size_qty(
                    CAPITAL_PER_STOCK, price, q["margin_rate"])
                row["%s_qty" % broker] = qty2
                row["%s_live_margin" % broker] = margin2
                row["%s_live_notional" % broker] = notional2
                row["%s_live_leverage" % broker] = lev2
                row["%s_size_note" % broker] = "live quote: " + note2
                if qty2 != probe and qty2 >= 1:
                    q2 = margin_quotes(row["symbol"], qty2, price).get(broker) or {}
                    row["quotes"][broker] = q2
                    if q2.get("ok"):
                        row["%s_live_margin" % broker] = q2.get("margin")
                        row["%s_live_notional" % broker] = q2.get("notional")
            elif not q.get("ok"):
                row["%s_size_note" % broker] = "quote failed: %s" % q.get("why")
        time.sleep(0.15)
    return rows


def held_mtf_symbols(book):
    held = set()
    for broker in ("upstox", "kite"):
        st = (book or {}).get(broker) or {}
        held.update((st.get("held") or {}).keys())
        held.update((st.get("qty") or {}).keys())
    return {s.upper() for s in held}


# --------------- report ---------------
def print_table(rows, title):
    print()
    print(title)
    print("-" * len(title))
    hdr = ("#", "Symbol", "Cond", "LTP", "SMA50", "SMA150", "ST", "ADX",
           "Ext%", "Upstox qty", "Up lev", "Up margin", "Kite qty", "Kite lev",
           "Kite margin", "Stop", "Both MTF")
    print(" | ".join(hdr))
    for i, r in enumerate(rows, 1):
        both = "Y" if r.get("upstox_eligible") and r.get("kite_eligible") else "N"
        up_m = r.get("upstox_live_margin") or r.get("upstox_est_margin")
        kt_m = r.get("kite_live_margin") or r.get("kite_est_margin")
        cols = [
            str(i),
            r.get("symbol") or "",
            r.get("condition") or "",
            fmt_inr(r.get("close")),
            fmt_inr(r.get("sma50")),
            fmt_inr(r.get("sma150")),
            fmt_inr(r.get("st")),
            ("" if r.get("adx") is None else "%.1f" % r["adx"]),
            ("" if r.get("extension_pct") is None else "%.1f" % r["extension_pct"]),
            str(r.get("upstox_qty") or 0),
            ("" if r.get("upstox_live_leverage") is None and r.get("upstox_leverage") is None
             else "%.2fx" % (r.get("upstox_live_leverage") or r.get("upstox_leverage"))),
            fmt_inr(up_m),
            str(r.get("kite_qty") or 0),
            ("" if r.get("kite_live_leverage") is None and r.get("kite_leverage") is None
             else "%.2fx" % (r.get("kite_live_leverage") or r.get("kite_leverage"))),
            fmt_inr(kt_m),
            fmt_inr(r.get("stop") or r.get("st")),
            both,
        ]
        print(" | ".join(cols))


def coverage_note(book, n_new, per=CAPITAL_PER_STOCK):
    need = n_new * per
    lines = []
    for broker in ("upstox", "kite"):
        st = (book or {}).get(broker)
        if not st:
            err = (book or {}).get("errors", {}).get(broker) or "unavailable"
            lines.append("%s: book unread (%s) — cannot confirm margin coverage." % (broker, err))
            continue
        free = st.get("free")
        cap = st.get("capital")
        ok = free is not None and free + 1e-9 >= need
        lines.append(
            "%s: free %s, capital %s (%s). %s new x %s = %s — %s."
            % (broker, fmt_inr(free), fmt_inr(cap), st.get("capital_basis") or "n/a",
               n_new, fmt_inr(per), fmt_inr(need),
               "COVERED by available margin" if ok else "NOT COVERED by available margin")
        )
        # standing Monday cap
        if cap:
            per_stock_cap = 0.10 * cap
            pool = 0.25 * cap
            lines.append(
                "  standing Monday policy would cap one name at 10%% = %s and the "
                "whole MTF book at 25%% = %s. Rs 50k per name %s that 10%% cap."
                % (fmt_inr(per_stock_cap), fmt_inr(pool),
                   "exceeds" if per > per_stock_cap + 1e-9 else "fits")
            )
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Long-term daily ST MTF scanner (no orders)")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Scan and report only (default).")
    ap.add_argument("--execute", action="store_true", default=False,
                    help="Guarded off. Will not place orders.")
    ap.add_argument("--no-quotes", action="store_true",
                    help="Skip live margin quotes (use published margin rates only).")
    ap.add_argument("--no-book", action="store_true",
                    help="Skip the live holdings/funds read.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Scan only the first N universe names (debug).")
    ap.add_argument("--sleep", type=float, default=0.05)
    args = ap.parse_args(argv)

    now = dt.datetime.now(IST)
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    print("Long-term daily Supertrend MTF scan")
    print("Time: %s Asia/Calcutta" % now.strftime("%Y-%m-%d %H:%M:%S"))
    print("Mode: DRY-RUN (no orders)" + (" ; --execute passed but GUARDED OFF" if args.execute else ""))
    print()
    print("Assumptions / defaults:")
    print("  Daily completed bars; Supertrend(%d, %d); SMA%d / SMA%d." % (ST_PERIOD, ST_MULT, SMA_FAST, SMA_SLOW))
    print("  Long only when close > ST and ST is between the two SMAs or above both.")
    print("  Rank: above_both first, then between; then higher ADX, then less extended.")
    print("  Capital: Rs %s per stock per broker, max %d concurrent names (up to Rs %s / broker)."
          % (f"{CAPITAL_PER_STOCK:,}", MAX_TRADES, f"{CAPITAL_PER_STOCK * MAX_TRADES:,}"))
    print("  Initial stop: daily Supertrend. Liquidity floor: 20-session ADV >= Rs 2 crore.")
    print("  Results exclusion: next %d calendar days, if a calendar is on the box." % RESULTS_WINDOW_DAYS)
    print("  Universe: Nifty 500 intersect Upstox-MTF intersect Kite-MTF.")

    nifty = load_nifty500()
    up_mtf = load_upstox_mtf()
    if not os.path.exists(KITE_MTF_CACHE):
        try:
            refresh_kite_mtf()
        except Exception as e:
            print("Kite MTF list refresh failed:", _sanitize(e))
    kite_mtf = load_kite_mtf()
    both, up_only, kite_only, neither = build_universe(up_mtf, kite_mtf, nifty)
    km, inst_name = keymap()
    today = now.date()
    excl = results_exclusions(load_results_rows(), today)
    print()
    print("Universe: Nifty 500 = %d; Upstox MTF file %s (%d NSE names); "
          "Kite MTF list %s (%d names)."
          % (len(nifty), os.path.basename(UPSTOX_MTF_DEFAULT), len(up_mtf),
             os.path.basename(KITE_MTF_CACHE), len(kite_mtf)))
    print("  Intersection eligible on BOTH brokers: %d" % len(both))
    print("  Nifty 500 but Upstox-only MTF: %d; Kite-only: %d; neither: %d"
          % (len(up_only), len(kite_only), len(neither)))
    print("  Instrument master: %s (%d EQ keys)." % (inst_name, len(km)))
    if excl:
        print("  Results exclusions in the next %d days: %s"
              % (RESULTS_WINDOW_DAYS, ", ".join("%s (%s)" % (s, d) for s, d in sorted(excl.items()))))
    else:
        print("  Results calendars on the box have no dates in the next %d days "
              "(results_universe.csv last dates are late Oct 2026; stockopt_calendar.json "
              "has TATASTEEL 2026-11-07). No name was excluded for results. The calendar may be stale."
              % RESULTS_WINDOW_DAYS)

    book = {"upstox": None, "kite": None, "errors": {}}
    if not args.no_book:
        print()
        print("Reading live MTF books and funds (read only)...")
        book = live_book()
        for broker in ("upstox", "kite"):
            st = book.get(broker)
            if not st:
                print("  %s: FAILED %s" % (broker, book.get("errors", {}).get(broker)))
                continue
            held = st.get("held") or {}
            qty = st.get("qty") or {}
            print("  %s: capital %s (%s), free %s, MTF names %d, bot-tagged %s"
                  % (broker, fmt_inr(st.get("capital")), st.get("capital_basis"),
                     fmt_inr(st.get("free")), len(held), st.get("mine") or []))
            for sym in sorted(held):
                print("    %s qty=%s cost=%s" % (sym, qty.get(sym), fmt_inr(held[sym])))

    held = held_mtf_symbols(book)
    watch = both
    if args.limit and args.limit > 0:
        watch = both[: args.limit]

    print()
    print("Scanning %d names on daily candles..." % len(watch))
    rows, skipped, failed = [], [], []
    for i, row in enumerate(watch, 1):
        rec = scan_symbol(row, km, now, excl)
        if rec.get("ok") and rec.get("condition"):
            attach_sizing(rec)
            rows.append(rec)
        elif rec.get("skip", "").startswith("candles:"):
            failed.append(rec)
        else:
            skipped.append(rec)
        if i % 50 == 0 or i == len(watch):
            print("  %d/%d  candidates=%d  skipped=%d  failed=%d"
                  % (i, len(watch), len(rows), len(skipped), len(failed)))
        if args.sleep:
            time.sleep(args.sleep)

    rows.sort(key=rank_key)
    # keep held names out of the entry shortlist but report if they still qualify
    held_hits = [r for r in rows if r["symbol"] in held]
    fresh = [r for r in rows if r["symbol"] not in held]

    n_slots_if_existing_counts = max(0, MAX_TRADES - len(held))
    shortlist = fresh[:MAX_TRADES]
    reserves = fresh[MAX_TRADES:MAX_TRADES + 3]
    # if existing MTF counts, the actionable set is shorter
    actionable = fresh[:n_slots_if_existing_counts]

    to_quote = []
    seen = set()
    for r in shortlist + reserves + actionable + held_hits:
        if r["symbol"] not in seen:
            to_quote.append(r)
            seen.add(r["symbol"])
    if not args.no_quotes and to_quote:
        print()
        print("Live MTF margin quotes for the shortlist / reserves (read only)...")
        attach_live_quotes(to_quote)

    print_table(shortlist, "Top %d shortlist (new names, SRF excluded from the list itself)" % MAX_TRADES)
    if reserves:
        print_table(reserves, "Reserves (next 3)")
    if held_hits:
        print_table(held_hits, "Existing MTF holdings that still meet the daily rule (not a new entry)")

    print()
    print("Existing MTF holdings and slot count")
    print("-----------------------------------")
    if not held:
        print("  No open MTF positions on either broker. All %d long-term slots are free." % MAX_TRADES)
    else:
        print("  Open MTF (both brokers combined): %s" % ", ".join(sorted(held)))
        print("  None of these were opened under this long-term daily-ST sleeve (bot-tagged mine is empty).")
        print("  If existing MTF names count toward the max-%d: %d slot(s) free."
              % (MAX_TRADES, n_slots_if_existing_counts))
        print("  If this sleeve is separate: all %d slots are free." % MAX_TRADES)
        if n_slots_if_existing_counts < MAX_TRADES:
            print("  Actionable new names if existing MTF counts: %s"
                  % (", ".join(r["symbol"] for r in actionable) or "(none)"))

    print()
    print("Margin coverage for new trades at Rs %s each" % f"{CAPITAL_PER_STOCK:,}")
    for line in coverage_note(book, MAX_TRADES):
        print("  " + line)
    if held and n_slots_if_existing_counts != MAX_TRADES:
        print("  If existing MTF counts, coverage for %d new name(s):" % n_slots_if_existing_counts)
        for line in coverage_note(book, n_slots_if_existing_counts):
            print("    " + line)

    # skip reasons summary
    from collections import Counter
    reasons = Counter()
    for r in skipped + failed:
        why = r.get("skip") or "unknown"
        if why.startswith("illiquid"):
            reasons["illiquid"] += 1
        elif why.startswith("ST condition"):
            reasons["ST condition not met"] += 1
        elif why.startswith("results"):
            reasons["results window"] += 1
        elif why.startswith("candles"):
            reasons["candle fetch failed"] += 1
        elif "fewer than" in (why or ""):
            reasons["short history"] += 1
        else:
            reasons[why] += 1
    print()
    print("Scan outcomes: %d candidates, %d skipped, %d failed of %d scanned."
          % (len(rows), len(skipped), len(failed), len(watch)))
    for k, v in reasons.most_common():
        print("  %s: %d" % (k, v))

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Calcutta",
        "defaults": {
            "timeframe": "daily",
            "supertrend": {"period": ST_PERIOD, "multiplier": ST_MULT},
            "sma": [SMA_FAST, SMA_SLOW],
            "capital_per_stock": CAPITAL_PER_STOCK,
            "max_trades": MAX_TRADES,
            "capital_per_broker_max": CAPITAL_PER_STOCK * MAX_TRADES,
            "adv_min": ADV_MIN,
            "min_daily_bars": MIN_DAILY_BARS,
            "results_window_days": RESULTS_WINDOW_DAYS,
            "initial_stop": "daily Supertrend",
            "execute_enabled": EXECUTE_ENABLED,
        },
        "universe": {
            "nifty500": len(nifty),
            "upstox_mtf": len(up_mtf),
            "upstox_mtf_source": os.path.basename(UPSTOX_MTF_DEFAULT),
            "kite_mtf": len(kite_mtf),
            "kite_mtf_source": os.path.basename(KITE_MTF_CACHE),
            "both": len(both),
            "upstox_only": up_only,
            "kite_only": kite_only,
            "neither": neither,
            "results_exclusions": {s: d.isoformat() for s, d in excl.items()},
        },
        "book": book,
        "held_mtf": sorted(held),
        "slots_if_existing_counts": n_slots_if_existing_counts,
        "candidates": rows,
        "shortlist": shortlist,
        "reserves": reserves,
        "actionable_if_existing_counts": actionable,
        "held_hits": held_hits,
        "skip_counts": dict(reasons),
        "failed": failed,
    }
    # make json-safe
    def conv(o):
        if isinstance(o, dt.date) and not isinstance(o, dt.datetime):
            return o.isoformat()
        if isinstance(o, dt.datetime):
            return o.isoformat(timespec="seconds")
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(type(o))
    out_path = os.path.join(OUT_DIR, "scan_%s.json" % today.isoformat())
    with open(out_path, "w") as f:
        json.dump(payload, f, default=conv, indent=2)
    print()
    print("Wrote", out_path)
    print("Prepared script: %s" % os.path.abspath(__file__))
    print("EXECUTE_ENABLED=%s — no orders placed." % EXECUTE_ENABLED)

    if args.execute:
        print()
        print("This scanner does not place orders. Use equity/longterm_daily_st_entry.py")
        print("via equity/longterm_daily_st_live.sh --execute after 09:20 IST on a trading day.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
