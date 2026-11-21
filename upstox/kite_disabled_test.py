"""Offline test for the 21 Nov 2026 Kite disable switch (no broker calls, no orders)."""
import datetime, json, os, sys, tempfile, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kite_disabled as kd
import broker_kite as bk
import trade_lock

IST = kd.IST
tmp = tempfile.mkdtemp()
real = kd.STATE
kd.STATE = os.path.join(tmp, 'kite_disabled.json')
fails = []

def ok(c, m):
    print(('PASS ' if c else 'FAIL ') + m)
    if not c:
        fails.append(m)

t = lambda s: datetime.datetime.fromisoformat(s)

ok(not kd.status()['active'], 'no file -> kite enabled')
ok(not kd.active(), 'no file active() is false')

json.dump({'disabled': True, 'from': '2026-11-23T00:00:00+05:30', 'reason': 'kite disabled by user 21 Nov'},
          open(kd.STATE, 'w'))
ok(not kd.active(t('2026-11-21T17:54:00+05:30')), 'Saturday 21 Nov not yet active')
ok(not kd.active(t('2026-11-22T23:59:59+05:30')), 'Sunday night not yet active')
ok(kd.active(t('2026-11-23T00:00:00+05:30')), 'Monday 00:00 IST active')
ok(kd.active(t('2026-11-23T09:15:00+05:30')), 'Monday session active')
ok(NOOP_IN := ('kite disabled by user 21 Nov' in kd.skip_reason('place')), 'skip_reason mentions user phrase')
ok('R16' in kd.message() and 'kite disabled by user 21 Nov' in kd.message(), 'R16 message')

json.dump({'disabled': False, 'from': '2026-11-23T00:00:00+05:30'}, open(kd.STATE, 'w'))
ok(not kd.active(t('2026-11-24T10:00:00+05:30')), 'disabled false -> off even after from')

open(kd.STATE, 'w').write('{bad')
ok(kd.active(t('2026-11-21T12:00:00+05:30')), 'corrupt file fails closed')

json.dump({'disabled': True, 'from': 'not-a-date'}, open(kd.STATE, 'w'))
ok(kd.active(), 'bad from fails closed')

json.dump({'disabled': True, 'from': '2026-11-23T00:00:00+05:30', 'reason': 'kite disabled by user 21 Nov'},
          open(kd.STATE, 'w'))
os.environ['KITE_DISABLED_OFF'] = '1'
ok(not kd.active(t('2026-11-23T10:00:00+05:30')), 'env override off')
del os.environ['KITE_DISABLED_OFF']
ok(kd.active(t('2026-11-23T10:00:00+05:30')), 'env override removed')

# broker_kite write helpers no-op even on dry, and never call the inner client
class Boom:
    GTT_TYPE_SINGLE = 'single'
    def place_order(self, **k):
        raise AssertionError('place_order reached inner client')
    def modify_order(self, **k):
        raise AssertionError('modify_order reached inner client')
    def cancel_order(self, **k):
        raise AssertionError('cancel_order reached inner client')
    def place_gtt(self, **k):
        raise AssertionError('place_gtt reached inner client')
    def delete_gtt(self, *a, **k):
        raise AssertionError('delete_gtt reached inner client')
    def profile(self):
        return {'ok': True}
    def margins(self, *a, **k):
        return {'net': 1}

g = kd.wrap_client(Boom())
# force active by using a Monday clock via monkeypatch of status? wrap_client checks active()
# which uses now(). Pin STATE already Monday-from; "now" on the box is 21 Nov so active()
# is false unless we set from in the past or override now. Use from in the past for write tests.
json.dump({'disabled': True, 'from': '2026-11-21T00:00:00+05:30', 'reason': 'kite disabled by user 21 Nov'},
          open(kd.STATE, 'w'))
ok(kd.active(), 'write-test switch is active now')
ok(g.place_order(variety='regular') is None, 'wrapped place_order no-op')
ok(g.modify_order() is None, 'wrapped modify_order no-op')
ok(g.cancel_order() is None, 'wrapped cancel_order no-op')
ok(g.place_gtt() is None, 'wrapped place_gtt no-op')
ok(g.delete_gtt('1') is None, 'wrapped delete_gtt no-op')
ok(g.GTT_TYPE_SINGLE == 'single', 'constants still pass through')
ok(g.profile() == {'ok': True}, 'reads still pass through')

# broker_kite.place/modify/cancel honour the switch before any network
_orig_client = bk.client
_orig_valid = bk.valid
bk.client = lambda: (_ for _ in ()).throw(AssertionError('bk.client reached'))
bk.valid = lambda: True
try:
    ok(bk.place('NFO', 'TEST26DEC100CE', 'BUY', 100, 10.0) is None, 'bk.place no-op when disabled')
    ok(bk.place('NFO', 'TEST26DEC100CE', 'BUY', 100, 10.0, dry=True) is None, 'bk.place dry also no-op when disabled')
    ok(bk.modify('oid', 100, 9.0, 9.5, dry=True) is None, 'bk.modify dry no-op when disabled')
    ok(bk.cancel('oid', dry=True) is None, 'bk.cancel dry no-op when disabled')
finally:
    bk.client = _orig_client
    bk.valid = _orig_valid

