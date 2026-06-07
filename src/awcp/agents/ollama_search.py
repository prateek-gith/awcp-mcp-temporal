from typing import Any

from awcp.agents.base import AgentSpec
from awcp.runtime.config import FACT_PATTERNS, SEARCH_MODEL
from awcp.runtime.json_utils import extract_json
from awcp.runtime.ollama_client import ask_ollama
from awcp.runtime.schemas import PromptRequest
from awcp.runtime.tool_runtime import execute_tool


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


def answer_from_search(req: PromptRequest, search_query: str) -> dict[str, Any]:

    search_results = execute_tool(
        "web_search",
        {"query": search_query}
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

    search_answer_prompt = build_search_answer_prompt(
        req.input,
        str(search_results)
    )

    output = ask_ollama(
        search_answer_prompt,
        SEARCH_MODEL
    )

    return {
        "input": req.input,
        "model": SEARCH_MODEL,
        "output": output,
        "search_used": True,
        "search_query": search_query
    }


def run(req: PromptRequest) -> dict[str, Any]:
    """
    Search agent handler.

    Governance enforcement is handled upstream by the central middleware
    in agent_service.py. By the time this function is called, req.input
    has already been rewritten if the autonomy_profile is
    RECOMMENDATION_ONLY, so this handler runs its normal routing logic
    unconditionally.
    """

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

    if action == "SEARCH":
        search_query = decision.get(
            "search_query",
            req.input
        )

        return answer_from_search(
            req,
            search_query
        )

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


def route(prompt: str) -> dict[str, Any]:
    """Tool-routing decision for the control plane (Temporal/MCP).

    Mirrors the in-handler logic: deterministic keyword pre-filter first, then
    the LLM router. Returns {"action": "SEARCH"|"ANSWER", "search_query": ...}.
    Declared on AGENT.router so the MCP server can call it generically.
    """
    if should_search(prompt):
        return {"action": "SEARCH", "search_query": prompt}

    decision = decide_action(prompt)
    if decision.get("action") == "SEARCH":
        decision.setdefault("search_query", prompt)
    return decision


AGENT = AgentSpec(
    name="ollama-search",
    route="/chat/ollama-search",
    request_model=PromptRequest,
    handler=run,
    runtime="ollama",
    model=SEARCH_MODEL,
    router=route,
    tool="web_search",
)
