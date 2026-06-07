from contextvars import ContextVar
from typing import Any


EXECUTION_EVENTS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "EXECUTION_EVENTS",
    default=None
)

CURRENT_PROFILE: ContextVar[str] = ContextVar("CURRENT_PROFILE", default="active")


def begin_execution_capture():

    return EXECUTION_EVENTS.set([])


def end_execution_capture(token) -> None:

    EXECUTION_EVENTS.reset(token)


def get_execution_events() -> list[dict[str, Any]]:

    return EXECUTION_EVENTS.get() or []


def get_tool_events() -> list[dict[str, Any]]:

    raw = [
        event
        for event in get_execution_events()
        if event.get("event_type") == "tool_call"
    ]

    merged = {}

    for event in raw:
        tool_name = event.get("tool_name")
        status    = event.get("status")

        if tool_name not in merged:
            merged[tool_name] = {
                "tool_name": tool_name,
                "input":     event.get("input"),
                "status":    status,
            }
        else:
            merged[tool_name]["status"] = status
            if "output" in event:
                merged[tool_name]["output"] = event["output"]
            if "error" in event:
                merged[tool_name]["error"] = event["error"]

    return list(merged.values())


def emit_execution_event(event: dict[str, Any]) -> None:

    events = EXECUTION_EVENTS.get()

    if events is not None:
        events.append(event)
