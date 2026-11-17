#!/usr/bin/env python3
"""Synthetic tests for t687u stock-option 1h signal and next-month debit spreads.

No broker, no network, does not import auto_entry.py or risk_manager.py.
Run: python3 equity/stockopt_spread_test.py
"""
import ast
import datetime as dt
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import stockopt_spread as S

TODAY = dt.date(2026, 11, 16)       # Monday, ISO week 47
EXPIRY_MON = dt.date(2026, 11, 23)  # Monday, ISO week 48 — Nov monthly
NEXT_MON = dt.date(2026, 12, 29)
JAN = dt.date(2027, 1, 25)


def row(expiry, strike, typ="CE", weekly=False, symbol=None, lot=50):
    return {
        "expiry_date": expiry, "strike_price": strike, "instrument_type": typ,
        "weekly": weekly, "lot_size": lot,
        "trading_symbol": symbol or ("RELIANCE%s%d%s" % (expiry.strftime("%y%b").upper(), int(strike), typ)),
        "instrument_key": "NSE_FO|%s-%s-%s" % (expiry, strike, typ),
        "underlying_symbol": "RELIANCE",
    }


def chain(side="CE", strikes=None, expiries=None):
    strikes = strikes or [1200, 1220, 1240, 1260, 1280, 1300, 1320]
    expiries = expiries or [EXPIRY_MON, NEXT_MON, JAN]
    return [row(e, k, side) for e in expiries for k in strikes]


class TimeframeTests(unittest.TestCase):
    def test_constants_are_1h(self):
        self.assertEqual(S.SIGNAL_UNIT, "hours")
        self.assertEqual(S.SIGNAL_INTERVAL, 1)
        self.assertEqual(S.SIGNAL_DAYS, 60)
        self.assertEqual(S.SIGNAL_TIMEFRAME, "1h")
        self.assertEqual(S.signal_analyze_args(), {"unit": "hours", "interval": 1, "days": 60})

    def test_entry_scripts_call_1h_analyze(self):
        files = [
            os.path.join(HERE, "stockopt_st_ema_entry.py"),
            os.path.join(HERE, "stockopt_tech_entry.py"),
        ]
        for path in files:
            src = open(path).read()
            ast.parse(src)
            # live analyze must use the named 1h constants (or a literal hours, 1)
            self.assertTrue("SIGNAL_UNIT" in src or "hours" in src, path)
            self.assertTrue(
                "ind.analyze(" in src and (
                    "SIGNAL_UNIT" in src or "'hours', 1" in src or '"hours", 1' in src),
                "1h analyze missing in %s" % path)
            self.assertNotIn('ind.analyze(chain[0][\'underlying_key\'], \'minutes\'', src)
            self.assertNotIn('ind.analyze(key, "minutes"', src)


class ExpiryWeekTests(unittest.TestCase):
    def test_this_week_16_20_nov_is_not_nov_monthly_expiry_week(self):
        self.assertEqual(S.current_monthly([EXPIRY_MON, NEXT_MON, JAN], TODAY), EXPIRY_MON)
        self.assertFalse(S.in_monthly_expiry_week(TODAY, [EXPIRY_MON, NEXT_MON, JAN]))
        for d in (dt.date(2026, 11, 16), dt.date(2026, 11, 20), dt.date(2026, 11, 22)):
            self.assertFalse(S.in_expiry_week(EXPIRY_MON, d), d)

    def test_week_starting_23_nov_is_expiry_week(self):
        self.assertTrue(S.in_expiry_week(EXPIRY_MON, dt.date(2026, 11, 23)))
        self.assertTrue(S.in_monthly_expiry_week(dt.date(2026, 11, 23), [EXPIRY_MON, NEXT_MON, JAN]))
        # once the Nov contract has expired (24 Nov), current monthly rolls to December
        self.assertEqual(S.current_monthly([EXPIRY_MON, NEXT_MON, JAN], dt.date(2026, 11, 24)), NEXT_MON)
        self.assertFalse(S.in_monthly_expiry_week(dt.date(2026, 11, 24), [EXPIRY_MON, NEXT_MON, JAN]))

    def test_monthly_skips_weeklies_when_futures_given(self):
        rows = chain() + [row(dt.date(2026, 11, 17), 1260, weekly=True)]
        m = S.monthly_expiries(rows, fut_dates=[EXPIRY_MON, NEXT_MON])
        self.assertEqual(m, [EXPIRY_MON, NEXT_MON])
        self.assertEqual(S.next_monthly(m, TODAY, EXPIRY_MON), NEXT_MON)