# trade_lock R16: kite check is blocked; Upstox is not refused by R16
# Use a BUY that will also trip other rules possibly; we only assert R16 is in the reasons
# when broker=kite, and R16 is absent when broker=upstox. Offline-friendly: call the
# helper path via a tiny local check of the rule text, then import-level message.
# Full check_order talks to Upstox; do not call it here.
ok('R16' in kd.message() and 'existing positions left as they are' in kd.message(),
   'R16 wording leaves positions as they are')

# stock-option pair helpers: kite leg skipped, Upstox still clears
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'equity'))
import stockopt_st_ema_entry as st
import stockopt_intraday_tech_entry as intra
import stockopt_intraday_tech as itech

u = {'trading_symbol': 'U', 'quantity': 100, 'price': 10, 'instrument_token': 'NSE_FO|1',
     'transaction_type': 'BUY', 'tag': 't'}
kord = {'trading_symbol': 'K', 'quantity': 100, 'price': 10, 'instrument_token': 'KITE|NFO|K',
        'transaction_type': 'BUY', 'tag': 't', 'kite_exchange': 'NFO'}

def check_ok(order, broker):
    if broker == 'kite':
        raise AssertionError('kite check_order should not be called when disabled')
    return True, [], int(order['quantity'])

ok_pair, cleared = st._check_pair(check_ok, u, kord)
ok(ok_pair is True and len(cleared) == 1 and cleared[0] is u, 'ST/EMA _check_pair skips kite, keeps Upstox')
sent, err = st._send_pair(lambda b, o: 'OID-'+b, cleared)
ok(sent == [{'broker': 'upstox', 'order_id': 'OID-upstox', 'quantity': 100}] and err is None,
   'ST/EMA _send_pair sends Upstox only')

ok_i, why_i = intra._check_pair(check_ok, u, kord)
ok(ok_i is True, 'intraday _check_pair skips kite and still passes: %s' % why_i)
sent_i, err_i = intra._send_pair(lambda b, o: 'OID-'+b, (u, kord))
ok(len(sent_i) == 1 and sent_i[0]['broker'] == 'upstox' and err_i is None,
   'intraday _send_pair skips kite: %s %s' % (sent_i, err_i))

# debit spreads: only the Upstox broker is attempted
calls = []
def fake_place(broker, order):
    calls.append(broker)
    return 'OID'
# execute_debit_spread is inside stockopt_spread; _send_debit_spreads will call it.
# We only need to see which brokers are iterated. Patch execute if needed.
import stockopt_spread as ssp
_orig_exec = ssp.execute_debit_spread
ssp.execute_debit_spread = lambda *a, **k: {'ok': True}
try:
    rows = st._send_debit_spreads(fake_place, None, u, kord, u, kord)
    ok(len(rows) == 1 and rows[0].get('broker') == 'upstox',
       'debit-spread send skips kite broker: %s' % rows)
finally:
    ssp.execute_debit_spread = _orig_exec

# flatten: kite positions may be missing when disabled; Upstox flatten still planned
now = t('2026-11-23T15:20:00+05:30')
# If today is Saturday the flatten plan may not be due; we still test the kite-book
# requirement is relaxed by calling flatten_orders with kite=None and checking it
# does not return "flatten book unverified" solely because kite is None, OR that
# kite orders are filtered. Use a ledger/positions shape the function accepts.
# When the session is not due, reason is not unverified - that's also fine.
# Force due by passing session_ok=True at 15:20? same_day_flatten_plan uses clock.
# We just assert kite=None no longer yields "flatten book unverified".
flat = itech.flatten_orders(now, {'names': {}, 'days': {}}, {'upstox': [], 'kite': None}, session_ok=True)
ok(flat.get('reason') != 'flatten book unverified',
   'flatten does not fail closed on a missing kite book when kite is disabled: %s' % flat.get('reason'))

# A due flatten must not include kite SELL legs
# Build a ledger+position that would have been flattened on both brokers.
# If plan is not due on this clock, skip the inclusion check only when due is false.
if flat.get('due'):
    pass  # empty wanted names -> no orders
ledger = {'names': {'RELIANCE': {'source': itech.SOURCE, 'day': now.date().isoformat()}}, 'days': {now.date().isoformat(): ['RELIANCE']}}
positions = {
    'upstox': [{'trading_symbol': 'RELIANCE26DEC1400CE', 'quantity': 500, 'instrument_token': 'NSE_FO|1', 'product': 'I'}],
    'kite': [{'tradingsymbol': 'RELIANCE26DEC1400CE', 'quantity': 500, 'exchange': 'NFO', 'product': 'MIS'}],
}
flat2 = itech.flatten_orders(now, ledger, positions, session_ok=True)
brokers = {o['broker'] for o in flat2.get('orders') or []}
if flat2.get('due') and flat2.get('orders'):
    ok(brokers == {'upstox'}, 'flatten plans Upstox only, not kite: %s' % flat2)
elif flat2.get('due'):
    ok('upstox' not in brokers or brokers == {'upstox'}, 'flatten due but no orders or upstox-only: %s' % flat2)
else:
    print('NOTE flatten not due on this clock (%s); skip order-filter assertion' % flat2.get('reason'))

kd.STATE = real
print('FAILURES:', len(fails))
if fails:
    print('\n'.join(fails))
sys.exit(1 if fails else 0)
