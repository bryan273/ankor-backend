"""Catalog reads — powers the products page and the picker images."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from app.deps import require_api_key
from app.errors import NotFound
from app.services import orders as order_svc
from app.services import products as product_svc

router = APIRouter(tags=["catalog"])


@router.get("/products")
async def list_products(q: str = "", brand: Optional[str] = None,
                        category: Optional[str] = None, limit: int = 24,
                        _: str = Depends(require_api_key)) -> Dict[str, Any]:
    rows = await product_svc.search_products(q, brand, category, limit=min(limit, 100))
    return {"count": len(rows), "products": rows}


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
