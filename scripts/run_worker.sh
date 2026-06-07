#!/bin/bash
# Start the Temporal worker that drives agents/tools over the MCP server (stdio).
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src"
echo "🚀 Starting AWCP Temporal worker (task queue: awcp-governance-queue)..."
./.venv/bin/python -m awcp.temporal.worker.run_worker
