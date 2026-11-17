#!/bin/bash
# Live Supertrend-versus-EMA weekend-watchlist stock-option entry, plus the
# t694u intraday strong-technical path. Arms each path's execute flag only
# for this process. The liquid scanner stays paused.
# Same-day flatten of the intraday book is equity/stockopt_intraday_tech_entry.py
# --flatten-only (do not pass that flag through this wrapper).
set -euo pipefail
cd /workspace/upstox
export STOCKOPT_ST_EMA_EXECUTE=1
export STOCKOPT_INTRADAY_TECH_EXECUTE=1
status=0
python3 equity/stockopt_st_ema_entry.py "$@" || status=$?
python3 equity/stockopt_intraday_tech_entry.py "$@" || status=$?
exit "$status"
