#!/usr/bin/env python3
"""Durable decision log for one stock-option technical scan.

Each run writes its own JSON file and appends one JSON line to that day's log.
The file is the evidence for that run only. This module does not reconstruct
earlier scans, and it does not place or cancel orders.

Location (next to this file, on the machine that runs the scan):

    equity/logs/stockopt_tech/runs/<YYYYMMDDTHHMMSS+0530>-<pid>.json
    equity/logs/stockopt_tech/decisions-<YYYYMMDD>.jsonl

Retention: on each successful write, files in that directory whose date is
strictly older than 30 calendar days are deleted. The run just written is kept.
These files are runtime logs. They are gitignored (equity/logs/ and *.jsonl)
and must not be committed. Tokens, secrets, PINs and proxy URLs are redacted
before a record is stored.
"""
import datetime as dt, json, os, re

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
RETAIN_DAYS = 30
SCHEMA = 1
DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'stockopt_tech')

_SENSITIVE = ('access_token', 'refresh_token', 'api_key', 'apikey', 'api_secret', 'apisecret',
              'secret', 'password', 'passwd', 'totp', 'pin', 'mobile', 'authorization',
              'proxy', 'cookie', 'auth_token')
_SCRUB = re.compile(
    r'(?i)((?:bearer\s+|access[_-]?token|api[_-]?key|api[_-]?secret|secret|totp|pin|proxy_url|proxy)'
    r'[\s=:]+)([^\s,"\']+)')


def redact(value):
    """Drop credential-shaped keys and inline secret assignments. Instrument ids stay."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _sensitive(key):
                out[key] = '[redacted]'
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SCRUB.sub(r'\1[redacted]', value)
    return value


def _sensitive(key):
    name = str(key).lower().replace('-', '_')
    if name in ('instrument_token',):
        return False
    return any(part in name for part in _SENSITIVE)


def explain_signal(detail):
    """One explicit reason a setup was not a CE/PE candidate. Does not decide the trade."""
    if not isinstance(detail, dict):
        return 'setup not eligible'
    if detail.get('skip'):
        return str(detail['skip'])
    parts = []
    if not detail.get('aligned_side'):
        parts.append('close/EMA20 and Supertrend are not aligned')
    if detail.get('adx_ok') is False:
        parts.append('ADX is below 25 or not rising (adx %s, previous %s)' % (
            detail.get('adx'), detail.get('prev_adx')))
    return '; '.join(parts) if parts else 'setup not eligible'


def broker_decision(check):
    """The sizing / funds / cap answer already produced for one leg, or not-evaluated."""
    if not check:
        return {'evaluated': False, 'reason': 'not evaluated'}
    return {'evaluated': True, 'ok': bool(check.get('ok')), 'qty': check.get('qty'),
            'reasons': list(check.get('reasons') or [])}


def build_record(started, finished, status, outcome, dry_run, symbols, evidence, error=None,
                 window=None, max_entries=None, loop_stopped=None):
    """Assemble one scan record. `evidence` rows are the scan's own decisions, not a replay."""
    rows = [redact(dict(row)) for row in (evidence or [])]
    errors = [row for row in rows if row.get('status') == 'error']
    summary = {
        'symbols_requested': len(symbols or []),
        'recorded': len(rows),
        'skipped': sum(1 for row in rows if row.get('status') == 'skipped'),
        'rejected': sum(1 for row in rows if row.get('status') == 'rejected'),
        'eligible': sum(1 for row in rows if row.get('status') == 'eligible'),
        'errors': len(errors),
        'broker_checks': sum(1 for row in rows if (row.get('brokers') or {}).get('upstox', {}).get('evaluated')
                             or (row.get('brokers') or {}).get('kite', {}).get('evaluated')),
        'orders_placed': sum(1 for row in rows if row.get('orders_placed')),
    }
    record = {
        'schema': SCHEMA,
        'scan': 'stockopt_tech_entry',
        'tag': 'stockopt_tech',
        'started_at': _iso(started),
        'finished_at': _iso(finished),
        'status': status,
        'outcome': outcome,
        'dry_run': bool(dry_run),
        'symbols_requested': list(symbols or []),
        'window': window or {},
        'max_entries': max_entries,
        'loop_stopped': loop_stopped,
        'summary': summary,
        'candidates': rows,
        'errors': [{'underlying': row.get('underlying'), 'error': row.get('reason')} for row in errors],
        'note': 'Written by the scan that just finished. Earlier runs are not reconstructed.',
    }
    if error:
        record['error'] = redact(str(error))
    record['completion'] = completion_text(record)
    return record


def completion_text(record):
    summary = record.get('summary') or {}
    if record.get('status') == 'aborted':
        return 'aborted: %s' % (record.get('error') or 'scan did not finish')
    if record.get('outcome') == 'early_exit':
        reason = (record.get('window') or {}).get('reason') or 'entry window closed'
        return 'completed: early exit, no names evaluated (%s)' % reason
    orders = summary.get('orders_placed') or 0
    head = 'completed: %s order(s) recorded' % orders if orders else 'completed: no orders'
    return '%s; recorded %s, rejected %s, eligible %s, errors %s, skipped %s' % (
        head, summary.get('recorded'), summary.get('rejected'), summary.get('eligible'),
        summary.get('errors'), summary.get('skipped'))


def write_audit(record, directory=None, now=None):
    """Write the per-run file and append the daily line. Returns the run-file path.

    A failure here must be handled by the caller. The scan does not treat a log
    failure as a trading decision.
    """
    directory = directory or DEFAULT_DIR
    moment = now if isinstance(now, dt.datetime) else dt.datetime.now(IST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    run_dir = os.path.join(directory, 'runs')
    os.makedirs(run_dir, exist_ok=True)
    path = _unique(run_dir, moment)
    payload = json.dumps(record, indent=2, sort_keys=True, default=str) + '\n'
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    day = moment.astimezone(IST).strftime('%Y%m%d')
    daily = os.path.join(directory, 'decisions-%s.jsonl' % day)
    line = json.dumps({'file': os.path.basename(path), 'completion': record.get('completion'),
                       'status': record.get('status'), 'outcome': record.get('outcome'),
                       'started_at': record.get('started_at'), 'summary': record.get('summary')},
                      default=str)
    with open(daily, 'a') as handle:
        handle.write(line + '\n')
        handle.flush()
    rotate(directory, moment.astimezone(IST).date(), retain_days=RETAIN_DAYS)
    return path


def rotate(directory, today, retain_days=RETAIN_DAYS):
    """Delete this log's dated files strictly older than `retain_days` calendar days."""
    if not directory or not os.path.isdir(directory):
        return []
    cutoff = today - dt.timedelta(days=int(retain_days))
    removed = []
    for folder, _, names in os.walk(directory):
        for name in names:
            day = _file_day(name)
            if day is None or day >= cutoff:
                continue
            path = os.path.join(folder, name)
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    return removed


def _file_day(name):
    match = re.match(r'(?:decisions-)?(\d{8})', name)
    if not match:
        return None
    text = match.group(1)
    try:
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _unique(run_dir, moment):
    stamp = moment.astimezone(IST).strftime('%Y%m%dT%H%M%S%z')
    base = '%s-%s.json' % (stamp, os.getpid())
    path = os.path.join(run_dir, base)
    seq = 2
    while os.path.exists(path):
        path = os.path.join(run_dir, '%s-%s-%s.json' % (stamp, os.getpid(), seq))
        seq += 1
    return path


def _iso(moment):
    if isinstance(moment, str):
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment.astimezone(IST).isoformat(timespec='seconds')
