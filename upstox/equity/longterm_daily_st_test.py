#!/usr/bin/env python3
"""Pure-function tests for longterm_daily_st.py. No broker calls, no orders."""
import datetime as dt
import os
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import longterm_daily_st as m

IST = ZoneInfo("Asia/Calcutta")


class SmaTests(unittest.TestCase):
    def test_window(self):
        s = m.sma([1, 2, 3, 4, 5], 3)
        self.assertEqual(s[:2], [None, None])
        self.assertAlmostEqual(s[2], 2.0)
        self.assertAlmostEqual(s[3], 3.0)
        self.assertAlmostEqual(s[4], 4.0)

    def test_short_series(self):
        self.assertEqual(m.sma([1, 2], 3), [None, None])


class ClassifyTests(unittest.TestCase):
    def test_requires_close_above_st(self):
        # ST between MAs but close at/below ST is not a long
        self.assertIsNone(m.classify_st(close=99, st=100, sma50=120, sma150=80))
        self.assertIsNone(m.classify_st(close=100, st=100, sma50=120, sma150=80))

    def test_above_both_when_st_above_higher_ma(self):
        # SMA50 > SMA150, ST above both
        self.assertEqual(m.classify_st(close=130, st=121, sma50=120, sma150=80), "above_both")
        # SMA150 > SMA50, ST above both
        self.assertEqual(m.classify_st(close=200, st=151, sma50=80, sma150=150), "above_both")

    def test_between_inclusive_of_edges(self):
        self.assertEqual(m.classify_st(close=110, st=100, sma50=120, sma150=80), "between")
        self.assertEqual(m.classify_st(close=121, st=120, sma50=120, sma150=80), "between")
        self.assertEqual(m.classify_st(close=90, st=80, sma50=120, sma150=80), "between")

    def test_st_below_both_is_out(self):
        self.assertIsNone(m.classify_st(close=100, st=70, sma50=120, sma150=80))

    def test_missing_inputs(self):
        self.assertIsNone(m.classify_st(close=100, st=90, sma50=None, sma150=80))


class RankTests(unittest.TestCase):
    def test_above_both_ranks_ahead_of_between(self):
        a = {"symbol": "BBB", "condition": "between", "adx": 40, "extension_pct": 1}
        b = {"symbol": "AAA", "condition": "above_both", "adx": 18, "extension_pct": 8}
        rows = sorted([a, b], key=m.rank_key)
        self.assertEqual([r["symbol"] for r in rows], ["AAA", "BBB"])

    def test_higher_adx_then_less_extended(self):
        rows = [
            {"symbol": "ZZZ", "condition": "above_both", "adx": 20, "extension_pct": 2},
            {"symbol": "MMM", "condition": "above_both", "adx": 30, "extension_pct": 5},
            {"symbol": "AAA", "condition": "above_both", "adx": 30, "extension_pct": 1},
        ]
        got = [r["symbol"] for r in sorted(rows, key=m.rank_key)]
        self.assertEqual(got, ["AAA", "MMM", "ZZZ"])


class ResultsWindowTests(unittest.TestCase):
    def test_parses_common_formats(self):
        self.assertEqual(m.parse_results_date("2026-11-20"), dt.date(2026, 11, 20))
        self.assertEqual(m.parse_results_date("20-Nov-2026"), dt.date(2026, 11, 20))
        self.assertIsNone(m.parse_results_date(""))
        self.assertIsNone(m.parse_results_date("n/a"))

    def test_window_inclusive(self):
        today = dt.date(2026, 11, 16)
        rows = [
            ("OLD", "2026-11-15"),
            ("TODAY", "2026-11-16"),
            ("WEEK", "23-Nov-2026"),
            ("LATER", "2026-11-24"),
        ]
        got = m.results_exclusions(rows, today, window_days=7)
        self.assertEqual(set(got), {"TODAY", "WEEK"})


