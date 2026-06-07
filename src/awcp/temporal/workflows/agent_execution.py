from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

# Import activities and policies safely for Temporal's deterministic sandbox
with workflow.unsafe.imports_passed_through():
    from awcp.temporal.activities.mcp_gateway import (
        mcp_get_agent_info,
        mcp_agent_route,
        mcp_execute_tool,
        mcp_agent_generate,
    )
    from awcp.temporal.workflows.base_workflow import (
        FAST_INTERNAL_RETRY,
        AGENT_EXECUTION_RETRY,
    )


@workflow.defn
class AgentGovernanceWorkflow:
    """Orchestrates the agent loop step-by-step over the MCP server.

    Unlike the old FastAPI path (one opaque activity), Temporal here drives each
    stage as its own activity: admission -> reason -> [policy gate] -> tool ->
    generate. The control plane (this workflow) owns the decision of whether a
    tool actually runs, and degrades autonomy on failure.
    """

    @workflow.run
    async def run(self, workflow_input: dict) -> dict:
        agent_name = workflow_input["agent_name"]
        user_input = workflow_input["input"]

        # STEP 1: Admission Control (Registry / Quarantine status via MCP)
        registry_entry = await workflow.execute_activity(
            mcp_get_agent_info,
            agent_name,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        # STEP 2: The Quarantine Gate
        if registry_entry.get("status") == "quarantined":
            return {
                "system_action": "BLOCKED",
                "reason": f"Agent {agent_name} is in QUARANTINE. Execution denied by Control Plane.",
                "agent_details": registry_entry,
            }

        # STEP 3: Reasoning step — does the agent need a tool?
        decision = await workflow.execute_activity(
            mcp_agent_route,
            {"agent_name": agent_name, "input": user_input},
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        # STEP 4: Execution with Graceful Degradation (Failure Budget).
        # First attempt runs with full autonomy (tool calls allowed). If the
        # branch fails, we strip tool permissions and retry once.
        try:
            result = await self._execute_branch(
                agent_name, user_input, decision, autonomy_profile="active"
            )
            return {"system_action": "SUCCESS", "result": result}

        except ActivityError as e:
            workflow.logger.warn(
                f"Agent failed in active mode. Degrading to recommendation_only. Error: {str(e)}"
            )

            try:
                degraded = await self._execute_branch(
                    agent_name,
                    user_input,
                    decision,
                    autonomy_profile="recommendation_only",
                )
                return {"system_action": "DEGRADED_SUCCESS", "result": degraded}
            except ActivityError as final_error:
                return {
                    "system_action": "FATAL_FAILURE",
                    "reason": "Agent failed even in recommendation_only mode.",
                    "error": str(final_error),
                }

    async def _execute_branch(
        self,
        agent_name: str,
        user_input: str,
        decision: dict,
        autonomy_profile: str,
    ) -> dict:
        """Policy Approval Gate + execution for one autonomy profile.

        - SEARCH + active            -> Temporal calls the tool, then synthesizes
        - SEARCH + recommendation    -> tool call BLOCKED, answer from knowledge
        - ANSWER                     -> direct generation
        """
        action = decision.get("action", "ANSWER")
        tool_used = False
        search_results = None

        if action == "SEARCH" and autonomy_profile == "active":
            # --- Policy gate APPROVES the write/tool call ---
            # Tool + input come from the agent's routing decision, so any
            # tool-using agent works without changing this workflow.
            search_query = decision.get("search_query", user_input)
            tool_name = decision.get("tool_name", "web_search")
            tool_input = decision.get("tool_input") or {"query": search_query}
            search_results = await workflow.execute_activity(
                mcp_execute_tool,
                {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=AGENT_EXECUTION_RETRY,
            )
            tool_used = True
        # else: recommendation_only SKIPS the tool call (gate denies it), or the
        # agent chose ANSWER — either way we generate without grounding.

        answer = await workflow.execute_activity(
            mcp_agent_generate,
            {
                "agent_name": agent_name,
                "input": user_input,
                "search_results": search_results,
            },
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=AGENT_EXECUTION_RETRY,
        )

        return {
            "input": user_input,
            "output": answer,
            "agent": agent_name,
            "autonomy_profile": autonomy_profile,
            "action": action,
            "tool_used": tool_used,
            "tool_name": decision.get("tool_name", "web_search") if tool_used else None,
            "search_query": decision.get("search_query") if tool_used else None,
        }
