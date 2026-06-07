"""Temporal activities that drive the AWCP agent loop via the MCP server.

Each activity is one logical step (reason / tool call / generate / admission
check) and acts as an MCP client. By default it spawns a LOCAL AWCP MCP server
over stdio. If AWCP_MCP_SSE_URL is set, it instead connects to a REMOTE MCP
server over SSE — which is how a teammate points their own Temporal at a shared
MCP server (e.g. exposed via ngrok). Either way the orchestration lives in the
Temporal workflow, so Temporal — not the agent — decides when a tool runs.
"""

import base64
import json
import logging
import os
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


logger = logging.getLogger(__name__)


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


async def _call_mcp(tool_name: str, arguments: dict) -> str:
    """Open an MCP session, call one tool, return its text output.

    One session per call keeps the activity stateless and idempotent.
    """
    logger.info("Starting MCP tool call: %s", tool_name)
    try:
        async with _mcp_session() as session:
            result = await session.call_tool(tool_name, arguments)

            text_parts = [
                block.text
                for block in result.content
                if getattr(block, "type", None) == "text"
            ]
            output = "\n".join(text_parts).strip()
            logger.info(
                "Completed MCP tool call: %s output_chars=%s",
                tool_name,
                len(output),
            )
            return output
    except Exception:
        logger.exception("MCP tool call failed: %s", tool_name)
        raise


@activity.defn
async def mcp_get_agent_info(agent_name: str) -> dict:
    """Admission control: fetch the agent manifest (incl. quarantine status)."""
    raw = await _call_mcp("get_agent_info", {"agent_name": agent_name})

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

    return await _call_mcp("agent_generate", arguments)


@activity.defn(name="call_llm")
async def mcp_call_llm(payload: dict) -> dict:
    """First attempt: ask the MCP-hosted LLM for a final answer if safe."""
    logger.info("Starting call_llm activity")
    raw = await _call_mcp("call_llm", {"query": payload["query"]})
    try:
        parsed = json.loads(raw)
        logger.info("Completed call_llm activity final=%s", parsed.get("final"))
        return parsed
    except (json.JSONDecodeError, TypeError):
        logger.exception("call_llm returned malformed JSON")
        return {
            "configured": False,
            "final": False,
            "answer": "",
            "reason": "MCP call_llm returned malformed JSON.",
            "raw": raw,
        }


@activity.defn(name="discover_tools")
async def mcp_discover_tools() -> list[dict]:
    """Discover runtime tools dynamically from the MCP server."""
    logger.info("Starting discover_tools activity")
    raw = await _call_mcp("list_runtime_tools", {})
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.exception("discover_tools returned malformed JSON")
        raise ValueError(f"MCP list_runtime_tools returned malformed JSON: {raw}")

    if not isinstance(parsed, list):
        raise ValueError("MCP list_runtime_tools did not return a list.")

    logger.info("Completed discover_tools activity count=%s", len(parsed))
    return parsed


@activity.defn(name="select_tools")
async def mcp_select_tools(payload: dict) -> dict:
    """Ask the MCP-hosted selector to choose from discovered tools."""
    logger.info("Starting select_tools activity")
    raw = await _call_mcp(
        "select_runtime_tools",
        {"query": payload["query"], "tools": payload["tools"]},
    )
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.exception("select_tools returned malformed JSON")
        raise ValueError(f"MCP select_runtime_tools returned malformed JSON: {raw}")

    calls = parsed.get("tool_calls", [])
    if not isinstance(calls, list):
        parsed["tool_calls"] = []

    logger.info("Completed select_tools activity selected=%s", len(parsed["tool_calls"]))
    return parsed


@activity.defn(name="run_tool")
async def mcp_run_tool(payload: dict) -> dict:
    """Run exactly one dynamically selected runtime tool via MCP."""
    tool_name = payload["tool_name"]
    logger.info("Starting run_tool activity tool_name=%s", tool_name)
    output = await _call_mcp(
        "execute_tool",
        {"tool_name": tool_name, "tool_input": payload.get("tool_input") or {}},
    )

    if output.startswith(f"Error executing tool '{tool_name}'"):
        raise RuntimeError(output)

    logger.info("Completed run_tool activity tool_name=%s output_chars=%s", tool_name, len(output))
    return {
        "tool_name": tool_name,
        "tool_input": payload.get("tool_input") or {},
        "output": output,
        "status": "succeeded",
    }


@activity.defn(name="synthesize_answer")
async def mcp_synthesize_answer(payload: dict) -> str:
    """Generate the final answer from collected tool outputs."""
    logger.info(
        "Starting synthesize_answer activity tool_result_count=%s",
        len(payload.get("tool_results") or []),
    )
    try:
        answer = await _call_mcp(
            "synthesize_tool_results",
            {"query": payload["query"], "tool_results": payload["tool_results"]},
        )
        logger.info("Completed synthesize_answer activity answer_chars=%s", len(answer))
        return answer
    except Exception:
        logger.exception("synthesize_answer failed")
        raise
