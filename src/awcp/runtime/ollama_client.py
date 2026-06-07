import requests

from awcp.runtime.config import OLLAMA_BASE


def ask_ollama(prompt: str, model: str) -> str:

    response = requests.post(
        f"{OLLAMA_BASE}/api/chat",
        json={
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        },
        timeout=120
    )

    response.raise_for_status()

    return response.json()["message"]["content"].strip()
