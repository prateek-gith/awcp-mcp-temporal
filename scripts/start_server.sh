#!/bin/bash
# Start the FastAPI agent service (legacy direct REST path) on :8001.
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src"
echo "🧹 Cleaning Python cache..."
find src -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
echo "🚀 Agent service -> http://localhost:8001  (docs: /docs)"
./.venv/bin/uvicorn awcp.service:app --host 0.0.0.0 --port 8001 --reload
