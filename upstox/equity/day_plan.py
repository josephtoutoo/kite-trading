#!/usr/bin/env python3
"""Pre-market capital and condition snapshot (user, 4 Nov 2026 t335u).

"Every day before market opens check for the capital based on that decide and based on the market
conditions." This gathers the facts that decision needs, in one read, so the pre-market routine is
interpreting numbers rather than assembling them:

  * free and used capital on Upstox and Kite, and the commodity purse;
  * MTF room per broker on the margin caps that actually apply (10% per stock, 50% total, half-room
    reserve), counting only names this bot opened;
  * slots free per broker, and the open MTF mirror state;
  * open F&O positions and their P&L;
  * yesterday's closes and the overnight move for NIFTY, BANKNIFTY and SENSEX, plus the 5m/daily
    trend of each, so the day's index bias comes from the chart, not a guess.

CLI: python3 day_plan.py            # all of it, as JSON
     python3 day_plan.py --capital  # capital and room only (cheap)
Read-only. Never places, modifies or cancels an order.
"""
import sys, json, datetime, os

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, D)
import api
import mtf_sizing as ms

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IDX = {'NIFTY': 'NSE_INDEX|Nifty 50', 'BANKNIFTY': 'NSE_INDEX|Nifty Bank', 'SENSEX': 'BSE_INDEX|SENSEX'}


def upstox_capital():
    d = api.get('/v2/user/get-funds-and-margin')['data']
    eq = d.get('equity') or {}
    cm = d.get('commodity') or {}
    avail = float(eq.get('available_margin') or 0)
    used = float(eq.get('used_margin') or 0)
    return dict(available=round(avail), used=round(used), capital=round(avail + used),
                commodity_available=round(float(cm.get('available_margin') or 0)))


def kite_capital():
    import broker_kite as bk
    if not bk.valid():
        return {'ok': False, 'why': bk.why()}
    m = bk.client().margins('equity')
    av = m.get('available') or {}
    net = float(m.get('net') or 0)
    free = min(net, float(av.get('live_balance') or 0) + float(av.get('collateral') or 0))
    return {'ok': True, 'net': round(net), 'free': round(free),
            'utilised_debits': round(float((m.get('utilised') or {}).get('debits') or 0))}


def _ltp(symbols):
    """Upstox last price per symbol. Needed because Upstox quotes margin PER SHARE and ignores the
    price you pass (GAIL returned 45.996 at Rs 100 and at Rs 183.59), so the ratio needs the real
    price. Kite needs no price: its quote is linear, 25.5 at Rs 100 and 227.7 at Rs 893."""
    out = {}
    if not symbols:
        return out
    try:
        km = ms.keymap()
        want = {km[s]: s for s in symbols if s in km}
        if not want:
            return out
        d = api.get('/v2/market-quote/ltp?instrument_key=' + ','.join(want)).get('data') or {}
        for v in d.values():
            s_ = want.get(v.get('instrument_token'))
            if s_:
                out[s_] = float(v['last_price'])
    except Exception as e:
        out['_error'] = f'{type(e).__name__}: {e}'
    return out


def _ratios(broker, symbols):
    """margin / notional per name from the broker's own MTF margin quote.

    One ratio applied to the whole book was wrong: the names differ (Upstox RVNL blocks 29% of
    notional, GAIL 25%; and the old code divided the per-share quote by the wrong base, which is
    what made the blocked margin look like a few hundred rupees)."""
    ltps = _ltp(list(symbols)) if broker == 'upstox' else {}
    out, unknown = {}, []
    for s_ in symbols:
        px = ltps.get(s_) if broker == 'upstox' else 100.0
        if not px:
            unknown.append(s_)
            continue
        q = ms.margin_quote(broker, s_, 1, px)
        if not q.get('ok'):
            unknown.append(s_)
            continue
        out[s_] = round(float(q['margin']) / px, 4)
    return out, unknown


