"""Supertrend-versus-EMA weekend watchlist decisions. Sends nothing.

The list is monitor_scope.json field st_ema_category_watchlist, at most 5
names. A first entry is allowed on either completed 1-hour path:

  * a fresh trigger (index_entry_logic.baseline_signal): the prior bar was not
    already aligned, with MACD histogram in the trade direction and the shared
    ADX floor;
  * a later join of a trend that is already aligned on the last completed bar:
    calls when the uptrend has Supertrend above EMA20, puts when the downtrend
    has Supertrend below EMA20. Equal values do not qualify.

Already aligned is not a reason to skip. A fresh trigger is not removed or
downgraded. A name already held, or already entered this week on this list,
is not entered again and is not added to. One PSU bank qualifies at a time;
the clearer Supertrend-versus-EMA separation wins. Remaining names are tried
strongest separation first, inside the existing distinct-underlying cap.

Calls in an uptrend, puts in a downtrend. Size is 1 lot per broker. The index
2-lot one-shot and the index Supertrend-versus-EMA double are not applied.
The 5-6 Nov 2026 stock-option 2-lot grant is expired and is not applied.
Adds are not decided here. Names outside the list are rejected and nothing is
substituted. A missing chain, unreadable liquidity, unreadable results
calendar, or a name with results due is a recorded skip.

A trade taken from the weekly list must be flat by that Friday's close
(decision due from 15:10 IST). This does not change the results-day-before
exit or the expiry square-off.

t689u: on each entry-routine run, before names are evaluated, a completed 1h
bar that closes against both EMA20 and Supertrend for that stock's watched
direction (long: close below EMA20 and Supertrend bearish; short: the
opposite) removes the name from st_ema_category_watchlist. Open positions
are not closed. Removals are logged. A dry run reports but does not write.
"""
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import index_entry_logic as iel
import stockopt_policy as pol
import stockopt_spread as ssp

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TAG = "stockopt_tech"
SOURCE = "st_ema_week"
MAX_NAMES = 5
LOTS = 1
ADX_MIN = iel.ADX_MIN
ENTRY_OPEN = dt.time(10, 30)
ENTRY_CLOSE = dt.time(14, 30)
FLATTEN_AT = dt.time(15, 10)
SPREAD_MAX = 0.10
ENABLE_ENV = "STOCKOPT_ST_EMA_EXECUTE"
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9&-]*$")
# Public-sector banks. At most one of these may be entered from one pass.
# This week's list contains SBIN, BANKBARODA and PNB.
PSU_BANKS = frozenset({
    "SBIN", "BANKBARODA", "PNB", "UNIONBANK", "CANBK", "BANKINDIA",
    "INDIANB", "MAHABANK", "CENTRALBK", "UCOBANK", "PSB", "IOB",
})


def execute_enabled(environ=None):
    """True only when the scoped path is explicitly switched on. Default off."""
    env = os.environ if environ is None else environ
    return str(env.get(ENABLE_ENV, "")).strip() == "1"


def stock_lots(when=None):
    """1 lot per broker for this path.

    equity/autonomy.py grants STOCK_OPT_MAX_LOTS=2 only on 2026-11-05 and
    2026-11-06. That grant is not active after 6 Nov 2026 and is not applied
    here. Index pending_first_lots and index Supertrend-versus-EMA doubling
    are not read.
    """
    del when
    return LOTS


def normalize_watchlist(names, limit=MAX_NAMES):
    """(names, dropped, error). Order is kept. Past `limit` is not a candidate."""
    if not isinstance(names, (list, tuple)):
        return [], [], "st_ema_category_watchlist is not a list"
    kept, dropped, seen = [], [], set()
    for raw in names:
        text = str(raw or "").strip().upper()
        if not text or not _SYMBOL.match(text):
            dropped.append({"symbol": raw, "reason": "unreadable symbol"})
            continue
        if text in seen:
            continue
        if len(kept) >= limit:
            dropped.append({"symbol": text, "reason": "above max %s" % limit})
            continue
        seen.add(text)
        kept.append(text)
    return kept, dropped, None


