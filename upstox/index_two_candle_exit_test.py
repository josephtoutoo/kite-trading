#!/usr/bin/env python3
"""Synthetic-candle tests for the t685u two-candle and t686u EMA50 exits.

No broker, no network, does not import auto_entry.py or risk_manager.py.
Run: python3 index_two_candle_exit_test.py
"""
import datetime as dt
import os
import sys
import tempfile

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
import index_two_candle_exit as E
import index_routes as R

IST = E.IST


def ts(h, m, day=16):
    return dt.datetime(2026, 11, day, h, m, tzinfo=IST)


def bar(t, close):
    """Completed-bar dict; t is the open time (ISO)."""
    if isinstance(t, dt.datetime):
        t = t.isoformat()
    return {"ts": t, "close": close}


def bars_3m(start, closes):
    t, out = start, []
    for c in closes:
        out.append(bar(t, c))
        t = t + dt.timedelta(minutes=3)
    return out


def fire(bars, side, entry, minutes=3, count_from=None):
    return E.consecutive_adverse(bars, side, entry, minutes, count_from=count_from)


def test_ce_two_lower_closes_fires():
    # 09:15, 09:18, 09:21, 09:24; entry 09:16 so first two post-entry are 09:18 and 09:21
    b = bars_3m(ts(9, 15), [100, 99, 98, 97])
    ok, d = fire(b, "CE", ts(9, 16), 3)
    assert ok, d
    assert d["pair"]["first"]["close"] == 99 and d["pair"]["second"]["close"] == 98


def test_pe_two_higher_closes_fires():
    b = bars_3m(ts(9, 15), [100, 101, 102, 101])
    ok, d = fire(b, "PE", ts(9, 16), 3)
    assert ok, d
    assert d["pair"]["second"]["close"] == 102


def test_one_adverse_not_enough():
    b = bars_3m(ts(9, 15), [100, 99, 100, 101])  # only one down after entry
    ok, d = fire(b, "CE", ts(9, 16), 3)
    assert not ok, d


def test_equal_close_is_not_adverse():
    b = bars_3m(ts(9, 15), [100, 99, 99, 98])
    ok, d = fire(b, "CE", ts(9, 16), 3)
    assert not ok, d  # 99 then 99 (equal) then 98: no two consecutive strict downs


def test_down_up_down_down_fires_on_last_pair():
    # post-entry: 99 (down), 100 (up), 99 (down), 98 (down) -> fires on the last pair
    b = bars_3m(ts(9, 15), [100, 99, 100, 99, 98])
    ok, d = fire(b, "CE", ts(9, 16), 3)
    assert ok, d
    assert d["pair"]["first"]["close"] == 99 and d["pair"]["second"]["close"] == 98
    assert "09:24" in d["pair"]["first"]["ts"] or d["pair"]["first"]["ts"].endswith("09:24:00+05:30")


def test_pe_two_lower_does_not_fire():
    b = bars_3m(ts(9, 15), [100, 99, 98, 97])
    ok, d = fire(b, "PE", ts(9, 16), 3)
    assert not ok, d


def test_pre_entry_candles_are_not_counted():
    # two downs complete at 09:18 and 09:21, entry is 09:22 — those bars don't count
    b = bars_3m(ts(9, 15), [100, 99, 98, 99, 100])
    ok, d = fire(b, "CE", ts(9, 22), 3)
    assert not ok, d
    # 09:21-09:24 (completes 09:24), 09:24-09:27, 09:27-09:30 are post-entry; downs then up
    assert d.get("post_entry") == 3, d


def test_first_post_entry_compares_to_pre_entry_close():
    # 09:15-09:18 close 100 (pre), 09:18-09:21 close 99, 09:21-09:24 close 98; entry 09:19
    b = bars_3m(ts(9, 15), [100, 99, 98])
    ok, d = fire(b, "CE", ts(9, 19), 3)
    assert ok, d
    assert d["pair"]["first"]["prev_close"] == 100
    assert d["pair"]["first"]["close"] == 99