class ChainAndHedgeTests(unittest.TestCase):
    def test_atm_nearest_strike(self):
        c = chain()
        atm = S.pick_atm(c, "CE", 1265, NEXT_MON)
        self.assertEqual(float(atm["strike_price"]), 1260)
        atm2 = S.pick_atm(c, "CE", 1270, NEXT_MON)
        self.assertEqual(float(atm2["strike_price"]), 1260)  # tie -> lower strike first via abs then strike

    def test_two_strikes_further_otm_ce_and_pe(self):
        c = chain("CE")
        hedge, why = S.pick_otm_hedge(c, "CE", 1260, NEXT_MON, steps=2)
        self.assertIsNone(why)
        self.assertEqual(float(hedge["strike_price"]), 1300)  # 1280 then 1300
        pe = chain("PE")
        hedge_pe, why = S.pick_otm_hedge(pe, "PE", 1260, NEXT_MON, steps=2)
        self.assertIsNone(why)
        self.assertEqual(float(hedge_pe["strike_price"]), 1220)  # 1240 then 1220

    def test_hedge_refuses_when_chain_too_short(self):
        c = [row(NEXT_MON, 1260), row(NEXT_MON, 1280)]
        hedge, why = S.pick_otm_hedge(c, "CE", 1260, NEXT_MON, steps=2)
        self.assertIsNone(hedge)
        self.assertIn("have 1", why)

    def test_modal_strike_step(self):
        st = S.strike_steps([100, 110, 120, 130, 150])
        self.assertEqual(st["modal"], 10.0)
        self.assertEqual(st["steps"], [10.0, 20.0])


class PlanEntryTests(unittest.TestCase):
    def test_plain_mode_this_week(self):
        p = S.plan_entry(chain(), "CE", 1265, TODAY, fut_dates=[EXPIRY_MON, NEXT_MON, JAN])
        self.assertTrue(p["ok"])
        self.assertEqual(p["mode"], "plain")
        self.assertTrue(p["send_allowed"])
        self.assertEqual(p["current_monthly"], "2026-11-23")
        self.assertFalse(p["expiry_week"])
        self.assertEqual(float(p["long"]["strike_price"]), 1260)
        self.assertIsNone(p["short"])
        self.assertEqual(p["strike_step"], 20.0)

    def test_spread_mode_in_expiry_week(self):
        p = S.plan_entry(chain(), "CE", 1265, EXPIRY_MON, fut_dates=[EXPIRY_MON, NEXT_MON, JAN])
        self.assertTrue(p["ok"])
        self.assertEqual(p["mode"], "spread")
        self.assertTrue(p["expiry_week"])
        self.assertEqual(p["next_monthly"], "2026-12-29")
        self.assertEqual(p["long_strike"], 1260)
        self.assertEqual(p["short_strike"], 1300)
        self.assertEqual(p["hedge_steps"], 2)
        self.assertEqual(p["hedge_distance"], 40.0)
        self.assertEqual(p["strike_step"], 20.0)
        self.assertTrue(p["send_allowed"])
        self.assertTrue(p.get("send_block") in (None, ""))

    def test_refuse_plain_current_month_in_expiry_week_without_next(self):
        only = [row(EXPIRY_MON, k) for k in (1200, 1220, 1240, 1260, 1280, 1300)]
        p = S.plan_entry(only, "CE", 1265, EXPIRY_MON, fut_dates=[EXPIRY_MON])
        self.assertEqual(p["mode"], "refuse")
        self.assertFalse(p["ok"])
        self.assertIn("next monthly is not listed", p["reason"])


class LegOrderTests(unittest.TestCase):
    def test_buy_then_sell_after_fill(self):
        log = []
        def place(order):
            log.append(("place", order["transaction_type"], order["strike"]))
            return "OID-" + order["transaction_type"]
        def wait(oid, order):
            log.append(("fill", oid))
            return {"filled": True, "qty": 50}
        buy = {"transaction_type": "BUY", "strike": 1260}
        sell = {"transaction_type": "SELL", "strike": 1300}
        r = S.execute_debit_spread(place, wait, buy, sell)
        self.assertTrue(r["ok"])
        self.assertTrue(r["buy_filled"] and r["sell_placed"] and r["sell_filled"])
        self.assertEqual([x[0] for x in log], ["place", "fill", "place", "fill"])
        self.assertEqual(log[0][1], "BUY")
        self.assertEqual(log[2][1], "SELL")

    def test_no_sell_if_buy_rejected(self):
        placed = []
        r = S.execute_debit_spread(lambda o: placed.append(o) or None, lambda *a: {"filled": True},
                                   {"transaction_type": "BUY"}, {"transaction_type": "SELL"})
        self.assertFalse(r["ok"])
        self.assertFalse(r["sell_placed"])
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["transaction_type"], "BUY")
        self.assertIn("SELL was not sent", r["reason"])

    def test_no_sell_if_buy_does_not_fill_and_buy_is_cancelled(self):
        cancelled = []
        r = S.execute_debit_spread(lambda o: "B1", lambda *a: {"filled": False},
                                   {"transaction_type": "BUY"}, {"transaction_type": "SELL"},
                                   cancel=lambda oid, o: cancelled.append(oid))
        self.assertFalse(r["sell_placed"])
        self.assertEqual(cancelled, ["B1"])
        self.assertIn("did not fill", r["reason"])

    def test_sell_failure_after_buy_fill_alerts_and_does_not_claim_ok(self):
        alerts = []
        r = S.execute_debit_spread(lambda o: "B1" if o["transaction_type"] == "BUY" else None,
                                   lambda *a: {"filled": True},
                                   {"transaction_type": "BUY"}, {"transaction_type": "SELL"},
                                   alert=lambda msg, det: alerts.append(msg))
        self.assertTrue(r["buy_filled"])
        self.assertFalse(r["sell_placed"])
        self.assertFalse(r["ok"])
        self.assertTrue(r["alert"])
        self.assertEqual(len(alerts), 1)
        self.assertIn("unhedged", r["reason"])

    def test_never_places_sell_first_even_if_place_raises_on_buy(self):
        placed = []
        def place(order):
            placed.append(order["transaction_type"])
            raise RuntimeError("broker down")
        r = S.execute_debit_spread(place, lambda *a: {"filled": True},
                                   {"transaction_type": "BUY"}, {"transaction_type": "SELL"})
        self.assertEqual(placed, ["BUY"])
        self.assertFalse(r["sell_placed"])

    def test_exit_buys_short_back_first(self):
        plan = S.exit_plan({"sym": "LONG", "qty": 50}, {"sym": "SHORT", "qty": -50})
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["short_first"])
        self.assertEqual(plan["legs"][0]["transaction_type"], "BUY")
        self.assertEqual(plan["legs"][0]["role"], "cover_short")
        self.assertEqual(plan["legs"][1]["transaction_type"], "SELL")
        self.assertEqual(plan["legs"][1]["role"], "close_long")


