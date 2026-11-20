#!/bin/bash
# Live CRUDEOIL option entry / evening management on Kite (user-authorized 12 Nov 2026).
# Usage: crude_auto_live.sh entry|manage [--dry-run]
# CRUDE_TF=15m uses completed 15-minute bars for entry; default is 1h. Shared daily cap.
set -euo pipefail
cd /workspace/upstox
# 12 Nov 09:00 IST: flags come from full_auto_flags.json (crude entries off, ladder on, 1h exit off)
exec python3 /workspace/upstox/crude_auto.py "$@"