class SizeTests(unittest.TestCase):
    def test_qty_fits_capital(self):
        # 24% margin, price 100 -> margin/share 24; 50000/24 = 2083 shares
        qty, margin, notional, lev, note = m.size_qty(50_000, 100, 0.24)
        self.assertEqual(qty, 2083)
        self.assertLessEqual(margin, 50_000)
        self.assertAlmostEqual(lev, 1 / 0.24)
        self.assertEqual(note, "ok")
        self.assertAlmostEqual(notional, qty * 100)

    def test_one_share_too_expensive(self):
        qty, margin, notional, lev, note = m.size_qty(50_000, 80_000, 0.80)
        self.assertEqual(qty, 0)
        self.assertIn("exceeds", note)
        self.assertGreater(margin, 50_000)

    def test_t693u_is_per_stock_not_total(self):
        # 3 names at 50k is 1.5 lakh, not 16.6k each
        self.assertEqual(m.CAPITAL_PER_STOCK, 50_000)
        self.assertEqual(m.MAX_TRADES, 3)
        self.assertEqual(m.CAPITAL_PER_STOCK * m.MAX_TRADES, 150_000)


class SessionCutTests(unittest.TestCase):
    def test_before_close_uses_previous_day(self):
        now = dt.datetime(2026, 11, 16, 10, 0, tzinfo=IST)
        cut = m.session_cut(now)
        self.assertEqual(cut.date(), dt.date(2026, 11, 15))

    def test_after_close_includes_today(self):
        now = dt.datetime(2026, 11, 16, 15, 30, tzinfo=IST)
        cut = m.session_cut(now)
        self.assertEqual(cut.date(), dt.date(2026, 11, 16))
        later = dt.datetime(2026, 11, 16, 23, 0, tzinfo=IST)
        self.assertEqual(m.session_cut(later).date(), dt.date(2026, 11, 16))


class ExecuteGuardTests(unittest.TestCase):
    def test_execute_is_hard_off(self):
        self.assertFalse(m.EXECUTE_ENABLED)


class AdvTests(unittest.TestCase):
    def test_average_value(self):
        # 20 bars of close=10, volume=1_000_000 -> ADV = 1 crore, below the 2-crore floor
        cs = [["ts", 0, 0, 0, 10, 1_000_000, 0] for _ in range(20)]
        adv = m.avg_traded_value(cs, 20)
        self.assertAlmostEqual(adv, 10_000_000)
        self.assertLess(adv, m.ADV_MIN)



class ExecuteFlagTests(unittest.TestCase):
    def test_env_flag(self):
        self.assertFalse(m.execute_flag_on({}))
        self.assertFalse(m.execute_flag_on({"LONGTERM_DST_EXECUTE": "0"}))
        self.assertTrue(m.execute_flag_on({"LONGTERM_DST_EXECUTE": "1"}))
        self.assertTrue(m.execute_flag_on({"LONGTERM_DST_EXECUTE": "yes"}))


class EntryWindowTests(unittest.TestCase):
    def test_before_first_day(self):
        now = dt.datetime(2026, 11, 16, 23, 30, tzinfo=IST)
        ok, why = m.entry_window(now, trading_day=True)
        self.assertFalse(ok)
        self.assertIn("17 Nov", why)

    def test_opens_at_0920_on_17_nov(self):
        early = dt.datetime(2026, 11, 17, 9, 19, tzinfo=IST)
        ok, _ = m.entry_window(early, trading_day=True)
        self.assertFalse(ok)
        open_at = dt.datetime(2026, 11, 17, 9, 20, tzinfo=IST)
        ok, why = m.entry_window(open_at, trading_day=True)
        self.assertTrue(ok, why)

    def test_closes_at_1500(self):
        last = dt.datetime(2026, 11, 17, 14, 59, tzinfo=IST)
        self.assertTrue(m.entry_window(last, trading_day=True)[0])
        done = dt.datetime(2026, 11, 17, 15, 0, tzinfo=IST)
        ok, why = m.entry_window(done, trading_day=True)
        self.assertFalse(ok)
        self.assertIn("15:00", why)

    def test_skips_weekend(self):
        sat = dt.datetime(2026, 11, 21, 10, 0, tzinfo=IST)
        ok, why = m.entry_window(sat, trading_day=False)
        self.assertFalse(ok)
        self.assertIn("trading day", why)


class GapSkipTests(unittest.TestCase):
    def test_allows_flat_or_down(self):
        skip, _ = m.gap_skip(433.7, 433.7)
        self.assertFalse(skip)
        skip, _ = m.gap_skip(420.0, 433.7)
        self.assertFalse(skip)

    def test_allows_just_under_3pct(self):
        # 433.7 * 1.03 = 446.711
        skip, _ = m.gap_skip(446.71, 433.7)
        self.assertFalse(skip)

    def test_blocks_over_3pct_up(self):
        skip, why = m.gap_skip(447.0, 433.7)
        self.assertTrue(skip)
        self.assertIn("gapped", why)

    def test_unreadable(self):
        skip, why = m.gap_skip(None, 433.7)
        self.assertTrue(skip)


