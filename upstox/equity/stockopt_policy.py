"""Stock-option name cap and replacement gate. Pure: no broker calls and no orders.

Three distinct underlyings per broker, across technical, results, and manual
books. Pending buys and open sells count. A new name is not opened because the
count is under three. It needs a verified full close of one name on both
brokers, or an explicit initial-entry flag the caller set on purpose. Partial
quantity, the other contract, a residual, an open sell, or an unreadable book
does not free the name.
"""
import datetime
import re

MAX_UNDERLYINGS = 3
LOT_CEILING = 2          # the dated 2-lot grant may stand; nothing looser is applied
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
INDEX = frozenset({
    'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX', 'BANKEX',
})
# A status not in this set still reserves the name. A blank status is not 'complete'.
TERMINAL_STATUSES = frozenset({
    'complete', 'cancelled', 'canceled', 'rejected', 'failed',
    'cancelled amo', 'amo cancelled', 'cancelled after market order',
})


def underlying(symbol):
    text = re.sub(r'-EQ$', '', (symbol or '').upper())
    match = re.match(r'^[A-Z&\-]+', text)
    return match.group(0) if match else text


def is_stock_option(symbol):
    text = (symbol or '').upper()
    if not re.match(r'^[A-Z&\-]+\d.*(CE|PE)$', text):
        return False
    return underlying(text) not in INDEX


def _symbol(row):
    return (row.get('trading_symbol') or row.get('tradingsymbol') or '').upper()


def _status(order):
    return (order.get('status') or '').lower().replace('_', ' ').strip()


def _open_qty(order):
    qty = order.get('quantity')
    if qty in (None, ''):
        return None
    filled = order.get('filled_quantity')
    if filled is None:
        filled = order.get('filled') or 0
    try:
        return max(int(qty) - int(filled or 0), 0)
    except (TypeError, ValueError):
        return None


def contract_qty(positions):
    """Absolute quantity by underlying. Any non-zero contract keeps the name."""
    out = {}
    for row in positions or []:
        sym = _symbol(row)
        if not is_stock_option(sym):
            continue
        try:
            qty = int(row.get('quantity') or 0)
        except (TypeError, ValueError):
            qty = None
        if qty is None or qty == 0:
            if qty is None:
                name = underlying(sym)
                out[name] = out.get(name, 0) + 1
            continue
        name = underlying(sym)
        out[name] = out.get(name, 0) + abs(qty)
    return out


def open_sell_names(orders):
    names = set()
    for order in orders or []:
        if (order.get('transaction_type') or '').upper() != 'SELL':
            continue
        if _status(order) in TERMINAL_STATUSES:
            continue
        sym = _symbol(order)
        if is_stock_option(sym):
            names.add(underlying(sym))
    return names


def book_view(positions, orders):
    """One broker. None when either input is missing, so the caller fails closed.

    `names` is every underlying that is long, reserved by a buy, or still has
    an open sell. `pending` is the subset not already long.
    """
    if positions is None or orders is None:
        return None
    held_qty = contract_qty(positions)
    held = set(held_qty)
    pending = set()
    pending_qty = {}
    uncertain = set()
    for order in orders:
        if (order.get('transaction_type') or '').upper() != 'BUY':
            continue
        if _status(order) in TERMINAL_STATUSES:
            continue
        sym = _symbol(order)
        if not is_stock_option(sym):
            continue
        name = underlying(sym)
        qty = _open_qty(order)
        if qty is None:
            uncertain.add(name)
            if name not in held:
                pending.add(name)
            continue
        if qty <= 0:
            continue
        pending_qty[name] = pending_qty.get(name, 0) + qty
        if name not in held:
            pending.add(name)
    sells = open_sell_names(orders)
    names = held | pending | sells | uncertain
    return {
        'held': held,
        'held_qty': held_qty,
        'pending': pending,
        'pending_qty': pending_qty,
        'open_sells': sells,
        'uncertain': uncertain,
        'names': names,
        'count': len(names),
    }


