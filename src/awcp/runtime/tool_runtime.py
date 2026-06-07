import importlib
import pkgutil

from typing import Any, Callable

from awcp.runtime.event_runtime import emit_execution_event, CURRENT_PROFILE


TOOL_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_tool(name: str, handler: Callable[..., Any]) -> None:

    TOOL_REGISTRY[name] = handler


def tool(name: str):

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:

        register_tool(name, handler)

        return handler

    return decorator


def summarize_tool_output(output: Any) -> dict[str, Any]:

    text = str(output)

    return {
        "type": type(output).__name__,
        "preview": text[:500]
    }


def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any]
) -> Any:

    if CURRENT_PROFILE.get() == "recommendation_only":
        block_msg = (
            f"SYSTEM CONTROL BLOCK: Write permissions are REVOKED. "
            f"Tool '{tool_name}' execution denied. "
            f"You must fall back to recommendation mode based on your existing knowledge."
        )
        emit_execution_event({
            "event_type": "tool_call",
            "tool_name": tool_name,
            "status": "blocked",
            "input": tool_input,
            "error": block_msg,
        })
        return block_msg

    handler = TOOL_REGISTRY.get(tool_name)

    if not handler:
        raise ValueError(f"Unknown tool: {tool_name}")

    event = {
        "event_type": "tool_call",
        "tool_name": tool_name,
        "status": "started",
        "input": tool_input
    }

    emit_execution_event(event.copy())

    try:
        output = handler(**tool_input)

        event["status"] = "succeeded"
        event["output"] = summarize_tool_output(output)
        emit_execution_event(event.copy())

        return output

    except Exception as e:
        event["status"] = "failed"
        event["error"] = str(e)
        emit_execution_event(event.copy())

        raise


def discover_tools(package_name: str = "awcp.tools") -> None:

    package = importlib.import_module(package_name)

    for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        importlib.import_module(module.name)
