#!/usr/bin/env python3
"""Watchlist entry decisions. No broker calls and no orders.

Fresh 1-hour triggers stay eligible. An already-aligned 1-hour trend is also a
first entry when Supertrend is strictly beyond EMA20. The same name is not
entered twice. One PSU bank is kept, the one with the clearer separation.
"""
import datetime as dt
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import stockopt_st_ema as st
import stockopt_st_ema_entry as entry
import stockopt_add as add
import stockopt_policy as pol
import stockopt_tech_entry as tech
import autonomy

IST = st.IST
WEEK = ["2026-11-09", "2026-11-11", "2026-11-12", "2026-11-13"]
NAMES = ["KALYANKJIL", "BANKBARODA", "PNB", "SBIN", "IREDA"]
NOW = dt.datetime(2026, 11, 11, 11, 15, tzinfo=IST)
QUOTE = {"ltp": 10.0, "bid": 10.0, "ask": 10.2, "oi": 500, "tick": 0.05}
RESULTS = {"ok": True, "names": []}
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("ok   ", name)
        return
    FAILS.append(name)
    print("FAIL ", name, detail)


def contract(symbol="SBIN", lot=250):
    return {"expiry_date": dt.date(2026, 11, 26), "instrument_type": "CE", "strike_price": 100,
            "lot_size": lot, "trading_symbol": "%s26NOV100CE" % symbol,
            "instrument_key": "NSE_FO|%s" % symbol, "underlying_key": "NSE_EQ|%s" % symbol}


def fresh_ce(**kw):
    bar = dict(close=110.0, ema20=100.0, dir=1, st=105.0,
               prev_close=99.0, prev_ema=100.0, prev_dir=-1, hist=1.0, adx=20)
    bar.update(kw)
    return bar


def aligned_ce(st_value=105.0, ema=100.0, close=110.0, hist=1.0):
    return dict(close=close, ema20=ema, dir=1, st=st_value,
                prev_close=close, prev_ema=ema, prev_dir=1, hist=hist, adx=30)


def plan(name, bar, watchlist=None, **kw):
    base = dict(chain_count=4, quote=QUOTE, results=RESULTS, contract=contract(name),
                held=False, entered_this_week=False, books_readable=True)
    base.update(kw)
    return st.plan_name(name, watchlist or NAMES, bar, **base)


def test_paths_and_no_second_entry():
    fresh = st.entry_signal(fresh_ce())
    check("fresh trigger still enters", fresh["side"] == "CE" and fresh["path"] == "fresh" and fresh["fresh"] is True, fresh)
    joined = st.entry_signal(aligned_ce())
    check("already-aligned uptrend joins", joined["side"] == "CE" and joined["path"] == "aligned" and joined["fresh"] is False, joined)
    put = st.entry_signal(dict(close=90, ema20=100, dir=-1, st=95,
                               prev_close=90, prev_ema=100, prev_dir=-1, hist=-1, adx=22))
    check("already-aligned downtrend is a put", put["side"] == "PE" and put["path"] == "aligned", put)
    equal = st.entry_signal(aligned_ce(st_value=100.0, ema=100.0, close=110.0))
    check("equal Supertrend and EMA20 does not join", equal["side"] is None, equal)
    weak_macd = st.entry_signal(fresh_ce(hist=-1))
    check("a fresh bar with MACD against is not loosened into a join", weak_macd["side"] is None, weak_macd)
    ireda = st.entry_signal(aligned_ce(st_value=117.98, ema=117.67, close=120.2, hist=-0.19))
    check("already aligned still joins when the histogram is negative", ireda["side"] == "CE" and ireda["path"] == "aligned", ireda)
    fresh_plan = plan("SBIN", fresh_ce())
    aligned_plan = plan("BANKBARODA", aligned_ce(st_value=266.07, ema=262.86, close=275.0))
    check("fresh plan is 1 lot and not an add",
          fresh_plan["action"] == "enter" and fresh_plan["lots"] == 1 and fresh_plan["quantity"] == 250
          and fresh_plan["add"] is False and fresh_plan["send"] is False, fresh_plan)
    check("aligned plan is a 1-lot first entry",
          aligned_plan["action"] == "enter" and aligned_plan["lots"] == 1 and aligned_plan["path"] == "aligned"
          and aligned_plan["add"] is False, aligned_plan)
    held = plan("SBIN", aligned_ce(), held=True)
    again = plan("SBIN", aligned_ce(), entered_this_week=True)
    check("held name is not entered from the aligned path",
          held["action"] == "skip" and held["lots"] == 0 and held["add"] is False, held)
    check("a name already entered this week is not entered again",
          again["action"] == "skip" and again["lots"] == 0 and "add" in again["reason"], again)
    duplicate = st.select_entries([fresh_plan, dict(fresh_plan, path="aligned", fresh=False)], open_count=0)
    enters = [row for row in duplicate if row["action"] == "enter"]
    check("the two paths do not both enter the same name",
          len(enters) == 1 and any("other path" in row["reason"] for row in duplicate), duplicate)
    check("off-list name is rejected",
          plan("RELIANCE", fresh_ce(), watchlist=NAMES)["reason"].startswith("off watchlist"), )
    wide = NAMES + ["RELIANCE"]
    kept, dropped, err = st.normalize_watchlist(wide)
    check("max 5 list is respected", kept == NAMES and dropped[-1]["symbol"] == "RELIANCE" and err is None, (kept, dropped))
    check("dated 2-lot grant stays expired on this path",
          autonomy.active(dt.date(2026, 11, 5)) is True and autonomy.active(dt.date(2026, 11, 11)) is False
          and st.stock_lots(dt.date(2026, 11, 5)) == 1 and st.stock_lots() == 1)
    check("adds stay off", add.ADD_PAUSED is True and pol.ADD_EXECUTION_ENABLED is False and tech.TECHNICAL_PAUSED is True)