def observe_closures(prior, current, open_sells=None, readable=None, consumed=None, today=None):
    """One entitlement per verified full close of one underlying on both brokers.

    The name must have been positive on both books and be zero on both, with
    no open sell of any of its contracts. A leftover call, put, or single unit
    is not a close. An unreadable book returns no snapshot.
    """
    readable = readable or {}
    if not readable.get('upstox') or not readable.get('kite'):
        return {'ok': False, 'closures': [], 'snapshot': None,
                'reason': 'a broker book was unreadable; no closure and no new name'}
    open_sells = open_sells or {}
    selling = {broker: {underlying(s) for s in (open_sells.get(broker) or [])} for broker in ('upstox', 'kite')}
    prior = prior or {}
    current = current or {}
    consumed = set(consumed or [])
    held_since = dict(prior.get('held_since') or {})
    day = _day(today)
    day_s = day.isoformat() if day else None

    def qty_map(book, broker):
        raw = (book or {}).get(broker) or {}
        out = {}
        for key, value in raw.items():
            if key == 'held_since':
                continue
            name = underlying(key)
            try:
                out[name] = int(value.get('qty') if isinstance(value, dict) else (value or 0))
            except (TypeError, ValueError):
                out[name] = 1
        return out

    prev_u, prev_k = qty_map(prior, 'upstox'), qty_map(prior, 'kite')
    now_u, now_k = qty_map(current, 'upstox'), qty_map(current, 'kite')
    symbols = set(prev_u) | set(prev_k) | set(now_u) | set(now_k) | set(held_since)
    new_since = {}
    found = []
    for name in symbols:
        was_both = int(prev_u.get(name) or 0) > 0 and int(prev_k.get(name) or 0) > 0
        now_flat = int(now_u.get(name) or 0) == 0 and int(now_k.get(name) or 0) == 0
        now_both = int(now_u.get(name) or 0) > 0 and int(now_k.get(name) or 0) > 0
        if name in selling['upstox'] or name in selling['kite']:
            if was_both and held_since.get(name):
                new_since[name] = held_since[name]
            elif int(now_u.get(name) or 0) > 0 or int(now_k.get(name) or 0) > 0:
                new_since[name] = held_since.get(name) or day_s
            continue
        if now_both:
            new_since[name] = held_since.get(name) or day_s
            continue
        if was_both and now_flat and held_since.get(name):
            ident = '%s|%s' % (name, held_since[name])
            if ident not in consumed:
                found.append({'id': ident, 'symbol': name, 'held_since': held_since[name], 'status': 'open'})
                consumed.add(ident)
        elif int(now_u.get(name) or 0) > 0 or int(now_k.get(name) or 0) > 0:
            if was_both and held_since.get(name):
                new_since[name] = held_since[name]
    snapshot = {'upstox': now_u, 'kite': now_k, 'held_since': new_since}
    return {'ok': True, 'closures': found, 'snapshot': snapshot, 'reason': None, 'consumed': sorted(consumed)}


def replacement_room(closures, pending_new, name_counts):
    """How many new underlyings this pass may open. Pending buys consume one each."""
    closures = list(closures or [])
    open_ids = [c['id'] for c in closures if c.get('status', 'open') == 'open' and c.get('id')]
    reserved_for = {underlying(c.get('reserved_for')) for c in closures
                    if c.get('status') == 'reserved' and c.get('reserved_for')}
    pending = {underlying(s) for s in (pending_new or [])}
    uncovered = pending - reserved_for
    usable = max(0, len(open_ids) - len(uncovered))
    counts = name_counts or {}
    if counts.get('upstox') is None or counts.get('kite') is None:
        return {'allowed': 0, 'closure_ids': [], 'vacancy': 0, 'pending_consumed': len(uncovered),
                'reason': 'a broker book is unreadable'}
    try:
        vacancy = min(MAX_UNDERLYINGS - int(counts['upstox']), MAX_UNDERLYINGS - int(counts['kite']))
    except (TypeError, ValueError):
        return {'allowed': 0, 'closure_ids': [], 'vacancy': 0, 'pending_consumed': len(uncovered),
                'reason': 'name count is unreadable'}
    vacancy = max(0, vacancy)
    take = min(usable, vacancy)
    return {'allowed': take, 'closure_ids': open_ids[:take], 'vacancy': vacancy,
            'pending_consumed': len(uncovered), 'reason': None}


