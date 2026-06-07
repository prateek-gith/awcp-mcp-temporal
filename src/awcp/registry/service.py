import hashlib
import inspect
import os
import getpass

from awcp.agents.base import AgentSpec
from awcp.registry.discovery import discover_agents
from awcp.registry.models import AgentEntry
from awcp.registry.store import populate


def _make_stable_id(route: str) -> str:
    """Generate a stable ID by hashing the agent's route."""
    return f"agt_{hashlib.md5(route.encode()).hexdigest()[:8]}"


def _hash_agent_file(spec: AgentSpec) -> str:
    """
    Locate the physical source file of the agent's handler function
    and hash its contents to produce a deterministic version string.
    """
    try:
        file_path = inspect.getfile(spec.handler)
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        return f"1.0.0-auto+{hashlib.md5(file_bytes).hexdigest()[:7]}"
    except (TypeError, OSError):
        return "1.0.0-auto+unknown"


def build_registry() -> list[AgentSpec]:
    """
    Discover agents, dynamically infer governance metadata, enforce
    admission control, populate the in-memory store, and return the
    raw AgentSpec list so the caller can wire FastAPI routes.

    Environment variables:
      AWCP_TUNNEL_BASE_URL    - Public base URL for endpoint_url construction.
                                Defaults to http://localhost:8001.
      AWCP_DEFAULT_OWNER      - Owner assigned to all agents.
                                Defaults to the current OS username.
      AWCP_TELEMETRY_ENABLED  - Master telemetry flag. Defaults to "true".
                                Agents with write_scopes are quarantined
                                when this is "false".
    """
    base_url: str = os.getenv("AWCP_TUNNEL_BASE_URL", "http://localhost:8001")
    env_owner: str = os.getenv("AWCP_DEFAULT_OWNER", getpass.getuser())
    telemetry_on: bool = os.getenv("AWCP_TELEMETRY_ENABLED", "true").lower() == "true"

    specs: list[AgentSpec] = discover_agents()

    entries: list[AgentEntry] = []

    for spec in specs:
        stable_id = _make_stable_id(spec.route)
        dynamic_version = spec.version if spec.version is not None else _hash_agent_file(spec)
        owner = spec.owner if spec.owner is not None else env_owner
        feature_flags = {**spec.feature_flags, "telemetry_enabled": telemetry_on}

        # Admission control: quarantine agents that declare write scopes
        # but are running without telemetry enabled.
        if len(spec.write_scopes) > 0 and not telemetry_on:
            status = "quarantined"
        else:
            status = "active"

        entries.append(AgentEntry(
            agent_id=stable_id,
            name=spec.name,
            route=spec.route,
            endpoint_url=f"{base_url.rstrip('/')}{spec.route}",
            runtime=spec.runtime,
            version=dynamic_version,
            owner=owner,
            write_scopes=spec.write_scopes,
            feature_flags=feature_flags,
            status=status,
        ))

    populate(entries)

    return specs
