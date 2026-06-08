import asyncio
import inspect
import json
import logging
import os
import sys
import time
from typing import Annotated, Any

from dotenv import load_dotenv
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from starlette.responses import HTMLResponse
from starlette.routing import Route

# Load .env so OTel settings are available before configure_observability()
load_dotenv()

# AWCP Integration Imports
from awcp.registry.service import build_registry
from awcp.registry import store
from awcp.runtime.tool_runtime import (
    TOOL_REGISTRY,
    discover_tools,
    execute_tool as run_tool,
)
from awcp.runtime.ollama_client import ask_ollama
from awcp.runtime.config import SEARCH_MODEL
from awcp.runtime.json_utils import extract_json
from awcp.runtime.schemas import PromptRequest
from awcp.agents.ollama_search import build_search_answer_prompt
from awcp.observability import configure_observability, get_tracer
from awcp.observability.metrics import instruments

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialise OTel for the MCP server process.
configure_observability(component="mcp-server")
tracer = get_tracer(__name__)

# Initialize the FastMCP server. FastMCP auto-generates each tool's JSON schema
# from the function signature/type hints, and wires both stdio and SSE transports.
mcp = FastMCP("awcp-control-plane")
# NOTE: logs go to stderr so they never corrupt the stdio JSON-RPC stream
# (stdout is the protocol channel when this server runs over stdio).
print("Initializing AWCP Control Plane components...", file=sys.stderr)
discover_tools()
agent_specs = build_registry()
print(f"✓ Registered {len(agent_specs)} agents in MCP Control Plane", file=sys.stderr)

# Look up the discovered AgentSpec by name. The decomposed tools below read
# each agent's self-declared `router` / `tool` / `model` from its spec, so no
# agent is special-cased here — new tool-using agents work just by declaring
# those fields in their AgentSpec (dynamic discovery stays intact).
_SPECS_BY_NAME = {s.name: s for s in agent_specs}
# Fallback model when an agent does not declare one.
_DEFAULT_MODEL = SEARCH_MODEL
_MAX_SYNTHESIS_OUTPUT_CHARS = int(os.getenv("AWCP_SYNTHESIS_TOOL_CHARS", "12000"))


# ======================================================================
# Workspace tools
# ======================================================================

@mcp.tool(description="Read the contents of a file in the workspace.")
def read_file(
    path: Annotated[str, Field(description="Relative path to the file")],
) -> str:
    if ".." in path or path.startswith("/"):
        return "Error: Invalid path."
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"


@mcp.tool(description="Write or overwrite a file in the workspace.")
def write_file(
    path: Annotated[str, Field(description="Relative path to the file")],
    content: Annotated[str, Field(description="Content to write")],
) -> str:
    if ".." in path or path.startswith("/"):
        return "Error: Invalid path."
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


@mcp.tool(description="Execute a shell command in the workspace.")
async def run_command(
    command: Annotated[str, Field(description="The shell command to run")],
) -> str:
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return f"STDOUT:\n{stdout.decode()}\nSTDERR:\n{stderr.decode()}"
    except Exception as e:
        return f"Error executing command: {str(e)}"


# ======================================================================
# Registry tools
# ======================================================================

@mcp.tool(description="List all agents registered in the AWCP Model Registry.")
def list_agents() -> str:
    agents = store.get_all()
    if not agents:
        return "No agents found in registry."
    result = "\n".join(
        f"- {a.name} (Status: {a.status}, ID: {a.agent_id})" for a in agents
    )
    return f"AWCP Registered Agents:\n{result}"


@mcp.tool(description="Get detailed manifest and status for a specific agent.")
def get_agent_info(
    agent_name: Annotated[str, Field(description="The name of the agent")],
) -> str:
    agents = store.get_all()
    agent = next((a for a in agents if a.name == agent_name), None)
    if not agent:
        return f"Agent '{agent_name}' not found."

    return (
        f"Agent Manifest: {agent.name}\n"
        f"{'='*30}\n"
        f"ID: {agent.agent_id}\n"
        f"Status: {agent.status}\n"
        f"Owner: {agent.owner}\n"
        f"Version: {agent.version}\n"
        f"Runtime: {agent.runtime}\n"
        f"Write Scopes: {', '.join(agent.write_scopes) if agent.write_scopes else 'None'}\n"
        f"Route: {agent.route}\n"
        f"Endpoint: {agent.endpoint_url}"
    )


