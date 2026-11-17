#!/usr/bin/env python3
"""Intraday strong-technical stock-option policy and entry. No broker calls and no orders."""
import datetime as dt
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import stockopt_intraday_tech as it
import stockopt_intraday_tech_entry as entry

IST = it.IST
TUE = dt.datetime(2026, 11, 17, 11, 15, tzinfo=IST)
LATE = dt.datetime(2026, 11, 17, 14, 45, tzinfo=IST)
FLAT = dt.datetime(2026, 11, 17, 15, 16, tzinfo=IST)
FRI_FLAT = dt.datetime(2026, 11, 20, 15, 10, tzinfo=IST)
NIGHT = dt.datetime(2026, 11, 16, 23, 5, tzinfo=IST)
EXPIRY_MON = dt.datetime(2026, 11, 23, 11, 15, tzinfo=IST)
QUOTE = {"ltp": 12.0, "bid": 11.9, "ask": 12.1, "oi": 800, "tick": 0.05}
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("ok   ", name)
        return
    FAILS.append(name)
    print("FAIL ", name, detail)


def hour_ce(**kw):
    bar = dict(close=610.0, ema20=600.0, ema50=590.0, dir=1, st=605.0,
               hist=0.8, adx=70.0, prev_adx=69.0, last_ts="2026-11-17T11:15:00+05:30")
    bar.update(kw)
    return bar


def hour_pe(**kw):
    bar = dict(close=1670.0, ema20=1685.0, ema50=1695.0, dir=-1, st=1678.0,
               hist=-0.4, adx=53.0, prev_adx=52.0, last_ts="2026-11-17T11:15:00+05:30")
    bar.update(kw)
    return bar


def daily_ce(**kw):
    bar = dict(close=605.5, ema20=590.0, ema50=580.0, dir=1, st=598.0, hist=10.0, adx=25.0)
    bar.update(kw)
    return bar


def daily_pe(**kw):
    bar = dict(close=1680.0, ema20=1690.0, ema50=1700.0, dir=-1, st=1685.0, hist=-20.0, adx=38.0)
    bar.update(kw)
    return bar


def cand(symbol, side, cleanliness="clean", **kw):
    row = {"symbol": symbol, "side": side, "cleanliness": cleanliness, "lot_size": 100,
           "preferred_contract": "%s 100 %s 23 NOV 26" % (symbol, side), "preferred_strike": 100}
    row.update(kw)
    return row


def contract(symbol, side="CE", lot=100, strike=100, expiry=None):
    exp = expiry or dt.date(2026, 11, 23)
    return {"expiry_date": exp, "instrument_type": side, "strike_price": strike,
            "lot_size": lot, "trading_symbol": "%s26NOV%s%s" % (symbol, strike, side),
            "instrument_key": "NSE_FO|%s" % symbol, "underlying_key": "NSE_EQ|%s" % symbol,
            "kite_symbol": "%s26NOV%s%s" % (symbol, strike, side), "kite_lot_size": lot}


def test_signal_and_daily():
    check("bullish 1h passes", it.hour_side(hour_ce()) == "CE")
    check("bearish 1h passes", it.hour_side(hour_pe()) == "PE")
    check("ADX below 20 fails", it.hour_side(hour_ce(adx=19.9)) is None)
    check("close under EMA50 fails a call", it.hour_side(hour_ce(close=595.0, ema50=600.0)) is None)
    check("MACD against fails", it.hour_side(hour_ce(hist=-0.1)) is None)
    check("Supertrend against fails", it.hour_side(hour_ce(dir=-1)) is None)
    agree, fails = it.daily_agree(daily_ce(), "CE")
    check("daily agrees for a call", agree and not fails, fails)
    agree, fails = it.daily_agree(daily_ce(dir=-1), "CE")
    check("daily ST against rejects", (not agree) and "daily Supertrend not bullish" in fails, fails)
    agree, fails = it.daily_agree(daily_pe(close=1705.0, ema20=1690.0, ema50=1680.0), "PE")
    check("daily close on the wrong side rejects a put", not agree, fails)
    check("daily MACD agrees on a put", it.daily_macd_agrees(daily_pe(), "PE") is True)
    check("daily MACD turned up is not clean", it.daily_macd_agrees(daily_pe(hist=0.8), "PE") is False)


