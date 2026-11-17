#!/usr/bin/env python3
"""Offline tests for index_routes (t673u). No broker, no network, auto_entry not imported."""
import ast, datetime as dt, os, sys, tempfile
D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
import index_routes as R
d = dt.date
IST = R.IST
NIFTY = [{"expiry": d(2026, 11, 17), "weekly": True}, {"expiry": d(2026, 11, 23), "weekly": False},
         {"expiry": d(2026, 12, 1), "weekly": True}, {"expiry": d(2026, 12, 8), "weekly": True},
         {"expiry": d(2026, 12, 29), "weekly": False}]
NFUT = {d(2026, 11, 23), d(2026, 12, 29), d(2027, 1, 25)}
SENSEX = [{"expiry": d(2026, 11, 19), "weekly": True}, {"expiry": d(2026, 11, 26), "weekly": False},
          {"expiry": d(2026, 12, 3), "weekly": True}, {"expiry": d(2026, 12, 31), "weekly": False}]
SFUT = {d(2026, 11, 26), d(2026, 12, 31)}
BANK = [{"expiry": d(2026, 11, 23), "weekly": False}, {"expiry": d(2026, 12, 29), "weekly": False}]


def ex(u, rt, s, rows, fut):
    c = R.select_expiry(u, rt, s, rows, fut); assert c["ok"], c; return c["expiry"]


def test_expiry():
    s = d(2026, 11, 16)
    assert ex("NIFTY", "3m", s, NIFTY, NFUT) == d(2026, 11, 17)
    assert ex("NIFTY", "15m", s, NIFTY, NFUT) == d(2026, 11, 23)
    assert ex("NIFTY", "1h", s, NIFTY, NFUT) == d(2026, 11, 23)       # monthly is next week -> kept
    # 18 Nov: 23 Nov monthly still next ISO week; 15m = week after 23 Nov = 1 Dec
    assert ex("NIFTY", "15m", d(2026, 11, 18), NIFTY, NFUT) == d(2026, 12, 1)
    assert ex("NIFTY", "1h", d(2026, 11, 18), NIFTY, NFUT) == d(2026, 11, 23)
    # 23 Nov (Mon): monthly is in the current week -> next month
    assert ex("NIFTY", "1h", d(2026, 11, 23), NIFTY, NFUT) == d(2026, 12, 29)
    assert ex("NIFTY", "3m", d(2026, 11, 23), NIFTY, NFUT) == d(2026, 11, 23)
    assert ex("SENSEX", "3m", s, SENSEX, SFUT) == d(2026, 11, 19)
    assert ex("SENSEX", "15m", s, SENSEX, SFUT) == d(2026, 11, 26)
    assert ex("SENSEX", "1h", s, SENSEX, SFUT) == d(2026, 11, 26)
    assert ex("SENSEX", "1h", d(2026, 11, 23), SENSEX, SFUT) == d(2026, 12, 31)
    assert ex("BANKNIFTY", "3m", s, BANK, NFUT) == d(2026, 11, 23)
    assert ex("BANKNIFTY", "15m", s, BANK, NFUT) == d(2026, 11, 23)
    assert ex("BANKNIFTY", "1h", s, BANK, NFUT) == d(2026, 11, 23)
    assert ex("BANKNIFTY", "1h", d(2026, 11, 23), BANK, NFUT) == d(2026, 12, 29)
    # nothing substituted when next week is not listed
    assert R.select_expiry("NIFTY", "15m", s, NIFTY[:1], NFUT)["ok"] is False
    assert R.select_expiry("FINNIFTY", "1h", s, NIFTY, NFUT)["ok"] is False


def test_window():
    mon = lambda h, m: dt.datetime(2026, 11, 16, h, m, tzinfo=IST)
    fri = lambda h, m: dt.datetime(2026, 11, 20, h, m, tzinfo=IST)
    assert R.in_window("1h", mon(9, 29))[0] is False and R.in_window("1h", mon(9, 30))[0]
    assert R.in_window("1h", mon(15, 25))[0] and R.in_window("1h", mon(15, 26))[0] is False
    assert R.in_window("3m", mon(15, 10))[0] and R.in_window("3m", mon(15, 11))[0] is False
    assert R.in_window("15m", fri(15, 5))[0] and R.in_window("15m", fri(15, 6))[0] is False
    assert R.in_window("1h", dt.datetime(2026, 11, 21, 10, 0, tzinfo=IST))[0] is False


def bar(ts, c, e, di, h=1.0, adx=20):
    return {"ts": ts, "close": c, "ema20": e, "dir": di, "hist": h, "adx": adx}