def admit_new(this_count, other_count, replacement=None, initial_entry=False, pending_new=0):
    """Whether this buy may open a new underlying. Count under 3 is not enough."""
    if this_count is None or other_count is None:
        return {'ok': False, 'authority': None,
                'reason': 'a broker book is unreadable; new stock-option name refused'}
    try:
        this_count = int(this_count)
        other_count = int(other_count)
        pending_new = int(pending_new or 0)
    except (TypeError, ValueError):
        return {'ok': False, 'authority': None,
                'reason': 'stock-option name count is unreadable; new name refused'}
    if this_count >= MAX_UNDERLYINGS or other_count >= MAX_UNDERLYINGS or pending_new < 0:
        return {'ok': False, 'authority': None,
                'reason': 'stock-option cap is %s distinct underlyings per broker (pending included); not opening another' % MAX_UNDERLYINGS}
    if initial_entry is True:
        return {'ok': True, 'authority': 'initial_entry', 'reason': 'explicit initial-entry authority'}
    repl = replacement if isinstance(replacement, dict) else {}
    ids = [item for item in (repl.get('closure_ids') or []) if item]
    try:
        allowed = int(repl.get('allowed') or 0) - pending_new
    except (TypeError, ValueError):
        allowed = 0
    if allowed < 1 or not ids:
        return {'ok': False, 'authority': None,
                'reason': 'new stock-option name needs a verified full closure of an existing name, or explicit initial-entry authority; a count under %s is not a bootstrap' % MAX_UNDERLYINGS}
    return {'ok': True, 'authority': 'replacement', 'reason': 'replacement', 'closure_id': ids[0]}


def technical_score(row):
    """Strength of one closed 1h setup. Missing numbers are not a score."""
    try:
        close = float(row.get('close') or 0)
        hist = abs(float(row.get('hist') or 0))
        adx = float(row.get('adx') or 0)
        if row.get('gap_pct') is None:
            ema = float(row.get('ema20') or 0)
            gap = abs((close - ema) / ema * 100) if ema else 0.0
        else:
            gap = abs(float(row.get('gap_pct') or 0))
    except (TypeError, ValueError):
        return None
    if close <= 0:
        return None
    return adx + 1000.0 * hist / close + 2.0 * gap


def rank_setups(rows):
    """Strongest closed-1h setup first. Equal scores sort by underlying."""
    ranked = []
    for row in rows or []:
        if not row.get('side'):
            continue
        name = row.get('underlying') or row.get('symbol')
        score = technical_score(row)
        if not name or score is None:
            continue
        ranked.append((score, str(name), row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [dict(row, underlying=name, score=score) for score, name, row in ranked]


def next_new(rows, name_counts, replacement=None, initial_entry=False, pending_new=0):
    """The single strongest new name, or nothing. Never a sell or a rotation."""
    ranked = rank_setups(rows)
    if not ranked:
        return {'action': None, 'underlying': None, 'reason': 'no closed 1h candidate', 'actions': []}
    counts = name_counts or {}
    admit = admit_new(counts.get('upstox'), counts.get('kite'), replacement,
                      initial_entry is True, pending_new)
    top = ranked[0]
    if not admit['ok']:
        return {'action': None, 'underlying': top['underlying'], 'score': top['score'],
                'reason': admit['reason'], 'actions': []}
    action = {'action': 'NEW', 'underlying': top['underlying'], 'score': top['score'],
              'authority': admit['authority'], 'closure_id': admit.get('closure_id')}
    return {'action': 'NEW', 'underlying': top['underlying'], 'score': top['score'],
            'reason': None, 'authority': admit['authority'], 'closure_id': admit.get('closure_id'),
            'actions': [action]}


# An add is not a new name and does not use a closure. The sender stays off:
# this process has no broker log of a successful stop modification.
FINAL_SESSIONS = 4
ADD_EXECUTION_ENABLED = False
EXECUTION_BLOCKER = (
    'Stock-option adds are not sent. This process has no broker-acknowledged '
    'history of a tighter stop on the current position: the live order shows a '
    'trigger, not proof that a modify succeeded after the initial stop. '
    'Covering the full post-fill quantity without cancelling the existing stop '
    'first, and without lowering that trigger, is not validated here. A one-leg '
    'fill is not repaired from this module. No stop event is invented.'
)


def _no_add(reason):
    return {'ok': False, 'action': None, 'send': False, 'symbol': None, 'add_id': None,
            'reasons': [reason], 'blocker': EXECUTION_BLOCKER}


def _when(value):
    if value is None:
        return None
    try:
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=IST)
            return value.astimezone(IST)
        if isinstance(value, str):
            parsed = datetime.datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=IST)
            return parsed.astimezone(IST)
    except Exception:
        return None
    return None