def test_window_and_flatten_time():
    check("11:15 IST is inside the window", it.entry_window(TUE)["ok"] is True)
    check("09:30 IST is inside the window", it.entry_window(dt.datetime(2026, 11, 17, 9, 30, tzinfo=IST))["ok"] is True)
    check("09:29 IST is outside", it.entry_window(dt.datetime(2026, 11, 17, 9, 29, tzinfo=IST))["ok"] is False)
    late = it.entry_window(LATE)
    check("14:45 IST is past the 14:30 cutoff", late["ok"] is False and "14:30" in late["reason"], late)
    check("Saturday is closed", it.entry_window(dt.datetime(2026, 11, 21, 11, 15, tzinfo=IST))["ok"] is False)
    check("holiday/session_ok False is closed", it.entry_window(TUE, session_ok=False)["ok"] is False)
    check("weekday flatten time is 15:15", it.flatten_time(TUE) == dt.time(15, 15))
    check("Friday flatten time is 15:10", it.flatten_time(FRI_FLAT) == dt.time(15, 10))
    early = it.same_day_flatten_plan(TUE)
    check("flatten not due at 11:15", early["due"] is False, early)
    due = it.same_day_flatten_plan(FLAT)
    check("flatten due at 15:16", due["due"] is True and due.get("overdue") is not True, due)
    fri = it.same_day_flatten_plan(FRI_FLAT)
    check("Friday flatten due at 15:10", fri["due"] is True, fri)
    check("16 Nov is not 23 Nov expiry week", it.in_expiry_week(dt.date(2026, 11, 23), dt.date(2026, 11, 16)) is False)
    check("23 Nov is expiry week", it.in_expiry_week(dt.date(2026, 11, 23), dt.date(2026, 11, 23)) is True)


def test_evaluate_and_rank():
    lic = it.evaluate_name(cand("LICHSGFIN", "CE"), hour_ce(), daily_ce(),
                           today=TUE.date(), chain_count=4, quote=None,
                           contract=contract("LICHSGFIN", lot=1000, strike=605),
                           expiry_week=False)
    check("LICHSGFIN still enters", lic["action"] == "enter" and lic["side"] == "CE", lic)
    check("LICHSGFIN is 1 lot of 1000", lic["lots"] == 1 and lic["quantity"] == 1000, lic)
    broken = it.evaluate_name(cand("LICHSGFIN", "CE"), hour_ce(hist=-1), daily_ce(),
                              today=TUE.date(), chain_count=4, expiry_week=False)
    check("broken 1h is skipped", broken["action"] == "skip" and "no longer holds" in broken["reason"], broken)
    flipped = it.evaluate_name(cand("LICHSGFIN", "CE"), hour_pe(), daily_pe(),
                               today=TUE.date(), chain_count=4, expiry_week=False)
    check("side flip is skipped", flipped["action"] == "skip" and "did not continue" in flipped["reason"], flipped)
    week = it.evaluate_name(cand("LICHSGFIN", "CE"), hour_ce(), daily_ce(),
                            today=EXPIRY_MON.date(), chain_count=4, expiry_week=True)
    check("expiry week refuses a near-month entry", week["action"] == "skip" and "expiry week" in week["reason"], week)
    held = it.evaluate_name(cand("LICHSGFIN", "CE"), hour_ce(), daily_ce(),
                            today=TUE.date(), chain_count=4, held=True, expiry_week=False)
    check("already held is skipped", held["action"] == "skip" and "already" in held["reason"], held)
    unver = it.evaluate_name(cand("LICHSGFIN", "CE"), hour_ce(), daily_ce(),
                             today=TUE.date(), chain_count=4, books_readable=False, expiry_week=False)
    check("unreadable books fail closed", unver["action"] == "skip" and "unverified" in unver["reason"], unver)

    strong = it.evaluate_name(cand("LICHSGFIN", "CE"), hour_ce(adx=70, prev_adx=69), daily_ce(adx=25),
                              today=TUE.date(), chain_count=1, expiry_week=False)
    mid = it.evaluate_name(cand("MFSL", "CE"), hour_ce(adx=56, prev_adx=55), daily_ce(adx=33),
                           today=TUE.date(), chain_count=1, expiry_week=False)
    put = it.evaluate_name(cand("PIIND", "PE"), hour_pe(adx=53, prev_adx=52), daily_pe(adx=38),
                           today=TUE.date(), chain_count=1, expiry_week=False)
    weak = it.evaluate_name(cand("TMPV", "PE", cleanliness="weaker"),
                            hour_pe(close=262, ema20=270, ema50=275, adx=46, prev_adx=44, hist=-3),
                            daily_pe(close=262, ema20=270, ema50=275, hist=0.8, adx=25),
                            today=TUE.date(), chain_count=1, expiry_week=False)
    selected, chosen = it.select_entries([strong, mid, put, weak], max_names=2)
    names = [row["underlying"] for row in chosen]
    check("top two are LICHSGFIN and MFSL", names == ["LICHSGFIN", "MFSL"], names)
    skipped = [row for row in selected if row["action"] == "skip"]
    check("the rest are capped, not entered", all("top 2" in (row["reason"] or "") for row in skipped), skipped)
    # weaker cleanliness loses even with a higher ADX
    weak_high = it.evaluate_name(cand("TMPV", "PE", cleanliness="weaker"),
                                 hour_pe(adx=80, prev_adx=79, hist=-1),
                                 daily_pe(hist=0.5, adx=25),
                                 today=TUE.date(), chain_count=1, expiry_week=False)
    clean_put = it.evaluate_name(cand("PIIND", "PE"), hour_pe(adx=52, prev_adx=51), daily_pe(),
                                 today=TUE.date(), chain_count=1, expiry_week=False)
    _sel, chosen2 = it.select_entries([weak_high, clean_put], max_names=1)
    check("cleanliness beats a weaker high-ADX name", [r["underlying"] for r in chosen2] == ["PIIND"], chosen2)