def test_need_two_post_entry_bars():
    # entry after the first bar has already closed, so only the 09:18-09:21 bar counts
    b = bars_3m(ts(9, 15), [100, 99])
    ok, d = fire(b, "CE", ts(9, 19), 3)
    assert not ok, d
    assert d.get("post_entry") == 1, d


def test_raw_candle_tuples_accepted():
    raw = [
        ["2026-11-16T09:15:00+05:30", 1, 2, 0, 100],
        ["2026-11-16T09:30:00+05:30", 1, 2, 0, 99],
        ["2026-11-16T09:45:00+05:30", 1, 2, 0, 98],
    ]
    ok, d = fire(raw, "CE", ts(9, 16), 15)
    assert ok, d
    assert d["minutes"] == 15


def test_1h_bucket_completion():
    # 09:15 and 10:15 1h bars complete at 10:15 and 11:15
    b = [bar(ts(9, 15), 100), bar(ts(10, 15), 99), bar(ts(11, 15), 98)]
    ok, d = fire(b, "CE", ts(10, 16), 60)
    assert ok, d
    # first post-entry is 10:15-11:15 (completes 11:15), second 11:15-12:15 (completes 12:15)
    # pair stores the bar open ts; the first post-entry 1h bar opens 10:15 and completes 11:15
    assert "10:15" in d["pair"]["first"]["ts"], d
    assert "11:15" in d["pair"]["first"]["completed_at"], d
    # a check at 11:00 (only one completed post-entry bar) must not fire
    early = [bar(ts(9, 15), 100), bar(ts(10, 15), 99)]
    ok2, d2 = fire(early, "CE", ts(10, 16), 60)
    assert not ok2, d2


def test_count_from_floors_old_bars():
    b = bars_3m(ts(9, 15), [100, 99, 98, 97])
    ok, d = fire(b, "CE", ts(9, 16), 3, count_from=ts(9, 30))
    assert not ok, d  # last bar opens 09:24, completes 09:27, all before 09:30


def test_unknown_side_and_empty():
    assert fire([], "CE", ts(9, 16), 3)[0] is False
    assert fire(bars_3m(ts(9, 15), [100, 99, 98]), "XX", ts(9, 16), 3)[0] is False
    assert E.adverse_close("CE", 10, 10) is False
    assert E.adverse_close("PE", 10, 10) is False
    assert E.side_of("SENSEX26N1982000CE") == "CE"
    assert E.side_of("BANKNIFTY26NOV61000PE") == "PE"


def test_evaluate_uses_route_record_and_supplied_bars():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.json")
        st = R.load(path)
        opened = ts(9, 31).isoformat()
        key = R.ukey("NSE_FO|TEST")
        R.record(st, key, route_name="3m", index="SENSEX", symbol="SENSEX26N1982000CE",
                 product="I", qty=20, ts=opened, broker="upstox")
        R.save(st, path)
        # point the module at this state file
        old = R.STATE
        try:
            R.STATE = path
            ctx = E.prepare(now=ts(10, 0), state=R.load(path), master_rows=[])
            b = bars_3m(ts(9, 15), [100, 101, 102, 99, 98])  # last two after 09:31 are downs
            # entry 09:31; bars at 09:15,09:18,09:21,09:24,09:27 — all complete before 09:31. Need later bars.
            b = bars_3m(ts(9, 27), [100, 99, 98])  # 09:27 (pre, completes 09:30), 09:30 (completes 09:33), 09:33 (completes 09:36)
            fire_, why, action = E.evaluate("SENSEX26N1982000CE", product="I", broker="upstox",
                                    instrument_key="NSE_FO|TEST", state=ctx, now=ts(9, 40),
                                    bars=b, apply_effective=False, log=False)
            assert fire_, why
            assert "3m" in why
            assert action == "TWO_CANDLE_EXIT"
        finally:
            R.STATE = old


def test_evaluate_skips_non_index():
    ctx = E.prepare(now=ts(10, 0), state={}, master_rows=[])
    fire_, why, action = E.evaluate("RELIANCE26NOV1400CE", product="D", broker="upstox",
                            instrument_key="NSE_FO|X", state=ctx, now=ts(10, 0),
                            bars=bars_3m(ts(9, 15), [100, 99, 98]), apply_effective=False, log=False)
    assert fire_ is False
    assert action is None
    assert "not a NIFTY" in why


