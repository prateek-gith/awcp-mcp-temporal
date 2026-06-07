import aiohttp
from temporalio import activity
from awcp.temporal.config import FASTAPI_BASE_URL

@activity.defn
async def execute_agent(payload: dict) -> dict:
    """Posts the execution payload to the local FastAPI agent endpoint."""
    route = payload.pop("route") # Extract route, don't send it in body
    url = f"{FASTAPI_BASE_URL.rstrip('/')}{route}"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                # Throwing an exception triggers Temporal's automatic retry or workflow catch block
                raise RuntimeError(f"Agent execution failed with status {response.status}: {error_text}")
            
            return await response.json()