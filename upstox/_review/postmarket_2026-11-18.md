# Post-market review: Wed 18 Nov 2026 (IST)
Trading day (market_calendar.is_trading_day → True). Read-only broker dumps at about 16:10 IST: Upstox 12 orders / 18 trades / 4 positions; Kite 41 orders / 53 trades, GTT list read. No orders, GTTs or routines were touched.
Files: /workspace/perf_review_20261118/ (raw_today.json, raw_gtt.json, tests_*.txt). Backtest: /workspace/backtests/postmarket_20261118/ (bt.py, bt_18.py, bt_18_robust.py, results_18*.txt).

## Day context (user-directed, not tuned against)
09:17 "Don't keep any sl" (stop routines paused) · 14:57 Kite stops/exits resumed (Upstox stays without stops) · 15:20 one-trade override PNB26DEC151CE 1 lot Kite with −20% stop (t738u) · 15:25 no new Upstox trades until end of Wed 25 Nov (t739u; already enforced as trade_lock R15 + api guard, upstox-trading ad58f8c).

## Books (gross, broker position P&L, realised + open M2M)
- Index options. Upstox −25,042: NIFTY 26NOV 25400CE −8,086 (carried, sold 09:15), BANKNIFTY 26NOV 61400CE −14,182 (carried + manual re-buys, exits 11:23/13:09), BANKNIFTY 26DEC 62400CE 90 open −2,774 (manual buys 11:45/13:04/13:08). Kite −5,880: NIFTY 01DEC 25500CE +8,424 (many manual/tfc_tv adds and trims, 130 open), SENSEX 26NOV 82800CE −14,304 (carried 160 sold 09:15, manual 200 round trip 13:27→14:08).
- Stock options. Upstox INDHOTEL 26DEC 900CE 3,000 open −25,840. Kite −3,650: INDHOTEL 900CE 2,000 open −7,950, APOLLOHOSP 8800CE 125 open −900, PNB 150CE +5,440, PNB 148PE 0, PNB 151CE (t738u override) 8,000 open −240. Two PNB FUT buys rejected for margin.
- MTF: SRF ×6 sold on Upstox 13:11 (manual, untagged); Kite SRF ×6 still held. Kite mtfsl stop at 15:15 was rejected "Stop-loss orders not allowed in the current session". No long-term daily-ST run today.
- MCX crude: Kite CRUDEOIL 26DEC 8050PE (17 Nov bot entry @489.3) sold 09:24 @561.6, +7,230 (untagged exit).
- Kite CNC SWIGGY/GOLDBEES/TITAN −133 (manual).
- No order today carries a bot entry tag (auto_entry / stock-option / long-term). All fills are manual, tfc_tv or the t738u override.

## Algo behaviour
- Daily loss cap tripped at 09:30:53 (2 losing closed trades, realised −25,670). Both losers were the user's 09:15 exits of positions carried from 17 Nov, so the bot was blocked for the whole index window. 603 decision rows; signals logged but not sent: 3m NIFTY/SENSEX PE continuation 09:33–13:39, 15m SENSEX PE 09:15, 15m BANKNIFTY PE 09:30/12:15/14:30, 3m BANKNIFTY CE 10:24–14:39, 1h NIFTY PE 12:15. Kite NIFTY 15m route was also busy with the held 25500CE. R2b combined day P&L was −50,202 at 15:23 (overridden only for t738u).
- Counterfactual with the cap off, same proxy as the backtest: 3m +1,632 (NIFTY −272, BANKNIFTY +2,174, SENSEX −270), 15m −1,547, 1h −6,368 → about −6,300 net. The cap cost nothing today.
- Overnight protection (observation only, nothing changed): the Kite −20% stops (trailsl / t738u) were regular DAY SL orders and the exchange cancelled them at the session end (Kite shows 15:44). Active Kite GTTs: only URBANCO. NIFTY 25500CE 130, APOLLOHOSP 8800CE 125, INDHOTEL 900CE 2,000, PNB 151CE 8,000 and SRF MTF 6 carry overnight with no stop. The both-broker overnight GTT routine is still paused by the user.

## Backtest (premium proxy, 1 lot, costs; IS Jan–Jun, OOS 1 Jul–18 Nov; spot 3m/15m refreshed to 18 Nov; 1h built from 15m, monthly premium ×1.66, max hold 5 sessions)
| Route | Variant | IS net (n) | OOS net (n) | OOS months + |
|---|---|---|---|---|
| 3m | CURRENT | −96,236 (344) | −32,002 (273) | 1/5 |
| 3m | no continuation | −85,528 | −62,375 | 0/5 |
| 3m | ADX 25 | −108,623 | −22,103 | 1/5 |
| 3m | cut 13:00 | −94,115 | −22,347 | 1/5 |
| 15m | CURRENT | −219,258 (319) | −57,346 (222) | 1/5 |
| 15m | start 10:00 | +1,839 | −25,791 | 2/5 |
| 15m | continuation from 10:00 | −84,090 | −40,919 | 1/5 |
| 1h | CURRENT | −374,403 (219) | +646 (145) | 1/5 |
| 1h | no two-candle exit | −292,129 | +17,665 | 3/5 |
| 1h | 3-candle exit | −287,901 | −6,479 | 3/5 |

Robustness (bt_18_robust): 3m cut 13:00 is worse on NIFTY and most of its gain is Nov/Mar. 15m start 10:00 rests on April (+93,938) and gap days (+87,089 vs −111,041 on no-gap days). 1h no-two-candle is worse on BANKNIFTY (−178k vs −111k) with large monthly swings, and it removes the user's own exit rule (t685u). Expiry-day and gap-day splits favour no variant consistently.
**Nothing adopted.** No variant clearly beats current both IS and OOS without overfitting, and every candidate changes a user-set window or exit. 15m start 10:00 and 3m cut 13:00 are recommendations for the user only. All three routes still lose under this proxy.

## Code change
- kite-trading: mirrored upstox-trading ad58f8c (R15 Upstox entry block: trade_lock rule + api guard + upstox_entry_block.py and its test). It is a pure restriction on Upstox BUYs, and Kite behaviour is unchanged. Only the four files changed. No signal, sizing, cap or breaker changes.

## Tests
- Live 27/35, stage 28/35, kite mirror 11/17 before → 12/18 after (only the new upstox_entry_block_test was added, PASS).
- New failures since 17 Nov are caused by the active R15 block file. The tests use real trade_lock, and some import /workspace/upstox. With UPSTOX_ENTRY_BLOCK_OFF=1, autonomy goes back to 12, and capital_policy, stockopt_policy and the mtf_policy R15 line clear. They clear by themselves after 25 Nov.
- full_auto_sim still has 1 failure: the OVERSIZE fixture expects "allowed 2" but gets the expiry-day 3. It is date-dependent.
- Failures that already existed: autonomy 12, mtf_policy (r10_mtf_capital off), stockopt_intraday_tech 2 (16 Nov fixture), stockopt_st_ema scope fixture, orphan auto_entry_signal_test, index_contract_policy needs .git. In the kite mirror, mtf_policy, stockopt_st_ema and watchlist_trend fail on modules missing from the mirror (capital_policy, index_entry_logic), and stockopt_spread_exit has 3 errors.
