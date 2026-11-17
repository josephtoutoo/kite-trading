#!/usr/bin/env python3
"""t691u: paired day exit, net-premium stop, GTT companion, flatten, recovery.

No broker, no network, does not import auto_entry.py or risk_manager.py.
"""
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import stockopt_spread as S

LONG = {"sym": "RELIANCE26DEC1260CE", "qty": 50, "avg": 100.0, "ltp": 90.0,
        "strike": 1260, "side": "CE", "underlying": "RELIANCE",
        "expiry": dt.date(2026, 12, 29), "broker": "upstox", "product": "D", "key": "L"}
SHORT = {"sym": "RELIANCE26DEC1300CE", "qty": -50, "avg": 40.0, "ltp": 35.0,
         "strike": 1300, "side": "CE", "underlying": "RELIANCE",
         "expiry": dt.date(2026, 12, 29), "broker": "upstox", "product": "D", "key": "S"}


class NetPremiumTests(unittest.TestCase):
    def test_net_debit_and_20pct_stop(self):
        self.assertEqual(S.net_premium(100, 40), 60)
        self.assertEqual(S.net_stop_level(60), 48)  # 80% of 60
        # fire when net now 45 <= 48
        d = S.spread_day_decision({**LONG, "ltp": 80}, {**SHORT, "ltp": 35})  # net 45
        self.assertTrue(d["fire"])
        self.assertEqual(d["method"], "net_premium")
        self.assertEqual(d["action"], "SPREAD_NET_STOP")
        self.assertTrue(d["short_first"])
        # not fired when net is 55
        d2 = S.spread_day_decision({**LONG, "ltp": 95}, {**SHORT, "ltp": 40})  # net 55
        self.assertFalse(d2["fire"])

    def test_ladder_breakeven_on_net(self):
        # entry debit 60, high 60*1.15=69 -> floor = entry 60; net now 59 fires ladder
        d = S.spread_day_decision({**LONG, "ltp": 100}, {**SHORT, "ltp": 31},  # net 69
                                  state={})
        self.assertFalse(d["fire"])
        self.assertEqual(d["state"]["net_high"], 69)
        d2 = S.spread_day_decision({**LONG, "ltp": 95}, {**SHORT, "ltp": 36},  # net 59
                                   state=d["state"])
        self.assertTrue(d2["fire"])
        self.assertEqual(d2["action"], "SPREAD_LADDER_STOP")

    def test_falls_back_to_long_leg_when_short_mark_missing(self):
        d = S.spread_day_decision({**LONG, "avg": 100, "ltp": 79}, {"sym": "S", "qty": -50})
        self.assertTrue(d["fire"])
        self.assertEqual(d["method"], "long_leg")
        self.assertEqual(d["action"], "SPREAD_LONG_STOP")

    def test_extra_long_exit_closes_both(self):
        d = S.spread_day_decision(LONG, SHORT, extra_fire="EMA50_EXIT", extra_why="ema50")
        self.assertTrue(d["fire"])
        self.assertEqual(d["action"], "EMA50_EXIT")
        self.assertTrue(d["short_first"])


class PairExitTests(unittest.TestCase):
    def test_buys_short_then_sells_long(self):
        log = []
        def place(o):
            log.append(o["transaction_type"])
            return "ID-" + o["transaction_type"]
        r = S.execute_pair_exit(place, lambda *a: {"filled": True, "qty": a[1]["quantity"]},
                                {"sym": "S", "quantity": 50}, {"sym": "L", "quantity": 50})
        self.assertTrue(r["ok"])
        self.assertEqual(log, ["BUY", "SELL"])
        self.assertTrue(r["cover_filled"] and r["long_sold"])

    def test_cover_reject_does_not_sell_long(self):
        log = []
        def place(o):
            log.append(o["transaction_type"])
            return None if o["transaction_type"] == "BUY" else "L1"
        alerts = []
        r = S.execute_pair_exit(place, lambda *a: {"filled": True},
                                {"sym": "S", "quantity": 50}, {"sym": "L", "quantity": 50},
                                retries=1, alert=lambda m, d: alerts.append(m))
        self.assertFalse(r["ok"])
        self.assertEqual(log, ["BUY", "BUY"])  # initial + 1 retry
        self.assertNotIn("SELL", log)
        self.assertTrue(r["alert"])
        self.assertEqual(len(alerts), 1)

    def test_partial_cover_retries_remainder_and_holds_long_if_still_short(self):
        log = []
        def place(o):
            log.append((o["transaction_type"], o["quantity"]))
            return "C" if o["transaction_type"] == "BUY" else "L"
        fills = [ {"filled": True, "qty": 20}, {"filled": True, "qty": 10} ]
        def wait(oid, o):
            return fills.pop(0) if fills else {"filled": False, "qty": 0}
        r = S.execute_pair_exit(place, wait, {"sym": "S", "quantity": 50}, {"sym": "L", "quantity": 50},
                                retries=1)
        self.assertFalse(r["ok"])
        self.assertEqual([x[0] for x in log], ["BUY", "BUY"])
        self.assertEqual(log[1][1], 30)  # remainder after 20 fill
        self.assertNotIn("SELL", [x[0] for x in log])
        self.assertIn("partial", r["reason"])

    def test_live_qty_reread(self):
        seen = []
        def live(o):
            seen.append(o["transaction_type"])
            return 25 if o["transaction_type"] == "BUY" else 25
        placed = []
        def place(o):
            placed.append(o["quantity"])
            return "X"
        r = S.execute_pair_exit(place, lambda *a: {"filled": True, "qty": a[1]["quantity"]},
                                {"sym": "S", "quantity": 50}, {"sym": "L", "quantity": 50},
                                live_qty=live)
        self.assertTrue(r["ok"])
        self.assertEqual(placed, [25, 25])


