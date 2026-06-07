#!/bin/bash
# Start the Live Control Surface (manual input UI + Temporal trigger API) on :8003.
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src"
echo "🚀 Control surface -> http://localhost:8003"
echo "   (requires: Temporal server + ./scripts/run_worker.sh running)"
./.venv/bin/uvicorn awcp.control.api:app --host 0.0.0.0 --port 8003 --reload
