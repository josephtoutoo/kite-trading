# Post-market review: Mon 23 Nov 2026 (IST)
Trading day (market_calendar.is_trading_day → True). Read-only Upstox dump at ~16:20 IST: 17 orders / 17 trades / 4 positions / 2 holdings / 2 GTTs. **Upstox only**: since 23 Nov 10:24 IST the user wants no Kite monitoring or reporting, and Kite trading is stopped (kite_disabled.json armed), so no Kite API reads were made for this review and no Kite data is reported. No order, GTT or routine was touched.
Files: /workspace/perf_review_20261123/ (raw_today.json, tests_*.txt). Backtest: /workspace/backtests/postmarket_20261123/ (bt_23.py, bt_23_robust.py, bt_23_adx1h.py, results_23*.txt; spot 3m/15m refreshed to 23 Nov 15:27).

## Context
- Nov monthly NIFTY/BANKNIFTY and NIFTY weekly expiry today (24 Nov NSE holiday).
- ~10:59 IST user asked for no Upstox stop sell orders for the rest of the day; the stop manager, MTF trail and overnight GTT routines were paused (resume set for 25 Nov). Stop behaviour after that is per instruction, not an algo fault. Both Upstox GTTs (SBIN 1150CE 41.2, BANKNIFTY 63600CE 1871.55) show CANCELLED, so the 30-lot-qty BANKNIFTY 26DEC 63600CE carry and the 1,000 INDHOTEL 26DEC 920CE have no overnight stop by user choice.
- Daily loss cap (₹15,000 / 2 losing trades) tripped at 09:31:26 IST (3 losing closed trades, day realised −₹14,457 on the cap's mark-to-previous-close basis) and blocked every new bot entry for the day. Caps and the R2b ₹30,000 breaker unchanged.

## Upstox books (broker fields: realised / unrealised)
- Index options: BANKNIFTY 26DEC 63600CE (carried 30 from 20 Nov, untagged buys 90+30+60, exits target_book 30 @1669.3 09:29, 90 @1548.4 09:51, 30 @1472.6 10:49, untagged market 30 @1084.85 15:12): realised +4,505.9, 30 open unrealised −11,084.3 (LTP 1084.15). BANKNIFTY 26DECFUT: untagged buy 30 @63725.4 10:48, market sell @62960.2 15:12: realised −22,956. No bot index entry (all blocked, see below).
- Stock options: SBIN 26DEC 1150CE 750 sold by trailsl 09:25 @40.6 (avg 34.35): realised +4,687.5. INDHOTEL 26DEC 920CE untagged buys 1,000 each @27 / @26 / @25, sold 2,000 @23.7 at 15:12: realised −4,600, 1,000 open unrealised −1,500. No ST/EMA or intraday-tech bot entry (cap blocked; outside 10:30–14:30 window; "Supertrend not beyond EMA20"; intraday path skips expiry-week near-month).
- MTF: no Upstox MTF trade. Holdings TEMPSENS ×1, JPL ×12 unchanged.
- MCX crude: no Upstox crude trade.
- Upstox realised total across rows: −18,362.6 (broker figures; untagged trades are not bot orders).

## Algo behaviour (753 decision rows, 23 Nov)
- Fresh signals: BANKNIFTY + SENSEX 15m PE (09:15 bar), NIFTY 15m PE (09:45), NIFTY 3m PE (12:03), BANKNIFTY + SENSEX 1h PE (12:15), NIFTY 1h PE (13:15); 1h CE continuation 10:15/11:15 and SENSEX 3m CE continuation 11:24–11:36.
- Outcomes: every routed signal refused by the daily loss cap (3→6 losing trades as the day went on), plus the expiry rule (NIFTY/BANKNIFTY 23 NOV contracts) and one "route already holds BANKNIFTY26DEC63600CE" refusal. No bot index entry on Upstox.
- Backtest replay of today's live-faithful signals (1 lot, premium proxy): 3m SENSEX CE −1,870; 15m NIFTY PE +705, BANKNIFTY PE −319, SENSEX PE +806; 1h BANKNIFTY PE +1,851 ⇒ about +1,173 missed in total. The cap's block cost little.

## Backtest (premium proxy, 1 lot, costs; IS Jan–Jun, OOS 1 Jul–23 Nov)
| Route | Variant | IS net (n) | OOS net (n) |
|---|---|---|---|
| 3m | LIVE_skip (current) | −44,160 (234) | −20,198 (193) |
| 3m | LIVE_skip + cut 13:00 | −47,155 | −15,617 |
| 3m | LIVE_skip + ADX25 | −63,882 | −16,707 |
| 3m | ROLL | −104,341 | −38,478 |
| 15m | LIVE_skip (current) | −222,569 (318) | −46,324 (230) |
| 15m | start 10:00 (old proxy) | +1,839 | −10,762 |
| 15m | NOCONT | +5,372 | −48,028 |
| 1h | LIVE_skip (current) | −384,628 (219) | +19,386 (149) |
| 1h | NO_2C | −292,129 | +33,249 |
| 1h | LIVE_skip + ADX22 / 25 / 28 | −319,787 / −314,041 / −262,392 | +51,629 / +21,088 / +75,132 |
New candidate today: 1h ADX gate (the only variant better than current in both periods). Rejected as overfit: OOS gain comes entirely from Nov 2026 (ADX28 Nov +166,133; Jul–Oct all negative, and Jul/Sep worse than current), IS remains deeply negative, and ADX25 is worse than current in Mar/Jun. 3m cut13/ADX25 are worse IS and better OOS (inconsistent, as on 18–20 Nov). 15m start10:00 and 1h NO_2C fail the same April / gap-day / BANKNIFTY robustness checks again (results_23_robust.txt).
**Nothing adopted.** No signal, size, stop, cap, breaker or book-weight change.

## Tests (pass/total)
stage 30/37, live 27/37. Same failures as 20 Nov (autonomy, full_auto_sim, capital_policy, stockopt_policy, mtf_policy, stockopt_st_ema, auto_entry_signal; live also index_contract_policy needing .git). New in live only: stockopt_intraday_tech_test and stockopt_spread_exit_test fail because the runtime kite_disabled.json is armed and those tests expect a Kite leg; stage (no switch file) passes them with identical code. Left as is (the switch is a safety guard; not overridden in tests). Kite mirror tests not run (no code change).
