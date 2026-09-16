"""Health and metrics.

`/healthz` pings every dependency for real rather than reporting "configured". A key
that is present but 403s is worse than a key that is missing, because it fails at
demo time instead of at boot.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from fastapi import APIRouter

from app.clients import db
from app.clients.embed import get_embedder
from app.clients.llm import get_llm
from app.clients.rkapi import get_rkapi
from app.clients.vectors import get_vectors
from app.config import settings

router = APIRouter(tags=["ops"])

_metrics: Dict[str, Any] = {"turns": 0, "errors": 0, "cost_credits": 0.0,
                            "guard_hits": 0, "tool_calls": 0}


def bump(key: str, amount: float = 1) -> None:
    _metrics[key] = _metrics.get(key, 0) + amount


async def _timed(name: str, coro) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(coro, timeout=90)
        return {"ok": True, "ms": int((time.perf_counter() - started) * 1000), "info": result}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": int((time.perf_counter() - started) * 1000),
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


async def _ping_llm() -> str:
    """Probe the model that actually serves turns, not the one that used to.

    This checked RKAPI by name. Once routing moved to DeepSeek that made /healthz
    actively misleading: it would report green off a provider nothing calls, while every
    real turn failed. A health check has to exercise the serving path.
    """
    llm = get_llm()
    text, usage = await llm.complete(
        [{"role": "user", "content": "reply with the single word OK"}], max_tokens=2500)
    model = getattr(llm.text, "model", "?")
    return f"{text.strip()[:20]} ({usage.input}/{usage.output} tok, {model})"


async def _ping_embed() -> str:
    v = await get_embedder().embed_query("robot vacuum reduced suction")
    return f"{len(v)}-d"


async def _ping_vectors() -> str:
    stats = await get_vectors().stats()
    ns = stats.get("namespaces", {})
    return f"{stats.get('totalVectorCount', 0)} vectors across {len(ns)} namespaces"


async def _ping_db() -> str:
    row = await db.fetch_one("select count(*) as n from products")
    return f"{row['n']} products"


@router.get("/healthz")
async def healthz() -> Dict[str, Any]:
    llm, embed, vectors, database = await asyncio.gather(
        _timed("llm", _ping_llm()),
        _timed("embed", _ping_embed()),
        _timed("pinecone", _ping_vectors()),
        _timed("supabase", _ping_db()),
    )
    deps = {"llm": llm, "embed": embed, "pinecone": vectors, "supabase": database}
    ok = all(d["ok"] for d in deps.values())
    return {"status": "ok" if ok else "degraded", "version": settings.version,
            "model": getattr(get_llm().text, "model", "?"),
            "text_provider": settings.text_provider,
            "vision_provider": settings.vision_provider,
            "deps": deps}


@router.get("/livez")
async def livez() -> Dict[str, str]:
    """Cheap liveness — no upstream calls, safe to poll."""
    return {"status": "ok", "version": settings.version}


@router.get("/metrics")
async def metrics() -> Dict[str, Any]:
    lanes = []
    try:
        lanes = get_rkapi().spend_report()
    except Exception:  # noqa: BLE001 — metrics must never be the thing that breaks
        pass
    return {**_metrics, "lanes": lanes}
