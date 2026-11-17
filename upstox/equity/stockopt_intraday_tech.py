"""Intraday strong-technical stock-option policy. Sends nothing.

t694u (16 Nov 2026): from a pinned strong-technicals shortlist, take at most
two INTRADAY stock-option names when a completed 1-hour cash bar still shows
the same setup (price on the right side of EMA20 and EMA50, Supertrend and
MACD agreeing, ADX >= 20) and the daily bar still agrees (close vs EMA20 and
EMA50, Supertrend). Rank by 1h ADX / strength, with daily-MACD disagreement
treated as weaker cleanliness. Stock-option signals are always the completed
1-hour bar (never 3m or 15m).

This week (ISO week of 16 Nov 2026) is not the 23 Nov expiry week, so the
entry is the current monthly ATM as a plain option, not a debit spread. In
the contract's own expiry week this path refuses a new near-month entry
(the next-month spread path is separate). Size is 1 lot per broker, product
Upstox I / Kite MIS, squared off the same session (15:15 IST, or 15:10 IST
on Friday). New entries are allowed 09:30-14:30 IST only, so a fill can
still be managed before the square-off. The 20% initial stop and breakeven
ladder are the shared option-stop rules, not a special case here.

t696u (16 Nov 2026): total premium deployed per broker on this path — already
open positions plus the new entry — is capped at Rs 20,000. Premium is
lot size x limit price (1 lot). A name that would breach the cap is skipped
with a logged reason, and the next-ranked still-valid name is tried. The
name cap of two is unchanged.

The weekend Supertrend-versus-EMA watchlist path is separate and stays on
delivery (D / NRML) with a Friday flatten. This module does not add to that
list and does not send an add. Importing it does not read a broker and does
not place an order.
"""
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import stockopt_policy as pol

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TAG = "stockopt_intraday"
SOURCE = "intraday_tech"
MAX_NAMES = 2
LOTS = 1
PREMIUM_CAP_PER_BROKER = 20000  # t696u: lot size x limit price, this path only
ADX_MIN = 20
ENTRY_OPEN = dt.time(9, 30)
ENTRY_CLOSE = dt.time(14, 30)
FLATTEN_AT = dt.time(15, 15)
FRIDAY_FLATTEN_AT = dt.time(15, 10)
SPREAD_MAX = 0.10
ENABLE_ENV = "STOCKOPT_INTRADAY_TECH_EXECUTE"
SHORTLIST_PATH = os.path.join(HERE, "stockopt_intraday_shortlist.json")
REPORTS_DIR = os.path.join(os.path.dirname(ROOT), "reports")
SIGNAL_UNIT = "hours"
SIGNAL_INTERVAL = 1
SIGNAL_DAYS = 60
DAILY_UNIT = "days"
DAILY_INTERVAL = 1
DAILY_DAYS = 200
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9&-]*$")
UPSTOX_PRODUCT = "I"
KITE_PRODUCT = "MIS"


def execute_enabled(environ=None):
    """True only when this path is explicitly switched on. Default off."""
    env = os.environ if environ is None else environ
    return str(env.get(ENABLE_ENV, "")).strip() == "1"


def stock_lots(when=None):
    """1 lot per broker. The expired 5-6 Nov 2-lot grant is not applied."""
    del when
    return LOTS


def _as_ist(now):
    if not isinstance(now, dt.datetime):
        return None
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def _num(bar, key):
    if not isinstance(bar, dict):
        return None
    try:
        value = bar.get(key)
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _direction(bar):
    if not isinstance(bar, dict):
        return None
    raw = bar.get("dir")
    try:
        if raw is not None and raw != "":
            value = float(raw)
            if value > 0:
                return 1
            if value < 0:
                return -1
    except (TypeError, ValueError):
        pass
    state = str(bar.get("st_state") or bar.get("d_st_state") or "").strip().lower()
    if state in ("bullish", "up", "long"):
        return 1
    if state in ("bearish", "down", "short"):
        return -1
    return None


def flatten_time(now):
    clock = _as_ist(now)
    if clock is None:
        return FLATTEN_AT
    if clock.weekday() == 4:
        return FRIDAY_FLATTEN_AT
    return FLATTEN_AT


def in_expiry_week(expiry, today=None):
    """True when expiry falls in the same ISO week as today. None if unverified."""
    if expiry is None or today is None:
        return None
    try:
        if not isinstance(expiry, dt.date):
            expiry = dt.date.fromisoformat(str(expiry)[:10])
        if not isinstance(today, dt.date):
            today = dt.date.fromisoformat(str(today)[:10])
    except (TypeError, ValueError):
        return None
    return expiry.isocalendar()[:2] == today.isocalendar()[:2]


def current_monthly_expiry(rows, today):
    """Nearest unexpired option expiry in the chain (stock options are monthlies)."""
    dates = []
    for row in rows or []:
        exp = row.get("expiry_date") if isinstance(row, dict) else None
        if exp is None:
            continue
        if not isinstance(exp, dt.date):
            try:
                exp = dt.date.fromisoformat(str(exp)[:10])
            except (TypeError, ValueError):
                continue
        if exp >= today:
            dates.append(exp)
    return min(dates) if dates else None