def resolve_flatten_by(week_trading_days):
    """The Friday of this weekly list. Missing Friday refuses a carry."""
    fridays = []
    for raw in week_trading_days or []:
        try:
            day = dt.date.fromisoformat(str(raw)[:10])
        except ValueError:
            return {"ok": False, "date": None, "reason": "week trading day unreadable"}
        if day.weekday() == 4:
            fridays.append(day)
    if not fridays:
        return {"ok": False, "date": None, "reason": "week has no Friday; a weekly-list trade cannot be carried"}
    return {"ok": True, "date": fridays[-1], "reason": None}


def prepare_scope(scope):
    """Watchlist, dropped extras, and the Friday the list must be flat."""
    if not isinstance(scope, dict):
        return {"ok": False, "names": [], "dropped": [], "flatten_by": None,
                "reason": "monitor scope unreadable"}
    names, dropped, err = normalize_watchlist(scope.get("st_ema_category_watchlist"))
    if err:
        return {"ok": False, "names": [], "dropped": dropped, "flatten_by": None, "reason": err}
    if not names:
        return {"ok": False, "names": [], "dropped": dropped, "flatten_by": None,
                "reason": "st_ema watchlist is empty; the liquid universe is not a fallback"}
    flat = resolve_flatten_by(scope.get("week_trading_days"))
    if not flat["ok"]:
        return {"ok": False, "names": names, "dropped": dropped, "flatten_by": None,
                "reason": flat["reason"]}
    return {"ok": True, "names": names, "dropped": dropped, "flatten_by": flat["date"],
            "reason": None, "week_trading_days": list(scope.get("week_trading_days") or [])}


def load_scope(path):
    try:
        with open(path) as handle:
            scope = json.load(handle)
    except Exception as exc:
        return {"ok": False, "names": [], "dropped": [], "flatten_by": None,
                "reason": "monitor scope unreadable: %s" % type(exc).__name__}
    loaded = prepare_scope(scope)
    loaded["scope"] = scope if isinstance(scope, dict) else None
    return loaded


def _num(bar, key):
    try:
        value = float(bar[key])
    except (KeyError, TypeError, ValueError):
        return None
    if value != value:
        return None
    return value


def _trend_side(close, ema, direction):
    """Uptrend calls, downtrend puts. Same close/EMA20/Supertrend-direction test as the baseline."""
    if close is None or ema is None or direction is None:
        return None
    if close > ema and direction == 1:
        return "CE"
    if close < ema and direction == -1:
        return "PE"
    return None


def fresh_signal(bar, adx_min=ADX_MIN):
    """Shared baseline on one completed bar plus its prior completed bar.

    Returns a side only when the cross is fresh. The caller still considers an
    already-aligned join; this helper does not downgrade that path, and a
    missing prior bar is unverified rather than treated as fresh.
    """
    required = ("close", "ema20", "dir", "prev_close", "prev_ema", "prev_dir", "hist", "adx")
    if not isinstance(bar, dict) or any(bar.get(key) is None for key in required):
        return {"side": None, "trend": None, "fresh": False, "path": "fresh", "why": [],
                "reason": "completed bar or prior bar unverified"}
    try:
        sig, trend, why = iel.baseline_signal(bar, adx_min=adx_min)
    except Exception as exc:
        return {"side": None, "trend": None, "fresh": False, "path": "fresh", "why": [],
                "reason": "signal unverified: %s" % type(exc).__name__}
    why = list(why or [])
    if not sig:
        reason = "; ".join(why) if why else "not a fresh 1h signal"
        return {"side": None, "trend": trend, "fresh": False, "path": "fresh", "why": why, "reason": reason}
    return {"side": sig, "trend": trend, "fresh": True, "path": "fresh", "why": why,
            "reason": "; ".join(why) if why else "fresh 1h signal"}