@mcp.tool(description="Directly invoke an agent model with a prompt.")
def invoke_agent(
    agent_name: Annotated[str, Field(description="The name of the agent to invoke")],
    prompt: Annotated[str, Field(description="The input prompt for the agent")],
) -> str:
    spec = next((s for s in agent_specs if s.name == agent_name), None)
    if not spec:
        return f"Agent spec for '{agent_name}' not found."

    try:
        # Special case for agents requiring API keys (like deepseek)
        if agent_name == "deepseek-chat":
            return (
                "Error: DeepSeek agent requires an NVIDIA API key which must be "
                "provided via the main Agent Service REST API."
            )

        req = PromptRequest(input=prompt)
        response = spec.handler(req)
        output = response.get("output", str(response))
        return f"Agent Response:\n{output}"
    except Exception as e:
        return f"Error invoking agent: {str(e)}"


# ======================================================================
# Decomposed execution primitives
# These let an external orchestrator (e.g. Temporal) drive the agent loop
# step-by-step: reason -> (policy gate) -> tool call -> generate.
# ======================================================================

@mcp.tool(
    description=(
        "Reasoning/decision step. Decides whether an agent needs an external "
        'tool (SEARCH) or can answer directly (ANSWER). Returns JSON: '
        '{"action": "SEARCH"|"ANSWER", "search_query": str}.'
    )
)
def agent_route(
    agent_name: Annotated[str, Field(description="The name of the agent")],
    prompt: Annotated[str, Field(description="The user prompt to route")],
) -> str:
    # Delegate to the agent's self-declared router. Agents without one never
    # use a tool and answer directly.
    spec = _SPECS_BY_NAME.get(agent_name)

    if spec is not None and spec.router is not None:
        try:
            decision = spec.router(prompt)
        except Exception as e:
            # Fail safe: answer directly rather than blocking the workflow.
            decision = {"action": "ANSWER", "error": str(e)}

        # Wire the agent's declared tool onto a SEARCH decision unless the
        # router already chose a specific tool.
        if decision.get("action") == "SEARCH" and spec.tool:
            decision.setdefault("tool_name", spec.tool)
    else:
        decision = {"action": "ANSWER"}

    return json.dumps(decision)


@mcp.tool(
    description=(
        "Tool Executor primitive. Runs a single registered tool (e.g. "
        "'web_search') and returns its raw result. This is the call an "
        "orchestrator drives directly after the policy gate approves it."
    )
)
def execute_tool(
    tool_name: Annotated[str, Field(description="Registered tool name, e.g. web_search")],
    tool_input: Annotated[dict, Field(description="Arguments passed to the tool")],
) -> str:
    """Dispatch to a dynamically registered AWCP runtime tool.

    Instrumented: every call creates an OTel span and records mcp_tool_* metrics
    so Tempo + Prometheus capture per-tool latency, success, and failure rates
    without hardcoding any tool name.
    """
    m = instruments()
    tool_attrs = {"tool_name": tool_name}
    m["mcp_tool_calls"].add(1, tool_attrs)
    start = time.perf_counter()

    logger.info("mcp_execute_tool_started tool_name=%s", tool_name)

    with tracer.start_as_current_span("mcp.execute_tool") as span:
        span.set_attribute("mcp.tool_name", tool_name)
        try:
            result = run_tool(tool_name, tool_input or {})
            duration = time.perf_counter() - start
            m["mcp_tool_duration"].record(duration, tool_attrs)
            logger.info(
                "mcp_execute_tool_completed tool_name=%s duration_seconds=%.4f",
                tool_name, duration,
            )
            return str(result)
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except ImportError:
                pass
            m["mcp_tool_failures"].add(1, tool_attrs)
            m["mcp_tool_duration"].record(duration, tool_attrs)
            logger.exception("mcp_execute_tool_failed tool_name=%s", tool_name)
            return f"Error executing tool '{tool_name}': {str(exc)}"


