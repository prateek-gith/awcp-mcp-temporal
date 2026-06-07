#!/bin/bash
# Start the MCP Control Server (SSE transport + dashboard) on :8002.
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src"
echo "🧹 Cleaning Python cache..."
find src -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
echo "🚀 MCP server -> http://localhost:8002  (SSE: /sse, messages: /messages)"
./.venv/bin/uvicorn awcp.mcp.server:app --host 0.0.0.0 --port 8002 --reload
