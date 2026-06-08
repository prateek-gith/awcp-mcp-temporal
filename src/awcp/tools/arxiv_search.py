"""Arxiv academic paper search tool for the AWCP runtime tool registry.

Registered as 'arxiv_search' so it is auto-discovered by discover_tools()
at startup and becomes available to the DynamicAskWorkflow via the MCP
select_runtime_tools / execute_tool pipeline.
"""

import json

import arxiv

from awcp.runtime.tool_runtime import tool


@tool("arxiv_search")
def run_arxiv_search(query: str, max_results: int = 5) -> str:
    """Search arxiv.org for academic papers matching the query.

    Returns a formatted string of papers with title, authors, summary,
    published date, and PDF URL. Use this tool for queries about research,
    papers, studies, preprints, scientific literature, or academic topics.

    Args:
        query: The search query string (e.g. 'transformer attention mechanism').
        max_results: Maximum number of papers to return (default 5, max 10).
    """
    max_results = min(int(max_results), 10)  # safety cap

    try:
        client = arxiv.Client()
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.Relevance,
        )

        papers = []
        for paper in client.results(search):
            papers.append(
                {
                    "title": paper.title,
                    "authors": [str(a) for a in paper.authors[:5]],  # cap to 5 authors
                    "summary": paper.summary[:600] + ("..." if len(paper.summary) > 600 else ""),
                    "published": str(paper.published)[:10],  # YYYY-MM-DD
                    "pdf_url": paper.pdf_url,
                    "entry_id": paper.entry_id,
                }
            )

        if not papers:
            return f"No arxiv papers found for query: {query!r}"

        # Format as readable text for the synthesis LLM
        lines = [f"Arxiv Search Results for: {query!r}\n{'=' * 60}"]
        for i, p in enumerate(papers, start=1):
            lines.append(
                f"\n[{i}] {p['title']}\n"
                f"  Authors  : {', '.join(p['authors'])}\n"
                f"  Published: {p['published']}\n"
                f"  PDF URL  : {p['pdf_url']}\n"
                f"  Abstract : {p['summary']}"
            )

        return "\n".join(lines)

    except Exception as e:
        raise RuntimeError(f"Arxiv search failed: {str(e)}") from e