def pick_monthly_atm(rows, side, close, today, expiry=None):
    """ATM of the current monthly expiry (or a supplied expiry)."""
    if not rows or side not in ("CE", "PE") or close is None:
        return None
    try:
        close = float(close)
    except (TypeError, ValueError):
        return None
    want = expiry or current_monthly_expiry(rows, today)
    if want is None:
        return None
    if not isinstance(want, dt.date):
        try:
            want = dt.date.fromisoformat(str(want)[:10])
        except (TypeError, ValueError):
            return None
    same = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        exp = row.get("expiry_date")
        if not isinstance(exp, dt.date):
            try:
                exp = dt.date.fromisoformat(str(exp)[:10])
            except (TypeError, ValueError):
                continue
        if exp == want and (row.get("instrument_type") or row.get("type")) == side:
            same.append(row)
    if not same:
        return None
    return min(same, key=lambda row: abs(float(row.get("strike_price") or row.get("strike") or 0) - close))


def hour_side(bar, adx_min=ADX_MIN):
    """'CE', 'PE', or None against the completed 1h rule set."""
    if not isinstance(bar, dict):
        return None
    close, e20, e50 = _num(bar, "close"), _num(bar, "ema20"), _num(bar, "ema50")
    hist, adx = _num(bar, "hist"), _num(bar, "adx")
    direction = _direction(bar)
    if None in (close, e20, e50, hist, adx, direction):
        return None
    if adx < adx_min:
        return None
    if close > e20 and close > e50 and direction == 1 and hist > 0:
        return "CE"
    if close < e20 and close < e50 and direction == -1 and hist < 0:
        return "PE"
    return None


def daily_agree(bar, side):
    """Daily close vs EMA20+EMA50 and Supertrend. MACD/ADX are reported separately."""
    if not isinstance(bar, dict) or side not in ("CE", "PE"):
        return False, ["insufficient daily"]
    close, e20, e50 = _num(bar, "close"), _num(bar, "ema20"), _num(bar, "ema50")
    direction = _direction(bar)
    if None in (close, e20, e50, direction):
        return False, ["insufficient daily"]
    fails = []
    if side == "CE":
        if not (close > e20 and close > e50):
            fails.append("daily close not above EMA20+EMA50")
        if direction != 1:
            fails.append("daily Supertrend not bullish")
    else:
        if not (close < e20 and close < e50):
            fails.append("daily close not below EMA20+EMA50")
        if direction != -1:
            fails.append("daily Supertrend not bearish")
    return (not fails), fails


def daily_macd_agrees(bar, side):
    hist = _num(bar, "hist")
    if hist is None or side not in ("CE", "PE"):
        return False
    return (side == "CE" and hist > 0) or (side == "PE" and hist < 0)


def strength(hour, daily=None):
    """1h ADX plus a small rising-ADX and daily-ADX boost. Same rank as the 16 Nov scan."""
    adx = _num(hour, "adx")
    if adx is None:
        return None
    score = adx
    prev = _num(hour, "prev_adx")
    if prev is not None:
        score += max(0.0, adx - prev) * 0.15
    d_adx = _num(daily, "adx") if daily else None
    if d_adx is not None:
        score += min(d_adx, 40) * 0.05
    return round(score, 2)


def pack_bars(cs):
    """Indicator pack for a completed OHLC series. Uses the same maths as ind.py."""
    if not cs or len(cs) < 60:
        return None
    import ind
    high = [float(row[2]) for row in cs]
    low = [float(row[3]) for row in cs]
    close = [float(row[4]) for row in cs]
    st_v, direction = ind.supertrend(high, low, close, 10, 3)
    e20 = ind.ema(close, 20)
    e50 = ind.ema(close, 50)
    macd, sig, hist = ind.macd(close)
    pdi, mdi, adx = ind.dmi(high, low, close, 14)
    idx = -1
    def _r(value, digits=2):
        return None if value is None else round(float(value), digits)
    return {
        "last_ts": cs[idx][0],
        "close": _r(close[idx]),
        "ema20": _r(e20[idx]),
        "ema50": _r(e50[idx]),
        "st": _r(st_v[idx]),
        "dir": int(direction[idx]),
        "hist": _r(hist[idx], 3),
        "macd": _r(macd[idx], 3),
        "sig": _r(sig[idx], 3),
        "adx": _r(adx[idx]),
        "prev_adx": _r(adx[idx - 1]) if len(adx) > 1 else None,
        "pdi": _r(pdi[idx]),
        "mdi": _r(mdi[idx]),
        "bars": len(cs),
    }


def drop_incomplete(cs, unit, interval, now):
    """Drop a still-forming last bar. Same rule as the 16 Nov scan."""
    if not cs:
        return cs
    clock = _as_ist(now)
    if clock is None:
        return cs
    last = dt.datetime.fromisoformat(cs[-1][0])
    if last.tzinfo is None:
        last = last.replace(tzinfo=IST)
    else:
        last = last.astimezone(IST)
    if unit == "minutes":
        step = dt.timedelta(minutes=interval)
        return cs if last + step <= clock else cs[:-1]
    if unit == "hours":
        step = dt.timedelta(hours=interval)
        return cs if last + step <= clock else cs[:-1]
    close = last.replace(hour=15, minute=30, second=0, microsecond=0)
    if last.date() == clock.date() and last.hour == 0:
        close = close.replace(year=last.year, month=last.month, day=last.day)
    return cs if clock >= close else cs[:-1]


