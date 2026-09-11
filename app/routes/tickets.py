"""Ticket reads and manual creation."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.deps import require_api_key
from app.errors import NotFound
from app.schemas.api import TicketCreateRequest
from app.services import products as product_svc
from app.services import tickets as ticket_svc

router = APIRouter(tags=["tickets"])


@router.get("/tickets")
async def list_tickets(limit: int = 20, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    return {"tickets": await ticket_svc.recent_tickets(limit)}


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, _: str = Depends(require_api_key)) -> Dict[str, Any]:
    row = await ticket_svc.get_ticket(ticket_id)
    if not row:
        raise NotFound("no such ticket", {"ticket_id": ticket_id})
    return row


@router.post("/tickets")
async def create_ticket(req: TicketCreateRequest,
                        _: str = Depends(require_api_key)) -> Dict[str, Any]:
    product_id = None
    if req.sku:
        row = await product_svc.get_product(req.sku)
        product_id = row["product_id"] if row else None
    return await ticket_svc.create_ticket(
        req.summary, session_id=req.session_id, product_id=product_id,
        priority=req.priority, reason=req.reason)
