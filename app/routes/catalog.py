"""Catalog reads — powers the products page and the picker images."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from app.deps import require_api_key
from app.errors import NotFound
from app.services import kb
from app.services import orders as order_svc
from app.services import products as product_svc

router = APIRouter(tags=["catalog"])


@router.get("/products")
async def list_products(q: str = "", brand: Optional[str] = None,
                        category: Optional[str] = None, limit: int = 24,
                        sort: str = "", _: str = Depends(require_api_key)) -> Dict[str, Any]:
    k = min(limit, 100)
    rows = await product_svc.search_products(q, brand, category, limit=k, sort=sort)
    # The name search is ASCII word matching, so it cannot answer a query written in a
    # script the catalogue is not written in: every product name here is English, and
    # "扫地机器人" shares no word with "eufy Robot Vacuum Omni S2". The embeddings are
    # multilingual and already hold every product, so the same fallback the agent's
    # `search_products` tool uses is what the storefront search box needs too --
    # otherwise the shop answers a Chinese shopper with an empty shelf. Routed back
    # through `by_skus` so a vector hit produces the same collapsed card as a name hit.
    #
    # Over-fetch and demote bundles before trimming, because the two paths otherwise
    # disagree about the same shelf: the scored path already ranks "the thing itself
    # before a bundle of it with two others", the vector path has no such rule, and a
    # bundle's name is longer and mentions more, so it embeds nearer. Measured: "robot
    # vacuum" opened on the plain Omni S2 while 扫地机器人 opened on three accessory
    # bundles. Same query, same shelf, so it should be the same order.
    if q and not rows:
        hits = await kb.search_products_vector(q, k=max(k * 4, 12), category=category)
        skus = [h["sku"] for h in hits if h.get("sku")]
        skus.sort(key=lambda s: s.upper().startswith(("BUNDLE-", "COMBO-")))
        rows = (await product_svc.by_skus(skus))[:k] if skus else []
    return {"count": len(rows), "products": rows}


# Declared before `/products/{sku}`, or FastAPI matches this path as a SKU named
# "facets" and answers 404.
@router.get("/products/facets")
async def product_facets(_: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Brand and category counts over the whole catalog, for the storefront filters."""
    return await product_svc.facets()


@router.get("/products/{sku}")
async def get_product(sku: str, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    row = await product_svc.get_product(sku)
    if not row:
        raise NotFound("no such product", {"sku": sku})
    return row


@router.get("/orders/{order_no}")
async def get_order(order_no: str, email: Optional[str] = None,
                    _: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Deliberately mirrors the agent's view: a dealer invoice is *not* found here.
    That asymmetry is scenario S3, and the API should not paper over it."""
    result = await order_svc.lookup_order(order_no, email)
    if not result.get("found"):
        raise NotFound("order not in the order system",
                       {"order_no": order_no, "reason": result.get("reason"),
                        "hint": "may be a dealer order — the agent checks that directory"})
    return result["order"]


@router.get("/dealers")
async def list_dealers(_: str = Depends(require_api_key)) -> Dict[str, Any]:
    return {"dealers": await order_svc.list_dealers()}
