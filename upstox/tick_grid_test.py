"""11 Nov 2026: contract tick grid for stops, and the Kite GTT exchange for MCX.
risk_manager.py runs at import, so its helpers are lifted with ast and never imported."""
import ast, os, sys
D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D); sys.path.insert(0, os.path.join(D, "equity"))
src = open(os.path.join(D, "risk_manager.py")).read()
ns = {}
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name in ("tick", "tick_size_for", "tick_on"):
        exec(compile(ast.Module([node], []), "risk_manager.py", "exec"), ns)
ok = 0; bad = 0
def check(name, got, want):
    global ok, bad
    if abs(float(got) - float(want)) < 1e-9: ok += 1
    else: bad += 1; print("FAIL", name, got, "!=", want)
tick, tick_on = ns["tick"], ns["tick_on"]
# 0.05 grid is unchanged for options
for p in (123.47, 99.99, 0.07, 452.123):
    check(f"opt {p}", tick_on(p, 0.05), tick(p))
check("unknown tick falls back", tick_on(24966.73, 0), tick(24966.73))
# The 11 Nov reject: 24966.75 is off the NIFTY FUT 0.10 grid
v = tick_on(24966.75, 0.10); check("nifty fut grid", round(v / 0.10) * 0.10, v)
check("nifty fut value", v in (24966.7, 24966.8), True)
v = tick_on(59123.37, 0.20); check("banknifty fut grid", abs(v / 0.2 - round(v / 0.2)) < 1e-6, True)
v = tick_on(203.55, 0.10); check("crude opt grid", v in (203.5, 203.6), True)
check("crude fut", tick_on(8195.4, 1.0), 8195.0)
# master lookup (no network needed when today's master is already on disk)
tsf = ns["tick_size_for"]
check("bogus key -> default", tsf("NSE_FO|0000000"), 0.05)
import gtt_overnight as g
check("mcx exchange", g.kite_exchange({"book": "Option", "sym": "CRUDEOIL26NOV8200PE", "exchange": "MCX"}) == "MCX", True)
check("sensex fallback", g.kite_exchange({"book": "Option", "sym": "SENSEX26N1982400CE"}) == "BFO", True)
check("nfo fallback", g.kite_exchange({"book": "Option", "sym": "NIFTY26N1725000CE"}) == "NFO", True)
check("mtf nse", g.kite_exchange({"book": "MTF", "sym": "SRF", "exchange": "NSE"}) == "NSE", True)
check("eq tick fallback", g.contract_tick({"broker": "kite", "kind": "eq", "book": "MTF", "sym": "SRF"}, 2385.1), 0.10)
check("unknown contract fallback", g.contract_tick({"broker": "kite", "kind": "opt", "book": "Option", "sym": "NOSUCH26NOV1CE", "exchange": "NFO"}, 50), 0.05)
print(f"tick_grid_test: {ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
