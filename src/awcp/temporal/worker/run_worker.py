"""Temporal worker entry-point.

Starts the AWCP worker process, connects to Temporal, and registers all
workflows + activities. OpenTelemetry is configured here so the worker process
exports its own traces, metrics, and logs alongside the control API.

If the ``temporalio`` package ships an OpenTelemetry interceptor
(``temporalio.contrib.opentelemetry``), it is installed automatically to give
Temporal-native spans for every workflow schedule, activity dispatch, and retry.
The manual spans added inside each activity function complement these with
fine-grained inner-step visibility (LLM calls, MCP calls, tool execution).
"""

import asyncio
import logging

from dotenv import load_dotenv

# Load .env before anything reads environment variables.
load_dotenv()

from temporalio.client import Client
from temporalio.worker import Worker

from awcp.observability import configure_observability
from awcp.temporal.config import TEMPORAL_SERVER_URL, TASK_QUEUE_NAME
from awcp.temporal.workflows.agent_execution import AgentGovernanceWorkflow
from awcp.temporal.workflows.dynamic_ask import DynamicAskWorkflow
from awcp.temporal.activities.mcp_gateway import (
    mcp_call_llm,
    mcp_discover_tools,
    mcp_get_agent_info,
    mcp_agent_route,
    mcp_execute_tool,
    mcp_agent_generate,
    mcp_run_tool,
    mcp_select_tools,
    mcp_synthesize_answer,
)

logger = logging.getLogger(__name__)


def _build_interceptors() -> list:
    """Return OTel interceptors if the temporalio contrib package is available.

    The interceptor attaches to every workflow and activity execution, creating
    parent spans automatically so the full workflow tree appears in Tempo without
    any extra manual instrumentation at the workflow level.
    """
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor
        tracer_interceptor = TracingInterceptor()
        logger.info("Temporal OpenTelemetry interceptor enabled")
        return [tracer_interceptor]
    except ImportError:
        logger.info(
            "temporalio.contrib.opentelemetry not available; "
            "using manual activity spans only"
        )
        return []


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Configure OTel for the worker process.
    # mcp_gateway module also calls this (idempotent — runs once per process).
    configure_observability(component="temporal-worker")
    logger.info("OpenTelemetry configured for temporal-worker")

    # Connect to Temporal Server.
    client = await Client.connect(TEMPORAL_SERVER_URL)
    logger.info("temporal_worker_connected server=%s", TEMPORAL_SERVER_URL)

    interceptors = _build_interceptors()

    # Initialize the Worker.
    worker = Worker(
        client,
        task_queue=TASK_QUEUE_NAME,
        workflows=[AgentGovernanceWorkflow, DynamicAskWorkflow],
        activities=[
            mcp_get_agent_info,
            mcp_agent_route,
            mcp_execute_tool,
            mcp_agent_generate,
            mcp_call_llm,
            mcp_discover_tools,
            mcp_select_tools,
            mcp_run_tool,
            mcp_synthesize_answer,
        ],
        interceptors=interceptors,
    )

    logger.info(
        "temporal_worker_started task_queue=%s interceptors=%s",
        TASK_QUEUE_NAME,
        len(interceptors),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