def aligned_join(bar):
    """First entry into a trend that is already aligned on the last completed bar.

    The prior completed bar must already be on the same side. Calls need
    Supertrend above EMA20. Puts need Supertrend below EMA20. Equal values
    do not qualify. This is not an add.
    """
    required = ("close", "ema20", "dir", "st", "prev_close", "prev_ema", "prev_dir")
    if not isinstance(bar, dict) or any(bar.get(key) is None for key in required):
        return {"side": None, "fresh": False, "path": "aligned", "established": False,
                "reason": "completed bar unverified for an aligned join"}
    close, ema, direction = _num(bar, "close"), _num(bar, "ema20"), _num(bar, "dir")
    prev_close, prev_ema, prev_dir = _num(bar, "prev_close"), _num(bar, "prev_ema"), _num(bar, "prev_dir")
    st_v = _num(bar, "st")
    if None in (close, ema, direction, prev_close, prev_ema, prev_dir, st_v):
        return {"side": None, "fresh": False, "path": "aligned", "established": False,
                "reason": "completed bar unverified for an aligned join"}
    current = _trend_side(close, ema, direction)
    previous = _trend_side(prev_close, prev_ema, prev_dir)
    if current is None or previous != current:
        return {"side": None, "fresh": False, "path": "aligned", "established": False,
                "reason": "1h trend is not already aligned"}
    if current == "CE" and st_v > ema:
        return {"side": "CE", "fresh": False, "path": "aligned", "established": True,
                "reason": "already aligned uptrend; Supertrend above EMA20"}
    if current == "PE" and st_v < ema:
        return {"side": "PE", "fresh": False, "path": "aligned", "established": True,
                "reason": "already aligned downtrend; Supertrend below EMA20"}
    return {"side": None, "fresh": False, "path": "aligned", "established": True,
            "reason": "already aligned, but Supertrend is not beyond EMA20"}


def entry_signal(bar, adx_min=ADX_MIN):
    """Fresh trigger first. An already-aligned join is the other first-entry path.

    A fresh signal is returned as-is. It is not replaced by the join. A bar
    that is already aligned can still be a first entry when Supertrend is
    strictly beyond EMA20.
    """
    fresh = fresh_signal(bar, adx_min=adx_min)
    if fresh.get("side"):
        return fresh
    joined = aligned_join(bar)
    if joined.get("side"):
        return joined
    if joined.get("established"):
        return {"side": None, "trend": fresh.get("trend"), "fresh": False, "path": None,
                "why": fresh.get("why") or [], "reason": joined["reason"]}
    return fresh


def separation(bar, side):
    """Supertrend-versus-EMA gap as a fraction of EMA20. Larger is clearer.

    Calls use Supertrend minus EMA20. Puts use EMA20 minus Supertrend.
    Missing or equal values are not a positive gap.
    """
    if not isinstance(bar, dict) or side not in ("CE", "PE"):
        return None
    st_v, ema = _num(bar, "st"), _num(bar, "ema20")
    if st_v is None or ema is None or ema == 0:
        return None
    gap = (st_v - ema) if side == "CE" else (ema - st_v)
    return gap / abs(ema)


def results_skip(name, status):
    """None when the name is clear. Unreadable calendar fails closed."""
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
    """Poor or unverifiable option liquidity. A missing field does not pass."""
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


def _as_ist(now):
    if not isinstance(now, dt.datetime):
        return None
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def entry_window(now):
    clock = _as_ist(now)
    if clock is None:
        return {"ok": False, "reason": "clock unverified"}
    if clock.weekday() >= 5:
        return {"ok": False, "reason": "not a weekday"}
    if not (ENTRY_OPEN <= clock.time() <= ENTRY_CLOSE):
        return {"ok": False, "reason": "outside 10:30-14:30 IST entry window"}
    return {"ok": True, "reason": "inside 10:30-14:30 IST"}


def friday_flatten_plan(now, flatten_by):
    """Weekly-list trades are flat by Friday's close. This plans; it does not sell.

    Due from 15:10 IST on that Friday. A missing Friday or clock does not allow
    the position to be carried.
    """
    if flatten_by is None:
        return {"due": False, "overdue": False, "carry": False, "send": False,
                "flatten_by": None, "reason": "Friday close is unverified; do not carry a weekly-list trade"}
    if isinstance(flatten_by, str):
        try:
            flatten_by = dt.date.fromisoformat(flatten_by[:10])
        except ValueError:
            return {"due": False, "overdue": False, "carry": False, "send": False,
                    "flatten_by": None, "reason": "Friday close is unverified; do not carry a weekly-list trade"}
    clock = _as_ist(now)
    if clock is None:
        return {"due": False, "overdue": False, "carry": False, "send": False,
                "flatten_by": flatten_by.isoformat(), "reason": "clock unverified"}
    due_at = dt.datetime.combine(flatten_by, FLATTEN_AT, IST)
    if clock >= due_at:
        return {"due": True, "overdue": clock.date() > flatten_by, "carry": False, "send": False,
                "flatten_by": flatten_by.isoformat(),
                "reason": "weekly-list stock option must be closed by Friday's close"}
    return {"due": False, "overdue": False, "carry": True, "send": False,
            "flatten_by": flatten_by.isoformat(),
            "reason": "hold until 15:10 IST on %s" % flatten_by.isoformat()}


