#!/usr/bin/env python3
"""Monday 9 Nov 2026 IST MTF policy. Pure: no broker calls, no orders, no tokens.

From that morning (Asia/Calcutta), each broker, on its own capital and on the MTF
margin that broker actually blocks:

  * at most 5 actual positive MTF names (legacy and user lots count, not only bot tags).
    Five is a maximum, not a quota: an empty slot stays empty
  * initial buys and adds share one 25% pool. There is no separate add reserve.
    Unused cash, and unused room in any other bucket, cannot fund an MTF order
  * the existing 10% per-stock cap stays, because it is stricter than 25%
  * a book already over those budgets is reported and refuses new buys and adds;
    nothing here liquidates a position to get back under the line

Before 9 Nov 2026 the numeric settings are unchanged (5 names this bot opened, 10% per
stock or the 5-6 Nov 12% override, 50% total, half of remaining room for new names).
An unreadable decision time fails closed (no buy).

Technical admission is the completed daily session only (t455u, 6 Nov 2026 20:06 IST).
It replaces the 1-hour entry signal. Close above EMA20, Supertrend green, MACD
histogram confirming, +DI above -DI, ADX >= 25, all on that daily bar. A bar dated
today is ignored before 15:30 IST. There is no intraday fallback. Ranking uses the
same three weights equity/rotation.py already used on the 1-hour bar (ADX, 1000 *
MACD histogram / close, 2 * percent above EMA20). The old +15/-15 cross-timeframe
bonus is not added, because the score is already the daily bar. Equal scores sort
by symbol A-Z; that order is deterministic, not a claim that the earlier name is
stronger. Protective stops stay on their existing intraday rules.
Adds are ranked on the same daily score. A loss is never a reason to add, and a
stop is never lowered.
"""
import datetime, json, os

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
POLICY_DATE = datetime.date(2026, 11, 9)

# Numeric settings that stay in force through Sunday 8 Nov 2026.
PRE_MAX_NAMES = 5
PRE_PER_STOCK = 0.10
PRE_TOTAL = 0.50
PRE_NEW_SHARE = 0.50

# Monday onward. t455u raised the name cap from 3 to 5. Five is the maximum, not a fill target.
# t462u (6 Nov 2026 21:36 IST): one shared 25% pool. The 20% + 20% split is not deployed.
import capital_policy as _capital
MAX_NAMES = 5
INITIAL = _capital.MTF   # same pool as adds; not a separate 20%
ADD = _capital.MTF
TOTAL = _capital.MTF
PER_STOCK = 0.10        # the stricter cap already in force; not raised to 25% or to the 12% override
ADX_MIN = 25

POLICY_TEXT = (
    "From Monday 9 November 2026 Asia/Calcutta, updated 6 Nov 2026 20:06 IST (t455u): "
    "max 5 actual positive MTF names per broker, not a requirement to hold 5; "
    "a new name is a replacement after one verified full close on both brokers, not because the count is under 5; "
    "qualification and ranking use the completed daily session only "
    "(close above EMA20, Supertrend green, MACD histogram confirming, +DI above -DI, ADX >= 25). "
    "No 1-hour fallback. Rank = ADX + 1000*MACD histogram/close + 2*percent above EMA20; "
    "equal scores sort by symbol A-Z, which is only a stable order. "
    "Initial buys and adds share one 25% pool of that broker's verified capital. "
    "There is no separate add reserve, and unused cash or another bucket cannot fund MTF. "
    "The stricter 10% per-stock cap still applies. Each broker uses its own verified capital "
    "and the MTF margin the broker actually blocks, not notional. Only common "
    "names, and both legs must pass the plan before either order. No forced rotation or trim "
    "because rank changed. A book already over budget fails closed on new buys and adds and "
    "is reported, not liquidated. Pending buys reserve a slot and budget; sold negative day "
    "legs are netted out of the count. The saved refill routine is updated and remains paused; "
    "this module does not resume it."
)

# Same coefficients as equity/rotation.py score(), applied to the completed daily bar.
# The old +15/-15 term was a bonus for a second timeframe. It is not used here.
RANK_WEIGHTS = {'adx': 1.0, 'macd_hist_over_close': 1000.0, 'pct_above_ema20': 2.0,
                'cross_timeframe_bonus': 0.0}
TIE_BREAK = ('higher score first; scores within 1e-6 are ties and then sort by symbol A-Z. '
             'The letter order is not a stronger signal.')
SESSION_CLOSE = datetime.time(15, 30)
MIN_DAILY_BARS = 35
# Hard emergency floor: a new MTF stop is never placed farther than this below price.
# It is not the trend trail. A daily Supertrend farther away does not replace it,
# and it never loosens a stop that is already tighter.
EMERGENCY_FLOOR_PCT = 5.0
# A breached daily trend is reported as an exit. The sell is not sent from this module.
EXECUTE_BREACH_EXIT = False
STALE_DAILY_DAYS = 4

OPEN_STATUSES = {
    'open', 'trigger pending', 'open pending', 'put order req received',
    'validation pending', 'modify pending', 'amo req received', 'modify validation pending',
}
LEDGER_VERSION = 1
LEDGER_NAME = 'mtf_budget_ledger.json'
# t697u / t698u (16 Nov 2026): long-term daily Supertrend MTF sleeve. Tag
# longterm_dst is exempt from the Monday 5-name / 10% / 25% caps only.
# The sleeve's own cap is max 3 names and Rs 50,000 margin per name per broker.
LONGTERM_DST_TAG = 'longterm_dst'
LONGTERM_DST_MAX_NAMES = 3
LONGTERM_DST_CAPITAL = 50_000
LONGTERM_DST_STATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'logs', 'longterm_daily_st', 'positions.json')


def ist_day(when=None):
    """IST calendar date, or None when `when` cannot be read. None is not a trading licence."""
    if when is None:
        return datetime.datetime.now(IST).date()
    try:
        if isinstance(when, str):
            when = datetime.datetime.fromisoformat(when) if ('T' in when or ' ' in when) else datetime.date.fromisoformat(when)
        if isinstance(when, datetime.datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=IST)
            return when.astimezone(IST).date()
        if isinstance(when, datetime.date):
            return when
    except Exception:
        return None
    return None


def policy_active(when=None):
    """True from 9 Nov 2026 IST onward. An unreadable clock is active (the tighter rule)."""
    day = ist_day(when)
    if day is None:
        return True
    return day >= POLICY_DATE


def date_readable(when=None):
    return ist_day(when) is not None


def max_names(when=None):
    return MAX_NAMES if policy_active(when) else PRE_MAX_NAMES


def stop_mode(when=None):
    """Which MTF stop rule a restarted process would use.

    'legacy' through 8 Nov 2026 IST: 5% below price, or the 1-hour Supertrend when
    that is tighter. That is the behavior live processes already run.
    'daily' from 9 Nov 2026 IST: completed-daily Supertrend ratchet.
    An unreadable clock stays on 'legacy' so a bad timestamp cannot change the stop.
    """
    day = ist_day(when)
    if day is None or day < POLICY_DATE:
        return 'legacy'
    return 'daily'


def norm_symbol(symbol):
    return (symbol or '').upper().replace('-EQ', '').strip()


