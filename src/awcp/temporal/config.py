import os
import sys

# Temporal Server Configuration
TEMPORAL_SERVER_URL = os.getenv("TEMPORAL_SERVER_URL", "localhost:7233")
TASK_QUEUE_NAME = "awcp-governance-queue"

# Local Execution Substrate (FastAPI) Configuration
# Retained for the legacy FastAPI activities; the MCP-driven path below does
# not use it. If running Temporal remotely, this should be your ngrok URL.
FASTAPI_BASE_URL = os.getenv("AWCP_TUNNEL_BASE_URL", "http://localhost:8001")

# MCP Execution Substrate Configuration
# Temporal activities act as MCP clients that spawn the AWCP MCP server over
# stdio. By default we reuse the same interpreter running the worker (.venv),
# which has both temporalio and the mcp client/server installed.
#
# Path layout: this file is <repo>/src/awcp/temporal/config.py
#   SRC_DIR   = <repo>/src   (must be on PYTHONPATH so `import awcp` works)
#   REPO_ROOT = <repo>       (workdir, so file/web_search tools keep relative paths)
SRC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
REPO_ROOT = os.path.abspath(os.path.join(SRC_DIR, os.pardir))

MCP_WORKDIR = os.getenv("AWCP_MCP_WORKDIR", REPO_ROOT)
MCP_PYTHON = os.getenv("AWCP_MCP_PYTHON", sys.executable)
# Launch the server as a module so it resolves through the `awcp` package.
MCP_SERVER_ARGS = ["-m", "awcp.mcp.server", "stdio"]

# --- Remote MCP transport (for sharing one MCP server across machines) ---
# If AWCP_MCP_SSE_URL is set (e.g. a teammate points at your ngrok tunnel),
# the worker connects to that remote MCP server over SSE instead of spawning a
# local stdio subprocess. Leave it unset for the normal local stdio path.
#   AWCP_MCP_SSE_URL   e.g. https://<id>.ngrok-free.app/sse
#   AWCP_MCP_SSE_AUTH  optional "user:pass" for an ngrok basic-auth tunnel
MCP_SSE_URL = os.getenv("AWCP_MCP_SSE_URL", "").strip() or None
MCP_SSE_AUTH = os.getenv("AWCP_MCP_SSE_AUTH", "").strip() or None