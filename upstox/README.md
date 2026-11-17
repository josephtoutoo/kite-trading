# upstox-trading

Upstox risk-management and equity-trailing scripts. Secrets come from environment variables (UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_TOTP_SECRET, UPSTOX_PIN, UPSTOX_MOBILE, UPSTOX_PROXY_URL); token.json and state files are never committed.

## Rules (updated 6 Nov 2026)
**Capital split**: through 8 Nov 2026, equity MTF max 50% of capital (10% max per stock, 12% on 5-6 Nov only); options + futures use the remaining 50%.

**From Monday 9 Nov 2026 IST** (name cap and signal updated 6 Nov 20:06 IST): each broker, on its own capital and blocked MTF margin, may hold at most 5 actual positive MTF names (legacy and user lots count). Five is a maximum, not a quota. Qualification and ranking use the completed daily session only: close above EMA20, Supertrend green, MACD histogram confirming, +DI dominant, ADX at least 25. There is no 1-hour fallback for the MTF entry. Score = ADX + 1000*MACD histogram/close + 2*percent above EMA20; equal scores sort by symbol. Initial buys and adds share one 25% pool of that broker's verified capital (confirmed 6 Nov 2026 21:36 IST). There is no separate add reserve, and unused cash is not moved into MTF. Stock options, index options, and index BEEs are 25%, 25%, and 10%. Cash stays 15%. The 10% per-stock cap still applies because it is stricter. Margin capacity is not treated as that capital. From 9 Nov a restarted MTF stop process ratchets to the completed daily Supertrend and never lowers a live stop; until that date the process keeps the current 5% or tighter 1-hour rule. A breached daily level is an exit decision and is not sent while execute_exit stays false. Missing daily data does not cancel a stop and does not fall back to the 1-hour bar. The 5% band is only the emergency floor. Unused add reserve cannot fund a new name. The ranker chooses the strongest eligible names; it does not wait for a manual list. Both brokers must pass the plan before either order. A book already over those budgets is reported and refuses new buys and adds; nothing liquidates it to get back under the line. The saved refill routine is updated in source and remains paused until the runtime is deliberately started.

**Options (risk_manager.py, every 5 min, 9:15-15:25 IST weekdays)**: covers all open long options incl. carry-forward (product D). **Exit rule (4 Nov 2026)**: a fixed 30% stop under entry bounds every loss and never trails from the peak (trailing cut the backtest average from +12% to +5%). **Index options only** (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, BANKEX, SENSEX) add a trend exit once the contract's high is 15% above entry: sold when the underlying 5m chart closes against it (CE: close < 20 EMA with Supertrend red; PE: the opposite), so winners run. Stock options and futures keep the plain % stop. Target rolls +20% of entry per step while the trend holds, with the stop floor rising to the previous target less one step. Never sells more than held; cancels leftover SL orders on closed positions; no orders after 15:25.

**Daily loss lockout (trade_lock.py)**: if Upstox day loss (booked + open) exceeds Rs 30,000, no new trades for the rest of the day; next day max 1 new trade at half size. Each trade keeps its own stop; nothing auto-closed.

**Equity (equity_trail.py, 1h 20 EMA + Supertrend)**: when target is hit and trend intact, roll target up and raise stop (never lowered); book profit only when a 1h close falls below 20 EMA or Supertrend. Alerts only.

Every new order needs explicit user approval.

## Stock options: strong technicals and pre-results exits

`equity/stockopt_tech_entry.py` scans liquid NSE F&O stocks on closed 1h candles
(EMA20 + Supertrend + MACD + DMI, rising ADX >= 25), selects the nearest-expiry
ATM CE/PE, and pre-checks both broker legs through `trade_lock` before sending a
`stockopt_tech` LIMIT order. That liquid scanner stays paused and does not place.
`equity/stockopt_st_ema_entry.py` may send only for `st_ema_category_watchlist`
when `STOCKOPT_ST_EMA_EXECUTE=1`. A first entry is a fresh 1-hour baseline
trigger or an already-aligned 1-hour join (calls with Supertrend above EMA20,
puts with Supertrend below it), 1 lot per broker. Adds stay off. One PSU bank
per pass. The position is due flat by that Friday's close. The flag is off
unless the live process sets it.
`equity/stockopt_st_ema_live.sh` sets `STOCKOPT_ST_EMA_EXECUTE=1` and
`STOCKOPT_INTRADAY_TECH_EXECUTE=1` only for that process, then runs
`equity/stockopt_st_ema_entry.py` and `equity/stockopt_intraday_tech_entry.py`.

