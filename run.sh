#!/usr/bin/env bash
# Runs the monitor using the venv created by setup.sh.
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
python -m artnet_monitor.main --config config/patch.yaml "$@"