def bars_from_report_row(row):
    """Build (hour, daily, meta) from a strong-technicals scan row."""
    if not isinstance(row, dict):
        return None, None, {}
    hour = {
        "last_ts": row.get("h_last_ts"),
        "close": row.get("ltp") or row.get("h_close") or row.get("close"),
        "ema20": row.get("h_ema20"),
        "ema50": row.get("h_ema50"),
        "st": row.get("h_st"),
        "st_state": row.get("h_st_state"),
        "dir": row.get("h_dir") if row.get("h_dir") is not None else (
            1 if row.get("h_st_state") == "bullish" else (-1 if row.get("h_st_state") == "bearish" else None)
        ),
        "hist": row.get("h_macd_hist"),
        "adx": row.get("h_adx"),
        "prev_adx": row.get("h_prev_adx"),
        "pdi": row.get("h_pdi"),
        "mdi": row.get("h_mdi"),
    }
    daily = {
        "last_ts": row.get("d_last_ts"),
        "close": row.get("d_close"),
        "ema20": row.get("d_ema20"),
        "ema50": row.get("d_ema50"),
        "st": row.get("d_st"),
        "st_state": row.get("d_st_state"),
        "dir": row.get("d_dir") if row.get("d_dir") is not None else (
            1 if row.get("d_st_state") == "bullish" else (-1 if row.get("d_st_state") == "bearish" else None)
        ),
        "hist": row.get("d_macd_hist"),
        "adx": row.get("d_adx"),
    }
    meta = {
        "symbol": str(row.get("symbol") or "").strip().upper(),
        "side": row.get("side"),
        "lot_size": row.get("lot_size"),
        "contract": row.get("nov_contract") or row.get("preferred_contract"),
        "strike": row.get("nov_atm_strike") or row.get("preferred_strike"),
        "expiry": row.get("nov_expiry") or row.get("expiry"),
        "results_in_window": bool(row.get("results_in_window")),
        "cleanliness": row.get("cleanliness"),
        "daily_macd_agrees": row.get("daily_macd_agrees"),
        "strength": row.get("strength"),
    }
    return hour, daily, meta


def load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_shortlist(path=None):
    """Pinned shortlist. Missing or unreadable fails closed."""
    data = load_json(path or SHORTLIST_PATH)
    if not data:
        return {"ok": False, "reason": "intraday shortlist unreadable", "candidates": []}
    rows = data.get("candidates")
    if not isinstance(rows, (list, tuple)):
        return {"ok": False, "reason": "intraday shortlist has no candidates", "candidates": []}
    kept, dropped, seen = [], [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            dropped.append({"symbol": raw, "reason": "unreadable candidate"})
            continue
        name = str(raw.get("symbol") or "").strip().upper()
        side = str(raw.get("side") or "").strip().upper()
        if not name or not _SYMBOL.match(name):
            dropped.append({"symbol": raw.get("symbol"), "reason": "unreadable symbol"})
            continue
        if side not in ("CE", "PE"):
            dropped.append({"symbol": name, "reason": "side must be CE or PE"})
            continue
        if name in seen:
            continue
        seen.add(name)
        item = dict(raw)
        item["symbol"] = name
        item["side"] = side
        item["cleanliness"] = str(raw.get("cleanliness") or "clean").strip().lower()
        kept.append(item)
    if not kept:
        return {"ok": False, "reason": "intraday shortlist is empty", "candidates": [], "dropped": dropped}
    return {"ok": True, "candidates": kept, "dropped": dropped, "max_names": int(data.get("max_names") or MAX_NAMES),
            "expiry": data.get("expiry"), "as_of_session": data.get("as_of_session"), "path": path or SHORTLIST_PATH}


def latest_report_path(reports_dir=None, session=None):
    import glob
    folder = reports_dir or REPORTS_DIR
    if session:
        exact = os.path.join(folder, "stockopt_strong_technicals_%s.json" % session)
        if os.path.exists(exact):
            return exact
    matches = sorted(glob.glob(os.path.join(folder, "stockopt_strong_technicals_????-??-??.json")), reverse=True)
    return matches[0] if matches else None


def load_report(path=None, session=None):
    path = path or latest_report_path(session=session)
    data = load_json(path)
    if not data:
        return {"ok": False, "reason": "strong-technicals report unreadable", "path": path, "rows": {}}
    rows = {}
    for key in ("top_bullish", "top_bearish", "bullish_pass", "bearish_pass", "bullish_1h_only", "bearish_1h_only"):
        for row in data.get(key) or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("symbol") or "").strip().upper()
            if name and name not in rows:
                rows[name] = row
    return {"ok": True, "path": path, "data": data, "rows": rows,
            "as_of_session": data.get("as_of_session"), "expiry": (data.get("expiry") or {}).get("nov_monthly")}


def entry_window(now, session_ok=None):
    clock = _as_ist(now)
    if clock is None:
        return {"ok": False, "reason": "clock unverified"}
    if clock.weekday() >= 5:
        return {"ok": False, "reason": "not a weekday"}
    if session_ok is False:
        return {"ok": False, "reason": "not a trading session"}
    if not (ENTRY_OPEN <= clock.time() <= ENTRY_CLOSE):
        return {"ok": False, "reason": "outside 09:30-14:30 IST entry window (no new intraday entry after 14:30)"}
    return {"ok": True, "reason": "inside 09:30-14:30 IST"}