@mcp.tool(
    description=(
        "Search arxiv.org for academic research papers. Use this tool when the "
        "user asks about research, papers, studies, preprints, scientific "
        "literature, machine learning models, or any academic / scientific topic. "
        "Returns paper titles, authors, abstracts, publication dates, and PDF URLs."
    )
)
def search_arxiv(
    query: Annotated[str, Field(description="Research topic or keywords to search for on arxiv")],
    max_results: Annotated[int, Field(description="Maximum number of papers to return (1-10)")] = 5,
) -> str:
    """Fetch academic papers from arxiv.org matching the query."""
    from awcp.tools.arxiv_search import run_arxiv_search
    m = instruments()
    tool_attrs = {"tool_name": "arxiv_search"}
    m["mcp_tool_calls"].add(1, tool_attrs)
    start = time.perf_counter()
    logger.info("mcp_search_arxiv_started query=%r max_results=%s", query, max_results)
    with tracer.start_as_current_span("mcp.search_arxiv") as span:
        span.set_attribute("mcp.tool_name", "arxiv_search")
        span.set_attribute("arxiv.query", query)
        span.set_attribute("arxiv.max_results", max_results)
        try:
            result = run_arxiv_search(query=query, max_results=max_results)
            duration = time.perf_counter() - start
            m["mcp_tool_duration"].record(duration, tool_attrs)
            logger.info(
                "mcp_search_arxiv_completed query=%r duration_seconds=%.4f output_chars=%s",
                query, duration, len(result),
            )
            return result
        except Exception as exc:
            duration = time.perf_counter() - start
            span.record_exception(exc)
            try:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            except ImportError:
                pass
            m["mcp_tool_failures"].add(1, tool_attrs)
            m["mcp_tool_duration"].record(duration, tool_attrs)
            logger.exception("mcp_search_arxiv_failed query=%r", query)
            return f"Error searching arxiv: {str(exc)}"


@mcp.tool(
    description=(
        "List dynamically registered AWCP runtime tools that can be executed "
        "through execute_tool. Returns JSON with each tool name and parameters."
    )
)
def list_runtime_tools() -> str:
    tools: list[dict[str, Any]] = []

    for name, handler in sorted(TOOL_REGISTRY.items()):
        signature = inspect.signature(handler)
        parameters = []
        required = []

        for param_name, param in signature.parameters.items():
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            annotation = None
            if param.annotation is not inspect.Signature.empty:
                annotation = getattr(param.annotation, "__name__", str(param.annotation))

            parameters.append(
                {
                    "name": param_name,
                    "required": param.default is inspect.Signature.empty,
                    "type": annotation,
                }
            )
            if param.default is inspect.Signature.empty:
                required.append(param_name)

        tools.append(
            {
                "name": name,
                "description": inspect.getdoc(handler) or "",
                "parameters": parameters,
                "required": required,
            }
        )

    return json.dumps(tools)


@mcp.tool(
    description=(
        "First-attempt LLM call for natural-language questions. Returns JSON "
        "with final=true only when the model can answer without external tools."
    )
)
def call_llm(
    query: Annotated[str, Field(description="Natural-language user query")],
) -> str:
    prompt = f"""
You are the first-pass answerer in a durable Temporal workflow.

Return ONLY JSON with this schema:
{{"final": true|false, "answer": string, "reason": string}}

Set final=false when the user asks for:
- live, current, recent, price, news, weather, market, ranking, or date-sensitive facts
- any research papers, academic studies, preprints, scientific literature, or arxiv topics
- anything about a specific ML model, algorithm paper, or scientific findings
- externally verifiable facts that require looking something up

Set final=true ONLY when the answer can be safely produced from stable general
knowledge, reasoning, writing, math, or coding knowledge — and does NOT require
checking any external source or research database.

User query:
{query}
"""

    try:
        raw = ask_ollama(prompt, _DEFAULT_MODEL)
        parsed = extract_json(raw)
        return json.dumps(
            {
                "configured": True,
                "final": bool(parsed.get("final")),
                "answer": str(parsed.get("answer", "")),
                "reason": str(parsed.get("reason", "")),
                "raw": raw,
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "configured": False,
                "final": False,
                "answer": "",
                "reason": f"LLM unavailable or returned invalid JSON: {e}",
            }
        )