def closed_1h_strong(snapshot, contract_side):
    """Completed 1h trend. A missing field is not a pass. The side must match the open contract."""
    if not isinstance(snapshot, dict):
        return False, 'no completed 1h snapshot'
    if snapshot.get('timeframe') != 'hours' or snapshot.get('interval') != 1 or snapshot.get('closed') is not True:
        return False, 'completed 1h bar required'
    try:
        close = float(snapshot['close'])
        ema = float(snapshot['ema20'])
        direction = int(snapshot['dir'])
        hist = float(snapshot['hist'])
        pdi = float(snapshot['pdi'])
        mdi = float(snapshot['mdi'])
        adx = float(snapshot['adx'])
        prev = float(snapshot['prev_adx'])
    except (KeyError, TypeError, ValueError):
        return False, '1h snapshot is incomplete'
    if contract_side not in ('CE', 'PE'):
        return False, 'open contract side is not CE or PE'
    aligned = 'CE' if (close > ema and direction == 1) else ('PE' if (close < ema and direction == -1) else None)
    if aligned != contract_side:
        return False, '1h EMA20/Supertrend does not agree with the open contract'
    if adx < 25 or not adx > prev:
        return False, 'ADX is not rising through 25'
    if aligned == 'CE' and hist <= 0:
        return False, 'MACD histogram is not positive'
    if aligned == 'PE' and hist >= 0:
        return False, 'MACD histogram is not negative'
    if aligned == 'CE' and not pdi > mdi:
        return False, 'DMI is not positive'
    if aligned == 'PE' and not mdi > pdi:
        return False, 'DMI is not negative'
    return True, None


def _first_tighten(events, position):
    """The earliest real tighten on this position, or (None, reason)."""
    if not isinstance(position, dict):
        return None, 'position is missing'
    symbol = (position.get('symbol') or '').upper()
    if not is_stock_option(symbol):
        return None, 'position is not one stock-option contract'
    entry = str(position.get('entry_id') or '')
    life = str(position.get('lifecycle_id') or '')
    opened = _when(position.get('opened_at'))
    stop = position.get('live_stop') if isinstance(position.get('live_stop'), dict) else None
    if not entry or not life or opened is None or stop is None:
        return None, 'current position lifecycle or its live stop is missing'
    try:
        pos_qty = int(position.get('qty') or 0)
        live_trigger = float(stop.get('trigger'))
        live_qty = int(stop.get('qty') or 0)
    except (TypeError, ValueError):
        return None, 'live stop coverage is unreadable'
    if pos_qty <= 0 or live_qty < pos_qty or live_trigger <= 0:
        return None, 'live stop does not cover the full current quantity'
    if (stop.get('symbol') or '').upper() != symbol:
        return None, 'live stop is not on the open contract'
    if str(stop.get('status') or '').lower() in TERMINAL_STATUSES or not stop.get('order_id'):
        return None, 'live stop is not an active order'
    usable = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if (event.get('symbol') or '').upper() != symbol:
            continue
        if str(event.get('entry_id') or '') != entry or str(event.get('lifecycle_id') or '') != life:
            continue
        if (event.get('broker') or '') != position.get('broker'):
            continue
        moment = _when(event.get('event_time'))
        if moment is None or moment <= opened:
            continue
        if event.get('kind') != 'modify':
            continue
        if event.get('acknowledged') is not True or event.get('failed') is not False:
            continue
        if event.get('proposed') is not False or event.get('shadow') is not False:
            continue
        if event.get('quantity_only') is not False or event.get('trigger_changed') is not True:
            continue
        if str(event.get('order_id') or '') != str(stop.get('order_id')):
            continue
        try:
            previous = float(event.get('previous_trigger'))
            new = float(event.get('new_trigger'))
            confirmed = float(event.get('confirmed_trigger'))
        except (TypeError, ValueError):
            continue
        if not (new > previous and confirmed == new and live_trigger + 1e-9 >= new):
            continue
        if not event.get('event_id'):
            continue
        usable.append((moment, str(event['event_id']), event))
    if not usable:
        return None, 'no broker-acknowledged tighter stop on this position after the initial stop'
    usable.sort(key=lambda item: (item[0], item[1]))
    return usable[0][2], None