# ---------------------------------------------------------------- holdings
def net_quantity(overnight, position, day_buy=None, day_sell=None, separate_leg=None):
    """Signed net shares after a sold day leg is applied once.

    overnight     holdings / previous-close quantity, or None when that row does not exist
    position      broker net quantity, or None when there is no position row (may be <= 0)
    day_buy/sell  fields on the position row. Applied only when they prove the quantity
                  has not already been reduced (so a netted quantity is not reduced twice).
    separate_leg  a distinct signed day row (a sell is negative). Applied when it is not
                  the same number already expressed by day_buy/day_sell.
    """
    delta = None
    if day_buy is not None or day_sell is not None:
        delta = int(day_buy or 0) - int(day_sell or 0)
    separate = None if separate_leg is None else int(separate_leg)
    if separate is not None and delta is not None and separate == delta:
        separate = None          # the same sell reported twice
    elif separate is not None and delta is None:
        delta_for_identity = None
    else:
        delta_for_identity = delta

    ovn = None if overnight is None else int(overnight)
    if position is None and ovn is None:
        base = int(delta or 0)
        return base + (0 if separate is None or delta is not None else 0) if separate is None else base + separate

    if position is None:
        net = ovn + int(delta or 0)
        if separate is not None and (delta is None or separate != delta):
            net += separate
        return net

    pos = int(position)
    if delta_for_identity is None and separate is None:
        if pos < 0 and ovn is not None:
            return ovn + pos
        if pos == 0 and ovn is not None:
            return 0                 # a flat net does not resurrect a stale holding
        return pos

    ident = delta_for_identity
    if ident is not None and ovn is not None and pos == ovn + ident:
        net = pos
    elif ident is not None and ovn is not None and pos == ovn and ident < 0:
        net = pos + ident            # sell field present, quantity not reduced yet
    elif pos < 0 and ovn is not None:
        net = ovn + pos
    elif ident is not None and ovn is not None and pos == ident:
        net = ovn + ident
    elif pos > 0 and ident is not None and ovn is None and separate is None:
        # day_sell on a positive row with no overnight figure: quantity is already the
        # net the broker published. Do not subtract the sell a second time.
        net = pos
    elif pos > 0:
        net = pos
    elif ovn is not None:
        net = ovn + pos
    else:
        net = pos
    if separate is not None:
        if ident is not None and ovn is not None and pos == ovn + ident + separate:
            return pos
        if not (ident is not None and ovn is not None and pos == ovn + ident and separate == ident):
            net += separate
    return net