def same_day_flatten_plan(now, session_ok=None):
    """Due from the session square-off time until the cash close."""
    clock = _as_ist(now)
    if clock is None:
        return {"due": False, "send": False, "reason": "clock unverified"}
    if clock.weekday() >= 5:
        return {"due": False, "send": False, "reason": "not a weekday"}
    if session_ok is False:
        return {"due": False, "send": False, "reason": "not a trading session"}
    due_at = flatten_time(clock)
    stamp = dt.datetime.combine(clock.date(), due_at, tzinfo=IST)
    if clock < stamp:
        return {"due": False, "send": False, "flatten_at": due_at.isoformat(timespec="minutes"),
                "reason": "hold until %s IST" % due_at.strftime("%H:%M")}
    end = dt.datetime.combine(clock.date(), dt.time(15, 30), tzinfo=IST)
    if clock > end:
        return {"due": True, "overdue": True, "send": False, "flatten_at": due_at.isoformat(timespec="minutes"),
                "reason": "same-day square-off window has closed"}
    return {"due": True, "overdue": False, "send": False, "flatten_at": due_at.isoformat(timespec="minutes"),
            "reason": "square off same-day intraday stock options at %s IST" % due_at.strftime("%H:%M")}


def results_skip(name, status):
    if status is None:
        return None
    if not isinstance(status, dict) or status.get("ok") is not True:
        return "results calendar unverified"
    names = {str(item).strip().upper() for item in (status.get("names") or []) if str(item).strip()}
    if str(name or "").strip().upper() in names:
        return "results due"
    return None


def chain_skip(chain_count):
    if chain_count is None:
        return "option chain unverified"
    try:
        count = int(chain_count)
    except (TypeError, ValueError):
        return "option chain unverified"
    if count <= 0:
        return "no current option chain"
    return None


def liquidity_skip(quote):
    if not isinstance(quote, dict):
        return "liquidity unverified"
    try:
        ltp = float(quote.get("ltp") or 0)
        bid = float(quote.get("bid") or 0)
        ask = float(quote.get("ask") or 0)
    except (TypeError, ValueError):
        return "liquidity unverified"
    oi = quote.get("oi", quote.get("open_interest"))
    if oi is None:
        return "open interest unverified"
    try:
        oi = int(oi)
    except (TypeError, ValueError):
        return "open interest unverified"
    if ltp <= 0 and ask <= 0:
        return "no usable quote"
    if oi <= 0 or bid <= 0 or ask <= 0 or ask < bid:
        return "poor liquidity"
    mid = (bid + ask) / 2.0
    if mid <= 0 or (ask - bid) / mid > SPREAD_MAX:
        return "poor liquidity"
    return None


def tick(price, size=0.05):
    return round(max(round(float(price) / size) * size, size), 2)


def limit_price(quote, tick_fn=tick):
    if liquidity_skip(quote):
        return None
    try:
        ltp = float(quote.get("ltp") or 0)
        ask = float(quote.get("ask") or 0)
        size = float(quote.get("tick") or 0.05)
    except (TypeError, ValueError):
        return None
    if size > 1:
        size = size / 100.0
    raw = ask if ask > 0 else ltp * 1.005
    if raw <= 0:
        return None
    return tick_fn(raw, size)