def test_psu_bank_picks_the_clearer_name():
    published = {
        "KALYANKJIL": (637.06, 629.15, 663.75),
        "BANKBARODA": (266.07, 262.86, 275.0),
        "PNB": (133.81, 132.63, 138.48),
        "SBIN": (1043.76, 1043.18, 1061.2),
        "IREDA": (117.98, 117.67, 120.2),
    }
    bob = (published["BANKBARODA"][0] - published["BANKBARODA"][1]) / published["BANKBARODA"][1]
    pnb = (published["PNB"][0] - published["PNB"][1]) / published["PNB"][1]
    sbin = (published["SBIN"][0] - published["SBIN"][1]) / published["SBIN"][1]
    check("this week Bank of Baroda separates more than PNB and SBIN", bob > pnb > sbin, (bob, pnb, sbin))
    decisions = []
    for name, (st_value, ema, close) in published.items():
        decisions.append(plan(name, aligned_ce(st_value, ema, close, hist=-0.19 if name == "IREDA" else 0.5)))
    chosen = st.select_entries(decisions, open_count=0)
    entered = [row["underlying"] for row in chosen if row["action"] == "enter"]
    check("one PSU bank is kept and it is Bank of Baroda",
          "BANKBARODA" in entered and "PNB" not in entered and "SBIN" not in entered, entered)
    check("clearer non-bank names stay ahead of the thin gaps",
          entered[0] == "KALYANKJIL" and "IREDA" in entered, entered)
    tight = st.select_entries(decisions, open_count=2)
    tight_names = [row["underlying"] for row in tight if row["action"] == "enter"]
    check("limited capacity keeps the clearer name", tight_names == ["KALYANKJIL"], tight_names)
    psu_reasons = {row["underlying"]: row["reason"] for row in chosen if row["underlying"] in ("PNB", "SBIN")}
    check("the other PSU banks record the sector cap",
          all("BANKBARODA" in text and "PSU" in text for text in psu_reasons.values()), psu_reasons)


def _scope():
    return {"st_ema_category_watchlist": list(NAMES), "week_trading_days": list(WEEK),
            "indices": ["NIFTY", "BANKNIFTY", "SENSEX"], "commodities": ["CRUDEOIL"]}


def _market(bars, place):
    chains = {name: [contract(name)] for name in NAMES}

    def analyze(key):
        symbol = str(key).split("|")[-1]
        return bars[symbol]

    def quote(_key):
        return dict(QUOTE)

    def check_order(order, _broker):
        return True, [], order["quantity"]

    return {"chains": chains, "analyze": analyze, "quote": quote, "positions": {"upstox": [], "kite": []},
            "check_order": check_order, "place": place}


