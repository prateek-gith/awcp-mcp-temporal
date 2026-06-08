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
import time
import uuid

from dotenv import load_dotenv

# Load .env before any env-var reads (OTel settings, Temporal URL, etc.)
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from temporalio.client import Client, WorkflowExecutionStatus

from awcp.registry.service import build_registry
from awcp.registry import store
from awcp.runtime.tool_runtime import discover_tools
from awcp.temporal.config import TEMPORAL_SERVER_URL, TASK_QUEUE_NAME
from awcp.temporal.workflows.agent_execution import AgentGovernanceWorkflow
from awcp.temporal.workflows.dynamic_ask import DynamicAskWorkflow
from awcp.observability import (
    configure_observability,
    get_tracer,
    inject_context,
)
from awcp.observability.metrics import instruments
from awcp.observability.evidence import make_evidence_extra, set_governance_attributes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

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
configure_observability(component="control-api", fastapi_app=app)


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    start = time.perf_counter()
    route = request.url.path
    method = request.method
    metrics = instruments()

    logger.info(
        "http_request_started",
        extra=make_evidence_extra(method=method, route=route),
    )
    try:
        response = await call_next(request)
        duration = time.perf_counter() - start
        metric_attrs = {
            "method": method,
            "endpoint": route,
            "status_code": str(response.status_code),
        }
        metrics["http_requests"].add(1, metric_attrs)
        metrics["http_duration"].record(duration, metric_attrs)
        if response.status_code >= 500:
            metrics["http_errors"].add(1, metric_attrs)
        logger.info(
            "http_request_completed",
            extra=make_evidence_extra(
                method=method,
                route=route,
                status_code=str(response.status_code),
                duration_seconds=round(duration, 4),
            ),
        )
        return response
    except Exception:
        duration = time.perf_counter() - start
        error_attrs = {"method": method, "endpoint": route, "status_code": "exception"}
        metrics["http_errors"].add(1, error_attrs)
        metrics["http_duration"].record(duration, error_attrs)
        logger.exception(
            "http_request_failed",
            extra=make_evidence_extra(
                method=method, route=route, duration_seconds=round(duration, 4)
            ),
        )
        raise

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
    # Governance fields (AWCP Magazine §01 Operating Model — Agent Registry)
    agent_id: str = "dynamic"          # identifies which agent or profile is answering
    policy_mode: str = "active"         # active | recommendation_only | safe_mode
    autonomy_mode: str = "active"       # mirrors policy_mode for display


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
    """Run a dynamic MCP-backed Temporal workflow for any user query.

    Governance fields passed through (AWCP Magazine §01 Operating Model):
      agent_id, policy_mode, autonomy_mode → workflow → every activity span.
    Evidence returned:
      replay_context block with context_hash, decision_path, degradation_mode.
    """
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    workflow_id = f"awcp-ask-{uuid.uuid4().hex[:8]}"
    agent_id = req.agent_id
    policy_mode = req.policy_mode
    autonomy_mode = req.autonomy_mode

    logger.info(
        "workflow_starting",
        extra=make_evidence_extra(
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            query_chars=len(query),
        ),
    )
    workflow_attrs = {
        "workflow_id": workflow_id,
        "workflow_type": "DynamicAskWorkflow",
        "task_queue": TASK_QUEUE_NAME,
    }
    start = time.perf_counter()

    try:
        with tracer.start_as_current_span("workflow_start") as span:
            span.set_attribute("temporal.workflow_id", workflow_id)
            span.set_attribute("temporal.workflow_type", "DynamicAskWorkflow")
            span.set_attribute("temporal.task_queue", TASK_QUEUE_NAME)
            # Governance span attributes (G7 fix — agent registry metadata in root span)
            set_governance_attributes(
                span,
                workflow_id=workflow_id,
                agent_id=agent_id,
                policy_mode=policy_mode,
                autonomy_mode=autonomy_mode,
            )

            client = await _client()

            handle = await client.start_workflow(
                DynamicAskWorkflow.run,
                {
                    "query": query,
                    "otel_context": inject_context(),
                    # Governance metadata propagated into workflow and all activities
                    "agent_id": agent_id,
                    "policy_mode": policy_mode,
                    "autonomy_mode": autonomy_mode,
                },
                id=workflow_id,
                task_queue=TASK_QUEUE_NAME,
            )
            instruments()["workflow_executions"].add(1, workflow_attrs)
            result = await handle.result()
    except Exception as e:
        instruments()["workflow_failures"].add(1, workflow_attrs)
        logger.exception(
            "workflow_failed",
            extra=make_evidence_extra(
                workflow_id=workflow_id,
                agent_id=agent_id,
                failure_reason=str(e)[:200],
            ),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Dynamic ask workflow failed",
                "workflow_id": workflow_id,
                "error": str(e),
                "temporal_url": _temporal_url(workflow_id),
            },
        ) from e

    duration = time.perf_counter() - start
    instruments()["workflow_duration"].record(duration, workflow_attrs)
    replay_ctx = result.get("replay_context", {})
    logger.info(
        "workflow_completed",
        extra=make_evidence_extra(
            workflow_id=workflow_id,
            agent_id=agent_id,
            synthesis_status=result.get("synthesis_status"),
            degradation_mode=replay_ctx.get("degradation_mode"),
            decision_path=replay_ctx.get("decision_path"),
            context_hash=replay_ctx.get("context_hash"),
            duration_seconds=round(duration, 4),
        ),
    )

    return {
        "workflow_id": workflow_id,
        "temporal_url": _temporal_url(workflow_id),
        "result": result,
        # Governance evidence surface — allows API consumers to correlate runs
        "replay_context": replay_ctx,
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
