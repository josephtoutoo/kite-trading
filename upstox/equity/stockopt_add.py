#!/usr/bin/env python3
"""Guarded add for a stock option that is already open. Nothing is sent.

The parent rule (6 Nov 2026, 20:40 IST): when an existing stock option is strong
on the completed 1h bar and its trailing stop has been tightened once, an
average of that same contract may be considered. It is not a new name, not a
roll into next month, and not a second average of the same tightening.

This routine is paused. A live run does not read broker accounts and does not
invent a stop modification. Eligibility is computed only from evidence the
caller already holds; the live entry point has none, so it stays ineligible.
stockopt_policy.EXECUTION_BLOCKER is why a fixture that passes is still not sent.
"""
import datetime as dt, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stockopt_audit as audit
import stockopt_policy as pol

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ADD_PAUSED = True
ADD_DIR = os.path.join(HERE, 'logs', 'stockopt_add')


def plan(payload):
    """Eligibility from caller-supplied evidence. `send` stays false."""
    if not isinstance(payload, dict):
        payload = {}
    decision = pol.add_decision(
        payload.get('upstox'), payload.get('kite'), payload.get('events'), payload.get('signal'),
        payload.get('consumed'), payload.get('pending_adds'), payload.get('sessions_left'),
        payload.get('upstox_orders'), payload.get('kite_orders'))
    decision['send'] = False
    decision['paused'] = True
    decision['orders_placed'] = False
    return decision


def main(argv=None, now=None, audit_dir=None):
    """Record that the paused routine did not look for a stop ratchet and did not trade."""
    del argv
    started = now or dt.datetime.now(IST)
    if started.tzinfo is None:
        started = started.replace(tzinfo=IST)
    started = started.astimezone(IST)
    record = {
        'schema': 1,
        'scan': 'stockopt_add',
        'started_at': started.isoformat(),
        'status': 'completed',
        'outcome': 'no_trade',
        'paused': ADD_PAUSED,
        'send': False,
        'eligible': False,
        'orders_placed': False,
        'reason': 'no broker-acknowledged stop-ratchet history is read by this process',
        'blocker': pol.EXECUTION_BLOCKER,
        'completion': 'completed: no orders; add routine paused',
        'candidates': [],
    }
    path = audit.write_audit(record, audit_dir or ADD_DIR, started)
    record['audit_file'] = path
    print(json.dumps({'paused': True, 'send': False, 'eligible': False, 'audit_file': path,
                      'blocker': pol.EXECUTION_BLOCKER}))
    return record


if __name__ == '__main__':
    main()
