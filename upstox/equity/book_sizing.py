#!/usr/bin/env python3
"""Capital- and conditions-driven position sizing across the three option/MTF books (4 Nov 2026, t336u).

User (4 Nov 2026): "Based on the capital and the market conditions decide lot sizes instead of fixed size
for each type of index option, stock option, stock mtf. Which can give better returns allocate funds to that."

WHAT THIS IS
  A pure sizing library. It answers "how many lots / shares may this single trade be", from
   * the rupees actually at risk on one lot (the 30% stop under entry that risk_manager enforces),
   * free capital,
   * market conditions (ADX tier, daily trend agreement, expiry day),
   * and how well each book has historically earned the capital it used (book_weights).
  It places NO orders and writes NO state. Every hard cap stays in trade_lock.py, which still runs
  downstream on every order; this module never widens those caps, it only aims inside them.

THE ARITHMETIC (index options)
  risk_budget   = free_capital x BASE_RISK x conviction x expiry_mult x weight_scale
  per_lot_risk  = premium x STOP_FRAC x lot_size          # what one lot costs if the 30% stop is hit
  lots          = floor(risk_budget / per_lot_risk)
  premium_cap   = free_capital x PREMIUM_CAP x weight_scale   # total outlay, not just the risk
  lots          = min(lots, floor(premium_cap / (premium x lot_size)), MAX_LOTS)
  weight_scale  = weight / (1/3), so equal weights = 1.00x, a 0.50 weight = 1.50x.

  If that rounds to 0 but one lot's premium still fits the premium cap, it returns 1 lot (a 0-lot
  answer is only honest when even one lot cannot be afforded). If one lot does not fit, 0 lots with
  "cannot afford 1 lot".

  CONSEQUENCE, stated plainly: because of that floor, when the weight is cut for a bad book record
  the first thing that happens is the lot count falls to 1, not to 0 - the trade still happens, at
  one lot, and the risk budget goes over. In this account's present numbers the 30% premium cap is
  never the binding limit at 1 lot (NIFTY premium cap Rs 59,749 vs Rs 9,529 for a lot), so the floor
  is what is doing the work. If the user ever wants "the book is losing, so stand down" rather than
  "trade it smaller", that is a different rule and belongs in the entry gate, not here.

BOOK WEIGHTS
  Derived from each book's measured return on the capital it consumed (pnl / capital_used), shrunk
  toward equal weights in proportion to how thin the sample is:
      w = (1-c) x 1/3 + c x measured_share,   c = min(1, closed_trades / THIN_TRADES)
  A book with fewer than THIN_TRADES (10) closed trades contributes its evidence only in proportion -
  so a 3-trade fluke cannot take the whole budget. A missing or unreadable book_pnl.json, or every
  book thin, gives EXACTLY equal weights (1/3 each) and says so in the note. Every weight is then
  clamped to [0.15, 0.50] and renormalised, so no book is starved and none takes everything.

SCOPE OF THE RISK NUMBER, stated plainly: the budget is per DECISION. auto_entry mirrors the same
lot count on Upstox and on Kite, so when both legs fill the rupees at risk are twice the number in
these notes. That mirroring is the existing design (one decision, two brokers, one entry counted
against the 3/day cap) and this module does not change it - it only makes the per-leg size honest.

CLI (read-only):
  python3 book_sizing.py            # what-if from live capital, ATM premium, ADX, expiry, MTF room
  python3 book_sizing.py --json     # the same decision as JSON
"""
import glob, json, os, sys, urllib.parse

BOOKS = ('index_options', 'stock_options', 'mtf')
EQUAL = {b: 1.0 / len(BOOKS) for b in BOOKS}

BASE_RISK = 0.015        # rupees at risk per trade, as a fraction of free capital
PREMIUM_CAP = 0.30       # total premium outlay per trade, as a fraction of free capital
STOP_FRAC = 0.30         # risk_manager's fixed stop: 30% under entry
MAX_LOTS = 10            # hard ceiling on one index-option decision
STOCK_OPT_MAX_LOTS = 1   # trade_lock R11: 1 lot per underlying
# t339u (4 Nov 2026): R11 allows 2 lots per stock option on 5-6 Nov only. Read through
# equity/autonomy.py at call time so the sizer agrees with trade_lock on those days and returns
# to 1 on 7 Nov - this module still only aims INSIDE the cap, it never sets one.
# Loaded by path for the same reason as mtf_sizing: a caller may not have this folder on
# sys.path, and falling back to the user's 1-lot cap on an override day would be silent.
_autonomy = None
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        'autonomy', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'autonomy.py'))
    _autonomy = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_autonomy)
    sys.modules.setdefault('autonomy', _autonomy)
