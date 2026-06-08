from functools import lru_cache

from awcp.observability import get_meter


@lru_cache(maxsize=1)
def instruments():
    meter = get_meter("awcp")
    return {
        "http_requests": meter.create_counter(
            "awcp_http_requests_total",
            description="Total FastAPI requests.",
        ),
        "http_errors": meter.create_counter(
            "awcp_http_errors_total",
            description="Total FastAPI request errors.",
        ),
        "http_duration": meter.create_histogram(
            "awcp_http_request_duration_seconds",
            unit="s",
            description="FastAPI request duration.",
        ),
        "workflow_executions": meter.create_counter(
            "awcp_workflow_executions_total",
            description="Total Temporal workflow executions started by AWCP.",
        ),
        "workflow_failures": meter.create_counter(
            "awcp_workflow_failures_total",
            description="Total Temporal workflow failures observed by AWCP.",
        ),
        "workflow_duration": meter.create_histogram(
            "awcp_workflow_duration_seconds",
            unit="s",
            description="Temporal workflow duration observed by AWCP.",
        ),
        "activity_executions": meter.create_counter(
            "awcp_activity_executions_total",
            description="Total Temporal activity attempts.",
        ),
        "activity_failures": meter.create_counter(
            "awcp_activity_failures_total",
            description="Total Temporal activity failures.",
        ),
        "activity_retries": meter.create_counter(
            "awcp_activity_retries_total",
            description="Total Temporal activity retry attempts.",
        ),
        "activity_duration": meter.create_histogram(
            "awcp_activity_duration_seconds",
            unit="s",
            description="Temporal activity attempt duration.",
        ),
        "mcp_tool_calls": meter.create_counter(
            "awcp_mcp_tool_calls_total",
            description="Total MCP tool calls.",
        ),
        "mcp_tool_failures": meter.create_counter(
            "awcp_mcp_tool_failures_total",
            description="Total MCP tool failures.",
        ),
        "mcp_tool_duration": meter.create_histogram(
            "awcp_mcp_tool_duration_seconds",
            unit="s",
            description="MCP tool call duration.",
        ),
        "runtime_tool_calls": meter.create_counter(
            "awcp_runtime_tool_calls_total",
            description="Total dynamically registered runtime tool calls.",
        ),
        "runtime_tool_failures": meter.create_counter(
            "awcp_runtime_tool_failures_total",
            description="Total dynamically registered runtime tool failures.",
        ),
        "runtime_tool_duration": meter.create_histogram(
            "awcp_runtime_tool_duration_seconds",
            unit="s",
            description="Runtime tool execution duration.",
        ),
        # ── Governance metrics (AWCP Magazine §01 Operating Model) ──────────
        "policy_denials": meter.create_counter(
            "awcp_policy_denials_total",
            description=(
                "Total write-capable actions blocked by policy. "
                "Labels: agent_id, tool_name, policy_mode."
            ),
        ),
        "degradation_events": meter.create_counter(
            "awcp_degradation_events_total",
            description=(
                "Total autonomy reduction events. "
                "Labels: degradation_mode (limited|recommendation_only|safe_mode), workflow_id."
            ),
        ),
        "workflow_retries": meter.create_counter(
            "awcp_workflow_retries_total",
            description=(
                "Total workflow-level retry attempts (distinct from activity retries). "
                "Labels: workflow_id, activity."
            ),
        ),
        "tool_timeouts": meter.create_counter(
            "awcp_tool_timeouts_total",
            description=(
                "Total tool calls that hit a timeout. "
                "Labels: tool_name, activity."
            ),
        ),
        "replay_context_generated": meter.create_counter(
            "awcp_replay_context_generated_total",
            description=(
                "Total evidence/replay context packets generated. "
                "Incremented once per completed workflow branch. "
                "Labels: workflow_id, degradation_mode."
            ),
        ),
    }
