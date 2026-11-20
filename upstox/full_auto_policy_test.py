#!/usr/bin/env python3
"""Synthetic tests for full_auto_policy (12 Nov 2026). No broker calls, no orders."""
import datetime, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import full_auto_policy as f

fails = 0
def check(name, cond):
    global fails
    print(("ok    " if cond else "FAIL  ") + name)
    if not cond: fails += 1

UP = {"close": 110, "ema20": 100, "dir": 1}
DN = {"close": 90, "ema20": 100, "dir": -1}

# flags
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as t:
    json.dump({"adds": True, "trend_exit": False}, t)
check("flag file true is on", f.flag("adds", env={}, flags_file=t.name))
check("flag file false is off", not f.flag("trend_exit", env={}, flags_file=t.name))
check("missing key is off", not f.flag("crude_entry", env={}, flags_file=t.name))
check("env 0 overrides file", not f.flag("adds", env={"FULL_AUTO_ADDS": "0"}, flags_file=t.name))
check("env 1 overrides file", f.flag("trend_exit", env={"FULL_AUTO_TREND_EXIT": "1"}, flags_file=t.name))
check("unreadable file is off", not f.flag("adds", env={}, flags_file="/nonexistent.json"))
# capital_gate (t598u 12 Nov 2026): a safety gate, ON unless explicitly off
check("capital_gate missing key is ON", f.flag("capital_gate", env={}, flags_file=t.name))
check("capital_gate unreadable file is ON", f.flag("capital_gate", env={}, flags_file="/nonexistent.json"))
check("capital_gate odd env value stays ON", f.flag("capital_gate", env={"FULL_AUTO_CAPITAL_GATE": "no"}, flags_file="/nonexistent.json"))
check("capital_gate env 0 is OFF", not f.flag("capital_gate", env={"FULL_AUTO_CAPITAL_GATE": "0"}, flags_file="/nonexistent.json"))
_cg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump({"capital_gate": False}, _cg); _cg.close()
check("capital_gate file false is OFF", not f.flag("capital_gate", env={}, flags_file=_cg.name))
check("capital_gate env 1 beats file false", f.flag("capital_gate", env={"FULL_AUTO_CAPITAL_GATE": "1"}, flags_file=_cg.name))
os.unlink(_cg.name)
# r10_mtf_capital (t713u/t714u 17 Nov 2026): safety gate, ON unless explicitly off; live file is off
check("r10_mtf_capital missing key is ON", f.flag("r10_mtf_capital", env={}, flags_file=t.name))
check("r10_mtf_capital unreadable file is ON", f.flag("r10_mtf_capital", env={}, flags_file="/nonexistent.json"))
check("r10_mtf_capital odd env value stays ON", f.flag("r10_mtf_capital", env={"FULL_AUTO_R10_MTF_CAPITAL": "no"}, flags_file="/nonexistent.json"))
check("r10_mtf_capital env 0 is OFF", not f.flag("r10_mtf_capital", env={"FULL_AUTO_R10_MTF_CAPITAL": "0"}, flags_file="/nonexistent.json"))
_mg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump({"r10_mtf_capital": False}, _mg); _mg.close()
check("r10_mtf_capital file false is OFF", not f.flag("r10_mtf_capital", env={}, flags_file=_mg.name))
check("r10_mtf_capital env 1 beats file false", f.flag("r10_mtf_capital", env={"FULL_AUTO_R10_MTF_CAPITAL": "1"}, flags_file=_mg.name))
os.unlink(_mg.name)
# index_r1_r7_off (t716u 17 Nov 2026): defaults OFF; on only with a until date on or after the decision date
_when = datetime.datetime(2026, 11, 17, 12, 30)
_next = datetime.datetime(2026, 11, 18, 9, 0)
check("index_r1_r7_off missing file is OFF", not f.index_r1_r7_off(_when, env={}, flags_file="/nonexistent.json"))
_no_until = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump({"index_r1_r7_off": True}, _no_until); _no_until.close()
check("index_r1_r7_off flag true without until is OFF", not f.index_r1_r7_off(_when, env={}, flags_file=_no_until.name))
os.unlink(_no_until.name)
_r17 = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"index_r1_r7_off": True, "index_r1_r7_off_until": "2026-11-17"}, _r17); _r17.close()
check("index_r1_r7_off on 17 Nov with until 17 Nov is ON", f.index_r1_r7_off(_when, env={}, flags_file=_r17.name))
check("index_r1_r7_off on 18 Nov 09:00 is already OFF", not f.index_r1_r7_off(_next, env={}, flags_file=_r17.name))
check("index_r1_r7_off env 0 beats file true", not f.index_r1_r7_off(_when, env={"FULL_AUTO_INDEX_R1_R7_OFF": "0"}, flags_file=_r17.name))
check("index_r1_r7_off env 1 + until works without a file", f.index_r1_r7_off(_when, env={"FULL_AUTO_INDEX_R1_R7_OFF": "1", "FULL_AUTO_INDEX_R1_R7_OFF_UNTIL": "2026-11-17"}, flags_file="/nonexistent.json"))
os.unlink(_r17.name)
_live = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "full_auto_flags.json")))
check("live flags: capital_gate false (t598u), r10_mtf_capital false (t713u/t714u), index_r1_r7_off true until 2026-11-17 (t716u), crude_entry still false",
      _live.get("capital_gate") is False and _live.get("r10_mtf_capital") is False
      and _live.get("index_r1_r7_off") is True and _live.get("index_r1_r7_off_until") == "2026-11-17"
      and _live.get("crude_entry") is False)
