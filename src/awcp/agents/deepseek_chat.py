from typing import Any

from openai import OpenAI

from awcp.agents.base import AgentSpec
from awcp.runtime.config import NVIDIA_BASE, NVIDIA_MODEL
from awcp.runtime.schemas import NvidiaPromptRequest


def run(req: NvidiaPromptRequest) -> dict[str, Any]:

    client = OpenAI(
        base_url=NVIDIA_BASE,
        api_key=req.api_key
    )

    completion = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[
            {
                "role": "user",
                "content": req.input
            }
        ],
        temperature=1,
        top_p=0.95,
        max_tokens=16384,
        extra_body={
            "chat_template_kwargs": {
                "thinking": True,
                "reasoning_effort": "low"
            }
        },
        stream=False,
    )

    output = completion.choices[0].message.content

    return {
        "input": req.input,
        "model": NVIDIA_MODEL,
        "output": output
    }


AGENT = AgentSpec(
    name="deepseek",
    route="/chat/deepseek",
    request_model=NvidiaPromptRequest,
    handler=run,
    runtime="nvidia"
)
