"""Tickets — how a conversation stops being a conversation and becomes a commitment.

The brief's phrase is "close the loop". A ticket is that loop closing: something with
an id the customer can quote, a timeline they can watch, and a priority that reflects
what the agent understood about their situation.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

import structlog

from app.clients import db

log = structlog.get_logger(__name__)

PRIORITIES = ("low", "normal", "high", "urgent")

# What each priority promises. Shown to the customer, so it must not be aspirational.
ETA = {"urgent": "within 1 hour", "high": "within 4 hours",
       "normal": "within 1 business day", "low": "within 2 business days"}


def ticket_number() -> str:
    return f"TCK-{random.randint(1000, 9999)}"


def derive_priority(emotion: str, has_deadline: bool, safety: bool,
                    failed_steps: int = 0) -> str:
    """Priority from what the agent observed, not from what the customer demanded.

    Deliberately generous: a frustrated customer with a deadline who has already tried
    two fixes is genuinely more urgent than a calm one, and treating them identically
    is how support earns its reputation.
    """
    if safety:
        return "urgent"
    if emotion == "angry" and has_deadline:
        return "urgent"
    if has_deadline or emotion in ("angry", "frustrated") or failed_steps >= 2:
        return "high"
    if emotion == "anxious":
        return "high"
    return "normal"


async def create_ticket(
    summary: str, *, session_id: Optional[str] = None, customer_id: Optional[str] = None,
    product_id: Optional[str] = None, priority: str = "normal", verdict: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    priority = priority if priority in PRIORITIES else "normal"
    row = await db.fetch_one(
        """
        insert into tickets (ticket_no, session_id, customer_id, product_id, priority,
                             status, summary, verdict)
        values (%s, %s, %s, %s, %s, 'open', %s, %s)
        returning id::text as ticket_id, ticket_no, status, priority, summary, created_at
        """,
        (ticket_number(), session_id, customer_id, product_id, priority, summary, verdict),
    )
    await add_event(row["ticket_id"], "created", {"note": reason or summary,
                                                  "priority": priority})
    log.info("ticket.created", ticket_no=row["ticket_no"], priority=priority)
    return {**row, "eta": ETA.get(priority, ETA["normal"])}


async def add_event(ticket_id: str, kind: str,
                    payload: Optional[Dict[str, Any]] = None) -> None:
    import json
    await db.execute(
        "insert into ticket_events (ticket_id, kind, payload) values (%s, %s, %s)",
        (ticket_id, kind, json.dumps(payload or {})),
    )


async def get_ticket(ticket_id_or_no: str) -> Optional[Dict[str, Any]]:
    row = await db.fetch_one(
        """
        select t.id::text as ticket_id, t.ticket_no, t.status, t.priority, t.summary,
               t.verdict, t.created_at, t.updated_at, p.sku, p.name as product_name
        from tickets t left join products p on p.id = t.product_id
        where t.ticket_no = %s or t.id::text = %s
        """,
        (ticket_id_or_no, ticket_id_or_no),
    )
    if not row:
        return None
    row["timeline"] = await db.fetch(
        "select kind, payload, created_at from ticket_events where ticket_id = %s "
        "order by created_at",
        (row["ticket_id"],),
    )
    row["eta"] = ETA.get(row["priority"], ETA["normal"])
    return row


async def update_status(ticket_id: str, status: str, note: str = "") -> None:
    await db.execute(
        "update tickets set status = %s, updated_at = now() where id::text = %s",
        (status, ticket_id))
    await add_event(ticket_id, status, {"note": note})


async def recent_tickets(limit: int = 20) -> List[Dict[str, Any]]:
    return await db.fetch(
        """
        select t.id::text as ticket_id, t.ticket_no, t.status, t.priority, t.summary,
               t.created_at, p.sku
        from tickets t left join products p on p.id = t.product_id
        order by t.created_at desc limit %s
        """,
        (limit,),
    )