os.unlink(t.name)

# 1h flip exit
s = {}
check("CE with green 1h: hold", f.trend_exit_1h("CE", {"dir": 1, "ts": "a"}, s) == (False, "1h Supertrend still with the position"))
check("seen aligned recorded", s.get("h1_seen_aligned") is True)
check("CE flip to red after aligned: exit", f.trend_exit_1h("CE", {"dir": -1, "ts": "b"}, s)[0] is True)
s2 = {}
check("never aligned: no exit on first look", f.trend_exit_1h("PE", {"dir": 1}, s2)[0] is False)
check("PE flip to green after aligned: exit", f.trend_exit_1h("PE", {"dir": 1}, {"h1_seen_aligned": True})[0] is True)
check("forming bar: no exit", f.trend_exit_1h("CE", {"dir": -1, "forming": True}, {"h1_seen_aligned": True})[0] is False)
check("missing bar: no exit", f.trend_exit_1h("CE", None, {"h1_seen_aligned": True})[0] is False)

# ladder: upward only
check("no ladder below +15%", f.ladder_floor(100, 114) == 0)
check("breakeven at +15%", f.ladder_floor(100, 115) == 100)
check("first roll keeps entry", f.ladder_floor(100, 125) == 100)
check("second roll lifts to 120", abs(f.ladder_floor(100, 141) - 120) < 1e-9)
check("ratchet never below existing", f.ratchet(150, 120, 100) == 150)
check("ratchet never below disaster", f.ratchet(None, None, 100) == 70)

# oversell guard
check("sell capped to broker qty", f.capped_sell(150, 75) == 75)
check("broker shows 0: no sell", f.capped_sell(75, 0) == 0)
check("broker unreadable(None): no sell from helper", f.capped_sell(75, None) == 0)
check("normal sell unchanged", f.capped_sell(75, 75) == 75)

# adds
base = dict(side="CE", avg=100, ltp=110, held_qty=75, lot_size=75, adds_used=0, bar_1h=UP, live_stop_trigger=100, days_to_expiry=6)
d = f.add_decision(**base)
check("eligible add is 1 lot, same as entry", d["ok"] and d["lots"] == 1 and d["qty"] == 75)
check("2-lot entry adds 1 (3-lot cap)", f.add_decision(**{**base, "held_qty": 150})["lots"] == 1)
check("3 lots held: no add", not f.add_decision(**{**base, "held_qty": 225})["ok"])
check("second add refused", not f.add_decision(**{**base, "adds_used": 1})["ok"])
check("loss: no add", not f.add_decision(**{**base, "ltp": 99})["ok"])
check("past +20%: no add (R6)", not f.add_decision(**{**base, "ltp": 121})["ok"])
check("1h not aligned: no add", not f.add_decision(**{**base, "bar_1h": DN})["ok"])
check("PE needs down 1h", f.add_decision(**{**base, "side": "PE", "bar_1h": DN})["ok"])
check("stop below breakeven: no add", not f.add_decision(**{**base, "live_stop_trigger": 99.5})["ok"])
check("no live stop: no add", not f.add_decision(**{**base, "live_stop_trigger": None})["ok"])
check("expiry within 2 days: no add", not f.add_decision(**{**base, "days_to_expiry": 1})["ok"])
check("odd quantity: no add", not f.add_decision(**{**base, "held_qty": 80})["ok"])