def mtf_room(broker):
    """Room on the caps that actually apply, counting only my own names, each at its own margin rate."""
    try:
        st = ms.state_mine(broker)
    except Exception as e:
        return {'ok': False, 'why': f'{type(e).__name__}: {e}'}
    cap = float(st['capital'])
    held = st['held']
    used = float(st['used'])                 # at cost
    ratios, unknown = _ratios(broker, held)
    used_m = sum(cost * ratios.get(s_, 0.245) for s_, cost in held.items())
    if unknown:                               # fallback ratio, flagged so the number is not trusted blindly
        used_m += sum(cost * 0.245 for s_, cost in held.items() if s_ in unknown)
    per = 0.10 * cap
    total = 0.50 * cap
    room_total = max(total - used_m, 0.0)
    slots = ms.slots(broker)
    return {'ok': True, 'capital': round(cap), 'free': round(float(st.get('free') or 0)),
            'mine': sorted(held), 'mine_count': len(held),
            'not_mine': sorted((st.get('not_mine') or {}).keys()),
            'mtf_used_cost': round(used), 'mtf_used_margin': round(used_m),
            'margin_ratios': ratios, 'ratio_unknown': unknown,
            'per_stock_cap': round(per), 'total_cap': round(total),
            'room_total': round(room_total), 'room_half': round(room_total * 0.5),
            'slots_cap': slots.get('cap'), 'free_slots': slots.get('free_slots')}


def positions():
    out = {'upstox_fno': [], 'kite_fno': []}
    try:
        for p in api.get('/v2/portfolio/short-term-positions').get('data') or []:
            q = int(p.get('quantity') or 0)
            if q > 0:
                out['upstox_fno'].append({'sym': p.get('trading_symbol'), 'qty': q,
                                          'avg': p.get('average_price'), 'ltp': p.get('last_price'),
                                          'pnl': p.get('pnl')})
    except Exception as e:
        out['upstox_error'] = f'{type(e).__name__}: {e}'
    try:
        import broker_kite as bk
        if bk.valid():
            for p in bk.positions():
                q = int(p.get('quantity') or 0)
                if q > 0 and (p.get('segment') or '').startswith(('NFO', 'BFO', 'MCX')):
                    out['kite_fno'].append({'sym': p.get('trading_symbol'), 'qty': q,
                                            'avg': p.get('average_price'), 'ltp': p.get('last_price'),
                                            'pnl': p.get('pnl')})
    except Exception as e:
        out['kite_error'] = f'{type(e).__name__}: {e}'
    return out


def conditions():
    """Index levels and trend, from our own candles."""
    import ind
    out = {}
    for name, key in IDX.items():
        try:
            a = ind.analyze(key, 'minutes', 5, 5)
            d = ind.analyze(key, 'days', 1, 60)
            out[name] = {'close': a['close'], 'ema20_5m': a['ema20'], 'st_5m': a['st'],
                         'dir_5m': a['dir'], 'adx_5m': round(a.get('adx') or 0, 1),
                         'day_close': d['close'], 'day_ema20': d['ema20'], 'day_dir': d['dir'],
                         'last_ts': a['last_ts']}
        except Exception as e:
            out[name] = {'error': f'{type(e).__name__}: {e}'}
    return out


def main():
    now = datetime.datetime.now(IST)
    out = {'now': now.strftime('%Y-%m-%d %H:%M IST'), 'weekday': now.strftime('%A')}
    try:
        out['upstox'] = upstox_capital()
    except Exception as e:
        out['upstox'] = {'error': f'{type(e).__name__}: {e}'}
    out['kite'] = kite_capital()
    if '--capital' not in sys.argv:
        out['mtf'] = {b: mtf_room(b) for b in ('upstox', 'kite')}
        out['positions'] = positions()
        out['conditions'] = conditions()
    print(json.dumps(out, indent=1, default=str))


if __name__ == '__main__':
    main()
