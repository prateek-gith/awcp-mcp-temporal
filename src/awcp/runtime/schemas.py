from enum import Enum

from pydantic import BaseModel, Field


class AutonomyProfile(str, Enum):
    ACTIVE = "active"
    RECOMMENDATION_ONLY = "recommendation_only"


class PromptRequest(BaseModel):
    input: str
    autonomy_profile: AutonomyProfile = Field(
        default=AutonomyProfile.ACTIVE,
        description="AWCP Governance permission profile"
    )


class NvidiaPromptRequest(BaseModel):
    input: str
    api_key: str
    autonomy_profile: AutonomyProfile = Field(
        default=AutonomyProfile.ACTIVE,
        description="AWCP Governance permission profile"
    )


class AgentErrorResponse(BaseModel):
    error_type: str
    message: str
    agent_name: str
    execution_events: list[dict]
    tool_calls: list[dict]
    autonomy_profile: str