except Exception:      # no module -> the user's 1 lot
    _autonomy = None


def stock_opt_max_lots(when=None):
    """The R11 per-underlying lot cap in force now: 1, or 2 on 5-6 Nov 2026."""
    if _autonomy is None:
        return STOCK_OPT_MAX_LOTS
    try:
        return int(_autonomy.effective('STOCK_OPT_MAX_LOTS', STOCK_OPT_MAX_LOTS, when))
    except Exception:
        return STOCK_OPT_MAX_LOTS
THIN_TRADES = 10         # below this many closed trades a book's record is shrunk toward equal
W_MIN, W_MAX = 0.15, 0.50

PNL_PATH = '/workspace/equity/book_pnl.json'
MTF_SIZING = '/workspace/equity/mtf_sizing.py'


# ---------------------------------------------------------------- day streak (t351u)
# User (5 Nov 2026): "Previous days loss continuous try to reduce position sizing, as vice versa."
# Each streak book (NIFTY, SENSEX, BANKNIFTY index options separately; stock options; MTF) gets a
# multiplier from its run of consecutive losing / winning CLOSED days (realised P&L per day, from
# book_pnl.json 'daily'; today is excluded - it is not closed). Flat days (P&L 0) end a streak.
# It multiplies the RISK BUDGET only. Every cap is untouched: the 30% premium cap, MAX_LOTS, the
# R11 per-underlying cap, the 1-lot floor, the autonomy limits and the Rs 30k breaker downstream.
# FAIL-SAFE: an unreadable file, a missing series or a failed source read never gives more than 1.0.
STREAK_BOOKS = ('NIFTY', 'SENSEX', 'BANKNIFTY', 'stock_options', 'mtf')
STREAK_LOSS = {1: 0.85, 2: 0.70, 3: 0.50}     # 3 = "3 or more"
STREAK_WIN = {1: 1.00, 2: 1.10, 3: 1.20}
STREAK_MIN, STREAK_MAX = 0.50, 1.20
# which book_pnl source each streak book's history depends on; if that read failed, cap at 1.0
_STREAK_SRC = {'NIFTY': 'upstox_fo', 'SENSEX': 'upstox_fo', 'BANKNIFTY': 'upstox_fo',
               'stock_options': 'upstox_fo', 'mtf': 'upstox_eq'}


def streak_of(daily, today=None):
    """Signed streak from {date_iso: pnl}: -n = n losing closed days in a row, +n = n winning, 0 none.
    Days on/after `today` (ISO string) are ignored - only closed days count."""
    days = sorted(d for d in (daily or {}) if today is None or d < today)
    n = 0
    for d in reversed(days):
        try:
            v = float(daily[d])
        except (TypeError, ValueError):
            break
        if v == 0:
            break
        sgn = 1 if v > 0 else -1
        if n == 0 or (n > 0) == (sgn > 0):
            n += sgn
        else:
            break
    return n


def streak_multiplier(streak):
    """Multiplier for a signed streak, clamped to [STREAK_MIN, STREAK_MAX]."""
    try:
        k = int(streak)
    except (TypeError, ValueError):
        return 1.0
    if k < 0:
        m = STREAK_LOSS[min(-k, 3)]
    elif k > 0:
        m = STREAK_WIN[min(k, 3)]
    else:
        m = 1.0
    return max(STREAK_MIN, min(STREAK_MAX, m))


def streak_multipliers(path=None, today=None):
    """{book: {'streak', 'mult', 'note'}} for every STREAK_BOOK. Never above 1.0 on bad data."""
    import datetime as _dt
    path = path or PNL_PATH
    if today is None:
        today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30))).date().isoformat()
    out = {}
    try:
        raw = json.load(open(path))
        daily = raw.get('daily')
        ok = raw.get('daily_sources_ok') or {}
        if not isinstance(daily, dict):
            raise ValueError('no daily series in book P&L file')
    except Exception as e:
        for b in STREAK_BOOKS:
            out[b] = dict(streak=0, mult=1.0, note=f'streak unknown ({type(e).__name__}) -> 1.00x')
        return out
    for b in STREAK_BOOKS:
        ser = daily.get(b)
        if not isinstance(ser, dict) or not ser:
            out[b] = dict(streak=0, mult=1.0, note='no closed-day history -> 1.00x')
            continue
        k = streak_of(ser, today)
        m = streak_multiplier(k)
        why = (f'{-k} losing day(s) in a row' if k < 0 else f'{k} winning day(s) in a row' if k > 0
               else 'no streak')
        if not ok.get(_STREAK_SRC[b], False) and m > 1.0:
            m = 1.0
            why += ' (history read failed - capped at 1.00x)'
        out[b] = dict(streak=k, mult=m, note=f'{why} -> {m:.2f}x')
    return out