@mcp.tool(
    description=(
        "Select registered runtime tools for a natural-language query from a "
        "discovered tool list. Returns JSON selection; no tool is hardcoded."
    )
)
def select_runtime_tools(
    query: Annotated[str, Field(description="Natural-language user query")],
    tools: Annotated[list[dict], Field(description="Discovered runtime tool metadata")],
) -> str:
    if not tools:
        return json.dumps({"tool_calls": [], "reason": "No runtime tools discovered."})

    tool_summary = json.dumps(tools, indent=2)
    prompt = f"""
You choose tools for a workflow. Use only tools from the discovered list.

Return ONLY JSON:
{{
  "tool_calls": [
    {{"tool_name": "name from list", "tool_input": {{"arg": "value"}}, "reason": "why"}}
  ],
  "reason": "brief selection rationale"
}}

Rules:
- Prefer tools whose required parameters can be filled from the user query.
- For a parameter named query, pass the user's query.
- Select at most 3 tools.
- If no tool applies, return an empty tool_calls list.

User query:
{query}

Discovered tools:
{tool_summary}
"""

    try:
        raw = ask_ollama(prompt, _DEFAULT_MODEL)
        parsed = extract_json(raw)
        calls = parsed.get("tool_calls", [])
        discovered_names = {tool.get("name") for tool in tools}
        valid_calls = [
            call
            for call in calls
            if call.get("tool_name") in discovered_names
            and isinstance(call.get("tool_input"), dict)
        ]
        return json.dumps(
            {
                "tool_calls": valid_calls[:3],
                "reason": str(parsed.get("reason", "")),
                "raw": raw,
            }
        )
    except Exception as e:
        fallback_calls = []
        for tool_info in tools:
            required = set(tool_info.get("required") or [])
            param_names = {
                param.get("name") for param in tool_info.get("parameters", [])
            }
            if "query" in param_names and required.issubset({"query"}):
                fallback_calls.append(
                    {
                        "tool_name": tool_info["name"],
                        "tool_input": {"query": query},
                        "reason": "Fallback selected a discovered query-compatible tool.",
                    }
                )

        return json.dumps(
            {
                "tool_calls": fallback_calls[:3],
                "reason": f"LLM tool selection unavailable; used metadata fallback: {e}",
            }
        )


@mcp.tool(
    description=(
        "Synthesize a final answer from runtime tool results. The answer must "
        "be grounded in the provided tool outputs."
    )
)
def synthesize_tool_results(
    query: Annotated[str, Field(description="Natural-language user query")],
    tool_results: Annotated[list[dict], Field(description="Tool result records")],
) -> str:
    logger.info(
        "Starting synthesize_tool_results query=%r tool_result_count=%s",
        query,
        len(tool_results or []),
    )
    successful = [
        result
        for result in tool_results
        if result.get("status") == "succeeded" and str(result.get("output", "")).strip()
    ]

    if not successful:
        return (
            "I could not find enough tool output to answer this request. "
            "Check the failed tool activity details in Temporal UI."
        )

    compact_results = []
    for result in successful:
        output = str(result.get("output", ""))
        if len(output) > _MAX_SYNTHESIS_OUTPUT_CHARS:
            output = (
                output[:_MAX_SYNTHESIS_OUTPUT_CHARS]
                + "\n\n[Output truncated for synthesis prompt]"
            )

        compact_results.append(
            {
                "tool_name": result.get("tool_name"),
                "tool_input": result.get("tool_input"),
                "output": output,
            }
        )

    prompt = f"""
You are a factual QA assistant.

Answer the user using only the successful tool outputs below. Be concise.
If the outputs do not contain the answer, say that the tools did not provide
enough information.

User query:
{query}

Tool outputs:
{json.dumps(compact_results, indent=2)}

Answer:
"""

    try:
        answer = ask_ollama(prompt, _DEFAULT_MODEL)
        logger.info("Completed synthesize_tool_results answer_chars=%s", len(answer))
        return answer
    except Exception:
        logger.exception("synthesize_tool_results LLM failed; returning tool-output fallback")
        return _fallback_tool_answer(query, successful)


def _fallback_tool_answer(query: str, tool_results: list[dict]) -> str:
    excerpts = []
    for result in tool_results:
        output = str(result.get("output", "")).strip()
        if not output:
            continue
        excerpts.append(
            f"{result.get('tool_name', 'tool')} result:\n"
            f"{output[:1500]}"
        )

    if not excerpts:
        return (
            "I could not synthesize a final answer because the tools did not "
            "return usable output."
        )

    return (
        f"I could not complete LLM synthesis for: {query}\n\n"
        "Here is the most relevant fetched tool output:\n\n"
        + "\n\n".join(excerpts)
    )


