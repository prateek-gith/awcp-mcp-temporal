import importlib
import pkgutil

import awcp.agents as agents_pkg
from awcp.agents.base import AgentSpec


_EXCLUDED_SUFFIXES: tuple[str, ...] = (".base", ".discovery")


def discover_agents() -> list[AgentSpec]:

    agents: list[AgentSpec] = []

    for module in pkgutil.iter_modules(agents_pkg.__path__, agents_pkg.__name__ + "."):

        if any(module.name.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES):
            continue

        imported = importlib.import_module(module.name)
        agent: AgentSpec | None = getattr(imported, "AGENT", None)

        if agent is not None:
            agents.append(agent)

    return agents
