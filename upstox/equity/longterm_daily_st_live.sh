#!/bin/bash
# Live long-term daily Supertrend MTF entry (t697u / t698u).
# Arms LONGTERM_DST_EXECUTE only for this process. Without --execute, or
# outside 09:20-15:00 IST on an NSE trading day from 17 Nov 2026, the
# script stays in dry-run and places nothing.
set -euo pipefail
cd /workspace/upstox
export LONGTERM_DST_EXECUTE=1
exec python3 equity/longterm_daily_st_entry.py "$@"
