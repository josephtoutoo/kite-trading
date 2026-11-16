#!/usr/bin/env python3
"""Strong-technical stock-option entries, mirrored on Upstox and Kite.

Closed 1h candles only. A candidate needs close/EMA20 + Supertrend alignment,
MACD histogram and DMI in its direction, and rising ADX >= 25 (ind.strong).
Nearest unexpired ATM CE/PE, LIMIT at ask (or 0.5% over LTP), tag stockopt_tech.
trade_lock.check_order is called for BOTH legs before either would be written.
R11 caps distinct underlyings at 3 per broker, pending names included. The old
4-underlying figure is not applied. Lots stay 1, or 2 only on the dated override.

TECHNICAL_PAUSED is on. This file evaluates and writes the decision log. It does
not place an order. An add to a name already held is not decided here; that rule
is equity/stockopt_add.py and is also paused. --dry-run still places nothing.

Every run, including a no-trade run and an early exit, writes a decision file
under equity/logs/stockopt_tech/ (see stockopt_audit.py). That log is not a
reconstruction of earlier scans. A failed log write is reported and does not
change the order decision.
"""
import argparse, datetime as dt, json, os, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import api, broker_kite as bk, ind, market_calendar, trade_lock as tl
import stockopt_audit as audit

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TAG = 'stockopt_tech'
ADX_MIN = 25
LOTS = 2                 # R11 can reduce this; it can never increase it.
# This LIQUID / highly-traded scanner stays paused. It must not buy names outside
# the Supertrend-versus-EMA weekend list. That list is a separate path
# (equity/stockopt_st_ema_entry.py): a fresh 1h trigger or an already-aligned
# first entry, 1 lot, and only when STOCKOPT_ST_EMA_EXECUTE=1.
TECHNICAL_PAUSED = True
# Highly traded NSE F&O underlyings; rows without a current option chain are skipped.
LIQUID = '''RELIANCE HDFCBANK ICICIBANK SBIN INFY TCS BHARTIARTL AXISBANK KOTAKBANK
LT ITC HINDUNILVR MARUTI BAJFINANCE SUNPHARMA NTPC POWERGRID TATASTEEL TATAMOTORS
JSWSTEEL HINDALCO ADANIENT ADANIPORTS COALINDIA ONGC BPCL IOC GRASIM ULTRACEMCO
TITAN ASIANPAINT NESTLEIND TECHM WIPRO HCLTECH M&M BAJAJFINSV SBILIFE HDFCLIFE
INDUSINDBANK DLF VEDL JINDALSTEL'''.split()


def signal(a):
    ok, side, detail = ind.strong(a, ADX_MIN)
    if not ok:
        # aligned_side is recorded so the audit can say why. The returned side stays None.
        return None, {**detail, 'aligned_side': side}
    if (side == 'CE' and a['hist'] <= 0) or (side == 'PE' and a['hist'] >= 0):
        return None, {**detail, 'skip': 'MACD histogram not aligned'}
    if (side == 'CE' and a['pdi'] <= a['mdi']) or (side == 'PE' and a['mdi'] <= a['pdi']):
        return None, {**detail, 'skip': 'DMI not aligned'}
    return side, detail


def master(today):
    path = os.path.join(ROOT, 'instruments_NSE_%s.json.gz' % today)
    if not os.path.exists(path):
        url = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=60).read()
        tmp = path + '.part'
        with open(tmp, 'wb') as f: f.write(raw)
        os.replace(tmp, path)
    return api.gzip_json(path)


def chains(rows, today):
    out = {}
    for x in rows:
        if x.get('segment') != 'NSE_FO' or x.get('instrument_type') not in ('CE', 'PE'): continue
        u = x.get('underlying_symbol') or x.get('name')
        if u not in LIQUID or not x.get('underlying_key') or not x.get('expiry'): continue
        expiry = dt.datetime.fromtimestamp(x['expiry'] / 1000, IST).date()
        if expiry < today: continue
        out.setdefault(u, []).append({**x, 'expiry_date': expiry})
    return out


def pick(rows, side, close, today):
    if not rows: return None
    expiry = min(x['expiry_date'] for x in rows)
    same = [x for x in rows if x['expiry_date'] == expiry and x['instrument_type'] == side]
    return min(same, key=lambda x: abs(float(x.get('strike_price') or 0) - close)) if same else None


def quote(key):
    import urllib.parse
    # /v2/market-quote/full returns HTTP 400 UDAPI100012. /quotes has the
    # same last_price, depth and tick fields this helper returns.
    d = api.get('/v2/market-quote/quotes?instrument_key=' + urllib.parse.quote(key, safe='')).get('data') or {}
    v = next(iter(d.values()), {})
    ltp = float(v.get('last_price') or 0)
    try: ask = float(v['depth']['sell'][0]['price'])
    except Exception: ask = 0
    tick = float(v.get('tick_size') or 0.05)
    if tick > 1: tick /= 100
    return ltp, ask, tick


