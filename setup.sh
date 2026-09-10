#!/usr/bin/env bash
# One-time setup: creates an isolated Python virtual environment in ./venv
# and installs all dependencies into it. Does not touch system Python at all.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
  echo "python3 not found. Install it first (macOS: 'brew install python3')." >&2
  exit 1
fi

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "Setup complete."
echo "Next: edit config/patch.yaml (set listen_ip to this machine's real LAN IP),"
echo "then run: ./run.sh"
