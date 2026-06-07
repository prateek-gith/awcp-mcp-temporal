from typing import Any

from awcp.agents.base import AgentSpec
from awcp.runtime.config import GEMMA_MODEL
from awcp.runtime.ollama_client import ask_ollama
from awcp.runtime.schemas import PromptRequest


def run(req: PromptRequest) -> dict[str, Any]:

    output = ask_ollama(
        req.input,
        GEMMA_MODEL
    )

    return {
        "input": req.input,
        "model": GEMMA_MODEL,
        "output": output
    }


AGENT = AgentSpec(
    name="ollama",
    route="/chat/ollama",
    request_model=PromptRequest,
    handler=run,
    runtime="ollama",
    model=GEMMA_MODEL,
)
