#!/usr/bin/env python3
"""Save real ATM option premiums on index expiry days (user, 4 Nov 2026 t334u).

Why: every expiry-day conclusion we have is in NIFTY index points, because expired-option candles
are unavailable (Upstox wants a Plus plan, Kite's historicals return 403). Points do not pay; premium
does. So from now on, every time an index expires we snapshot its ATM call and put through the day.
In a few weeks that is a real premium series, built on our own data.

Two modes:
  snap   - record the live ATM CE/PE (ltp, oi, volume, bid/ask) plus the index level. Cheap, run often.
  close  - after the close, fill in the 5-minute candle history for the same contracts, so the day is
           complete even if a snapshot was missed.

CLI: python3 atm_premium_log.py snap | close | show [--date YYYY-MM-DD]
Writes /workspace/equity/atm_premium/YYYY-MM-DD.jsonl (snapshots) and .json (daily candles).
Read-only: it never places, modifies or cancels an order.
"""
import os, sys, json, glob, datetime, urllib.parse

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, '/workspace/upstox')
sys.path.insert(0, D)
import api

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OUT = os.path.join(D, 'atm_premium')
INDEX = {
    'NIFTY':     dict(file='NSE', key='NSE_INDEX|Nifty 50',      step=50),
    'BANKNIFTY': dict(file='NSE', key='NSE_INDEX|Nifty Bank',    step=100),
    'SENSEX':    dict(file='BSE', key='BSE_INDEX|SENSEX',        step=100),
}


def instruments(name, day):
    """Option rows for one index from the newest master file on or before `day`."""
    f = INDEX[name]['file']
    files = sorted(glob.glob(f'/workspace/upstox/instruments_{f}_*.json.gz'))
    if not files:
        raise RuntimeError(f'no {f} instruments file')
    rows = api.gzip_json(files[-1])
    seg = 'NSE_FO' if f == 'NSE' else 'BSE_FO'
    return [r for r in rows if r.get('segment') == seg and r.get('name') == name]


CACHE = os.path.join(D, 'atm_premium', 'expiry_today.json')


def expiring(names, now):
    """Which of `names` expire today, with their strike grid. Empty on a non-expiry day.
    Cached per day: the master file is 2-38 MB, and this script runs every 5 minutes."""
    cached = {}
    try:
        cached = json.load(open(CACHE))
    except Exception:
        cached = {}
    if cached.get('date') == now.date().isoformat():
        # names only: the rows themselves are re-read below from the same master, which is cached
        # in-process by api.gzip_json and cheap when the file is unchanged
        names = [n for n in names if n in (cached.get('names') or [])]
        if not names:
            return {}
    out = {}
    for n in names:
        try:
            rows = instruments(n, now.date())
        except Exception as e:
            print(f'atm_premium: {n} instruments unavailable: {e}', file=sys.stderr)
            continue
        today = [r for r in rows if datetime.datetime.fromtimestamp(r['expiry'] / 1000, IST).date() == now.date()]
        if not today:
            continue
        out[n] = today
    if not cached.get('date') == now.date().isoformat():
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            json.dump({'date': now.date().isoformat(), 'names': sorted(out)}, open(CACHE, 'w'))
        except Exception:
            pass
    return out


def ltp(key):
    try:
        d = api.get('/v2/market-quote/ltp?instrument_key=' + urllib.parse.quote(key, safe='')).get('data') or {}
        v = list(d.values())[0] if d else {}
        return float(v.get('last_price') or 0) or None
    except Exception as e:
        print(f'atm_premium: ltp {key} failed: {type(e).__name__}: {e}', file=sys.stderr)
        return None


def quotes(keys):
    """Batched full quotes (ltp, oi, volume, bid/ask). Returns {instrument_key: row}."""
    out = {}
    keys = [k for k in keys if k]
    for i in range(0, len(keys), 20):
        q = urllib.parse.quote(','.join(keys[i:i + 20]), safe='')
        try:
            d = api.get('/v2/market-quote/quotes?instrument_key=' + q).get('data') or {}
        except Exception as e:
            print(f'atm_premium: quote batch failed: {type(e).__name__}: {e}', file=sys.stderr)
            d = {}
        for v in d.values():
            out[v.get('instrument_token')] = v
    return out