def test_inferred_bn_monthly_is_1h():
    rows = [{"instrument_type": "FUT", "underlying_symbol": "BANKNIFTY",
             "expiry": int(dt.datetime(2026, 11, 23, 0, 0, tzinfo=IST).timestamp() * 1000),
             "instrument_key": "NSE_FO|BNFUT"},
            {"instrument_type": "CE", "underlying_symbol": "BANKNIFTY",
             "expiry": int(dt.datetime(2026, 11, 23, 0, 0, tzinfo=IST).timestamp() * 1000),
             "instrument_key": "NSE_FO|BNCE"}]
    ctx = E.prepare(now=ts(10, 0), state={}, master_rows=rows)
    route, how, entry, src, key, und = E.classify(ctx, "upstox", "NSE_FO|BNCE", None,
                                                   "BANKNIFTY26NOV61300CE", "D")
    assert und == "BANKNIFTY" and route == "1h" and how == "inferred", (route, how, und)
    assert src == "session_open_fallback"


def test_intraday_inferred_is_3m():
    ctx = E.prepare(now=ts(10, 0), state={}, master_rows=[])
    route, how, _, src, _, und = E.classify(ctx, "upstox", "NSE_FO|X", None, "NIFTY26NOV25250CE", "I")
    assert route == "3m" and how == "inferred" and und == "NIFTY"


def _warmup(n=80, start=None, base=100.0, step=0.2, minutes=3):
    """Rising series so EMA50 trails the close; enough bars to arm EMA50."""
    t = start or dt.datetime(2026, 11, 12, 9, 15, tzinfo=IST)
    out, px = [], base
    for _ in range(n):
        out.append(bar(t, round(px, 4)))
        px += step
        t = t + dt.timedelta(minutes=minutes)
    return out, t, px


def test_ema50_ce_close_below_fires():
    b, t, px = _warmup()
    # last warmup close is above its EMA50; drop sharply after entry
    entry = t  # last warmup bar completes at t, so it is not post-entry
    b.append(bar(t, 50.0))  # well below EMA50
    ok, d = E.ema50_break(b, "CE", entry, 3)
    assert ok, d
    assert d["action"] == "EMA50_EXIT"
    assert d["bar"]["close"] == 50.0
    assert d["bar"]["close"] < d["bar"]["ema50"]


def test_ema50_pe_close_above_fires():
    # falling warmup so EMA50 is above the last close; then a sharp bounce
    t0 = dt.datetime(2026, 11, 12, 9, 15, tzinfo=IST)
    b, px = [], 200.0
    t = t0
    for _ in range(80):
        b.append(bar(t, round(px, 4)))
        px -= 0.2
        t += dt.timedelta(minutes=3)
    entry = t  # last warmup bar completes at t, so it is not post-entry
    b.append(bar(t, 250.0))
    ok, d = E.ema50_break(b, "PE", entry, 3)
    assert ok, d
    assert d["bar"]["close"] == 250.0
    assert d["bar"]["close"] > d["bar"]["ema50"]


def test_ema50_ce_close_above_does_not_fire():
    b, t, px = _warmup()
    entry = t  # last warmup bar completes at t, so it is not post-entry
    b.append(bar(t, px + 1))  # continues up, stays above EMA50
    ok, d = E.ema50_break(b, "CE", entry, 3)
    assert not ok, d


def test_ema50_equal_close_is_not_a_break():
    # construct a bar whose close equals the EMA50 after entry
    b, t, px = _warmup()
    emas = E.ema50_series(b)
    # one more bar needed; set its close equal to what EMA would be if close==ema
    # EMA_new = ema + a*(close-ema); close==EMA_new iff close==prev ema
    prev = emas[-1]
    entry = t  # last warmup bar completes at t, so it is not post-entry
    b.append(bar(t, prev))
    ok, d = E.ema50_break(b, "CE", entry, 3)
    assert not ok, d
    ok2, d2 = E.ema50_break(b, "PE", entry, 3)
    assert not ok2, d2
    assert E.adverse_ema50("CE", 10, 10) is False
    assert E.adverse_ema50("PE", 10, 10) is False


