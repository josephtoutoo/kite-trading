"""Offline test for the 18 Nov 2026 Upstox entry block (no broker calls, no orders)."""
import datetime, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upstox_entry_block as ueb
import api

IST = ueb.IST
tmp = tempfile.mkdtemp(); real = ueb.STATE
ueb.STATE = os.path.join(tmp, 'b.json')
fails = []
def ok(c, m):
    print(('PASS ' if c else 'FAIL ') + m)
    if not c: fails.append(m)

P = 'https://api-hft.upstox.com/v2/order/place'
buy = {'transaction_type': 'BUY', 'tag': 'auto_entry', 'quantity': 75}
sell = {'transaction_type': 'SELL', 'tag': 'trailsl', 'order_type': 'SL'}
ok(not ueb.status()['active'], 'no file -> no block')
json.dump({'until': '2026-11-25T23:59:59+05:30', 'reason': 't'}, open(ueb.STATE, 'w'))
t = lambda s: datetime.datetime.fromisoformat(s)
ok(ueb.status(t('2026-11-18T15:30:00+05:30'))['active'], '18 Nov active')
ok(ueb.status(t('2026-11-25T23:59:00+05:30'))['active'], '25 Nov 23:59 active')
ok(not ueb.status(t('2026-11-26T00:00:01+05:30'))['active'], '26 Nov expired')
ok(ueb.api_refusal(P, buy) is not None, 'BUY place refused')
ok(ueb.api_refusal(P, json.dumps(buy)) is not None, 'BUY place (str body) refused')
ok(ueb.api_refusal('https://api.upstox.com/v3/order/gtt/place', dict(buy, tag='x')) is not None, 'GTT BUY refused')
ok(ueb.api_refusal('https://api-hft.upstox.com/v2/order/multi/place', [sell, buy]) is not None, 'multi with BUY refused')
ok(ueb.api_refusal(P, sell) is None, 'SELL stop allowed')
for tg in ueb.EXIT_BUY_TAGS:
    ok(ueb.api_refusal(P, dict(buy, tag=tg)) is None, f'exit BUY tag {tg} allowed')
ok(ueb.api_refusal('https://api-hft.upstox.com/v2/order/cancel?order_id=1', buy, 'DELETE') is None, 'cancel allowed')
ok(ueb.api_refusal('https://api-hft.upstox.com/v2/order/modify', buy, 'PUT') is None, 'modify not inspected')
os.environ['UPSTOX_ENTRY_BLOCK_OFF'] = '1'; ok(ueb.api_refusal(P, buy) is None, 'env override off'); del os.environ['UPSTOX_ENTRY_BLOCK_OFF']
open(ueb.STATE, 'w').write('{bad'); ok(ueb.status()['active'], 'corrupt file fails closed')
# api.request raises before any network I/O
json.dump({'until': '2099-01-01T00:00:00+05:30'}, open(ueb.STATE, 'w'))
def boom(*a, **k): raise AssertionError('network attempted')
api._attempt = boom
try:
    api.request(P, body=buy, method='POST'); ok(False, 'api.request BUY should raise')
except api.ApiError as e:
    ok(e.code == 403 and 'R15' in e.body, 'api.request BUY raises R15 before network')
r = api.call(P, body=buy, method='POST'); ok(r.get('status') == 'error' and 'R15' in r.get('body', ''), 'api.call returns error dict')
try:
    api.request(P, body=sell, method='POST'); ok(False, 'sell should reach network')
except AssertionError:
    ok(True, 'SELL passes guard to network layer')
ueb.STATE = real
print('FAILURES:', len(fails)); sys.exit(1 if fails else 0)
