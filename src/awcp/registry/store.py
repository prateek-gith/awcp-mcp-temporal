from awcp.registry.models import AgentEntry


# In-memory registry — populated once at startup, never mutated at runtime.
_registry: dict[str, AgentEntry] = {}


def populate(entries: list[AgentEntry]) -> None:
    """Replace the registry contents. Called once during startup."""
    _registry.clear()
    for entry in entries:
        _registry[entry.agent_id] = entry


def get_all() -> list[AgentEntry]:
    return list(_registry.values())


def get_by_id(agent_id: str) -> AgentEntry | None:
    return _registry.get(agent_id)