def streak_mult(book, path=None, today=None):
    """One book's multiplier (float); 1.0 on any doubt, never higher on a failure."""
    try:
        return float(streak_multipliers(path, today).get(book, {}).get('mult', 1.0))
    except Exception:
        return 1.0


def _sm(x):
    try:
        v = float(x)
        if v != v:
            return 1.0
        return max(STREAK_MIN, min(STREAK_MAX, v))
    except (TypeError, ValueError):
        return 1.0


# ---------------------------------------------------------------- weights
def _num(d, *names):
    """First numeric value among `names` in dict d, else None."""
    for n in names:
        if isinstance(d, dict) and n in d:
            try:
                v = float(d[n])
                return v
            except (TypeError, ValueError):
                continue
    return None


def _read_book_stats(path):
    """{book: {trades, pnl, capital}} from book_pnl.json, or ({}, why).

    Tolerant of the writer: accepts {'books': {...}} or a flat mapping, and each book either as a
    dict of numbers (trades/closed/n, pnl/realised/net, capital/capital_used/deployed) or as a
    list of closed trades. Unknown shapes are ignored rather than crashed on.
    """
    try:
        raw = json.load(open(path))
    except FileNotFoundError:
        return {}, f'no book P&L file at {path} - equal weights'
    except Exception as e:
        return {}, f'book P&L file unreadable ({type(e).__name__}: {e}) - equal weights'
    if not isinstance(raw, dict):
        return {}, 'book P&L file is not a JSON object - equal weights'
    src = raw.get('books') if isinstance(raw.get('books'), dict) else raw
    out = {}
    for b in BOOKS:
        v = src.get(b)
        if v is None and b == 'mtf':
            # the book report calls the MTF equity book 'mtf_equity'
            v = src.get('mtf_equity', src.get('mtf_eq'))
        if v is None:
            continue
        if isinstance(v, list):
            pnl = 0.0
            for t in v:
                x = _num(t, 'pnl', 'net', 'realised', 'p')
                if x is not None:
                    pnl += x
            out[b] = dict(trades=len(v), pnl=pnl, capital=None)
            continue
        if not isinstance(v, dict):
            continue
        # The book report nests the numbers under 'window' (recent regime) and 'since_inception'.
        # Prefer the recent window - that is what allocation should follow - and fall back either way.
        scope = v
        for k in ('window', 'since_inception'):
            if isinstance(v.get(k), dict) and _num(v[k], 'trades', 'closed', 'n', 'count') is not None:
                scope = v[k]
                break
        n = _num(scope, 'trades', 'closed', 'closed_trades', 'n', 'count', 'entries')
        pnl = _num(scope, 'pnl', 'realised_pnl', 'realised', 'net', 'net_pnl', 'total_pnl')
        cap = _num(scope, 'capital', 'capital_used', 'capital_deployed', 'deployed', 'avg_capital',
                   'capital_at_risk')
        if n is None and isinstance(v.get('trades_list'), list):
            n = float(len(v['trades_list']))
        out[b] = dict(trades=int(n) if n is not None else None, pnl=pnl, capital=cap)
    return out, None


def _clamp_norm(vals, lo=W_MIN, hi=W_MAX):
    """Normalise to sum 1 with every element inside [lo, hi]. Returns None if impossible.

    Exact projection onto the box-constrained simplex: find the single scale factor L for which
    sum(clip(v_i * L, lo, hi)) == 1. The clipped sum is non-decreasing in L, so a bisection finds it
    deterministically in ~100 steps. (An iterative clamp-and-renormalise was rejected: the clamped
    simplex is a whole set of fixed points, so the answer depended on how many times it happened to
    loop - not defensible for an allocation of money.)
    """
    n = len(vals)
    if n * lo > 1 + 1e-9 or n * hi < 1 - 1e-9:
        return None
    if sum(vals) <= 0:
        return None

    def clipped(lam):
        return [min(max(v * lam, lo), hi) for v in vals]

    a, b = 0.0, 1.0
    for _ in range(200):                      # grow b until the clipped sum reaches 1
        if sum(clipped(b)) >= 1:
            break
        b *= 2
        if b > 1e30:
            return None
    for _ in range(200):
        m = (a + b) / 2
        if sum(clipped(m)) < 1:
            a = m
        else:
            b = m
    out = clipped((a + b) / 2)
    if abs(sum(out) - 1) > 1e-9 or any(not (lo - 1e-9 <= o <= hi + 1e-9) for o in out):
        return None
    return out


