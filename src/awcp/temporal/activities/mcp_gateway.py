"""Temporal activities that drive the AWCP agent loop via the MCP server.

Each activity is one logical step (reason / tool call / generate / admission
check) and acts as an MCP client. By default it spawns a LOCAL AWCP MCP server
over stdio. If AWCP_MCP_SSE_URL is set, it instead connects to a REMOTE MCP
server over SSE — which is how a teammate points their own Temporal at a shared
MCP server (e.g. exposed via ngrok). Either way the orchestration lives in the
Temporal workflow, so Temporal — not the agent — decides when a tool runs.

Governance telemetry (AWCP Magazine §01 Operating Model):
  - All spans carry governance attributes via ``set_governance_attributes``
  - All logs use ``make_evidence_extra`` for Loki structured-field extraction
  - Degradation mode is tracked and surfaced on failures
  - Tool timeouts are counted separately from generic failures
"""

import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from temporalio import activity

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from awcp.temporal.config import (
    MCP_PYTHON,
    MCP_SERVER_ARGS,
    MCP_WORKDIR,
    SRC_DIR,
    MCP_SSE_URL,
    MCP_SSE_AUTH,
)
from awcp.observability import (
    configure_observability,
    get_tracer,
    extract_context,
)
from awcp.observability.metrics import instruments
from awcp.observability.evidence import (
    build_checkpoint_id,
    make_evidence_extra,
    set_governance_attributes,
    DEGRADATION_LIMITED,
    DEGRADATION_RECOMMENDATION_ONLY,
)

# Initialise OTel for the worker process (safe to call multiple times — idempotent).
configure_observability(component="temporal-worker")

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)


# ---------------------------------------------------------------------------
# MCP session helpers
# ---------------------------------------------------------------------------

def _server_params() -> StdioServerParameters:
    # Inherit the current environment but ensure `src` is importable so the
    # spawned `python -m awcp.mcp.server` subprocess can resolve the package.
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + existing if existing else "")

    return StdioServerParameters(
        command=MCP_PYTHON,
        args=MCP_SERVER_ARGS,
        cwd=MCP_WORKDIR,
        env=env,
    )


