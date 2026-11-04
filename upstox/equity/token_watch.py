#!/usr/bin/env python3
"""Are both broker sessions alive right now? (5 Nov 2026, audit #11)

A dead Kite token used to be silent: every Kite script logged one line to kite_broker.log and
carried on, so the MTF stops simply stopped being maintained with nothing said out loud. This
checks each broker the way the trading code will use it, and exits non-zero when a session is
broken **during market hours** so the routine that runs it can say so.

CLI: python3 token_watch.py [--json]
Exit: 0 both fine (or outside market hours) / 1 a session is broken / 2 check itself failed
"""
import sys, json, datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
D = '/workspace/upstox'
sys.path.insert(0, D)
sys.path.insert(0, '/workspace/equity')


def market_open(now=None):
    now = now or datetime.datetime.now(IST)
    try:
        import market_calendar
        holiday = not market_calendar.is_trading_day(now.date())
    except Exception:
        holiday = False
    return now.weekday() < 5 and not holiday and datetime.time(9, 15) <= now.time() <= datetime.time(15, 30)


def check():
    out = {'now': datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'), 'kite': None, 'upstox': None}
    # Kite: the same live profile call broker_kite.valid() makes
    try:
        import broker_kite as bk
        ok = bool(bk.valid())
        out['kite'] = {'ok': ok, 'why': 'ok' if ok else bk.why()}
    except Exception as e:
        out['kite'] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}
    # Upstox: a cheap authenticated read (funds), which every order path also needs
    try:
        import api
        r = api.get('/v2/user/get-funds-and-margin')
        ok = isinstance(r, dict) and r.get('status') == 'success'
        out['upstox'] = {'ok': ok, 'why': 'ok' if ok else f"funds read returned {r.get('status') if isinstance(r, dict) else r}"}
    except Exception as e:
        out['upstox'] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}
    out['market_open'] = market_open()
    out['broken'] = [b for b in ('kite', 'upstox') if not out[b]['ok']]
    return out


if __name__ == '__main__':
    try:
        res = check()
    except Exception as e:
        print(json.dumps({'error': f'{type(e).__name__}: {e}'}))
        sys.exit(2)
    if '--json' in sys.argv or not res['broken']:
        print(json.dumps(res, indent=1))
    else:
        parts = [f"{b} session is down ({res[b]['why']})" for b in res['broken']]
        print(('ALERT: ' if res['market_open'] else 'noted outside market hours: ') + '; '.join(parts))
    sys.exit(1 if (res['broken'] and res['market_open']) else 0)
