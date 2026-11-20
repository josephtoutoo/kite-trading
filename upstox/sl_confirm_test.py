#!/usr/bin/env python3
"""20 Nov 2026 post-market: Upstox entry-SL confirmation fixes (no broker, no orders)."""
import json, os, sys, tempfile, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sl_confirm as sc
fails = 0
def ok(c, m):
    global fails
    print(("ok    " if c else "FAIL  ") + m)
    if not c: fails += 1
# 1) trade_lock live-read cache is dropped, instrument caches kept
tl = types.SimpleNamespace(_cache={"/v2/portfolio/short-term-positions": {"data": []}, "/v2/order/retrieve-all": {},
                                   "_kite": ([], []), "_kiteh": [], "_kitemtf": [], ("lotmap", "f"): {1: 2}, ("lot", "K"): 65})
sc._fresh_trade_lock(tl)
ok(not any(isinstance(k, str) for k in tl._cache), "positions/orders/Kite live reads dropped from trade_lock cache")
ok(("lotmap", "f") in tl._cache and ("lot", "K") in tl._cache, "lot-size/instrument caches kept")
sc._fresh_trade_lock(types.SimpleNamespace())  # no _cache attr: no crash
ok(True, "module without _cache is ignored")
# 2) order-book symbol resolved from the entry order / instrument key
orig_go, orig_req = sc.get_orders, sc._req
sc.get_orders = lambda b: [{"order_id": "X1", "trading_symbol": "SENSEX26NOV83200CE", "instrument_token": "BSE_FO|1106450"}]
sc._req = lambda *a, **k: {"data": []}
ok(sc.upstox_book_symbol("BSE_FO|1106450", "X1", "SENSEX 83200 CE 26 NOV 26") == "SENSEX26NOV83200CE", "entry order -> book symbol")
ok(sc.upstox_book_symbol("BSE_FO|1106450", None, "F") == "SENSEX26NOV83200CE", "instrument key -> book symbol")
sc.get_orders = lambda b: []
sc._req = lambda *a, **k: {"data": [{"instrument_token": "NSE_FO|1", "trading_symbol": "NIFTY26D0125500CE"}]}
ok(sc.upstox_book_symbol("NSE_FO|1", "Y", "F") == "NIFTY26D0125500CE", "position row -> book symbol")
ok(sc.upstox_book_symbol("NSE_FO|9", "Y", "F") == "F", "no match -> fallback unchanged")
def boom(b): raise RuntimeError("down")
sc.get_orders = boom
ok(sc.upstox_book_symbol("NSE_FO|1", "Y", "F") == "F", "read error -> fallback unchanged")
sc.get_orders, sc._req = orig_go, orig_req
# 3) target level found by the Upstox instrument key
d = tempfile.mkdtemp(); f = os.path.join(d, "rs.json")
json.dump({"BSE_FO|822590": {"sym": "SENSEX26D0383500CE", "target": 1216.02}}, open(f, "w"))
old = sc.STATE; sc.STATE = f
r = sc.state_target("upstox", None, "SENSEX 83500 CE 03 DEC 26", key="BSE_FO|822590")
ok(bool(r) and r["target"] == 1216.02, "Upstox target found by instrument key")
ok(sc.state_target("upstox", None, "SENSEX 83500 CE 03 DEC 26") is None, "without key the display name still misses (old behaviour)")
sc.STATE = old
# 4) confirm() matches positions on the book symbol (no network: stubbed)
sc.get_orders = lambda b: [{"order_id": "S1", "trading_symbol": "SENSEX26NOV83200CE", "transaction_type": "SELL", "order_type": "SL",
                            "status": "trigger pending", "quantity": 20, "trigger_price": 496.9, "price": 472.0, "status_message": "", "tag": "trailsl"}]
sc.position_qty = lambda b, s: 20 if s == "SENSEX26NOV83200CE" else 0
r = sc.confirm("upstox", "SENSEX26NOV83200CE", 20, trigger=496.9, target=None, attempts=1, wait=0)
ok(r["ok"] and r["sl"] == "S1", "book symbol: entry and live SL confirmed")
r = sc.confirm("upstox", "SENSEX 83200 CE 26 NOV 26", 20, trigger=496.9, target=None, attempts=1, wait=0)
ok(not r["ok"], "display name: confirmation misses (the 20 Nov bug this fixes)")
print(f"{fails} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
