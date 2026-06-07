import aiohttp
from temporalio import activity
from awcp.temporal.config import FASTAPI_BASE_URL

@activity.defn
async def fetch_agent_registry(agent_name: str) -> dict:
    """Fetches the agent's identity, scopes, and quarantine status from the registry."""
    url = f"{FASTAPI_BASE_URL.rstrip('/')}/agents"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(f"Failed to fetch registry. Status: {response.status}")
            
            agents = await response.json()
            
            # Find the specific agent
            for agent in agents:
                if agent["name"] == agent_name:
                    return agent
                    
            raise ValueError(f"Agent '{agent_name}' not found in registry.")