def test_continuation():
    s = d(2026, 11, 16)
    bars = [bar("2026-11-13T15:00:00+05:30", 101, 100, 1), bar("2026-11-16T09:15:00+05:30", 102, 100, 1)]
    side, det = R.continuation(bars, s); assert side == "CE", det
    bars[-1]["hist"] = -1; assert R.continuation(bars, s)[0] is None
    bars[-1]["hist"] = 1; bars[-1]["adx"] = 10; assert R.continuation(bars, s)[0] is None
    pe = [bar("2026-11-13T15:00:00+05:30", 99, 100, -1, -1), bar("2026-11-16T09:15:00+05:30", 98, 100, -1, -2)]
    assert R.continuation(pe, s)[0] == "PE"
    flip = [bar("2026-11-13T15:00:00+05:30", 99, 100, -1, -1), bar("2026-11-16T09:15:00+05:30", 102, 100, 1)]
    assert R.continuation(flip, s)[0] is None
    stale = [bar("2026-11-12T15:00:00+05:30", 101, 100, 1), bar("2026-11-13T15:00:00+05:30", 101, 100, 1)]
    assert R.continuation(stale, s)[0] is None          # no completed bar today yet


def test_tracking_and_dedupe():
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "s.json")
        st = R.load(p); assert st == {}
        R.record(st, R.ukey("NSE_FO|1"), route_name="3m", index="NIFTY", symbol="X", product="I", qty=65, ts="t", broker="upstox")
        R.save(st, p); st2 = R.load(p)
        assert R.route_of(st2, R.ukey("NSE_FO|1"), "I", None, set()) == ("3m", "tracked")
    # legacy inference (read-only): existing BANKNIFTY NRML monthly -> 1h
    assert R.route_of({}, "kite|NFO|BANKNIFTY26NOV61200CE", "NRML", d(2026, 11, 23), NFUT) == ("1h", "inferred")
    assert R.infer_route("MIS", d(2026, 11, 23), NFUT) == "3m"
    assert R.infer_route("D", d(2026, 12, 1), NFUT) == "15m"
    occ = {"3m": [], "15m": [], "1h": ["BANKNIFTY26NOV61200CE"]}
    assert R.dedupe_route("BANKNIFTY", "kite", "1h", occ, [])["ok"] is False
    assert R.dedupe_route("BANKNIFTY", "kite", "3m", occ, [])["ok"] is True
    assert R.dedupe_route("BANKNIFTY", "kite", "3m", None, [])["ok"] is False
    rows = [{"index": "NIFTY", "timeframe": "1h", "brokers": ["upstox"]}]   # pre-t673u row counts as 1h
    assert R.dedupe_route("NIFTY", "upstox", "1h", {}, rows)["ok"] is False
    assert R.dedupe_route("NIFTY", "kite", "1h", {}, rows)["ok"] is True
    assert R.dedupe_route("NIFTY", "upstox", "15m", {}, rows)["ok"] is True
    assert R.dedupe_route("NIFTY", "upstox", "3m", {}, [{"index": "NIFTY", "route": "3m"}])["ok"] is False
    assert R.SPEC["3m"]["upstox_product"] == "I" and R.SPEC["3m"]["kite_product"] == "MIS"
    assert R.SPEC["15m"]["kite_product"] == "NRML" and R.SPEC["1h"]["upstox_product"] == "D"
    assert R.enabled_routes({}) == ["3m", "15m", "1h"] and R.enabled_routes({"INDEX_ROUTES": "1h,bogus"}) == ["1h"]


def test_refresh_opened():
    st = {}
    key = R.ukey("NSE_FO|1")
    assert R.refresh_opened(st, key, "2026-11-17T10:50:57+05:30") is False  # untracked
    R.record(st, key, route_name="3m", index="NIFTY", symbol="X", product="I", qty=65,
             ts="2026-11-17T09:31:30+05:30", broker="upstox", order_id="old")
    assert R.refresh_opened(st, key, "2026-11-17T10:50:57+05:30", order_id="new", qty=40) is True
    rec = st["positions"][key]
    assert rec["opened"] == "2026-11-17T10:50:57+05:30"
    assert rec["order_id"] == "new" and rec["qty"] == 40 and rec["route"] == "3m"
    assert R.refresh_opened(st, key, "2026-11-17T10:50:57+05:30", order_id="new", qty=40) is False


def test_wiring():
    src = open(os.path.join(D, "auto_entry.py")).read(); ast.parse(src)
    assert "iroutes.select_expiry(name, ROUTE, now.date(), _rows, monthlies)" in src
    assert "iroutes.dedupe_route(name, b, ROUTE," in src
    assert 'body={"quantity":uleg,"product":_uprod,' in src and "kprod=_kprod" in src
    assert "and not ekw and ROUTE is None:" in src           # no I->D fallback on a route
    i_cap = src.index('if not LOSS_CAP["ok"]:'); i_exp = src.index("prules.expiry_too_near(exp"); i_place = src.index("order/place")
    assert i_cap < i_exp < i_place
    sq = open(os.path.join(D, "index_3m_squareoff.py")).read(); ast.parse(sq)
    assert '.get("route") == "3m"' in sq and '"--execute" in sys.argv' in sq


if __name__ == "__main__":
    test_expiry(); test_window(); test_continuation(); test_tracking_and_dedupe(); test_refresh_opened(); test_wiring()
    print("index_routes tests passed (synthetic, no broker, no orders)")