def atm_rows(name, rows, spot):
    """The at-the-money CE and PE nearest `spot`."""
    step = INDEX[name]['step']
    strike = round(spot / step) * step
    picked = []
    for kind in ('CE', 'PE'):
        cand = [r for r in rows if r.get('instrument_type') == kind]
        if not cand:
            continue
        best = min(cand, key=lambda r: abs(float(r.get('strike_price') or 0) - strike))
        picked.append(best)
    return strike, picked


def snap(now):
    day = now.date().isoformat()
    exp = expiring(list(INDEX), now)
    if not exp:
        return {'now': now.strftime('%Y-%m-%d %H:%M IST'), 'action': 'no index expires today - nothing to capture'}
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f'{day}.jsonl')
    rec = {'ts': now.strftime('%Y-%m-%d %H:%M:%S'), 'indices': {}}
    for name, rows in exp.items():
        spot = ltp(INDEX[name]['key'])
        if not spot:
            rec['indices'][name] = {'error': 'no index price'}
            continue
        strike, picked = atm_rows(name, rows, spot)
        q = quotes([r['instrument_key'] for r in picked])
        legs = {}
        for r in picked:
            v = q.get(r['instrument_key']) or {}
            legs[r['instrument_type']] = {
                'symbol': r['trading_symbol'], 'key': r['instrument_key'],
                'strike': float(r.get('strike_price') or 0), 'lot': int(r.get('lot_size') or 0),
                'ltp': float(v.get('last_price') or 0) or None,
                'oi': int(v.get('oi') or 0) or None,
                'volume': int(v.get('volume') or 0) or None,
                'bid': (v.get('depth') or {}).get('buy', [{}])[0].get('price') if v.get('depth') else None,
                'ask': (v.get('depth') or {}).get('sell', [{}])[0].get('price') if v.get('depth') else None,
            }
        rec['indices'][name] = {'spot': spot, 'atm_strike': strike, 'legs': legs}
    with open(path, 'a') as fh:
        fh.write(json.dumps(rec) + '\n')
    return {'now': rec['ts'], 'wrote': path, 'indices': {k: v.get('atm_strike', v.get('error')) for k, v in rec['indices'].items()}}


def candles(key, day):
    """5-minute candles for one contract on `day` (works intraday and after the close)."""
    f = day.isoformat()
    url = f'https://api.upstox.com/v3/historical-candle/{urllib.parse.quote(key, safe="")}/minutes/5/{f}/{f}'
    try:
        d = api.get(url).get('data') or {}
    except Exception as e:
        print(f'atm_premium: candles {key} failed: {type(e).__name__}: {e}', file=sys.stderr)
        return []
    return d.get('candles') or []


def close(now):
    """Fill in the full 5-minute history for the contracts captured today."""
    day = now.date()
    path = os.path.join(OUT, f'{day.isoformat()}.jsonl')
    if not os.path.exists(path):
        return {'action': 'no snapshots today - nothing to complete'}
    seen, out = {}, {}
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        for name, v in (r.get('indices') or {}).items():
            for kind, leg in (v.get('legs') or {}).items():
                seen[(name, kind)] = leg
    for (name, kind), leg in seen.items():
        cs = candles(leg['key'], day)
        out[f'{name} {kind}'] = {'symbol': leg['symbol'], 'key': leg['key'], 'atm_strike': leg['strike'],
                                 'candles': cs, 'n': len(cs)}
    dest = os.path.join(OUT, f'{day.isoformat()}.json')
    json.dump(out, open(dest, 'w'), indent=1)
    return {'wrote': dest, 'series': {k: v['n'] for k, v in out.items()}}


def show(day=None):
    files = sorted(glob.glob(os.path.join(OUT, '*.jsonl')))
    rows = []
    for f in files:
        for line in open(f):
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return {'days': len(files), 'snapshots': len(rows), 'first': rows[0]['ts'] if rows else None,
            'last': rows[-1]['ts'] if rows else None}


if __name__ == '__main__':
    now = datetime.datetime.now(IST)
    what = sys.argv[1] if len(sys.argv) > 1 else 'snap'
    fn = {'snap': snap, 'close': close, 'show': lambda n: show()}[what]
    if what == 'close' and '--time' in sys.argv:
        now = datetime.datetime.strptime(sys.argv[sys.argv.index('--time') + 1], '%Y-%m-%d %H:%M').replace(tzinfo=IST)
    print(json.dumps(fn(now), indent=1, default=str))