def test_ema50_pre_entry_break_ignored():
    b, t, px = _warmup()
    # break happens on a bar that completes before entry
    b.append(bar(t, 50.0))
    entry = t + dt.timedelta(minutes=4)  # this bar completed at t+3, before entry
    # and a post-entry bar that stays above
    b.append(bar(t + dt.timedelta(minutes=3), 50.0 + 80))  # recover, likely still check
    # use a post-entry close back near the rising path so it stays above EMA
    ok, d = E.ema50_break(b[:-1], "CE", entry, 3)  # only the pre-entry drop
    assert not ok, d
    assert d.get("post_entry", 0) == 0


def test_ema50_insufficient_bars_does_not_fire():
    b = bars_3m(ts(9, 15), [100 + i for i in range(20)] + [50])
    ok, d = E.ema50_break(b, "CE", ts(9, 16), 3)
    assert not ok, d
    assert "not armed" in d["why"]


def test_first_route_ema50_beats_later_two_candle():
    # first post-entry bar drops through EMA50; two-candle needs two bars so EMA50 wins
    b, t, px = _warmup()
    entry = t  # last warmup bar completes at t, so it is not post-entry
    b.append(bar(t, 50.0))
    b.append(bar(t + dt.timedelta(minutes=3), 49.0))
    ok, d = E.first_route_exit(b, "CE", entry, 3)
    assert ok, d
    assert d["action"] == "EMA50_EXIT"
    # two-candle would also have fired on the second bar, but later
    c2, d2 = E.consecutive_adverse(b, "CE", entry, 3)
    assert c2
    t5 = E.parse_ts(d["bar"]["completed_at"])
    t2 = E.parse_ts(d2["pair"]["second"]["completed_at"])
    assert t5 < t2


def test_first_route_two_candle_beats_later_ema50():
    # two small down closes that stay above EMA50, then a later crash through EMA50
    b, t, px = _warmup()
    entry = t  # last warmup bar completes at t, so it is not post-entry
    # last warmup close ~ px-0.2; two tiny downs still above EMA50
    c1 = (px - 0.2) - 0.05
    c2 = c1 - 0.05
    b.append(bar(t, round(c1, 4)))
    b.append(bar(t + dt.timedelta(minutes=3), round(c2, 4)))
    # confirm two-candle fires and EMA50 has not yet on these two
    c2ok, _ = E.consecutive_adverse(b, "CE", entry, 3)
    e5ok, _ = E.ema50_break(b, "CE", entry, 3)
    assert c2ok and not e5ok, (c2ok, e5ok, c1, c2)
    # later crash
    b.append(bar(t + dt.timedelta(minutes=6), 50.0))
    ok, d = E.first_route_exit(b, "CE", entry, 3)
    assert ok and d["action"] == "TWO_CANDLE_EXIT", d
    e5ok2, d5 = E.ema50_break(b, "CE", entry, 3)
    assert e5ok2
    t2 = E.parse_ts(d["pair"]["second"]["completed_at"])
    t5 = E.parse_ts(d5["bar"]["completed_at"])
    assert t2 < t5


def test_first_route_tie_on_same_candle():
    # first post-entry down (compares to pre-entry), second down also crashes EMA50
    # Build so the *second* post-entry bar is the first EMA50 break, same moment as two-candle.
    b, t, px = _warmup()
    entry = t  # last warmup bar completes at t, so it is not post-entry
    last = px - 0.2
    b.append(bar(t, last - 0.05))          # slight down, still above EMA50
    b.append(bar(t + dt.timedelta(minutes=3), 50.0))  # second down + EMA50 break
    ok, d = E.first_route_exit(b, "CE", entry, 3)
    assert ok, d
    assert d["action"] == "TWO_CANDLE_EXIT"
    assert "EMA50" in d["why"]