def _blank(name):
    return {"underlying": str(name or "").strip().upper(), "action": "reject", "reason": "",
            "side": None, "lots": 0, "quantity": 0, "send": False, "orders_placed": False,
            "tag": TAG, "source": SOURCE, "add": False}


def plan_name(name, watchlist, bar=None, *, chain_count=None, quote=None, results=None,
              contract=None, held=False, entered_this_week=False, books_readable=True):
    """One name. `send` stays false. Off-list names are not replaced.

    Fresh and already-aligned are both first entries. A held name, or a name
    already entered this week on this list, is not entered again from the
    other path and is not added to.
    """
    decision = _blank(name)
    names = [str(item).strip().upper() for item in (watchlist or [])]
    if decision["underlying"] not in names:
        decision["reason"] = "off watchlist; no fallback name"
        return decision
    if books_readable is not True:
        decision["action"] = "skip"
        decision["reason"] = "broker book unverified"
        return decision
    if held or entered_this_week:
        decision["action"] = "skip"
        decision["reason"] = "already open or already entered this week; adds are off"
        return decision
    skip = results_skip(decision["underlying"], results)
    if skip:
        decision["action"] = "skip"
        decision["reason"] = skip
        return decision
    skip = chain_skip(chain_count)
    if skip:
        decision["action"] = "skip"
        decision["reason"] = skip
        return decision
    signal = entry_signal(bar)
    decision["signal"] = signal
    decision["path"] = signal.get("path")
    decision["fresh"] = bool(signal.get("fresh"))
    if not signal["side"]:
        decision["action"] = "skip"
        decision["reason"] = signal["reason"]
        return decision
    decision["side"] = signal["side"]
    decision["separation"] = separation(bar, signal["side"])
    if not isinstance(contract, dict):
        decision["action"] = "skip"
        decision["reason"] = "no ATM contract"
        return decision
    skip = liquidity_skip(quote)
    if skip:
        decision["action"] = "skip"
        decision["reason"] = skip
        return decision
    try:
        lot = int(contract.get("lot_size") or 0)
    except (TypeError, ValueError):
        lot = 0
    if lot <= 0:
        decision["action"] = "skip"
        decision["reason"] = "lot size unverified"
        return decision
    lots = stock_lots()
    decision.update(action="enter", reason=signal["reason"], lots=lots, lot_size=lot,
                    quantity=lots * lot, send=False, orders_placed=False, add=False)
    return decision


def _separation_value(decision):
    try:
        return float(decision.get("separation"))
    except (TypeError, ValueError):
        return float("-inf")


def _skip_copy(decision, reason):
    copied = dict(decision)
    copied.update(action="skip", lots=0, quantity=0, send=False, orders_placed=False, add=False, reason=reason)
    return copied


