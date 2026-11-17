#!/usr/bin/env python3
"""16 Nov 2026: stop floor resets on flatten and re-entry, same way as the peak."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import position_life as pl

ok = 0
bad = 0

def check(name, cond, detail=None):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("FAIL", name, "" if detail is None else detail)


def fills(rows):
    """rows: (ts, side, qty, oid, status=complete)."""
    out = []
    for r in rows:
        ts, side, qty, oid = r[:4]
        st = r[4] if len(r) > 4 else "complete"
        out.append({"order_timestamp": ts, "transaction_type": side, "filled_quantity": qty,
                    "quantity": qty, "order_id": oid, "status": st, "tradingsymbol": "CRUDEOIL26NOV8300CE",
                    "trading_symbol": "CRUDEOIL26NOV8300CE"})
    return out


# The live 16 Nov case: 3 lots MIS @ 125.40, sold flat, then 2 lots NRML @ 120.
orders = fills([
    ("2026-11-16 15:22:10", "BUY", 3, "oid-open"),
    ("2026-11-16 15:35:34", "SELL", 3, "oid-flat"),
    ("2026-11-16 15:42:56", "BUY", 2, "oid-reentry"),
])
gen, flats = pl.current_open_gen(orders, sym="CRUDEOIL26NOV8300CE", overnight=0)
check("reentry gen is the second buy", gen == "2026-11-16 15:42:56|oid-reentry", gen)
check("one flatten before current life", flats == 1, flats)

s = {"high": 125.7, "entry": 125.4, "target": 150.48, "stop": 100.3, "stop_pct": 0.2,
     "open_gen": "2026-11-16 15:22:10|oid-open", "live_qty": 3, "broker": "kite",
     "sym": "CRUDEOIL26NOV8300CE"}
seeds = {"CRUDEOIL26NOV8300CE": 100.3, "OTHER": 50}
reset, why, prev = pl.bind_position_peak(
    s, 120.0, 111.7, 2, overnight=0, orders=orders, sym="CRUDEOIL26NOV8300CE",
    step=0.20, initial_stop_pct=0.20, seeds=seeds,
)
check("detects new opening fill", reset is True and "opening fill" in (why or ""), why)
check("peak reset to new entry (not 125.7)", s["high"] == 120.0, s.get("high"))
check("entry is the new fill", s["entry"] == 120.0, s.get("entry"))
check("target is +20% of new entry", s["target"] == 144.0, s.get("target"))
check("stale stop popped", "stop" not in s, s)
check("new life is 20%", s.get("stop_pct") == 0.20, s.get("stop_pct"))
check("seed floor cleared for this symbol only",
      "CRUDEOIL26NOV8300CE" not in seeds and seeds.get("OTHER") == 50, seeds)
fresh = max(120.0 * (1 - 0.20), seeds.get("CRUDEOIL26NOV8300CE", 0), s.get("stop", 0), 0)
check("computed floor is 96", abs(fresh - 96.0) < 1e-9, fresh)
check("modify loosens on reset",
      pl.sl_modify_trigger(96.0, 100.3, qty_changed=True, reset=True) == 96.0)

# No reset: an add (same opening, live_qty stays >0) must keep the peak and not loosen.
s2 = {"high": 130.0, "entry": 120.0, "target": 144.0, "stop": 96.0, "stop_pct": 0.2,
      "open_gen": gen, "live_qty": 2}
seeds2 = {}
add_orders = orders + fills([("2026-11-16 16:00:00", "BUY", 1, "oid-add")])
r2, w2, _ = pl.bind_position_peak(s2, 120.0, 130.0, 3, overnight=0, orders=add_orders,
                                  sym="CRUDEOIL26NOV8300CE", initial_stop_pct=0.20, seeds=seeds2)
check("add is not a reset", r2 is False, w2)
check("add keeps prior peak", s2["high"] == 130.0, s2["high"])
check("add keeps stop floor", s2.get("stop") == 96.0, s2.get("stop"))
check("no loosen without reset", pl.sl_modify_trigger(90.0, 96.0, reset=False) is None)
check("ratchet up still works", pl.sl_modify_trigger(110.0, 96.0, reset=False) == 110.0)

# Carry / overnight position: no day tape, same gen, do not reset.
s3 = {"high": 345.0, "entry": 345.0, "target": 414.0, "stop": 241.5, "stop_pct": 0.3,
      "open_gen": "overnight:2", "live_qty": 2}
r3, w3, _ = pl.bind_position_peak(s3, 345.0, 323.1, 2, overnight=2, orders=[],
                                  sym="CRUDEOIL26DEC8700CE", initial_stop_pct=0.20, seeds={})
check("overnight carry is not a reset", r3 is False, w3)
check("legacy 30% stop kept", s3.get("stop") == 241.5 and s3.get("stop_pct") == 0.3, s3)

# First-seen new position (no prior state): not a 'reset', but no stale stop either.
s4 = {"high": 80}
r4, w4, _ = pl.bind_position_peak(s4, 80.0, 80.0, 1, overnight=0, orders=fills([
    ("2026-11-16 16:10:00", "BUY", 1, "oid-new")
]), sym="NEWCE", initial_stop_pct=0.20, seeds={})
check("brand new has no reset (nothing to inherit)", r4 is False, w4)
check("brand new has no stale stop", "stop" not in s4)

# Opening-fill helper: re-entry uses the latest opening, an add does not move it.
fts, foid, fflats = pl.current_open_fill(orders, sym="CRUDEOIL26NOV8300CE")
check("current_open_fill is the re-entry", fts == "2026-11-16 15:42:56" and foid == "oid-reentry", (fts, foid))
check("current_open_fill flats_before matches", fflats == 1, fflats)
fts2, foid2, _ = pl.current_open_fill(add_orders, sym="CRUDEOIL26NOV8300CE")
check("an add does not move the opening fill", fts2 == "2026-11-16 15:42:56" and foid2 == "oid-reentry", (fts2, foid2))
fts3, foid3, _ = pl.current_open_fill([], sym="CRUDEOIL26DEC8700CE", overnight=2)
check("overnight carry has no day opening fill", fts3 is None and foid3 is None, (fts3, foid3))
flat_orders = fills([
    ("2026-11-16 15:22:10", "BUY", 3, "oid-open"),
    ("2026-11-16 15:35:34", "SELL", 3, "oid-flat"),
])
fts4, foid4, flats4 = pl.current_open_fill(flat_orders, sym="CRUDEOIL26NOV8300CE")
check("flat book has no opening fill", fts4 is None and foid4 is None, (fts4, foid4, flats4))


# 17 Nov 2026 live case (Kite SENSEX26N1982200CE): a BUY LIMIT placed 11:09:21 filled at 13:08:06.
# Kite keeps exchange_timestamp at 11:09:21 (registration); order_timestamp and
# exchange_update_timestamp carry the 13:08:06 fill. The opening fill must be 13:08:06.
def kfill(place_ts, fill_ts, side, qty, oid):
    return {"order_timestamp": fill_ts, "exchange_timestamp": place_ts, "exchange_update_timestamp": fill_ts,
            "transaction_type": side, "filled_quantity": qty, "quantity": qty, "order_id": oid,
            "status": "complete", "tradingsymbol": "SENSEX26N1982200CE", "trading_symbol": "SENSEX26N1982200CE"}
late = [
    kfill("2026-11-17 09:31:47", "2026-11-17 09:31:48", "BUY", 40, "a"),
    kfill("2026-11-17 09:40:51", "2026-11-17 09:40:51", "SELL", 40, "b"),
    kfill("2026-11-17 11:09:21", "2026-11-17 13:08:06", "BUY", 20, "late"),
    kfill("2026-11-17 11:26:01", "2026-11-17 11:26:01", "BUY", 40, "c"),
    kfill("2026-11-17 11:35:17", "2026-11-17 11:35:17", "SELL", 40, "d"),
    kfill("2026-11-17 11:45:14", "2026-11-17 11:45:14", "BUY", 80, "e"),
    kfill("2026-11-17 12:08:14", "2026-11-17 12:08:14", "SELL", 80, "f"),
]
lts, loid, lfl = pl.current_open_fill(late, sym="SENSEX26N1982200CE")
check("late-filled limit opens at its fill time, not its placement time",
      lts == "2026-11-17 13:08:06" and loid == "late", (lts, loid, lfl))
check("late fill is a re-entry after three flats", lfl == 3, lfl)
# Upstox book rows carry only order_timestamp / exchange_timestamp (equal): unchanged behaviour.
up = [{"order_timestamp": "2026-11-17 12:35:44", "exchange_timestamp": "2026-11-17 12:35:44", "transaction_type": "BUY",
       "filled_quantity": 30, "quantity": 30, "order_id": "u1", "status": "complete", "trading_symbol": "BANKNIFTY26NOV62400CE"}]
uts, uoid, _ = pl.current_open_fill(up, sym="BANKNIFTY26NOV62400CE")
check("upstox row keeps its fill time", uts == "2026-11-17 12:35:44" and uoid == "u1", (uts, uoid))

print("position_life_test: %d passed, %d failed" % (ok, bad))
sys.exit(1 if bad else 0)
