# AWCP — Agent Workforce Control Plane

A governed multi-agent system. Agents route prompts to LLM backends and use tools
(web search), while **Temporal** orchestrates each step durably and an **MCP server**
(FastMCP) executes the actual work. A web **control surface** lets you trigger and watch
runs without the CLI.

> Branch note: this is the `agents_mcp` branch — the `src/awcp/` layout, FastMCP server,
> Temporal-driven governance, dynamic self-declared agents, the advanced search tool, and
> the control surface. `main` holds the original flat project.

---

## How it fits together

```
You ─▶ Control surface (web UI / CLI) ─▶ Temporal (the orchestrator)
                                              │  drives each step as an activity
                                              ▼
                                    MCP server (FastMCP)  ─▶  Agents · Tools · Ollama
                                    (stdio locally, or SSE over the network)
```

- **Temporal** = the *boss*: decides what runs and when, applies the autonomy/policy gate,
  retries, degrades gracefully, and records every step in history.
- **MCP server** = the *hands*: performs one atomic job per call — *route*, *run a tool*,
  *generate*. It owns the agent registry, the tool registry, and the model connections.
- **Control surface** = a small FastAPI app + web page to start runs and watch the steps live.

A single governed run decomposes into four Temporal activities:
`get_agent_info → agent_route → execute_tool → agent_generate`.

---

## Folder structure

```text
awcp_agents/
├── src/awcp/
│   ├── agents/                 # the agents (auto-discovered)
│   │   ├── base.py             # AgentSpec (name, route, handler, model, router, tool, …)
│   │   ├── ollama_chat.py      # "ollama"          — gemma2:2b, answer-only
│   │   ├── ollama_search.py    # "ollama-search"   — llama3.1:8b + web_search
│   │   ├── ollama_advanced_search.py # "ollama-advanced" — llama3.1:8b + advanced_web_search
│   │   ├── deepseek_chat.py    # "deepseek"        — NVIDIA cloud
│   │   └── llama_vision.py     # "llama-vision"    — NVIDIA cloud (vision)
│   ├── tools/
│   │   ├── web_search.py            # DuckDuckGo (ddgs) — tool "web_search"
│   │   └── advanced_web_search.py   # DDGS + Groq — tool "advanced_web_search"
│   ├── runtime/                # ollama client, tool registry, schemas, config, events
│   ├── registry/              # agent registry: discovery, store, service, models, routes
│   ├── mcp/server.py          # the MCP server (FastMCP) — stdio + SSE
│   ├── temporal/              # the governance layer
│   │   ├── workflows/agent_execution.py   # AgentGovernanceWorkflow (the 4-step DAG)
│   │   ├── activities/mcp_gateway.py       # activities that call the MCP server
│   │   ├── worker/run_worker.py            # the Temporal worker
│   │   ├── client/trigger_workflow.py      # CLI trigger helper
│   │   └── config.py                       # Temporal + MCP transport settings
│   ├── control/               # the non-CLI surface
│   │   ├── api.py             # FastAPI: /agents, /run, /status, serves the UI
│   │   └── static/index.html  # the Live Control Surface page
│   └── service.py             # legacy direct FastAPI agent service (optional)
├── scripts/                   # one-command launchers (below)
├── docs/                      # AWCP_Implementation_Guide.html, magazine, notes
├── requirements.txt
└── README.md
```

---

## Agents

Agents **self-register** via discovery — drop a file in `src/awcp/agents/` that defines an
`AGENT = AgentSpec(...)` and it appears everywhere (registry, MCP server, control UI). No
hardcoding. Each agent declares its own behaviour:

| Agent | Model | Tool it uses | Notes |
|---|---|---|---|
| `ollama` | `gemma2:2b` | — | plain chat, answer-only |
| `ollama-search` | `llama3.1:8b` | `web_search` | DuckDuckGo, grounded answers |
| `ollama-advanced` | `llama3.1:8b` | `advanced_web_search` | DDGS **+** Groq, one or both |
| `deepseek` | NVIDIA | — | cloud (needs API key) |
| `llama-vision` | NVIDIA | — | cloud vision (needs API key) |

An agent with a `router` + `tool` is tool-using; without a `router` it is answer-only. The
control plane reads those declarations, so Temporal drives whichever tool the agent declares
with **no server or workflow changes**.

### Tools
- **`web_search`** — DuckDuckGo, multiple query variants, deduped top results.
- **`advanced_web_search`** — combines DuckDuckGo and a **Groq** agentic web search. It uses
  one or both based on runtime conditions (no key → DDGS only; DDGS empty → Groq; DDGS thin or
  query needs synthesis/recency → both; DDGS strong + simple → DDGS only). The Groq key is read
  from the `groq_api_key` call argument or `GROQ_API_KEY`; without it, it falls back to DDGS.

---

## Running it (single machine)

Prereqs: **Python ≥ 3.10**, **Ollama** running with the models, and the **Temporal CLI**.

```bash
# 1. Install
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
ollama pull gemma2:2b && ollama pull llama3.1:8b

# 2. Temporal (engine :7233, web UI :8233)
temporal server start-dev

# 3. The worker (defaults to a local stdio MCP server — no env needed)
./scripts/run_worker.sh

# 4a. The control surface (web UI)
./scripts/run_control.sh            # http://localhost:8003

# 4b. …or trigger from the CLI
temporal workflow execute \
  --type AgentGovernanceWorkflow --task-queue awcp-governance-queue \
  --workflow-id r1 \
  --input '{"agent_name":"ollama-advanced","input":"current price of gold per gram"}'
```

Watch the run in the control surface or the Temporal UI (`http://localhost:8233`).

