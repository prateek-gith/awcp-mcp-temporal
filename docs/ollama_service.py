import json
import re
import requests

from openai import OpenAI
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from ddgs import DDGS

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────

OLLAMA_BASE = "http://localhost:11434"

# Pure Ollama endpoint
GEMMA_MODEL = "gemma2:2b"

# Search-enabled agent endpoint
SEARCH_MODEL = "llama3.1:8b"

FACT_PATTERNS = [
    "when",
    "who",
    "where",
    "current",
    "latest",
    "today",
    "price",
    "stock",
    "ceo",
    "founder",
    "date",
    "year"
]

# NVIDIA DeepSeek
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "deepseek-ai/deepseek-v4-flash"

app = FastAPI()

# ──────────────────────────────────────────────────────────────
# Request Models
# ──────────────────────────────────────────────────────────────

class PromptRequest(BaseModel):
    input: str


class NvidiaPromptRequest(BaseModel):
    input: str
    api_key: str


# ──────────────────────────────────────────────────────────────
# Ollama Helper
# ──────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────
# JSON Extraction Helper
# ──────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict:
    """
    Attempts to extract the first JSON object
    from model output.
    """

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)

    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass

    raise ValueError("No valid JSON found")


# ──────────────────────────────────────────────────────────────
# Agent Decision
# ──────────────────────────────────────────────────────────────

def should_search(query: str) -> bool:

    q = query.lower()

    return any(
        word in q
        for word in FACT_PATTERNS
    )


def decide_action(query: str) -> dict:

    prompt = f"""
You are a routing agent.

Determine whether answering the user's question requires
external information.

Use SEARCH if the question involves:

- current information
- recent events
- dates
- timelines
- people
- companies
- products
- news
- prices
- statistics
- rankings
- facts that may have changed
- factual verification

Use ANSWER only for:

- math
- logic
- coding
- writing
- explanations
- definitions
- general knowledge concepts

When in doubt choose SEARCH.

Return ONLY JSON.

Examples:

Question: current gold prices
{{"action":"SEARCH","search_query":"current gold prices"}}

Question: who is the CEO of OpenAI
{{"action":"SEARCH","search_query":"OpenAI CEO"}}

Question: when did katsuhiro harada leave bandai namco
{{"action":"SEARCH","search_query":"Katsuhiro Harada leave Bandai Namco"}}

Question: explain recursion
{{"action":"ANSWER","response":"placeholder"}}

User Question:
{query}
"""

    response = ask_ollama(
        prompt,
        SEARCH_MODEL
    )

    return extract_json(response)


# ──────────────────────────────────────────────────────────────
# DuckDuckGo Search
# ──────────────────────────────────────────────────────────────

def run_web_search(query: str) -> str:

    try:

        search_queries = [
            {
                "query": f"{query} latest recent",
                "timelimit": "y"
            },
            {
                "query": query,
                "timelimit": None
            }
        ]

        q = query.lower()

        if any(word in q for word in ["gross", "box office", "worldwide", "movie"]):
            search_queries.append(
                {
                    "query": f"{query} box office mojo the numbers worldwide gross",
                    "timelimit": None
                }
            )

        results = []
        seen_urls = set()

        for search in search_queries:

            batch = list(
                DDGS().text(
                    search["query"],
                    region="wt-wt",
                    safesearch="off",
                    timelimit=search["timelimit"],
                    max_results=10
                )
            )

            for result in batch:

                url = result.get("href", "")

                if url in seen_urls:
                    continue

                seen_urls.add(url)
                results.append(result)

                if len(results) >= 10:
                    break

            if len(results) >= 10:
                break

        if not results:
            return ""

        output = []

        for idx, result in enumerate(results, start=1):

            output.append(
                f"""
Result {idx}
Title: {result.get('title', '')}
Snippet: {result.get('body', '')}
URL: {result.get('href', '')}
"""
            )

        return "\n".join(output)

    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────
# Search Answer Prompt
# ──────────────────────────────────────────────────────────────

