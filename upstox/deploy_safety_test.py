#!/usr/bin/env python3
"""Compile the deploy set without importing scripts that trade or read tokens.

stop_run.py is compiled only. Importing it reads token.json. auto_entry.py and
risk_manager.py are compiled only: importing them runs the routine.
"""
import os, py_compile, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import index_size_policy as size

COMPILE = [
    "auto_entry.py", "risk_manager.py", "option_monitor.py", "trade_lock.py", "api.py",
    "ind.py", "sl_confirm.py", "broker_kite.py", "kite_disabled.py", "market_calendar.py",
    "equity/upstox_mtf_stops.py", "equity/kite_mtf_stops.py", "equity/gtt_overnight.py",
    "equity/stop_run.py", "equity/mtf_dual_entry.py", "equity/mtf_policy.py",
    "equity/mtf_sizing.py", "equity/capital_policy.py", "equity/stockopt_tech_entry.py",
    "equity/stockopt_add.py", "equity/stockopt_results_exit.py", "equity/autonomy.py",
    "equity/book_sizing.py", "equity/stockopt_st_ema.py", "equity/stockopt_st_ema_entry.py",
    "equity/stockopt_intraday_tech.py", "equity/stockopt_intraday_tech_entry.py",
    "full_auto_policy.py", "full_auto_feed.py", "full_auto_add.py", "crude_auto.py",
    "position_life.py", "perf_rules.py",
]
DO_NOT_IMPORT = {"equity/stop_run.py", "auto_entry.py", "risk_manager.py", "full_auto_add.py", "crude_auto.py"}


def main():
    for rel in COMPILE:
        py_compile.compile(os.path.join(HERE, rel), doraise=True)
    assert "equity/stop_run.py" in DO_NOT_IMPORT
    src = open(os.path.join(HERE, "auto_entry.py")).read()
    assert src.find('r["fresh_entry"]="fail_closed"') < src.find("order/place")
    assert "INDEX_REVERSAL_CONFIRM" in src and 'os.environ.get("INDEX_REVERSAL_CONFIRM","")=="1"' in src
    # 30-day 1h/15m indicator window: named constant, used only on the resample path
    assert "INDICATOR_HISTORY_DAYS=30" in src or "INDICATOR_HISTORY_DAYS = 30" in src
    assert "INDICATOR_HISTORY_DAYS if CONTRACT_POLICY" in src
    assert "ind.candles(signal_key,\"minutes\",5,INDICATOR_HISTORY_DAYS)" in src or "INDICATOR_HISTORY_DAYS)" in src
    assert "icp.resample(futures_5m," in src
    # 5m paths and the trade_lock R4 5m check stay at 5 days
    assert 'a=ind.analyze(signal_key,"minutes",5,5)' in src
    assert 'cs=ind.candles(signal_key,"minutes",5,5)' in src
    assert 'sa=ind.analyze(cfg["key"],"minutes",5,5)' in src
    tl = open(os.path.join(HERE, "trade_lock.py")).read()
    assert 'a=ind.analyze(_key,"minutes",5,5)' in tl
    assert size.SENDER_ENABLED is False
    rm = open(os.path.join(HERE, "risk_manager.py")).read()
    assert 'os.environ.get("TREND_STOP_POLICY", "") != "1"' in rm
    assert "avg*(1-TRAIL)" in rm
    # 12 Nov 2026 full auto: the 1h exit and the ladder only run behind the flag
    assert "FA_TREND=fap.flag(\"trend_exit\")" in rm and rm.count("if FA_TREND and not fut") >= 2 and rm.count("if FA_LADDER and not fut") >= 4
    kd = open(os.path.join(HERE, "kite_disabled.py")).read()
    assert "kite disabled by user 21 Nov" in kd
    bk = open(os.path.join(HERE, "broker_kite.py")).read()
    assert "import kite_disabled" in bk and "_blocked(" in bk
    tl = open(os.path.join(HERE, "trade_lock.py")).read()
    assert 'if broker=="kite":' in tl and "kite_disabled" in tl
    print("deploy safety checks passed (compile only; stop_run was not imported)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
