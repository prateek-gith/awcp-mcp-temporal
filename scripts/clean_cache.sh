#!/bin/bash
# Clean Python bytecode cache under src/.
set -e
cd "$(dirname "$0")/.."
echo "Cleaning Python cache..."
find src -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find src -name "*.pyc" -delete 2>/dev/null || true
echo "Cache cleared."