def build_search_answer_prompt(question: str, search_results: str) -> str:

    return f"""
You are a factual QA assistant.

Answer ONLY using the search results provided.

Rules:

1. Treat search results as the source of truth.
2. Do not use prior knowledge.
3. Do not speculate.
4. If search results disagree, say so.
5. If information is missing, say you could not find it.
6. Give a concise factual answer.
7. Do not include references, citations, URLs, or result numbers.

User Question:
{question}

Search Results:
{search_results}

Answer:
"""


# ──────────────────────────────────────────────────────────────
# Endpoint 1
# Pure Gemma
# ──────────────────────────────────────────────────────────────

@app.post("/chat/ollama")
def run_ollama(req: PromptRequest):

    try:

        output = ask_ollama(
            req.input,
            GEMMA_MODEL
        )

        return {
            "input": req.input,
            "model": GEMMA_MODEL,
            "output": output
        }

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=str(e)
        )


# ──────────────────────────────────────────────────────────────
# Endpoint 2
# Llama Agent + Search
# ──────────────────────────────────────────────────────────────

@app.post("/chat/ollama-search")
def run_search(req: PromptRequest):

    try:

        # Step 1: Use deterministic routing for factual/current queries.

        try:
            if should_search(req.input):
                decision = {
                    "action": "SEARCH",
                    "search_query": req.input
                }
            else:
                decision = decide_action(req.input)

            print("\n===== AGENT DECISION =====")
            print(decision)
            print("==========================\n")

        except Exception as e:

            print("\n===== DECISION FAILED =====")
            print(str(e))
            print("===========================\n")

            # Fallback: try search first

            search_results = run_web_search(req.input)
            print("\n===== SEARCH RESULTS =====")
            print(search_results)
            print("==========================\n")

            if search_results:

                prompt = build_search_answer_prompt(
                    req.input,
                    search_results
                )

                output = ask_ollama(
                    prompt,
                    SEARCH_MODEL
                )

                return {
                    "input": req.input,
                    "model": SEARCH_MODEL,
                    "output": output,
                    "search_used": True,
                    "search_query": req.input
                }

            # Final fallback

            output = ask_ollama(
                req.input,
                SEARCH_MODEL
            )

            return {
                "input": req.input,
                "model": SEARCH_MODEL,
                "output": output,
                "search_used": False
            }

        action = decision.get("action")

        # Direct answer

        if action == "ANSWER":

            output = ask_ollama(
                req.input,
                SEARCH_MODEL
            )

            return {
                "input": req.input,
                "model": SEARCH_MODEL,
                "output": output,
                "search_used": False
            }

        # Search branch

        if action == "SEARCH":

            search_query = decision.get(
                "search_query",
                req.input
            )

            search_results = run_web_search(
                search_query
            )

            if not search_results:

                output = ask_ollama(
                    req.input,
                    SEARCH_MODEL
                )

                return {
                    "input": req.input,
                    "model": SEARCH_MODEL,
                    "output": output,
                    "search_used": False
                }

            # IMPORTANT:
            # Answer using search results.
            # Do NOT ask for another action decision.

            prompt = build_search_answer_prompt(
                req.input,
                search_results
            )

            final_output = ask_ollama(
                prompt,
                SEARCH_MODEL
            )

            return {
                "input": req.input,
                "model": SEARCH_MODEL,
                "output": final_output,
                "search_used": True,
                "search_query": search_query
            }

        # Unexpected action

        output = ask_ollama(
            req.input,
            SEARCH_MODEL
        )

        return {
            "input": req.input,
            "model": SEARCH_MODEL,
            "output": output,
            "search_used": False
        }

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=str(e)
        )

# ──────────────────────────────────────────────────────────────
# Endpoint 3
# NVIDIA DeepSeek
# ──────────────────────────────────────────────────────────────

@app.post("/chat/deepseek")
def run_deepseek(req: NvidiaPromptRequest):

    try:

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

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=str(e)
        )


# ──────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():

    return {
        "status": "healthy",
        "ollama_base": OLLAMA_BASE,
        "gemma_model": GEMMA_MODEL,
        "search_model": SEARCH_MODEL,
        "deepseek_model": NVIDIA_MODEL
    }