class GttAndSquareoffTests(unittest.TestCase):
    def test_gtt_skips_short_and_spread_long(self):
        self.assertIn("increase the short", S.gtt_skip_reason({"qty": -50, "sym": "R1300CE"}))
        ledger = {"positions": [{"broker": "upstox", "long": "RELIANCE26DEC1260CE",
                                 "short": "RELIANCE26DEC1300CE", "side": "CE"}]}
        self.assertIn("leave the short naked", S.gtt_skip_reason({"qty": 50, "sym": "RELIANCE26DEC1260CE"}, ledger))
        self.assertIsNone(S.gtt_skip_reason({"qty": 50, "sym": "RELIANCE26NOV1260CE"}, ledger))

    def test_gtt_infers_pair_from_books(self):
        pairs = S.infer_pairs([
            {"broker": "upstox", "underlying": "RELIANCE", "expiry": NEXT_MON, "side": "CE",
             "qty": 50, "strike": 1260, "sym": "L1260"},
            {"broker": "upstox", "underlying": "RELIANCE", "expiry": NEXT_MON, "side": "CE",
             "qty": -50, "strike": 1300, "sym": "S1300"},
        ])
        self.assertEqual(len(pairs), 1)
        why = S.gtt_skip_reason({"qty": 50, "sym": "L1260"}, pairs=pairs)
        self.assertIn("inferred debit-spread long", why)

    def test_next_month_spread_not_squared_in_nov_expiry_week(self):
        self.assertTrue(S.leave_next_month_spread(NEXT_MON, EXPIRY_MON))
        self.assertFalse(S.leave_next_month_spread(EXPIRY_MON, EXPIRY_MON))
        self.assertFalse(S.in_expiry_week(NEXT_MON, EXPIRY_MON))

    def test_ledger_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            st = S.record_spread({}, broker="upstox", underlying="RELIANCE", expiry=NEXT_MON,
                                 side="CE", long_sym="L", short_sym="S", qty=50, ts="t", strike_step=20)
            S.save_ledger(st, path)
            loaded = S.load_ledger(path)
            self.assertTrue(S.is_recorded_spread_leg("L", loaded, "long"))
            self.assertTrue(S.is_recorded_spread_leg("S", loaded, "short"))


class ParseAndPairTests(unittest.TestCase):
    def test_compact_and_spaced_symbols(self):
        c = S.parse_option_symbol("RELIANCE26DEC1260CE")
        self.assertEqual(c["underlying"], "RELIANCE")
        self.assertEqual(c["strike"], 1260)
        self.assertEqual(c["side"], "CE")
        self.assertEqual(c["expiry"], dt.date(2026, 12, 1))
        self.assertFalse(c["expiry_precise"])
        s = S.parse_option_symbol("RELIANCE 1260 CE 29 DEC 26")
        self.assertEqual(s["expiry"], dt.date(2026, 12, 29))
        self.assertTrue(s["expiry_precise"])
        self.assertEqual(S.expiry_key(c["expiry"]), S.expiry_key(s["expiry"]))

    def test_legs_pair_compact_december_symbols(self):
        legs = S.legs_from_positions([
            {"trading_symbol": "RELIANCE26DEC1260CE", "quantity": 50},
            {"trading_symbol": "RELIANCE26DEC1300CE", "quantity": -50},
        ], "upstox")
        pairs = S.infer_pairs(legs)
        self.assertEqual(len(pairs), 1)
        self.assertIn("naked", S.gtt_skip_reason({"qty": 50, "sym": "RELIANCE26DEC1260CE"}, pairs=pairs))


if __name__ == "__main__":
    unittest.main()
