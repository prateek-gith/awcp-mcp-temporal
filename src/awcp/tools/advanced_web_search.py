"""Advanced web search tool.

Combines two independent sources and compiles a single result:

  1. DuckDuckGo (``ddgs``) — the existing free, keyless ``web_search`` tool.
  2. Groq agentic web search — an LLM (Groq "compound" model) that performs its
     own live web search. Requires a Groq API key, supplied at call time
     (``groq_api_key`` argument) or via the ``GROQ_API_KEY`` env var. The key is
     never hardcoded; if none is available the tool simply uses DDGS only.

It does NOT always call both. The decision is made from real runtime conditions
(see ``_decide_strategy``), e.g. only escalate to Groq when DDGS comes back empty
or thin, or when the query needs synthesis / very-recent info. When DDGS already
returns strong results for a simple lookup, Groq is skipped to save the quota.

Registering this as a *second* search tool (alongside ``web_search``) is what
lets us exercise multiple distinct tool-call activities in Temporal.
"""

import os
from typing import Any

from awcp.runtime.tool_runtime import tool
from awcp.tools.web_search import run_web_search


GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
# Groq's agentic model that has built-in web search. Override if Groq renames it.
GROQ_SEARCH_MODEL = os.getenv("GROQ_SEARCH_MODEL", "groq/compound")
# DDGS result count below which we consider the keyless search "thin".
MIN_DDGS_RESULTS = int(os.getenv("ADV_SEARCH_MIN_DDGS_RESULTS", "3"))

# Query signals that benefit from an agentic/synthesis search even when DDGS has
# hits: comparisons, reasoning, and freshness-sensitive asks.
_DEEP_SIGNALS = (
    "compare", "comparison", " vs ", "versus", "why ", "how ", "explain",
    "analysis", "pros and cons", "difference between", "latest", "today",
    "current", "this week", "right now", "breaking", "as of",
)


def _count_ddgs_results(text: str) -> int:
    """How many result blocks DDGS produced (its format starts each with 'Result ')."""
    return text.count("Result ") if text else 0


def _needs_deeper_search(query: str) -> bool:
    q = f" {query.lower()} "
    return any(sig in q for sig in _DEEP_SIGNALS)


def _decide_strategy(query: str, ddgs_count: int, has_key: bool) -> tuple[bool, str]:
    """Return (use_groq, human-readable reason) from actual runtime conditions.

    Scenarios where ONLY ONE source is used:
      • No Groq key available           -> DDGS only (cannot call Groq).
      • DDGS already strong + simple ask -> DDGS only (Groq unnecessary).
      • DDGS empty (and key present)     -> Groq only (DDGS failed to find it).
    Scenarios where BOTH are used:
      • DDGS thin but non-empty          -> cross-check / enrich with Groq.
      • Query needs synthesis/freshness  -> corroborate DDGS with Groq.
    """
    if not has_key:
        return False, "no Groq key available -> DuckDuckGo only"
    if ddgs_count == 0:
        return True, "DuckDuckGo returned nothing -> escalating to Groq"
    if ddgs_count < MIN_DDGS_RESULTS:
        return True, f"DuckDuckGo thin ({ddgs_count} results) -> cross-checking with Groq"
    if _needs_deeper_search(query):
        return True, "query needs synthesis/recent info -> corroborating with Groq"
    return False, f"DuckDuckGo sufficient ({ddgs_count} results) -> Groq not needed"


def _groq_search(query: str, api_key: str) -> str:
    """Run Groq's agentic web search and return its answer text."""
    from openai import OpenAI  # lazy import; openai SDK is Groq-compatible

    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    resp = client.chat.completions.create(
        model=GROQ_SEARCH_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a web research assistant. Search the web and answer "
                    "the query with concrete, current facts. Be concise and cite "
                    "sources inline."
                ),
            },
            {"role": "user", "content": query},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


@tool("advanced_web_search")
def run_advanced_web_search(query: str, groq_api_key: str | None = None) -> str:
    """DDGS + Groq advanced search. Uses one or both based on runtime conditions,
    then compiles a single labeled result for the agent to synthesize from."""
    api_key = (groq_api_key or os.getenv("GROQ_API_KEY") or "").strip() or None

    # Source 1 — DuckDuckGo (always tried first: free, fast, keyless).
    ddgs_text = run_web_search(query) or ""
    ddgs_count = _count_ddgs_results(ddgs_text)

    # Decide whether to also consult Groq.
    use_groq, reason = _decide_strategy(query, ddgs_count, has_key=api_key is not None)

    groq_text = ""
    groq_error = ""
    if use_groq:
        try:
            groq_text = _groq_search(query, api_key)
        except Exception as e:
            groq_error = str(e)

    # --- Compile ---
    if not ddgs_text and not groq_text:
        # Nothing found anywhere; mirror web_search's empty contract so the
        # workflow's degradation logic still kicks in.
        return ""

    sources_used = [s for s, used in (("duckduckgo", bool(ddgs_text)),
                                      ("groq", bool(groq_text))) if used]

    parts = [
        "[advanced_web_search]",
        f"decision: {reason}",
        f"sources_used: {', '.join(sources_used) if sources_used else 'none'}",
    ]
    if len(sources_used) > 1:
        parts.append(
            "cross_check: two independent sources gathered below; reconcile them "
            "and flag any disagreement in the final answer."
        )
    if groq_error:
        parts.append(f"note: Groq attempt failed ({groq_error}); using DuckDuckGo only.")

    if ddgs_text:
        parts.append("=== DuckDuckGo results ===\n" + ddgs_text)
    if groq_text:
        parts.append("=== Groq web search ===\n" + groq_text)

    return "\n\n".join(parts)