def select_entries(decisions, *, open_count=0, max_underlyings=None, reserved=None):
    """One row per name, one PSU bank, strongest separation first within the name cap.

    `open_count` is distinct stock-option underlyings already reserved on the
    fuller broker. None means that count is unverified and no new name is sent.
    `reserved` is names already held or already entered this week. A PSU bank
    in that set blocks every other PSU bank. A duplicate of the same name
    (fresh plus aligned) keeps a single first entry.
    """
    if max_underlyings is None:
        max_underlyings = pol.MAX_UNDERLYINGS
    reserved_names = {str(name).strip().upper() for name in (reserved or []) if str(name).strip()}
    reserved_psu = reserved_names & PSU_BANKS
    others = [row for row in decisions or [] if row.get("action") != "enter"]
    enters = [row for row in decisions or [] if row.get("action") == "enter"]
    best = {}
    duplicates = []
    for row in enters:
        name = row.get("underlying")
        current = best.get(name)
        if current is None:
            best[name] = row
            continue
        new_sep = _separation_value(row)
        old_sep = _separation_value(current)
        prefer_new = new_sep > old_sep or (new_sep == old_sep and row.get("path") == "fresh" and current.get("path") != "fresh")
        if prefer_new:
            duplicates.append(current)
            best[name] = row
        else:
            duplicates.append(row)
    ranked = sorted(best.values(), key=lambda row: (-_separation_value(row), row.get("underlying") or ""))
    psu_winner = None
    kept = []
    sector_skips = []
    for row in ranked:
        if row.get("underlying") in PSU_BANKS:
            if reserved_psu and row.get("underlying") not in reserved_psu:
                blocked = sorted(reserved_psu)[0]
                sector_skips.append(_skip_copy(
                    row, "PSU bank sector cap; %s is already entered this week" % blocked))
            elif psu_winner is None:
                psu_winner = row
                kept.append(row)
            else:
                sector_skips.append(_skip_copy(
                    row, "PSU bank sector cap; %s has the clearer Supertrend-versus-EMA separation" % psu_winner.get("underlying")))
        else:
            kept.append(row)
    kept.sort(key=lambda row: (-_separation_value(row), row.get("underlying") or ""))
    cap_skips = []
    if open_count is None:
        cap_skips = [_skip_copy(row, "distinct underlying count unverified") for row in kept]
        kept = []
    else:
        try:
            room = int(max_underlyings) - int(open_count)
        except (TypeError, ValueError):
            room = -1
        if room < 0:
            cap_skips = [_skip_copy(row, "distinct underlying count unverified") for row in kept]
            kept = []
        else:
            overflow = kept[room:]
            kept = kept[:room]
            cap_skips = [_skip_copy(row, "ranked below the names that fit the distinct-underlying cap") for row in overflow]
    duplicate_skips = [_skip_copy(row, "already selected this week from the other path; adds are off") for row in duplicates]
    return kept + sector_skips + cap_skips + duplicate_skips + others


def limit_price(quote, tick_fn):
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


def read_ledger(path):
    """Empty ledger if the file is absent. None if it cannot be trusted."""
    if not path:
        return None
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"names": {}}
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("names"), dict):
        return None
    return data


def remember_entry(path, name, flatten_by):
    """Record a filled weekly-list name so Friday flatten can find it."""
    data = read_ledger(path)
    if data is None:
        return False
    day = flatten_by.isoformat() if hasattr(flatten_by, "isoformat") else str(flatten_by)
    data["names"][str(name).upper()] = {"flatten_by": day, "tag": TAG, "source": SOURCE}
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


def flatten_orders(now, flatten_by, ledger, positions):
    """Sell instructions for ledger names only. `send` stays false.

    An unreadable ledger or book produces no orders. Other underlyings,
    including crude and names that are merely open, are not selected.
    """
    plan = friday_flatten_plan(now, flatten_by)
    if not plan["due"]:
        return {"due": False, "send": False, "orders": [], "reason": plan["reason"]}
    if ledger is None or not isinstance(positions, dict):
        return {"due": True, "send": False, "orders": [], "reason": "flatten book unverified"}
    if positions.get("upstox") is None or positions.get("kite") is None:
        return {"due": True, "send": False, "orders": [], "reason": "flatten book unverified"}
    wanted = set()
    for name, row in (ledger.get("names") or {}).items():
        if not isinstance(row, dict) or row.get("source") != SOURCE:
            continue
        own = friday_flatten_plan(now, row.get("flatten_by") or flatten_by)
        if own["due"]:
            wanted.add(str(name).upper())
    rows = []
    for broker in ("upstox", "kite"):
        for pos in positions.get(broker) or []:
            if not isinstance(pos, dict):
                return {"due": True, "send": False, "orders": [], "reason": "position row unverified"}
            sym = (pos.get("trading_symbol") or pos.get("tradingsymbol") or "")
            if not sym or not pol.is_stock_option(sym):
                continue
            try:
                qty = int(pos.get("quantity") or 0)
            except (TypeError, ValueError):
                return {"due": True, "send": False, "orders": [], "reason": "position quantity unverified"}
            if qty == 0:
                continue
        # Parse expiry/side/strike so inferred debit-spread pairs close short first
        # even when the spread ledger is empty.
        rows.extend(ssp.legs_from_positions(positions.get(broker) or [], broker))
    try:
        spread_ledger = ssp.load_ledger()
    except Exception:
        spread_ledger = {}
    legs = ssp.flatten_leg_order(rows, wanted, ledger=spread_ledger)
    orders = []
    for leg in legs:
        orders.append({
            "broker": leg.get("broker"), "trading_symbol": leg.get("trading_symbol") or leg.get("sym"),
            "underlying": leg.get("underlying"), "quantity": int(leg.get("quantity") or leg.get("qty") or 0),
            "transaction_type": leg.get("transaction_type") or "SELL", "order_type": "LIMIT",
            "tag": TAG, "source": SOURCE, "send": False, "role": leg.get("role"),
            "sequence": leg.get("sequence") or 2,
            "reason": leg.get("reason") or "close by Friday's close",
        })
    return {"due": True, "send": False, "orders": orders, "reason": plan["reason"]}


