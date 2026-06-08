"""DynamicAskWorkflow — governance-aware durable natural-language query workflow.

Extends the base workflow with evidence metadata described in the AWCP Magazine:
  - agent_id, policy_mode, autonomy_mode propagated from API to every activity
  - context_hash (SHA-256) computed after tool selection
  - checkpoint_id injected into each activity payload for replay resume points
  - decision_path tracks the ordered sequence of steps actually executed
  - degradation_mode derived from tool failure count and synthesis outcome
  - replay_context block returned with every workflow result

The workflow itself stays deterministic: all LLM calls, MCP sessions, tool
discovery, and tool execution happen inside activities. Each selected tool
is scheduled as its own `run_tool` activity, so Temporal retries only the
failed tool activity and preserves completed workflow state.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from awcp.temporal.activities.mcp_gateway import (
        mcp_call_llm,
        mcp_discover_tools,
        mcp_run_tool,
        mcp_select_tools,
        mcp_synthesize_answer,
    )
    from awcp.temporal.workflows.base_workflow import (
        FAST_INTERNAL_RETRY,
        SYNTHESIS_RETRY,
        TOOL_EXECUTION_RETRY,
    )
    from awcp.observability.evidence import (
        build_context_hash,
        build_checkpoint_id,
        build_decision_path,
        determine_degradation_mode,
    )


@workflow.defn
class DynamicAskWorkflow:
    """Durable governed natural-language query workflow backed by the MCP server.

    Governance evidence fields (AWCP Magazine §01 Evidence Boundary):
      - context_hash      deterministic SHA-256 of governing inputs
      - checkpoint_id     step-scoped resume point identifier
      - decision_path     ordered sequence of steps executed
      - degradation_mode  autonomy level inferred from failure signals
    """

    @workflow.run
    async def run(self, workflow_input: dict) -> dict:
        query = workflow_input["query"].strip()
        otel_context = workflow_input.get("otel_context") or {}
        workflow_id = workflow.info().workflow_id

        # ── Governance defaults from API (G7 fix) ───────────────────────────
        agent_id = workflow_input.get("agent_id", "dynamic")
        policy_mode = workflow_input.get("policy_mode", "active")
        autonomy_mode = workflow_input.get("autonomy_mode", "active")

        # Track decision path — steps actually executed (G6 fix)
        decision_path_steps: list[str] = []
        tool_failure_count = 0
        synthesis_fallback = False

        # ── Step 1: LLM Decision (call_llm) ─────────────────────────────────
        decision_path_steps.append("llm_decision")
        first_attempt = await workflow.execute_activity(
            mcp_call_llm,
            {
                "query": query,
                "otel_context": otel_context,
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "policy_mode": policy_mode,
                "autonomy_mode": autonomy_mode,
                "checkpoint_id": build_checkpoint_id(workflow_id, "call_llm"),
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        if first_attempt.get("final") and first_attempt.get("answer"):
            degradation_mode = determine_degradation_mode(
                tool_failure_count=0, synthesis_fallback=False
            )
            context_hash = build_context_hash(workflow_id, query, [])
            return {
                "system_action": "SUCCESS",
                "path": "llm",
                "query": query,
                "answer": first_attempt["answer"],
                "llm": first_attempt,
                "tool_results": [],
                "replay_context": {
                    "context_hash": context_hash,
                    "checkpoint_id": build_checkpoint_id(workflow_id, "call_llm"),
                    "decision_path": build_decision_path(decision_path_steps),
                    "tool_execution_sequence": [],
                    "degradation_mode": degradation_mode,
                },
            }

        # ── Step 2: Tool Discovery ───────────────────────────────────────────
        decision_path_steps.append("tool_discovery")
        tools = await workflow.execute_activity(
            mcp_discover_tools,
            {
                "otel_context": otel_context,
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "policy_mode": policy_mode,
                "autonomy_mode": autonomy_mode,
                "checkpoint_id": build_checkpoint_id(workflow_id, "discover_tools"),
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        # ── Step 3: Tool Selection ───────────────────────────────────────────
        decision_path_steps.append("tool_selection")
        # Build context_hash now that we know the available tool set (G5 fix)
        available_tool_names = [t.get("name", "") for t in tools]
        context_hash = build_context_hash(workflow_id, query, available_tool_names)

        selection = await workflow.execute_activity(
            mcp_select_tools,
            {
                "query": query,
                "tools": tools,
                "otel_context": otel_context,
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "policy_mode": policy_mode,
                "autonomy_mode": autonomy_mode,
                "checkpoint_id": build_checkpoint_id(workflow_id, "select_tools"),
                "context_hash": context_hash,
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        # ── Step 4: Tool Execution (one activity per tool) ───────────────────
        tool_results: list[dict] = []
        tool_execution_sequence: list[str] = []

        for tool_call in selection.get("tool_calls", []):
            tool_name = tool_call.get("tool_name")
            if not tool_name:
                continue

            decision_path_steps.append(f"run_tool[{tool_name}]")
            tool_execution_sequence.append(tool_name)

            try:
                result = await workflow.execute_activity(
                    mcp_run_tool,
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_call.get("tool_input") or {},
                        "otel_context": otel_context,
                        "workflow_id": workflow_id,
                        "agent_id": agent_id,
                        "policy_mode": policy_mode,
                        "autonomy_mode": autonomy_mode,
                        "context_hash": context_hash,
                        "checkpoint_id": build_checkpoint_id(
                            workflow_id, f"run_tool[{tool_name}]"
                        ),
                    },
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=TOOL_EXECUTION_RETRY,
                )
                result["reason"] = tool_call.get("reason")
                tool_results.append(result)
            except ActivityError as e:
                tool_failure_count += 1
                tool_results.append(
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_call.get("tool_input") or {},
                        "status": "failed",
                        "error": str(e),
                        "reason": tool_call.get("reason"),
                    }
                )

        # ── Step 5: Synthesis ────────────────────────────────────────────────
        decision_path_steps.append("synthesis")
        # Compute degradation mode before synthesis so it flows into the span
        pre_synthesis_degradation = determine_degradation_mode(
            tool_failure_count=tool_failure_count, synthesis_fallback=False
        )

        synthesis_status = "succeeded"
        synthesis_error = None
        try:
            answer = await workflow.execute_activity(
                mcp_synthesize_answer,
                {
                    "query": query,
                    "tool_results": tool_results,
                    "otel_context": otel_context,
                    "workflow_id": workflow_id,
                    "agent_id": agent_id,
                    "policy_mode": policy_mode,
                    "autonomy_mode": autonomy_mode,
                    "context_hash": context_hash,
                    "checkpoint_id": build_checkpoint_id(workflow_id, "synthesize_answer"),
                    "degradation_mode": pre_synthesis_degradation,
                },
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=SYNTHESIS_RETRY,
            )
        except ActivityError as e:
            workflow.logger.warning(
                f"synthesize_answer failed; returning deterministic fallback. Error: {e}"
            )
            synthesis_status = "fallback"
            synthesis_fallback = True
            synthesis_error = str(e)
            answer = self._fallback_answer(query, tool_results)

        # ── Build final evidence/replay context block (G5 fix) ───────────────
        final_degradation_mode = determine_degradation_mode(
            tool_failure_count=tool_failure_count,
            synthesis_fallback=synthesis_fallback,
        )

        return {
            "system_action": "SUCCESS",
            "path": "tools",
            "query": query,
            "answer": answer,
            "synthesis_status": synthesis_status,
            "synthesis_error": synthesis_error,
            "llm": first_attempt,
            "discovered_tools": available_tool_names,
            "selection": selection,
            "tool_results": tool_results,
            # ── Governance evidence block (AWCP Magazine §05 Evidence Boundary)
            "replay_context": {
                "context_hash": context_hash,
                "checkpoint_id": build_checkpoint_id(workflow_id, "synthesize_answer"),
                "decision_path": build_decision_path(decision_path_steps),
                "tool_execution_sequence": tool_execution_sequence,
                "degradation_mode": final_degradation_mode,
            },
        }

    def _fallback_answer(self, query: str, tool_results: list[dict]) -> str:
        excerpts = []
        for result in tool_results:
            if result.get("status") != "succeeded":
                continue

            output = str(result.get("output", "")).strip()
            if not output:
                continue

            tool_name = result.get("tool_name", "tool")
            excerpts.append(f"{tool_name} result:\n{output[:1500]}")

        if not excerpts:
            return (
                "I could not synthesize a final answer, and the workflow did "
                "not have successful tool output to fall back to."
            )

        return (
            f"I could not complete final LLM synthesis for: {query}\n\n"
            "Here is the most relevant fetched tool output:\n\n"
            + "\n\n".join(excerpts)
        )
