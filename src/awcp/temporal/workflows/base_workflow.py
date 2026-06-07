from temporalio.common import RetryPolicy
from datetime import timedelta
from dataclasses import dataclass
from typing import Any, Dict, Optional

# ==========================================
# Standard Retry Policies
# ==========================================

# Use this for fast, internal API calls (like fetching from the Registry)
# It retries quickly but gives up after 3 attempts so workflows don't hang.
FAST_INTERNAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)

# Use this for LLM Agent executions
# We limit attempts strictly to 2 so that we can gracefully degrade 
# to 'recommendation_only' if the agent keeps failing (e.g., LLM hallucinations).
AGENT_EXECUTION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=2,
    # Do not retry if the error is a definitive code crash (only retry network/transient issues)
    non_retryable_error_types=["ValueError", "TypeError", "KeyError"] 
)

TOOL_EXECUTION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

SYNTHESIS_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=2,
    non_retryable_error_types=["ValueError", "TypeError", "KeyError"],
)

# ==========================================
# Standard Payload Dataclasses (Optional)
# ==========================================
# Temporal natively supports dataclasses. While we used standard dictionaries 
# in the MVP for speed, you can use these classes to strictly type your inputs!

@dataclass
class AgentExecutionInput:
    agent_name: str
    input: str

@dataclass
class AgentExecutionResult:
    system_action: str
    result: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    error: Optional[str] = None
    agent_details: Optional[Dict[str, Any]] = None