def _fmt_inr(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "Rs ?"
    if abs(amount - round(amount)) < 1e-9:
        return "Rs {:,.0f}".format(round(amount))
    return "Rs {:,.2f}".format(amount)


def entry_premium(lot_size, limit_px, lots=None):
    """Premium for a new entry: lots * lot_size * limit_price. None if unverified."""
    try:
        lot = int(lot_size)
        price = float(limit_px)
        n = int(LOTS if lots is None else lots)
    except (TypeError, ValueError):
        return None
    if lot <= 0 or price <= 0 or n <= 0:
        return None
    return round(n * lot * price, 2)


def position_premium(pos):
    """qty * average (deployed) price. last_price is not used. None if unverified."""
    if not isinstance(pos, dict):
        return None
    try:
        qty = int(pos.get("quantity") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return 0.0
    for key in ("average_price", "buy_price", "average_buy_price", "entry_price"):
        raw = pos.get(key)
        if raw is None or raw == "":
            continue
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return round(qty * price, 2)
    return None


def _norm_sym(sym):
    return "".join(ch for ch in str(sym or "").upper() if ch.isalnum())


def path_ledger_names(ledger):
    """Underlyings and compact trading symbols recorded for this path."""
    names, symbols = set(), set()
    if not isinstance(ledger, dict):
        return names, symbols
    for name, row in (ledger.get("names") or {}).items():
        if not isinstance(row, dict):
            continue
        if row.get("source") not in (None, "", SOURCE) and row.get("tag") not in (None, "", TAG):
            continue
        if row.get("source") and row.get("source") != SOURCE:
            continue
        if row.get("tag") and row.get("tag") != TAG:
            continue
        if row.get("source") != SOURCE and row.get("tag") != TAG:
            # Unlabelled ledger rows are ignored so a different path is not counted.
            continue
        names.add(str(name).upper())
        sym = row.get("trading_symbol") or row.get("tradingsymbol")
        if sym:
            symbols.add(_norm_sym(sym))
    return names, symbols


def is_path_position(pos, ledger=None):
    """True if this long stock-option is on the intraday-tech path. None if unverified."""
    if not isinstance(pos, dict):
        return None
    sym = pos.get("trading_symbol") or pos.get("tradingsymbol") or ""
    if not sym:
        return False
    if not pol.is_stock_option(sym):
        return False
    try:
        qty = int(pos.get("quantity") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return False
    product = str(pos.get("product") or "").upper()
    if product and product not in (UPSTOX_PRODUCT, KITE_PRODUCT, "I", "MIS"):
        return False
    if pos.get("tag") == TAG or pos.get("source") == SOURCE or pos.get("order_tag") == TAG:
        return True
    names, symbols = path_ledger_names(ledger)
    if _norm_sym(sym) in symbols:
        return True
    try:
        underlying = pol.underlying(sym)
    except Exception:
        return None
    return bool(underlying and underlying in names)


def path_premium_deployed(positions, ledger=None):
    """Per-broker premium already deployed on this path. None if unverified."""
    if not isinstance(positions, dict) or positions.get("upstox") is None or positions.get("kite") is None:
        return None
    out = {"upstox": 0.0, "kite": 0.0}
    for broker in ("upstox", "kite"):
        for pos in positions[broker] or []:
            flag = is_path_position(pos, ledger)
            if flag is None:
                return None
            if not flag:
                continue
            prem = position_premium(pos)
            if prem is None:
                return None
            out[broker] = round(out[broker] + prem, 2)
    return out


def decision_premiums(decision):
    """Per-broker new-entry premium from a decision, or None if not computed."""
    if not isinstance(decision, dict):
        return None
    per = decision.get("premium_per_broker")
    if isinstance(per, dict):
        try:
            upstox = float(per.get("upstox"))
            kite = float(per.get("kite"))
        except (TypeError, ValueError):
            return None
        if upstox <= 0 or kite <= 0:
            return None
        return {"upstox": round(upstox, 2), "kite": round(kite, 2)}
    raw = decision.get("premium")
    if raw is None or raw == "":
        return None
    try:
        prem = float(raw)
    except (TypeError, ValueError):
        return None
    if prem <= 0:
        return None
    prem = round(prem, 2)
    return {"upstox": prem, "kite": prem}


def attach_entry_premium(decision, quote=None, contract=None, tick_fn=None):
    """Stamp limit_price and per-broker premium on an enter decision. May skip it."""
    if not isinstance(decision, dict) or decision.get("action") != "enter":
        return decision
    lot = decision.get("lot_size")
    kite_lot = lot
    if isinstance(contract, dict):
        if not lot:
            lot = contract.get("lot_size")
        kite_lot = contract.get("kite_lot_size") or lot
        decision["lot_size"] = decision.get("lot_size") or lot
    price = decision.get("limit_price")
    if price is None and quote is not None:
        price = limit_price(quote, tick_fn or tick)
    if quote is None and price is None:
        return decision
    upstox = entry_premium(lot, price)
    kite = entry_premium(kite_lot, price)
    if price is None or upstox is None or kite is None:
        decision["action"] = "skip"
        decision["lots"] = 0
        decision["quantity"] = 0
        decision["send"] = False
        decision["orders_placed"] = False
        decision["reason"] = "premium unverified"
        return decision
    decision["limit_price"] = price
    decision["premium"] = upstox
    decision["premium_per_broker"] = {"upstox": upstox, "kite": kite}
    return decision


def premium_cap_reason(new_premium, deployed, cap=PREMIUM_CAP_PER_BROKER):
    """Skip reason if new + open exceeds the cap on either broker. None if it fits."""
    if deployed == "unverified":
        return "open premium unverified; new entry refused"
    if new_premium is None:
        return None
    if not isinstance(new_premium, dict):
        try:
            value = float(new_premium)
        except (TypeError, ValueError):
            return "premium unverified"
        if value <= 0:
            return "premium unverified"
        new_premium = {"upstox": value, "kite": value}
    try:
        cap_value = float(PREMIUM_CAP_PER_BROKER if cap is None else cap)
    except (TypeError, ValueError):
        cap_value = float(PREMIUM_CAP_PER_BROKER)
    open_book = deployed if isinstance(deployed, dict) else {"upstox": 0.0, "kite": 0.0}
    for broker in ("upstox", "kite"):
        try:
            add = float(new_premium.get(broker) or 0)
            already = float(open_book.get(broker) or 0)
        except (TypeError, ValueError):
            return "premium unverified"
        total = already + add
        if total > cap_value + 1e-9:
            if already <= 0:
                return "premium %s exceeds %s cap per broker" % (_fmt_inr(add), _fmt_inr(cap_value))
            return "premium %s plus open %s exceeds %s cap per broker" % (
                _fmt_inr(add), _fmt_inr(already), _fmt_inr(cap_value))
    return None


def evaluate_name(candidate, hour, daily, *, today=None, chain_count=None, quote=None,
                  contract=None, held=False, entered_today=False, books_readable=True,
                  results=None, expiry_week=None):
    """One shortlist name. `send` stays false. Off-list callers should not use this."""
    name = str((candidate or {}).get("symbol") or "").strip().upper()
    want = str((candidate or {}).get("side") or "").strip().upper()
    decision = {
        "underlying": name, "action": "reject", "reason": "", "side": want or None,
        "lots": 0, "quantity": 0, "send": False, "orders_placed": False,
        "tag": TAG, "source": SOURCE, "add": False, "product": {"upstox": UPSTOX_PRODUCT, "kite": KITE_PRODUCT},
        "cleanliness": str((candidate or {}).get("cleanliness") or ""),
    }
    if not name or want not in ("CE", "PE"):
        decision["reason"] = "unreadable shortlist candidate"
        return decision
    if books_readable is not True:
        decision["action"] = "skip"
        decision["reason"] = "broker book unverified"
        return decision
    if held or entered_today:
        decision["action"] = "skip"
        decision["reason"] = "already open or already entered today; adds are off"
        return decision
    if expiry_week is True:
        decision["action"] = "skip"
        decision["reason"] = "expiry week; this intraday path does not open a near-month option"
        return decision
    if expiry_week is None and today is not None:
        decision["action"] = "skip"
        decision["reason"] = "expiry calendar unverified"
        return decision
    skip = results_skip(name, results)
    if skip:
        decision["action"] = "skip"
        decision["reason"] = skip
        return decision
    if isinstance(candidate, dict) and candidate.get("results_in_window"):
        decision["action"] = "skip"
        decision["reason"] = "results due"
        return decision
    skip = chain_skip(chain_count)
    if skip:
        decision["action"] = "skip"
        decision["reason"] = skip
        return decision
    side = hour_side(hour)
    decision["hour_side"] = side
    decision["hour"] = {
        "last_ts": None if not isinstance(hour, dict) else hour.get("last_ts"),
        "close": _num(hour, "close"), "ema20": _num(hour, "ema20"), "ema50": _num(hour, "ema50"),
        "adx": _num(hour, "adx"), "hist": _num(hour, "hist"), "dir": _direction(hour),
    }
    if side is None:
        decision["action"] = "skip"
        decision["reason"] = "1h setup no longer holds (EMA20/EMA50, Supertrend, MACD, ADX>=20)"
        return decision
    if side != want:
        decision["action"] = "skip"
        decision["reason"] = "1h side is %s, shortlist is %s; setup did not continue" % (side, want)
        return decision
    agree, fails = daily_agree(daily, want)
    decision["daily_agree"] = agree
    decision["daily_agree_fails"] = fails
    macd_ok = daily_macd_agrees(daily, want)
    decision["daily_macd_agrees"] = macd_ok
    if not agree:
        decision["action"] = "skip"
        decision["reason"] = "daily no longer agrees: " + ", ".join(fails)
        return decision
    score = strength(hour, daily)
    decision["strength"] = score
    decision["clean"] = bool(macd_ok) and str((candidate or {}).get("cleanliness") or "clean").lower() != "weaker"
    if not macd_ok or str((candidate or {}).get("cleanliness") or "").lower() == "weaker":
        decision["cleanliness"] = "weaker"
    if liquidity_skip(quote) and quote is not None:
        # A supplied but unusable quote is a skip. A missing quote is allowed at
        # the planning stage so a preview from the close report can still rank.
        decision["action"] = "skip"
        decision["reason"] = liquidity_skip(quote)
        return decision
    lots = stock_lots()
    qty = 0
    lot = None
    if isinstance(contract, dict):
        try:
            lot = int(contract.get("lot_size") or 0)
        except (TypeError, ValueError):
            lot = None
        if lot and lot > 0:
            qty = lots * lot
    decision["action"] = "enter"
    decision["reason"] = "1h and daily setup still holds"
    decision["side"] = want
    decision["lots"] = lots
    decision["quantity"] = qty
    decision["lot_size"] = lot
    if isinstance(contract, dict):
        decision["trading_symbol"] = contract.get("trading_symbol")
        decision["instrument_key"] = contract.get("instrument_key")
        decision["expiry"] = str(contract.get("expiry_date") or "")
        decision["strike"] = contract.get("strike_price")
    elif (candidate or {}).get("preferred_contract"):
        decision["trading_symbol"] = candidate.get("preferred_contract")
        decision["strike"] = candidate.get("preferred_strike")
        decision["lot_size"] = candidate.get("lot_size") or lot
        if decision["lot_size"] and not qty:
            try:
                decision["quantity"] = lots * int(decision["lot_size"])
            except (TypeError, ValueError):
                pass
    if quote is not None:
        attach_entry_premium(decision, quote=quote, contract=contract)
    return decision


def rank_key(decision):
    """Strongest first: clean names, then strength, then 1h ADX, then symbol."""
    clean = 0 if decision.get("clean") or decision.get("daily_macd_agrees") and decision.get("cleanliness") != "weaker" else 1
    # Prefer explicit clean=True; weaker cleanliness sorts later even if ADX is high.
    if decision.get("cleanliness") == "weaker" or decision.get("clean") is False:
        clean = 1
    else:
        clean = 0
    score = decision.get("strength")
    if score is None:
        score = -1
    adx = (decision.get("hour") or {}).get("adx")
    if adx is None:
        adx = -1
    return (clean, -float(score), -float(adx), str(decision.get("underlying") or ""))


def select_entries(decisions, *, max_names=MAX_NAMES, reserved=None,
                   deployed=None, premium_cap=PREMIUM_CAP_PER_BROKER):
    """Keep at most `max_names` enter actions, strongest first, under the premium cap.

    A name whose lot-size x limit-price, plus this path's already-open premium
    on either broker, would exceed `premium_cap` is skipped and the next-ranked
    still-valid name is tried. `deployed` is a per-broker dict, or the string
    "unverified" to refuse new entries. Decisions with no premium computed
    are not cap-skipped (report previews without a quote).
    """
    reserved = set(reserved or [])
    cap = int(max_names or MAX_NAMES)
    remaining = max(0, cap - len(reserved))
    chosen = []
    out = []
    ordered = [row for row in decisions if row.get("action") == "enter"]
    rest = [row for row in decisions if row.get("action") != "enter"]
    if deployed == "unverified":
        running = "unverified"
    elif isinstance(deployed, dict):
        running = {
            "upstox": float(deployed.get("upstox") or 0),
            "kite": float(deployed.get("kite") or 0),
        }
    else:
        running = {"upstox": 0.0, "kite": 0.0}
    for decision in sorted(ordered, key=rank_key):
        name = decision.get("underlying")
        if name in reserved:
            skip = dict(decision, action="skip", lots=0, quantity=0, send=False, orders_placed=False,
                        reason="already reserved; this path caps at %s names" % cap)
            out.append(skip)
            continue
        if remaining <= 0:
            skip = dict(decision, action="skip", lots=0, quantity=0, send=False, orders_placed=False,
                        reason="not in the top %s still-valid names" % cap)
            out.append(skip)
            continue
        why = premium_cap_reason(decision_premiums(decision), running, cap=premium_cap)
        if why:
            skip = dict(decision, action="skip", lots=0, quantity=0, send=False, orders_placed=False,
                        reason=why)
            out.append(skip)
            continue
        prem = decision_premiums(decision)
        if prem and isinstance(running, dict):
            for broker in ("upstox", "kite"):
                running[broker] = round(running[broker] + prem[broker], 2)
            decision["premium_open"] = {
                "upstox": round(running["upstox"] - prem["upstox"], 2),
                "kite": round(running["kite"] - prem["kite"], 2),
            }
            decision["premium_total"] = dict(running)
            decision["premium_cap"] = premium_cap if premium_cap is not None else PREMIUM_CAP_PER_BROKER
        chosen.append(decision)
        reserved.add(name)
        remaining -= 1
        out.append(decision)
    out.extend(rest)
    return out, chosen


def held_underlyings(positions):
    """Distinct stock-option underlyings, or None if either book is unreadable."""
    if not isinstance(positions, dict) or positions.get("upstox") is None or positions.get("kite") is None:
        return None
    names = set()
    for broker in ("upstox", "kite"):
        for pos in positions[broker] or []:
            if not isinstance(pos, dict):
                return None
            try:
                qty = int(pos.get("quantity") or 0)
            except (TypeError, ValueError):
                return None
            sym = pos.get("trading_symbol") or pos.get("tradingsymbol") or ""
            if qty > 0 and pol.is_stock_option(sym):
                names.add(pol.underlying(sym))
    return names


def name_is_held(positions, underlying):
    found = held_underlyings(positions)
    if found is None:
        return None
    return underlying in found


def read_ledger(path):
    if not path:
        return None
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"names": {}, "days": {}}
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("names"), dict):
        return None
    data.setdefault("days", {})
    return data


def remember_entry(path, name, *, day, contract=None, flatten_at=None, side=None,
                   limit_price=None, premium=None, lot_size=None):
    data = read_ledger(path)
    if data is None:
        return False
    key = str(name).upper()
    day_s = day.isoformat() if hasattr(day, "isoformat") else str(day)
    row = {
        "day": day_s,
        "flatten_at": flatten_at,
        "tag": TAG,
        "source": SOURCE,
        "side": side,
        "trading_symbol": None if not isinstance(contract, dict) else contract.get("trading_symbol"),
    }
    if limit_price is not None:
        row["limit_price"] = limit_price
    if premium is not None:
        row["premium"] = premium
    if lot_size is not None:
        row["lot_size"] = lot_size
    elif isinstance(contract, dict) and contract.get("lot_size"):
        row["lot_size"] = contract.get("lot_size")
    data["names"][key] = row
    days = data.setdefault("days", {})
    bucket = days.setdefault(day_s, [])
    if key not in bucket:
        bucket.append(key)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def entered_today(ledger, today):
    if not isinstance(ledger, dict):
        return set()
    day_s = today.isoformat() if hasattr(today, "isoformat") else str(today)
    names = set()
    for name in (ledger.get("days") or {}).get(day_s) or []:
        names.add(str(name).upper())
    for name, row in (ledger.get("names") or {}).items():
        if isinstance(row, dict) and str(row.get("day") or "")[:10] == day_s:
            names.add(str(name).upper())
    return names


def flatten_orders(now, ledger, positions, session_ok=None):
    """SELL instructions for this path's same-day names only. `send` stays false."""
    plan = same_day_flatten_plan(now, session_ok=session_ok)
    if not plan["due"]:
        return {"due": False, "send": False, "orders": [], "reason": plan["reason"],
                "flatten_at": plan.get("flatten_at")}
    if ledger is None or not isinstance(positions, dict):
        return {"due": True, "send": False, "orders": [], "reason": "flatten book unverified",
                "flatten_at": plan.get("flatten_at")}
    if positions.get("upstox") is None or positions.get("kite") is None:
        return {"due": True, "send": False, "orders": [], "reason": "flatten book unverified",
                "flatten_at": plan.get("flatten_at")}
    clock = _as_ist(now)
    day_s = clock.date().isoformat()
    wanted = set()
    for name, row in (ledger.get("names") or {}).items():
        if not isinstance(row, dict) or row.get("source") != SOURCE:
            continue
        if str(row.get("day") or "")[:10] == day_s:
            wanted.add(str(name).upper())
    for name in (ledger.get("days") or {}).get(day_s) or []:
        wanted.add(str(name).upper())
    if not wanted:
        return {"due": True, "send": False, "orders": [], "reason": "no same-day intraday stock-option ledger names",
                "flatten_at": plan.get("flatten_at")}
    orders = []
    for broker in ("upstox", "kite"):
        for pos in positions.get(broker) or []:
            if not isinstance(pos, dict):
                return {"due": True, "send": False, "orders": [], "reason": "position row unverified",
                        "flatten_at": plan.get("flatten_at")}
            sym = pos.get("trading_symbol") or pos.get("tradingsymbol") or ""
            if not sym or not pol.is_stock_option(sym):
                continue
            try:
                qty = int(pos.get("quantity") or 0)
            except (TypeError, ValueError):
                return {"due": True, "send": False, "orders": [], "reason": "position quantity unverified",
                        "flatten_at": plan.get("flatten_at")}
            if qty <= 0:
                continue
            name = pol.underlying(sym)
            if name not in wanted:
                continue
            product = pos.get("product") or (UPSTOX_PRODUCT if broker == "upstox" else KITE_PRODUCT)
            orders.append({
                "broker": broker,
                "trading_symbol": sym,
                "instrument_token": pos.get("instrument_token") or pos.get("instrument_key"),
                "underlying": name,
                "quantity": qty,
                "transaction_type": "SELL",
                "order_type": "LIMIT",
                "product": product,
                "tag": TAG,
                "source": SOURCE,
                "send": False,
                "reason": plan["reason"],
            })
    return {"due": True, "send": False, "orders": orders, "reason": plan["reason"],
            "flatten_at": plan.get("flatten_at"), "wanted": sorted(wanted)}


def preview_from_report(shortlist, report, *, today=None, results=None, expiry_week=None):
    """Rank still-valid shortlist names from a saved close report. Places nothing."""
    loaded = shortlist if isinstance(shortlist, dict) and "candidates" in shortlist else load_shortlist()
    if not loaded.get("ok"):
        return {"ok": False, "reason": loaded.get("reason"), "valid": [], "chosen": []}
    rows = (report or {}).get("rows") if isinstance(report, dict) else {}
    if isinstance(report, dict) and report.get("ok") is False and not rows:
        return {"ok": False, "reason": report.get("reason") or "report unreadable", "valid": [], "chosen": []}
    drafts = []
    for cand in loaded["candidates"]:
        row = (rows or {}).get(cand["symbol"])
        hour = daily = None
        meta = {}
        extra = dict(cand)
        if row:
            hour, daily, meta = bars_from_report_row(row)
            if meta.get("results_in_window"):
                extra["results_in_window"] = True
        else:
            # No live/report bar: cannot claim the setup still holds.
            drafts.append({
                "underlying": cand["symbol"], "action": "skip", "reason": "no completed 1h/daily bar in the report",
                "side": cand["side"], "lots": 0, "quantity": 0, "send": False, "orders_placed": False,
                "tag": TAG, "source": SOURCE,
            })
            continue
        drafts.append(evaluate_name(
            extra, hour, daily, today=today, chain_count=1, quote=None,
            contract=None, held=False, entered_today=False, books_readable=True,
            results=results, expiry_week=expiry_week))
    selected, chosen = select_entries(drafts, max_names=loaded.get("max_names") or MAX_NAMES)
    return {
        "ok": True,
        "valid": [row for row in selected if row.get("action") == "enter"],
        "chosen": chosen,
        "actions": selected,
        "max_names": loaded.get("max_names") or MAX_NAMES,
    }


def mirror_orders(contract, price, flatten_at):
    """1-lot INTRADAY LIMIT buys, one per broker. Quantity is the full lot."""
    if not isinstance(contract, dict) or price is None:
        return None, None
    try:
        lot = int(contract["lot_size"])
        kite_lot = int(contract.get("kite_lot_size") or lot)
    except (TypeError, ValueError, KeyError):
        return None, None
    if lot <= 0 or kite_lot <= 0:
        return None, None
    qty_u = stock_lots() * lot
    qty_k = stock_lots() * kite_lot
    stamp = flatten_at
    if hasattr(flatten_at, "isoformat"):
        stamp = flatten_at.isoformat()
    common = {
        "trading_symbol": contract.get("trading_symbol"),
        "instrument_token": contract.get("instrument_key"),
        "transaction_type": "BUY",
        "order_type": "LIMIT",
        "price": price,
        "tag": TAG,
        "_initial_entry": True,
        "_intraday": True,
        "_flatten_at": stamp,
        "_paired": True,
    }
    upstox = dict(common, product=UPSTOX_PRODUCT, quantity=qty_u)
    kite = dict(common, product=KITE_PRODUCT, quantity=qty_k,
                trading_symbol=contract.get("kite_symbol") or contract.get("trading_symbol"),
                kite_exchange=contract.get("kite_exchange") or "NFO")
    return upstox, kite
