"""Search-augmented agent that uses the ADVANCED (DDGS + Groq) search tool.

Identical reasoning to ``ollama-search`` — it reuses that agent's ``route``
function so there is no duplicated routing logic — but it declares
``tool="advanced_web_search"`` instead of plain ``web_search``. That single
declaration is enough for the control plane (Temporal + MCP) to drive the
advanced tool: ``agent_route`` attaches the agent's declared tool to a SEARCH
decision, and the workflow records that tool call as its own activity.

The Groq API key is not handled here — the tool itself reads ``groq_api_key``
(call-time arg) or the ``GROQ_API_KEY`` env var, and falls back to DuckDuckGo
only when no key is available. Nothing is hardcoded.
"""

from typing import Any

from awcp.agents.base import AgentSpec
from awcp.agents.ollama_search import route, build_search_answer_prompt
from awcp.runtime.config import SEARCH_MODEL
from awcp.runtime.ollama_client import ask_ollama
from awcp.runtime.schemas import PromptRequest
from awcp.runtime.tool_runtime import execute_tool


TOOL_NAME = "advanced_web_search"


def run(req: PromptRequest) -> dict[str, Any]:
    """Direct REST-path handler (mirrors ollama-search, advanced tool)."""
    decision = route(req.input)

    if decision.get("action") == "SEARCH":
        search_query = decision.get("search_query", req.input)
        results = execute_tool(TOOL_NAME, {"query": search_query})

        if results:
            prompt = build_search_answer_prompt(req.input, str(results))
            output = ask_ollama(prompt, SEARCH_MODEL)
            return {
                "input": req.input,
                "model": SEARCH_MODEL,
                "output": output,
                "search_used": True,
                "search_query": search_query,
                "tool": TOOL_NAME,
            }

    output = ask_ollama(req.input, SEARCH_MODEL)
    return {
        "input": req.input,
        "model": SEARCH_MODEL,
        "output": output,
        "search_used": False,
    }


AGENT = AgentSpec(
    name="ollama-advanced",
    route="/chat/ollama-advanced",
    request_model=PromptRequest,
    handler=run,
    runtime="ollama",
    model=SEARCH_MODEL,
    router=route,
    tool="advanced_web_search",
)
