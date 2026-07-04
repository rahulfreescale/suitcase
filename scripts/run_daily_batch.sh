#!/usr/bin/env bash
# Daily live-traffic evaluation. Schedule with cron, e.g.:
#   0 2 * * *  cd /path/to/suitcase && ./scripts/run_daily_batch.sh >> eval.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
python -m eval.live_traffic_eval
