from pydantic import BaseModel


class AgentEntry(BaseModel):
    agent_id: str
    name: str
    route: str
    endpoint_url: str
    runtime: str
    version: str
    owner: str
    write_scopes: list[str]
    feature_flags: dict[str, bool]
    status: str = "active"
