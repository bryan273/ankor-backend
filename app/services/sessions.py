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
    reasoning: Optional[List[Dict[str, Any]]] = None,
) -> str:
    row = await db.fetch_one(
        """
        insert into messages (session_id, role, text, emotion, intensity, intent, urgency,
                              reasoning)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        returning id::text as id
        """,
        (session_id, role, text, emotion, intensity, intent,
         json.dumps(urgency) if urgency else None,
         json.dumps(reasoning or [], default=str)),
    )
    return row["id"]


async def link_attachments(message_id: str, attachment_ids: List[str],
                           session_id: Optional[str] = None) -> None:
    """Bind uploaded photos to the message they were sent with.

    Uploads happen before the message exists — the vision pass runs while the customer is
    still typing — so the row starts with no message and no session. Without this the
    photo is orphaned: the customer sees it in their own bubble because the browser still
    holds it, and the support agent opening that conversation later sees nothing at all.
    """
    if not attachment_ids:
        return
    await db.execute(
        "update attachments set message_id = %s, session_id = coalesce(session_id, %s) "
        "where id::text = any(%s) and message_id is null",
        (message_id, session_id, attachment_ids),
    )


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
        select m.id::text as message_id, m.role, m.text, m.emotion, m.intent,
               m.reasoning, m.created_at
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
    attachments = await db.fetch(
        """
        select a.message_id::text as message_id, a.id::text as attachment_id,
               a.mime, a.vlm_facts
        from attachments a
        join messages m on m.id = a.message_id
        where m.session_id = %s order by a.created_at
        """,
        (session_id,),
    )
    by_att: Dict[str, List[Dict[str, Any]]] = {}
    for a in attachments:
        facts = a["vlm_facts"]
        if isinstance(facts, str):
            facts = json.loads(facts)
        by_att.setdefault(a["message_id"], []).append({
            "id": a["attachment_id"],
            "url": f"/api/v1/attachments/{a['attachment_id']}/raw",
            "caption": ((facts or {}).get("caption") or "")[:200],
        })
    # The tool trail was already being written on every turn and never read back. It is
    # the other half of "why did it say that", so it comes back with the transcript.
    traces = await db.fetch(
        """
        select tt.message_id::text as message_id, tt.tool, tt.ok, tt.ms
        from tool_traces tt join messages m on m.id = tt.message_id
        where m.session_id = %s order by tt.created_at
        """,
        (session_id,),
    )
    by_tool: Dict[str, List[Dict[str, Any]]] = {}
    for t in traces:
        by_tool.setdefault(t["message_id"], []).append(
            {"tool": t["tool"], "ok": t["ok"], "ms": t["ms"]})

    guards = await db.fetch(
        """
        select gh.message_id::text as message_id, gh.rule_id
        from guard_hits gh join messages m on m.id = gh.message_id
        where m.session_id = %s order by gh.id
        """,
        (session_id,),
    )
    by_guard: Dict[str, List[str]] = {}
    for g in guards:
        by_guard.setdefault(g["message_id"], []).append(g["rule_id"])

    for r in rows:
        r["blocks"] = by_message.get(r["message_id"], [])
        r["attachments"] = by_att.get(r["message_id"], [])
        reasoning = r.get("reasoning")
        if isinstance(reasoning, str):
            reasoning = json.loads(reasoning)
        r["reasoning"] = reasoning or []
        r["tools"] = by_tool.get(r["message_id"], [])
        r["guard_hits"] = by_guard.get(r["message_id"], [])
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


async def list_sessions(limit: int = 40, unresolved_only: bool = False) -> List[Dict[str, Any]]:
    """The agent console's inbox: one row per conversation, newest activity first.

    Every turn has always been written to Postgres — messages, blocks, tool traces,
    guard hits — but nothing could ask for the *list*, so the console could only ever
    show the conversation happening in front of it and a page refresh looked like data
    loss. One support agent handles many customers at once; this is the query that makes
    that possible.

    The columns are the ones an agent triages on, not everything we store: who, what
    they last said, how they sounded, whether the conversation is sitting waiting on a
    reply, and whether a ticket already exists.
    """
    rows = await db.fetch(
        """
        with last_msg as (
            select distinct on (m.session_id)
                   m.session_id, m.text, m.role, m.emotion, m.intent, m.created_at
            from messages m
            order by m.session_id, m.created_at desc
        ),
        counts as (
            select session_id, count(*) as n from messages group by session_id
        )
        select s.id::text            as session_id,
               s.resolved_sku,
               s.updated_at,
               c.email               as customer_email,
               c.name                as customer_name,
               p.name                as product_name,
               lm.text               as last_text,
               lm.role               as last_role,
               lm.emotion            as last_emotion,
               lm.intent             as last_intent,
               lm.created_at         as last_at,
               coalesce(cn.n, 0)     as message_count,
               t.ticket_no,
               t.status              as ticket_status,
               -- A turn that paused on a block is a customer sitting and waiting. The
               -- checkpoint lives in sessions.meta rather than its own table.
               (s.meta ? 'checkpoint') as awaiting
        from sessions s
        left join customers c on c.id = s.customer_id
        left join products  p on p.sku = s.resolved_sku
        left join last_msg lm on lm.session_id = s.id
        left join counts   cn on cn.session_id = s.id
        left join lateral (
            select ticket_no, status from tickets tk
            where tk.session_id = s.id order by tk.created_at desc limit 1
        ) t on true
        where cn.n > 0
        order by coalesce(lm.created_at, s.updated_at) desc
        limit %s
        """,
        (limit,),
    )
    if unresolved_only:
        rows = [r for r in rows if r.get("ticket_status") != "resolved"]
    return rows