def test_ema50_count_from_floors_old_bars():
    b, t, px = _warmup()
    entry = t  # last warmup bar completes at t, so it is not post-entry
    b.append(bar(t, 50.0))
    ok, d = E.ema50_break(b, "CE", entry, 3, count_from=t + dt.timedelta(minutes=10))
    assert not ok, d


def test_evaluate_ema50_action():
    b, t, px = _warmup()
    entry = t  # last warmup bar completes at t, so it is not post-entry
    b.append(bar(t, 50.0))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.json")
        st = R.load(path)
        key = R.ukey("NSE_FO|TEST2")
        R.record(st, key, route_name="3m", index="NIFTY", symbol="NIFTY26NOV25250CE",
                 product="I", qty=65, ts=entry.isoformat(), broker="upstox")
        old = R.STATE
        try:
            R.STATE = path
            ctx = E.prepare(now=t + dt.timedelta(minutes=5), state=st, master_rows=[])
            fire_, why, action = E.evaluate("NIFTY26NOV25250CE", product="I", broker="upstox",
                                            instrument_key="NSE_FO|TEST2", state=ctx,
                                            now=t + dt.timedelta(minutes=5),
                                            bars=b, apply_effective=False, log=False)
            assert fire_, why
            assert action == "EMA50_EXIT"
            assert "EMA50" in why
        finally:
            R.STATE = old


def _fill_orders(sym, rows):
    """rows: (ts, side, qty, oid)."""
    out = []
    for ts, side, qty, oid in rows:
        out.append({"order_timestamp": ts, "exchange_timestamp": ts,
                    "transaction_type": side, "filled_quantity": qty, "quantity": qty,
                    "order_id": oid, "status": "complete",
                    "tradingsymbol": sym, "trading_symbol": sym})
    return out


def test_candle_closing_at_entry_is_ignored():
    """A bar that completes exactly at entry is not a post-entry candle (3m/15m/1h)."""
    # 09:33 bar completes 09:36; 09:36 bar completes 09:39. Entry at 09:36.
    b = bars_3m(ts(9, 30, day=17), [82345.0, 82300.0, 82248.75, 82200.0])
    # opens: 09:30, 09:33, 09:36, 09:39. Entry 09:36 == close of the 09:33 bar.
    ok, d = fire(b, "CE", ts(9, 36, day=17), 3)
    # post-entry: 09:36 (completes 09:39) and 09:39 (completes 09:42) — two downs after a down
    # 09:33 close 82300 is AT entry, ignored as a counted bar; it may still be the comparison close.
    # first post is 09:36 close 82248.75 < 82300, second 09:39 close 82200 < 82248.75 -> fires
    # Wait: we want to assert the 09:33 bar itself is not in the pair as a *counted* first bar
    # if there were only those two early downs. Build a tighter case: only early downs, no later.
    early = bars_3m(ts(9, 30, day=17), [82345.0, 82300.0, 82248.75])
    ok, d = fire(early, "CE", ts(9, 36, day=17), 3)
    assert not ok, d
    assert d.get("post_entry") == 1, d  # only 09:36-09:39 completed after 09:36

    # 15m: bars 09:15 and 09:30 complete at 09:30 and 09:45; entry exactly 09:30
    b15 = [bar(ts(9, 15, day=17), 100), bar(ts(9, 30, day=17), 99), bar(ts(9, 45, day=17), 98)]
    ok15, d15 = fire(b15, "CE", ts(9, 30, day=17), 15)
    # 09:15 completes at 09:30 (= entry) ignored; post: 09:30 (09:45) and 09:45 (10:00) — two downs, fires
    # To test ignore-at-entry, drop the last bar so only one post-entry remains
    ok15e, d15e = fire(b15[:-1], "CE", ts(9, 30, day=17), 15)
    assert not ok15e, d15e
    assert d15e.get("post_entry") == 1, d15e
    ok15, d15 = fire(b15, "CE", ts(9, 30, day=17), 15)
    assert ok15, d15

    # 1h: 09:15 completes 10:15; entry at 10:15 means that bar is ignored
    b1h = [bar(ts(9, 15, day=17), 100), bar(ts(10, 15, day=17), 99), bar(ts(11, 15, day=17), 98)]
    ok1h, d1h = fire(b1h[:-1], "CE", ts(10, 15, day=17), 60)
    assert not ok1h, d1h
    assert d1h.get("post_entry") == 1, d1h


