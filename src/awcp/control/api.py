"""AWCP Control Surface — HTTP bridge between the browser and Temporal.

The browser cannot speak Temporal's gRPC protocol, so this thin FastAPI service
accepts a prompt, starts the governed workflow (which drives the MCP server over
stdio), and exposes live status by polling the workflow's event history. The UI
(static/index.html) renders the per-step progress and links out to the Temporal
Web UI for the full event history.

No agent logic lives here — it reuses the existing registry and Temporal pieces.
"""

import os
import logging
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from temporalio.client import Client, WorkflowExecutionStatus

from awcp.registry.service import build_registry
from awcp.registry import store
from awcp.runtime.tool_runtime import discover_tools
from awcp.temporal.config import TEMPORAL_SERVER_URL, TASK_QUEUE_NAME
from awcp.temporal.workflows.agent_execution import AgentGovernanceWorkflow
from awcp.temporal.workflows.dynamic_ask import DynamicAskWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Temporal Web UI base (dev server default). Used only to build deep links.
TEMPORAL_UI_BASE = os.getenv("AWCP_TEMPORAL_UI_BASE", "http://localhost:8233")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# The ordered governance steps the workflow schedules as activities. Used to
# render a stable timeline even before each activity has been scheduled.
_STEP_SEQUENCE = [
    "mcp_get_agent_info",
    "mcp_agent_route",
    "mcp_execute_tool",
    "mcp_agent_generate",
]

app = FastAPI(title="AWCP Control Surface")

# Populate the in-memory registry so the agent picker has data (same bootstrap
# pattern as the MCP server and the FastAPI agent service).
discover_tools()
build_registry()


def _temporal_url(workflow_id: str) -> str:
    return f"{TEMPORAL_UI_BASE}/namespaces/default/workflows/{workflow_id}"


async def _client() -> Client:
    return await Client.connect(TEMPORAL_SERVER_URL)


class RunRequest(BaseModel):
    agent_name: str
    input: str


class AskRequest(BaseModel):
    query: str


@app.get("/agents")
def list_agents() -> list[dict]:
    """Agent names/status for the picker, straight from the registry."""
    return [
        {"name": a.name, "status": a.status, "runtime": a.runtime}
        for a in store.get_all()
    ]


@app.post("/run")
async def run(req: RunRequest) -> dict:
    """Start the governed workflow (non-blocking) and return its handle info."""
    if not req.input.strip():
        raise HTTPException(status_code=400, detail="input must not be empty")

    workflow_id = f"awcp-exec-{req.agent_name}-{uuid.uuid4().hex[:8]}"
    client = await _client()

    await client.start_workflow(
        AgentGovernanceWorkflow.run,
        {"agent_name": req.agent_name, "input": req.input},
        id=workflow_id,
        task_queue=TASK_QUEUE_NAME,
    )

    return {"workflow_id": workflow_id, "temporal_url": _temporal_url(workflow_id)}


@app.post("/ask")
async def ask(req: AskRequest) -> dict:
    """Run a dynamic MCP-backed Temporal workflow for any user query."""
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    workflow_id = f"awcp-ask-{uuid.uuid4().hex[:8]}"
    logger.info("Starting /ask workflow_id=%s query=%r", workflow_id, query)

    try:
        client = await _client()

        handle = await client.start_workflow(
            DynamicAskWorkflow.run,
            {"query": query},
            id=workflow_id,
            task_queue=TASK_QUEUE_NAME,
        )
        result = await handle.result()
    except Exception as e:
        logger.exception("/ask workflow failed workflow_id=%s", workflow_id)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Dynamic ask workflow failed",
                "workflow_id": workflow_id,
                "error": str(e),
                "temporal_url": _temporal_url(workflow_id),
            },
        ) from e

    logger.info(
        "Completed /ask workflow_id=%s synthesis_status=%s",
        workflow_id,
        result.get("synthesis_status"),
    )

    return {
        "workflow_id": workflow_id,
        "temporal_url": _temporal_url(workflow_id),
        "result": result,
    }


def _extract_steps(events) -> list[dict]:
    """Fold Temporal history events into per-activity step states."""
    scheduled: dict[int, str] = {}   # event_id -> activity name
    states: dict[str, str] = {}      # activity name -> status

    for e in events:
        sched = e.activity_task_scheduled_event_attributes
        if sched and sched.activity_type.name:
            name = sched.activity_type.name
            scheduled[e.event_id] = name
            states.setdefault(name, "scheduled")
            continue

        started = e.activity_task_started_event_attributes
        if started and started.scheduled_event_id in scheduled:
            states[scheduled[started.scheduled_event_id]] = "running"
            continue

        completed = e.activity_task_completed_event_attributes
        if completed and completed.scheduled_event_id in scheduled:
            states[scheduled[completed.scheduled_event_id]] = "completed"
            continue

        failed = e.activity_task_failed_event_attributes
        if failed and failed.scheduled_event_id in scheduled:
            states[scheduled[failed.scheduled_event_id]] = "failed"

    # Present the canonical sequence, marking not-yet-seen steps as pending.
    return [
        {"name": name, "status": states.get(name, "pending")}
        for name in _STEP_SEQUENCE
    ]


@app.get("/status/{workflow_id}")
async def status(workflow_id: str) -> dict:
    """Poll workflow status + per-step progress; include result when finished."""
    client = await _client()
    handle = client.get_workflow_handle(workflow_id)

    try:
        desc = await handle.describe()
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"workflow not found: {e}")

    status_name = desc.status.name if desc.status else "UNKNOWN"

    events = [e async for e in handle.fetch_history_events()]
    steps = _extract_steps(events)

    result = None
    if desc.status == WorkflowExecutionStatus.COMPLETED:
        result = await handle.result()

    return {
        "workflow_id": workflow_id,
        "status": status_name,
        "steps": steps,
        "result": result,
        "temporal_url": _temporal_url(workflow_id),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))
