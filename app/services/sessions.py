"""Session, message and block persistence.

Also the checkpoint store. When the agent pauses on a `product_picker` it has to be
able to wake up in the same place after the user clicks, possibly after a page reload,
so the paused state is written to `sessions.meta.checkpoint` rather than held in
process memory.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import structlog

from app.clients import db

log = structlog.get_logger(__name__)


async def create_session(customer_id: Optional[str] = None, locale: str = "en") -> str:
    row = await db.fetch_one(
        "insert into sessions (customer_id, locale) values (%s, %s) returning id::text as id",
        (customer_id, locale),
    )
    return row["id"]


async def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    return await db.fetch_one(
        """
        select s.id::text as session_id, s.customer_id::text as customer_id, s.locale,
               s.resolved_sku, s.meta, s.created_at, c.email, c.name
        from sessions s left join customers c on c.id = s.customer_id
        where s.id::text = %s
        """,
        (session_id,),
    )


async def ensure_session(session_id: Optional[str], customer_email: Optional[str] = None,
                         locale: str = "en") -> Dict[str, Any]:
    """Get or create, resolving the customer by email when the client supplied one —
    that link is what lets purchase history disambiguate a product silently."""
    if session_id:
        existing = await get_session(session_id)
        if existing:
            return existing
    customer_id = None
    if customer_email:
        row = await db.fetch_one(
            "select id::text as id from customers where lower(email) = lower(%s)",
            (customer_email,))
        customer_id = row["id"] if row else None
    new_id = await create_session(customer_id, locale)
    return await get_session(new_id)


async def set_resolved_sku(session_id: str, sku: Optional[str]) -> None:
    await db.execute(
        "update sessions set resolved_sku = %s, updated_at = now() where id::text = %s",
        (sku, session_id))


async def add_message(
    session_id: str, role: str, text: str, *, emotion: Optional[str] = None,
    intensity: Optional[float] = None, intent: Optional[str] = None,
    urgency: Optional[Dict[str, Any]] = None,
) -> str:
    row = await db.fetch_one(
        """
        insert into messages (session_id, role, text, emotion, intensity, intent, urgency)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning id::text as id
        """,
        (session_id, role, text, emotion, intensity, intent,
         json.dumps(urgency) if urgency else None),
    )
    return row["id"]


async def history(session_id: str, limit: int = 12) -> List[Dict[str, str]]:
    rows = await db.fetch(
        "select role, text from messages where session_id = %s and text is not null "
        "order by created_at desc limit %s",
        (session_id, limit),
    )
    return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]


async def messages_with_blocks(session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """
        select m.id::text as message_id, m.role, m.text, m.emotion, m.intent, m.created_at
        from messages m where m.session_id = %s order by m.created_at limit %s
        """,
        (session_id, limit),
    )
    if not rows:
        return []
    blocks = await db.fetch(
        """
        select mb.message_id::text as message_id, mb.block_id, mb.type, mb.payload,
               mb.actions, mb.state
        from message_blocks mb
        join messages m on m.id = mb.message_id
        where m.session_id = %s order by mb.created_at
        """,
        (session_id,),
    )
    by_message: Dict[str, List[Dict[str, Any]]] = {}
    for b in blocks:
        by_message.setdefault(b["message_id"], []).append(b)
    for r in rows:
        r["blocks"] = by_message.get(r["message_id"], [])
    return rows


async def save_block(message_id: str, block: Dict[str, Any]) -> None:
    await db.execute(
        """
        insert into message_blocks (message_id, block_id, type, payload, actions, state)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (block_id) do update set payload = excluded.payload,
                                             actions = excluded.actions,
                                             state = excluded.state
        """,
        (message_id, block["block_id"], block["type"], json.dumps(block.get("payload", {}), default=str),
         json.dumps(block.get("actions", []), default=str), block.get("state", "active")),
    )


async def get_block(block_id: str) -> Optional[Dict[str, Any]]:
    return await db.fetch_one(
        """
        select mb.block_id, mb.type, mb.payload, mb.actions, mb.state,
               mb.message_id::text as message_id, m.session_id::text as session_id
        from message_blocks mb join messages m on m.id = mb.message_id
        where mb.block_id = %s
        """,
        (block_id,),
    )


async def answer_block(block_id: str, value: Dict[str, Any]) -> None:
    await db.execute(
        "update message_blocks set state = 'answered', answered_with = %s where block_id = %s",
        (json.dumps(value, default=str), block_id),
    )


async def save_checkpoint(session_id: str, checkpoint: Dict[str, Any]) -> None:
    """Persisted so `interrupt → user clicks → resume` survives a page reload."""
    await db.execute(
        """
        update sessions
        set meta = coalesce(meta, '{}'::jsonb) || jsonb_build_object('checkpoint', %s::jsonb),
            updated_at = now()
        where id::text = %s
        """,
        (json.dumps(checkpoint, default=str), session_id),
    )


async def load_checkpoint(session_id: str) -> Optional[Dict[str, Any]]:
    row = await db.fetch_one(
        "select meta->'checkpoint' as cp from sessions where id::text = %s", (session_id,))
    if not row or not row.get("cp"):
        return None
    cp = row["cp"]
    return json.loads(cp) if isinstance(cp, str) else cp


async def clear_checkpoint(session_id: str) -> None:
    await db.execute(
        "update sessions set meta = coalesce(meta, '{}'::jsonb) - 'checkpoint' "
        "where id::text = %s", (session_id,))


async def save_tool_traces(message_id: str, traces: List[Dict[str, Any]]) -> None:
    if not traces:
        return
    await db.execute_many(
        """
        insert into tool_traces (message_id, call_id, tool, args, result, ms, ok)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        [(message_id, t.get("call_id"), t["tool"], json.dumps(t.get("args", {})),
          json.dumps(t.get("result", {}), default=str), t.get("ms", 0), t.get("ok", True))
         for t in traces],
    )


async def save_guard_hits(message_id: str, hits: List[Dict[str, Any]]) -> None:
    if not hits:
        return
    await db.execute_many(
        "insert into guard_hits (message_id, rule_id, detail, repaired) values (%s,%s,%s,%s)",
        [(message_id, h["rule_id"], h.get("detail", ""), h.get("repaired", False))
         for h in hits],
    )