def test_stale_route_opened_does_not_fire_on_morning_candles():
    """17 Nov 2026 regression: 10:50 re-entry must not use 09:33/09:36 closes.

    The 3m route record still held 09:31 from the earlier life. Two-candle and
    EMA50 must use the 10:50:57 fill, on both brokers, and ignore pre-fill bars.
    """
    entry_stale = ts(9, 31, day=17)
    fill_ts = "2026-11-17T10:50:57+05:30"
    # 09:30, 09:33, 09:36 would fire a CE two-candle if entry were 09:31
    morning = bars_3m(ts(9, 30, day=17), [82345.0, 82300.0, 82248.75])
    # sanity: stale entry alone WOULD fire
    ok_stale, d_stale = fire(morning, "CE", entry_stale, 3)
    assert ok_stale, d_stale

    orders = _fill_orders("SENSEX26N1982200CE", [
        (entry_stale.isoformat(), "BUY", 40, "oid-morning"),
        ("2026-11-17T09:40:00+05:30", "SELL", 40, "oid-flat"),
        (fill_ts, "BUY", 40, "2122560602726916096"),
    ])

    def _run(broker, key, product, exchange, symbol, persist_path=None, extra_bars=None, sync=False):
        st = R.load(persist_path) if persist_path else {}
        rec_key = R.kkey(exchange, symbol) if broker == "kite" else R.ukey(key)
        R.record(st, rec_key, route_name="3m", index="SENSEX", symbol=symbol,
                 product=product, qty=40, ts=entry_stale.isoformat(), broker=broker,
                 order_id="oid-morning")
        bars = list(morning) + list(extra_bars or [])
        ctx = E.prepare(now=ts(10, 53, day=17), state=st, master_rows=[], orders=orders,
                        state_path=persist_path)
        return E.evaluate(symbol, product=product, broker=broker,
                          instrument_key=None if broker == "kite" else key,
                          exchange=exchange if broker == "kite" else None,
                          state=ctx, now=ts(10, 53, day=17), bars=bars,
                          apply_effective=False, log=False, orders=orders,
                          sync_route=sync), st, rec_key

    for broker, key, product, exchange, symbol in (
        ("kite", None, "MIS", "BFO", "SENSEX26N1982200CE"),
        ("upstox", "BSE_FO|821516", "I", None, "SENSEX26N1982200CE"),
    ):
        (fire_, why, action), _st, _k = _run(broker, key, product, exchange, symbol)
        assert not fire_, (broker, why)
        assert action is None
        # entry source should be the fill, not the 09:31 route record
        route, how, entry, src, rkey, und = E.classify(
            E.prepare(now=ts(10, 53, day=17), state=_st, master_rows=[], orders=orders),
            broker, key, exchange, symbol, product)
        assert src == "opening_fill", (broker, src)
        assert entry.hour == 10 and entry.minute == 50, entry

    # After 10:50, two later 3m downs should still fire (both brokers)
    later = [
        bar(ts(10, 51, day=17), 82200.0),  # completes 10:54
        bar(ts(10, 54, day=17), 82150.0),  # completes 10:57
    ]
    (fire2, why2, action2), _st, _k = _run("kite", None, "MIS", "BFO",
                                           "SENSEX26N1982200CE", extra_bars=later)
    assert fire2, why2
    assert action2 == "TWO_CANDLE_EXIT"
    assert "10:51" in (why2 or "") or "10:54" in (why2 or ""), why2
    assert "09:33" not in (why2 or "") and "09:36" not in (why2 or ""), why2

    # 15m and 1h: stale opened 09:31, fill 10:50; pre-fill adverse bars must not fire
    fill_15 = "2026-11-17T10:50:57+05:30"
    b15 = [bar(ts(9, 15, day=17), 100), bar(ts(9, 30, day=17), 99), bar(ts(9, 45, day=17), 98),
           bar(ts(10, 0, day=17), 97), bar(ts(10, 15, day=17), 96)]
    b1h = [bar(ts(9, 15, day=17), 100), bar(ts(10, 15, day=17), 99)]  # 10:15 completes 11:15, after fill? 11:15 > 10:50 so one post-entry only
    orders15 = _fill_orders("NIFTY26NOV25350CE", [
        ("2026-11-17T09:31:30+05:30", "BUY", 65, "oid-m"),
        ("2026-11-17T09:40:00+05:30", "SELL", 65, "oid-f"),
        (fill_15, "BUY", 65, "oid-re"),
    ])
    for route_name, minutes, bars, expect_fire in (("15m", 15, b15, False), ("1h", 60, b1h, False)):
        st = {}
        key = R.ukey("NSE_FO|TF%s" % route_name)
        R.record(st, key, route_name=route_name, index="NIFTY", symbol="NIFTY26NOV25350CE",
                 product="D", qty=65, ts="2026-11-17T09:31:30+05:30", broker="upstox")
        ctx = E.prepare(now=ts(11, 0, day=17), state=st, master_rows=[], orders=orders15)
        fire_, why, action = E.evaluate("NIFTY26NOV25350CE", product="D", broker="upstox",
                                        instrument_key="NSE_FO|TF%s" % route_name, state=ctx,
                                        now=ts(11, 0, day=17), bars=bars, apply_effective=False,
                                        log=False, orders=orders15)
        assert fire_ is expect_fire, (route_name, why, action)

    # Two 15m downs after 10:50 do fire
    b15_post = b15 + [bar(ts(10, 45, day=17), 95), bar(ts(11, 0, day=17), 94)]
    st = {}
    key = R.ukey("NSE_FO|TF15m")
    R.record(st, key, route_name="15m", index="NIFTY", symbol="NIFTY26NOV25350CE",
             product="D", qty=65, ts="2026-11-17T09:31:30+05:30", broker="upstox")
    ctx = E.prepare(now=ts(11, 20, day=17), state=st, master_rows=[], orders=orders15)
    fire_, why, action = E.evaluate("NIFTY26NOV25350CE", product="D", broker="upstox",
                                    instrument_key="NSE_FO|TF15m", state=ctx,
                                    now=ts(11, 20, day=17), bars=b15_post, apply_effective=False,
                                    log=False, orders=orders15)
    assert fire_ and action == "TWO_CANDLE_EXIT", (why, action)
    assert "10:45" in (why or "") or "11:00" in (why or ""), why


