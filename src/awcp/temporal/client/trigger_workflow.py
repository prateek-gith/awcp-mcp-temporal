import asyncio
import uuid
from temporalio.client import Client
from awcp.temporal.config import TEMPORAL_SERVER_URL, TASK_QUEUE_NAME
from awcp.temporal.workflows.agent_execution import AgentGovernanceWorkflow

async def trigger_agent_workflow(agent_name: str, prompt: str):
    # Connect to Temporal Server
    client = await Client.connect(TEMPORAL_SERVER_URL)
    
    # Generate a unique workflow ID
    workflow_id = f"awcp-exec-{agent_name}-{uuid.uuid4().hex[:8]}"
    
    workflow_input = {
        "agent_name": agent_name,
        "input": prompt
    }
    
    print(f"Triggering Workflow ID: {workflow_id}")
    
    # Execute the workflow and wait for the result
    result = await client.execute_workflow(
        AgentGovernanceWorkflow.run,
        workflow_input,
        id=workflow_id,
        task_queue=TASK_QUEUE_NAME,
    )
    
    print("\n--- WORKFLOW COMPLETED ---")
    print(f"System Action: {result.get('system_action')}")
    print(result)
    return result

if __name__ == "__main__":
    # Example usage for testing locally
    test_agent = "ollama-search"
    test_prompt = "What is the price of apple per kg in Maharashtra?"
    
    asyncio.run(trigger_agent_workflow(test_agent, test_prompt))