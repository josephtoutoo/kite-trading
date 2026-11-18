"""Date-bounded block on NEW Upstox positions (account 5XC5NC).

18 Nov 2026 15:25 IST (t739u, user): "Next one week don't trade in upstock".
While state/upstox_entry_block.json holds an "until" in the future, no automated path may
open or add to an Upstox position. Exits are untouched: SELLs of held longs, stop-loss SELLs,
GTT stop SELLs, and BUYs that cover an existing short leg still go through.
Kite is never affected by this file.

State file (beside this module):  state/upstox_entry_block.json
    {"until": "2026-11-25T23:59:59+05:30", "reason": "...", "set_by": "..."}
  * file missing            -> no block
  * "until" in the past     -> no block (auto-expires; nothing has to be switched back)
  * file present but unreadable / bad "until" -> BLOCK (fail closed, never silently trade)
Override per process (only with the user's own words): UPSTOX_ENTRY_BLOCK_OFF=1.
Used by trade_lock.check_order (rule R15) and by api.request (last guard before an
Upstox order-place POST)."""
import datetime, json, os

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, 'state', 'upstox_entry_block.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
# Exit-only BUY tags: these BUY orders close an existing SHORT option leg (debit-spread
# cover, expiry square-off cover, results-day exit). They never open a position.
EXIT_BUY_TAGS = frozenset({'spread_cover', 'expiry_sqoff', 'results_exit'})


def status(now=None):
    """-> dict(active: bool, until: str|None, reason: str)."""
    if os.environ.get('UPSTOX_ENTRY_BLOCK_OFF') == '1':
        return {'active': False, 'until': None, 'reason': 'per-process override UPSTOX_ENTRY_BLOCK_OFF=1'}
    if not os.path.exists(STATE):
        return {'active': False, 'until': None, 'reason': 'no block file'}
    now = now or datetime.datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    try:
        d = json.load(open(STATE))
        until = datetime.datetime.fromisoformat(str(d['until']))
        if until.tzinfo is None:
            until = until.replace(tzinfo=IST)
    except Exception as e:
        return {'active': True, 'until': None,
                'reason': f'block file unreadable ({type(e).__name__}) - failing closed'}
    if now <= until:
        return {'active': True, 'until': until.isoformat(),
                'reason': d.get('reason') or 'user paused new Upstox trades'}
    return {'active': False, 'until': until.isoformat(), 'reason': 'expired'}


def message(st):
    u = st.get('until')
    when = datetime.datetime.fromisoformat(u).astimezone(IST).strftime('%d %b %Y %H:%M IST') if u else 'unknown'
    return f"R15: new Upstox positions blocked until {when} ({st.get('reason')}) - exits/stops still allowed"


def _is_place(url):
    u = (url or '').split('?', 1)[0].rstrip('/')
    return ('upstox.com' in u) and (u.endswith('/order/place') or u.endswith('/order/multi/place')
                                    or u.endswith('/order/gtt/place'))


def _bodies(body):
    if isinstance(body, (bytes, bytearray)):
        body = body.decode('utf-8', 'replace')
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return [None]
    if isinstance(body, list):
        return body
    return [body]


def api_refusal(url, body, method='POST'):
    """-> refusal text if this HTTP write would open a new Upstox position, else None.
    Only order-place endpoints are inspected; cancels, modifies and reads pass."""
    if (method or 'POST').upper() != 'POST' or not _is_place(url):
        return None
    st = status()
    if not st['active']:
        return None
    for b in _bodies(body):
        if not isinstance(b, dict):
            return message(st) + ' [order body unreadable]'
        side = str(b.get('transaction_type') or '').upper()
        if side == 'BUY' and str(b.get('tag') or '') not in EXIT_BUY_TAGS:
            return message(st)
    return None