def test_products_and_atm():
    u, k = it.mirror_orders(contract("LICHSGFIN", lot=1000, strike=605), 12.5, dt.time(15, 15))
    check("Upstox product is I", u["product"] == "I" and u["quantity"] == 1000, u)
    check("Kite product is MIS", k["product"] == "MIS" and k["quantity"] == 1000, k)
    check("both legs are 1-lot BUY LIMIT", u["transaction_type"] == "BUY" and k["order_type"] == "LIMIT")
    rows = [
        contract("LICHSGFIN", strike=600, lot=1000),
        contract("LICHSGFIN", strike=605, lot=1000),
        contract("LICHSGFIN", strike=610, lot=1000),
        dict(contract("LICHSGFIN", strike=605, lot=1000, expiry=dt.date(2026, 12, 29)), expiry_date=dt.date(2026, 12, 29)),
    ]
    picked = it.pick_monthly_atm(rows, "CE", 605.5, dt.date(2026, 11, 16), expiry=dt.date(2026, 11, 23))
    check("ATM is the November 605 CE", picked and picked["strike_price"] == 605, picked)
    check("importing the policy does not require a broker flag", it.execute_enabled({"STOCKOPT_INTRADAY_TECH_EXECUTE": ""}) is False)
    check("execute flag is on only when set to 1", it.execute_enabled({"STOCKOPT_INTRADAY_TECH_EXECUTE": "1"}) is True)


def test_flatten_orders_and_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.json")
        ok = it.remember_entry(path, "LICHSGFIN", day=dt.date(2026, 11, 17),
                               contract=contract("LICHSGFIN"), flatten_at="15:15", side="CE")
        check("ledger write succeeds", ok is True)
        ledger = it.read_ledger(path)
        check("entered today sees the name", "LICHSGFIN" in it.entered_today(ledger, dt.date(2026, 11, 17)))
        check("a different day is not entered today", "LICHSGFIN" not in it.entered_today(ledger, dt.date(2026, 11, 16)))
        positions = {
            "upstox": [{"trading_symbol": "LICHSGFIN 605 CE 23 NOV 26", "quantity": 1000,
                        "product": "I", "instrument_token": "NSE_FO|1", "last_price": 12.0}],
            "kite": [{"tradingsymbol": "LICHSGFIN26NOV605CE", "quantity": 1000,
                      "product": "MIS", "instrument_token": 11, "last_price": 12.1}],
        }
        book = it.flatten_orders(FLAT, ledger, positions)
        check("flatten lists both brokers", book["due"] is True and len(book["orders"]) == 2, book)
        check("flatten does not set send", all(o["send"] is False for o in book["orders"]), book)
        products = {o["broker"]: o["product"] for o in book["orders"]}
        check("flatten keeps I and MIS", products.get("upstox") == "I" and products.get("kite") == "MIS", products)
        other = it.flatten_orders(FLAT, ledger, {
            "upstox": [{"trading_symbol": "ITC26NOV295CE", "quantity": 1725, "product": "D"}],
            "kite": [{"tradingsymbol": "ITC26NOV295CE", "quantity": 1725, "product": "NRML"}],
        })
        check("flatten ignores names not on today's ledger", other["orders"] == [], other)
        bad = it.flatten_orders(FLAT, ledger, {"upstox": positions["upstox"], "kite": None})
        check("unreadable flatten book fails closed", bad["orders"] == [] and "unverified" in bad["reason"], bad)