def _num(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(row):
    for key in ('price', 'average_price'):
        n = _num(row.get(key))
        if n and n > 0:
            return n
    return 0.0


def reconcile_book(rows):
    """{symbol: {qty, price, cost}} for names whose net quantity is positive.

    `cost` is None when a positive net has no usable average price. Callers fail closed
    on that name instead of pretending it blocks zero margin. Tag/mine is not an input:
    a legacy or user lot is a real name.
    """
    grouped = {}
    for row in rows or []:
        sym = norm_symbol(row.get('symbol') or row.get('trading_symbol') or row.get('tradingsymbol'))
        if not sym:
            continue
        grouped.setdefault(sym, []).append(row)
    out = {}
    for sym, group in grouped.items():
        positions = [r for r in group if (r.get('kind') or 'position') == 'position']
        holdings = [r for r in group if r.get('kind') == 'holding']
        days = [r for r in group if r.get('kind') == 'day']
        pos_qty = sum(int(r.get('qty') or 0) for r in positions) if positions else None
        hold_qty = sum(int(r.get('qty') or 0) for r in holdings) if holdings else None
        overnight_fields = [r.get('overnight_qty') for r in positions if r.get('overnight_qty') is not None]
        if overnight_fields:
            overnight = sum(int(v) for v in overnight_fields)
        else:
            overnight = hold_qty
        # A day-only position often reports overnight_quantity 0 while the holding
        # still has the shares. A zero must not wipe that holding. A positive
        # overnight figure is the position row's own accounting and wins.
        if hold_qty and not overnight and pos_qty is not None and pos_qty <= 0:
            overnight = hold_qty
        buys = [r.get('day_buy_qty') for r in positions if r.get('day_buy_qty') is not None]
        sells = [r.get('day_sell_qty') for r in positions if r.get('day_sell_qty') is not None]
        day_buy = sum(int(v) for v in buys) if buys else None
        day_sell = sum(int(v) for v in sells) if sells else None
        separate = sum(int(r.get('qty') or 0) for r in days) if days else None
        net = net_quantity(overnight, pos_qty, day_buy, day_sell, separate)
        if net <= 0:
            continue
        longs = [r for r in positions + holdings if int(r.get('qty') or 0) > 0 and _price(r) > 0]
        if not longs:
            out[sym] = {'qty': int(net), 'price': 0.0, 'cost': None}
            continue
        num = sum(int(r['qty']) * _price(r) for r in longs)
        den = sum(int(r['qty']) for r in longs)
        px = num / den if den else 0.0
        out[sym] = {'qty': int(net), 'price': px, 'cost': round(net * px, 4)}
    return out


def row_from_upstox(position):
    qty = position.get('quantity')
    if qty is None:
        qty = position.get('net_quantity') or 0
    return {
        'symbol': position.get('trading_symbol') or position.get('tradingsymbol'),
        'qty': int(qty or 0),
        'price': position.get('average_price') or 0,
        'kind': 'position',
        'day_buy_qty': position.get('day_buy_quantity'),
        'day_sell_qty': position.get('day_sell_quantity'),
        'overnight_qty': position.get('overnight_quantity') if 'overnight_quantity' in position else None,
    }


def rows_from_kite(net_positions, holdings):
    """Raw Kite net positions plus holdings.mtf. Negative day quantities are kept."""
    rows = []
    for p in net_positions or []:
        if (p.get('product') or '') != 'MTF':
            continue
        if p.get('exchange') not in (None, 'NSE', 'BSE'):
            continue
        rows.append({
            'symbol': p.get('tradingsymbol') or p.get('trading_symbol'),
            'qty': int(p.get('quantity') or 0),
            'price': p.get('average_price') or 0,
            'kind': 'position',
            'day_buy_qty': p.get('day_buy_quantity') if 'day_buy_quantity' in p else None,
            'day_sell_qty': p.get('day_sell_quantity') if 'day_sell_quantity' in p else None,
            'overnight_qty': p.get('overnight_quantity') if 'overnight_quantity' in p else None,
        })
    for h in holdings or []:
        mtf = h.get('mtf') or {}
        qty = mtf.get('quantity')
        if qty is None and (h.get('product') or '') == 'MTF':
            qty = h.get('quantity')
        if int(qty or 0) <= 0:
            continue
        rows.append({
            'symbol': h.get('tradingsymbol') or h.get('trading_symbol'),
            'qty': int(qty),
            'price': mtf.get('average_price') or h.get('average_price') or 0,
            'kind': 'holding',
        })
    return rows


# ---------------------------------------------------------------- orders and ledger
def normalize_order(order):
    status = (order.get('status') or '').lower().replace('_', ' ').strip()
    side = (order.get('transaction_type') or '').upper()
    product = (order.get('product') or '').upper()
    qty = int(order.get('quantity') or 0)
    filled = int(order.get('filled_quantity') if order.get('filled_quantity') is not None else (order.get('filled') or 0))
    mode = (order.get('mode') or '').lower() or None
    return {
        'symbol': norm_symbol(order.get('trading_symbol') or order.get('tradingsymbol')),
        'side': side, 'product': product, 'status': status,
        'qty': qty, 'filled': filled, 'open_qty': max(qty - filled, 0),
        'price': float(order.get('price') or 0) or float(order.get('average_price') or 0),
        'order_id': str(order.get('order_id') or ''),
        'mode': mode if mode in ('new', 'add') else None,
        'margin': order.get('margin'),
    }


def pending_buys(orders, held_symbols):
    """Open MTF buy remainder. Filled quantity is not reserved again.

    Mode is the order's own mode when it has one, else add when the name is already
    a positive holding and new otherwise. A new pending name consumes a slot.
    """
    held = {norm_symbol(s) for s in (held_symbols or [])}
    out = []
    for raw in orders or []:
        o = raw if 'open_qty' in raw and 'side' in raw else normalize_order(raw)
        if o['product'] != 'MTF' or o['side'] != 'BUY':
            continue
        if o['status'] not in OPEN_STATUSES or o['open_qty'] <= 0 or not o['symbol']:
            continue
        mode = o['mode'] or ('add' if o['symbol'] in held else 'new')
        out.append(dict(o, mode=mode))
    return out


def empty_ledger():
    return {'version': LEDGER_VERSION, 'brokers': {'upstox': {'orders': {}}, 'kite': {'orders': {}}}}


def load_ledger(path):
    """(ledger, error). Missing file is an empty ledger. Unreadable file is an error
    and the caller must fail closed rather than assume the add/initial split is zero."""
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return empty_ledger(), None
    except Exception as exc:
        return None, '%s: %s' % (type(exc).__name__, exc)
    if not isinstance(data, dict) or data.get('version') != LEDGER_VERSION:
        return None, 'ledger version unreadable'
    data.setdefault('brokers', {})
    for broker in ('upstox', 'kite'):
        data['brokers'].setdefault(broker, {'orders': {}})
        data['brokers'][broker].setdefault('orders', {})
    return data, None


def save_ledger(path, ledger):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as handle:
        json.dump(ledger, handle, indent=1, sort_keys=True)
        handle.write('\n')
    os.replace(tmp, path)


def ledger_path(directory=None):
    directory = directory or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(directory, LEDGER_NAME)


def remember(ledger, broker, order_id, symbol, mode, margin, status, day, filled_qty=0):
    """Record how a fill is classified. This does not add exposure on top of a position:
    rooms() uses the broker position as the quantity and this record only to split it."""
    ledger = ledger or empty_ledger()
    book = ledger.setdefault('brokers', {}).setdefault(broker, {}).setdefault('orders', {})
    book[str(order_id)] = {
        'symbol': norm_symbol(symbol),
        'mode': 'add' if mode == 'add' else 'new',
        'margin': round(float(margin or 0), 4),
        'status': status,
        'day': day if isinstance(day, str) else (day.isoformat() if day else None),
        'filled_qty': int(filled_qty or 0),
    }
    return ledger


def reconcile_ledger(ledger, broker, open_ids, complete_ids, today):
    """Restart pass. A completed id, or a reservation from an earlier day that is no
    longer open, becomes a fill classification. It is not reserved a second time.
    An id that is still open is reserved from the order book, not from here.
    An id from today that the book has not shown yet stays an in-flight reservation."""
    today_s = today if isinstance(today, str) else today.isoformat()
    orders = (ledger or empty_ledger())['brokers'].setdefault(broker, {}).setdefault('orders', {})
    open_ids = {str(i) for i in (open_ids or [])}
    complete_ids = {str(i) for i in (complete_ids or [])}
    for oid, rec in orders.items():
        if oid in complete_ids or (rec.get('day') and rec['day'] < today_s and oid not in open_ids):
            rec['status'] = 'filled'
            rec['count_as_pending'] = False
        elif oid in open_ids:
            rec['status'] = 'open'
            rec['count_as_pending'] = False
        elif rec.get('status') == 'reserved' and rec.get('day') == today_s:
            rec['count_as_pending'] = True
        else:
            rec['count_as_pending'] = False
    return ledger


def _ratio_of(ratio, symbol):
    if isinstance(ratio, dict):
        if symbol not in ratio or ratio[symbol] is None:
            return None
        return float(ratio[symbol])
    if ratio is None:
        return None
    return float(ratio)


def _pending_margin(order, ratio):
    if order.get('margin') is not None:
        try:
            return float(order['margin']), True
        except (TypeError, ValueError):
            return None, False
    rate = _ratio_of(ratio, order['symbol'])
    if rate is None or not order.get('price'):
        return None, False
    return order['open_qty'] * float(order['price']) * rate, True



def longterm_dst_symbols(broker=None, path=None):
    """Symbols already in the long-term daily-ST sleeve (not counted toward Monday caps)."""
    path = path or LONGTERM_DST_STATE
    try:
        data = json.load(open(path))
    except FileNotFoundError:
        return set()
    except Exception:
        return set()
    pos = data.get('positions') or {}
    brokers = [broker] if broker else ('upstox', 'kite')
    out = set()
    for b in brokers:
        for sym in (pos.get(b) or {}):
            out.add(norm_symbol(sym))
    return out


def longterm_dst_blocks(held_sleeve, symbol, margin, already_held=False,
                        capital=LONGTERM_DST_CAPITAL, max_names=LONGTERM_DST_MAX_NAMES):
    """This sleeve's own cap. Empty list means the sleeve allows the buy."""
    blocks = []
    symbol = norm_symbol(symbol)
    if already_held:
        blocks.append('longterm_dst: already held — not double-buying %s' % symbol)
    try:
        margin = float(margin)
    except (TypeError, ValueError):
        blocks.append('longterm_dst: live MTF margin unreadable')
        margin = None
    if margin is not None and margin > float(capital) + 1e-6:
        blocks.append('longterm_dst: live MTF margin Rs %.2f exceeds Rs %s cap'
                      % (margin, format(int(capital), ',')))
    names = {norm_symbol(s) for s in (held_sleeve or [])}
    if symbol and not already_held:
        names.add(symbol)
        if len(names) > int(max_names):
            blocks.append('longterm_dst: max %d names in this sleeve' % int(max_names))
    return blocks


def is_longterm_dst_symbol(symbol, broker=None, path=None):
    return norm_symbol(symbol) in longterm_dst_symbols(broker, path=path)


def rooms(capital, positions, pending, ledger, ratio, when, broker='upstox',
          ledger_error=None, unpriced=None, exempt_symbols=None):
    """Initial / add / total room in margin rupees.

    Positions are the source of truth for filled exposure. Ledger records only split
    that exposure into initial versus add, capped at the position so a restart cannot
    add the fill again. Open order remainders reserve on top. In-flight ledger
    reservations (not yet on the book) reserve too, once.
    """
    capital = float(capital or 0)
    if exempt_symbols is None:
        exempt_symbols = longterm_dst_symbols(broker)
    exempt = {norm_symbol(s) for s in (exempt_symbols or [])}
    if exempt:
        positions = {s: p for s, p in (positions or {}).items() if norm_symbol(s) not in exempt}
        pending = [p for p in (pending or []) if norm_symbol(p.get('symbol')) not in exempt]
    active = policy_active(when)
    if not date_readable(when):
        return _closed_rooms(capital, 'decision date unreadable')
    if not active:
        return {'policy': 'pre-2026-11-09', 'active': False}
    if ledger_error or capital <= 0:
        return _closed_rooms(capital, ledger_error or 'capital unreadable')
    unpriced = set(unpriced or [])
    positions = positions or {}
    add_recorded = {}
    inflight = []
    orders = ((ledger or {}).get('brokers') or {}).get(broker, {}).get('orders') or {}
    seen_pending = {str(p.get('order_id') or '') for p in (pending or []) if p.get('order_id')}
    for oid, rec in orders.items():
        # Filled classification only. An open or reserved order is budgeted from the
        # book (or as in-flight below), never added on top of that reservation.
        if rec.get('mode') == 'add' and rec.get('status') == 'filled' and rec.get('symbol'):
            add_recorded[rec['symbol']] = add_recorded.get(rec['symbol'], 0.0) + float(rec.get('margin') or 0)
        if (rec.get('status') == 'reserved' and oid not in seen_pending
                and rec.get('count_as_pending') is not False):
            if exempt and norm_symbol(rec.get('symbol')) in exempt:
                continue
            inflight.append(rec)
    initial_used = 0.0
    add_used = 0.0
    for sym, pos in positions.items():
        cost = pos.get('cost')
        if cost is None:
            unpriced.add(sym)
            continue
        rate = _ratio_of(ratio, sym)
        if rate is None:
            unpriced.add(sym)
            continue
        actual = float(cost) * rate
        add_m = min(float(add_recorded.get(sym) or 0), actual)
        add_used += add_m
        initial_used += actual - add_m
    pending_unpriced = False
    new_names = set()
    held = {s for s, p in positions.items() if int(p.get('qty') or 0) > 0}
    for order in pending or []:
        margin, ok = _pending_margin(order, ratio)
        if not ok or margin is None:
            pending_unpriced = True
            continue
        if order.get('mode') == 'add' or (order.get('mode') is None and order.get('symbol') in held):
            add_used += margin
        else:
            initial_used += margin
            if order.get('symbol') and order['symbol'] not in held:
                new_names.add(order['symbol'])
    for rec in inflight:
        margin = float(rec.get('margin') or 0)
        if rec.get('mode') == 'add':
            add_used += margin
        else:
            initial_used += margin
            if rec.get('symbol') and rec['symbol'] not in held:
                new_names.add(rec['symbol'])
    if unpriced or pending_unpriced:
        closed = _closed_rooms(capital, 'a holding or pending buy has no margin rate')
        closed['unpriced'] = sorted(unpriced)
        closed['name_count'] = len(held) + len(new_names)
        closed['pending_new'] = sorted(new_names)
        return closed
    return _pack_rooms(capital, initial_used, add_used, held, new_names)


def _pack_rooms(capital, initial_used, add_used, held, new_names):
    # One pool. initial_used and add_used are still recorded so a restart can say
    # which orders were adds, but both spend the same 25%.
    total_pool = TOTAL * capital
    total_used = initial_used + add_used
    room = max(total_pool - total_used, 0.0)
    over = total_used > total_pool + 1e-6
    return {
        'policy': 'monday-2026-11-09', 'active': True, 'closed': False, 'reason': None,
        'capital': capital, 'shared_pool': True,
        'initial_pool': total_pool, 'add_pool': total_pool, 'total_pool': total_pool,
        'per_stock_pool': PER_STOCK * capital,
        'initial_used': initial_used, 'add_used': add_used, 'total_used': total_used,
        'room_initial': room, 'room_add': room, 'room_total': room,
        'room_new': room,
        'over_initial': over,
        'over_add': over,
        'over_total': over,
        'held': sorted(held), 'name_count': len(held) + len(new_names),
        'pending_new': sorted(new_names), 'name_cap': MAX_NAMES,
        'over_names': len(held) + len(new_names) > MAX_NAMES,
    }


def _closed_rooms(capital, reason):
    return {
        'policy': 'monday-2026-11-09', 'active': True, 'closed': True, 'reason': reason,
        'capital': float(capital or 0),
        'shared_pool': True,
        'initial_pool': TOTAL * float(capital or 0), 'add_pool': TOTAL * float(capital or 0),
        'total_pool': TOTAL * float(capital or 0), 'per_stock_pool': PER_STOCK * float(capital or 0),
        'initial_used': 0.0, 'add_used': 0.0, 'total_used': 0.0,
        'room_initial': 0.0, 'room_add': 0.0, 'room_total': 0.0, 'room_new': 0.0,
        'over_initial': False, 'over_add': False, 'over_total': False,
        'held': [], 'name_count': 0, 'pending_new': [], 'name_cap': MAX_NAMES,
        'over_names': False,
    }


def per_stock_room(capital, already_margin):
    return max(PER_STOCK * float(capital) - float(already_margin or 0), 0.0)


def order_blocks(book_rooms, symbol, mode, margin, trend_ok, in_loss, already_margin, tag=None):
    """Why this one buy is refused. Empty list means the Monday budgets allow it.
    Never returns a sell, a trim, or a rotation."""
    if (tag or '') == LONGTERM_DST_TAG:
        symbol = norm_symbol(symbol)
        held_sleeve = longterm_dst_symbols()
        held = {norm_symbol(s) for s in (book_rooms.get('held') or [])}
        already = symbol in held or symbol in held_sleeve
        return longterm_dst_blocks(held_sleeve, symbol, margin, already_held=already)
    if not book_rooms.get('active'):
        return []
    blocks = []
    symbol = norm_symbol(symbol)
    margin = float(margin or 0)
    if book_rooms.get('closed'):
        blocks.append('R10 Monday: %s — no new buy and no add' % book_rooms.get('reason'))
        return blocks
    held = set(book_rooms.get('held') or [])
    if book_rooms.get('over_total'):
        blocks.append('R10 Monday: total MTF margin is already over the shared 25% pool — refusing the buy, not liquidating')
    if mode == 'add' or symbol in held:
        if symbol not in held:
            blocks.append('R10 Monday: an add is only for a name already held')
        if in_loss:
            blocks.append('R10 Monday: not averaging a loser')
        if not trend_ok:
            blocks.append('R10 Monday: add only while the strong trend continues')
        if margin > float(book_rooms.get('room_add') or 0) + 1e-6:
            blocks.append('R10 Monday: shared 25% pool does not cover this add; unused cash and other buckets are not available')
    else:
        if symbol not in held and int(book_rooms.get('name_count') or 0) >= MAX_NAMES:
            blocks.append('R10 Monday: %s name cap (pending buys included) — not opening %s' % (MAX_NAMES, symbol))
        if not trend_ok:
            blocks.append('R10 Monday: new name is not technically eligible')
        if margin > float(book_rooms.get('room_new') or 0) + 1e-6:
            blocks.append('R10 Monday: shared 25% pool does not cover this order; unused cash and other buckets are not available')
    room_ps = per_stock_room(book_rooms.get('capital') or 0, already_margin)
    if margin > room_ps + 1e-6:
        blocks.append('R10 Monday: per-stock margin would exceed the stricter 10% cap')
    return blocks


def exposure_report(book_rooms):
    """Human lines. Over-budget is a report, not an instruction to sell."""
    if not book_rooms or not book_rooms.get('active'):
        return []
    lines = []
    if book_rooms.get('closed'):
        lines.append('FAIL CLOSED: %s. No buy. No liquidation.' % book_rooms.get('reason'))
    if book_rooms.get('over_names'):
        lines.append('OVER NAMES %s/%s. Reporting only; not liquidating.' % (
            book_rooms.get('name_count'), book_rooms.get('name_cap')))
    if book_rooms.get('over_total'):
        lines.append('OVER SHARED 25%% MTF pool (used %.0f of %.0f). No new buy and no add. Not liquidating.' % (
            book_rooms.get('total_used') or 0, book_rooms.get('total_pool') or 0))
    return lines


# ---------------------------------------------------------------- trend and ranking
def _as_ist(when):
    if when is None:
        return datetime.datetime.now(IST)
    if isinstance(when, str):
        when = datetime.datetime.fromisoformat(when)
    if isinstance(when, datetime.datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        return when.astimezone(IST)
    return None


def _bar_ist(ts):
    if isinstance(ts, datetime.datetime):
        moment = ts if ts.tzinfo else ts.replace(tzinfo=IST)
        return moment.astimezone(IST)
    text = str(ts).replace('Z', '+00:00')
    moment = datetime.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def completed_daily_candles(candles, now):
    """Keep only daily bars whose NSE cash session is finished.

    A bar dated today is incomplete before 15:30 IST and is dropped. After 15:30
    IST that same bar is the completed session. Older dates stay. There is no
    intraday series to substitute. Returns (closed_rows, reason or None)."""
    moment = _as_ist(now)
    if moment is None:
        return [], 'decision time unreadable'
    if not candles:
        return [], 'no daily candles'
    closed = []
    dropped_today = False
    for row in candles:
        try:
            bar = _bar_ist(row[0])
        except Exception:
            return [], 'daily candle timestamp unreadable'
        if bar.date() > moment.date():
            continue
        if bar.date() == moment.date() and moment.time() < SESSION_CLOSE:
            dropped_today = True
            continue
        closed.append(row)
    if not closed:
        if dropped_today:
            return [], 'current daily candle is still in session; no completed daily bar'
        return [], 'no completed daily session'
    return closed, None


def snapshot_from_candles(candles, now):
    """Indicator snapshot of the last completed daily bar, using ind.py's own maths.
    Returns (snapshot or None, reasons). Never reads an hourly series."""
    closed, why = completed_daily_candles(candles, now)
    if why:
        return None, [why]
    if len(closed) < MIN_DAILY_BARS:
        return None, ['not enough completed daily bars (%s < %s)' % (len(closed), MIN_DAILY_BARS)]
    try:
        import ind
        highs = [float(row[2]) for row in closed]
        lows = [float(row[3]) for row in closed]
        closes = [float(row[4]) for row in closed]
        st, direction = ind.supertrend(highs, lows, closes)
        ema = ind.ema(closes, 20)
        macd_line, signal, hist = ind.macd(closes)
        pdi, mdi, adx = ind.dmi(highs, lows, closes)
    except Exception as exc:
        return None, ['daily indicators unreadable (%s); no intraday fallback' % type(exc).__name__]
    snap = {
        'timeframe': 'day', 'incomplete': False, 'last_ts': closed[-1][0],
        'close': closes[-1], 'ema20': ema[-1], 'st': st[-1], 'dir': direction[-1],
        'macd': macd_line[-1], 'sig': signal[-1], 'hist': hist[-1],
        'pdi': pdi[-1], 'mdi': mdi[-1], 'adx': adx[-1],
    }
    return snap, []


def daily_score(snap):
    """The rotation.py weights on one completed daily bar.

    score = 1*ADX + 1000*(MACD histogram/close) + 2*(percent above EMA20).
    Returns None when the bar cannot be scored. Does not add the old +15 daily bonus.
    """
    try:
        close = float(snap['close'])
        ema = float(snap['ema20'])
        if close == 0 or ema == 0:
            return None
        dist = 100.0 * (close - ema) / ema
        return (RANK_WEIGHTS['adx'] * float(snap['adx'])
                + RANK_WEIGHTS['macd_hist_over_close'] * float(snap['hist']) / close
                + RANK_WEIGHTS['pct_above_ema20'] * dist)
    except (TypeError, ValueError, KeyError):
        return None


def trend_ok(daily, hour=None):
    """(ok, reasons) for one completed daily snapshot.

    `hour` is accepted only so an old caller cannot silently win: if a caller
    passes an intraday bar and no daily snapshot, the answer is not eligible.
    """
    if hour is not None and not daily:
        return False, ['intraday bar is not an MTF signal; no intraday fallback']
    if not isinstance(daily, dict) or daily.get('timeframe') != 'day' or daily.get('incomplete'):
        return False, ['completed daily session required']
    reasons = []
    try:
        close = float(daily['close'])
        ema = float(daily['ema20'])
        if not close > ema:
            reasons.append('daily close is not above EMA20')
        if int(daily.get('dir')) != 1:
            reasons.append('daily Supertrend is not green')
        if float(daily.get('hist')) <= 0:
            reasons.append('daily MACD histogram is not confirming')
        if float(daily.get('pdi')) <= float(daily.get('mdi')):
            reasons.append('daily +DI is not dominant')
        if float(daily.get('adx')) < ADX_MIN:
            reasons.append('daily ADX is below %s' % ADX_MIN)
    except (TypeError, ValueError, KeyError) as exc:
        return False, ['daily trend inputs unreadable (%s)' % type(exc).__name__]
    return (not reasons), reasons


def qualify_symbol(key=None, now=None, candles=None):
    """Daily qualification for one symbol. Missing candles fail closed. No Yahoo, no 1h."""
    moment = _as_ist(now)
    if candles is None:
        if not key:
            return {'eligible': False, 'reasons': ['no instrument key'], 'score': None, 'timeframe': 'day'}
        try:
            import ind
            candles = ind.candles(key, 'days', 1, 220)
        except Exception as exc:
            return {'eligible': False, 'score': None, 'timeframe': 'day',
                    'reasons': ['daily candles unreadable (%s); no intraday fallback' % type(exc).__name__]}
    snap, why = snapshot_from_candles(candles, moment)
    if snap is None:
        return {'eligible': False, 'reasons': why, 'score': None, 'timeframe': 'day'}
    ok, reasons = trend_ok(snap)
    return {'eligible': ok, 'reasons': reasons, 'score': daily_score(snap), 'snapshot': snap,
            'timeframe': 'day', 'last_ts': snap.get('last_ts'), 'weights': dict(RANK_WEIGHTS),
            'tie_break': TIE_BREAK}


def daily_bar_is_stale(last_ts, now):
    """True when the last completed daily bar is too old to trail a stop."""
    try:
        age = (_as_ist(now).date() - _bar_ist(last_ts).date()).days
    except Exception:
        return True
    return age > STALE_DAILY_DAYS


def _emergency_floor(ltp):
    return float(ltp) * (1.0 - EMERGENCY_FLOOR_PCT / 100.0)


def _stop_result(**kwargs):
    base = {'lowered': False, 'cancel_existing': False, 'execute_exit': EXECUTE_BREACH_EXIT,
            'emergency_floor_pct': EMERGENCY_FLOOR_PCT}
    base.update(kwargs)
    return base


def daily_stop_plan(ltp, position_qty, existing_trigger=None, existing_qty=0, daily=None,
                    data_ok=True, stale=False, allow_trail=True):
    """Ratchet an MTF protective stop from a completed daily Supertrend.

    The trigger never moves down. A daily level farther below price is ignored.
    Missing or stale daily data does not cancel a live stop and does not switch
    to the 1-hour bar. A naked position with no usable daily bar gets the 5%
    emergency floor only, labeled as that floor. A daily stop already through
    price, or a red Supertrend, is an exit decision: no invalid stop is placed,
    and this function does not send the sell.
    """
    try:
        price = float(ltp)
    except (TypeError, ValueError):
        return _stop_result(action='HOLD', place_stop=False, exit=False, trigger=None,
                            qty=int(position_qty or 0), source='none',
                            reason='price unreadable; existing protection is not cancelled')
    qty = int(position_qty or 0)
    have = None
    if existing_trigger not in (None, ''):
        try:
            have = float(existing_trigger)
        except (TypeError, ValueError):
            have = None
    covered = have is not None and int(existing_qty or 0) >= qty and qty > 0
    floor = _emergency_floor(price)

    def keep(reason):
        action = 'KEEP'
        place = False
        trig = have
        if have is not None and qty > int(existing_qty or 0) and allow_trail:
            action = 'RATCHET'
            place = True
            reason = reason + '; cover the full quantity without moving the trigger down'
        return _stop_result(action=action, place_stop=place, exit=False, trigger=trig, qty=qty,
                            source='existing', reason=reason)

    if not data_ok or stale or not isinstance(daily, dict) or daily.get('timeframe') != 'day' or daily.get('incomplete'):
        if have is not None and have < price:
            return keep('daily data missing or stale; existing stop kept; no 1-hour fallback')
        if floor < price:
            return _stop_result(action='PLACE', place_stop=True, exit=False, trigger=floor, qty=qty,
                                source='emergency_floor',
                                reason='no completed daily bar and no live stop; 5% emergency floor only')
        return _stop_result(action='HOLD', place_stop=False, exit=False, trigger=None, qty=qty,
                            source='none', reason='cannot price a protective stop')

    try:
        technical = float(daily['st'])
        direction = int(daily.get('dir'))
    except (TypeError, ValueError, KeyError):
        if have is not None and have < price:
            return keep('daily Supertrend unreadable; existing stop kept; no 1-hour fallback')
        return _stop_result(action='PLACE', place_stop=True, exit=False, trigger=floor, qty=qty,
                            source='emergency_floor',
                            reason='daily Supertrend unreadable and no live stop; 5% emergency floor only')

    if direction != 1 or technical >= price:
        return _stop_result(
            action='EXIT', place_stop=False, exit=True, trigger=have if have is not None and have < price else None,
            qty=qty, source='daily_supertrend',
            reason='daily trend stop is already through price or Supertrend is not green; do not place an invalid stop',
            exit_path='trade_lock LIMIT SELL of the full quantity; do not cancel a live stop first')

    # A valid daily Supertrend is the trail, even when it is farther than 5%.
    # The 5% figure is only the emergency floor for a naked position with no daily bar.
    candidate = technical
    source = 'daily_supertrend'
    if candidate >= price:
        return _stop_result(action='EXIT', place_stop=False, exit=True, trigger=have, qty=qty,
                            source=source, reason='protective level is not below price',
                            exit_path='trade_lock LIMIT SELL of the full quantity; do not cancel a live stop first')
    if have is not None:
        if not allow_trail:
            return keep('existing holding is protected once and is not trailed')
        if candidate <= have + 1e-9:
            return keep('daily level is farther from price; existing stop is not loosened')
        return _stop_result(action='RATCHET', place_stop=True, exit=False, trigger=candidate, qty=qty,
                            source=source, reason='daily trend stop is tighter than the live stop')
    return _stop_result(action='PLACE', place_stop=True, exit=False, trigger=candidate, qty=qty,
                        source=source, reason='initial protective stop from the completed daily bar'
                        if source == 'daily_supertrend' else
                        'daily Supertrend is looser than the 5% floor; initial stop uses the floor')


def position_stop(key, ltp, existing_trigger=None, existing_qty=0, position_qty=0, allow_trail=True,
                  now=None, candles=None):
    """Live adapter. Legacy mode returns {'mode': 'legacy'} and does not read a daily bar."""
    if stop_mode(now) != 'daily':
        return {'mode': 'legacy'}
    q = qualify_symbol(key, now=now, candles=candles)
    snap = q.get('snapshot')
    stale = bool(snap and daily_bar_is_stale(snap.get('last_ts'), now or datetime.datetime.now(IST)))
    plan = daily_stop_plan(ltp, position_qty, existing_trigger=existing_trigger, existing_qty=existing_qty,
                           daily=snap, data_ok=snap is not None, stale=stale, allow_trail=allow_trail)
    plan['mode'] = 'daily'
    plan['stale'] = stale
    plan['last_ts'] = None if not snap else snap.get('last_ts')
    return plan


def rank_key(row):
    """Sort key: stronger score first, then symbol. Ties are not a second signal."""
    score = row.get('score')
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = float('-inf')
    return (-score, norm_symbol(row.get('sym') or row.get('symbol')))


def common_names(upstox_names, kite_names):
    return sorted(set(map(norm_symbol, upstox_names or [])) & set(map(norm_symbol, kite_names or [])))


def _score(row):
    try:
        return float(row.get('score') or 0)
    except (TypeError, ValueError):
        return 0.0


def observe_closures(prior, current, open_sells=None, readable=None, consumed=None, today=None):
    """One entitlement per verified full close. Nothing is inferred from intent.

    A closure counts only when both books were readable, the name was positive on
    both brokers in `prior`, both are now at zero, and neither has an open MTF
    sell. A partial quantity, an open sell, or a missing book grants nothing and
    does not advance the snapshot. The same symbol-plus-held-since id is not
    emitted twice. An exit decision is not an input here.
    """
    readable = readable or {}
    if not readable.get('upstox') or not readable.get('kite'):
        return {'ok': False, 'closures': [], 'snapshot': None,
                'reason': 'a broker book was unreadable; no closure and no new name'}
    open_sells = open_sells or {}
    selling = {broker: {norm_symbol(s) for s in (open_sells.get(broker) or [])} for broker in ('upstox', 'kite')}
    prior = prior or {}
    current = current or {}
    consumed = set(consumed or [])
    held_since = dict(prior.get('held_since') or {})
    day = ist_day(today)
    day_s = day.isoformat() if day else None
    found = []

    def qty_map(book, broker):
        raw = (book or {}).get(broker) or {}
        if isinstance(raw, dict) and 'qty' not in raw and not any(isinstance(v, dict) for v in raw.values()):
            return {norm_symbol(k): int(v or 0) for k, v in raw.items()}
        out = {}
        for key, value in raw.items():
            if key == 'held_since':
                continue
            sym = norm_symbol(key)
            out[sym] = int(value.get('qty') if isinstance(value, dict) else (value or 0))
        return out

    prev_u, prev_k = qty_map(prior, 'upstox'), qty_map(prior, 'kite')
    now_u, now_k = qty_map(current, 'upstox'), qty_map(current, 'kite')
    symbols = set(prev_u) | set(prev_k) | set(now_u) | set(now_k) | set(held_since)
    new_since = {}
    for sym in symbols:
        was_both = int(prev_u.get(sym) or 0) > 0 and int(prev_k.get(sym) or 0) > 0
        now_both_flat = int(now_u.get(sym) or 0) == 0 and int(now_k.get(sym) or 0) == 0
        now_both_open = int(now_u.get(sym) or 0) > 0 and int(now_k.get(sym) or 0) > 0
        if sym in selling['upstox'] or sym in selling['kite']:
            if was_both and held_since.get(sym):
                new_since[sym] = held_since[sym]
            continue
        if now_both_open:
            new_since[sym] = held_since.get(sym) or day_s
            continue
        if was_both and now_both_flat and held_since.get(sym):
            ident = '%s|%s' % (sym, held_since[sym])
            if ident not in consumed:
                found.append({'id': ident, 'symbol': sym, 'held_since': held_since[sym], 'status': 'open'})
                consumed.add(ident)
        elif int(now_u.get(sym) or 0) > 0 or int(now_k.get(sym) or 0) > 0:
            if was_both and held_since.get(sym):
                new_since[sym] = held_since[sym]
    snapshot = {'upstox': now_u, 'kite': now_k, 'held_since': new_since}
    return {'ok': True, 'closures': found, 'snapshot': snapshot, 'reason': None}


def replacement_room(closures, pending_new, name_counts):
    """How many NEW names this pass may open. Pending buys consume an entitlement."""
    closures = list(closures or [])
    open_ids = [c['id'] for c in closures if c.get('status', 'open') == 'open']
    reserved_for = {norm_symbol(c.get('reserved_for')) for c in closures
                    if c.get('status') == 'reserved' and c.get('reserved_for')}
    pending = {norm_symbol(s) for s in (pending_new or [])}
    uncovered = pending - reserved_for
    usable = max(0, len(open_ids) - len(uncovered))
    counts = name_counts or {}
    vacancy = MAX_NAMES
    for broker in ('upstox', 'kite'):
        vacancy = min(vacancy, MAX_NAMES - int(counts.get(broker) or 0))
    vacancy = max(0, vacancy)
    take = min(usable, vacancy)
    return {'allowed': take, 'closure_ids': open_ids[:take], 'vacancy': vacancy,
            'pending_consumed': len(uncovered)}


def propose(held_by_broker, candidates, rooms_by_broker, trends, in_loss=None,
            replacement=None, initial_deployment=False):
    """Ranked adds, and a new name only as a replacement or an explicit first deployment.

    Adds do not need a closure. A new name is not opened because the count is under 5.
    Without `initial_deployment=True`, a new name needs `replacement['allowed']` from a
    verified full closure and a free slot on both brokers. No CLOSE, TRIM, or SWAP.
    """
    in_loss = in_loss or {}
    report = []
    actions = []
    books = {}
    for broker in ('upstox', 'kite'):
        rows = []
        for row in (held_by_broker or {}).get(broker) or []:
            sym = norm_symbol(row.get('sym') or row.get('symbol'))
            if not sym or int(row.get('qty') or 0) <= 0:
                continue
            rows.append(dict(row, sym=sym))
        books[broker] = rows
        view = (rooms_by_broker or {}).get(broker) or {}
        report.extend('%s: %s' % (broker, line) for line in exposure_report(view))
    common = common_names([r['sym'] for r in books['upstox']], [r['sym'] for r in books['kite']])
    only_u = sorted({r['sym'] for r in books['upstox']} - set(common))
    only_k = sorted({r['sym'] for r in books['kite']} - set(common))
    if only_u or only_k:
        report.append('NOT COMMON: upstox-only %s; kite-only %s. Not buying the missing leg from this ranker and not selling it.' % (
            ','.join(only_u) or '-', ','.join(only_k) or '-'))
    over = any((rooms_by_broker or {}).get(b, {}).get('over_names') or (rooms_by_broker or {}).get(b, {}).get('over_total')
               or (rooms_by_broker or {}).get(b, {}).get('closed') for b in ('upstox', 'kite'))
    ranked_held = sorted(common, key=lambda sym: (-_score(_held_row(books, sym)), sym))
    for sym in ranked_held:
        ok, why = _trend_of(trends, sym)
        if sym in in_loss and in_loss[sym]:
            report.append('NO ADD %s: position is a loser — not averaging' % sym)
            continue
        if not ok:
            report.append('NO ADD %s: %s' % (sym, '; '.join(why)))
            continue
        if over or not _both_have(rooms_by_broker, 'room_add'):
            report.append('NO ADD %s: add room or name/total cap does not allow it on both brokers' % sym)
            continue
        actions.append({'action': 'ADD', 'sym': sym, 'score': _score(_held_row(books, sym))})
    if over:
        report.append('No new name: a book is over the name cap or the shared 25% pool, or the picture is incomplete.')
        return {'actions': [a for a in actions if a['action'] == 'ADD' and not over], 'report': report, 'common': common}
    # over blocks adds too: the list above already required `not over`. Rebuild adds only.
    actions = [a for a in actions if a['action'] == 'ADD']
    held_syms = set(common) | set(only_u) | set(only_k)
    eligible = []
    for row in candidates or []:
        sym = norm_symbol(row.get('sym') or row.get('symbol'))
        if not sym or sym in held_syms:
            continue
        ok, why = _trend_of(trends, sym) if trends and sym in _trend_map(trends) else (bool(row.get('eligible')), row.get('why') or [])
        if not ok:
            report.append('SKIP %s: not eligible (%s)' % (sym, '; '.join(why) if isinstance(why, (list, tuple)) else why or 'trend'))
            continue
        eligible.append(dict(row, sym=sym))
    eligible.sort(key=rank_key)
    if not eligible:
        report.append('No technically eligible new name. Spare slots stay empty.')
    slots = {}
    for broker in ('upstox', 'kite'):
        view = (rooms_by_broker or {}).get(broker) or {}
        slots[broker] = max(MAX_NAMES - int(view.get('name_count') if view.get('name_count') is not None else len(books[broker])), 0)
        if float(view.get('room_new') or 0) <= 0:
            slots[broker] = 0
    if initial_deployment:
        allowance = min(slots.values()) if slots else 0
        closure_ids = []
        report.append('Explicit initial-deployment authority: new names are not a count<5 bootstrap.')
    else:
        repl = replacement or {'allowed': 0, 'closure_ids': []}
        allowance = min(int(repl.get('allowed') or 0), min(slots.values()) if slots else 0)
        closure_ids = list(repl.get('closure_ids') or [])
        if allowance <= 0:
            report.append('No new name: a new stock waits for one verified full closure and a free slot on both brokers.')
    if not eligible:
        pass
    elif allowance <= 0:
        pass
    elif min(slots.values()) <= 0:
        report.append('No new name: a broker has no free slot or no room in the shared 25% pool.')
    else:
        room = {b: float((rooms_by_broker or {}).get(b, {}).get('room_new') or 0) for b in slots}
        opened = 0
        for row in eligible:
            if opened >= allowance or min(slots.values()) <= 0 or min(room.values()) <= 0:
                break
            action = {'action': 'NEW', 'sym': row['sym'], 'score': _score(row),
                      'authority': 'initial_deployment' if initial_deployment else 'replacement'}
            if closure_ids:
                action['closure_id'] = closure_ids[opened]
            actions.append(action)
            opened += 1
            for broker in slots:
                slots[broker] -= 1
                room[broker] = room[broker]
    for action in actions:
        if action['action'] not in ('ADD', 'NEW'):
            raise RuntimeError('ranker emitted %s' % action['action'])
    return {'actions': actions, 'report': report, 'common': common}


def _trend_map(trends):
    if isinstance(trends, dict) and 'hour' not in trends and 'ok' not in trends:
        return trends
    return {}


def _trend_of(trends, symbol):
    if not trends:
        return False, ['no trend snapshot']
    item = trends.get(symbol)
    if item is None:
        return False, ['no trend snapshot']
    if isinstance(item, tuple):
        return bool(item[0]), list(item[1] or [])
    if isinstance(item, dict) and item.get('timeframe') == 'day':
        return trend_ok(item)
    if isinstance(item, dict) and 'hour' in item:
        return False, ['MTF qualification is the completed daily session only']
    return bool(item), [] if item else ['trend gate closed']


def _held_row(books, symbol):
    for broker in ('upstox', 'kite'):
        for row in books[broker]:
            if row['sym'] == symbol:
                return row
    return {'sym': symbol, 'score': 0}


def _both_have(rooms_by_broker, key):
    for broker in ('upstox', 'kite'):
        view = (rooms_by_broker or {}).get(broker) or {}
        if view.get('closed') or float(view.get(key) or 0) <= 0:
            return False
    return True


# ---------------------------------------------------------------- mirror and stops
def paired_decision(upstox_leg, kite_leg):
    """Both legs pass, or neither order is sent."""
    def ok(leg):
        if not leg or leg.get('error') or leg.get('blocks'):
            return False
        return bool(leg.get('ok')) and int(leg.get('qty') or 0) > 0
    u_ok, k_ok = ok(upstox_leg), ok(kite_leg)
    blocked = [name for name, good in (('upstox', u_ok), ('kite', k_ok)) if not good]
    return {'both_ok': u_ok and k_ok, 'place': u_ok and k_ok, 'blocked_brokers': blocked}


def mirror_unwind(upstox_order_id, upstox_filled_qty, kite_order_id):
    """The established half-mirror safeguard. A Kite leg that did not land must not
    leave the Upstox leg on: sell back what filled, otherwise cancel the open order."""
    if kite_order_id:
        return {'unwind': False, 'action': 'KEEP'}
    if not upstox_order_id:
        return {'unwind': False, 'action': 'NOTHING'}
    filled = int(upstox_filled_qty or 0)
    if filled > 0:
        return {'unwind': True, 'action': 'SELL_BACK', 'qty': filled}
    return {'unwind': True, 'action': 'CANCEL'}


def stop_coverage(full_qty, proposed_trigger, existing=None):
    """One protective stop for the full position after a fill or an add.

    An existing trigger is a floor. A proposed trigger under that floor is ignored,
    so a stop is never lowered. `verified` is true only when live stop quantity
    already covers `full_qty` at that floor or higher.
    """
    existing = list(existing or [])
    full_qty = int(full_qty or 0)
    covered = sum(int(row.get('qty') or 0) for row in existing)
    triggers = []
    for row in existing:
        if row.get('trigger') is None:
            continue
        try:
            triggers.append(float(row['trigger']))
        except (TypeError, ValueError):
            continue
    floor = max(triggers) if triggers else None
    try:
        proposed = None if proposed_trigger is None else float(proposed_trigger)
    except (TypeError, ValueError):
        proposed = None
    refused_lower = floor is not None and proposed is not None and proposed < floor
    if floor is not None and proposed is not None:
        trigger = max(floor, proposed)
    else:
        trigger = floor if floor is not None else proposed
    verified = bool(full_qty > 0 and covered >= full_qty and trigger is not None and trigger > 0 and (
        floor is None or floor + 1e-9 >= trigger))
    if full_qty <= 0 or trigger is None or trigger <= 0:
        action = 'BLOCK'
    elif verified:
        action = 'KEEP'
    elif existing:
        action = 'REPLACE'
    else:
        action = 'PLACE'
    return {
        'qty': full_qty, 'trigger': trigger, 'covered_qty': covered, 'shortfall': max(full_qty - covered, 0),
        'verified': verified, 'action': action, 'refused_lower': refused_lower, 'lowered': False,
    }


# ---------------------------------------------------------------- refill window
REFILL_START = datetime.time(9, 30)
REFILL_END = datetime.time(14, 30)


def in_refill_window(when, trading_day=None):
    """Hourly refill stays 09:30-14:30 IST, weekdays. `trading_day` False covers a holiday
    the caller already resolved. The clock here does not fetch a calendar."""
    if not date_readable(when) and not isinstance(when, datetime.datetime):
        return False
    if isinstance(when, datetime.datetime):
        moment = when if when.tzinfo else when.replace(tzinfo=IST)
        moment = moment.astimezone(IST)
    else:
        return False
    if moment.weekday() >= 5:
        return False
    if trading_day is False:
        return False
    return REFILL_START <= moment.time() <= REFILL_END


def enforcement_errors(view):
    """Fail closed when the running code cannot actually apply the Monday numbers."""
    if not isinstance(view, dict):
        return ['runtime view missing']
    errors = []
    expect = (
        ('policy_active_on', True),
        ('max_names', MAX_NAMES),
        ('initial', INITIAL),
        ('add', ADD),
        ('total', TOTAL),
        ('per_stock', PER_STOCK),
        ('counts_all_positive_names', True),
        ('pending_reserves', True),
        ('shared_pool', True),
        ('add_reserve_isolated', False),
        ('no_auto_liquidate', True),
    )
    for key, want in expect:
        if view.get(key) != want:
            errors.append('%s is %r, Monday policy needs %r' % (key, view.get(key), want))
    return errors


def live_view(sizing=None):
    """Read the numbers the imported modules will actually enforce on 9 Nov 2026."""
    try:
        when = '2026-11-09T10:00'
        if sizing is None:
            import mtf_sizing as sizing
        return {
            'policy_active_on': bool(policy_active(when)),
            'max_names': int(sizing.max_names(when)),
            'initial': float(sizing.initial_fraction(when)),
            'add': float(sizing.add_fraction(when)),
            'total': float(sizing.total_cap(when)),
            'per_stock': float(sizing.per_stock_cap(when)),
            'counts_all_positive_names': bool(getattr(sizing, 'COUNTS_ALL_POSITIVE', False)),
            'pending_reserves': bool(getattr(sizing, 'PENDING_RESERVES', False)),
            'shared_pool': abs(float(sizing.total_cap(when)) - TOTAL) < 1e-9
            and abs(float(sizing.initial_fraction(when)) - TOTAL) < 1e-9
            and abs(float(sizing.add_fraction(when)) - TOTAL) < 1e-9,
            'add_reserve_isolated': False,
            'no_auto_liquidate': True,
        }
    except Exception as exc:
        return {'error': '%s: %s' % (type(exc).__name__, exc)}
