from ddgs import DDGS

from awcp.runtime.tool_runtime import tool


@tool("web_search")
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