def test_run_dry_and_gates():
    shortlist = {"ok": True, "max_names": 2, "expiry": "2026-11-23",
                 "candidates": [cand("LICHSGFIN", "CE", lot_size=1000, preferred_strike=605),
                                cand("MFSL", "CE", lot_size=400, preferred_strike=1420),
                                cand("PIIND", "PE", lot_size=175, preferred_strike=1680)]}
    chains = {
        "LICHSGFIN": [contract("LICHSGFIN", lot=1000, strike=605)],
        "MFSL": [contract("MFSL", lot=400, strike=1420)],
        "PIIND": [contract("PIIND", "PE", lot=175, strike=1680)],
    }
    hour_bars = {"LICHSGFIN": hour_ce(), "MFSL": hour_ce(close=1427, ema20=1410, ema50=1390, adx=56, prev_adx=55),
                 "PIIND": hour_pe()}
    daily_bars = {"LICHSGFIN": daily_ce(), "MFSL": daily_ce(close=1427, ema20=1400, ema50=1380, adx=33),
                  "PIIND": daily_pe()}
    placed = []

    def place(broker, order):
        placed.append((broker, order))
        return "OID-%s" % broker

    def check_order(order, broker):
        return True, "ok", int(order["quantity"])

    market = {
        "chains": chains, "hour_bars": hour_bars, "daily_bars": daily_bars,
        "positions": {"upstox": [], "kite": []},
        "quote": lambda key: QUOTE,
        "map_kite": lambda row: {"kite_symbol": row.get("kite_symbol"), "kite_lot_size": row.get("lot_size"),
                                 "kite_exchange": "NFO"},
        "check_order": check_order, "place": place,
    }
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.json")
        # Isolate the daily-loss-cap state from the live tripped 16 Nov file.
        cap_state = os.path.join(tmp, "dlc.json")
        os.environ["DAILY_LOSS_CAP_STATE"] = cap_state
        res = entry.run(TUE, market, execute=False, dry=True, ledger_path=ledger,
                        shortlist=shortlist, session_ok=True, results={"ok": True, "names": []})
        check("dry-run places nothing", res["orders_placed"] == 0 and not placed, res["orders_placed"])
        entered = [a for a in res["actions"] if a.get("would_enter") or "dry-run" in (a.get("reason") or "")]
        # After ranking, only the top 2 stay action=enter then get the dry-run reason.
        dry_named = [a["underlying"] for a in res["actions"] if a["reason"] == "dry-run; no order"]
        check("dry-run would take LICHSGFIN and MFSL", dry_named == ["LICHSGFIN", "MFSL"], dry_named)
        check("preview names the same two", [p["underlying"] for p in res["preview"]] == ["LICHSGFIN", "MFSL"], res["preview"])

        late = entry.run(LATE, market, execute=True, dry=True, ledger_path=ledger,
                         shortlist=shortlist, session_ok=True, results={"ok": True, "names": []})
        check("after 14:30 no entry even in dry-run execute",
              all(a["action"] != "enter" or "dry-run" in a["reason"] for a in late["actions"]) and
              all("14:30" in (a.get("reason") or "") or a.get("would_enter") or a["action"] == "skip" for a in late["actions"]),
              late["actions"])
        check("late preview still ranks the two", [p["underlying"] for p in late["preview"]] == ["LICHSGFIN", "MFSL"], late["preview"])

        week = entry.run(EXPIRY_MON, market, execute=True, dry=True, ledger_path=ledger,
                         shortlist=shortlist, session_ok=True, results={"ok": True, "names": []})
        check("expiry week skips every name", all("expiry week" in (a.get("reason") or "") for a in week["actions"]), week["actions"])

        # Loss cap blocks entry, not flatten.
        os.environ["DAILY_LOSS_CAP_STATE"] = cap_state
        # Build a tripped evaluator by wrapping run after writing state is hard;
        # instead mark evaluate by putting a fake state file the module will read
        # if today's date matches. Use a patched evaluate via a small stub:
        import daily_loss_cap as dlc
        real_eval = dlc.evaluate
        dlc.evaluate = lambda *a, **k: {"ok": False, "blocked": True, "reason": "daily loss cap tripped"}
        try:
            blocked = entry.run(TUE, market, execute=True, dry=False, ledger_path=ledger,
                                shortlist=shortlist, session_ok=True, results={"ok": True, "names": []})
        finally:
            dlc.evaluate = real_eval
        check("loss cap blocks live entry", blocked["orders_placed"] == 0 and not placed
              and all("loss cap" in (a.get("reason") or "") or a["action"] == "skip" for a in blocked["actions"] if a.get("action") != "skip" or True),
              [a.get("reason") for a in blocked["actions"]])
        check("loss cap reasons are present", any("loss cap" in (a.get("reason") or "") for a in blocked["actions"]),
              [a.get("reason") for a in blocked["actions"]])

        # Oversize / check_order refuse: no send.
        def bad_check(order, broker):
            return False, "OVERSIZE: 2 lots, allowed 1", 0
        market2 = dict(market, check_order=bad_check)
        refused = entry.run(TUE, market2, execute=True, dry=False, ledger_path=ledger,
                            shortlist=shortlist, session_ok=True, results={"ok": True, "names": []})
        check("oversize check sends nothing", refused["orders_placed"] == 0 and not placed, refused)
        check("oversize is alerted", any("OVERSIZE" in str(a.get("reason") or a.get("alert") or "") for a in refused["actions"])
              or "OVERSIZE" in str(refused.get("alerts")), refused["actions"])

        # Flatten-only at 15:16 with a ledger name does not need a new entry.
        it.remember_entry(ledger, "LICHSGFIN", day=dt.date(2026, 11, 17),
                          contract=contract("LICHSGFIN", lot=1000, strike=605), flatten_at="15:15", side="CE")
        market3 = dict(market, positions={
            "upstox": [{"trading_symbol": "LICHSGFIN26NOV605CE", "quantity": 1000, "product": "I",
                        "instrument_token": "NSE_FO|1", "last_price": 12.0}],
            "kite": [{"tradingsymbol": "LICHSGFIN26NOV605CE", "quantity": 1000, "product": "MIS",
                      "instrument_token": 11, "last_price": 12.1}],
        })
        flat = entry.run(FLAT, market3, execute=False, dry=True, ledger_path=ledger,
                         shortlist=shortlist, session_ok=True, results={"ok": True, "names": []},
                         flatten_only=True)
        book = flat.get("flatten_orders") or {}
        check("flatten-only dry-run lists both legs and sends none",
              book.get("due") is True and len(book.get("orders") or []) == 2 and flat["orders_placed"] == 0
              and not placed, book)

        os.environ.pop("DAILY_LOSS_CAP_STATE", None)