class SetupHoldsTests(unittest.TestCase):
    def test_igil_like_row_holds(self):
        row = {"close": 433.7, "st": 385.70, "sma50": 350.0, "sma150": 300.0}
        self.assertTrue(m.setup_holds(row))
        self.assertEqual(m.classify_st(433.7, 385.70, 350.0, 300.0), "above_both")

    def test_breaks_when_close_drops_through_st(self):
        row = {"close": 380.0, "st": 385.70, "sma50": 350.0, "sma150": 300.0}
        self.assertFalse(m.setup_holds(row))


class SleeveCapTests(unittest.TestCase):
    def test_already_held(self):
        self.assertTrue(m.already_held({"IGIL", "SRF"}, "igil"))
        self.assertFalse(m.already_held({"SRF"}, "IGIL"))

    def test_margin_cap(self):
        blocks = m.longterm_dst_blocks([], "IGIL", 50001)
        self.assertTrue(any("50,000" in b or "50000" in b for b in blocks), blocks)
        self.assertFalse(m.longterm_dst_blocks([], "IGIL", 50000))

    def test_max_three_names(self):
        held = ["IGIL", "PVRINOX", "LTTS"]
        blocks = m.longterm_dst_blocks(held, "KOTAKBANK", 40000)
        self.assertTrue(any("max 3" in b for b in blocks), blocks)
        # adding a 3rd when 2 are held is fine
        self.assertFalse(m.longterm_dst_blocks(["IGIL", "PVRINOX"], "LTTS", 40000))

    def test_double_buy_block(self):
        blocks = m.longterm_dst_blocks(["IGIL"], "IGIL", 40000, already_held_name=True)
        self.assertTrue(any("already held" in b for b in blocks), blocks)


class TrailPlanTests(unittest.TestCase):
    def test_initial_stop_is_daily_st(self):
        plan = m.longterm_dst_trail_plan(433.7, 385.70)
        self.assertTrue(plan["place_stop"])
        self.assertEqual(plan["action"], "PLACE")
        self.assertAlmostEqual(plan["trigger"], 385.70)

    def test_never_loosens(self):
        plan = m.longterm_dst_trail_plan(450.0, 385.70, existing_trigger=400.0)
        self.assertFalse(plan["place_stop"])
        self.assertEqual(plan["action"], "KEEP")
        self.assertAlmostEqual(plan["trigger"], 400.0)

    def test_ratchets_up_to_higher_st(self):
        plan = m.longterm_dst_trail_plan(450.0, 410.0, existing_trigger=385.70)
        self.assertTrue(plan["place_stop"])
        self.assertEqual(plan["action"], "RATCHET")
        self.assertAlmostEqual(plan["trigger"], 410.0)

    def test_floor_is_respected(self):
        plan = m.longterm_dst_trail_plan(450.0, 370.0, existing_trigger=None, floor_stop=385.70)
        self.assertFalse(plan["place_stop"])
        self.assertAlmostEqual(plan["trigger"], 385.70)

    def test_never_places_at_or_above_ltp(self):
        plan = m.longterm_dst_trail_plan(385.70, 385.70)
        self.assertFalse(plan["place_stop"])


class LimitPriceTests(unittest.TestCase):
    def test_buffer_above_ltp(self):
        px = m.limit_buy_price(433.7)
        self.assertGreater(px, 433.7)
        # snapped to 0.05
        self.assertAlmostEqual(px, round(px / 0.05) * 0.05, places=2)

    def test_ltts_tick_is_10p(self):
        px = m.limit_buy_price(4235.0)
        self.assertGreater(px, 4235.0)
        self.assertAlmostEqual(px, round(px / 0.10) * 0.10, places=2)


class SquareoffExcludeTests(unittest.TestCase):
    def test_tag_and_product(self):
        self.assertTrue(m.is_longterm_dst_squareoff_excluded(tag="longterm_dst", product="MTF"))
        self.assertFalse(m.is_longterm_dst_squareoff_excluded(tag="stockopt_intraday", product="I"))

if __name__ == "__main__":
    unittest.main()