def _sse_headers() -> dict:
    # Skip ngrok's browser-interstitial for programmatic requests, and add
    # basic-auth if the shared tunnel is protected.
    headers = {"ngrok-skip-browser-warning": "true"}
    if MCP_SSE_AUTH:
        token = base64.b64encode(MCP_SSE_AUTH.encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


@asynccontextmanager
async def _mcp_session():
    """Yield an initialized MCP session over SSE (if configured) or stdio."""
    if MCP_SSE_URL:
        async with sse_client(MCP_SSE_URL, headers=_sse_headers()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        async with stdio_client(_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _call_mcp(
    tool_name: str,
    arguments: dict,
    *,
    workflow_id: str = "unknown",
    agent_id: str = "unknown",
    activity_name: str = "mcp_tool_call",
) -> str:
    """Open an MCP session, call one tool, return its text output.

    One session per call keeps the activity stateless and idempotent.
    Wraps the call in a child OTel span so every MCP hop is visible in Tempo.
    Governance attributes and structured logs are always included.
    """
    logger.info(
        "mcp_tool_call_started",
        extra=make_evidence_extra(
            tool_name=tool_name,
            workflow_id=workflow_id,
            agent_id=agent_id,
            activity=activity_name,
            transport="sse" if MCP_SSE_URL else "stdio",
        ),
    )
    start = time.perf_counter()
    m = instruments()

    with tracer.start_as_current_span("mcp_tool_call") as span:
        span.set_attribute("mcp.tool_name", tool_name)
        span.set_attribute("mcp.transport", "sse" if MCP_SSE_URL else "stdio")
        set_governance_attributes(
            span,
            workflow_id=workflow_id,
            agent_id=agent_id,
            tool_name=tool_name,
        )
        try:
            async with _mcp_session() as session:
                result = await session.call_tool(tool_name, arguments)

                text_parts = [
                    block.text
                    for block in result.content
                    if getattr(block, "type", None) == "text"
                ]
                output = "\n".join(text_parts).strip()
                duration = time.perf_counter() - start
                span.set_attribute("mcp.output_chars", len(output))
                tool_attrs = {"tool_name": tool_name, "workflow_id": workflow_id}
                m["mcp_tool_calls"].add(1, tool_attrs)
                m["mcp_tool_duration"].record(duration, tool_attrs)
                logger.info(
                    "mcp_tool_call_completed",
                    extra=make_evidence_extra(
                        tool_name=tool_name,
                        workflow_id=workflow_id,
                        agent_id=agent_id,
                        activity=activity_name,
                        output_chars=len(output),
                        duration_seconds=round(duration, 4),
                    ),
                )
                return output
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            exc_str = str(exc)
            # Detect timeout signals for the tool_timeouts governance metric
            is_timeout = any(
                kw in type(exc).__name__.upper() or kw in exc_str.upper()
                for kw in ("TIMEOUT", "DEADLINE", "TIMED_OUT", "START_TO_CLOSE")
            )
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, exc_str))
            except ImportError:
                pass
            m["mcp_tool_failures"].add(1, {"tool_name": tool_name, "workflow_id": workflow_id})
            m["mcp_tool_duration"].record(duration, {"tool_name": tool_name, "workflow_id": workflow_id})
            if is_timeout:
                m["tool_timeouts"].add(1, {"tool_name": tool_name, "activity": activity_name})
            logger.exception(
                "mcp_tool_call_failed",
                extra=make_evidence_extra(
                    tool_name=tool_name,
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    activity=activity_name,
                    is_timeout=is_timeout,
                    failure_reason=exc_str[:200],
                ),
            )
            raise


# ---------------------------------------------------------------------------
# Helper: restore propagated OTel context from workflow input
# ---------------------------------------------------------------------------

def _restore_context(payload):
    """Extract and activate the OTel trace context propagated from the API."""
    otel_ctx = None
    if isinstance(payload, dict):
        otel_ctx = extract_context(payload.get("otel_context"))
    return otel_ctx


# ---------------------------------------------------------------------------
# Legacy governance activities (AgentGovernanceWorkflow)
# ---------------------------------------------------------------------------

@activity.defn
async def mcp_get_agent_info(agent_name: str) -> dict:
    """Admission control: fetch the agent manifest (incl. quarantine status)."""
    raw = await _call_mcp(
        "get_agent_info",
        {"agent_name": agent_name},
        workflow_id="governance",
        agent_id=agent_name,
        activity_name="mcp_get_agent_info",
    )

    if raw.startswith("Agent '") and "not found" in raw:
        raise ValueError(raw)

    # Parse the "Key: Value" manifest lines into a dict (lowercased keys).
    manifest: dict = {"name": agent_name, "raw": raw}
    for line in raw.splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            manifest[key.strip().lower()] = value.strip()

    return manifest


@activity.defn
async def mcp_agent_route(payload: dict) -> dict:
    """Reasoning step: decide SEARCH vs ANSWER for the prompt."""
    raw = await _call_mcp(
        "agent_route",
        {"agent_name": payload["agent_name"], "prompt": payload["input"]},
        workflow_id=payload.get("workflow_id", "unknown"),
        agent_id=payload.get("agent_name", "unknown"),
        activity_name="mcp_agent_route",
    )
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Fail safe: if routing output is malformed, answer directly.
        return {"action": "ANSWER", "raw": raw}


@activity.defn
async def mcp_execute_tool(payload: dict) -> str:
    """Tool Executor step: run a single registered tool (e.g. web_search)."""
    return await _call_mcp(
        "execute_tool",
        {"tool_name": payload["tool_name"], "tool_input": payload["tool_input"]},
        workflow_id=payload.get("workflow_id", "unknown"),
        agent_id=payload.get("agent_id", "unknown"),
        activity_name="mcp_execute_tool",
    )


@activity.defn
async def mcp_agent_generate(payload: dict) -> str:
    """Generation/synthesis step. Grounds the answer if search_results given."""
    arguments = {
        "agent_name": payload["agent_name"],
        "prompt": payload["input"],
    }
    if payload.get("search_results"):
        arguments["search_results"] = payload["search_results"]

    return await _call_mcp(
        "agent_generate",
        arguments,
        workflow_id=payload.get("workflow_id", "unknown"),
        agent_id=payload.get("agent_name", "unknown"),
        activity_name="mcp_agent_generate",
    )


# ---------------------------------------------------------------------------
# DynamicAskWorkflow activities — governance OTel-instrumented
# ---------------------------------------------------------------------------

@activity.defn(name="call_llm")
async def mcp_call_llm(payload: dict) -> dict:
    """First attempt: ask the MCP-hosted LLM for a final answer if safe."""
    workflow_id = payload.get("workflow_id", "unknown")
    agent_id = payload.get("agent_id", "dynamic")
    policy_mode = payload.get("policy_mode", "active")
    autonomy_mode = payload.get("autonomy_mode", "active")
    checkpoint_id = build_checkpoint_id(workflow_id, "call_llm")
    context_hash = payload.get("context_hash")

    m = instruments()
    act_attrs = {"activity": "call_llm", "workflow_id": workflow_id}

    logger.info(
        "activity_started",
        extra=make_evidence_extra(
            activity="call_llm",
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        ),
    )
    m["activity_executions"].add(1, act_attrs)
    start = time.perf_counter()

    with tracer.start_as_current_span("call_llm") as span:
        span.set_attribute("temporal.activity", "call_llm")
        span.set_attribute("temporal.workflow_id", workflow_id)
        set_governance_attributes(
            span,
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        )
        try:
            raw = await _call_mcp(
                "call_llm",
                {"query": payload["query"]},
                workflow_id=workflow_id,
                agent_id=agent_id,
                activity_name="call_llm",
            )
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.exception(
                    "call_llm_json_parse_failed",
                    extra=make_evidence_extra(
                        workflow_id=workflow_id,
                        agent_id=agent_id,
                        checkpoint_id=checkpoint_id,
                    ),
                )
                parsed = {
                    "configured": False,
                    "final": False,
                    "answer": "",
                    "reason": "MCP call_llm returned malformed JSON.",
                    "raw": raw,
                }
            duration = time.perf_counter() - start
            span.set_attribute("llm.final", bool(parsed.get("final")))
            m["activity_duration"].record(duration, act_attrs)
            logger.info(
                "activity_completed",
                extra=make_evidence_extra(
                    activity="call_llm",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    llm_final=parsed.get("final"),
                    duration_seconds=round(duration, 4),
                    checkpoint_id=checkpoint_id,
                ),
            )
            return parsed
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except ImportError:
                pass
            m["activity_failures"].add(1, act_attrs)
            m["activity_duration"].record(duration, act_attrs)
            logger.exception(
                "activity_failed",
                extra=make_evidence_extra(
                    activity="call_llm",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    failure_reason=str(exc)[:200],
                    checkpoint_id=checkpoint_id,
                ),
            )
            raise


@activity.defn(name="discover_tools")
async def mcp_discover_tools(payload: dict = None) -> list[dict]:
    """Discover runtime tools dynamically from the MCP server."""
    payload = payload or {}
    workflow_id = payload.get("workflow_id", "unknown")
    agent_id = payload.get("agent_id", "dynamic")
    policy_mode = payload.get("policy_mode", "active")
    autonomy_mode = payload.get("autonomy_mode", "active")
    checkpoint_id = build_checkpoint_id(workflow_id, "discover_tools")
    context_hash = payload.get("context_hash")

    m = instruments()
    act_attrs = {"activity": "discover_tools", "workflow_id": workflow_id}

    logger.info(
        "activity_started",
        extra=make_evidence_extra(
            activity="discover_tools",
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        ),
    )
    m["activity_executions"].add(1, act_attrs)
    start = time.perf_counter()

    with tracer.start_as_current_span("discover_tools") as span:
        span.set_attribute("temporal.activity", "discover_tools")
        span.set_attribute("temporal.workflow_id", workflow_id)
        set_governance_attributes(
            span,
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        )
        try:
            raw = await _call_mcp(
                "list_runtime_tools",
                {},
                workflow_id=workflow_id,
                agent_id=agent_id,
                activity_name="discover_tools",
            )
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.exception(
                    "discover_tools_json_parse_failed",
                    extra=make_evidence_extra(
                        workflow_id=workflow_id,
                        agent_id=agent_id,
                        checkpoint_id=checkpoint_id,
                    ),
                )
                raise ValueError(f"MCP list_runtime_tools returned malformed JSON: {raw}")

            if not isinstance(parsed, list):
                raise ValueError("MCP list_runtime_tools did not return a list.")

            duration = time.perf_counter() - start
            span.set_attribute("tools.count", len(parsed))
            m["activity_duration"].record(duration, act_attrs)
            logger.info(
                "activity_completed",
                extra=make_evidence_extra(
                    activity="discover_tools",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    tool_count=len(parsed),
                    duration_seconds=round(duration, 4),
                    checkpoint_id=checkpoint_id,
                ),
            )
            return parsed
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except ImportError:
                pass
            m["activity_failures"].add(1, act_attrs)
            m["activity_duration"].record(duration, act_attrs)
            logger.exception(
                "activity_failed",
                extra=make_evidence_extra(
                    activity="discover_tools",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    failure_reason=str(exc)[:200],
                    checkpoint_id=checkpoint_id,
                ),
            )
            raise


@activity.defn(name="select_tools")
async def mcp_select_tools(payload: dict) -> dict:
    """Ask the MCP-hosted selector to choose from discovered tools."""
    workflow_id = payload.get("workflow_id", "unknown")
    agent_id = payload.get("agent_id", "dynamic")
    policy_mode = payload.get("policy_mode", "active")
    autonomy_mode = payload.get("autonomy_mode", "active")
    checkpoint_id = build_checkpoint_id(workflow_id, "select_tools")
    context_hash = payload.get("context_hash")

    m = instruments()
    act_attrs = {"activity": "select_tools", "workflow_id": workflow_id}

    logger.info(
        "activity_started",
        extra=make_evidence_extra(
            activity="select_tools",
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        ),
    )
    m["activity_executions"].add(1, act_attrs)
    start = time.perf_counter()

    with tracer.start_as_current_span("select_tools") as span:
        span.set_attribute("temporal.activity", "select_tools")
        span.set_attribute("temporal.workflow_id", workflow_id)
        set_governance_attributes(
            span,
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        )
        try:
            raw = await _call_mcp(
                "select_runtime_tools",
                {"query": payload["query"], "tools": payload["tools"]},
                workflow_id=workflow_id,
                agent_id=agent_id,
                activity_name="select_tools",
            )
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.exception(
                    "select_tools_json_parse_failed",
                    extra=make_evidence_extra(
                        workflow_id=workflow_id,
                        agent_id=agent_id,
                        checkpoint_id=checkpoint_id,
                    ),
                )
                raise ValueError(f"MCP select_runtime_tools returned malformed JSON: {raw}")

            calls = parsed.get("tool_calls", [])
            if not isinstance(calls, list):
                parsed["tool_calls"] = []

            duration = time.perf_counter() - start
            selected_count = len(parsed.get("tool_calls", []))
            selected_names = [
                tc.get("tool_name", "unknown")
                for tc in parsed.get("tool_calls", [])
            ]
            span.set_attribute("tools.selected_count", selected_count)
            span.set_attribute("tools.selected_names", ",".join(selected_names))
            m["activity_duration"].record(duration, act_attrs)
            logger.info(
                "activity_completed",
                extra=make_evidence_extra(
                    activity="select_tools",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    tools_selected=selected_count,
                    tool_names=",".join(selected_names),
                    duration_seconds=round(duration, 4),
                    checkpoint_id=checkpoint_id,
                    context_hash=context_hash,
                ),
            )
            return parsed
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except ImportError:
                pass
            m["activity_failures"].add(1, act_attrs)
            m["activity_duration"].record(duration, act_attrs)
            logger.exception(
                "activity_failed",
                extra=make_evidence_extra(
                    activity="select_tools",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    failure_reason=str(exc)[:200],
                    checkpoint_id=checkpoint_id,
                ),
            )
            raise


@activity.defn(name="run_tool")
async def mcp_run_tool(payload: dict) -> dict:
    """Run exactly one dynamically selected runtime tool via MCP."""
    tool_name = payload["tool_name"]
    workflow_id = payload.get("workflow_id", "unknown")
    agent_id = payload.get("agent_id", "dynamic")
    policy_mode = payload.get("policy_mode", "active")
    autonomy_mode = payload.get("autonomy_mode", "active")
    context_hash = payload.get("context_hash")
    checkpoint_id = build_checkpoint_id(workflow_id, f"run_tool[{tool_name}]")

    m = instruments()
    # Retry count is tracked via Temporal's activity info
    attempt = activity.info().attempt
    act_attrs = {
        "activity": "run_tool",
        "tool_name": tool_name,
        "workflow_id": workflow_id,
    }

    if attempt > 1:
        m["activity_retries"].add(1, act_attrs)
        m["workflow_retries"].add(1, {"workflow_id": workflow_id, "activity": "run_tool"})
        logger.info(
            "activity_retry",
            extra=make_evidence_extra(
                activity="run_tool",
                tool_name=tool_name,
                workflow_id=workflow_id,
                agent_id=agent_id,
                retry_count=attempt,
                checkpoint_id=checkpoint_id,
            ),
        )

    logger.info(
        "activity_started",
        extra=make_evidence_extra(
            activity="run_tool",
            tool_name=tool_name,
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            retry_count=attempt,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        ),
    )
    m["activity_executions"].add(1, act_attrs)
    start = time.perf_counter()

    with tracer.start_as_current_span("run_tool") as span:
        span.set_attribute("temporal.activity", "run_tool")
        span.set_attribute("temporal.workflow_id", workflow_id)
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("temporal.attempt", attempt)
        set_governance_attributes(
            span,
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            tool_name=tool_name,
            tool_type="read",           # web_search is read-only; write_capable=False
            write_capable=False,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
            retry_count=attempt,
            degradation_mode="normal",  # presumed normal at start; updated on failure
        )
        try:
            output = await _call_mcp(
                "execute_tool",
                {"tool_name": tool_name, "tool_input": payload.get("tool_input") or {}},
                workflow_id=workflow_id,
                agent_id=agent_id,
                activity_name="run_tool",
            )

            if output.startswith(f"Error executing tool '{tool_name}'"):
                raise RuntimeError(output)

            duration = time.perf_counter() - start
            span.set_attribute("tool.output_chars", len(output))
            m["activity_duration"].record(duration, act_attrs)
            logger.info(
                "activity_completed",
                extra=make_evidence_extra(
                    activity="run_tool",
                    tool_name=tool_name,
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    output_chars=len(output),
                    duration_seconds=round(duration, 4),
                    checkpoint_id=checkpoint_id,
                    degradation_mode="normal",
                ),
            )
            return {
                "tool_name": tool_name,
                "tool_input": payload.get("tool_input") or {},
                "output": output,
                "status": "succeeded",
            }
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            # Tool failure → degradation_mode escalates to "limited"
            set_governance_attributes(span, degradation_mode=DEGRADATION_LIMITED)
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except ImportError:
                pass
            m["activity_failures"].add(1, act_attrs)
            m["activity_duration"].record(duration, act_attrs)
            m["degradation_events"].add(1, {
                "degradation_mode": DEGRADATION_LIMITED,
                "workflow_id": workflow_id,
            })
            logger.exception(
                "activity_failed",
                extra=make_evidence_extra(
                    activity="run_tool",
                    tool_name=tool_name,
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    degradation_mode=DEGRADATION_LIMITED,
                    failure_reason=str(exc)[:200],
                    retry_count=attempt,
                    checkpoint_id=checkpoint_id,
                ),
            )
            raise


@activity.defn(name="synthesize_answer")
async def mcp_synthesize_answer(payload: dict) -> str:
    """Generate the final answer from collected tool outputs."""
    workflow_id = payload.get("workflow_id", "unknown")
    agent_id = payload.get("agent_id", "dynamic")
    policy_mode = payload.get("policy_mode", "active")
    autonomy_mode = payload.get("autonomy_mode", "active")
    context_hash = payload.get("context_hash")
    checkpoint_id = build_checkpoint_id(workflow_id, "synthesize_answer")
    tool_result_count = len(payload.get("tool_results") or [])
    degradation_mode = payload.get("degradation_mode", "normal")

    m = instruments()
    act_attrs = {"activity": "synthesize_answer", "workflow_id": workflow_id}

    logger.info(
        "activity_started",
        extra=make_evidence_extra(
            activity="synthesize_answer",
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            tool_result_count=tool_result_count,
            degradation_mode=degradation_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        ),
    )
    m["activity_executions"].add(1, act_attrs)
    start = time.perf_counter()

    with tracer.start_as_current_span("synthesize_answer") as span:
        span.set_attribute("temporal.activity", "synthesize_answer")
        span.set_attribute("temporal.workflow_id", workflow_id)
        span.set_attribute("synthesis.tool_result_count", tool_result_count)
        set_governance_attributes(
            span,
            workflow_id=workflow_id,
            agent_id=agent_id,
            policy_mode=policy_mode,
            autonomy_mode=autonomy_mode,
            degradation_mode=degradation_mode,
            checkpoint_id=checkpoint_id,
            context_hash=context_hash,
        )
        try:
            answer = await _call_mcp(
                "synthesize_tool_results",
                {"query": payload["query"], "tool_results": payload["tool_results"]},
                workflow_id=workflow_id,
                agent_id=agent_id,
                activity_name="synthesize_answer",
            )
            duration = time.perf_counter() - start
            span.set_attribute("synthesis.answer_chars", len(answer))
            # Record replay context — evidence packet generated for this branch
            m["replay_context_generated"].add(1, {
                "workflow_id": workflow_id,
                "degradation_mode": degradation_mode,
            })
            m["activity_duration"].record(duration, act_attrs)
            logger.info(
                "activity_completed",
                extra=make_evidence_extra(
                    activity="synthesize_answer",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    answer_chars=len(answer),
                    duration_seconds=round(duration, 4),
                    degradation_mode=degradation_mode,
                    checkpoint_id=checkpoint_id,
                    context_hash=context_hash,
                ),
            )
            return answer
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            # Synthesis failure → degradation escalates to recommendation_only
            set_governance_attributes(
                span, degradation_mode=DEGRADATION_RECOMMENDATION_ONLY
            )
            m["degradation_events"].add(1, {
                "degradation_mode": DEGRADATION_RECOMMENDATION_ONLY,
                "workflow_id": workflow_id,
            })
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except ImportError:
                pass
            m["activity_failures"].add(1, act_attrs)
            m["activity_duration"].record(duration, act_attrs)
            logger.exception(
                "activity_failed",
                extra=make_evidence_extra(
                    activity="synthesize_answer",
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    degradation_mode=DEGRADATION_RECOMMENDATION_ONLY,
                    failure_reason=str(exc)[:200],
                    checkpoint_id=checkpoint_id,
                    context_hash=context_hash,
                ),
            )
            raise
