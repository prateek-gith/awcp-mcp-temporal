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


@workflow.defn
class DynamicAskWorkflow:
    """Durable natural-language query workflow backed by the MCP server.

    The workflow itself stays deterministic: all LLM calls, MCP sessions, tool
    discovery, and tool execution happen inside activities. Each selected tool
    is scheduled as its own `run_tool` activity, so Temporal retries only the
    failed tool activity and preserves completed workflow state.
    """

    @workflow.run
    async def run(self, workflow_input: dict) -> dict:
        query = workflow_input["query"].strip()

        first_attempt = await workflow.execute_activity(
            mcp_call_llm,
            {"query": query},
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        if first_attempt.get("final") and first_attempt.get("answer"):
            return {
                "system_action": "SUCCESS",
                "path": "llm",
                "query": query,
                "answer": first_attempt["answer"],
                "llm": first_attempt,
                "tool_results": [],
            }

        tools = await workflow.execute_activity(
            mcp_discover_tools,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        selection = await workflow.execute_activity(
            mcp_select_tools,
            {"query": query, "tools": tools},
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=FAST_INTERNAL_RETRY,
        )

        tool_results: list[dict] = []
        for tool_call in selection.get("tool_calls", []):
            tool_name = tool_call.get("tool_name")
            if not tool_name:
                continue

            try:
                result = await workflow.execute_activity(
                    mcp_run_tool,
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_call.get("tool_input") or {},
                    },
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=TOOL_EXECUTION_RETRY,
                )
                result["reason"] = tool_call.get("reason")
                tool_results.append(result)
            except ActivityError as e:
                tool_results.append(
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_call.get("tool_input") or {},
                        "status": "failed",
                        "error": str(e),
                        "reason": tool_call.get("reason"),
                    }
                )

        synthesis_status = "succeeded"
        synthesis_error = None
        try:
            answer = await workflow.execute_activity(
                mcp_synthesize_answer,
                {"query": query, "tool_results": tool_results},
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=SYNTHESIS_RETRY,
            )
        except ActivityError as e:
            workflow.logger.warning(
                f"synthesize_answer failed; returning deterministic fallback. Error: {e}"
            )
            synthesis_status = "fallback"
            synthesis_error = str(e)
            answer = self._fallback_answer(query, tool_results)

        return {
            "system_action": "SUCCESS",
            "path": "tools",
            "query": query,
            "answer": answer,
            "synthesis_status": synthesis_status,
            "synthesis_error": synthesis_error,
            "llm": first_attempt,
            "discovered_tools": [tool.get("name") for tool in tools],
            "selection": selection,
            "tool_results": tool_results,
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