REMOVAL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "st_ema_watchlist_removals.jsonl")
REPORTS_DIR = os.path.join(os.path.dirname(ROOT), "reports")
WATCHLIST_FIELDS = ("st_ema_category_watchlist", "stock_option_underlyings")


def normalize_direction(value):
    """Map stored or scanned direction to 'long' or 'short'."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and value == value:
        if value > 0:
            return "long"
        if value < 0:
            return "short"
        return None
    text = str(value).strip().lower()
    if text in ("long", "up", "uptrend", "ce", "call", "1", "+1", "bull", "bullish"):
        return "long"
    if text in ("short", "down", "downtrend", "pe", "put", "-1", "bear", "bearish"):
        return "short"
    return None


def _scan_direction(row):
    if not isinstance(row, dict):
        return None, None
    name = str(row.get("sym") or row.get("symbol") or row.get("underlying") or "").strip().upper()
    if not name:
        return None, None
    if "hourly" in row and isinstance(row["hourly"], dict):
        raw = row["hourly"].get("dir") or row["hourly"].get("side")
    else:
        raw = row.get("dir") or row.get("side") or row.get("direction") or row.get("watched_direction")
    return name, normalize_direction(raw)


def load_watched_directions(scope=None, extra=None, reports_dir=None):
    """Watched direction per symbol. Scope map wins, then caller extras, then scan files."""
    out = {}
    def take(name, direction):
        d = normalize_direction(direction)
        if name and d and str(name).strip().upper() not in out:
            out[str(name).strip().upper()] = d
    # caller extras first? User-persisted scope should win over a stale scan.
    if isinstance(scope, dict):
        stored = scope.get("st_ema_watched_direction") or {}
        if isinstance(stored, dict):
            for key, value in stored.items():
                take(key, value)
    if isinstance(extra, dict):
        for key, value in extra.items():
            if str(key).strip().upper() not in out:
                take(key, value)
    reports_dir = reports_dir or REPORTS_DIR
    week = None
    if isinstance(scope, dict):
        days = [str(d)[:10] for d in (scope.get("week_trading_days") or [])]
        week = days[0] if days else None
    candidates = []
    if week:
        candidates.append(os.path.join(reports_dir, "weekly_st_ema_watchlist_%s.json" % week))
    try:
        import glob
        candidates.extend(sorted(glob.glob(os.path.join(reports_dir, "st_ema_scan_*.json")), reverse=True))
        candidates.extend(sorted(glob.glob(os.path.join(reports_dir, "weekly_st_ema_watchlist_*.json")), reverse=True))
    except Exception:
        pass
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path) as handle:
                data = json.load(handle)
        except Exception:
            continue
        rows = data
        if isinstance(data, dict):
            rows = data.get("lists") or data.get("qualified") or data.get("symbols") or data.get("rows") or []
            if isinstance(data.get("st_ema_watched_direction"), dict):
                for key, value in data["st_ema_watched_direction"].items():
                    take(key, value)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, str):
                    continue
                name, direction = _scan_direction(row)
                if name and direction:
                    take(name, direction)
    return out


def trend_broken(bar, direction):
    """True when a completed 1h bar closes against both EMA20 and Supertrend.

    Long / uptrend watch: close below EMA20 and Supertrend bearish (dir < 0).
    Short / downtrend watch: close above EMA20 and Supertrend bullish (dir > 0).
    Equal closes are not a break. Missing fields are unverified, not a break.
    """
    want = normalize_direction(direction)
    if want is None:
        return False, "watched direction unverified"
    if not isinstance(bar, dict):
        return False, "completed 1h bar unverified"
    close, ema, st_dir = _num(bar, "close"), _num(bar, "ema20"), _num(bar, "dir")
    if None in (close, ema, st_dir):
        return False, "completed 1h bar unverified"
    if want == "long":
        if close < ema and st_dir < 0:
            return True, "completed 1h close below EMA20 with Supertrend bearish (watched long)"
        return False, None
    if close > ema and st_dir > 0:
        return True, "completed 1h close above EMA20 with Supertrend bullish (watched short)"
    return False, None


def append_removal_log(rows, path=None):
    path = path or REMOVAL_LOG
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
    return path


def save_scope(scope, path):
    tmp = path + ".part"
    with open(tmp, "w") as handle:
        json.dump(scope, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def prune_broken_watchlist(scope, bars, now, *, persist=False, scope_path=None,
                           log_path=None, directions=None, reports_dir=None):
    """Remove names whose completed 1h bar has broken the watched trend.

    Mutates `scope` in memory for the rest of the run. Writes the scope file
    and the removal log only when persist is True. Does not close positions.
    Saturday weekly cap reports are not rewritten: they do not feed the
    ST/EMA entry list (that list comes from the 1h ST-versus-EMA scan).
    Names are also dropped from stock_option_underlyings when present, so
    the broader monitor list stays in sync.
    """
    result = {"removed": [], "kept": [], "unverified": [], "persisted": False,
              "log_path": None, "directions": {}}
    if not isinstance(scope, dict):
        result["reason"] = "monitor scope unreadable"
        return result
    names, dropped, err = normalize_watchlist(scope.get("st_ema_category_watchlist"))
    if err:
        result["reason"] = err
        return result
    direction_map = load_watched_directions(scope, extra=directions, reports_dir=reports_dir)
    result["directions"] = dict(direction_map)
    clock = _as_ist(now)
    stamp = clock.isoformat() if clock else None
    remaining = []
    removed = []
    for name in names:
        bar = None if not isinstance(bars, dict) else bars.get(name)
        direction = direction_map.get(name)
        broken, why = trend_broken(bar, direction)
        if direction is None:
            result["unverified"].append({"symbol": name, "reason": why or "watched direction unverified"})
            remaining.append(name)
            continue
        if not isinstance(bar, dict) or why == "completed 1h bar unverified":
            result["unverified"].append({"symbol": name, "direction": direction,
                                         "reason": "completed 1h bar unverified"})
            remaining.append(name)
            continue
        if broken:
            row = {"symbol": name, "direction": direction, "reason": why, "removed_at": stamp,
                   "close": None if not isinstance(bar, dict) else bar.get("close"),
                   "ema20": None if not isinstance(bar, dict) else bar.get("ema20"),
                   "dir": None if not isinstance(bar, dict) else bar.get("dir"),
                   "bar_ts": None if not isinstance(bar, dict) else bar.get("last_ts") or bar.get("ts"),
                   "open_positions_untouched": True}
            removed.append(row)
        else:
            remaining.append(name)
            result["kept"].append({"symbol": name, "direction": direction})
    result["removed"] = removed
    scope["st_ema_category_watchlist"] = remaining
    # keep the broader monitor list in sync, without touching names that were
    # never on the ST/EMA entry list (e.g. HDFCLIFE / CHOLAFIN from cap lists).
    dropped_names = {row["symbol"] for row in removed}
    if dropped_names and isinstance(scope.get("stock_option_underlyings"), list):
        scope["stock_option_underlyings"] = [str(n).strip().upper() for n in scope["stock_option_underlyings"]
                                             if str(n).strip().upper() not in dropped_names]
    # persist known directions so later runs do not depend on the weekend scan file
    stored = dict(scope.get("st_ema_watched_direction") or {})
    for name, direction in direction_map.items():
        stored.setdefault(name, direction)
    if stored:
        scope["st_ema_watched_direction"] = stored
    if persist and removed and (scope_path or log_path):
        if scope_path:
            try:
                save_scope(scope, scope_path)
                result["persisted"] = True
            except Exception as exc:
                result["persist_error"] = type(exc).__name__
        try:
            result["log_path"] = append_removal_log(removed, log_path or REMOVAL_LOG)
        except Exception as exc:
            result["log_error"] = type(exc).__name__
    elif persist and stored and scope_path and not result.get("persist_error"):
        # seed directions even when nothing was removed, so the next run is self-contained
        try:
            save_scope(scope, scope_path)
            result["directions_persisted"] = True
        except Exception as exc:
            result["persist_error"] = type(exc).__name__
    return result
