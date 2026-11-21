"""Persistent switch: no Kite (XE9253) order, GTT, modify, or cancel.

21 Nov 2026 (t783u, t784u): "Stop trading kite monday onwards" / "But use upstock".
The user then confirmed: "Stop all Kite activity including stops, leave positions as they are".
From Monday 23 Nov 2026 00:00 IST, every Kite write is a logged no-op. Open Kite
positions and existing stops/GTTs are left untouched. Upstox is never affected
by this file.

Switch file (beside this module):  kite_disabled.json
    {"disabled": true, "from": "2026-11-23T00:00:00+05:30",
     "reason": "kite disabled by user 21 Nov", "set_by": "t783u t784u"}
  * file missing              -> Kite writes allowed
  * disabled is false         -> Kite writes allowed
  * "from" still in the future -> not yet active
  * file present but unreadable / bad "from" -> DISABLED (fail closed, never silently trade)
Optional process override (tests only): KITE_DISABLED_OFF=1.
Honoured by broker_kite (place/modify/cancel and client write methods), trade_lock R16,
and dual-broker planners that would otherwise require a Kite leg before sending Upstox.
"""
import datetime, json, os

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, 'kite_disabled.json')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
NOOP = 'kite disabled by user 21 Nov'
WRITE_METHODS = frozenset({
    'place_order', 'modify_order', 'cancel_order',
    'place_gtt', 'modify_gtt', 'delete_gtt',
    'convert_position',
})


def status(now=None):
    """-> dict(active: bool, since: str|None, reason: str)."""
    if os.environ.get('KITE_DISABLED_OFF') == '1':
        return {'active': False, 'since': None, 'reason': 'per-process override KITE_DISABLED_OFF=1'}
    if not os.path.exists(STATE):
        return {'active': False, 'since': None, 'reason': 'no disable file'}
    now = now or datetime.datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    try:
        d = json.load(open(STATE))
        disabled = d.get('disabled', True)
        if isinstance(disabled, str):
            disabled = disabled.strip().lower() in ('1', 'true', 'yes', 'on')
        raw_from = d.get('from') or d.get('since')
        since = None
        if raw_from:
            since = datetime.datetime.fromisoformat(str(raw_from))
            if since.tzinfo is None:
                since = since.replace(tzinfo=IST)
    except Exception as e:
        return {'active': True, 'since': None,
                'reason': f'disable file unreadable ({type(e).__name__}) - failing closed'}
    if not disabled:
        return {'active': False, 'since': since.isoformat() if since else None,
                'reason': d.get('reason') or 'disabled flag is false'}
    if since is not None and now < since:
        return {'active': False, 'since': since.isoformat(),
                'reason': 'not yet active (from %s)' % since.isoformat()}
    return {'active': True, 'since': since.isoformat() if since else None,
            'reason': d.get('reason') or NOOP}


def active(now=None):
    return bool(status(now)['active'])


def message(st=None):
    st = st or status()
    return "R16: %s - no Kite orders, stops, or GTTs; existing positions left as they are (%s)" % (
        NOOP, st.get('reason') or NOOP)


def skip_reason(what=None):
    if what:
        return '%s: no-op %s' % (NOOP, what)
    return NOOP


class _WriteGuard:
    """Proxy that no-ops Kite write methods while the switch is on. Reads pass through."""

    def __init__(self, inner):
        object.__setattr__(self, '_inner', inner)

    def __getattr__(self, name):
        attr = getattr(object.__getattribute__(self, '_inner'), name)
        if name in WRITE_METHODS and callable(attr):
            def _wrapped(*a, **k):
                if active():
                    return None
                return attr(*a, **k)
            return _wrapped
        return attr


def wrap_client(inner):
    if inner is None or isinstance(inner, _WriteGuard):
        return inner
    return _WriteGuard(inner)
