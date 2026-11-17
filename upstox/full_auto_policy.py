"""Full-auto trading rules authorized by the user on 12 Nov 2026 06:16 IST. Pure: no broker calls.

The user accepted three known risks before turning these on: a buy-fill to stop-ack race,
a possible dual-broker oversell, and exit stops that are not guaranteed to fill.

Switches (environment wins, then full_auto_flags.json beside this file; anything else is OFF):
  FULL_AUTO_ADDS=1         one add per position on winning index options (full_auto_add.py)
  FULL_AUTO_TREND_EXIT=1   1h Supertrend-flip exit + upward-only stop ladder (risk_manager.py, crude_auto.py)
  CRUDE_EXECUTE=1          CRUDEOIL option entries on Kite (crude_auto.py)
  FULL_AUTO_CAPITAL_GATE=0 trade_lock R10 no longer refuses an OPTION buy for a missing
                           verified_capital.json (default ON; t598u 12 Nov 2026 turned it off)
  FULL_AUTO_R10_MTF_CAPITAL=0  trade_lock R10 no longer refuses an equity MTF stock buy for a
                           missing verified capital denominator (default ON; t713u/t714u 17 Nov
                           2026 turned it off). Futures and options are unchanged.

Rules
  Add      at most one per broker position; open profit (LTP > average) and not past the
           +20% R6 line; the completed 1h underlying bar still aligned (CE: close>EMA20 and
           Supertrend green; PE: the opposite); a broker-acknowledged SELL stop at or above
           the average price already live; same lots as the entry, never above 3 lots in total
           (the existing index shape). trade_lock.check_order still re-checks every order.
  Exit     a COMPLETED 1h Supertrend flip against the position, after the position has seen
           the 1h trend on its side at least once. 5m/15m flips are not exits here.
  Stop     upward only. 30% under entry stays the floor. Breakeven once the high is +15%;
           after each +20% target step the floor is the target two steps back
           (first roll: entry, second: entry*1.2, ...). Never lowered.
  Sell     every exit SELL is capped to the quantity the broker reports right now.
"""
import json
import math
import os

D = os.path.dirname(os.path.abspath(__file__))
FLAGS_FILE = os.path.join(D, "full_auto_flags.json")
FLAG_NAMES = {"adds": "FULL_AUTO_ADDS", "trend_exit": "FULL_AUTO_TREND_EXIT", "crude_entry": "CRUDE_EXECUTE",
              "stop_ladder": "FULL_AUTO_STOP_LADDER",  # 12 Nov 09:00: ladder split from the 1h trend exit
              "capital_gate": "FULL_AUTO_CAPITAL_GATE",  # 12 Nov 10:38 (t598u): trade_lock R10 verified-capital gate for options
              "r10_mtf_capital": "FULL_AUTO_R10_MTF_CAPITAL"}  # 17 Nov 11:40 (t713u/t714u): R10 capital check for MTF stock
# Safety gates that are ON unless explicitly switched off ("0" in env, or false in the flags file).
DEFAULT_ON = frozenset({"capital_gate", "r10_mtf_capital"})

DISASTER = 0.30          # option floor under entry, unchanged
STEP = 0.20              # target step, unchanged
BREAKEVEN_AT = 1.15      # high/entry at which the stop moves to entry
ADD_MAX_RUN = 1.20       # trade_lock R6 refuses an add above avg*1.2
INDEX_MAX_LOTS = 3       # index_size_policy shape max (1h routes)
ADDS_PER_POSITION = 1


def flag(name, env=None, flags_file=None):
    """True only when the env var is exactly '1', or the flags file says true and env is unset.

    DEFAULT_ON gates (capital_gate, r10_mtf_capital) are the reverse: ON unless the env var is exactly '0' or the
    flags file says exactly false. A missing/unreadable file or an odd env value keeps them ON."""
    env = os.environ if env is None else env
    var = FLAG_NAMES[name]
    default_on = name in DEFAULT_ON
    raw = env.get(var)
    if raw is not None and str(raw).strip() != "":
        if default_on:
            return str(raw).strip() != "0"
        return str(raw).strip() == "1"
    try:
        data = json.load(open(flags_file or FLAGS_FILE))
    except Exception:
        return default_on
    if not isinstance(data, dict):
        return default_on
    if default_on:
        return data.get(name) is not False
    return data.get(name) is True


# ---------------- trend ----------------
def aligned(side, bar):
    """True/False for a completed bar, None when the bar is unusable."""
    if not bar:
        return None
    c, e, d = bar.get("close"), bar.get("ema20"), bar.get("dir")
    if c is None or e is None or d not in (1, -1):
        return None
    if side == "CE":
        return bool(c > e and d == 1)
    if side == "PE":
        return bool(c < e and d == -1)
    return None


def st_against(side, bar):
    """Supertrend direction alone against the position (the flip). None when unusable."""
    if not bar or bar.get("dir") not in (1, -1):
        return None
    if side == "CE":
        return bar["dir"] == -1
    if side == "PE":
        return bar["dir"] == 1
    return None


def trend_exit_1h(side, bar, state):
    """Decide the 1h flip exit. `state` is the per-position dict kept in risk_state.json.

    Mutates state["h1_seen_aligned"] / state["h1_last_ts"]. Returns (exit: bool, why: str).
    The exit needs the position to have had Supertrend on its side once, so a position
    opened against the 1h trend is not sold on its first look (its fixed stop still protects it).
    """
    if side not in ("CE", "PE"):
        return False, "not an option side"
    if not bar or bar.get("forming"):
        return False, "1h bar missing or forming"
    against = st_against(side, bar)
    if against is None:
        return False, "1h bar unusable"
    state["h1_last_ts"] = bar.get("ts") or bar.get("last_ts")
    if not against:
        state["h1_seen_aligned"] = True
        return False, "1h Supertrend still with the position"
    if state.get("h1_seen_aligned"):
        return True, "completed 1h Supertrend flipped against the position"
    return False, "1h Supertrend against, but never seen aligned while held; fixed stop only"


