import asyncio
import logging
from temporalio.client import Client
from temporalio.worker import Worker

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

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Connect to Temporal Server
    client = await Client.connect(TEMPORAL_SERVER_URL)
    logging.info(f"Connected to Temporal Server at {TEMPORAL_SERVER_URL}")

    # Initialize the Worker
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
    )

    logging.info(f"Worker started on task queue: '{TASK_QUEUE_NAME}'. Waiting for jobs...")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