def _stop_in_book(orders, position):
    """The loaded order book must still show the stop the evidence names."""
    stop = position.get('live_stop') or {}
    want = str(stop.get('order_id') or '')
    symbol = (position.get('symbol') or '').upper()
    if orders is None or not want:
        return False
    for order in orders:
        if str(order.get('order_id') or '') != want:
            continue
        if (order.get('transaction_type') or '').upper() != 'SELL':
            return False
        if (order.get('order_type') or '').upper() not in ('SL', 'SL-M'):
            return False
        if _status(order) in TERMINAL_STATUSES or _symbol(order) != symbol:
            return False
        try:
            qty = int(order.get('quantity') or 0)
            trigger = float(order.get('trigger_price') or 0)
            need = int(position.get('qty') or 0)
            claimed = float(stop.get('trigger') or 0)
        except (TypeError, ValueError):
            return False
        return qty >= need and trigger + 1e-9 >= claimed > 0
    return False


def add_decision(upstox, kite, events, signal, consumed=None, pending_adds=None, sessions_left=None,
                 upstox_orders=None, kite_orders=None):
    """Whether the same open contract may be averaged. Never a new contract and never a send.

    `sessions_left` is the number of trading sessions from today through the
    contract expiry, inclusive. Four or fewer skips the add. The next-month
    contract is not offered instead.
    """
    if ADD_EXECUTION_ENABLED:
        return _no_add('add execution flag is on; this module has no safe sender')
    if not isinstance(upstox, dict) or not isinstance(kite, dict):
        return _no_add('both broker positions are required')
    if upstox.get('broker') != 'upstox' or kite.get('broker') != 'kite':
        return _no_add('the two books are not labelled upstox and kite')
    su = (upstox.get('symbol') or '').upper()
    sk = (kite.get('symbol') or '').upper()
    if su != sk or not is_stock_option(su):
        return _no_add('the brokers do not hold the same stock-option contract; it is not rolled')
    side = 'PE' if su.endswith('PE') else ('CE' if su.endswith('CE') else None)
    ok, why = closed_1h_strong(signal, side)
    if not ok:
        return _no_add(why)
    if isinstance(sessions_left, bool) or not isinstance(sessions_left, int):
        return _no_add('expiry window is unreadable; add skipped and the contract is not rolled')
    if sessions_left <= FINAL_SESSIONS:
        return _no_add('existing contract is inside the final %s sessions; add skipped, contract is not rolled' % FINAL_SESSIONS)
    for book in (upstox, kite):
        try:
            ltp = float(book.get('ltp'))
            avg = float(book.get('avg'))
        except (TypeError, ValueError):
            return _no_add('price or average is missing; not averaging')
        if ltp < avg:
            return _no_add('premium is below the average; not averaging down')
    by_broker = {'upstox': [], 'kite': []}
    for event in events or []:
        if isinstance(event, dict) and event.get('broker') in by_broker:
            by_broker[event['broker']].append(event)
    chosen = {}
    for broker, book in (('upstox', upstox), ('kite', kite)):
        event, reason = _first_tighten(by_broker[broker], book)
        if event is None:
            return _no_add('%s: %s' % (broker, reason))
        orders = upstox_orders if broker == 'upstox' else kite_orders
        if not _stop_in_book(orders, book):
            return _no_add('%s live order book does not show full stop coverage for this position' % broker)
        chosen[broker] = event
    add_id = '%s|%s|%s' % (su, chosen['upstox']['event_id'], chosen['kite']['event_id'])
    used = {str(item) for item in (consumed or [])}
    pending = {str(item) for item in (pending_adds or [])}
    if add_id in used or add_id in pending or su in pending:
        return _no_add('this first tightening was already used or an add is already pending')
    return {'ok': True, 'action': 'ADD', 'send': False, 'symbol': su, 'add_id': add_id,
            'reasons': ['eligible once on this first tightening; not sent'],
            'blocker': EXECUTION_BLOCKER, 'brokers': ('upstox', 'kite')}


def guard_add(payload, symbol, broker, this_orders, other_orders):
    """Re-check an add from the order payload. A bare flag is not evidence."""
    if not isinstance(payload, dict):
        return _no_add('stock-option add needs both positions and a broker-acknowledged tighter stop; none was supplied')
    decision = add_decision(
        payload.get('upstox'), payload.get('kite'), payload.get('events'), payload.get('signal'),
        payload.get('consumed'), payload.get('pending_adds'), payload.get('sessions_left'),
        this_orders if broker == 'upstox' else other_orders,
        other_orders if broker == 'upstox' else this_orders)
    if not decision['ok']:
        return decision
    if decision['symbol'] != (symbol or '').upper() or broker not in ('upstox', 'kite'):
        return _no_add('add evidence is for a different contract')
    return decision


def _day(when):
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