t694u (16 Nov 2026): `equity/stockopt_intraday_tech.py` / `equity/stockopt_intraday_tech_entry.py`
takes at most two INTRADAY stock-option names from the pinned strong-technicals
shortlist (`equity/stockopt_intraday_shortlist.json`) when a completed 1-hour
cash bar still holds the setup (price vs EMA20 and EMA50, Supertrend and MACD
agreeing, ADX >= 20) and the daily bar still agrees. Rank is 1h ADX / strength,
with daily-MACD disagreement treated as weaker. 1 lot per broker, LIMIT, tag
`stockopt_intraday`, Upstox product I and Kite product MIS, current monthly ATM
(23 Nov 2026 this month; this path refuses a new near-month entry in expiry
week). Both brokers are checked before either send. New entries 09:30-14:30 IST
only. Same-day square-off is 15:15 IST (15:10 IST on Friday) via
`--flatten-only`. The 20% initial stop and breakeven ladder are the shared
option-stop rules. The daily loss cap, same-contract cooldown, and oversize
checks apply; a failed check sends nothing. The execute flag is off unless the
live wrapper or `STOCKOPT_INTRADAY_TECH_EXECUTE=1` sets it. `--dry-run` and
`--from-report` place nothing.
t696u (16 Nov 2026): total premium deployed per broker on this intraday path
(already-open path positions plus the new entry) is capped at Rs 20,000.
Premium is lot size x limit price. A candidate that would breach the cap is
skipped with a logged reason, and the next-ranked still-valid name is tried.
The two-name limit is unchanged. The cap is checked before either broker is
sent, and again at send time.
R11 allows at most 3 distinct underlyings per broker, pending names included;
the old 4-underlying figure is not applied. The watchlist path sizes 1 lot and
does not use the expired 5-6 Nov 2-lot grant. A new watchlist name is marked
as an explicit initial entry; the 3-name cap, capital bucket, day-loss,
cooldown, and funds checks still apply and fail closed when unverified.
`equity/stockopt_add.py` can mark an existing contract eligible and does not
send the add. Every technical run writes a decision file, including
a no-trade run and an early exit:

`equity/logs/stockopt_tech/runs/<YYYYMMDDTHHMMSS+0530>-<pid>.json`

and one index line in `equity/logs/stockopt_tech/decisions-<YYYYMMDD>.jsonl`.
Files in that directory older than 30 calendar days are deleted on the next scan.
The log records candidates, rejection reasons, per-broker check results when a
check ran, errors, and a completed or aborted summary. It does not rebuild a
scan that did not write a file. The directory is gitignored; do not commit it.

`equity/stockopt_results_exit.py` reads `stockopt_calendar.json` and exits only
positions sourced/tagged `results_entry` at/after 15:10 IST on the last trading
day before the expected announcement (clearing overnight GTT first). TATASTEEL's verified 7 Nov 2026 date maps
to Friday 6 Nov. Run every five minutes from 15:10-15:20 IST; `--dry-run` is
read-only. The committed calendar and `.example` document the source/date; the
calendar remains force-added because this repository ignores JSON state files.

Stock-option entries always use the completed 1-hour bar (t687u, 16 Nov 2026).
When the current monthly stock-option expiry falls in the current ISO week
(the expiry week), a first entry is a next-month debit spread: buy the
next-month ATM chosen by the existing nearest-strike rule, and sell two
listed strikes further OTM of the same type, same quantity, same broker
(`equity/stockopt_spread.py`, `HEDGE_STEPS=2`). The buy is placed first and
the sell is sent only after that buy fill is confirmed (one SELL retry, then
alert; the long stays on its normal stop). Exits close both legs, buying
back the short first, then selling the long. `SPREAD_SEND_ALLOWED` is True
once those pair paths are in place (t691u, 16 Nov 2026). Overnight, a GTT
SELL BELOW sits on the long at the 20% net-debit stop, and a companion GTT
BUY BELOW sits on the short at a price scaled to the same move (Upstox
`type=SINGLE` + `trigger_type=BELOW`; Kite `GTT_TYPE_SINGLE`). A SELL GTT
is never placed on the short. A 9:15 IST (and every day-session
`gtt_overnight` / `risk_manager`) reconcile buys back any leftover lone
short immediately. Day-time pair exits in `risk_manager.py` fire on the
net-premium 20% stop plus the existing ladder on net value (fallback: 20%
on the long, still closing both). Friday 15:10 flatten, expiry-week
square-off of a spread in *its own* expiry week, and results-day exits all
close short first. The expiry-week square-off still does not flatten a
later-month (next-month) spread. Adds on a spread are refused. A lone day
SL is never left on a spread long.

t689u (16 Nov 2026): on each ST/EMA entry-routine run, before names are
evaluated, a completed 1h candle that closes against both EMA20 and
Supertrend for that stock's watched direction (long: close below EMA20 and
Supertrend bearish; short: the opposite) is removed from
`st_ema_category_watchlist` in `monitor_scope.json` and dropped from
`stock_option_underlyings` if present. Removals are appended to
`equity/logs/st_ema_watchlist_removals.jsonl` with the reason and timestamp.
Open positions are not closed. A `--dry-run` reports would-remove names and
writes nothing. Saturday weekly cap lists (`weekly_watchlists_*.json`) do
not feed the ST/EMA entry list (that list comes from the 1h ST-versus-EMA
scan, e.g. `reports/st_ema_scan_1114.json`) and are not rewritten mid-week.

