# Post-market review: Fri 20 Nov 2026 (IST)
Trading day (market_calendar.is_trading_day → True). Read-only broker dumps at ~16:15 IST: Upstox 27 orders / 15 trades / 7 positions + 2 GTTs; Kite 44 orders / 37 trades, positions, holdings, 6 GTTs. No order, GTT or routine was touched.
Files: /workspace/perf_review_20261120/ (raw_today.json, raw_gtt.json, tests_*.txt). Backtest: /workspace/backtests/postmarket_20261120/ (bt20.py, bt_20.py, bt_20_robust.py, results_20*.txt; spot 3m/15m refreshed to 20 Nov 15:27).

## Context
09:49 user t761u/t762u re-enabled full auto trading + stops on both brokers (R15 Upstox block lifted, no-stop list emptied). Upstox one-time best-2 Dec stock-option buys 09:28. Friday 15:05 index cutoff. Nov monthly/weekly NIFTY+BANKNIFTY expiry is Mon 23 Nov (24 Nov holiday).

## Books (broker figures; Upstox `pnl` = realised + unrealised; Kite `pnl` vs avg, `m2m` vs prev close)
- Index options, Upstox: bot SENSEX 26NOV 83200CE 1h +8,890, SENSEX 26NOV 83400CE 3m MIS +8,308.8, SENSEX 03DEC 83500CE 15m +4,393 (all closed; 3m/1h via target_book + cw_nw_close 14:29). BANKNIFTY 26DEC 63600CE (manual, carried): realised +6,086.4, 30 open unrealised +26,388.6.
- Index options, Kite: same three bot SENSEX trades +8,962 / +8,448 / +4,400. NIFTY 01DEC 25500CE (carried 130 + manual/tfc_tv round trips, flat) pnl +29,864.2. BANKNIFTY 26DEC 62500CE (carried 60, 30 sold 15:00 user_close) pnl +82,102.5 / m2m +62,515.5, 30 open.
- Stock options, Upstox: BANKINDIA 160CE (best2) +14,352 realised; SBIN 1150CE (best2) 750 open +7,462.5; INDHOTEL 900CE 2,000 sold +16,240 realised.
- Stock options, Kite: APOLLOHOSP 8800CE sold 09:16 +6,518.8 (m2m +7,993.8); PNB 150CE manual 09:27 → user_close 15:04 +8,320; ITC 300CE user_close −1,207.5 (m2m −1,897.5); INDHOTEL 910CE 1,000 bought 15:30 (untagged) −100 open. No ST/EMA or intraday-tech bot entry.
- MTF: Kite SRF ×6 held (day +5.4); mtfsl cancelled by exchange 15:15 (3rd day); overnight GTT 345826303 placed 16:12. Upstox none.
- MCX crude: Kite CRUDEOIL 26DEC 7800PE 2 lots carried, sold 09:31 @437.5 and 15:43 @577.5 (user_close); pnl +14,140 (m2m +18,120).
- Kite CNC manual: ACUTAAS/SWIGGY/GOLDBEES/TITAN ≈ +86.
- Daily loss cap not tripped (no losing bot index trade).

## Algo behaviour (803 decision rows)
- Bot entries: SENSEX 1h CE 10:46, 3m CE 12:00, 15m CE (03 DEC) 12:17 on both brokers. All three hit target_book / cw_nw_close; each broker +21.6–21.8k.
- BANKNIFTY 15m (62×) and 3m (24×), NIFTY 3m (21×) refused "expires 2026-11-23 (within 1 trading day)". NIFTY 15m refused 46× by R1 (manual tfc_tv NIFTY trades used the 3-per-index decision cap) + R7/R12 cooldowns.
- **Bug: every Upstox bot entry logged SL_CONFIRM_FAILED** ("entry not filled (no position); SL not live"; detail "R8: no live long … SELL would open a short"). Two causes in sl_confirm.guard_upstox: (1) trade_lock caches broker reads for the whole process, and auto_entry runs the BUY check and then the SL check in one process, so R8 saw the pre-entry book and refused the stop; (2) auto_entry passes the option-master name ("SENSEX 83200 CE 26 NOV 26") but the Upstox book uses "SENSEX26NOV83200CE", so position/SL matching never hit. risk_manager placed the stops ~2 min later (10:48:42, 12:02:00, 12:17:34). The 17 Nov records are the same bug. Kite path unaffected.

