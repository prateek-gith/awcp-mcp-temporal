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

---

---

# 🖥️ Running on a New Machine (macOS / Windows) — Complete Setup Guide

Follow every step in order. Each step is labelled with the OS it applies to.
You need **four terminal windows** open at the same time once setup is done.

---

## Prerequisites — Install These First (one-time)

### 1 · Python 3.10 or newer

| OS | How to install |
|---|---|
| **macOS** | `brew install python@3.11` — or download from [python.org](https://www.python.org/downloads/) |
| **Windows** | Download the installer from [python.org/downloads](https://www.python.org/downloads/). During install **tick "Add Python to PATH"**. |

Verify:

```bash
# macOS / Linux
python3 --version

# Windows PowerShell
python --version
```

---

### 2 · Git

| OS | How to install |
|---|---|
| **macOS** | `brew install git`  —  or `xcode-select --install` |
| **Windows** | Download from [git-scm.com](https://git-scm.com/) with default settings |

---

### 3 · Docker Desktop

Required for the observability stack (Grafana, Loki, Tempo, Prometheus, OTel Collector).

- Download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
- Install and **start Docker Desktop** before continuing.

Verify:

```bash
docker --version
docker compose version
```

---

### 4 · Ollama (local LLM runtime)

| OS | How to install |
|---|---|
| **macOS** | `brew install ollama`  —  or download from [ollama.com](https://ollama.com) |
| **Windows** | Download the `.exe` installer from [ollama.com](https://ollama.com) and run it |

Start Ollama:

```bash
# macOS — runs as a background service automatically after install.
# Windows — open "Ollama" from the Start Menu, or run in a terminal:
ollama serve
```

Pull the two required models (one-time download, ~10 GB total):

```bash
ollama pull gemma2:2b
ollama pull llama3.1:8b
```

Verify the models are ready:

```bash
ollama list
```

---

### 5 · Temporal CLI

| OS | How to install |
|---|---|
| **macOS** | `brew install temporal` |
| **Windows** | Download `temporal_windows_amd64.zip` from [github.com/temporalio/cli/releases](https://github.com/temporalio/cli/releases), unzip it, and add the folder to your system `PATH`. |

Verify:

```bash
temporal --version
```

---

## Step 1 — Clone the Repository

```bash
git clone <your-repo-url> awcp_v2
cd awcp_v2
```

> Replace `<your-repo-url>` with the actual Git remote URL.

---

## Step 2 — Copy / Review the Environment File

The project ships a `.env` file with correct local defaults. **No edits are needed to run locally.**

```bash
# macOS / Linux — just inspect it
cat .env

# Windows PowerShell
Get-Content .env
```

**Optional** — if you want Groq-powered advanced search, add your API key:

```bash
# macOS / Linux
echo "GROQ_API_KEY=your_key_here" >> .env

# Windows PowerShell
Add-Content .env "GROQ_API_KEY=your_key_here"
```

---

## Step 3 — Create the Virtual Environment & Install Dependencies

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If PowerShell blocks scripts, run once as Administrator:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

Verify the install:

```bash
python -c "import temporalio, fastapi, mcp, arxiv; print('All packages OK')"
```

---

## Step 4 — Start the Observability Stack (Docker)

Run this **once** in any terminal. The containers run in the background and survive terminal restarts.

```bash
docker compose -f docker/docker-compose.observability.yml up -d
```

Verify all five containers are `Up`:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

You should see: `awcp-grafana`, `awcp-loki`, `awcp-tempo`, `awcp-prometheus`, `awcp-otel-collector`.

---

## Step 5 — Start the Four Application Processes

Open **four separate terminal windows** and run one command in each.

---

### Terminal 1 — Temporal Dev Server

Orchestration engine — ports 7233 (gRPC) + 8233 (Web UI).

```bash
# macOS / Linux
temporal server start-dev

# Windows PowerShell (same command)
temporal server start-dev
```

✅ Open `http://localhost:8233` — you should see the Temporal Web UI.

---

### Terminal 2 — Temporal Worker

Executes governed workflow activities. Spawns the MCP server (stdio) automatically.

**macOS / Linux:**

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src"
python -m awcp.temporal.worker.run_worker
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"
python -m awcp.temporal.worker.run_worker
```

✅ Look for: `Worker started, task queue: awcp-governance-queue`

---

### Terminal 3 — FastAPI Control API

HTTP bridge between the browser / CLI and Temporal — port 8080.

**macOS / Linux:**

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src"
uvicorn awcp.control.api:app --host 0.0.0.0 --port 8080 --reload
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"
uvicorn awcp.control.api:app --host 0.0.0.0 --port 8080 --reload
```

✅ Look for: `Application startup complete.`
Open `http://localhost:8080/` — the AWCP Control Surface UI will load.

---

### Terminal 4 — Service Health Check (recommended)

Run this once to confirm everything is alive before sending real requests.

**macOS / Linux:**

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src"
python scripts/verify_services.py
```

**Windows PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"
python scripts/verify_services.py
```

✅ Expected output: `SUCCESS: All critical services and models are ready!`

---

## Step 6 — Send a Test Request

### Option A — Web UI (easiest)

1. Open `http://localhost:8080/` in your browser.
2. Select an agent from the dropdown (e.g. `ollama-search`).
3. Type a query and click **Run**.
4. Watch each activity step light up live.

---

### Option B — General web search query

**macOS / Linux (curl):**

```bash
curl -X POST http://localhost:8080/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the price of gold today?", "agent_id": "dynamic"}'
```

**Windows PowerShell:**

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8080/ask `
  -ContentType "application/json" `
  -Body '{"query": "What is the price of gold today?", "agent_id": "dynamic"}'
```

---

### Option C — Academic / research query (uses the arxiv_search tool)

**macOS / Linux (curl):**

```bash
curl -X POST http://localhost:8080/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me recent arxiv papers on retrieval augmented generation RAG"}'
```

**Windows PowerShell:**

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8080/ask `
  -ContentType "application/json" `
  -Body '{"query": "Show me recent arxiv papers on retrieval augmented generation RAG"}'
```

✅ The JSON response includes a `replay_context` block with:
- `context_hash` — SHA-256 fingerprint of the governing inputs
- `checkpoint_id` — step-level resume pointer
- `decision_path` — e.g. `llm_decision→tool_discovery→run_tool[arxiv_search]→synthesis`
- `degradation_mode` — `normal`, `limited`, or `recommendation_only`

---

## Step 7 — Verify Observability in Grafana

Open `http://localhost:3000` (no login needed — anonymous Admin is pre-configured).

| What to verify | Where to look |
|---|---|
| Main dashboard | Dashboards → *AWCP Observability — Governance Edition* |
| Traces | Explore → Tempo → `{service.name="awcp-control-api"}` |
| Logs | Explore → Loki → `{service_namespace="awcp"} \| json` |
| Governance metrics | Dashboard → *🏛 Governance — Evidence & Degradation* row |
| Raw metric | Explore → Prometheus → `awcp_replay_context_generated_total` |

---

## All Service URLs at a Glance

| Service | URL | Notes |
|---|---|---|
| **Control Surface (UI)** | `http://localhost:8080/` | Main web interface |
| **Control API** | `http://localhost:8080/ask` | POST endpoint |
| **Temporal Web UI** | `http://localhost:8233` | Workflow history & debug |
| **Grafana** | `http://localhost:3000` | Dashboards, traces, logs |
| **Prometheus** | `http://localhost:9090` | Raw metrics browser |
| **Loki** | `http://localhost:3100` | Log backend (API only) |
| **Tempo** | `http://localhost:3200` | Trace backend (API only) |
| **OTel Collector health** | `http://localhost:13133` | Health check endpoint |
| **Ollama** | `http://localhost:11434` | LLM inference server |

---

## Governance Fields Reference

Every `/ask` request accepts these optional fields:

```json
{
  "query": "your question",
  "agent_id": "dynamic",
  "policy_mode": "active",
  "autonomy_mode": "active"
}
```

| Field | Allowed values | Meaning |
|---|---|---|
| `agent_id` | `dynamic`, `ollama`, `ollama-search`, `ollama-advanced` | Agent profile to use |
| `policy_mode` | `active`, `recommendation_only`, `safe_mode` | Strictness of policy enforcement |
| `autonomy_mode` | `active`, `recommendation_only`, `safe_mode` | How much decision autonomy the agent has |

---

## Stopping Everything

**macOS / Linux:**

```bash
# Press Ctrl+C in each of the four terminals, then stop Docker:
docker compose -f docker/docker-compose.observability.yml down
```

**Windows PowerShell:**

```powershell
# Press Ctrl+C in each of the four terminals, then stop Docker:
docker compose -f docker/docker-compose.observability.yml down
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `temporal: command not found` | Temporal CLI not in PATH | Add the Temporal binary directory to your system PATH |
| `ollama: command not found` | Ollama not installed / not started | Install Ollama and run `ollama serve` |
| Worker exits immediately | Missing packages | Run `pip install -r requirements.txt` with the venv active |
| `ModuleNotFoundError: awcp` | PYTHONPATH not set | Set `export PYTHONPATH="$PWD/src"` (macOS) or `$env:PYTHONPATH = "$PWD\src"` (Windows) |
| Port 8080 already in use | Another process on that port | Use `--port 8090` and update `AWCP_CONTROL_API_PORT` in `.env` |
| Docker containers not starting | Docker Desktop not running | Open Docker Desktop and wait for the engine to become ready |
| OTel Collector not receiving data | Container down | Run `docker compose -f docker/docker-compose.observability.yml up -d` |
| Grafana shows no data | OTel export disabled | Ensure `AWCP_OTEL_ENABLED=true` in `.env` |
| Ollama model missing | Model not pulled | Run `ollama pull gemma2:2b && ollama pull llama3.1:8b` |
| `arxiv search failed` | No internet / rate limited | Check internet connectivity and retry after a few seconds |
| PowerShell script blocked | Execution policy restricted | Run `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` as Administrator |

---

---

# 🐳 Observability Stack — Docker Setup & Configuration

This section explains **exactly** what each Docker container does, which config file controls it, what every port is for, and how to manage the stack day-to-day.

---

## File Structure

```text
docker/
├── docker-compose.observability.yml   ← single command launches everything
├── otel-collector-config.yaml         ← OTel Collector pipelines
├── tempo-config.yaml                  ← Grafana Tempo (traces)
├── loki-config.yaml                   ← Grafana Loki (logs)
├── prometheus.yml                     ← Prometheus scrape config (metrics)
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── datasources.yml        ← auto-wires Prometheus + Loki + Tempo into Grafana
        └── dashboards/
            ├── dashboard-provider.yml ← tells Grafana where to load dashboards from
            └── awcp-overview.json     ← the pre-built AWCP governance dashboard
```

---

## How the Five Containers Fit Together

```
Your App (FastAPI + Worker)
        │
        │  OTLP HTTP (port 4318)  ←── traces + metrics + logs
        ▼
┌─────────────────────┐
│   OTel Collector    │   receives everything, fans out to backends
└──────┬──────┬───────┘
       │      │      │
       │      │      └──── metrics ──▶ Prometheus  (remote-write :9090)
       │      └─────────── logs    ──▶ Loki        (HTTP push :3100)
       └────────────────── traces  ──▶ Tempo       (OTLP gRPC :4317)
                                            │
                                            ▼
                                      Grafana :3000
                              (queries all three backends)
```

All five containers share an isolated Docker bridge network named `observability`, so they resolve each other by **service name** (e.g. `tempo`, `loki`) — no IP addresses needed.

---

## Starting / Stopping the Stack

```bash
# Start all containers in the background (first run pulls images automatically)
docker compose -f docker/docker-compose.observability.yml up -d

# Stop all containers (data volumes are preserved)
docker compose -f docker/docker-compose.observability.yml down

# Stop AND delete all stored data (traces, logs, metrics, dashboards)
docker compose -f docker/docker-compose.observability.yml down -v

# Restart a single container (e.g. after editing its config file)
docker compose -f docker/docker-compose.observability.yml restart otel-collector

# Tail logs from a specific container
docker logs -f awcp-otel-collector
docker logs -f awcp-grafana
docker logs -f awcp-loki
docker logs -f awcp-tempo
docker logs -f awcp-prometheus

# Check status of all containers
docker compose -f docker/docker-compose.observability.yml ps
```

---

## Container Reference

### 1 · OTel Collector (`awcp-otel-collector`)

**Image:** `otel/opentelemetry-collector-contrib:0.102.0`  
**Config file:** `docker/otel-collector-config.yaml`  
**What it does:** Receives all telemetry signals from the Python app and routes them to the correct backend. It is the **single ingestion point** — the app only needs to know the collector's address.

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| `4317` | gRPC | App → Collector | OTLP traces / metrics / logs (gRPC) |
| `4318` | HTTP | App → Collector | OTLP traces / metrics / logs (HTTP) ← used by AWCP |
| `8889` | HTTP | Prometheus → Collector | Prometheus scrape endpoint (app metrics exposed here) |
| `13133` | HTTP | — | Health check: `curl http://localhost:13133` |

**Config breakdown (`otel-collector-config.yaml`):**

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }   # accepts gRPC signals
      http: { endpoint: 0.0.0.0:4318 }   # accepts HTTP signals (AWCP uses this)

processors:
  memory_limiter:    # prevents OOM — caps memory at 512 MiB
  batch:             # groups signals before export (512 items / 5 s)
  resource:          # stamps deployment.environment=development on every signal

exporters:
  otlp/tempo:            traces  → Tempo  via gRPC
  loki:                  logs    → Loki   via HTTP push
  prometheusremotewrite: metrics → Prometheus via remote-write
  prometheus:            also exposes a /metrics scrape endpoint on :8889

service:
  pipelines:
    traces:  receivers[otlp] → processors → exporters[tempo]
    metrics: receivers[otlp] → processors → exporters[prometheus]
    logs:    receivers[otlp] → processors → exporters[loki]
```

---

### 2 · Grafana Tempo (`awcp-tempo`)

**Image:** `grafana/tempo:2.5.0`  
**Config file:** `docker/tempo-config.yaml`  
**What it does:** Stores distributed traces. Every span your Python code emits (e.g. `workflow_start`, `run_tool`, `mcp.search_arxiv`) lands here as a searchable trace tree.

| Port | Purpose |
|---|---|
| `3200` | HTTP query API — Grafana reads traces from here |
| `4317` | Internal gRPC OTLP receiver from the OTel Collector (not exposed to host) |

**Config breakdown (`tempo-config.yaml`):**

```yaml
server:
  http_listen_port: 3200        # Grafana data source URL

storage:
  trace:
    backend: local              # stores traces on Docker volume (swap to S3 in prod)
    local: { path: /var/tempo/traces }
    wal:   { path: /var/tempo/wal }

distributor:
  receivers:
    otlp:                       # accepts OTLP gRPC + HTTP from the collector
      protocols: { grpc, http }

compactor:
  compaction:
    block_retention: 48h        # traces are kept for 2 days in dev

ingester:
  max_block_duration: 5m        # flush blocks every 5 minutes
```

---

### 3 · Grafana Loki (`awcp-loki`)

**Image:** `grafana/loki:3.0.0`  
**Config file:** `docker/loki-config.yaml`  
**What it does:** Stores structured log lines. Every `logger.info("event", extra={...})` call in the Python code is pushed here by the OTel Collector. Logs are queryable by label (e.g. `workflow_id`, `agent_id`, `degradation_mode`).

| Port | Purpose |
|---|---|
| `3100` | HTTP API — OTel Collector pushes logs here; Grafana reads from here |

**Config breakdown (`loki-config.yaml`):**

```yaml
auth_enabled: false             # no auth in dev

server:
  http_listen_port: 3100

common:
  storage:
    filesystem:
      chunks_directory: /loki/chunks   # stored on Docker volume
      rules_directory:  /loki/rules

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb                      # TSDB schema (v13) for better query performance
      schema: v13

limits_config:
  reject_old_samples: true
  reject_old_samples_max_age: 168h     # reject logs older than 7 days
  ingestion_rate_mb: 16
  max_query_series: 500
```

**Querying logs in Grafana:**

```logql
# All AWCP logs
{service_namespace="awcp"} | json

# Filter by workflow
{service_namespace="awcp"} | json | workflow_id = "awcp-ask-abc123"

# Filter by degradation mode
{service_namespace="awcp"} | json | degradation_mode = "limited"

# Filter by agent
{service_namespace="awcp"} | json | agent_id = "ollama-search"
```

---

### 4 · Prometheus (`awcp-prometheus`)

**Image:** `prom/prometheus:v2.52.0`  
**Config file:** `docker/prometheus.yml`  
**What it does:** Stores time-series metrics. The OTel Collector pushes app metrics (request counts, workflow durations, governance counters) here via **remote-write**. Prometheus also scrapes the Collector's own internal metrics.

| Port | Purpose |
|---|---|
| `9090` | HTTP UI + API — Grafana reads metrics from here |

**Config breakdown (`prometheus.yml`):**

```yaml
global:
  scrape_interval: 15s          # poll targets every 15 seconds
  external_labels:
    environment: development
    project: awcp

scrape_configs:
  - job_name: "otel-collector"          # scrapes app metrics from collector :8889
    static_configs:
      - targets: ["otel-collector:8889"]

  - job_name: "otel-collector-internal" # collector's own pipeline metrics :8888
    static_configs:
      - targets: ["otel-collector:8888"]

  - job_name: "prometheus"              # Prometheus self-scrape
    static_configs:
      - targets: ["localhost:9090"]
```

**Useful PromQL queries:**

```promql
# Evidence packets generated (one per successful /ask)
sum(awcp_replay_context_generated_total)

# Degradation events by mode
sum(awcp_degradation_events_total) by (degradation_mode)

# HTTP request rate
rate(awcp_http_requests_total[5m])

# Workflow execution count
sum(awcp_workflow_executions_total)

# Tool timeouts
sum(awcp_tool_timeouts_total) by (tool_name)

# Activity retries
sum(awcp_workflow_retries_total) by (activity)
```

---

### 5 · Grafana (`awcp-grafana`)

**Image:** `grafana/grafana:11.1.0`  
**Config:** environment variables in `docker-compose.observability.yml` + provisioning files  
**What it does:** The unified UI for querying and visualising traces, logs, and metrics. It is pre-configured with all data sources and the AWCP governance dashboard — no manual setup required.

| Port | Purpose |
|---|---|
| `3000` | Grafana web UI |

**Environment variables (set in docker-compose):**

| Variable | Value | Effect |
|---|---|---|
| `GF_AUTH_ANONYMOUS_ENABLED` | `true` | No login needed |
| `GF_AUTH_ANONYMOUS_ORG_ROLE` | `Admin` | Anonymous users get full Admin access |
| `GF_AUTH_DISABLE_LOGIN_FORM` | `true` | Login page hidden |
| `GF_FEATURE_TOGGLES_ENABLE` | `traceqlEditor traceToMetrics` | Enables TraceQL editor + trace-to-metrics |

---

## Grafana Auto-Provisioning (Zero Manual Setup)

Grafana reads two provisioning directories automatically on startup — no clicking required.

### Data Sources (`grafana/provisioning/datasources/datasources.yml`)

Three data sources are pre-wired:

| Data source | Type | URL | Default? | Special config |
|---|---|---|---|---|
| **Prometheus** | `prometheus` | `http://prometheus:9090` | No | 15 s scrape interval |
| **Loki** | `loki` | `http://loki:3100` | No | Derived field links `trace_id` → Tempo |
| **Tempo** | `tempo` | `http://tempo:3200` | **Yes** | Links traces → Loki logs; service map from Prometheus |

The Loki → Tempo link means: when you view a log line in Explore, a **"View in Tempo"** button appears automatically if the log contains a `trace_id` field — letting you jump straight to the full trace.

### Dashboards (`grafana/provisioning/dashboards/`)

Two files control dashboard auto-loading:

**`dashboard-provider.yml`** — tells Grafana to watch a directory for JSON files:
```yaml
providers:
  - name: "AWCP Dashboards"
    folder: "AWCP"
    type: file
    updateIntervalSeconds: 30       # re-loads every 30 s if the JSON changes
    options:
      path: /etc/grafana/provisioning/dashboards
```

**`awcp-overview.json`** — the pre-built AWCP governance dashboard. Contains:
- HTTP request rate, duration, and error panels
- Workflow execution and failure panels
- Activity execution and duration panels
- MCP tool call and failure panels
- **🏛 Governance row** with 5 panels:
  - Degradation Events by Mode (time series)
  - Policy Denials Total (stat)
  - Replay Context Generated (stat)
  - Tool Timeouts by Tool Name (time series)
  - Activity Retries per Workflow (time series)

To **edit the dashboard**, open it in Grafana → make changes → Export JSON → overwrite `awcp-overview.json`. It reloads within 30 seconds next time the stack starts.

---

## Data Volumes (Persistent Storage)

Docker named volumes ensure data survives container restarts:

| Volume name | Container | What it stores |
|---|---|---|
| `tempo-data` | Tempo | Trace blocks + WAL |
| `loki-data` | Loki | Log chunks + index |
| `prometheus-data` | Prometheus | Metric time-series DB |
| `grafana-data` | Grafana | User settings, saved dashboards |

```bash
# List volumes
docker volume ls | grep awcp

# Inspect a volume
docker volume inspect awcp_v2_tempo-data

# Delete all observability data and start fresh
docker compose -f docker/docker-compose.observability.yml down -v
docker compose -f docker/docker-compose.observability.yml up -d
```

---

## Container Startup Order

The `depends_on` relationships in the compose file enforce this boot order:

```
Tempo ──┐
Loki  ──┼──▶ OTel Collector
Prom  ──┘

Loki  ──┐
Tempo ──┼──▶ Grafana
Prom  ──┘
```

The OTel Collector only starts after Tempo, Loki, and Prometheus are ready so it never tries to push data before the backends exist. Grafana only starts after all three backends are ready so its data source health checks pass immediately.

---

## How the App Connects to the Collector

The `.env` file tells the Python app where to send telemetry:

```ini
# .env
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # HTTP OTLP → OTel Collector
AWCP_OTEL_ENABLED=true                              # master switch
AWCP_OTEL_TRACES_ENABLED=true
AWCP_OTEL_LOGS_ENABLED=true
AWCP_OTEL_METRICS_ENABLED=true
OTEL_METRIC_EXPORT_INTERVAL=15000                   # flush metrics every 15 s
```

The app uses **HTTP** (port 4318) rather than gRPC (4317) to avoid needing the `grpcio` native binary, which can be awkward to install on some platforms.

---

## Updating a Config File

1. Edit the config file (e.g. `docker/tempo-config.yaml`).
2. Restart only the affected container:
   ```bash
   docker compose -f docker/docker-compose.observability.yml restart tempo
   ```
3. Verify with logs:
   ```bash
   docker logs awcp-tempo --tail 20
   ```

No other containers need to restart unless the port or network changed.