Offline tests, with no broker requests or orders: `python3 equity/stockopt_test.py`, `python3 equity/stockopt_st_ema_test.py`, `python3 equity/stockopt_intraday_tech_test.py`, `python3 equity/stockopt_spread_test.py`, `python3 equity/stockopt_watchlist_trend_test.py`, `python3 equity/expiry_squareoff_test.py`, and `python3 index_size_policy_test.py`.
Deploy both scripts and the calendar into the live Upstox tree before enabling
the schedules. These scripts do not themselves create or modify scheduler jobs.

## Index option routes (t673u, 16 Nov 2026 20:55 IST)

`auto_entry.py` (contract-policy path, `INDEX_CONTRACT_POLICY=1`) now evaluates three independent
routes for NIFTY, BANKNIFTY and SENSEX on each broker; each route may hold its own position per index
(`index_routes.py`):

| Route | Candles (completed, futures) | Product | Option expiry |
|---|---|---|---|
| 3m | broker 3-minute | intraday (Upstox I / Kite MIS), squared off same day | current week (nearest listed; BANKNIFTY current monthly) |
| 15m | 15-minute resampled from 5m | delivery (D / NRML) | next week (BANKNIFTY: monthly rule) |
| 1h | 1-hour resampled from 5m | delivery (D / NRML) | monthly; a monthly in the current ISO week rolls to next month |

Entry window 09:30-15:25 IST (3m route no new entry after 15:10; Friday no entry after 15:05).
Signal per route: fresh EMA20/Supertrend cross + MACD hist + ADX>=18, or - when the route has no open
position - the previous session's trend still in force on that timeframe (same filters, no fresh cross).
All guards unchanged (loss cap, lot sizing, oversize refusal, 20% stop, re-entry block, expiry > 1
trading day, 15m trend filter, trade_lock R1-R14). Route of each bot position: `index_route_positions.json`;
untracked positions are classified read-only (intraday product -> 3m, monthly -> 1h, else 15m).
`index_3m_squareoff.py` closes only tracked 3m positions (live only with `--execute` 15:12-15:29 IST).
`risk_manager.py` (every 5 min, 09:15-15:25 IST) also closes a routed index option when two consecutive
completed futures candles on that route's timeframe, after the entry, each close against the previous
close (CE below, PE above), or when a completed route-timeframe futures candle after entry closes against
EMA50 (CE below, PE above). Whichever completes first is sent. Tracked routes use `index_route_positions.json`;
untracked / legacy index options are classified the same way as entry (intraday -> 3m, monthly -> 1h, else 15m).
The 3m case is checked on each 5-minute run against all completed post-entry 3m futures candles; the sell is
sent at that run (up to ~5 minutes after the completing close). Offline tests: `python3 index_routes_test.py`,
`python3 index_two_candle_exit_test.py`.

## Long-term daily Supertrend MTF sleeve (t692u–t698u, 16 Nov 2026)

`equity/longterm_daily_st.py` is the scanner. It does not place orders.
Approved names from the 16 Nov 2026 close, to buy as MTF on both brokers
after 09:20 IST on Tue 17 Nov 2026, are IGIL, PVRINOX and LTTS, each with a
stop at that day's Supertrend (₹385.70, ₹1,477.67, ₹3,865.56).

`equity/longterm_daily_st_entry.py` is the live path. A 17 Nov 09:20 IST
routine should run:

```
/workspace/upstox/equity/longterm_daily_st_live.sh --execute
```

The wrapper sets `LONGTERM_DST_EXECUTE=1` only for that process. Without
`--execute`, or outside 09:20–15:00 IST on an NSE trading day from 17 Nov
2026, the script stays in dry-run and places nothing.

Each name uses about ₹50,000 of MTF margin per broker (quantity is
recomputed from a live margin quote so it stays at or under that cap), at
most 3 names, so about ₹1.5 lakh per broker. Product is MTF, LIMIT a small
buffer above LTP, tag `longterm_dst`. A name that has already been bought,
or that no longer meets the 16 Nov daily setup, is skipped. A name that
gaps more than 3% above the 16 Nov close is skipped, logged, and alerted.
After a fill, a protective SL is placed at the daily Supertrend. The
existing `kite-mtf-stop-trail` and `upstox-mtf-stop-trail` routines then
trail that stop to the completed daily Supertrend and never loosen it.

These are long-term delivery MTF positions, not intraday, and must be
excluded from any same-day square-off (`is_longterm_dst_squareoff_excluded`).

Tag `longterm_dst` is exempt from the Monday 9 Nov MTF caps (max 5 names,
10% per stock, 25% pool) only. The sleeve's own cap is max 3 names and
₹50,000 margin per name per broker. Sleeve holdings do not consume the
Monday budget. Offline tests: `python3 equity/longterm_daily_st_test.py`
and the longterm case in `python3 equity/mtf_policy_test.py`.