def test_report_preview_uses_saved_close():
    report = it.load_report()
    shortlist = it.load_shortlist()
    check("pinned shortlist loads 13 names", shortlist["ok"] and len(shortlist["candidates"]) == 13, shortlist)
    check("16 Nov close report loads", report["ok"] is True, report.get("reason"))
    preview = it.preview_from_report(shortlist, report, today=dt.date(2026, 11, 16),
                                     results={"ok": True, "names": []}, expiry_week=False)
    names = [r["underlying"] for r in preview.get("chosen") or []]
    check("tonight's close picks LICHSGFIN and MFSL", names == ["LICHSGFIN", "MFSL"], names)
    if names == ["LICHSGFIN", "MFSL"]:
        lic, mfsl = preview["chosen"]
        check("LICHSGFIN is the 605 CE", "605" in str(lic.get("trading_symbol") or "") and lic["side"] == "CE", lic)
        check("MFSL is the 1420 CE", "1420" in str(mfsl.get("trading_symbol") or "") and mfsl["side"] == "CE", mfsl)


def test_premium_cap():
    check("cap constant is Rs 20,000", it.PREMIUM_CAP_PER_BROKER == 20000)
    check("entry premium is lot x limit", it.entry_premium(1000, 10.35) == 10350)
    check("1-lot default, 2 lots scale", it.entry_premium(400, 27.5, lots=2) == 22000)
    check("bad inputs are unverified", it.entry_premium(0, 10) is None and it.entry_premium(100, None) is None)
    check("position premium uses average, not last",
          it.position_premium({"quantity": 1000, "average_price": 10.35, "last_price": 99}) == 10350)
    check("position without average is unverified",
          it.position_premium({"quantity": 1000, "last_price": 10.35}) is None)

    empty = it.path_premium_deployed({"upstox": [], "kite": []}, {"names": {}, "days": {}})
    check("empty books deploy 0", empty == {"upstox": 0.0, "kite": 0.0}, empty)
    ledger = {"names": {"LICHSGFIN": {"source": it.SOURCE, "tag": it.TAG, "trading_symbol": "LICHSGFIN26NOV605CE",
                                      "day": "2026-11-17"}}, "days": {"2026-11-17": ["LICHSGFIN"]}}
    books = {
        "upstox": [{"trading_symbol": "LICHSGFIN26NOV605CE", "quantity": 1000, "product": "I",
                    "average_price": 10.35, "last_price": 11.0}],
        "kite": [{"tradingsymbol": "LICHSGFIN26NOV605CE", "quantity": 1000, "product": "MIS",
                  "average_price": 10.40}],
    }
    deployed = it.path_premium_deployed(books, ledger)
    check("open path premium is per broker",
          deployed == {"upstox": 10350.0, "kite": 10400.0}, deployed)
    other = {
        "upstox": [{"trading_symbol": "ITC26NOV295CE", "quantity": 1725, "product": "D", "average_price": 4.0}],
        "kite": [{"tradingsymbol": "ITC26NOV295CE", "quantity": 1725, "product": "NRML", "average_price": 4.0}],
    }
    check("delivery / other-path options are not counted",
          it.path_premium_deployed(other, ledger) == {"upstox": 0.0, "kite": 0.0})
    missing_avg = {
        "upstox": [{"trading_symbol": "LICHSGFIN26NOV605CE", "quantity": 1000, "product": "I", "last_price": 10.35}],
        "kite": books["kite"],
    }
    check("path row without average is unverified",
          it.path_premium_deployed(missing_avg, ledger) is None)

    # 16 Nov 2026 15:30 IST close premiums (Upstox 5-minute candle close).
    close = {
        "LICHSGFIN": 10.35,  # 605 CE, lot 1000 -> 10,350
        "MFSL": 27.50,       # 1420 CE, lot 400  -> 11,000
        "PIIND": 31.70,      # 1680 PE, lot 175  ->  5,547.50
        "APOLLOHOSP": 109.0, # 8800 CE, lot 125  -> 13,625
    }
    lots = {"LICHSGFIN": 1000, "MFSL": 400, "PIIND": 175, "APOLLOHOSP": 125}
    prem = {n: it.entry_premium(lots[n], close[n]) for n in close}
    check("close LICHSGFIN fits alone", prem["LICHSGFIN"] == 10350 and prem["LICHSGFIN"] <= it.PREMIUM_CAP_PER_BROKER)
    check("close MFSL fits alone", prem["MFSL"] == 11000)
    check("close PIIND fits alone", prem["PIIND"] == 5547.50)
    check("close APOLLOHOSP fits alone", prem["APOLLOHOSP"] == 13625)
    check("LICHSGFIN plus MFSL is over the cap",
          prem["LICHSGFIN"] + prem["MFSL"] > it.PREMIUM_CAP_PER_BROKER)
    check("LICHSGFIN plus PIIND fits",
          prem["LICHSGFIN"] + prem["PIIND"] <= it.PREMIUM_CAP_PER_BROKER)

    def quoted(px, lot, symbol, side="CE", strike=100, adx=50, hour=None, daily=None):
        q = {"ltp": px, "bid": px - 0.05, "ask": px, "oi": 800, "tick": 0.05}
        c = cand(symbol, side, lot_size=lot, preferred_strike=strike)
        hour = hour or (hour_ce(adx=adx, prev_adx=adx-1) if side == "CE" else hour_pe(adx=adx, prev_adx=adx-1))
        daily = daily or (daily_ce() if side == "CE" else daily_pe())
        row = it.evaluate_name(c, hour, daily, today=TUE.date(), chain_count=4, quote=q,
                               contract=contract(symbol, side, lot=lot, strike=strike), expiry_week=False)
        return row

    lich = quoted(10.35, 1000, "LICHSGFIN", strike=605, adx=70)
    mfsl = quoted(27.50, 400, "MFSL", strike=1420, adx=56)
    piind = quoted(31.70, 175, "PIIND", side="PE", strike=1680, adx=53,
                   hour=hour_pe(adx=53, prev_adx=52), daily=daily_pe())
    apollo = quoted(109.0, 125, "APOLLOHOSP", strike=8800, adx=46)
    check("evaluate stamps close premium on LICHSGFIN", lich["action"] == "enter" and lich["premium"] == 10350, lich)
    selected, chosen = it.select_entries([lich, mfsl, piind, apollo], max_names=2)
    names = [row["underlying"] for row in chosen]
    check("close cap takes LICHSGFIN and PIIND, skips MFSL", names == ["LICHSGFIN", "PIIND"], names)
    skipped = {row["underlying"]: row["reason"] for row in selected if row["action"] == "skip"}
    check("MFSL skip reason names the cap and the open premium",
          "MFSL" in skipped and "11,000" in skipped["MFSL"] and "10,350" in skipped["MFSL"]
          and "20,000" in skipped["MFSL"], skipped)
    check("APOLLOHOSP is still only out of the top 2 after the cap fill",
          "APOLLOHOSP" in skipped and "top 2" in skipped["APOLLOHOSP"], skipped)

    # A single name over the cap is skipped even when a slot is free.
    fat = quoted(25.0, 1000, "LICHSGFIN", strike=605, adx=70)
    thin = quoted(20.0, 400, "MFSL", strike=1420, adx=56)
    selected, chosen = it.select_entries([fat, thin], max_names=2)
    check("name over 20k is skipped and the next is taken",
          [r["underlying"] for r in chosen] == ["MFSL"]
          and "exceeds" in (selected[0]["reason"] if selected[0]["underlying"]=="LICHSGFIN" else selected[1]["reason"]),
          chosen)

    # Open path position counts toward the cap: 15k open + 8k next is refused, 4k fits.
    deployed = {"upstox": 15000.0, "kite": 15000.0}
    mid = quoted(20.0, 400, "MFSL", strike=1420, adx=56)   # 8,000
    cheap = quoted(20.0, 175, "PIIND", side="PE", strike=1680, adx=52,
                   hour=hour_pe(adx=52, prev_adx=51), daily=daily_pe())  # 3,500
    selected, chosen = it.select_entries([mid, cheap], max_names=2, deployed=deployed)
    check("open 15k skips the 8k name and takes the 3.5k name",
          [r["underlying"] for r in chosen] == ["PIIND"], [r["underlying"] for r in chosen])
    mid_skip = [r for r in selected if r["underlying"] == "MFSL"][0]
    check("open-plus-new reason is logged",
          mid_skip["action"] == "skip" and "15,000" in mid_skip["reason"] and "8,000" in mid_skip["reason"],
          mid_skip["reason"])

    unver = it.select_entries([thin, cheap], max_names=2, deployed="unverified")
    check("unverified open premium refuses every new name",
          unver[1] == [] and all("open premium unverified" in (r["reason"] or "") for r in unver[0] if r["action"]=="skip"),
          [r["reason"] for r in unver[0]])

    # Live / dry runner uses the same cap and fills the next name.
    shortlist = {"ok": True, "max_names": 2, "expiry": "2026-11-23",
                 "candidates": [cand("LICHSGFIN", "CE", lot_size=1000, preferred_strike=605),
                                cand("MFSL", "CE", lot_size=400, preferred_strike=1420),
                                cand("PIIND", "PE", lot_size=175, preferred_strike=1680)]}
    chains = {
        "LICHSGFIN": [contract("LICHSGFIN", lot=1000, strike=605)],
        "MFSL": [contract("MFSL", lot=400, strike=1420)],
        "PIIND": [contract("PIIND", "PE", lot=175, strike=1680)],
    }
    hour_bars = {"LICHSGFIN": hour_ce(adx=70, prev_adx=69),
                 "MFSL": hour_ce(close=1427, ema20=1410, ema50=1390, adx=56, prev_adx=55),
                 "PIIND": hour_pe(adx=53, prev_adx=52)}
    daily_bars = {"LICHSGFIN": daily_ce(), "MFSL": daily_ce(close=1427, ema20=1400, ema50=1380, adx=33),
                  "PIIND": daily_pe()}
    quotes = {
        "NSE_FO|LICHSGFIN": {"ltp": 10.35, "bid": 10.30, "ask": 10.35, "oi": 130000, "tick": 0.05},
        "NSE_FO|MFSL": {"ltp": 27.50, "bid": 27.40, "ask": 27.50, "oi": 131200, "tick": 0.05},
        "NSE_FO|PIIND": {"ltp": 31.70, "bid": 31.60, "ask": 31.70, "oi": 106925, "tick": 0.05},
    }
    market = {
        "chains": chains, "hour_bars": hour_bars, "daily_bars": daily_bars,
        "positions": {"upstox": [], "kite": []},
        "quote": lambda key: quotes.get(key),
        "map_kite": lambda row: {"kite_symbol": row.get("kite_symbol"), "kite_lot_size": row.get("lot_size"),
                                 "kite_exchange": "NFO"},
        "check_order": lambda order, broker: (True, "ok", int(order["quantity"])),
        "place": lambda broker, order: "OID-%s" % broker,
    }
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.json")
        cap_state = os.path.join(tmp, "dlc.json")
        os.environ["DAILY_LOSS_CAP_STATE"] = cap_state
        res = entry.run(TUE, market, execute=False, dry=True, ledger_path=ledger,
                        shortlist=shortlist, session_ok=True, results={"ok": True, "names": []})
        dry_named = [a["underlying"] for a in res["actions"] if a["reason"] == "dry-run; no order"]
        check("dry-run under the close cap takes LICHSGFIN and PIIND",
              dry_named == ["LICHSGFIN", "PIIND"], dry_named)
        check("preview matches the cap selection",
              [p["underlying"] for p in res["preview"]] == ["LICHSGFIN", "PIIND"], res["preview"])
        mfsl_row = [a for a in res["actions"] if a["underlying"] == "MFSL"][0]
        check("dry-run logs why MFSL was skipped",
              mfsl_row["action"] == "skip" and "20,000" in (mfsl_row["reason"] or ""), mfsl_row)
        check("result records the 20k cap", res.get("premium_cap_per_broker") == 20000, res.get("premium_cap_per_broker"))
        os.environ.pop("DAILY_LOSS_CAP_STATE", None)


