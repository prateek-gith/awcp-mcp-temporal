"""Governance evidence helpers for the AWCP control plane.

Pure, deterministic functions — no I/O, no randomness.
Safe to call inside Temporal workflow code (determinism requirement) and from
activities. Used across mcp_gateway, dynamic_ask, and the control API to emit
consistent evidence metadata into spans, logs, and the workflow result.

Evidence fields described in the AWCP Magazine document:
  - context_hash          SHA-256 of the workflow's governing inputs
  - checkpoint_id         Step-scoped identifier for replay resume points
  - decision_path         Human-readable ordered sequence of steps taken
  - tool_execution_sequence  Ordered list of tool names called
  - degradation_mode      Current autonomy level of the workflow branch
  - make_evidence_extra   Structured dict ready for logger.info(..., extra=...)
"""

import hashlib
from typing import Any


# ---------------------------------------------------------------------------
# Degradation mode ladder (document §01 Step 04, §02 Scenario A)
# ---------------------------------------------------------------------------

DEGRADATION_NORMAL = "normal"
DEGRADATION_LIMITED = "limited"                   # one or more tool failures
DEGRADATION_RECOMMENDATION_ONLY = "recommendation_only"  # synthesis fallback
DEGRADATION_SAFE_MODE = "safe_mode"               # escalation (future)


def determine_degradation_mode(
    tool_failure_count: int = 0,
    synthesis_fallback: bool = False,
) -> str:
    """Derive degradation_mode from observed failure signals.

    Ladder (matches document §01 Step 04 progressive autonomy reduction):
      normal             → no failures
      limited            → ≥1 tool failure, synthesis succeeded
      recommendation_only → synthesis had to fall back (no LLM synthesis)
    """
    if synthesis_fallback:
        return DEGRADATION_RECOMMENDATION_ONLY
    if tool_failure_count > 0:
        return DEGRADATION_LIMITED
    return DEGRADATION_NORMAL


# ---------------------------------------------------------------------------
# Context hash (document: "context snapshot hash", evidence ledger)
# ---------------------------------------------------------------------------

def build_context_hash(
    workflow_id: str,
    query: str,
    tool_names: list[str] | None = None,
) -> str:
    """SHA-256 of the workflow's governing inputs.

    Deterministic: same inputs always produce the same hash so the evidence
    ledger can detect context drift across retries or replays.
    """
    tool_names = tool_names or []
    raw = f"{workflow_id}|{query}|{','.join(sorted(tool_names))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]   # first 16 hex chars


# ---------------------------------------------------------------------------
# Checkpoint ID (document: "resume points", "replayable ledger")
# ---------------------------------------------------------------------------

def build_checkpoint_id(workflow_id: str, step_name: str) -> str:
    """Step-scoped identifier for evidence ledger entries and resume pointers.

    Format: ``<workflow_id>:<step_name>``
    Example: ``awcp-ask-abc123:run_tool``
    """
    return f"{workflow_id}:{step_name}"


# ---------------------------------------------------------------------------
# Decision path (document: "decision path", context engineering diagram)
# ---------------------------------------------------------------------------

def build_decision_path(steps_taken: list[str]) -> str:
    """Ordered human-readable string of steps actually executed.

    Example: ``llm_decision→tool_discovery→run_tool[web_search]→synthesis``
    """
    return "→".join(steps_taken) if steps_taken else "llm_decision"


# ---------------------------------------------------------------------------
# Structured log extra dict (document §03 evidence logging requirement)
# ---------------------------------------------------------------------------

def make_evidence_extra(**fields: Any) -> dict[str, Any]:
    """Build a structured extra dict for evidence-oriented logger calls.

    Only non-None values are included so Loki label cardinality stays low.

    Usage::

        logger.info("tool_execution_completed", extra=make_evidence_extra(
            workflow_id=workflow_id,
            tool_name=tool_name,
            degradation_mode=degradation_mode,
            trace_id=trace_id,
        ))
    """
    return {k: v for k, v in fields.items() if v is not None}


# ---------------------------------------------------------------------------
# OTel span attribute helpers
# ---------------------------------------------------------------------------

def set_governance_attributes(
    span: Any,
    *,
    workflow_id: str | None = None,
    agent_id: str | None = None,
    runtime: str | None = None,
    policy_mode: str | None = None,
    autonomy_mode: str | None = None,
    degradation_mode: str | None = None,
    tool_name: str | None = None,
    tool_type: str | None = None,
    write_capable: bool | None = None,
    owner: str | None = None,
    checkpoint_id: str | None = None,
    context_hash: str | None = None,
    retry_count: int | None = None,
    decision_id: str | None = None,
) -> None:
    """Attach all available governance attributes to an OTel span.

    Silently skips None values so callers don't need to guard each field.
    Uses the ``awcp.`` namespace for all governance-specific attributes.
    """
    attrs: dict[str, Any] = {
        "awcp.workflow_id": workflow_id,
        "awcp.agent_id": agent_id,
        "awcp.runtime": runtime,
        "awcp.policy_mode": policy_mode,
        "awcp.autonomy_mode": autonomy_mode,
        "awcp.degradation_mode": degradation_mode,
        "awcp.tool_name": tool_name,
        "awcp.tool_type": tool_type,
        "awcp.write_capable": write_capable,
        "awcp.owner": owner,
        "awcp.checkpoint_id": checkpoint_id,
        "awcp.context_hash": context_hash,
        "awcp.retry_count": retry_count,
        "awcp.decision_id": decision_id,
    }
    for key, value in attrs.items():
        if value is not None:
            try:
                span.set_attribute(key, value)
            except Exception:  # noqa: BLE001 — never crash span code
                pass