## Ops notes (not changed)
- Upstox BANKINDIA: trailsl SL sent 12:53:23, 1 s after the stockopt_book exit, rejected for margin (would have been a short). Harmless, but a stale-position race in risk_manager.
- Manual exits while a bot SL was pending were margin-rejected again (Kite NIFTY 10:39, CRUDE 11:21).
- Kite INDHOTEL 910CE bought 15:30:00 untagged (after the Friday cutoff); GTT stop 345821432 (trigger 26) placed 15:34:25, so about 4 min with no stop.
- Kite GTT 345670269 is a BUY INDHOTEL 900CE 1,000 @24 (created 09:12) with no position. Flag for the user.
- Overnight stops: Upstox GTTs on SBIN 1150CE (41.2) and BANKNIFTY 63600CE (1871.55); Kite GTTs on BANKNIFTY 62500CE (2406.3), INDHOTEL 910CE (26), SRF (2467.1).
- sl_confirm_failures.json still holds 8 stale unresolved Upstox records (17 + 20 Nov) that never clear (symbol mismatch). Left as they are.

## Backtest (premium proxy, 1 lot, costs; IS Jan–Jun, OOS 1 Jul–20 Nov)
New: bt20 models the live t669u rule (no contract expiring within 1 trading day). The old bt never did.
| Route | Variant | IS net (n) | OOS net (n) |
|---|---|---|---|
| 3m | old CUR (no expiry rule) | −96,236 (344) | −28,199 (279) |
| 3m | LIVE_skip (actual live) | −44,160 (234) | −18,328 (192) |
| 3m | ROLL to next expiry | −104,341 (rolled 110: −60,181) | −34,687 (rolled 87: −16,359) |
| 3m | LIVE_skip + cut 13:00 | −47,155 | −13,747 |
| 3m | LIVE_skip + ADX25 | −63,882 | −16,707 |
| 15m | CUR / LIVE_skip / ROLL | −219,258 / −222,569 / −219,210 | −43,572 / −47,516 / −43,686 (1 rolled trade per period) |
| 15m | start 10:00 | +1,839 | −12,273 |
| 1h | CUR / NO_2C | −374,403 / −292,129 | +646 / +17,665 |
The expiry rule helps 3m (both periods better than no rule). Rolling instead of skipping is worse IS and OOS. 15m/1h rolls happen about once a period, which is too few to judge. cut13 under the live baseline is worse IS and better OOS: inconsistent. start10:00 and NO_2C fail the same robustness checks as on 18/19 Nov (April / gap-day dependence, BANKNIFTY worse).
**Nothing adopted.** No signal, size, stop, cap or breaker change.

## Code change (ops only)
sl_confirm.py: `_fresh_trade_lock()` drops trade_lock's cached LIVE reads (Upstox positions/orders, Kite positions/holdings/MTF) before the entry-SL check, so R8 sees the fill. Instrument/lot caches are kept and reads stay fail-closed. `upstox_book_symbol()` resolves the order-book symbol from the entry order or instrument key, and `state_target(key=)` finds the Upstox target by instrument key. New sl_confirm_test.py (12 checks). Effect: the Upstox stop goes in at entry instead of ~2 min later. This only adds protection; no rule is loosened.

## Tests (pass/total, before → after)
live 27/35 → 28/36, stage 28/35 → 29/36, kite mirror 12/18 → 13/19. Only change: the new sl_confirm_test passes. The same failures as 19 Nov remain (autonomy 17, full_auto_sim 9 from date fixtures, capital_policy 1, stockopt_policy 2, mtf_policy, stockopt_st_ema scope, auto_entry_signal orphan, index_contract_policy needs .git; kite-mirror missing modules / spread_exit errors).
