#!/usr/bin/env python3
"""t689u: drop a watchlist name when its completed 1h bar breaks the watched trend.

No broker, no orders, does not import auto_entry.py or risk_manager.py.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import stockopt_st_ema as st

IST = st.IST
NOW = dt.datetime(2026, 11, 16, 22, 50, tzinfo=IST)


def bar(close, ema, direction, st_v=None, ts="2026-11-16T15:15:00+05:30"):
    return {"close": close, "ema20": ema, "dir": direction,
            "st": st_v if st_v is not None else (ema - 5 if direction < 0 else ema + 5),
            "last_ts": ts}


class DirectionAndBreakTests(unittest.TestCase):
    def test_normalize_direction(self):
        self.assertEqual(st.normalize_direction(1), "long")
        self.assertEqual(st.normalize_direction("CE"), "long")
        self.assertEqual(st.normalize_direction("downtrend"), "short")
        self.assertEqual(st.normalize_direction(-1), "short")
        self.assertIsNone(st.normalize_direction(0))
        self.assertIsNone(st.normalize_direction(None))

    def test_long_breaks_only_when_close_below_ema_and_st_bearish(self):
        broken, why = st.trend_broken(bar(99, 100, -1), "long")
        self.assertTrue(broken)
        self.assertIn("below EMA20", why)
        self.assertIn("bearish", why)
        self.assertFalse(st.trend_broken(bar(99, 100, 1), "long")[0])   # ST still bullish
        self.assertFalse(st.trend_broken(bar(101, 100, -1), "long")[0])  # close still above EMA
        self.assertFalse(st.trend_broken(bar(100, 100, -1), "long")[0])  # equal close is not a break

    def test_short_breaks_only_when_close_above_ema_and_st_bullish(self):
        broken, why = st.trend_broken(bar(101, 100, 1), "short")
        self.assertTrue(broken)
        self.assertIn("above EMA20", why)
        self.assertFalse(st.trend_broken(bar(101, 100, -1), "short")[0])
        self.assertFalse(st.trend_broken(bar(99, 100, 1), "short")[0])
        self.assertFalse(st.trend_broken(bar(100, 100, 1), "short")[0])

    def test_unverified_is_not_a_break(self):
        self.assertFalse(st.trend_broken(None, "long")[0])
        self.assertFalse(st.trend_broken(bar(99, 100, -1), None)[0])
        self.assertEqual(st.trend_broken({"close": 99}, "long")[1], "completed 1h bar unverified")


class PruneTests(unittest.TestCase):
    def test_removes_broken_keeps_intact_and_unverified(self):
        scope = {
            "st_ema_category_watchlist": ["ANURAS", "PAGEIND", "GABRIEL"],
            "stock_option_underlyings": ["ANURAS", "PAGEIND", "GABRIEL", "HDFCLIFE"],
            "st_ema_watched_direction": {"ANURAS": "long", "PAGEIND": "short", "GABRIEL": "long"},
            "week_trading_days": ["2026-11-16", "2026-11-20"],
        }
        bars = {
            "ANURAS": bar(99, 100, -1),          # long broken
            "PAGEIND": bar(31000, 33500, -1),    # short still intact (close below, ST bearish)
            "GABRIEL": None,                     # unverified, kept
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scope.json")
            log = os.path.join(tmp, "removals.jsonl")
            out = st.prune_broken_watchlist(scope, bars, NOW, persist=False,
                                            scope_path=path, log_path=log)
            self.assertEqual([r["symbol"] for r in out["removed"]], ["ANURAS"])
            self.assertTrue(out["removed"][0]["open_positions_untouched"])
            self.assertEqual(scope["st_ema_category_watchlist"], ["PAGEIND", "GABRIEL"])
            self.assertEqual(scope["stock_option_underlyings"], ["PAGEIND", "GABRIEL", "HDFCLIFE"])
            self.assertFalse(out["persisted"])
            self.assertFalse(os.path.exists(path))
            self.assertFalse(os.path.exists(log))

    def test_persist_writes_scope_and_log(self):
        scope = {
            "st_ema_category_watchlist": ["ANURAS"],
            "stock_option_underlyings": ["ANURAS", "HDFCLIFE"],
            "st_ema_watched_direction": {"ANURAS": "long"},
        }
        bars = {"ANURAS": bar(99, 100, -1)}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scope.json")
            log = os.path.join(tmp, "removals.jsonl")
            json.dump({"st_ema_category_watchlist": ["ANURAS"]}, open(path, "w"))
            out = st.prune_broken_watchlist(scope, bars, NOW, persist=True,
                                            scope_path=path, log_path=log)
            self.assertTrue(out["persisted"])
            saved = json.load(open(path))
            self.assertEqual(saved["st_ema_category_watchlist"], [])
            self.assertEqual(saved["stock_option_underlyings"], ["HDFCLIFE"])
            lines = open(log).read().strip().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["symbol"], "ANURAS")
            self.assertIn("below EMA20", rec["reason"])
            self.assertTrue(rec["open_positions_untouched"])

    def test_loads_direction_from_scan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = os.path.join(tmp, "st_ema_scan_1114.json")
            json.dump([{"sym": "PAGEIND", "dir": -1, "close": 1, "ema20": 2, "st": 1.5}], open(scan, "w"))
            scope = {"st_ema_category_watchlist": ["PAGEIND"]}
            directions = st.load_watched_directions(scope, reports_dir=tmp)
            self.assertEqual(directions["PAGEIND"], "short")
            # scope map wins over the scan
            scope["st_ema_watched_direction"] = {"PAGEIND": "long"}
            directions = st.load_watched_directions(scope, reports_dir=tmp)
            self.assertEqual(directions["PAGEIND"], "long")


class EntryHookTests(unittest.TestCase):
    def test_run_prunes_before_evaluation_and_dry_run_does_not_write(self):
        import stockopt_st_ema_entry as entry
        scope = {
            "st_ema_category_watchlist": ["ANURAS", "PAGEIND"],
            "week_trading_days": ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"],
            "st_ema_watched_direction": {"ANURAS": "long", "PAGEIND": "short"},
        }
        # ANURAS long is broken; PAGEIND short is intact but we give no chain so it skips later
        market = {
            "chains": {"ANURAS": [{"underlying_key": "k", "expiry_date": dt.date(2026, 11, 23)}],
                       "PAGEIND": []},
            "analyze": lambda key: bar(99, 100, -1) if key in ("k", "ANURAS") else bar(31000, 33500, -1),
            "positions": {"upstox": [], "kite": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scope.json")
            json.dump(scope, open(path, "w"))
            result = entry.run(dict(scope), NOW, market, execute=False, dry=True, scope_path=path)
            removed = [r["symbol"] for r in result["watchlist_prune"]["removed"]]
            self.assertEqual(removed, ["ANURAS"])
            self.assertIn("PAGEIND", result["watchlist"])
            self.assertNotIn("ANURAS", result["watchlist"])
            self.assertFalse(result["watchlist_prune"]["persisted"])
            # original file untouched
            saved = json.load(open(path))
            self.assertEqual(saved["st_ema_category_watchlist"], ["ANURAS", "PAGEIND"])
            # no orders
            self.assertEqual(result["orders_placed"], 0)


if __name__ == "__main__":
    unittest.main()