def tick(price, size=0.05):
    return round(max(round(float(price) / size) * size, size), 2)


def has_long(positions, underlying):
    return any(int(p.get('quantity') or 0) > 0 and tl.is_opt(tl.sym_of(p))
               and not tl.is_index(tl.sym_of(p)) and tl.underlying(tl.sym_of(p)) == underlying
               for p in positions)


def _window(now):
    """(inside, reason). Dry-run still uses this only as a recorded fact; the caller
    decides whether it stops the scan. The clock test itself is unchanged."""
    if not market_calendar.is_trading_day(now.date()):
        return False, 'not a trading day'
    if not (dt.time(10, 30) <= now.time() <= dt.time(14, 30)):
        return False, 'outside 10:30-14:30 IST trading-day entry window'
    return True, 'inside 10:30-14:30 IST'


def _save(started, status, outcome, dry, symbols, evidence, error, window, max_entries,
          loop_stopped, result, audit_dir, do_print):
    """Write the decision file. A write failure is attached and does not raise."""
    finished = dt.datetime.now(IST)
    record = audit.build_record(started, finished, status, outcome, dry, symbols, evidence,
                                error=error, window=window, max_entries=max_entries,
                                loop_stopped=loop_stopped)
    try:
        path = audit.write_audit(record, audit_dir, finished)
    except Exception as exc:
        print('stockopt_tech_entry: decision log failed: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
        if isinstance(result, dict):
            result['audit_error'] = '%s: %s' % (type(exc).__name__, exc)
        return result
    if isinstance(result, dict):
        result['audit_file'] = path
    if do_print and isinstance(result, dict):
        print(json.dumps(result, indent=2, default=str))
    return result


def main(argv=None, now=None, audit_dir=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--symbols', nargs='*', help='optional subset of the liquid universe')
    ap.add_argument('--max-entries', type=int, default=1)
    args = ap.parse_args(argv)
    started = now or dt.datetime.now(IST)
    if started.tzinfo is None:
        started = started.replace(tzinfo=IST)
    now = started.astimezone(IST)
    dry = args.dry_run
    wanted = [u.upper() for u in args.symbols] if args.symbols else list(LIQUID)
    inside, why = _window(now)
    window = {'inside': inside, 'reason': why, 'enforced': not dry}
    evidence = []
    if not dry and not inside:
        # Same early return as before: no book read and no order. The decision file is the only addition.
        return _save(started, 'completed', 'early_exit', dry, wanted, evidence, None, window,
                     args.max_entries, None,
                     {'skip': 'outside 10:30-14:30 IST trading-day entry window', 'now': now.isoformat()},
                     audit_dir, False)
    try:
        rows = master(now.date())
        all_chains = chains(rows, now.date())
        upos = api.get('/v2/portfolio/short-term-positions').get('data') or []
        kpos = bk.positions(strict=True)
        result = {'now': now.isoformat(), 'dry_run': dry, 'tag': TAG, 'technical_paused': TECHNICAL_PAUSED,
                  'candidates': [], 'actions': []}
        loop_stopped = None
        for u in wanted:
            chain = all_chains.get(u)
            if not chain:
                evidence.append({'underlying': u, 'status': 'skipped', 'evaluated': False,
                                 'reason': 'no current option chain',
                                 'brokers': {'upstox': audit.broker_decision(None),
                                             'kite': audit.broker_decision(None)}})
                continue
            if has_long(upos, u) or has_long(kpos, u):
                result['actions'].append({'underlying': u, 'skip': 'stock option already open on a broker'})
                evidence.append({'underlying': u, 'status': 'rejected', 'evaluated': True,
                                 'reason': 'stock option already open on a broker',
                                 'brokers': {'upstox': audit.broker_decision(None),
                                             'kite': audit.broker_decision(None)}})
                continue
            try:
                a = ind.analyze(chain[0]['underlying_key'], 'hours', 1, 60)
                side, detail = signal(a)
                item = {'underlying': u, 'candle': a['last_ts'], 'close': a['close'], 'side': side, 'signal': detail}
                if not side:
                    result['candidates'].append(item)
                    evidence.append({'underlying': u, 'status': 'rejected', 'evaluated': True,
                                     'reason': audit.explain_signal(detail), 'candle': a.get('last_ts'),
                                     'close': a.get('close'), 'signal': detail,
                                     'brokers': {'upstox': audit.broker_decision(None),
                                                 'kite': audit.broker_decision(None)}})
                    continue
                opt = pick(chain, side, a['close'], now.date())
                if not opt: raise RuntimeError('no ATM contract')
                lot = int(opt.get('lot_size') or 0)
                if not lot: raise RuntimeError('missing lot size')
                ltp, ask, ts = quote(opt['instrument_key'])
                px = tick(ask or ltp * 1.005, ts)
                if not ltp and not ask: raise RuntimeError('no usable quote')
                km = bk.from_upstox(opt['instrument_key'])
                if not km: raise RuntimeError('Kite symbol mapping unavailable')
                item.update(symbol=opt['trading_symbol'], instrument_key=opt['instrument_key'],
                            expiry=opt['expiry_date'].isoformat(), strike=opt['strike_price'],
                            lot_size=lot, price=px, kite_symbol=km['tradingsymbol'])
                uo = {'trading_symbol': '%s%s%s%s' % (u, opt['expiry_date'].strftime('%y%m%d'),
                      int(opt['strike_price']), side), 'instrument_token': opt['instrument_key'],
                      'transaction_type': 'BUY', 'order_type': 'LIMIT', 'product': 'D',
                      'quantity': LOTS * lot, 'price': px, '_paired': True}
                ko = {'trading_symbol': km['tradingsymbol'], 'instrument_token': opt['instrument_key'],  # lets R11 resolve the stock lot size
                      'transaction_type': 'BUY', 'order_type': 'LIMIT', 'product': 'NRML',
                      'quantity': LOTS * int(km['lot_size']), 'price': px, '_paired': True}
                uok, uwhy, uq = tl.check_order(uo, broker='upstox')
                kok, kwhy, kq = tl.check_order(ko, broker='kite')
                uq, kq = uq // lot * lot, kq // int(km['lot_size']) * int(km['lot_size'])
                item['checks'] = {'upstox': {'ok': uok, 'reasons': uwhy, 'qty': uq},
                                  'kite': {'ok': kok, 'reasons': kwhy, 'qty': kq}}
                brokers = {'upstox': audit.broker_decision(item['checks']['upstox']),
                           'kite': audit.broker_decision(item['checks']['kite'])}
                if not (uok and kok and uq > 0 and kq > 0):
                    item['skip'] = 'no slots / one or both brokers refused the full mirror'
                    result['actions'].append(item)
                    evidence.append({'underlying': u, 'status': 'rejected', 'evaluated': True,
                                     'reason': item['skip'], 'candle': a.get('last_ts'), 'close': a.get('close'),
                                     'symbol': item.get('symbol'), 'brokers': brokers})
                    continue
                if dry:
                    item['action'] = 'DRY-RUN: both legs eligible; no orders placed'
                elif TECHNICAL_PAUSED:
                    item['action'] = 'PAUSED: technical stock-option entries are not sent'
                    item['skip'] = 'technical routine paused pending an independent allocation'
                else:
                    body = {'quantity': uq, 'product': 'D', 'validity': 'DAY', 'price': px, 'tag': TAG,
                            'instrument_token': opt['instrument_key'], 'order_type': 'LIMIT',
                            'transaction_type': 'BUY', 'disclosed_quantity': 0, 'trigger_price': 0, 'is_amo': False}
                    ur = api.call('https://api-hft.upstox.com/v2/order/place', body, 'POST', proxy=True)
                    uid = (ur.get('data') or {}).get('order_id')
                    item['upstox_order'] = uid or ur
                    if uid:
                        item['kite_order'] = bk.place(km['exchange'], km['tradingsymbol'], 'BUY', kq, px, tag=TAG)
                        if item['kite_order']:
                            tl.record_entry()
                        else:
                            item['warning'] = 'Upstox accepted but Kite failed; inspect/cancel Upstox before retrying'
                    else:
                        item['skip'] = 'Upstox rejected; Kite was not sent'
                result['actions'].append(item)
                sent = (isinstance(item.get('upstox_order'), str) and bool(item.get('upstox_order'))
                        or bool(item.get('kite_order')))
                evidence.append({'underlying': u, 'status': 'paused' if (not dry and TECHNICAL_PAUSED) else 'eligible',
                                 'evaluated': True,
                                 'reason': item.get('skip') or item.get('warning') or item.get('action'),
                                 'candle': a.get('last_ts'), 'close': a.get('close'),
                                 'symbol': item.get('symbol'), 'brokers': brokers,
                                 'orders_placed': sent, 'dry_run': dry})
                if len([x for x in result['actions'] if x.get('action', '').startswith(('DRY-RUN',)) or x.get('kite_order')]) >= args.max_entries:
                    loop_stopped = 'max_entries'
                    break
            except Exception as e:
                message = '%s: %s' % (type(e).__name__, e)
                result['actions'].append({'underlying': u, 'error': message})
                evidence.append({'underlying': u, 'status': 'error', 'evaluated': True, 'reason': message,
                                 'brokers': {'upstox': audit.broker_decision(None),
                                             'kite': audit.broker_decision(None)}})
        orders = any(row.get('orders_placed') for row in evidence)
        outcome = 'orders' if orders else 'no_trade'
        return _save(started, 'completed', outcome, dry, wanted, evidence, None, window,
                     args.max_entries, loop_stopped, result, audit_dir, True)
    except Exception as exc:
        _save(started, 'aborted', 'aborted', dry, wanted, evidence, '%s: %s' % (type(exc).__name__, exc),
              window, args.max_entries, None, None, audit_dir, False)
        raise


if __name__ == '__main__':
    main()
