from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response

from awcp.registry.service import build_registry
from awcp.registry.routes import router as registry_router
from awcp.registry.store import get_all
from awcp.runtime.config import GEMMA_MODEL, NVIDIA_MODEL, OLLAMA_BASE, SEARCH_MODEL
from awcp.runtime.event_runtime import (
    begin_execution_capture,
    emit_execution_event,
    end_execution_capture,
    get_execution_events,
    get_tool_events,
    CURRENT_PROFILE,
)
from awcp.runtime.schemas import AgentErrorResponse, AutonomyProfile
from awcp.runtime.tool_runtime import discover_tools, TOOL_REGISTRY


app = FastAPI()


discover_tools()
app.include_router(registry_router)

# Global to hold agent specs, will be populated at startup
_agent_specs = []


def attach_execution_events(response: dict[str, Any]) -> dict[str, Any]:
    """Enrich a successful agent response with tool-call telemetry."""
    tool_events = get_tool_events()
    if tool_events:
        response["tool_calls"] = [
            {"tool_name": e["tool_name"], "status": e["status"]}
            for e in tool_events
        ]
    return response


def build_agent_endpoint(agent):
    """
    Wraps an AgentSpec handler with:
      - Quarantine gate (blocks quarantined agents with 403)
      - Execution event capture (ContextVar-scoped)
      - Universal governance gate (RECOMMENDATION_ONLY prompt rewrite)
      - Structured error responses (AgentErrorResponse)
    """

    def endpoint(req):

        token = begin_execution_capture()

        try:
            # --- Quarantine Gate ---
            # Checks the registry status before any execution.
            # Quarantined agents are blocked immediately with 403.
            registry_entry = next(
                (e for e in get_all() if e.name == agent.name), None
            )
            if registry_entry is not None and registry_entry.status == "quarantined":
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Agent '{agent.name}' is in QUARANTINE due to missing "
                        f"telemetry requirements for its write scopes. Execution blocked."
                    )
                )
            # --- End Quarantine Gate ---

            emit_execution_event(
                {
                    "event_type": "agent",
                    "agent_name": agent.name,
                    "status": "started",
                    "input": {
                        "input": getattr(req, "input", None)
                    }
                }
            )

            # --- Universal Governance Gate ---
            # Sets the governance profile in the ContextVar so the tool
            # runtime can physically block execution without touching the prompt.
            profile_token = CURRENT_PROFILE.set(
                getattr(req, "autonomy_profile", "active")
            )
            # --- End Universal Governance Gate ---

            response = agent.handler(req)

            emit_execution_event(
                {
                    "event_type": "agent",
                    "agent_name": agent.name,
                    "status": "succeeded"
                }
            )

            return attach_execution_events(response)

        except Exception as e:
            emit_execution_event(
                {
                    "event_type": "agent",
                    "agent_name": agent.name,
                    "status": "failed",
                    "error": str(e)
                }
            )

            error_response = AgentErrorResponse(
                error_type=type(e).__name__,
                message=str(e),
                agent_name=agent.name,
                execution_events=get_execution_events(),
                tool_calls=get_tool_events(),
                autonomy_profile=str(
                    getattr(req, "autonomy_profile", AutonomyProfile.ACTIVE)
                )
            )

            raise HTTPException(
                status_code=502,
                detail=error_response.model_dump()
            )

        finally:
            end_execution_capture(token)
            if "profile_token" in locals():
                CURRENT_PROFILE.reset(profile_token)

    endpoint.__name__ = f"run_{agent.name.replace('-', '_')}"
    endpoint.__annotations__ = {"req": agent.request_model}

    return endpoint


@app.on_event("startup")
def register_agents():
    """
    Register agent routes dynamically at startup.
    This ensures agents are discovered fresh each time the server restarts.
    """
    global _agent_specs
    
    # Discover and register agents
    _agent_specs = build_registry()
    
    # Wire up all agent routes
    for agent in _agent_specs:
        app.post(agent.route)(build_agent_endpoint(agent))
    
    print(f"\n{'='*60}")
    print(f"✓ Registered {len(_agent_specs)} agents:")
    for agent in _agent_specs:
        print(f"  - {agent.name} -> {agent.route}")
    print(f"{'='*60}\n")


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    """
    Active health check. Pings the local Ollama runtime and reports
    registered agent and tool counts. Returns HTTP 503 if Ollama is
    unreachable so load balancers and orchestrators can act on it.
    """
    ollama_reachable = False
    ollama_version: str | None = None

    try:
        ollama_response = httpx.get(
            f"{OLLAMA_BASE}/api/version",
            timeout=2.0
        )
        ollama_response.raise_for_status()
        ollama_version = ollama_response.json().get("version")
        ollama_reachable = True
    except Exception:
        pass

    if not ollama_reachable:
        response.status_code = 503

    return {
        "status": "healthy" if ollama_reachable else "degraded",
        "ollama_reachable": ollama_reachable,
        "ollama_version": ollama_version,
        "ollama_base": OLLAMA_BASE,
        "agent_count": len(get_all()),
        "tool_count": len(TOOL_REGISTRY),
        "models": {
            "gemma": GEMMA_MODEL,
            "search": SEARCH_MODEL,
            "deepseek": NVIDIA_MODEL
        }
    }