def book_weights(path=PNL_PATH):
    """Portfolio weights for the three books, from measured return on capital used.

    Keys 'index_options', 'stock_options', 'mtf' are the weights (sum 1, each 0.15-0.50).
    '_note' says in plain English how they were derived and which books were shrunk for thin data;
    '_measured' carries the raw return-on-capital per book; '_trades' the sample sizes.
    """
    stats, why = _read_book_stats(path)
    out = dict(EQUAL)
    if why:
        out['_note'] = why
        out['_measured'] = {}
        out['_trades'] = {}
        out['_thin'] = list(BOOKS)
        return out
    roc, conf, trades, reasons = {}, {}, {}, []
    for b in BOOKS:
        s = stats.get(b) or {}
        n = s.get('trades') or 0
        pnl = s.get('pnl')
        cap = s.get('capital')
        trades[b] = n
        conf[b] = max(0.0, min(1.0, n / float(THIN_TRADES)))
        if pnl is None or not cap or cap <= 0:
            # No usable record: this book contributes no evidence, it just keeps the equal share.
            conf[b] = 0.0
            roc[b] = None
            if n:
                reasons.append(f'{b}: {n} trades but no usable capital figure - no evidence used')
            continue
        roc[b] = pnl / cap
        if n < THIN_TRADES:
            reasons.append(f'{b}: only {n} closed trade(s) (<{THIN_TRADES}) - record shrunk to '
                           f'{conf[b]:.0%} weight ({roc[b]*100:+.2f}% on capital)')
    measured = {b: roc[b] for b in BOOKS if roc.get(b) is not None}
    if not measured:
        out['_note'] = 'no book has a usable closed-trade record yet - equal weights (1/3 each)'
        out['_measured'] = {}
        out['_trades'] = trades
        out['_thin'] = list(BOOKS)
        return out
    # Positive return -> bigger share; a losing book keeps a floor, it is not starved.
    pos = {b: max(roc[b], 0.0) for b in measured}
    tot = sum(pos.values())
    if tot <= 0:
        out['_note'] = 'every measured book returned <= 0 on capital used - equal weights'
        out['_measured'] = {b: round(roc[b], 6) for b in measured}
        out['_trades'] = trades
        out['_thin'] = [b for b in BOOKS if conf.get(b, 0) < 1]
        return out
    share = {b: pos[b] / tot for b in measured}
    blended = []
    for b in BOOKS:
        c = conf.get(b, 0.0)
        m = share.get(b, EQUAL[b])
        blended.append((1 - c) * EQUAL[b] + c * m)
    w = _clamp_norm(blended)
    if w is None:                                  # cannot happen with 3 books, but never crash the caller
        out['_note'] = 'weight clamp did not converge - equal weights'
        out['_measured'] = {b: round(roc[b], 6) for b in measured}
        out['_trades'] = trades
        out['_thin'] = []
        return out
    for b, x in zip(BOOKS, w):
        out[b] = round(x, 4)
    # rounding can drift the sum by a hair; put the remainder on a weight that has room inside the clamp
    drift = 1.0 - sum(out[b] for b in BOOKS)
    if abs(drift) > 1e-12:
        for b in sorted(BOOKS, key=lambda x: -out[x]):
            cand = round(out[b] + drift, 4)
            if W_MIN - 1e-9 <= cand <= W_MAX + 1e-9:
                out[b] = cand
                break
    for b in BOOKS:                      # belt and braces: never hand back a weight outside the clamp
        out[b] = min(max(out[b], W_MIN), W_MAX)
    parts = [f'{b} {out[b]:.0%}' for b in BOOKS]
    out['_note'] = ('weights from return on capital used: ' + ', '.join(
        f'{b} {roc[b]*100:+.2f}% on Rs {stats[b]["capital"]:,.0f} over {trades[b]} trades'
        for b in BOOKS if roc.get(b) is not None) + ' -> ' + ', '.join(parts)
        + (('; ' + '; '.join(reasons)) if reasons else ''))
    out['_measured'] = {b: round(roc[b], 6) for b in measured}
    out['_trades'] = trades
    out['_thin'] = [b for b in BOOKS if conf.get(b, 0.0) < 1.0]
    return out


