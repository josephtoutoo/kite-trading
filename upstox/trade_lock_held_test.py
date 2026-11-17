#!/usr/bin/env python3
"""17 Nov 2026: trade_lock.held_rows must not match tokenless rows by None==None. No network, no orders."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade_lock as tl
bad = 0
def check(name, cond, detail=None):
    global bad
    if not cond:
        bad += 1; print("FAIL", name, detail)
# Kite: holdings carry SRF MTF 6 (token); _kite_mtf rows carry SRF 6 with no token.
pos = [{"trading_symbol": "SRF", "instrument_token": "KITE|NSE|SRF", "quantity": 6, "product": "MTF", "average_price": 2400}]
mtf = [{"trading_symbol": "SRF", "quantity": 6, "average_price": 2400, "product": "MTF"}]
_a, held = tl.held_rows(pos, mtf, None, "IGIL", "kite")
check("tokenless IGIL order does not match SRF rows", held == [], held)
_a, held = tl.held_rows(pos, mtf, "KITE|NSE|IGIL", "IGIL", "kite")
check("tokened IGIL order holds nothing", held == [], held)
_a, held = tl.held_rows(pos, mtf, "KITE|NSE|SRF", "SRF", "kite")
check("SRF counted once on Kite (holding + MTF row)", sum(int(p["quantity"]) for p in held) == 6, held)
# Upstox: an MTF row with its own token is still added when short-term positions do not carry it.
upos = [{"trading_symbol": "NIFTY26NOV25400CE", "instrument_token": "NSE_FO|64462", "quantity": 130, "product": "D"}]
umtf = [{"trading_symbol": "SRF-EQ", "instrument_token": "NSE_EQ|INE647A01010", "quantity": 6, "product": "MTF"}]
allp, held = tl.held_rows(upos, umtf, "NSE_EQ|INE647A01010", "SRF-EQ", "upstox")
check("upstox MTF row kept and matched", len(allp) == 2 and sum(int(p["quantity"]) for p in held) == 6, held)
_a, held = tl.held_rows(upos, [{"trading_symbol": "X", "quantity": 5}], None, "", "upstox")
check("empty symbol and no token match nothing", held == [], held)
print("trade_lock_held_test: %s" % ("PASS" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