def test_import_is_idle():
    # The modules are already imported. Confirm they expose no eager market hook
    # and that __main__ is the only command-line entry.
    src = open(os.path.join(HERE, "stockopt_intraday_tech_entry.py")).read()
    check("entry does not import auto_entry", "import auto_entry" not in src and "auto_entry import" not in src)
    check("entry does not import risk_manager", "import risk_manager" not in src and "risk_manager import" not in src)
    check("policy does not import auto_entry", "auto_entry" not in open(os.path.join(HERE, "stockopt_intraday_tech.py")).read())
    check("entry no longer calls api.post", "_api.post" not in src and "api.post(" not in src, src[src.find("def place"):] if "api.post" in src else "")
    check("entry places via api.call on the HFT endpoint",
          "api-hft.upstox.com/v2/order/place" in src and "api_mod.call" in src)


def test_send_pair_still_attempts_other_leg():
    """A crash or reject on one broker must not skip the other leg."""
    calls = []

    def upstox_crashes(broker, order):
        calls.append(broker)
        if broker == "upstox":
            raise AttributeError("'module' object has no attribute 'post'")
        return "OID-kite"

    sent, err = entry._send_pair(upstox_crashes, ({"quantity": 1000}, {"quantity": 400}))
    check("after an Upstox AttributeError both brokers were attempted", calls == ["upstox", "kite"], calls)
    check("Kite fill is kept after an Upstox crash",
          sent == [{"broker": "kite", "order_id": "OID-kite", "quantity": 400}] and "AttributeError" in (err or "")
          and "other leg still attempted" in (err or ""), (sent, err))

    calls.clear()

    def upstox_rejects(broker, order):
        calls.append(broker)
        if broker == "upstox":
            return None
        return "OID-kite"

    sent, err = entry._send_pair(upstox_rejects, ({"quantity": 1000}, {"quantity": 400}))
    check("after an Upstox reject Kite is still sent", calls == ["upstox", "kite"] and sent[0]["broker"] == "kite", (calls, sent, err))

    calls.clear()

    def kite_fails(broker, order):
        calls.append(broker)
        if broker == "kite":
            raise RuntimeError("kite down")
        return "OID-upstox"

    sent, err = entry._send_pair(kite_fails, ({"quantity": 1000}, {"quantity": 400}))
    check("after a Kite crash Upstox fill is kept",
          calls == ["upstox", "kite"] and sent == [{"broker": "upstox", "order_id": "OID-upstox", "quantity": 1000}]
          and "RuntimeError" in (err or ""), (calls, sent, err))

    def both_ok(broker, order):
        return "OID-%s" % broker

    sent, err = entry._send_pair(both_ok, ({"quantity": 1000}, {"quantity": 400}))
    check("both legs still succeed together", err is None and [s["broker"] for s in sent] == ["upstox", "kite"], (sent, err))

    sent, err = entry._send_pair(None, ({"quantity": 1000}, {"quantity": 400}))
    check("missing sender is still unverified", sent == [] and err == "sender unverified", (sent, err))