# ---------------------------------------------------------------- shared maths
def _scale(weight):
    """Equal weight (1/3) = 1.00x. Guards against a nonsense weight rather than trusting it."""
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return None
    if not (w > 0):
        return None
    return w / (1.0 / len(BOOKS))


def _conviction(adx, daily_aligned):
    """(multiplier, plain-English label) from the 5m ADX and whether the daily trend agrees."""
    try:
        a = float(adx)
    except (TypeError, ValueError):
        return None, 'ADX unavailable'
    if a >= 25 and daily_aligned is True:
        return 1.25, f'ADX {a:g} >=25 with the daily trend aligned -> conviction 1.25x'
    if a >= 25:
        return 1.00, f'ADX {a:g} >=25 but the daily trend does not agree -> conviction 1.00x'
    if a >= 20:
        return 1.00, f'ADX {a:g} in 20-25 -> conviction 1.00x'
    if a >= 18:
        return 0.75, f'ADX {a:g} marginal (18-20) -> conviction 0.75x'
    return 0.60, f'ADX {a:g} below the 18 entry threshold -> conviction 0.60x'


def _fmt(x):
    return f'{x:,.0f}'


# ---------------------------------------------------------------- index options
def index_option_lots(premium, lot_size, free_capital, adx, daily_aligned, expire_today, weight,
                      streak_mult=1.0):
    """(lots, note) for one index-option decision.

    premium       option premium per unit at the price we would pay (the ask, not the LTP)
    lot_size      units per lot for that contract
    free_capital  rupees free to deploy right now
    adx           5m ADX of the index at decision time
    daily_aligned True when the daily trend points the same way as the 5m signal
    expire_today  the contract bought expires today (bigger moves on the same stop)
    weight        this book's weight from book_weights(); 1/3 is neutral
    """
    try:
        prem = float(premium)
        lot = int(lot_size)
        cap = float(free_capital)
    except (TypeError, ValueError):
        return 0, 'cannot size: premium / lot size / capital not numeric - use the fixed size'
    if lot <= 0:
        return 0, 'cannot size: lot size unknown - use the fixed size'
    if prem <= 0:
        return 0, 'cannot size: premium unknown (no quote) - use the fixed size'
    if cap <= 0:
        return 0, f'no free capital (Rs {_fmt(cap)}) -> 0 lots'
    scale = _scale(weight)
    if scale is None:
        return 0, f'book weight {weight!r} unusable -> 0 lots'
    conv, conv_note = _conviction(adx, daily_aligned)
    if conv is None:
        # Never size on a trend reading we do not have: the caller keeps its fixed size on 0+this note.
        return 0, f'cannot size: {conv_note} - use the fixed size'
    exp_mult = 1.15 if expire_today else 1.0
    sm = _sm(streak_mult)
    risk_budget = cap * BASE_RISK * conv * exp_mult * scale * sm
    per_lot_risk = prem * STOP_FRAC * lot
    prem_cap = cap * PREMIUM_CAP * scale
    per_lot_prem = prem * lot
    if per_lot_prem > prem_cap:
        return 0, (f'cannot afford 1 lot: premium Rs {_fmt(per_lot_prem)} ({prem:g} x {lot}) > '
                   f'premium cap Rs {_fmt(prem_cap)} ({PREMIUM_CAP:.0%} of Rs {_fmt(cap)} at weight '
                   f'{weight:.2f}) -> 0 lots')
    by_risk = int(risk_budget // per_lot_risk) if per_lot_risk > 0 else 0
    by_prem = int(prem_cap // per_lot_prem)
    lots = max(0, min(by_risk, by_prem, MAX_LOTS))
    parts = [f'free capital Rs {_fmt(cap)}', conv_note]
    if expire_today:
        parts.append(f'expiry day {exp_mult:g}x (moves run further on the same 30% stop)')
    parts.append(f'weight {weight:.2f} ({scale:.2f}x)')
    parts.append(f'day streak {sm:.2f}x')
    parts.append(f'risk budget Rs {_fmt(risk_budget)} vs Rs {_fmt(per_lot_risk)} risk per lot')
    parts.append(f'premium cap Rs {_fmt(prem_cap)} vs Rs {_fmt(per_lot_prem)} per lot')
    if lots == 0:
        # 1 lot already fits the premium cap (checked above); it is the risk budget that said zero.
        # NOTE: this floor is what makes "0 lots" mean "cannot afford a lot", not "risk said no".
        lots = 1
        parts.append(f'risk budget alone rounds to 0, but 1 lot fits the premium cap -> 1 lot '
                     f'(one lot risks Rs {_fmt(per_lot_risk)} against a Rs {_fmt(risk_budget)} budget)')
        return lots, '; '.join(parts)
    binding = 'premium cap' if by_prem <= by_risk else 'risk budget'
    cap_note = f' capped at MAX_LOTS {MAX_LOTS}' if min(by_risk, by_prem) > MAX_LOTS else ''
    parts.append(f'-> {lots} lot(s), binding on the {binding}{cap_note}')
    return lots, '; '.join(parts)


# ---------------------------------------------------------------- stock options
def stock_option_lots(premium, lot_size, free_capital, weight, when=None, streak_mult=1.0):
    """(lots, note) for one stock-option decision.

    The same rupee-risk arithmetic as an index option, but trade_lock R11 caps a stock option at
    1 lot per underlying and at 3 underlyings in total, so R11 - not capital - is normally the
    binding constraint here (2 lots on 5-6 Nov 2026 only, t339u). This function never returns more
    than R11's cap for the date, and says when capital would have allowed more.
    `when` (optional) is the decision date for the R11 cap; None = now.
    """
    try:
        prem = float(premium)
        lot = int(lot_size)
        cap = float(free_capital)
    except (TypeError, ValueError):
        return 0, 'cannot size: premium / lot size / capital not numeric - use the fixed size'
    if lot <= 0:
        return 0, 'cannot size: lot size unknown - use the fixed size'
    if prem <= 0:
        return 0, 'cannot size: premium unknown (no quote) - use the fixed size'
    if cap <= 0:
        return 0, f'no free capital (Rs {_fmt(cap)}) -> 0 lots'
    scale = _scale(weight)
    if scale is None:
        return 0, f'book weight {weight!r} unusable -> 0 lots'
    sm = _sm(streak_mult)
    risk_budget = cap * BASE_RISK * scale * sm
    per_lot_risk = prem * STOP_FRAC * lot
    per_lot_prem = prem * lot
    prem_cap = cap * PREMIUM_CAP * scale
    if per_lot_prem > prem_cap:
        return 0, (f'cannot afford 1 lot: premium Rs {_fmt(per_lot_prem)} > premium cap '
                   f'Rs {_fmt(prem_cap)} -> 0 lots (R11 would also allow only 1 lot)')
    by_risk = int(risk_budget // per_lot_risk) if per_lot_risk > 0 else 0
    _cap = stock_opt_max_lots(when)
    lots = max(1, min(by_risk, int(prem_cap // per_lot_prem), _cap))
    parts = [f'free capital Rs {_fmt(cap)}', f'weight {weight:.2f} ({scale:.2f}x)', f'day streak {sm:.2f}x',
             f'risk budget Rs {_fmt(risk_budget)} vs Rs {_fmt(per_lot_risk)} risk per lot',
             f'-> {lots} lot(s) (R11: max {_cap} lot(s) per underlying, max 3 underlyings, '
             f'counting this broker\'s existing positions)']
    if by_risk > _cap:
        parts.append(f'capital alone would have allowed {by_risk} lots, but R11 caps it - the binding rule is R11')
    return lots, '; '.join(parts)


# ---------------------------------------------------------------- MTF
def mtf_qty(symbol, price, broker, free_capital, weight, streak_mult=1.0):
    """(qty, note): the MTF quantity allowed for this stock at this book weight.

    Deliberately thin. The 10%-per-stock / 50%-total caps on the broker's MTF MARGIN, the half-room
    reserve for pullback adds, the 6-name limit and the free-margin check all live in
    mtf_sizing.size(); this asks it for the allowed quantity and then only scales that answer DOWN
    to the book's weight. It never re-derives - let alone widens - a cap.
    """
    try:
        cap = float(free_capital)
    except (TypeError, ValueError):
        return 0, 'cannot size: free capital not numeric'
    if cap <= 0:
        return 0, f'no free capital (Rs {_fmt(cap)}) -> 0 shares'
    scale = _scale(weight)
    if scale is None:
        return 0, f'book weight {weight!r} unusable -> 0 shares'
    try:
        sys.path.insert(0, os.path.dirname(MTF_SIZING))
        import mtf_sizing as ms
    except Exception as e:
        return 0, f'MTF sizing unavailable ({type(e).__name__}: {e}) -> 0 shares'
    try:
        r = ms.size(broker, symbol, float(price))
    except Exception as e:
        return 0, f'MTF size failed ({type(e).__name__}: {e}) -> 0 shares'
    allowed = int(r.get('qty') or 0)
    sm = _sm(streak_mult)
    qty = min(allowed, int(allowed * scale * sm))   # never above what the caps allow
    note = (f'{symbol} {broker}: caps allow {allowed} share(s) (10% stock cap / 50% total on MTF margin, '
            f'half-room reserve, {ms.MAX_STOCKS}-name limit); weight {weight:.2f} ({scale:.2f}x) '
            f'day streak {sm:.2f}x -> {qty} share(s)')
    if r.get('reason'):
        note += f'; {r["reason"]}'
    return qty, note


# ---------------------------------------------------------------- whole decision
def explain(free_capital, indexes, weights=None, path=PNL_PATH):
    """The whole sizing decision as a dict, for the CLI and the pre-market report.

    free_capital  rupees free for option premium
    indexes       {name: {premium, lot_size, adx, daily_aligned, expire_today}} - one per index
    """
    w = weights or book_weights(path)
    sk = streak_multipliers(path)
    out = {'free_capital': round(float(free_capital)), 'weights': {b: w[b] for b in BOOKS},
           'weights_note': w.get('_note'), 'trades': w.get('_trades') or {}, 'indexes': {},
           'streak': sk}
    for name, ix in (indexes or {}).items():
        lots, note = index_option_lots(ix.get('premium'), ix.get('lot_size'), free_capital,
                                       ix.get('adx'), ix.get('daily_aligned'), ix.get('expire_today'),
                                       w['index_options'],
                                       (sk.get(name) or {}).get('mult', 1.0))
        out['indexes'][name] = dict(lots=lots, note=note,
                                    premium=ix.get('premium'), lot_size=ix.get('lot_size'),
                                    premium_outlay=None if not ix.get('premium') or not ix.get('lot_size')
                                    else round(lots * float(ix['premium']) * int(ix['lot_size'])),
                                    max_loss_at_stop=None if not ix.get('premium') or not ix.get('lot_size')
                                    else round(lots * float(ix['premium']) * int(ix['lot_size']) * STOP_FRAC),
                                    adx=ix.get('adx'), daily_aligned=ix.get('daily_aligned'),
                                    expire_today=ix.get('expire_today'))
    return out


# ---------------------------------------------------------------- CLI (live inputs, read-only)
def _live():
    """Everything the CLI needs, gathered read-only. Any single failure is reported, not raised."""
    import datetime
    sys.path.insert(0, '/workspace/upstox')
    out = {'now': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
           .strftime('%Y-%m-%d %H:%M IST'), 'indexes': {}, 'errors': []}
    try:
        import day_plan
        cap = day_plan.upstox_capital()
        out['upstox'] = cap
        out['free_capital'] = cap['available']
    except Exception as e:
        out['errors'].append(f'upstox capital: {type(e).__name__}: {e}')
        out['free_capital'] = 0.0
    try:
        import day_plan
        out['kite'] = day_plan.kite_capital()
    except Exception as e:
        out['errors'].append(f'kite capital: {type(e).__name__}: {e}')
    try:
        import api, ind
        IDX = {'NIFTY': dict(key='NSE_INDEX|Nifty 50', exch='NSE', seg='NSE_FO'),
               'BANKNIFTY': dict(key='NSE_INDEX|Nifty Bank', exch='NSE', seg='NSE_FO'),
               'SENSEX': dict(key='BSE_INDEX|SENSEX', exch='BSE', seg='BSE_FO')}
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).date()
        for name, cfg in IDX.items():
            ix = {}
            try:
                files = sorted(glob.glob(f'/workspace/upstox/instruments_{cfg["exch"]}_*.json.gz'))
                ins = [x for x in api.gzip_json(files[-1])
                       if x.get('segment') == cfg['seg'] and x.get('instrument_type') == 'CE'
                       and x.get('underlying_symbol', x.get('name', '')) == name]
                today_ms = int(datetime.datetime.combine(today, datetime.time(0)).timestamp() * 1000)
                exps = sorted({x['expiry'] for x in ins if x['expiry'] >= today_ms})
                exp = exps[0]
                chain = [x for x in ins if x['expiry'] == exp]
                a5 = ind.analyze(cfg['key'], 'minutes', 5, 5)
                std = 50 if name == 'NIFTY' else 100
                atm = round(a5['close'] / std) * std
                best = min(chain, key=lambda x: (abs(x['strike_price'] - atm), x['strike_price']))
                q = api.get('/v2/market-quote/quotes?instrument_key=' +
                            urllib.parse.quote(best['instrument_key'], safe='')).get('data') or {}
                prem = 0.0
                for v in q.values():
                    try:
                        prem = float(v['depth']['sell'][0]['price']) or float(v.get('last_price') or 0)
                    except Exception:
                        prem = float(v.get('last_price') or 0)
                    break
                d1 = ind.analyze(cfg['key'], 'days', 1, 60)
                ix.update(premium=prem, lot_size=int(best['lot_size']), symbol=best['trading_symbol'],
                          expiry=str(datetime.datetime.fromtimestamp(exp / 1000).date()),
                          adx=a5['adx'], dir_5m=a5['dir'], dir_daily=d1['dir'],
                          daily_aligned=(d1['dir'] == a5['dir']),
                          expire_today=(datetime.datetime.fromtimestamp(exp / 1000).date() == today))
            except Exception as e:
                out['errors'].append(f'{name}: {type(e).__name__}: {e}')
            out['indexes'][name] = ix
    except Exception as e:
        out['errors'].append(f'index inputs: {type(e).__name__}: {e}')
    return out


def main():
    live = _live()
    w = book_weights()
    d = explain(live.get('free_capital') or 0.0, live.get('indexes') or {}, w)
    d['now'] = live.get('now')
    d['upstox'] = live.get('upstox')
    d['kite'] = live.get('kite')
    d['errors'] = live.get('errors')
    # MTF room, read-only, on the caps that actually apply
    d['mtf'] = {}
    try:
        import day_plan
        for b in ('upstox', 'kite'):
            try:
                d['mtf'][b] = day_plan.mtf_room(b)
            except Exception as e:
                d['mtf'][b] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}
    except Exception as e:
        d['mtf'] = {'error': f'{type(e).__name__}: {e}'}
    if '--json' in sys.argv:
        print(json.dumps(d, indent=1, default=str))
        return
    print(f"SIZING WHAT-IF  {d['now']}   (read-only: no orders, no state written)")
    print(f"free capital: Upstox Rs {d['free_capital']:,.0f}   Kite free Rs "
          f"{(d.get('kite') or {}).get('free', 0) or 0:,.0f}")
    print(f"book weights: " + ', '.join(f"{b} {w[b]:.0%}" for b in BOOKS))
    print(f"  why: {w.get('_note')}")
    print("day streak: " + ', '.join(f"{b} {v['mult']:.2f}x ({v['streak']:+d})" for b, v in d['streak'].items()))
    print("\nindex options (lots at this capital, premium and trend):")
    for name, r in d['indexes'].items():
        if not r.get('lots') and r.get('premium') is None:
            print(f"  {name:<10} n/a")
            continue
        print(f"  {name:<10} {r['lots']} lot(s)  {r.get('premium')} x {r.get('lot_size')}  "
              f"outlay Rs {r.get('premium_outlay') or 0:,.0f}  max loss at the 30% stop "
              f"Rs {r.get('max_loss_at_stop') or 0:,.0f}"
              f"  [ADX {r.get('adx')} daily {'aligned' if r.get('daily_aligned') else 'not aligned'}"
              f"{', EXPIRY TODAY' if r.get('expire_today') else ''}]")
        print(f"             {r.get('note')}")
    print("\nMTF room (caps on the broker's MTF margin, my names only):")
    for b, m in (d.get('mtf') or {}).items():
        if isinstance(m, dict) and m.get('ok'):
            print(f"  {b:<8} capital Rs {m.get('capital', 0):,.0f}  used margin Rs {m.get('mtf_used_margin', 0):,.0f}"
                  f"  total room Rs {m.get('room_total', 0):,.0f} (new name may use Rs {m.get('room_half', 0):,.0f})"
                  f"  {m.get('free_slots')} free slot(s) of {m.get('slots_cap')}")
        else:
            print(f"  {b:<8} unavailable: {(m or {}).get('why')}")
    if d.get('errors'):
        print("\nnotes:")
        for e in d['errors']:
            print('  ! ' + e)
    print("\nSizing rule: risk = free capital x 1.5% x conviction x expiry 1.15 x (weight / 1/3); "
          "lots = that / (premium x 30% x lot size), also capped by a 30% premium outlay cap and "
          f"MAX_LOTS {MAX_LOTS}. trade_lock.py still enforces every hard cap downstream.")


if __name__ == '__main__':
    main()