def test_runner_places_one_lot_and_does_not_double():
    calls = []

    def place(broker, order):
        calls.append((broker, order["trading_symbol"], order["quantity"], order["transaction_type"]))
        return "id-%s" % len(calls)

    bars = {name: aligned_ce(105 + index, 100, 110) for index, name in enumerate(NAMES)}
    # Give Bank of Baroda a clearly wider gap than the other PSU banks, and Kalyan the widest.
    bars["KALYANKJIL"] = aligned_ce(120, 100, 130)
    bars["BANKBARODA"] = aligned_ce(112, 100, 120)
    bars["PNB"] = aligned_ce(104, 100, 110)
    bars["SBIN"] = aligned_ce(101, 100, 108)
    bars["IREDA"] = aligned_ce(102, 100, 109, hist=-0.2)
    with tempfile.TemporaryDirectory() as directory:
        ledger = os.path.join(directory, "ledger.json")
        result = entry.run(_scope(), NOW, _market(bars, place), execute=True, dry=False,
                           ledger_path=ledger, session_ok=True, results=RESULTS)
        symbols = [row[1] for row in calls]
        check("aligned first entries place orders", result["orders_placed"] > 0 and calls, calls)
        check("each placed order is 1 lot", calls and all(qty == 250 for _b, _s, qty, side in calls if side == "BUY"), calls)
        check("runner keeps a single PSU bank",
              any(sym.startswith("BANKBARODA") for sym in symbols)
              and not any(sym.startswith("PNB") or sym.startswith("SBIN") for sym in symbols), symbols)
        saved = json.load(open(ledger))
        check("filled names are marked for Friday",
              saved["names"]["BANKBARODA"]["flatten_by"] == "2026-11-13"
              and saved["names"]["BANKBARODA"]["source"] == "st_ema_week", saved)
        before = len(calls)
        bars["BANKBARODA"] = fresh_ce(close=130, ema20=100, st=112)
        again = entry.run(_scope(), NOW, _market(bars, place), execute=True, dry=False,
                          ledger_path=ledger, session_ok=True, results=RESULTS)
        bob = [row for row in again["actions"] if row["underlying"] == "BANKBARODA"]
        check("the other path does not add to a name entered this week",
              len(calls) == before and bob and bob[0]["action"] == "skip" and bob[0]["lots"] == 0, bob)
    fresh_calls = []

    def place_fresh(broker, order):
        fresh_calls.append(order["quantity"])
        return "fresh"

    fresh_bars = {name: fresh_ce(hist=-1) for name in NAMES}
    fresh_bars["SBIN"] = fresh_ce()
    with tempfile.TemporaryDirectory() as directory:
        ledger = os.path.join(directory, "ledger.json")
        result = entry.run(_scope(), NOW, _market(fresh_bars, place_fresh), execute=True, dry=False,
                           ledger_path=ledger, session_ok=True, results=RESULTS)
        sbin = [row for row in result["actions"] if row["underlying"] == "SBIN"][0]
        check("fresh trigger still places 1 lot",
              sbin["action"] == "enter" and sbin["path"] == "fresh" and fresh_calls == [250, 250], (sbin, fresh_calls))
    scope = json.load(open(os.path.join(ROOT, "monitor_scope.json")))
    this_week = ["ANURAS", "PAGEIND", "GABRIEL", "POLICYBZR", "AEGISVOPAK"]
    check("published scope keeps crude and this week's five",
          scope["commodities"] == ["CRUDEOIL"] and scope["indices"] == ["NIFTY", "BANKNIFTY", "SENSEX"]
          and scope["st_ema_category_watchlist"] == this_week
          and "GLENMARK" not in scope["stock_option_underlyings"], scope.get("st_ema_category_watchlist"))
    friday = st.friday_flatten_plan(dt.datetime(2026, 11, 13, 15, 10, tzinfo=IST), dt.date(2026, 11, 13))
    check("weekly list is due flat by Friday close and this plan does not send by itself",
          friday["due"] is True and friday["send"] is False, friday)


def main():
    test_paths_and_no_second_entry()
    test_psu_bank_picks_the_clearer_name()
    test_runner_places_one_lot_and_does_not_double()
    print("%s failed" % len(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