def test_reentry_fill_resets_route_record():
    """sync_route=True rewrites a stale opened time to the current opening fill."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.json")
        stale = ts(9, 31, day=17).isoformat()
        fill_ts = "2026-11-17T10:50:57+05:30"
        symbol = "SENSEX26N1982200CE"
        key = R.kkey("BFO", symbol)
        st = {}
        R.record(st, key, route_name="3m", index="SENSEX", symbol=symbol,
                 product="MIS", qty=40, ts=stale, broker="kite", order_id="oid-morning")
        R.save(st, path)
        orders = _fill_orders(symbol, [
            (stale, "BUY", 40, "oid-morning"),
            ("2026-11-17T09:40:00+05:30", "SELL", 40, "oid-flat"),
            (fill_ts, "BUY", 40, "2122560602726916096"),
        ])
        morning = bars_3m(ts(9, 30, day=17), [82345.0, 82300.0, 82248.75])
        old = R.STATE
        try:
            R.STATE = path
            ctx = E.prepare(now=ts(10, 53, day=17), state=R.load(path), master_rows=[],
                            orders=orders, state_path=path)
            fire_, why, action = E.evaluate(symbol, product="MIS", broker="kite",
                                            exchange="BFO", state=ctx, now=ts(10, 53, day=17),
                                            bars=morning, apply_effective=False, log=False,
                                            orders=orders, sync_route=True)
            assert not fire_, why
            saved = R.load(path)
            rec = saved["positions"][key]
            assert rec["opened"] == fill_ts or rec["opened"].startswith("2026-11-17T10:50:57"), rec
            assert rec["order_id"] == "2122560602726916096", rec
            assert rec["route"] == "3m"
        finally:
            R.STATE = old


def test_add_does_not_reset_entry_time():
    """An add on the same life keeps the original opening fill as entry."""
    opened = ts(9, 31, day=17)
    symbol = "NIFTY26NOV25350CE"
    orders = _fill_orders(symbol, [
        (opened.isoformat(), "BUY", 65, "oid-open"),
        ("2026-11-17T10:50:57+05:30", "BUY", 65, "oid-add"),
    ])
    # morning two-candle would fire against 09:31; that is correct (same life)
    b = bars_3m(ts(9, 30, day=17), [100, 99, 98])
    st = {}
    key = R.ukey("NSE_FO|ADD")
    R.record(st, key, route_name="3m", index="NIFTY", symbol=symbol,
             product="I", qty=130, ts=opened.isoformat(), broker="upstox", order_id="oid-open")
    ctx = E.prepare(now=ts(11, 0, day=17), state=st, master_rows=[], orders=orders)
    fire_, why, action = E.evaluate(symbol, product="I", broker="upstox",
                                    instrument_key="NSE_FO|ADD", state=ctx,
                                    now=ts(11, 0, day=17), bars=b, apply_effective=False,
                                    log=False, orders=orders)
    assert fire_ and action == "TWO_CANDLE_EXIT", (why, action)
    _e, src, fts, foid = E.resolve_entry(st["positions"][key], orders, symbol, token="NSE_FO|ADD")
    assert src == "opening_fill"
    assert foid == "oid-open", foid
    assert "09:31" in (fts or "") or _e.hour == 9, fts


def test_ema50_ignores_pre_fill_break_on_stale_route():
    """EMA50 break on a pre-re-entry bar must not fire after a 10:50 fill."""
    b, t, px = _warmup()
    # drop through EMA50 at `t` (completes t+3), then a later harmless bar
    b.append(bar(t, 50.0))
    fill = t + dt.timedelta(minutes=10)
    b.append(bar(fill, px + 1.0))  # stays above EMA50 after the fill
    stale = (t - dt.timedelta(minutes=3)).isoformat()
    symbol = "NIFTY26NOV25250CE"
    orders = _fill_orders(symbol, [
        (stale, "BUY", 65, "oid-m"),
        ((t + dt.timedelta(minutes=4)).isoformat(), "SELL", 65, "oid-f"),
        (fill.isoformat(), "BUY", 65, "oid-re"),
    ])
    st = {}
    key = R.ukey("NSE_FO|E50")
    R.record(st, key, route_name="3m", index="NIFTY", symbol=symbol,
             product="I", qty=65, ts=stale, broker="upstox")
    ctx = E.prepare(now=fill + dt.timedelta(minutes=5), state=st, master_rows=[], orders=orders)
    fire_, why, action = E.evaluate(symbol, product="I", broker="upstox",
                                    instrument_key="NSE_FO|E50", state=ctx,
                                    now=fill + dt.timedelta(minutes=5), bars=b,
                                    apply_effective=False, log=False, orders=orders)
    assert not fire_, (why, action)
    # without the fill tape, the stale opened time would have fired EMA50
    fire2, why2, action2 = E.evaluate(symbol, product="I", broker="upstox",
                                      instrument_key="NSE_FO|E50",
                                      state=E.prepare(now=fill + dt.timedelta(minutes=5),
                                                       state=st, master_rows=[], orders=None),
                                      now=fill + dt.timedelta(minutes=5), bars=b,
                                      apply_effective=False, log=False)
    assert fire2 and action2 == "EMA50_EXIT", (why2, action2)


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok ", name)
            except Exception as e:
                failed += 1
                print("FAIL", name, type(e).__name__ + ":", e)
    if failed:
        raise SystemExit("%d test(s) failed" % failed)
    print("all passed")