# ---------------- stops ----------------
def ladder_floor(entry, high, step=STEP, breakeven_at=BREAKEVEN_AT):
    """Upward stop floor from the entry and the highest LTP seen. 0 when nothing applies."""
    try:
        entry = float(entry); high = float(high)
    except (TypeError, ValueError):
        return 0.0
    if entry <= 0 or high <= 0:
        return 0.0
    floor = 0.0
    if high >= entry * breakeven_at:
        floor = entry
    rolls = int(math.floor(((high / entry) - 1.0) / step + 1e-9))
    if rolls >= 1:
        floor = max(floor, entry * (1.0 + step * (rolls - 1)))
    return floor


def ratchet(existing, candidate, entry, disaster=DISASTER):
    """Never below the disaster floor, never below the existing stop."""
    floor = float(entry) * (1.0 - disaster)
    vals = [floor]
    if existing:
        vals.append(float(existing))
    if candidate:
        vals.append(float(candidate))
    return max(vals)


# ---------------- oversell guard ----------------
def capped_sell(requested, broker_qty):
    """Quantity to sell: never more than the broker reports right now. 0 means do not send."""
    try:
        r = int(requested); b = int(broker_qty)
    except (TypeError, ValueError):
        return 0
    if r <= 0 or b <= 0:
        return 0
    return min(r, b)


# ---------------- adds ----------------
def add_decision(*, side, avg, ltp, held_qty, lot_size, adds_used, bar_1h, live_stop_trigger,
                 days_to_expiry, max_lots=INDEX_MAX_LOTS):
    """-> dict(ok, lots, qty, reason). Pure."""
    def no(reason):
        return {"ok": False, "lots": 0, "qty": 0, "reason": reason}
    try:
        avg = float(avg); ltp = float(ltp); held_qty = int(held_qty); lot_size = int(lot_size)
    except (TypeError, ValueError):
        return no("price, quantity or lot size unreadable")
    if side not in ("CE", "PE"):
        return no("not an option")
    if lot_size <= 0 or held_qty <= 0 or held_qty % lot_size:
        return no("held quantity is not a whole number of lots")
    if int(adds_used or 0) >= ADDS_PER_POSITION:
        return no("this position already had its one add")
    if days_to_expiry is None or days_to_expiry < 2:
        return no("contract expires within 2 days; no add")
    if not (ltp > avg):
        return no("not in profit")
    if ltp > avg * ADD_MAX_RUN:
        return no("more than +20% above average; trade_lock R6 refuses adds there")
    ok = aligned(side, bar_1h)
    if ok is None:
        return no("completed 1h bar unreadable")
    if not ok:
        return no("completed 1h trend no longer aligned")
    if live_stop_trigger is None:
        return no("no broker-acknowledged SELL stop is live")
    if float(live_stop_trigger) < avg - 1e-9:
        return no("live stop %.2f is below breakeven %.2f; move the stop first" % (float(live_stop_trigger), avg))
    held_lots = held_qty // lot_size
    lots = min(held_lots, max_lots - held_lots)
    if lots < 1:
        return no("already at the %d-lot cap" % max_lots)
    return {"ok": True, "lots": lots, "qty": lots * lot_size,
            "reason": "add %d lot%s: in profit, 1h aligned, stop at/above breakeven" % (lots, "" if lots == 1 else "s")}


# ---------------- crude ----------------
CRUDE = "CRUDEOIL"
CRUDE_MIN_DAYS = 8          # MCX square-off closes options within 7 days of expiry
CRUDE_MAX_SPREAD = 0.03     # (ask-bid)/mid
CRUDE_STRIKE_STEP = 50
CRUDE_MAX_ENTRIES_PER_DAY = 2


def crude_fresh_signal(cur, prev):
    """Fresh 1h trigger on completed bars: CE/PE or None."""
    a_ce, a_pe = aligned("CE", cur), aligned("PE", cur)
    if a_ce and not aligned("CE", prev):
        return "CE"
    if a_pe and not aligned("PE", prev):
        return "PE"
    return None


def pick_crude_expiry(expiries, today, min_days=CRUDE_MIN_DAYS):
    """Nearest monthly option expiry at least min_days away (dates)."""
    ok = sorted(e for e in set(expiries) if (e - today).days >= min_days)
    return ok[0] if ok else None


def atm(spot, step=CRUDE_STRIKE_STEP):
    return int(round(float(spot) / step) * step)


def liquid(bid, ask, ltp, bid_qty=None, ask_qty=None, lot=None, max_spread=CRUDE_MAX_SPREAD):
    """(ok, why) for a quoted option."""
    try:
        bid = float(bid or 0); ask = float(ask or 0); ltp = float(ltp or 0)
    except (TypeError, ValueError):
        return False, "quote unreadable"
    if bid <= 0 or ask <= 0 or ltp <= 0:
        return False, "no two-sided quote"
    if ask < bid:
        return False, "crossed quote"
    mid = (ask + bid) / 2.0
    if (ask - bid) / mid > max_spread:
        return False, "spread %.1f%% > %.0f%%" % ((ask - bid) / mid * 100, max_spread * 100)
    if lot and ask_qty is not None and int(ask_qty or 0) < int(lot):
        return False, "ask size below one lot"
    return True, "liquid"