@mcp.tool(
    description=(
        "Generation/synthesis step. If 'search_results' is provided, the agent "
        "answers strictly from those results (grounded synthesis); otherwise it "
        "answers directly from the model."
    )
)
def agent_generate(
    agent_name: Annotated[str, Field(description="The name of the agent")],
    prompt: Annotated[str, Field(description="The user prompt")],
    search_results: Annotated[
        str | None,
        Field(description="Optional tool/search output to ground the answer in"),
    ] = None,
) -> str:
    spec = _SPECS_BY_NAME.get(agent_name)
    model = spec.model if spec and spec.model else _DEFAULT_MODEL

    try:
        if search_results:
            # Grounded synthesis: answer strictly from the tool output.
            grounded_prompt = build_search_answer_prompt(prompt, str(search_results))
            output = ask_ollama(grounded_prompt, model)
        else:
            # Direct generation using the agent's own declared model.
            output = ask_ollama(prompt, model)
        return output
    except Exception as e:
        return f"Error generating answer: {str(e)}"


# ======================================================================
# HTTP / SSE app (FastMCP supplies /sse + /messages/; we add the dashboard)
# ======================================================================

_DASHBOARD_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AWCP MCP Control Plane</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #F2EDE1; color: #0F0E0C; }
            h1 { color: #7A2E1E; border-bottom: 2px solid #7A2E1E; padding-bottom: 10px; }
            .card { background: #EDE5D3; border: 1px solid #1A1814; padding: 20px; border-radius: 4px; margin: 20px 0; }
            code { background: #1B1A18; color: #D4D0C8; padding: 2px 6px; border-radius: 4px; font-family: monospace; }
            .tag { display: inline-block; background: rgba(122,46,30,0.1); color: #7A2E1E; padding: 4px 12px; border-radius: 100px; font-size: 0.8em; font-weight: bold; text-transform: uppercase; }
            ul { padding-left: 20px; }
            li { margin-bottom: 10px; }
        </style>
    </head>
    <body>
        <h1>AWCP MCP Control Plane <span class="tag">Active</span></h1>
        <p>This is the <strong>Model Context Protocol (MCP)</strong> server for the Agent Workforce Control Plane.</p>

        <div class="card">
            <h3>🚀 How to use this server</h3>
            <p>This server is designed to be consumed by MCP clients, not browsers. To interact with the agent registry and models, use one of the following methods:</p>
            <ul>
                <li><strong>MCP Inspector:</strong> Run <code>PYTHONPATH=src npx @modelcontextprotocol/inspector ./.venv/bin/python -m awcp.mcp.server stdio</code></li>
                <li><strong>Claude Desktop:</strong> Add this server to your <code>claude_desktop_config.json</code> using the SSE transport.</li>
            </ul>
        </div>

        <div class="card">
            <h3>🛠 Available Control Tools</h3>
            <ul>
                <li><code>list_agents</code> - Query the AWCP Model Registry</li>
                <li><code>get_agent_info</code> - Inspect agent manifests and write scopes</li>
                <li><code>invoke_agent</code> - Directly trigger a model response</li>
                <li><code>agent_route / execute_tool / agent_generate</code> - Decomposed governed loop</li>
                <li><code>read_file / write_file / run_command</code> - Workspace management</li>
            </ul>
        </div>

        <p><small>Endpoints: <code>/sse</code> (Transport) | <code>/messages/</code> (Protocol)</small></p>
    </body>
    </html>
    """


async def get_index(request):
    return HTMLResponse(_DASHBOARD_HTML)


# FastMCP builds the Starlette SSE app (routes: GET /sse, POST /messages/).
# We attach the human-facing dashboard at GET / on the same app. This is the
# object uvicorn serves: `uvicorn awcp.mcp.server:app`.
app = mcp.sse_app()
app.router.routes.append(Route("/", endpoint=get_index, methods=["GET"]))


if __name__ == "__main__":
    # `python -m awcp.mcp.server stdio` -> stdio transport (for Temporal/MCP clients)
    # `python -m awcp.mcp.server`       -> SSE app + dashboard on :8002
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8002)