### Other launchers
| Script | What it starts | Port |
|---|---|---|
| `scripts/run_worker.sh` | Temporal worker (drives the MCP server) | — |
| `scripts/run_control.sh` | Control surface (web UI + trigger API) | 8003 |
| `scripts/start_mcp.sh` | MCP server over **SSE** + dashboard | 8002 |
| `scripts/start_server.sh` | Legacy direct REST agent service | 8001 |

---

## Dynamic `/ask` workflow

The control surface also exposes a generic natural-language endpoint:

```http
POST /ask
Content-Type: application/json

{"query": "What is the price of Gold today"}
```

This starts `DynamicAskWorkflow`, which drives the MCP server dynamically:

1. `call_llm` asks the MCP-hosted LLM for a final answer only when the query is
   safe to answer without external information.
2. If the LLM cannot answer, `discover_tools` lists runtime tools registered
   with the MCP server.
3. `select_tools` chooses from discovered tool metadata. It does not hardcode
   query-specific branches.
4. Each selected tool runs as its own `run_tool` activity with
   `TOOL_EXECUTION_RETRY` (`maximum_attempts=3`), so only the failed tool call is
   retried.
5. `synthesize_answer` creates the final grounded response from successful tool
   outputs.

### Step-by-step setup

Linux/macOS/Git Bash:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$PWD/src"
ollama pull gemma2:2b
ollama pull llama3.1:8b
temporal server start-dev
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "$PWD\src"
ollama pull gemma2:2b
ollama pull llama3.1:8b
temporal server start-dev
```

In a second terminal, run the Temporal worker:

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src"
python -m awcp.temporal.worker.run_worker
```

PowerShell equivalent:

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"
python -m awcp.temporal.worker.run_worker
```

In a third terminal, run the FastAPI control API:

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src"
uvicorn awcp.control.api:app --host 0.0.0.0 --port 8003 --reload
```

PowerShell equivalent:

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"
uvicorn awcp.control.api:app --host 0.0.0.0 --port 8003 --reload
```

Test the endpoint:

```bash
curl -X POST http://localhost:8003/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"What is the price of Gold today"}'
```

PowerShell:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8003/ask `
  -ContentType "application/json" `
  -Body '{"query":"What is the price of Gold today"}'
```

Open Temporal UI at `http://localhost:8233`, then open the workflow ID returned
by `/ask`. The history shows `call_llm`, `discover_tools`, `select_tools`, one
`run_tool` activity per selected MCP runtime tool, and `synthesize_answer`.

---

## Adding a new agent

Create `src/awcp/agents/my_agent.py`:

```python
from awcp.agents.base import AgentSpec
from awcp.agents.ollama_search import route          # reuse the SEARCH/ANSWER router
from awcp.runtime.config import SEARCH_MODEL
from awcp.runtime.schemas import PromptRequest

def run(req: PromptRequest) -> dict:
    ...  # direct REST-path handler

AGENT = AgentSpec(
    name="my-agent",
    route="/chat/my-agent",
    request_model=PromptRequest,
    handler=run,
    runtime="ollama",
    model=SEARCH_MODEL,      # used to write the answer
    router=route,            # omit for an answer-only agent
    tool="advanced_web_search",  # the tool Temporal will run on a SEARCH
)
```

Restart the worker + control surface; it appears in the dropdown and runs end-to-end —
tool calls and all — with no other changes.

---

## Running on a separate system (share one MCP server)

The worker can use a **remote** MCP server over SSE instead of a local one, so a teammate's
Temporal can drive the models on the host's machine.

- **Host:** `./scripts/start_mcp.sh` then expose it: `ngrok http 8002 --basic-auth "team:pass"`.
- **Teammate's worker:** set the env and start:
  ```bash
  export AWCP_MCP_SSE_URL="https://<host-ngrok>.ngrok-free.app/sse"
  export AWCP_MCP_SSE_AUTH="team:pass"          # if host used --basic-auth
  # export TEMPORAL_SERVER_URL="host:7233"      # if their Temporal isn't local
  ./scripts/run_worker.sh
  ```

⚠️ The MCP server exposes `run_command`/`read_file`/`write_file` — always protect the tunnel
with auth; this sharing path is for collaboration/demos, not production.

---

## Environment variables

| Variable | Used by | Default |
|---|---|---|
| `TEMPORAL_SERVER_URL` | worker, control, client | `localhost:7233` |
| `AWCP_MCP_SSE_URL` | worker | unset → local **stdio** MCP server |
| `AWCP_MCP_SSE_AUTH` | worker | unset (basic-auth `user:pass` for the tunnel) |
| `GROQ_API_KEY` | `advanced_web_search` | unset → DDGS-only fallback |
| `AWCP_DEFAULT_OWNER` | registry | OS username |
| `AWCP_TELEMETRY_ENABLED` | registry (quarantine gate) | `true` |
| `AWCP_TUNNEL_BASE_URL` | registry (endpoint URLs) | `http://localhost:8001` |

---

## More detail

A full plain-language walkthrough (architecture, the governed loop, how to run, how to add
an agent, FastMCP, the advanced search tool) is in **[`docs/AWCP_Implementation_Guide.html`](docs/AWCP_Implementation_Guide.html)** —
open it in a browser (it also prints cleanly to PDF).





# Run Tempo

## First - Terminal
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "$PWD\src"
ollama pull gemma2:2b
ollama pull llama3.1:8b
temporal server start-dev

```
## Second - Terminal
```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"
python -m awcp.temporal.worker.run_worker
```

## Third - Terminal
```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"
uvicorn awcp.control.api:app --host 0.0.0.0 --port 8003 --reload
```