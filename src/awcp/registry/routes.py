from fastapi import APIRouter, HTTPException

from awcp.registry.models import AgentEntry
from awcp.registry import store

router = APIRouter(prefix="/agents", tags=["registry"])


@router.get("", response_model=list[AgentEntry])
def list_agents() -> list[AgentEntry]:
    return store.get_all()


@router.get("/{agent_id}", response_model=AgentEntry)
def get_agent(agent_id: str) -> AgentEntry:
    entry = store.get_by_id(agent_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return entry
