# Post-market review: Thu 19 Nov 2026 (IST)
Trading day (market_calendar.is_trading_day → True). Read-only broker dumps at about 16:10 IST: Upstox 7 orders / 8 trades / 3 positions; Kite 43 orders / 31 trades; Kite GTTs read (only URBANCO 344995717 active). No order, GTT or routine was touched.
Files: /workspace/perf_review_20261119/ (raw_today.json, raw_gtt.json, tests_*.txt). Backtest: /workspace/backtests/postmarket_20261119/ (bt.py, bt_19.py, bt_19_robust.py, results_19*.txt). Spot 3m/15m refreshed to 19 Nov 15:27.

## Market
Strong up day: NIFTY 25,227.95 → 25,514.3 (+1.14%), BANKNIFTY 62,038.65 → 63,063.0 (+1.65%), SENSEX 82,031.81 → 83,021.75 (+1.21%); closes at the day high.

## Books (day P&L vs previous close; Kite = position m2m; Upstox = day sells + open MTM − day buys − overnight qty × prev close)
- Index options. Kite: BANKNIFTY 26DEC 62500CE +20,973 (manual buys 09:21 and 15:29, 60 open), NIFTY 01DEC 25500CE +5,122 (130 carried, no-stop per user since 10:35; manual trims 65@179.2 and 65@208.4, re-buys 130@277.7 at 15:29), SENSEX 26NOV 82800CE +6,218 (tfc_tv/manual), SENSEX 26NOV 82000PE −2,507 (manual buy 13:10, kite_opt_monitor stop exit 14:25), BANKNIFTY 26NOV 62400CE +288 (the only bot entry: auto_entry 15m CE 10:01:57 @404.4, untagged/manual sell 10:34 @414). Upstox: BANKNIFTY 26DEC 62400CE +39,872 (90 carried, sold 12:06/13:18/14:36; broker P&L vs cost +35,545.5), BANKNIFTY 26DEC 63600CE 60 open +2,732 (untagged BUY 15:30:10, not through the box api; the user's no-new-Upstox-entries instruction runs to 25 Nov).
- Stock options. Kite: PNB 151CE 8,000 sold 13:15 +19,520 (t738u trade, broker vs cost +20,320), INDHOTEL 900CE 2,000 sold +11,150, APOLLOHOSP 8800CE 125 open −2,506, ITC 26DEC 300CE 1,725 (t743u) open +259. Upstox INDHOTEL 900CE: sold 1,000@30, untagged re-buy 1,000@25 at 12:16, sold 1,000@27; 2,000 open; day +21,300 (broker vs cost −5,160).
- MTF: Kite SRF ×6 held, no trades. mtfsl stop placed 09:16 (09:08 pre-open attempt refused), cancelled by the exchange at 15:15:09 "Stop-loss orders not allowed in the current session" (same as 18 Nov).
- MCX crude: no position, no trade.
- Kite CNC (manual): PAYTM −183, TITAN +35.
- Totals: Kite +58,369; Upstox +63,904 vs prev close (broker P&L vs cost +33,117). Daily loss cap not tripped (1 losing closed trade, SENSEX 82000PE; recomputed realised +89,203.5).

## Algo behaviour (591 decision rows)
- 15m BANKNIFTY CE fresh cross (09:45 bar) → Kite entry 10:01:57 (Upstox refused by R15). Exited manually at 10:34 for +288; BANKNIFTY then rose another ~640 points.
- 15m NIFTY/BANKNIFTY PE continuation (09:15 bar) and 15m NIFTY/SENSEX CE fresh cross (11:15 bar), 1h BANKNIFTY CE continuation (15:18): no order and no pretrade row, so the skip reason was not recorded (most likely Upstox R15 + Kite route occupancy by the manual NIFTY 25500CE / BANKNIFTY 62500CE).
- 1h NIFTY PE continuation 10:20–11:11 blocked by the 15m trend filter (correct on an up day).
- SENSEX 3m CE continuation blocked all day (09:48–14:41, ~30 times) and 3m PE at 15:04: "expiry within 1 trading day" (SENSEX weekly expiry today; 3m uses the current-week contract and t669u forbids ≤1-day expiry).
- Counterfactual (spot proxy, cap off): 3m NIFTY CE +997, BANKNIFTY CE +5,222, SENSEX PE −2,284; 15m NIFTY CE +4,001, BANKNIFTY PE −4,146, SENSEX CE −1,598.

## Ops issues from the watchdog
- Four insufficient-funds rejects (PNB151CE 10:01, BANKNIFTY 26NOV 62400CE 10:34, SENSEX 82800CE 11:03, BANKNIFTY 26DEC 62500CE 13:02): all untagged manual SELL exits sent while a bot SL SELL for the same quantity was still pending. Kite margins the second sell as a possible short. Each exit went through after the SL was cancelled (except BANKNIFTY 62500CE, still held). Not a bot bug; exit via the bot or cancel the SL first.
- SRF stop cancel 15:15: exchange-side session cancel of the MTF SL order (also 18 Nov). Not a code bug; SRF has no stop from 15:15 and overnight while the overnight GTT routine stays paused by the user.
- Nine DAY cancels 15:43–15:44: 3 bot SLs expired (BANKNIFTY 62500CE, APOLLOHOSP 8800CE, ITC 300CE) plus 6 untagged manual buy limits (5× NIFTY 25500CE 130–170, INDHOTEL 900CE @25). Normal DAY expiry. Overnight with no stop: APOLLOHOSP 125, BANKNIFTY 62500CE 60, ITC 1,725, NIFTY 25500CE 130 (user no-stop), SRF 6 (Kite); INDHOTEL 2,000 and BANKNIFTY 63600CE 60 (Upstox, stops off by user).

## Backtest (premium proxy, 1 lot, costs; IS Jan–Jun, OOS 1 Jul–19 Nov)
| Route | Variant | IS net (n) | OOS net (n) | OOS months + |
|---|---|---|---|---|
| 3m | CURRENT | −96,236 (344) | −28,067 (276) | 1/5 |
| 3m | ADX 25 | −108,623 | −25,975 | 1/5 |
| 3m | cut 13:00 | −94,115 | −18,412 | 1/5 |
| 3m | no continuation | −85,528 | −55,245 | 0/5 |
| 15m | CURRENT | −219,258 (319) | −59,089 (225) | 1/5 |
| 15m | start 10:00 | +1,839 | −17,274 | 2/5 |
| 15m | continuation from 10:00 | −84,090 | −42,662 | 1/5 |
| 1h | CURRENT | −374,403 (219) | +646 (145) | 1/5 |
| 1h | no two-candle exit | −292,129 | +17,665 | 3/5 |
| 1h | 3-candle exit | −287,901 | −6,479 | 3/5 |
Robustness: 3m cut 13:00 is worse on NIFTY (−34,786 vs −32,077) and its edge sits in Mar/Nov. 15m start 10:00 depends on April (+93,938) and gap days (+87,089 vs −102,524 on no-gap days). 1h no-two-candle is worse on BANKNIFTY (−178,453 vs −111,015). All three change user-set windows/exits.
**Nothing adopted.** No signal, size, stop, cap or breaker change. 15m start 10:00 and 3m cut 13:00 remain user recommendations only.

## Code change (audit only)
auto_entry.done() now writes one "outcome" row to index_entry_decisions.jsonl for each routed signal that was not submitted (action, Kite action, trade_lock reasons, broker eligibility). Pure logging via index_routes.outcome_records(); skipped in dry-run; wrapped in try/except; no decision path changed. New test index_routes_test.test_outcome_records. Mirrored to upstox_stage and kite/upstox.

## Tests (pass/total, before → after)
live 27/35 → 27/35, stage 28/35 → 28/35, kite mirror 12/18 → 12/18; same per-test statuses. index_routes_test passes in all three. Failures already there: R15 block active (autonomy 17, or 12 with UPSTOX_ENTRY_BLOCK_OFF=1; capital_policy; mtf_policy; stockopt_policy), full_auto_sim (date-dependent), stockopt_st_ema scope fixture, auto_entry_signal_test orphan, index_contract_policy needs .git; kite-mirror missing modules / spread_exit errors.