class RecoverSellTests(unittest.TestCase):
    def test_sell_rejected_is_retried_once_then_alerts(self):
        places = []
        def place(o):
            places.append(o["transaction_type"])
            if o["transaction_type"] == "BUY":
                return "B1"
            return None
        alerts = []
        r = S.execute_debit_spread(place, lambda *a: {"filled": True},
                                   {"transaction_type": "BUY"}, {"transaction_type": "SELL"},
                                   alert=lambda m, d: alerts.append(m), sell_retries=1)
        self.assertTrue(r["buy_filled"])
        self.assertFalse(r["sell_placed"])
        self.assertEqual(places, ["BUY", "SELL", "SELL"])
        self.assertEqual(r["sell_attempts"], 2)
        self.assertTrue(r["alert"])
        self.assertEqual(len(alerts), 1)
        self.assertIn("normal stop", r["reason"])

    def test_sell_succeeds_on_retry(self):
        n = {"sells": 0}
        def place(o):
            if o["transaction_type"] == "BUY":
                return "B"
            n["sells"] += 1
            return "S" if n["sells"] >= 2 else None
        r = S.execute_debit_spread(place, lambda *a: {"filled": True},
                                   {"transaction_type": "BUY"}, {"transaction_type": "SELL"},
                                   sell_retries=1)
        self.assertTrue(r["ok"])
        self.assertEqual(n["sells"], 2)


class GttAndReconcileTests(unittest.TestCase):
    def test_gtt_plan_sell_below_on_long_buy_below_on_short(self):
        plan = S.gtt_spread_plan(LONG, SHORT, tick=0.05)
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["never_sell_short"])
        self.assertEqual(plan["long"]["transaction_type"], "SELL")
        self.assertEqual(plan["long"]["trigger_type"], "BELOW")
        self.assertEqual(plan["short"]["transaction_type"], "BUY")
        self.assertEqual(plan["short"]["trigger_type"], "BELOW")
        # long stop = 100 - 0.20*60 = 88, below ltp 90
        self.assertAlmostEqual(plan["long"]["trigger"], 88.0)
        # short trigger scales 40 * (88/100) = 35.2, snapped, and must be < short ltp 35?
        # short_px uses ltp 35 first: 35 * 0.88 = 30.8
        self.assertLess(plan["short"]["trigger"], SHORT["ltp"])
        self.assertGreater(plan["short"]["trigger"], 0)

    def test_gtt_fire_of_long_implies_short_buyback_and_reconcile(self):
        plan = S.gtt_spread_plan(LONG, SHORT)
        # Simulate the long GTT firing: long qty goes to 0, short remains
        books = [{**LONG, "qty": 0}, {**SHORT, "qty": -50}]
        pairs = S.infer_pairs([{**LONG, "qty": 50}, {**SHORT, "qty": -50}])
        # after fire, inference may miss the pair (no long). Ledger must still catch it.
        ledger = {"positions": [{"broker": "upstox", "long": LONG["sym"], "short": SHORT["sym"],
                                 "qty": 50, "underlying": "RELIANCE", "side": "CE"}]}
        rec = S.reconcile_naked_shorts(books, ledger=ledger, pairs=[])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["action"], "COVER_NAKED_SHORT")
        self.assertEqual(rec[0]["order"]["transaction_type"], "BUY")
        self.assertEqual(rec[0]["qty"], 50)
        # a still-hedged book must not cover
        rec2 = S.reconcile_naked_shorts([{**LONG, "qty": 50}, {**SHORT, "qty": -50}], ledger=ledger)
        self.assertEqual(rec2, [])

    def test_never_emits_sell_gtt_on_the_short(self):
        plan = S.gtt_spread_plan(LONG, SHORT)
        self.assertNotEqual(plan["short"]["transaction_type"], "SELL")
        why = S.gtt_skip_reason({"qty": -50, "sym": SHORT["sym"]})
        self.assertIn("increase the short", why)


