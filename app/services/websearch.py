"""Web search, restricted to Anker's own domains.

The allowlist is the point. A support agent that quotes a random blog's repair advice
is worse than one that says it does not know: the customer cannot tell the difference,
and the company wears the consequences. Tavily supports domain restriction server-side,
so the filter is applied at the query and again on the results.
"""
from __future__ import annotations

from typing import Any, Dict, List

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

ALLOWED_DOMAINS = [
    "anker.com", "eufy.com", "soundcore.com", "ankersolix.com", "ankerwork.com",
    "nebula.com", "seenebula.com", "support.anker.com", "community.anker.com",
]


def _allowed(url: str) -> bool:
    lowered = (url or "").lower()
    return any(f"{d}" in lowered for d in ALLOWED_DOMAINS)


async def search_official(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    if not settings.tavily_api_key:
        log.debug("websearch.disabled")
        return []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.tavily_api_key,
                    "query": query,
                    "max_results": max_results,
                    "include_domains": ALLOWED_DOMAINS,
                    "search_depth": "basic",
                },
            )
            r.raise_for_status()
            results = r.json().get("results", [])
    except Exception as e:  # noqa: BLE001
        log.warning("websearch.failed", error=str(e)[:160])
        return []

    # Belt and braces: filter again locally in case the provider ignores the allowlist.
    return [
        {"title": x.get("title", ""), "url": x.get("url", ""),
         "snippet": (x.get("content") or "")[:500]}
        for x in results if _allowed(x.get("url", ""))
    ][:max_results]
