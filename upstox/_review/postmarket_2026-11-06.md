# Post-market algorithm review — 6 Nov 2026 (Asia/Calcutta)

**Trading day:** confirmed (`market_calendar.is_trading_day('2026-11-06')` → True)

## What was examined
- Detector reports `_detector_report_20261106_*.{txt,json}` (full day)
- `seen_entries.json`, `risk_state.json`, `alert_state.json`
- Kite `option_monitor_state.json` (peaks, stops, overnight GTT)
- Audits `_audit_t433u_20261106_1548/FORENSIC.json` (PE→CE miss) and `_audit_t430u_*`
- Merged 5m candles NIFTY/BN/SENSEX Jul 30–Nov 6 (API recent + backup + expiry_backtest CSV)
- Prior t378u reject note; trade_lock R5/R6/R10 behaviour on midday adds

## Day timeline (algo vs external)
| Time IST | Event | Source |
|---|---|---|
| 09:27 | NIFTY 24500 PE ×65 both brokers @186.8 | **auto_entry** |
| 09:46 | PE trailsl @130.35 both | risk_manager |
| 09:48 | BN 57500 CE target_book exit (Kite) | risk_manager |
| 09:50 | BN fresh CE fire (hist+ADX ok) — **no auto_entry order found** | signal only |
| ~09:59+ | NIFTY CE / SENSEX CE adds | tfc_tv / manual_or_external |
| 10:37 | MARUTI MTF dual | rotation |
| ~10:54–11:45 | BOSCH SL; mass mtf_trim; INDHOTEL dual→Kite same-day sell block→unwind; TITAN same | ops |
| 13:07–13:11 | M&M + ULTRACEMCO MTF dual | rotation |
| 13:31–13:32 | Kite NIFTY CE add + Upstox SENSEX CE add | external (R5 MARKET / R6 >20% / R10) |
| ~13:55 | mtf_clear HYUNDAI/INDIGO/ICICI… → MTF qty 0 | user reshuffle |
| 14:01 | Upstox NIFTY 24800 PE + Nov FUT | external (PE vs UP trend — R4 would block) |
| 14:06+ | Kite NIFTY 17Nov 24800 CE builds to 325 | external / adds |
| EOD open | Upstox: SENSEX79800CE 60, NIFTY24800PE 130 (underwater), NIFTY FUT 130; Kite: NIFTY1724800CE 325 | |

Held alerts correct: NIFTY PE `15m_st_exit`; SENSEX CE `profit50` (no re-fire). Overnight GTT on Kite CE trig 319.25 qty 325.

## Candidate tweaks & backtests (index pts, ST-flip/EOD, 1 pos/index, 09:20–14:45, Jul30–Nov6)

| Variant | n | win% | total | ex-6Nov tot | incremental ex-6Nov |
|---|---|---|---|---|---|
| **fresh-only (current)** | 172 | 41.3 | +201 | −830 | — |
| live-flat (t378u) | 433 | 43.6 | +3588 | +1320 | live-only ex56 **−633** |
| reversal-after-stop | 227 | 40.5 | +189 | −1259 | incr **−1199** |

Stress on live-flat: exclude 5+6 Nov → live-only **−633**; BANKNIFTY live-only −1467; Nov month +3947 of +3533 total; top-10 winners = 8343 of 3533 (concentration). **REJECT** live-flat and reversal — both overfit to recent/today. Prefer current fresh-cross.

Signal-logic: **nothing beat current out of sample.**

## Ops fix adopted
**Same-day MTF sell pre-check** in `equity/mtf_dual_entry.py`: if either broker already completed an MTF SELL in that name today, do not attempt dual buy (Kite day-block; prevents Upstox-only fill + unwind as on INDHOTEL/TITAN). Pure helper `mtf_sell_today` + gates in `_buy` / `_mirror_leg`. Unit test `mtf_dual_entry_test.py` (9/9).

Invariants untouched: ₹30k day-loss breaker, fail-closed reads, mirroring, user caps, technical stock-opt pause.

## Not changed
- trade_lock R5/R6 — correctly would have blocked external MARKET / >20% adds; do not loosen
- PE held into UP day — alert fired; position was external/manual monthly PE
- BN 09:50 missed poll — insufficient evidence (no auto_entry stdout); observability gap noted