# crude
check("fresh CE", f.crude_fresh_signal(UP, {"close": 99, "ema20": 100, "dir": 1}) == "CE")
check("already aligned is not fresh", f.crude_fresh_signal(UP, UP) is None)
check("fresh PE", f.crude_fresh_signal(DN, UP) == "PE")
check("tf default 1h", f.parse_crude_tf(env={}) == "1h" and f.parse_crude_tf(raw="") == "1h")
check("tf 15m", f.parse_crude_tf(env={"CRUDE_TF": "15m"}) == "15m" and f.parse_crude_tf(raw="15M") == "15m")
check("tf 1h explicit", f.parse_crude_tf(raw="1h") == "1h")
check("tf invalid is None", f.parse_crude_tf(raw="5m") is None and f.parse_crude_tf(raw="15") is None)
check("bar used is per timeframe", f.crude_bar_used([{"bar": "T", "tf": "1h"}], "T", "1h") and not f.crude_bar_used([{"bar": "T", "tf": "1h"}], "T", "15m"))
check("legacy ledger row is 1h", f.crude_bar_used([{"bar": "T"}], "T", "1h") and not f.crude_bar_used([{"bar": "T"}], "T", "15m"))
today = datetime.date(2026, 11, 12)
exps = {datetime.date(2026, 11, 17), datetime.date(2026, 12, 16)}
check("expiry inside 7 days skipped", f.pick_crude_expiry(exps, today) == datetime.date(2026, 12, 16))
check("8 days ok", f.pick_crude_expiry({datetime.date(2026, 11, 20)}, today) == datetime.date(2026, 11, 20))
check("ATM 50 step", f.atm(8183) == 8200 and f.atm(8174) == 8150)
check("liquid quote", f.liquid(100, 101, 100.5)[0])
check("wide spread refused", not f.liquid(100, 110, 105)[0])
check("one-sided refused", not f.liquid(0, 101, 100)[0])

# capital policy override and crude cap constants
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "equity"))
import capital_policy as cp
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as t:
    json.dump({"upstox": 500000, "kite": 0, "confirmed_by": "t999u"}, t)
check("verified file gives verified basis", cp.basis_from_funds(cp.verified_override("upstox", t.name))["capital"] == 500000)
check("zero is not verified", cp.verified_override("kite", t.name) is None)
with open(t.name, "w") as g: json.dump({"upstox": 500000}, g)
check("unconfirmed file ignored", cp.verified_override("upstox", t.name) is None)
os.unlink(t.name)
check("missing file keeps refusal", cp.verified_override("upstox", "/nonexistent.json") is None)
check("crude cap is 1 lot", cp.CRUDE_AUTO_MAX_LOTS == 1 and cp.CRUDE_AUTO_BOOK == "crude_auto")

# source wiring (no import of risk_manager / auto_entry)
root = os.path.dirname(os.path.abspath(__file__))
rm = open(os.path.join(root, "risk_manager.py")).read()
check("risk_manager: 1h exit gated on the flag", "if FA_TREND and not fut:" in rm and "fap.trend_exit_1h" in rm)
check("risk_manager: 5m exit kept only when flag off", rm.count("ok,why=trend_ok(sym) if (not fut and INDEX_OPT)") == 2)
check("risk_manager: every exit re-reads broker qty", rm.count("_live=upstox_live_qty(k)") == 2 and rm.count("_live=kite_live_qty(bk,sym)") == 2)
check("risk_manager: ladder only under the flag", rm.count("if FA_LADDER and not fut: floor=max(floor,fap.ladder_floor(") == 2)
tl = open(os.path.join(root, "trade_lock.py")).read()
check("trade_lock: crude_auto is Kite-only and 1 lot", 'broker!="kite"' in tl and "CRUDE_AUTO_MAX_LOTS" in tl)
ca = open(os.path.join(root, "crude_auto.py")).read()
check("crude_auto: check_order before place", ca.find("trade_lock.check_order(order") < ca.find('bk.place("MCX", m["tradingsymbol"], "BUY"'))
check("crude_auto: 15m path uses bar_15m", "faf.bar_15m(fk) if tf == \"15m\" else faf.bar_1h(fk)" in ca)
check("crude_auto: ledger tags timeframe", '"tf": tf' in ca)
check("crude_auto: shared daily cap unchanged", "CRUDE_MAX_ENTRIES_PER_DAY" in ca)
feed = open(os.path.join(root, "full_auto_feed.py")).read()
check("feed: bar_1h still hours/1/20", 'def bar_1h(key):' in feed and '_completed_bar(key, "hours", 1, 20)' in feed)
check("feed: bar_15m is minutes/15/20", 'def bar_15m(key):' in feed and '_completed_bar(key, "minutes", 15, 20)' in feed)
fa = open(os.path.join(root, "full_auto_add.py")).read()
check("full_auto_add: check_order before place", fa.find("trade_lock.check_order(order") < fa.find("order/place"))
check("full_auto_add: ledger written before the order", fa.find('"state": "submitting"') < fa.find("order/place"))
print("%d failed" % fails)
sys.exit(1 if fails else 0)