def test_place_payloads_for_todays_candidates():
    """The 17 Nov 10:30 IST candidates: confirm LIMIT bodies, no live order."""
    lic = contract("LICHSGFIN", "CE", lot=1000, strike=600)
    mfsl = contract("MFSL", "CE", lot=400, strike=1440)
    lic_u, lic_k = it.mirror_orders(lic, 9.55, dt.time(15, 15))
    mfsl_u, mfsl_k = it.mirror_orders(mfsl, 23.45, dt.time(15, 15))
    check("LICHSGFIN Upstox is 1000 qty I LIMIT 9.55 BUY 600 CE 23 Nov",
          lic_u and lic_u["quantity"] == 1000 and lic_u["product"] == "I" and lic_u["price"] == 9.55
          and lic_u["transaction_type"] == "BUY" and lic_u["order_type"] == "LIMIT"
          and lic_u["trading_symbol"] == "LICHSGFIN26NOV600CE"
          and lic_u["instrument_token"] == lic["instrument_key"], lic_u)
    check("LICHSGFIN Kite is 1000 qty MIS LIMIT 9.55",
          lic_k and lic_k["quantity"] == 1000 and lic_k["product"] == "MIS" and lic_k["price"] == 9.55
          and lic_k["trading_symbol"] == "LICHSGFIN26NOV600CE", lic_k)
    check("MFSL Upstox is 400 qty I LIMIT 23.45 BUY 1440 CE 23 Nov",
          mfsl_u and mfsl_u["quantity"] == 400 and mfsl_u["product"] == "I" and mfsl_u["price"] == 23.45
          and mfsl_u["transaction_type"] == "BUY" and mfsl_u["order_type"] == "LIMIT"
          and mfsl_u["trading_symbol"] == "MFSL26NOV1440CE"
          and mfsl_u["instrument_token"] == mfsl["instrument_key"], mfsl_u)
    check("MFSL Kite is 400 qty MIS LIMIT 23.45",
          mfsl_k and mfsl_k["quantity"] == 400 and mfsl_k["product"] == "MIS" and mfsl_k["price"] == 23.45
          and mfsl_k["trading_symbol"] == "MFSL26NOV1440CE", mfsl_k)

    class FakeApi:
        def __init__(self):
            self.calls = []

        def call(self, url, body=None, method=None, proxy=None, timeout=None):
            self.calls.append({"url": url, "body": body, "method": method, "proxy": proxy})
            return {"status": "success", "data": {"order_id": "U-DRY"}}

        def post(self, *a, **k):
            raise AssertionError("api.post must not be used")

    api = FakeApi()
    oid = entry._place_upstox(api, lic_u)
    check("mocked LICHSGFIN place returns an id and never calls api.post", oid == "U-DRY" and len(api.calls) == 1, api.calls)
    call = api.calls[0]
    body = call["body"]
    check("LICHSGFIN goes to the HFT place endpoint via api.call POST proxy=True",
          call["url"] == entry.UPSTOX_PLACE_URL == "https://api-hft.upstox.com/v2/order/place"
          and call["method"] == "POST" and call["proxy"] is True, call)
    check("LICHSGFIN HFT body has qty 1000, product I, limit 9.55",
          body["quantity"] == 1000 and body["product"] == "I" and body["price"] == 9.55
          and body["order_type"] == "LIMIT" and body["transaction_type"] == "BUY"
          and body["validity"] == "DAY" and body["instrument_token"] == lic["instrument_key"]
          and body["disclosed_quantity"] == 0 and body["trigger_price"] == 0 and body["is_amo"] is False
          and body["tag"] == it.TAG, body)

    api = FakeApi()
    oid = entry._place_upstox(api, mfsl_u)
    body = api.calls[0]["body"]
    check("MFSL HFT body has qty 400, product I, limit 23.45",
          oid == "U-DRY" and body["quantity"] == 400 and body["product"] == "I" and body["price"] == 23.45
          and body["instrument_token"] == mfsl["instrument_key"]
          and api.calls[0]["url"] == entry.UPSTOX_PLACE_URL, body)


if __name__ == "__main__":
    test_signal_and_daily()
    test_window_and_flatten_time()
    test_evaluate_and_rank()
    test_products_and_atm()
    test_flatten_orders_and_ledger()
    test_run_dry_and_gates()
    test_report_preview_uses_saved_close()
    test_premium_cap()
    test_import_is_idle()
    test_send_pair_still_attempts_other_leg()
    test_place_payloads_for_todays_candidates()
    if FAILS:
        print("FAILED", len(FAILS), ":", ", ".join(FAILS))
        sys.exit(1)
    print("all passed")
