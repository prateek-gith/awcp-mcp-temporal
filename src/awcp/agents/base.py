from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel


@dataclass(frozen=True)
class AgentSpec:
    name: str
    route: str
    request_model: type[BaseModel]
    handler: Callable[[Any], dict[str, Any]]
    runtime: str = "unknown"
    owner: str | None = None
    version: str | None = None
    write_scopes: list[str] = field(default_factory=list)
    feature_flags: dict[str, bool] = field(default_factory=dict)

    # --- Self-declared execution metadata (used by the MCP-driven path) ---
    # These let the control plane (Temporal + MCP server) drive an agent's
    # reason -> tool -> generate loop generically, with no per-agent special
    # casing in the server. New tool-using agents work end-to-end just by
    # declaring these here.
    #
    #   model  : the model used for direct/grounded generation.
    #   router : optional fn(prompt) -> {"action": "SEARCH"|"ANSWER",
    #            "search_query": str}. Absent => the agent never uses a tool
    #            (always ANSWER).
    #   tool   : the tool invoked when the router decides SEARCH (e.g.
    #            "web_search"). The route decision may also carry its own
    #            "tool_name"/"tool_input" to override this.
    model: str | None = None
    router: Callable[[str], dict[str, Any]] | None = None
    tool: str | None = None