class FlattenTests(unittest.TestCase):
    def test_friday_flatten_covers_short_first(self):
        positions = [{**LONG, "qty": 50}, {**SHORT, "qty": -50}]
        legs = S.flatten_leg_order(positions, {"RELIANCE"})
        self.assertGreaterEqual(len(legs), 2)
        self.assertEqual(legs[0]["transaction_type"], "BUY")
        self.assertEqual(legs[0]["role"], "cover_short")
        self.assertEqual(legs[1]["transaction_type"], "SELL")
        self.assertEqual(legs[1]["role"], "close_long")
        # sequence
        self.assertEqual([x["sequence"] for x in legs], [1, 2])

    def test_plain_long_still_sells(self):
        plain = {"sym": "PAGEIND26NOV25000PE", "qty": 15, "underlying": "PAGEIND",
                 "strike": 25000, "side": "PE", "expiry": dt.date(2026, 11, 23), "broker": "kite"}
        legs = S.flatten_leg_order([plain], {"PAGEIND"})
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["transaction_type"], "SELL")



class EntrySendAndFlattenTests(unittest.TestCase):
    def test_send_debit_spreads_buy_then_sell_each_broker(self):
        import stockopt_st_ema_entry as entry
        log = []
        def place(broker, order):
            log.append((broker, order["transaction_type"], order["quantity"]))
            return "%s-%s" % (broker, order["transaction_type"])
        def wait(broker, oid, order):
            return {"filled": True, "qty": order["quantity"]}
        buy = {"transaction_type": "BUY", "quantity": 50, "price": 10}
        sell = {"transaction_type": "SELL", "quantity": 50, "price": 4}
        r = entry._send_debit_spreads(place, wait, buy, sell, buy, sell)
        self.assertEqual([x[1] for x in log], ["BUY", "SELL", "BUY", "SELL"])
        self.assertTrue(all(row["ok"] and row["buy_filled"] and row["sell_placed"] for row in r))

    def test_send_debit_spreads_does_not_sell_without_buy_fill(self):
        import stockopt_st_ema_entry as entry
        log = []
        def place(broker, order):
            log.append((broker, order["transaction_type"]))
            return "%s-%s" % (broker, order["transaction_type"])
        def wait(broker, oid, order):
            return {"filled": False}
        buy = {"transaction_type": "BUY", "quantity": 50, "price": 10}
        sell = {"transaction_type": "SELL", "quantity": 50, "price": 4}
        r = entry._send_debit_spreads(place, wait, buy, sell, buy, sell)
        self.assertEqual([x[1] for x in log], ["BUY", "BUY"])
        self.assertFalse(any(row["sell_placed"] for row in r))

    def test_flatten_orders_covers_short_before_long(self):
        import stockopt_st_ema as st
        now = dt.datetime(2026, 11, 20, 15, 12, tzinfo=S.IST)
        ledger = {"names": {"RELIANCE": {"source": st.SOURCE, "flatten_by": "2026-11-20"}}}
        positions = {
            "upstox": [
                {"trading_symbol": "RELIANCE26DEC1260CE", "quantity": 50, "last_price": 42, "product": "D"},
                {"trading_symbol": "RELIANCE26DEC1300CE", "quantity": -50, "last_price": 18, "product": "D"},
            ],
            "kite": [],
        }
        # Friday flatten is due at/after 15:10 IST on flatten_by
        out = st.flatten_orders(now, "2026-11-20", ledger, positions)
        self.assertTrue(out.get("due"))
        sides = [o.get("transaction_type") or o.get("side") for o in out.get("orders") or []]
        syms = [o.get("trading_symbol") or o.get("sym") for o in out.get("orders") or []]
        self.assertGreaterEqual(len(sides), 2)
        self.assertEqual(sides[0], "BUY")
        self.assertIn("1300", syms[0])
        self.assertEqual(sides[1], "SELL")
        self.assertIn("1260", syms[1])


if __name__ == "__main__":
    unittest.main()
